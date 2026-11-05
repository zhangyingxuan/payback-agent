"""JSONL run trace: one line per observable event, so an answer is auditable.

The point is not logging, it is attribution -- every number in a final answer has
to trace back to a ``tool_call`` line with its real payload. ``llm_request`` lines
carry the full message stack, which makes a recorded trace a replayable fixture.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class TraceWriter:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf8", newline="\n")
        self._sequence = 0

    async def write(self, event: dict) -> None:
        self._sequence += 1
        record = {
            "seq": self._sequence,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **event,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    async def aclose(self) -> None:
        self._fh.close()

    @property
    def path(self) -> Path:
        return self._path


@asynccontextmanager
async def open_trace(path: str | Path | None) -> Any:
    """``async with open_trace(args.trace) as trace`` -- ``None`` yields a no-op writer."""
    if path is None:
        yield _NullTrace()
        return
    writer = TraceWriter(path)
    try:
        yield writer
    finally:
        await writer.aclose()


class _NullTrace:
    async def write(self, event: dict) -> None:
        return None

    async def aclose(self) -> None:
        return None


def read_trace(path: str | Path) -> list[dict]:
    """Replay a recorded trace (used by the eval runner and by tests)."""
    lines: Iterator[str] = Path(path).read_text(encoding="utf8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
