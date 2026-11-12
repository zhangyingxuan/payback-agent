"""Where the tools come from: an MCP stdio session, or anything shaped like one.

``loop.py`` only ever sees the ``ToolSource`` protocol, so the tool-calling loop is
unit-testable without a subprocess, and a different transport (in-process, HTTP)
later is a new class here rather than a new loop.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .bridge import tool_message_content


class ToolSource(Protocol):
    def list_tools(self) -> Any:
        """Return the tool objects (``name`` / ``description`` / ``input_schema``)."""
        ...

    def call_tool(self, name: str, arguments: dict) -> Any:
        """Run one tool and return its result as the string for a tool message."""
        ...


class McpStdioSource:
    """Drive a stdio MCP server (payback-mcp) as a ToolSource.

    One session per process, reused across turns: the server is designed to idle
    between questions (it re-pings MySQL per call), and respawning it per message
    would re-run its startup degrade path on every keystroke.
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        self._params = StdioServerParameters(command=command, args=args, env=env)
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "McpStdioSource":
        # 统一管理子进程和会话，初始化失败时也能按相反顺序释放资源。
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._session = stack, session
        return self

    async def __aexit__(self, *exc: Any) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await stack.aclose()

    async def list_tools(self) -> Any:
        result = await self._require().list_tools()
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict) -> Any:
        return tool_message_content(await self._require().call_tool(name, arguments))

    def _require(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("McpStdioSource must be used as an async context manager")
        return self._session


@asynccontextmanager
async def open_stdio_source(argv: list[str]) -> Any:
    """Convenience: ``async with open_stdio_source(config.mcp_launch_args()) as src``."""
    async with McpStdioSource(argv[0], argv[1:]) as source:
        yield source
