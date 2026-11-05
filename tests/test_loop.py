"""Loop tests: the whole tool-calling contract, driven by two fakes.

No network and no subprocess: ``FakeLlm`` replays scripted gateway answers and
``FakeSource`` records tool calls, which is what makes the ugly paths (invalid
arguments, unknown tool, iteration cap) cheap enough to actually pin down.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import pytest

from payback_agent.llm import ChatResult
from payback_agent.loop import append_turn, ask, ask_events


def _tool(name):
    return SimpleNamespace(name=name, description=f"{name} desc",
                           input_schema={"type": "object", "properties": {}})


class FakeLlm:
    def __init__(self, *messages):
        self.scripted = list(messages)
        self.requests: list[tuple[list[dict], list[dict] | None]] = []

    async def chat(self, messages, tools=None):
        self.requests.append((json.loads(json.dumps(messages, default=str)), tools))
        message = self.scripted.pop(0)
        return ChatResult(message=message, finish_reason="stop",
                          usage={"prompt_tokens": 10, "completion_tokens": 5,
                                 "total_tokens": 15})


class FakeStreamLlm(FakeLlm):
    """Same scripted answers, but delivered through chat_stream token by token."""

    async def chat_stream(self, messages, tools=None):
        result = await self.chat(messages, tools)
        text = result.message.get("content") or ""
        for char in text:
            yield ("delta", char)
        yield ("result", result)


class FakeSource:
    def __init__(self, results=None, raises=None):
        self._results = results or {}
        self._raises = raises or {}
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return [_tool(name) for name in sorted(self._results)]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self._raises:
            raise self._raises[name]
        if name not in self._results:
            # what a real MCP session does when the model invents a tool
            raise LookupError(f"unknown tool {name!r}")
        return self._results[name]


def _ask(question, llm, source, *, max_turns=8, trace=None, history=None):
    async def go():
        return await ask(question, llm=llm, source=source, system_prompt="SYS",
                         max_turns=max_turns, trace=trace, history=history)

    return anyio.run(go)


def _events(question, llm, source, *, max_turns=8, trace=None, history=None):
    async def go():
        return [event async for event in ask_events(
            question, llm=llm, source=source, system_prompt="SYS",
            max_turns=max_turns, trace=trace, history=history)]

    return anyio.run(go)


def _call(id_, name, arguments="{}"):
    return {"id": id_, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def test_plain_answer_never_touches_a_tool():
    llm = FakeLlm({"role": "assistant", "content": "涨停 42 家（2026-10-13）"})
    source = FakeSource()
    answer = _ask("昨天涨停几家", llm, source)
    assert answer.content == "涨停 42 家（2026-10-13）"
    assert answer.iterations == 1 and answer.tool_calls == [] and not answer.truncated
    assert source.calls == []
    # system + user only; nothing invented was pushed onto the stack
    assert [m["role"] for m in answer.messages] == ["system", "user"]


def test_tool_result_is_backfilled_and_the_followup_answers():
    llm = FakeLlm(
        {"role": "assistant", "content": None, "tool_calls": [_call("c1", "get_market_overview", '{"date":"yesterday"}')]},
        {"role": "assistant", "content": "42 家，数据日期 2026-10-13"},
    )
    source = FakeSource({"get_market_overview": '{"data_as_of": "2026-10-13", "limit_up_qty": 42}'})
    answer = _ask("昨天涨停几家", llm, source)

    assert answer.content == "42 家，数据日期 2026-10-13"
    assert answer.iterations == 2
    assert source.calls == [("get_market_overview", {"date": "yesterday"})]
    assert answer.tool_calls[0].error is None
    assert json.loads(answer.tool_calls[0].result)["limit_up_qty"] == 42

    # the second request carries: assistant echo (tool_calls intact) + tool message
    second_messages = llm.requests[1][0]
    assert [m["role"] for m in second_messages] == ["system", "user", "assistant", "tool"]
    echo = second_messages[2]
    assert echo["tool_calls"][0]["id"] == "c1"
    assert echo["content"] == ""  # None normalised: some gateways reject null
    tool_message = second_messages[3]
    assert tool_message["tool_call_id"] == "c1"
    assert json.loads(tool_message["content"])["data_as_of"] == "2026-10-13"


def test_server_side_error_contract_reaches_the_model_unchanged():
    llm = FakeLlm(
        {"role": "assistant", "tool_calls": [_call("c1", "get_market_phase", '{"window_days":0}')]},
        {"role": "assistant", "content": "参数写错了"},
    )
    source = FakeSource({"get_market_phase": '{"requested": "today", "error": "window_days must be >= 1"}'})
    answer = _ask("看下", llm, source)
    sent = llm.requests[1][0][3]
    assert json.loads(sent["content"]) == {
        "requested": "today", "error": "window_days must be >= 1"
    }
    assert answer.tool_calls[0].error is None  # the tool ran; the *data* errored


def test_parallel_calls_in_one_turn_are_all_run_and_paired():
    llm = FakeLlm(
        {"role": "assistant", "tool_calls": [
            _call("a", "get_market_overview", '{"date":"yesterday"}'),
            _call("b", "get_limit_up_structure", '{"date":"yesterday"}'),
        ]},
        {"role": "assistant", "content": "两路都拿到了"},
    )
    source = FakeSource({"get_market_overview": "{}", "get_limit_up_structure": "{}"})
    answer = _ask("复盘", llm, source)
    assert [c[0] for c in source.calls] == ["get_market_overview", "get_limit_up_structure"]
    assert len(answer.tool_calls) == 2
    roles = llm.requests[1][0]
    assert [m["tool_call_id"] for m in roles if m["role"] == "tool"] == ["a", "b"]


def test_invalid_json_arguments_are_reported_not_run():
    llm = FakeLlm(
        {"role": "assistant", "tool_calls": [_call("c1", "get_market_overview", "{date: yesterday")]},
        {"role": "assistant", "content": "改好了"},
    )
    source = FakeSource({"get_market_overview": "{}"})
    answer = _ask("复盘", llm, source)

    assert source.calls == []  # never reached the server
    record = answer.tool_calls[0]
    assert record.error and "invalid arguments" in record.error
    assert record.arguments == "{date: yesterday"  # what the model actually sent
    assert "invalid arguments" in json.loads(llm.requests[1][0][3]["content"])["error"]


def test_arguments_that_are_not_an_object_are_rejected_the_same_way():
    llm = FakeLlm(
        {"role": "assistant", "tool_calls": [_call("c1", "get_market_overview", '["yesterday"]')]},
        {"role": "assistant", "content": "ok"},
    )
    source = FakeSource({"get_market_overview": "{}"})
    answer = _ask("复盘", llm, source)
    assert source.calls == []
    assert "must be a JSON object" in answer.tool_calls[0].error


def test_a_hallucinated_tool_becomes_a_tool_message_instead_of_crashing():
    llm = FakeLlm(
        {"role": "assistant", "tool_calls": [_call("c1", "get_stock_tip", "{}")]},
        {"role": "assistant", "content": "这个工具不存在，我改用现有数据回答"},
    )
    source = FakeSource({"get_market_overview": "{}"})
    answer = _ask("推荐一只票", llm, source)
    assert answer.content == "这个工具不存在，我改用现有数据回答"
    payload = json.loads(llm.requests[1][0][3]["content"])
    assert "get_stock_tip" in payload["error"]
    assert answer.tool_calls[0].error.startswith("tool call failed")


def test_iteration_cap_returns_a_flagged_partial_instead_of_looping_forever():
    llm = FakeLlm(*[{"role": "assistant", "tool_calls": [_call(f"c{i}", "get_market_overview")]}
                    for i in range(5)])
    source = FakeSource({"get_market_overview": "{}"})
    answer = _ask("复盘", llm, source, max_turns=3)
    assert answer.truncated is True
    assert answer.iterations == 3
    assert len(answer.tool_calls) == 3


def test_missing_ids_are_backfilled_consistently():
    llm = FakeLlm(
        {"role": "assistant", "tool_calls": [{"function": {"name": "get_market_overview"}}]},
        {"role": "assistant", "content": "好了"},
    )
    source = FakeSource({"get_market_overview": "{}"})
    _ask("复盘", llm, source)
    echo_id = llm.requests[1][0][2]["tool_calls"][0]["id"]
    tool_id = llm.requests[1][0][3]["tool_call_id"]
    assert echo_id and echo_id == tool_id


def test_usage_is_summed_across_iterations():
    llm = FakeLlm({"role": "assistant", "tool_calls": [_call("c1", "get_market_overview")]},
                  {"role": "assistant", "content": "done"})
    answer = _ask("复盘", llm, FakeSource({"get_market_overview": "{}"}))
    assert answer.usage == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def test_tools_reach_the_gateway_in_openai_shape():
    llm = FakeLlm({"role": "assistant", "content": "x"})
    _ask("复盘", llm, FakeSource({"get_market_phase": "{}"}))
    tools = llm.requests[0][1]
    assert [t["function"]["name"] for t in tools] == ["get_market_phase"]
    assert tools[0]["function"]["description"] == "get_market_phase desc"


def test_trace_sees_request_response_and_call_in_order():
    events = []

    class RecordingTrace:
        async def write(self, event):
            events.append(event["type"])

    llm = FakeLlm({"role": "assistant", "tool_calls": [_call("c1", "get_market_overview")]},
                  {"role": "assistant", "content": "done"})
    _ask("复盘", llm, FakeSource({"get_market_overview": "{}"}), trace=RecordingTrace())
    assert events == ["tools", "llm_request", "llm_response", "tool_call",
                      "llm_request", "llm_response"]


def test_history_sits_between_the_system_prompt_and_the_new_question():
    llm = FakeLlm({"role": "assistant", "content": "二板晋级率 17%"})
    history = [{"role": "user", "content": "复盘一下昨天"},
               {"role": "assistant", "content": "① 涨停 42 家……"}]
    _ask("那二板呢", llm, FakeSource(), history=history)
    sent = llm.requests[0][0]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[3]["content"] == "那二板呢"


def test_history_with_a_non_visible_turn_raises_before_the_request():
    llm = FakeLlm({"role": "assistant", "content": "x"})
    with pytest.raises(ValueError, match="non-visible turn"):
        _ask("追问", llm, FakeSource(),
             history=[{"role": "tool", "tool_call_id": "c1", "content": "{}"}])
    assert llm.requests == []


def test_blank_turns_in_the_history_are_dropped():
    llm = FakeLlm({"role": "assistant", "content": "x"})
    _ask("追问", llm, FakeSource(),
         history=[{"role": "user", "content": "   "},
                  {"role": "assistant", "content": "有内容"}])
    assert [m["role"] for m in llm.requests[0][0]] == ["system", "assistant", "user"]


def test_append_turn_keeps_the_pair_and_nothing_else():
    history = append_turn([], "复盘一下昨天", "① 涨停 42 家")
    assert history == [{"role": "user", "content": "复盘一下昨天"},
                       {"role": "assistant", "content": "① 涨停 42 家"}]
    # the previous turn's tool results are in Answer.messages, never in the history
    assert all(m["role"] in ("user", "assistant") for m in history)


def test_append_turn_drops_the_oldest_pairs_at_the_cap():
    history: list[dict] = []
    for index in range(12):
        history = append_turn(history, f"问{index}", f"答{index}", keep=3)
    assert [m["content"] for m in history] == ["问9", "答9", "问10", "答10", "问11", "答11"]


def test_append_turn_ignores_a_turn_the_model_answered_with_nothing():
    assert append_turn([], "问", "  ") == []


def test_ask_events_streams_deltas_then_exactly_one_done():
    llm = FakeStreamLlm({"role": "assistant", "content": "42 家"})
    events = _events("昨天涨停几家", llm, FakeSource())
    kinds = [e["type"] for e in events]
    assert kinds == ["delta"] * 4 + ["done"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "42 家"
    done = events[-1]
    assert done["content"] == "42 家"
    assert done["iterations"] == 1 and done["truncated"] is False
    assert done["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert [m["role"] for m in done["messages"]] == ["system", "user"]


def test_ask_events_announces_tool_calls_before_their_results():
    llm = FakeStreamLlm(
        {"role": "assistant", "content": None,
         "tool_calls": [_call("c1", "get_market_overview", '{"date":"yesterday"}')]},
        {"role": "assistant", "content": "42 家"},
    )
    source = FakeSource({"get_market_overview": '{"limit_up_qty": 42}'})
    events = _events("昨天涨停几家", llm, source)
    kinds = [e["type"] for e in events]
    assert kinds == ["tool_call", "tool_result", "delta", "delta", "delta", "delta", "done"]

    announced = events[0]
    assert announced["iteration"] == 1 and announced["name"] == "get_market_overview"
    assert announced["arguments"] == '{"date":"yesterday"}'  # raw, pre-parse
    resolved = events[1]
    assert resolved["arguments"] == {"date": "yesterday"}  # parsed for the record
    assert resolved["result"] == '{"limit_up_qty": 42}'
    assert resolved["error"] is None and resolved["tool_call_id"] == "c1"
    assert events[-1]["content"] == "42 家" and events[-1]["iterations"] == 2


def test_ask_events_falls_back_to_chat_as_one_delta_per_iteration():
    llm = FakeLlm({"role": "assistant", "content": "42 家"})
    events = _events("昨天涨停几家", llm, FakeSource())
    assert [e["type"] for e in events] == ["delta", "done"]
    assert events[0]["text"] == "42 家"


def test_ask_events_delta_carries_the_iteration_and_done_keeps_the_last_nonempty_one():
    llm = FakeStreamLlm(
        {"role": "assistant", "content": "先看整体",
         "tool_calls": [_call("c1", "get_market_overview")]},
        {"role": "assistant", "content": "最终结论"},
    )
    events = _events("复盘", llm, FakeSource({"get_market_overview": "{}"}))
    deltas = [e for e in events if e["type"] == "delta"]
    assert {e["iteration"] for e in deltas if e["text"] in "先看整体"} == {1}
    assert all(e["iteration"] == 2 for e in deltas[-4:])
    assert events[-1]["content"] == "最终结论"


def test_ask_events_reports_bad_arguments_in_the_tool_result_event():
    llm = FakeStreamLlm(
        {"role": "assistant", "tool_calls": [_call("c1", "get_market_overview", "{oops")]},
        {"role": "assistant", "content": "改好了"},
    )
    events = _events("复盘", llm, FakeSource({"get_market_overview": "{}"}))
    resolved = next(e for e in events if e["type"] == "tool_result")
    assert resolved["arguments"] == "{oops"  # kept raw: that is what was sent
    assert "invalid arguments" in resolved["error"]
    assert "invalid arguments" in json.loads(resolved["result"])["error"]


def test_ask_events_flags_the_iteration_cap_in_done():
    llm = FakeStreamLlm(*[{"role": "assistant", "tool_calls": [_call(f"c{i}", "get_market_overview")]}
                          for i in range(5)])
    events = _events("复盘", llm, FakeSource({"get_market_overview": "{}"}), max_turns=3)
    done = events[-1]
    assert done["truncated"] is True and done["iterations"] == 3
    assert sum(1 for e in events if e["type"] == "tool_result") == 3


def test_ask_events_rejects_a_non_visible_history_turn_before_any_event():
    llm = FakeStreamLlm({"role": "assistant", "content": "x"})
    with pytest.raises(ValueError, match="non-visible turn"):
        _events("追问", llm, FakeSource(),
                history=[{"role": "tool", "tool_call_id": "c1", "content": "{}"}])
    assert llm.requests == []
