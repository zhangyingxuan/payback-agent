"""Trace tests: the JSONL must be replayable, so ordering and round-tripping matter."""

from __future__ import annotations

import json
from datetime import date

import anyio

from payback_agent.trace import TraceWriter, open_trace, read_trace


def _write(path, events):
    async def go():
        writer = TraceWriter(path)
        try:
            for event in events:
                await writer.write(event)
        finally:
            await writer.aclose()

    anyio.run(go)
    return read_trace(path)


def test_events_are_appended_in_order_with_sequence_and_timestamp(tmp_path):
    path = tmp_path / "run.jsonl"
    records = _write(path, [{"type": "tools", "names": ["a"]},
                            {"type": "llm_request", "iteration": 1}])
    assert [r["seq"] for r in records] == [1, 2]
    assert [r["type"] for r in records] == ["tools", "llm_request"]
    assert all(r["ts"].startswith("20") and r["ts"].endswith("+00:00") for r in records)


def test_non_ascii_survives_the_round_trip(tmp_path):
    path = tmp_path / "run.jsonl"
    records = _write(path, [{"type": "tool_call", "result": '{"名称": "涨停", "备注": "中文"}'}])
    assert json.loads(records[0]["result"]) == {"名称": "涨停", "备注": "中文"}


def test_unserialisable_values_are_stringified_instead_of_killing_the_trace(tmp_path):
    path = tmp_path / "run.jsonl"
    records = _write(path, [{"type": "x", "when": date(2026, 10, 13)}])
    assert records[0]["when"] == "2026-10-13"


def test_sequence_is_per_writer_and_files_are_appended(tmp_path):
    path = tmp_path / "run.jsonl"
    _write(path, [{"type": "a"}])
    records = _write(path, [{"type": "b"}])  # second run: seq restarts, line is appended
    assert [r["type"] for r in records] == ["a", "b"]
    assert [r["seq"] for r in records] == [1, 1]


def test_open_trace_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "run.jsonl"

    async def go():
        async with open_trace(path) as writer:
            await writer.write({"type": "hello"})

    anyio.run(go)
    assert read_trace(path)[0]["type"] == "hello"


def test_open_trace_without_a_path_is_a_noop_writer():
    async def go():
        async with open_trace(None) as writer:
            await writer.write({"type": "dropped"})
        return True

    assert anyio.run(go) is True
