"""Evidence bundle tests (--dry-run): every level's data, with no LLM involved."""

from __future__ import annotations

import json

import anyio

from payback_agent.evidence import STEPS, collect_evidence


class FakeSource:
    def __init__(self, results=None, raises=None):
        self._results = results or {}
        self._raises = raises or {}
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self._raises:
            raise self._raises[name]
        return self._results.get(name, json.dumps({"requested": arguments["date"],
                                                   "data_as_of": "2026-10-13"}))


def _collect(source, date="yesterday", levels=None):
    async def go():
        return await collect_evidence(source, date, levels=levels)

    return anyio.run(go)


def test_every_step_is_called_once_in_chain_order_with_the_requested_date():
    source = FakeSource()
    bundle = _collect(source)
    assert [name for name, _ in source.calls] == [name for name, _ in STEPS]
    assert all(args["date"] == "yesterday" for _, args in source.calls)
    assert bundle["date"] == "yesterday"
    assert set(bundle["sections"]) == {name for name, _ in STEPS}


def test_payloads_are_parsed_back_to_dicts():
    source = FakeSource({"get_market_overview": '{"data_as_of": "2026-10-13", "limit_up_qty": 42}'})
    section = _collect(source)["sections"]["get_market_overview"]
    assert section["limit_up_qty"] == 42


def test_windows_are_the_tools_own_defaults_not_something_this_layer_invents():
    source = FakeSource()
    _collect(source)
    by_name = dict(source.calls)
    assert by_name["get_market_phase"]["window_days"] == 60
    assert by_name["get_position_outcomes"]["window_days"] == 250


def test_one_failing_tool_does_not_abort_the_bundle():
    source = FakeSource(raises={"get_market_phase": RuntimeError("boom")})
    bundle = _collect(source)
    assert "error" in bundle["sections"]["get_market_phase"]
    assert bundle["sections"]["get_market_overview"]["data_as_of"] == "2026-10-13"
    assert len(source.calls) == len(STEPS)


def test_a_non_json_result_is_reported_instead_of_raising():
    source = FakeSource({"get_even_board_ladder": "<html>502</html>"})
    section = _collect(source)["sections"]["get_even_board_ladder"]
    assert section["error"] == "tool result is not JSON"
    assert "502" in section["raw"]


def test_level_narrows_the_position_step_only():
    source = FakeSource()
    _collect(source, levels=[2])
    assert dict(source.calls)["get_position_outcomes"]["level"] == 2
    assert "level" not in dict(source.calls)["get_market_phase"]
