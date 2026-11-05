"""Tool-source tests: the session guard and argv plumbing only.

Spawning the real payback-mcp subprocess is a cross-repo integration step and lives
in ``scripts/smoke_agent.py`` -- same reason payback-mcp keeps its live checks out of
pytest: the unit suite must not need a second checkout, uv, or a database.
"""

from __future__ import annotations

import anyio
import pytest

from payback_agent import tools_source
from payback_agent.tools_source import McpStdioSource, open_stdio_source


def test_using_the_source_without_opening_it_fails_loudly():
    async def go():
        source = McpStdioSource("uv", ["--directory", ".", "run", "payback-mcp"])
        with pytest.raises(RuntimeError, match="context manager"):
            await source.list_tools()
        with pytest.raises(RuntimeError, match="context manager"):
            await source.call_tool("get_market_overview", {})
        # closing an unopened source must not raise on top of the real error
        await source.__aexit__()

    anyio.run(go)


def test_open_stdio_source_splits_command_from_args(monkeypatch):
    captured = {}

    class Recording(McpStdioSource):
        def __init__(self, command, args, env=None):
            captured["argv"] = [command, *args]
            captured["env"] = env
            super().__init__(command, args, env)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(tools_source, "McpStdioSource", Recording)

    async def go():
        async with open_stdio_source(["uv", "--directory", "D:/x/payback-mcp",
                                      "run", "payback-mcp"]) as source:
            assert isinstance(source, Recording)

    anyio.run(go)
    assert captured["argv"] == ["uv", "--directory", "D:/x/payback-mcp", "run", "payback-mcp"]
    assert captured["env"] is None
