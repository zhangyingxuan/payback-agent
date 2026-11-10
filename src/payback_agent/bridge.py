"""MCP <-> OpenAI Chat Completions bridging (pure functions, no I/O).

MCP owns "how tools are discovered"; the OpenAI protocol owns "how the model
decides to call one". This module is the whole seam between them: two directions,
both lossless, both testable without a network.
"""

from __future__ import annotations

import json
from typing import Any


def openai_tools(tools: list[Any]) -> list[dict]:
    """MCP ``Tool`` objects -> the ``tools`` array of a Chat Completions request.

    ``input_schema`` is passed through untouched: it is already JSON Schema, and
    re-deriving it here would silently diverge from what the server validates.
    A tool with no description/schema gets the empty-but-valid shape rather than
    ``None``, because the gateway rejects a null ``parameters``.
    """
    # 直接复用服务端的参数结构，避免模型看到的约束与工具实际校验不一致。
    out = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None)
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": getattr(tool, "description", None) or "",
                    "parameters": schema,
                },
            }
        )
    return out


def tool_message_content(result: Any) -> str:
    """MCP ``CallToolResult`` -> the string a ``role:"tool"`` message carries.

    Text parts are joined verbatim, so payback-mcp's three-state contract
    (``{requested, data_as_of, ...}`` / ``found: false`` / ``error``) reaches the
    model exactly as the server wrote it -- the agent layer must not smooth it
    over, that contract is half of the model's instructions.
    """
    # 优先保留工具原始文本；没有文本时再尝试结构化结果。
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    return '{"error": "tool returned no content"}'
