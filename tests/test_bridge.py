"""Bridge tests: both directions of the MCP <-> OpenAI seam, no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

from payback_agent.bridge import openai_tools, tool_message_content


def _tool(name="get_market_overview", description="d", schema=None):
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema if schema is not None else {"type": "object", "properties": {"date": {}}},
    )


def test_openai_tools_wraps_mcp_tool_into_function_shape():
    tools = openai_tools([_tool()])
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "get_market_overview",
                "description": "d",
                "parameters": {"type": "object", "properties": {"date": {}}},
            },
        }
    ]


def test_openai_tools_keeps_the_server_schema_verbatim():
    # Re-deriving the schema here is how the two sides would drift apart.
    schema = {"type": "object", "properties": {"level": {"type": "integer", "minimum": 1}},
              "required": ["date"]}
    assert openai_tools([_tool(schema=schema)])[0]["function"]["parameters"] == schema


def test_openai_tools_survives_a_tool_with_no_metadata():
    tool = openai_tools([SimpleNamespace(name="x")])[0]["function"]
    assert tool["description"] == ""
    assert tool["parameters"] == {"type": "object", "properties": {}}


def test_tool_message_content_joins_text_parts_verbatim():
    payload = json.dumps({"requested": "yesterday", "data_as_of": "2026-10-13",
                          "limit_up_qty": 42})
    result = SimpleNamespace(content=[SimpleNamespace(text=payload),
                                      SimpleNamespace(text="第二段")],
                             structured_content=None)
    assert tool_message_content(result) == payload + "\n" + "第二段"


def test_tool_message_content_passes_the_error_contract_through_unedited():
    """The three-state contract is half the model's instructions; the agent layer
    must not rewrite it into prose."""
    payload = json.dumps({"requested": "2020-01-01", "found": False, "hint": "数据起点 2023-04-13"})
    result = SimpleNamespace(content=[SimpleNamespace(text=payload)], structured_content=None)
    assert json.loads(tool_message_content(result)) == {
        "requested": "2020-01-01", "found": False, "hint": "数据起点 2023-04-13"
    }


def test_tool_message_content_falls_back_to_structured_content():
    result = SimpleNamespace(content=[], structured_content={"a": 1})
    assert json.loads(tool_message_content(result)) == {"a": 1}


def test_tool_message_content_with_nothing_at_all_says_so():
    result = SimpleNamespace(content=[], structured_content=None)
    assert json.loads(tool_message_content(result))["error"]
