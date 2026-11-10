"""The tool-calling loop: the one piece of "agent" in this repo.

No httpx, no MCP import -- the LLM and the ToolSource are injected, so the whole
contract (stack management, assistant-message echo, tool-result backfill, invalid
arguments, unknown tools, iteration cap) is unit-testable with two fakes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    echoed = {"role": "assistant", "content": message.get("content") or "",
              "tool_calls": echoed_calls}
    # 部分推理模型要求工具调用后回传原始推理字段。
    if message.get("reasoning_content"):
        echoed["reasoning_content"] = message["reasoning_content"]
    return echoed


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
    # 只接收用户可见的历史文本，避免旧工具结果混入新一轮证据。
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
    evidence: dict[str, dict] = {}
    failed: dict[str, str] = {}
    stream = getattr(llm, "chat_stream", None)

    # 模型可以多轮请求工具；设置上限防止反复调用导致请求无法结束。
    for iteration in range(1, max_turns + 1):
        if trace is not None:
            await trace.write({"type": "llm_request", "iteration": iteration,
                               "messages": messages, "tools": tools})
        if stream is not None:
            result = None
            async for kind, payload in stream(messages, tools):
                if kind == "reasoning_delta":
                    yield {"type": kind, "iteration": iteration, "text": payload}
                elif kind == "result":
                    result = payload
        else:
            result = await llm.chat(messages, tools)
            if result.message.get("reasoning_content"):
                yield {"type": "reasoning_delta", "iteration": iteration,
                       "text": result.message["reasoning_content"]}
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
            # 先核对证据再展示正文，避免未经核验的数字在流式输出中短暂出现。
            requested_code = re.search(r'(?<!\d)\d{6}(?!\d)', question)
            # 涉及具体个股的次日操作或走势必须有同一标的证据，不能仅凭模型记忆作答。
            stock_plan = bool(re.search(r'支撑|压力|个股.*预案|\d{6}.*预案', question)) or bool(
                (requested_code or re.search(r'个股|股票', question))
                and re.search(r'明天|明日|次日|下一交易日|操作|策略|预案|走势|分析|怎么看', question)
            )
            stock_evidence = evidence.get("get_stock_review", {})
            stock_code = str(stock_evidence.get("stock", {}).get("thscode", ""))
            stock_missing = stock_plan and (not stock_evidence or (requested_code and not stock_code.startswith(requested_code.group())))
            if stock_missing or ((not evidence or failed) and (re.search(r'\d', content) or re.search(r'行情|复盘|涨停|跌停|出手|市场', question))):
                content = "证据不足：本轮数据缺失或查询失败，无法判断。"
            notes = []
            if failed:
                notes.append("部分数据查询失败或缺失：" + "、".join(failed))
            dates = {str(value.get("data_as_of")) for value in evidence.values() if value.get("data_as_of")}
            if dates:
                label = "数据日期不一致" if len(dates) > 1 else "实际数据日期"
                notes.append(label + "：" + "、".join(f"{name}={value.get('data_as_of')}" for name, value in evidence.items() if value.get("data_as_of")))
            today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            future_plan = bool(stock_evidence and re.search(r'明日|明天|次日|下一交易日', question))
            if today in dates:
                if not future_plan and re.search(r'今天|今日|现在|当前|盘中|出手', question):
                    content = "证据不足：当日数据的完成状态未知，无法判断当前盘面。"
                notes.append("当日数据的收盘完成状态和原始数据更新时间未知，不可据此认定为实时或完整收盘数据。")
            elif dates and not future_plan and re.search(r'今天|今日|现在|当前|盘中|出手', question):
                content = "证据不足：历史数据不能用于判断当前盘面。"
                notes.append("所用数据并非今日数据，无法据此判断当前盘面；原始数据更新时间未知。")
            if notes:
                content = content.strip() + "\n\n数据提示：" + "；".join(notes)
            if content.strip():
                yield {"type": "delta", "iteration": iteration, "text": content.strip()}
            yield {"type": "done", "content": content.strip(), "iterations": iteration,
                   "truncated": False, "usage": usage, "messages": messages}
            return

        # 先回放模型的工具请求，再用相同 ID 回填结果，满足对话协议。
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
            # 只有可解析且未报错的数据才作为回答依据，失败信息单独保留。
            if error is None:
                try:
                    payload = json.loads(outcome)
                except (ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    if payload.get("error") or payload.get("found") is False:
                        error = str(payload.get("error") or payload.get("hint") or "数据缺失")
                    elif payload.get("data_as_of") or (name == "get_global_quote" and payload.get("price") is not None):
                        evidence[name] = payload
            if error:
                failed[name] = error
            else:
                failed.pop(name, None)
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
    # 迭代上限时模型尚未基于最后一次工具结果回答，中间文本不能当作结论。
    yield {"type": "done", "content": "证据处理未完成，无法判断。", "iterations": max_turns,
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
