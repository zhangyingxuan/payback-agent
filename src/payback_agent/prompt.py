"""System prompt loading.

The prompt lives in ``prompts/system.md`` as data, not in code: it is the product,
it changes on every review of a real answer, and it must be diffable without
touching Python. ``{today}`` is the one hole filled at runtime -- the server's
clock, never a date the model reports (payback-mcp enforces the same rule).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.md"


def load_system_prompt(today: date | None = None, path: Path | None = None) -> str:
    text = (path or SYSTEM_PROMPT_PATH).read_text(encoding="utf8")
    return text.replace("{today}", (today or date.today()).isoformat())
