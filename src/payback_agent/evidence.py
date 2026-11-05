"""``--dry-run`` evidence bundle: the four reasoning levels' data, with no LLM.

Two jobs. It verifies the whole data path (server boot -> date resolve -> payload)
before any gateway credentials exist, and it is the shape the agent should be able
to reconstruct from tool calls -- so a human can compare the two directly.
"""

from __future__ import annotations

import json
from typing import Any

# (tool, extra args) in the order the reasoning chain consumes them:
# ① environment -> ② phase -> ③ position -> ④ conditional regime match, then the
# ladder detail that ④'s tradability statements need.
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
        # Not JSON means the transport or a legacy tool broke the contract; say so
        # in the payload rather than raising out of a dry run.
        return {"error": "tool result is not JSON", "raw": text[:400]}


async def collect_evidence(source: Any, date: str = "yesterday",
                           *, levels: list[int] | None = None) -> dict:
    """Call every shipped tool once for ``date`` and return their payloads."""
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
