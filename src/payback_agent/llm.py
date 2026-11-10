"""The only place that talks to the LLM gateway (OpenAI-compatible Chat Completions).

Deliberately raw ``httpx`` instead of the ``openai`` SDK: this repo exists to learn
what a tool-calling loop actually sends over the wire, and an SDK that builds the
request body for you hides exactly the part worth learning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import LlmConfig

CHAT_PATH = "/chat/completions"
DEFAULT_TIMEOUT = 120.0

# What ``chat_stream`` yields: ("delta", str) per content token as it arrives,
# then exactly one ("result", ChatResult) with the fully assembled message.
StreamEvent = tuple[str, Any]


class LlmError(Exception):
    """Gateway refused or answered unusably. Never contains the API key."""


@dataclass(frozen=True)
class ChatResult:
    message: dict
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)


def _result_from_data(data: Any) -> ChatResult:
    """Turn one non-streaming Chat Completions body into a ``ChatResult``.

    Shared by ``chat`` and the ``chat_stream`` fallback so the two paths cannot
    drift on what counts as a usable response.
    """
    choices = data.get("choices") or []
    if not choices:
        raise LlmError(f"gateway returned no choices: {str(data)[:200]}")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LlmError("gateway choice has no message object")
    usage = data.get("usage")
    return ChatResult(
        message=message,
        finish_reason=choices[0].get("finish_reason"),
        usage=usage if isinstance(usage, dict) else {},
    )


class ChatClient:
    def __init__(self, config: LlmConfig, *, timeout: float = DEFAULT_TIMEOUT,
                 temperature: float = 0.2, transport: httpx.AsyncBaseTransport | None = None):
        # The trailing slash is stripped where the join happens, not only in
        # get_llm_config: base_url is handed to httpx as one opaque string, so a
        # double slash survives into the request line and 404s on strict gateways.
        self._url = f"{config.base_url.rstrip('/')}{CHAT_PATH}"
        self._config = config
        self._temperature = temperature
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,  # tests inject httpx.MockTransport; production stays None
            headers={"Authorization": f"Bearer {config.api_key}",
                     "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[dict], tools: list[dict] | None,
                 *, stream: bool = False) -> dict:
        payload: dict = {"model": self._config.model, "messages": messages,
                         "temperature": self._temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            payload["stream"] = True
            # Without this the final usage chunk is omitted by most gateways,
            # and a streamed answer would report no token counts at all.
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        try:
            response = await self._client.post(self._url,
                                               json=self._payload(messages, tools))
        except httpx.HTTPError as exc:
            raise LlmError(f"gateway unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            # Body first 500 chars: gateway reasons are usually in there, and the
            # request id matters. The key is never echoed by the server, and we
            # never print the request, so this stays safe to show a user.
            raise LlmError(f"gateway HTTP {response.status_code}: {response.text[:500]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmError(f"gateway returned non-JSON: {response.text[:200]}") from exc
        return _result_from_data(data)

    async def chat_stream(self, messages: list[dict],
                          tools: list[dict] | None = None) -> AsyncIterator[StreamEvent]:
        """Same request as ``chat`` with ``stream: true``, yielding as it arrives.

        Two shapes a gateway may answer with, both handled:

        * ``text/event-stream``: ``data: {chunk}`` lines. Content deltas are
          yielded immediately; ``tool_calls`` arrive as *fragments* keyed by
          ``index`` (id and name once, ``arguments`` split across chunks) and are
          assembled here, because a half-parsed arguments string is not JSON and
          must never reach ``json.loads`` in the loop.
        * plain JSON (a proxy that ignores ``stream``): parsed exactly like
          ``chat`` does, yielded as one result with no deltas.

        A malformed ``data:`` line is skipped, not fatal: keep-alive comments and
        one corrupt chunk must not kill an otherwise healthy answer.
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: dict = {}
        role = "assistant"
        try:
            async with self._client.stream(
                    "POST", self._url, json=self._payload(messages, tools, stream=True)) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    raise LlmError(f"gateway HTTP {response.status_code}: {body[:500]}")
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    body = (await response.aread()).decode(errors="replace")
                    try:
                        data = json.loads(body)
                    except ValueError as exc:
                        raise LlmError(f"gateway returned non-JSON: {body[:200]}") from exc
                    result = _result_from_data(data)
                    reasoning = result.message.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        yield ("reasoning_delta", reasoning)
                    yield ("result", result)
                    return
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except ValueError:
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("role"):
                        role = delta["role"]
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_parts.append(reasoning)
                        yield ("reasoning_delta", reasoning)
                    text = delta.get("content")
                    if text:
                        content_parts.append(text)
                        yield ("delta", text)
                    for fragment in delta.get("tool_calls") or []:
                        index = fragment.get("index") or 0
                        slot = tool_calls.setdefault(
                            index, {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
                        if fragment.get("id"):
                            slot["id"] = fragment["id"]
                        if fragment.get("type"):
                            slot["type"] = fragment["type"]
                        function = fragment.get("function") or {}
                        slot["function"]["name"] += function.get("name") or ""
                        slot["function"]["arguments"] += function.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        except httpx.HTTPError as exc:
            raise LlmError(f"gateway unreachable: {type(exc).__name__}: {exc}") from exc

        message: dict = {"role": role, "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        yield ("result", ChatResult(message=message, finish_reason=finish_reason, usage=usage))
