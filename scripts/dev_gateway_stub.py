#!/usr/bin/env python
"""DEV gateway stub: an OpenAI-compatible /chat/completions that streams nonsense-free
but clearly-fake answers, so the web chat page can be built and demoed with no LLM
credentials and no network.

Not a test and not packaged: lives in ``scripts/`` so ``uv run pytest`` never
collects it. It speaks exactly the wire dialect ``payback_agent.llm.chat_stream``
parses -- SSE ``data:`` chunks, ``tool_calls`` fragments keyed by ``index``,
``stream_options.include_usage`` honoured with a final usage-only chunk -- so the
agent side runs its real streaming path, its real tool loop and its real MCP
subprocess; only the "model" is made of cardboard.

Scripted behaviour (deterministic, 2 iterations per question):

1. a request whose messages contain no ``tool`` row  -> streams one tool_call for
   ``get_market_overview`` (a real payback-mcp tool, so the tool chip in the UI
   shows a real round trip);
2. a request that carries the tool result            -> streams a four-level-shaped
   final answer whose every number is labelled as stub data.

Run::

    uv run python scripts/dev_gateway_stub.py [--host 127.0.0.1] [--port 8310]

then point the agent at it::

    AGENT_LLM_BASE_URL=http://127.0.0.1:8310/v1
    AGENT_LLM_API_KEY=stub-not-a-secret
    AGENT_LLM_MODEL=stub-model

Exit codes: 0 clean shutdown; anything else is a uvicorn/bind failure.
"""

from __future__ import annotations

import argparse
import json

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

FINAL_ANSWER = """【桩网关示例回答 — 以下所有数字均为演示数据，不是真实行情】

① 当日市场环境：全市场涨停 42 家、跌停 3 家，炸板率 18%，成交额较前一交易日放量 12%——示例口径，仅供联调页面展示。

② 短线生态阶段：以示例规则看处于「发酵期」：连板高度 5 板，二板晋级率 34%（桩数据）。

③ 出手机会：桩网关不做真实判断。真实运行时，这一段的每个数字都必须来自 payback-mcp 工具返回。

④ 买卖点结构性条件：本条为占位文本，用于验证 markdown 渲染、流式打字机效果与长回答的滚动行为。

> 你正在看到的是 `scripts/dev_gateway_stub.py` 生成的假数据。内容由 AI 生成，不构成投资建议。"""

CHUNK_DELAY = 0.015  # seconds per SSE chunk: slow enough to watch the typewriter


def _chunk(delta: dict, finish: str | None = None) -> str:
    payload = {"id": "chatcmpl-stub", "object": "chat.completion.chunk",
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage_chunk() -> str:
    payload = {"id": "chatcmpl-stub", "object": "chat.completion.chunk",
               "choices": [], "usage": {"prompt_tokens": 128, "completion_tokens": 256,
                                        "total_tokens": 384}}
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_tool_call() -> anyio.AsyncIterator[str]:
    yield _chunk({"role": "assistant", "content": None, "tool_calls": [
        {"index": 0, "id": "call_stub_1", "type": "function",
         "function": {"name": "get_market_overview", "arguments": ""}}]})
    arguments = '{"date": "yesterday"}'
    for start in range(0, len(arguments), 6):  # fragments, like a real gateway
        yield _chunk({"tool_calls": [
            {"index": 0, "function": {"arguments": arguments[start:start + 6]}}]})
        await anyio.sleep(CHUNK_DELAY)
    yield _chunk({}, finish="tool_calls")
    yield _usage_chunk()
    yield "data: [DONE]\n\n"


async def _stream_content(text: str) -> anyio.AsyncIterator[str]:
    yield _chunk({"role": "assistant", "content": ""})
    for start in range(0, len(text), 8):
        yield _chunk({"content": text[start:start + 8]})
        await anyio.sleep(CHUNK_DELAY)
    yield _chunk({}, finish="stop")
    yield _usage_chunk()
    yield "data: [DONE]\n\n"


def _completion(body: dict) -> anyio.AsyncIterator[str]:
    messages = body.get("messages") or []
    # A tool row only exists in iteration 2 of the loop, so this is a reliable
    # "did we already call the tool" flag for the scripted two-step dance.
    answered = any(m.get("role") == "tool" for m in messages)
    if answered:
        return _stream_content(FINAL_ANSWER)
    return _stream_tool_call()


async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "body must be JSON"}}, status_code=400)
    if body.get("model") != "stub-model":
        # Loud on purpose: a mis-pointed AGENT_LLM_MODEL should fail here, not
        # silently return stub data to something that expected a real gateway.
        return JSONResponse({"error": {"message": (
            f"stub only serves model 'stub-model', got {body.get('model')!r} -- "
            "check AGENT_LLM_MODEL / AGENT_LLM_BASE_URL")}}, status_code=400)
    if not body.get("stream"):
        # Non-stream fallback so `payback-agent "问题"` (ask -> chat) also works.
        text = FINAL_ANSWER if any(m.get("role") == "tool"
                                   for m in body.get("messages") or []) else ""
        message = {"role": "assistant", "content": text}
        if not text:
            message["tool_calls"] = [{
                "id": "call_stub_1", "type": "function",
                "function": {"name": "get_market_overview",
                             "arguments": '{"date": "yesterday"}'}}]
        return JSONResponse({"id": "chatcmpl-stub", "object": "chat.completion",
                             "choices": [{"index": 0, "message": message,
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 128, "completion_tokens": 256,
                                       "total_tokens": 384}})
    return StreamingResponse(_completion(body), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


app = Starlette(routes=[
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/healthz", lambda _: JSONResponse({"status": "stub"})),
])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8310)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
