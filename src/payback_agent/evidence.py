"""``--dry-run`` evidence bundle: the four reasoning levels' data, with no LLM.

Two jobs. It verifies the whole data path (server boot -> date resolve -> payload)
before any gateway credentials exist, and it is the shape the agent should be able
to reconstruct from tool calls -- so a human can compare the two directly.
"""

from __future__ import annotations

import json
from typing import Any

# 按推理顺序获取环境、阶段、位置、相似情景，以及可交易性所需的梯队明细。
STEPS: tuple[tuple[str, dict], ...] = (
    ("get_market_overview", {}),
    ("get_limit_up_structure", {}),
    ("get_market_phase", {"window_days": 60}),
    ("get_position_outcomes", {"window_days": 250}),
    ("get_similar_days", {"window_days": 250}),
    ("get_even_board_ladder", {"max_stocks": 12}),
)


def _payload(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        # 非 JSON 表示传输或旧工具不符合契约；返回错误，避免试运行中断。
        return {"error": "tool result is not JSON", "raw": text[:400]}


async def collect_evidence(source: Any, date: str = "yesterday",
                           *, levels: list[int] | None = None) -> dict:
    """Call every shipped tool once for ``date`` and return their payloads."""
    # 按分析链依次采集，单个工具失败不阻止检查其他工具。
    sections: dict[str, Any] = {}
    for name, extra in STEPS:
        args = {"date": date, **extra}
        if name == "get_position_outcomes" and levels:
            args = {**args, "level": levels[0]}
        try:
            sections[name] = _payload(await source.call_tool(name, args))
        except Exception as exc:
            sections[name] = {"requested": args, "error": f"{type(exc).__name__}: {exc}"}
    return {"date": date, "sections": sections}
