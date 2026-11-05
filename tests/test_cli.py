"""CLI wiring tests: exit codes and output shape, with the loop and the transport stubbed.

Nothing here starts a subprocess or reaches a gateway -- ``main`` is exercised
through the paths that fail before either could happen (config errors) and through
``_emit`` directly (output shape).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import anyio
import pytest

from payback_agent import cli, config
from payback_agent.loop import Answer, ToolCallRecord

KEYS = ("AGENT_LLM_BASE_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL",
        "PAYBACK_MCP_DIR", "PAYBACK_MCP_COMMAND", "AGENT_MAX_TURNS")


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", tmp_path / "no-such.env")
    return monkeypatch


def _answer():
    return Answer(
        content="① 环境：涨停 42 家",
        tool_calls=[ToolCallRecord(iteration=1, name="get_market_overview",
                                   arguments={"date": "yesterday"},
                                   result='{"data_as_of": "2026-10-13"}', error=None),
                    ToolCallRecord(iteration=1, name="get_nope", arguments={},
                                   result='{"error": "boom"}', error="tool call failed: boom")],
        iterations=2, truncated=False, usage={"total_tokens": 42},
    )


def test_missing_llm_config_exits_2_and_names_the_keys(clean_env, capsys):
    assert cli.main(["随便问一句"]) == 2
    err = capsys.readouterr().err
    assert "AGENT_LLM_BASE_URL" in err and "AGENT_LLM_API_KEY" in err


def test_bad_max_turns_exits_2(clean_env, capsys):
    clean_env.setenv("AGENT_MAX_TURNS", "lots")
    assert cli.main(["问"]) == 2
    assert "AGENT_MAX_TURNS" in capsys.readouterr().err


def test_unreachable_mcp_command_exits_2(clean_env, capsys):
    clean_env.setenv("AGENT_LLM_BASE_URL", "https://g.test/v1")
    clean_env.setenv("AGENT_LLM_API_KEY", "k")
    clean_env.setenv("AGENT_LLM_MODEL", "m")
    clean_env.setenv("PAYBACK_MCP_COMMAND", "definitely-not-a-real-binary-xyz")
    assert cli.main(["问"]) == 2
    assert "payback-mcp" in capsys.readouterr().err


def test_prose_output_prints_the_answer_then_calls_on_stderr(capsys):
    args = cli._parser().parse_args([])
    cli._emit(_answer(), args)
    captured = capsys.readouterr()
    assert captured.out == "① 环境：涨停 42 家\n"
    assert "get_market_overview" in captured.err
    assert "get_nope" in captured.err and "失败" in captured.err


def test_quiet_suppresses_the_call_list(capsys):
    args = cli._parser().parse_args(["--quiet"])
    cli._emit(_answer(), args)
    assert capsys.readouterr().err == ""


def test_json_output_is_machine_readable(capsys):
    args = cli._parser().parse_args(["--json"])
    cli._emit(_answer(), args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "① 环境：涨停 42 家"
    assert payload["iterations"] == 2 and payload["usage"]["total_tokens"] == 42
    assert [c["name"] for c in payload["tool_calls"]] == ["get_market_overview", "get_nope"]
    assert payload["tool_calls"][1]["error"].startswith("tool call failed")


def test_truncated_answer_is_warned_about(capsys):
    answer = Answer(content="半截话", iterations=8, truncated=True)
    args = cli._parser().parse_args(["--quiet"])
    cli._emit(answer, args)
    captured = capsys.readouterr()
    assert captured.out == "半截话\n"
    assert "最大迭代数 8" in captured.err


def test_console_script_is_wired():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf8"))
    assert data["project"]["scripts"]["payback-agent"] == "payback_agent.cli:main"
    assert callable(cli.main)


def test_ask_one_forwards_the_session_history(monkeypatch):
    """The REPL's memory is one line of wiring, so pin that line: without it every
    follow-up question restarts from zero and the session is a series of one-shots."""
    seen: dict = {}

    async def fake_ask(question, **kwargs):
        seen["question"] = question
        seen["history"] = kwargs["history"]
        return Answer(content="ok")

    monkeypatch.setattr(cli, "ask", fake_ask)
    args = cli._parser().parse_args([])
    args.max_turns = 8
    anyio.run(cli._ask_one, object(), object(), "那二板呢", "SYS", args, None,
              [{"role": "user", "content": "复盘"}])
    assert seen["question"] == "那二板呢"
    assert seen["history"] == [{"role": "user", "content": "复盘"}]
