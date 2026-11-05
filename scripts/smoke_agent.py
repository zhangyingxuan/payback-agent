#!/usr/bin/env python
"""LIVE smoke for payback-agent against a real payback-mcp subprocess -- no LLM.

Not a unit test: it lives in ``scripts/`` and is named ``smoke_agent.py`` so
``uv run pytest`` never collects it, and it spawns the actual stdio server the way
the CLI does (``uv --directory <payback-mcp> run payback-mcp``).

What it proves, in order:

1. the cross-repo seam works -- the server boots, ``initialize`` handshake completes,
   ``list_tools`` returns the shipped registry;
2. the MCP -> OpenAI bridge keeps the shape a gateway will accept (name /
   description / parameters, ``required`` visible so a missing-arg 400 is diagnosable);
3. the ``--dry-run`` evidence bundle reaches real data for every level of the chain,
   with each tool's ``data_as_of`` and sample sizes printed next to it.

Run::

    uv run python scripts/smoke_agent.py [--for-date yesterday] [--level N]

Exit codes: 0 = every tool answered with data; 1 = at least one tool returned an
error/miss payload (the bundle is printed either way, so you can see which);
2 = could not launch the server (bad PAYBACK_MCP_DIR / missing uv / crash at boot).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import anyio  # noqa: E402

from payback_agent import config  # noqa: E402
from payback_agent.bridge import openai_tools  # noqa: E402
from payback_agent.evidence import STEPS, collect_evidence  # noqa: E402
from payback_agent.tools_source import open_stdio_source  # noqa: E402


def _fail(code: int, message: str) -> None:
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(code)


def _shape(tool: dict) -> dict:
    function = tool["function"]
    schema = function["parameters"]
    return {
        "name": function["name"],
        "description_chars": len(function["description"]),
        "params": sorted((schema.get("properties") or {}).keys()),
        "required": schema.get("required") or [],
    }


def _section_summary(name: str, payload: dict) -> dict:
    """The few numbers that show the tool really reached the database.

    The multi-row payloads need their own shrinking: a ladder's ``levels`` carries
    stock lists, a position outcome's carries statistics and a similar-day match
    carries two full state dicts, and printing any of them raw would bury the one
    number worth eyeballing.
    """
    if not isinstance(payload, dict):
        return {"note": f"not an object: {type(payload).__name__}"}
    if "error" in payload:
        return {"error": payload["error"], "requested": payload.get("requested")}
    if payload.get("found") is False:
        return {"found": False, "hint": payload.get("hint")}

    keep = ("data_as_of", "trade_date", "create_time",
            # market_data
            "shangzheng_point", "shangzheng_rise_and_fall", "rise_amount", "fall_amount",
            "market_turnover", "market_score",
            # short_term_data
            "daily_limit_quantity", "daily_limit_open_quantity", "down_limit_quantity",
            "sealing_rate", "market_height", "even_board_amount", "cycle",
            # dm_day_state
            "limit_up_qty", "prev_cohort_avg_return", "prev_cohort_promotion_rate",
            "percentile_of_window",
            # ladder
            "max_height", "yizi", "stocks_returned", "stocks_total", "truncated",
            "window")
    out = {key: payload[key] for key in keep if key in payload}
    levels = payload.get("levels")
    if isinstance(levels, list):
        if name == "get_position_outcomes":
            out["levels"] = [
                {"level": item.get("level"),
                 "days": (item.get("promotion") or {}).get("days"),
                 "cohort_stocks_total": (item.get("promotion") or {}).get("cohort_stocks_total"),
                 "pooled_promotion_rate": (item.get("promotion") or {}).get("pooled_promotion_rate"),
                 "premium_observations": (item.get("next_day_premium") or {}).get("observations"),
                 "premium_median": (item.get("next_day_premium") or {}).get("median")}
                for item in levels
            ]
        else:
            out["levels"] = [{"level": item.get("level"),
                              "stocks": len(item.get("stocks") or [])} for item in levels]
    if name == "get_similar_days":
        # The matches are the payload; each one shrinks to "how alike" and "what
        # happened the next day", which is the pair this tool exists to show.
        out["anchor"] = payload.get("anchor", {}).get("state")
        out["matches"] = [
            {"date": item.get("trade_date"),
             "distance": item.get("distance"),
             "features": item.get("features_compared"),
             "next": item.get("next_trade_date"),
             "next_premium": (item.get("next_day") or {}).get("prev_cohort_avg_return"),
             "next_promotion": (item.get("next_day") or {}).get("prev_cohort_promotion_rate")}
            for item in payload.get("matches") or []
        ]
        summary = payload.get("summary") or {}
        out["summary"] = {
            "days": summary.get("days"),
            "premium": summary.get("next_day_premium"),
            "promotion_median": (summary.get("next_day_promotion") or {}).get("median"),
            "up_days": summary.get("next_day_up_days"),
            "next_cycle_counts": summary.get("next_day_cycle_counts"),
        }
        out["candidates"] = (payload.get("meta") or {}).get("candidates")
        out["skipped"] = (payload.get("meta") or {}).get("skipped")
    if isinstance(out.get("window"), dict):
        out["window"] = {k: out["window"][k] for k in
                         ("days", "start", "end", "ladder_missing_days",
                          "short_term_missing_days") if k in out["window"]}
    return out


async def _run(args: argparse.Namespace, argv: list[str]) -> int:
    async with open_stdio_source(argv) as source:
        tools = openai_tools(await source.list_tools())
        print("--- 1. MCP handshake + registry (as the gateway will see it) ---")
        print(f"launch command: {' '.join(argv)}")
        print(f"tools registered: {len(tools)}")
        for shape in map(_shape, tools):
            print(json.dumps(shape, ensure_ascii=False))

        print("\n--- 2. evidence bundle (the four levels, no LLM) ---")
        bundle = await collect_evidence(source, args.for_date,
                                        levels=[args.level] if args.level else None)
    problems = 0
    for name, _ in STEPS:
        payload = bundle["sections"][name]
        summary = _section_summary(name, payload)
        as_of = summary.get("data_as_of", "-")
        print(f"\n[{name}] data_as_of={as_of}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if isinstance(payload, dict) and ("error" in payload or payload.get("found") is False):
            problems += 1
    print("\n--- 3. summary ---")
    if problems:
        print(f"{problems}/{len(STEPS)} tools did not return data -- check payback-mcp's "
              ".env (AGENT_MYSQL_*) and that scripts/build_metrics.py has run", file=sys.stderr)
        return 1
    print(f"All {len(STEPS)} tools reached data for {args.for_date!r}. Next step: "
          "`uv run payback-agent --trace traces/smoke.jsonl \"帮我复盘一下昨天\"` "
          "and confirm every number in the answer appears in the trace's tool_call lines.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--for-date", default="yesterday")
    parser.add_argument("--level", type=int, default=None)
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True, encoding="utf8")
    config.load_env()
    argv = config.mcp_launch_args()  # [command, "--directory", <dir>, "run", "payback-mcp"]
    if not Path(argv[2]).is_dir():
        _fail(2, f"payback-mcp directory not found: {argv[2]} -- set PAYBACK_MCP_DIR")
    try:
        return anyio.run(_run, args, argv)
    except (FileNotFoundError, PermissionError, BaseExceptionGroup) as exc:
        from payback_agent.cli import _launch_failure_reason

        _fail(2, f"could not launch payback-mcp: {_launch_failure_reason(exc)}")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
