"""LLM client tests via ``httpx.MockTransport``: the real request builder runs,
the network does not."""

from __future__ import annotations

import json

import anyio
import httpx
import pytest

from payback_agent.config import LlmConfig
from payback_agent.llm import ChatClient, LlmError

CFG = LlmConfig(base_url="https://gateway.test/v1", api_key="sk-secret", model="glm-4.5")


def _client(handler, **kw):
    return ChatClient(CFG, transport=httpx.MockTransport(handler), **kw)


def _gateway(message, *, finish_reason="stop", usage=None, extra=None):
    body = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage is not None:
        body["usage"] = usage
    if extra:
        body.update(extra)
    return httpx.Response(200, json=body)


def _chat(client, messages=None, tools=None):
    async def go():
        try:
            return await client.chat(messages or [{"role": "user", "content": "hi"}], tools)
        finally:
            await client.aclose()

    return anyio.run(go)


def test_chat_returns_the_assistant_message_usage_and_finish_reason():
    message = {"role": "assistant", "content": "42 家"}
    client = _client(lambda request: _gateway(
        message, usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}))
    result = _chat(client)
    assert result.message == message
    assert result.finish_reason == "stop"
    assert result.usage["total_tokens"] == 14


def test_request_body_and_url_are_the_openai_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return _gateway({"role": "assistant", "content": "ok"})

    _chat(_client(handler), [{"role": "user", "content": "问"}],
          tools=[{"type": "function", "function": {"name": "x"}}])

    assert seen["url"] == "https://gateway.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-secret"
    body = seen["body"]
    assert body["model"] == "glm-4.5"
    assert body["messages"] == [{"role": "user", "content": "问"}]
    assert body["tools"][0]["function"]["name"] == "x"
    assert body["tool_choice"] == "auto"


def test_no_tools_means_no_tools_fields():
    """A tools-less request with ``tool_choice: auto`` is a 400 on strict gateways."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _gateway({"role": "assistant", "content": "ok"})

    _chat(_client(handler))
    assert "tools" not in seen["body"]
    assert "tool_choice" not in seen["body"]


def test_base_url_without_a_trailing_slash_is_joined_once():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return _gateway({"role": "assistant", "content": "ok"})

    client = ChatClient(LlmConfig(base_url="https://gateway.test/v1/", api_key="k", model="m"),
                        transport=httpx.MockTransport(handler))
    _chat(client)
    assert seen["url"] == "https://gateway.test/v1/chat/completions"


def test_http_error_becomes_llm_error_without_leaking_the_key():
    client = _client(lambda request: httpx.Response(401, text='{"error":{"message":"invalid api key"}}'))
    with pytest.raises(LlmError) as excinfo:
        _chat(client)
    assert "401" in str(excinfo.value)
    assert "sk-secret" not in str(excinfo.value)


def test_transport_failure_is_an_llm_error_not_a_httpx_one():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LlmError, match="gateway unreachable"):
        _chat(_client(handler))


def test_unusable_bodies_are_rejected_with_a_snippet():
    for response, expected in [
        (httpx.Response(200, text="not json at all"), "non-JSON"),
        (httpx.Response(200, json={"choices": []}), "no choices"),
        (httpx.Response(200, json={"choices": [{"message": None}]}), "no message"),
    ]:
        with pytest.raises(LlmError, match=expected):
            _chat(_client(lambda request, r=response: r))


# --- chat_stream -----------------------------------------------------------


def _sse(*chunks, done=True, status=200, content_type="text/event-stream"):
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(status, content=body.encode("utf-8"),
                          headers={"content-type": content_type})


def _delta(text=None, tool_calls=None, role=None, finish=None):
    delta = {}
    if role:
        delta["role"] = role
    if text is not None:
        delta["content"] = text
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _stream(client, messages=None, tools=None):
    async def go():
        events = []
        try:
            async for event in client.chat_stream(
                    messages or [{"role": "user", "content": "hi"}], tools):
                events.append(event)
        finally:
            await client.aclose()
        return events

    return anyio.run(go)


def test_stream_yields_deltas_then_one_assembled_result():
    client = _client(lambda request: _sse(
        _delta("昨天", role="assistant"), _delta("涨停 "), _delta("42 家"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"total_tokens": 21}}))
    events = _stream(client)
    assert [e for e in events if e[0] == "delta"] == \
        [("delta", "昨天"), ("delta", "涨停 "), ("delta", "42 家")]
    kind, result = events[-1]
    assert kind == "result"
    assert result.message == {"role": "assistant", "content": "昨天涨停 42 家"}
    assert result.finish_reason == "stop" and result.usage["total_tokens"] == 21


def test_stream_request_asks_for_streaming_and_usage():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _sse(_delta("ok", role="assistant"))

    _stream(_client(handler))
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}


def test_streamed_tool_call_fragments_are_assembled_by_index():
    """The wire splits one call's arguments across chunks; a half string is not
    JSON, so assembly must happen here, not in the loop."""
    client = _client(lambda request: _sse(
        _delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                            "function": {"name": "get_market_overview", "arguments": ""}}],
               role="assistant"),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"date"'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": ': "yesterday"}'}}]),
        _delta(tool_calls=[{"index": 1, "id": "call_b", "type": "function",
                            "function": {"name": "get_market_phase", "arguments": "{}"}}]),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}))
    events = _stream(client)
    assert [e for e in events if e[0] == "delta"] == []
    message = events[-1][1].message
    assert [call["id"] for call in message["tool_calls"]] == ["call_a", "call_b"]
    assert message["tool_calls"][0]["function"] == {
        "name": "get_market_overview", "arguments": '{"date": "yesterday"}'}
    assert events[-1][1].finish_reason == "tool_calls"


def test_a_gateway_that_ignores_stream_falls_back_to_the_json_body():
    client = _client(lambda request: httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "42"},
                                "finish_reason": "stop"}],
                   "usage": {"total_tokens": 5}},
        headers={"content-type": "application/json"}))
    events = _stream(client)
    assert [kind for kind, _ in events] == ["result"]
    assert events[0][1].message["content"] == "42"


def test_malformed_sse_lines_are_skipped_not_fatal():
    body = ('data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
            ": keep-alive comment\n\n"
            "data: {broken json\n\n"
            'data: {"choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n")
    client = _client(lambda request: httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}))
    events = _stream(client)
    assert [text for kind, text in events if kind == "delta"] == ["a", "b"]


def test_stream_http_error_becomes_llm_error():
    client = _client(lambda request: _sse(_delta("x"), done=False, status=429,
                                          content_type="application/json"))
    with pytest.raises(LlmError, match="429"):
        _stream(client)


def test_stream_transport_failure_is_an_llm_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LlmError, match="gateway unreachable"):
        _stream(_client(handler))
