"""The tool-calling loop: the one piece of "agent" in this repo.

No httpx, no MCP import -- the LLM and the ToolSource are injected, so the whole
contract (stack management, assistant-message echo, tool-result backfill, invalid
arguments, unknown tools, iteration cap) is unit-testable with two fakes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from .bridge import openai_tools


class Llm(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> Any:
        ...
    # Optional: ``chat_stream(messages, tools)`` yielding ("delta", text) tuples
    # then one ("result", ChatResult). ask_events uses it when present and falls
    # back to chat() otherwise, so both shapes satisfy this protocol.


class ToolSource(Protocol):
    async def list_tools(self) -> Any:
        ...

    async def call_tool(self, name: str, arguments: dict) -> Any:
        ...


@dataclass(frozen=True)
class ToolCallRecord:
    iteration: int
    name: str
    arguments: Any
    result: str
    error: str | None = None


@dataclass(frozen=True)
class Answer:
    content: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    iterations: int = 0
    truncated: bool = False
    usage: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)


# How many earlier visible turns a REPL session carries. Tool results are never
# replayed (see ``append_turn``), so this is what bounds the request growth.
MAX_HISTORY_TURNS = 8


def append_turn(history: list[dict], question: str, content: str,
                *, keep: int = MAX_HISTORY_TURNS) -> list[dict]:
    """Return the history for the next REPL question: the visible turns only.

    Prior tool results are deliberately not carried over. Two reasons, both real:
    one four-level review is ~29k characters of JSON, so replaying it turns the
    third question into a context overflow; and by the time the user asks again,
    those numbers are already stale -- the model has to call the tools again, which
    is exactly the discipline ``prompts/system.md`` demands ("数字只能来自工具返回").
    The previous answer text carries the narrative; the data carries nothing.
    """
    if not content.strip():
        return list(history)
    turn = [{"role": "user", "content": question},
            {"role": "assistant", "content": content}]
    return (list(history) + turn)[-keep * 2:]


def _error_payload(reason: str) -> str:
    """The tool-message body used when the call never reached a real tool.

    Shaped like payback-mcp's own error payload (``{"requested", "error"}``) minus
    the date, because the model is trained by the server's contract to read
    ``error`` and change course -- a prose apology here would be parsed as data.
    """
    return json.dumps({"error": reason}, ensure_ascii=False)


def _echo_assistant(message: dict, ids: list[str], iteration: int) -> dict:
    """Copy the assistant message onto the stack, with tool_call ids made stable.

    Some gateways omit ``id``; a tool result then has nothing to point at and the
    next request is rejected. The ids are generated from (iteration, index) so the
    assistant echo and the tool messages below always agree on the pairing.
    """
    calls = message.get("tool_calls") or []
    echoed_calls = []
    for index, call in enumerate(calls):
        function = call.get("function") or {}
        call_id = call.get("id") or f"call_{iteration}_{index}"
        ids[index] = call_id
        echoed_calls.append(
            {"id": call_id, "type": "function",
             "function": {"name": function.get("name") or "",
                          "arguments": function.get("arguments") or "{}"}}
        )
    return {"role": "assistant", "content": message.get("content") or "",
            "tool_calls": echoed_calls}


async def ask_events(
    question: str,
    *,
    llm: Llm,
    source: ToolSource,
    system_prompt: str,
    max_turns: int = 8,
    trace: Any = None,
    history: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Run the tool-calling loop as an event stream -- the kernel everything else wraps.

    Events, in order of appearance:
      ``{"type": "delta", "iteration", "text"}``      answer text as it arrives
      ``{"type": "tool_call", "iteration", "name", "arguments"}``
                                                       before execution (arguments may be
                                                       the raw string when unparsable)
      ``{"type": "tool_result", "iteration", "name", "arguments", "result",
        "error", "tool_call_id"}``                     after execution
      ``{"type": "done", "content", "iterations", "truncated", "usage", "messages"}``
                                                       exactly once, last

    A UI can render deltas as a typewriter and tool_call/tool_result as progress
    chips; ``done.content`` is authoritative and replaces whatever was streamed
    (the model may emit text in an iteration that later turns out not to be the
    last, and only the last non-empty one counts -- same rule ``ask`` always had).
    ``LlmError`` propagates: a dead gateway is the caller's problem, not an event.

    ``history`` is the visible turns of an ongoing session (see ``append_turn``);
    it sits between the system prompt and this question, so a follow-up like
    "那二板呢" is answerable without a fresh four-level chain every time. Anything
    but a user/assistant text turn is a caller bug and raises before the request
    goes out -- a silently mangled stack is worse than a loud one.
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for turn in history or []:
        if turn.get("role") not in ("user", "assistant") or not isinstance(
                turn.get("content"), str):
            raise ValueError(f"history holds a non-visible turn: {turn!r}")
        if turn["content"].strip():
            messages.append(dict(turn))
    messages.append({"role": "user", "content": question})
    tools = openai_tools(await source.list_tools())
    if trace is not None:
        await trace.write({"type": "tools", "names": [t["function"]["name"] for t in tools]})

    usage: dict = {}
    content = ""
    stream = getattr(llm, "chat_stream", None)

    for iteration in range(1, max_turns + 1):
        if trace is not None:
            await trace.write({"type": "llm_request", "iteration": iteration,
                               "messages": messages, "tools": tools})
        if stream is not None:
            result = None
            async for kind, payload in stream(messages, tools):
                if kind == "delta":
                    yield {"type": "delta", "iteration": iteration, "text": payload}
                else:  # ("result", ChatResult) -- exactly one, terminal
                    result = payload
        else:
            result = await llm.chat(messages, tools)
            text = result.message.get("content") or ""
            if text:
                yield {"type": "delta", "iteration": iteration, "text": text}
        message = result.message
        if (message.get("content") or "").strip():
            content = message["content"]
        if trace is not None:
            await trace.write({"type": "llm_response", "iteration": iteration,
                               "message": message, "finish_reason": result.finish_reason,
                               "usage": result.usage})
        for key, value in (result.usage or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value

        calls = message.get("tool_calls") or []
        if not calls:
            yield {"type": "done", "content": content.strip(), "iterations": iteration,
                   "truncated": False, "usage": usage, "messages": messages}
            return

        ids = [""] * len(calls)
        messages.append(_echo_assistant(message, ids, iteration))

        for index, call in enumerate(calls):
            function = call.get("function") or {}
            name = function.get("name") or ""
            raw = function.get("arguments")
            yield {"type": "tool_call", "iteration": iteration, "name": name,
                   "arguments": raw}
            error: str | None = None
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if not isinstance(arguments, dict):
                    raise ValueError(
                        f"arguments must be a JSON object, got {type(arguments).__name__}")
            except ValueError as exc:
                arguments = raw  # keep what the model actually sent, for the trace
                error = f"invalid arguments ({exc})"
                outcome = _error_payload(
                    f"invalid arguments: {exc}; retry with a JSON object")
            else:
                try:
                    outcome = await source.call_tool(name, arguments)
                except Exception as exc:  # unknown tool, transport hiccup, server crash
                    error = f"tool call failed: {exc}"
                    outcome = _error_payload(f"tool {name!r} failed: {exc}")
            if trace is not None:
                await trace.write({"type": "tool_call", "iteration": iteration,
                                   "tool_call_id": ids[index], "name": name,
                                   "arguments": arguments, "result": outcome,
                                   "error": error})
            yield {"type": "tool_result", "iteration": iteration, "name": name,
                   "arguments": arguments, "result": outcome, "error": error,
                   "tool_call_id": ids[index]}
            messages.append({"role": "tool", "tool_call_id": ids[index], "content": outcome})

    # Iteration cap: hand back whatever the model last said, flagged. Silence here
    # would read as "the agent finished thinking", which is the opposite of true.
    yield {"type": "done", "content": content.strip(), "iterations": max_turns,
           "truncated": True, "usage": usage, "messages": messages}


async def ask(
    question: str,
    *,
    llm: Llm,
    source: ToolSource,
    system_prompt: str,
    max_turns: int = 8,
    trace: Any = None,
    history: list[dict] | None = None,
) -> Answer:
    """Answer one question, calling tools until the model stops or ``max_turns`` runs out.

    The non-streaming facade over ``ask_events``: same contract, same Answer, the
    deltas simply discarded. CLI one-shot mode and the tests use this; serve mode
    consumes the event stream directly so the two can never drift apart.
    """
    records: list[ToolCallRecord] = []
    done: dict = {}
    async for event in ask_events(question, llm=llm, source=source,
                                  system_prompt=system_prompt, max_turns=max_turns,
                                  trace=trace, history=history):
        if event["type"] == "tool_result":
            records.append(ToolCallRecord(
                iteration=event["iteration"], name=event["name"],
                arguments=event["arguments"], result=event["result"],
                error=event["error"]))
        elif event["type"] == "done":
            done = event
    return Answer(content=done["content"], tool_calls=records,
                  iterations=done["iterations"], truncated=done["truncated"],
                  usage=done["usage"], messages=done["messages"])
