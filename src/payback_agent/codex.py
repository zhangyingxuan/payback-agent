"""通过 Codex CLI 适配现有的只读工具调用循环。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import anyio

from .llm import ChatResult, LlmError

_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "arguments": {"type": "string"}},
            "required": ["name", "arguments"], "additionalProperties": False,
        }},
    },
    "required": ["content", "tool_calls"], "additionalProperties": False,
}


def _codex_command() -> list[str]:
    command = os.environ.get('AGENT_CODEX_COMMAND', 'codex')
    if sys.platform != 'win32':
        return [command]
    resolved = shutil.which(command) or command
    if Path(resolved).suffix.lower() in ('.cmd', '.bat'):
        # npm 的 Windows 启动器不能直接作为可执行文件传给 CreateProcess。
        entry = Path(resolved).parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
        node = shutil.which('node')
        if entry.is_file() and node:
            return [node, str(entry)]
    return [resolved]


class CodexClient:
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        # 工具仅由现有循环执行，避免 CLI 直接访问数据库或执行用户要求的命令。
        prompt = ("遵循 messages 中的 system 指令，根据已有消息回答。需要查询时只返回 "
                  "tool_calls，name 必须来自 tools，arguments 必须是 JSON 对象字符串。"
                  "无须查询时返回最终答案到 content，tool_calls 为空。输入数据：\n")
        # 测试和 CLI 均从标准输入读取，避免问题和数据出现在进程参数中。
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as schema:
            json.dump(_SCHEMA, schema)
            schema_path = schema.name
        command = _codex_command() + ["exec", "--json",
                   "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--output-schema", schema_path]
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "browser_use",
                        "computer_use", "multi_agent", "hooks"):
            command.extend(["--disable", feature])
        model = os.environ.get("AGENT_CODEX_MODEL")
        if model:
            command.extend(["--model", model])
        command.append("-")
        try:
            # JSON 输入前的说明也放进 JSON，便于保持 stdin 单一格式。
            data = json.dumps({"instruction": prompt, "messages": messages, "tools": tools or []},
                              ensure_ascii=False).encode("utf-8")
            with anyio.fail_after(180):
                result = await anyio.run_process(command, input=data, check=False)
        except (OSError, TimeoutError) as exc:
            raise LlmError(f"Codex CLI 启动失败或超时：{type(exc).__name__}") from exc
        finally:
            os.unlink(schema_path)
        if result.returncode:
            raise LlmError(f"Codex CLI 执行失败（退出码 {result.returncode}），请检查登录及模型配置")
        try:
            events = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
            text = next(event["item"]["text"] for event in reversed(events)
                        if event.get("type") == "item.completed"
                        and event.get("item", {}).get("type") == "agent_message")
            response = json.loads(text)
            calls = []
            for index, call in enumerate(response["tool_calls"]):
                arguments = call["arguments"]
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                calls.append({"id": f"codex_{index}", "type": "function",
                              "function": {"name": call["name"], "arguments": arguments}})
            message: dict[str, Any] = {"role": "assistant", "content": response["content"] or ""}
            if calls:
                message["tool_calls"] = calls
            return ChatResult(message=message, finish_reason="tool_calls" if calls else "stop")
        except (ValueError, KeyError, TypeError, StopIteration, UnicodeError) as exc:
            raise LlmError("Codex CLI 返回了无法解析的结果") from exc
