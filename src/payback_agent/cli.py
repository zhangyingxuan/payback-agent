"""CLI entry: one question, or a REPL, against payback-mcp through an LLM gateway.

Exit codes follow ``payback-mcp/scripts/smoke_live.py``: 0 answered, 1 the run
failed (gateway/loop), 2 configuration or server-launch failed. Configuration
problems are loud on purpose -- a silently degraded agent answers from memory,
which is the one thing this product must never do.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import anyio

from . import config
from .evidence import STEPS as EVIDENCE_STEPS
from .evidence import collect_evidence
from .llm import ChatClient, LlmError
from .loop import MAX_HISTORY_TURNS, Answer, append_turn, ask
from .prompt import load_system_prompt
from .tools_source import open_stdio_source
from .trace import open_trace


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="payback-agent",
        description="四级短线复盘助手：payback-mcp 工具 + OpenAI 兼容 tool_calls 循环",
    )
    p.add_argument("question", nargs="*", help="一次性提问；不给则进入 REPL")
    p.add_argument("--dry-run", action="store_true",
                   help="不调用 LLM，只把四级证据取全并打印（无需网关凭据）")
    p.add_argument("--for-date", default="yesterday",
                   help="--dry-run 的日期参数：YYYY-MM-DD / today / yesterday / last_trade_day")
    p.add_argument("--level", type=int, default=None,
                   help="--dry-run 时只看某一板高的位置期望")
    p.add_argument("--trace", metavar="PATH", default=None,
                   help="把每一轮请求/响应/工具调用写进 JSONL，可回放")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="输出结构化结果（答案 + 工具调用清单），而不是散文")
    p.add_argument("--quiet", action="store_true", help="不打印工具调用清单")
    p.add_argument("--max-turns", type=int, default=None,
                   help="tool-calling 循环上限，默认取 AGENT_MAX_TURNS（8）")
    p.add_argument("--mcp-dir", default=None, help="payback-mcp 仓库路径，覆盖 PAYBACK_MCP_DIR")
    p.add_argument("--serve", action="store_true",
                   help="启动 HTTP+SSE 服务（网页 chat 页面对接用），不进入 REPL")
    p.add_argument("--host", default="127.0.0.1", help="--serve 监听地址（默认仅本机）")
    p.add_argument("--port", type=int, default=8300, help="--serve 监听端口")
    return p


def _print_calls(answer: Answer, stream: Any) -> None:
    for record in answer.tool_calls:
        mark = "  <- 失败" if record.error else ""
        args = json.dumps(record.arguments, ensure_ascii=False, default=str)
        print(f"[调用 {record.iteration}] {record.name} {args}{mark}", file=stream)


def _emit(answer: Answer, args: argparse.Namespace) -> None:
    if args.as_json:
        print(json.dumps({
            "answer": answer.content,
            "iterations": answer.iterations,
            "truncated": answer.truncated,
            "usage": answer.usage,
            "tool_calls": [
                {"iteration": r.iteration, "name": r.name, "arguments": r.arguments,
                 "error": r.error, "result": r.result}
                for r in answer.tool_calls
            ],
        }, ensure_ascii=False, indent=2, default=str))
        return
    print(answer.content or "（模型没有给出正文）")
    if answer.truncated:
        print(f"[警告] 达到最大迭代数 {answer.iterations}，以上是不完整的推理", file=sys.stderr)
    if not args.quiet:
        _print_calls(answer, sys.stderr)


async def _dry_run(args: argparse.Namespace, argv: list[str]) -> int:
    levels = [args.level] if args.level is not None else None
    async with open_stdio_source(argv) as source:
        bundle = await collect_evidence(source, args.for_date, levels=levels)
    print(json.dumps(bundle, ensure_ascii=False, indent=2))
    errored = [name for name, payload in bundle["sections"].items()
               if isinstance(payload, dict) and "error" in payload]
    if errored:
        print(f"[警告] {len(errored)}/{len(EVIDENCE_STEPS)} 个工具报错：{', '.join(errored)}",
              file=sys.stderr)
        return 1
    return 0


async def _ask_one(client: ChatClient, source: Any, question: str, system_prompt: str,
                   args: argparse.Namespace, trace: Any,
                   history: list[dict] | None = None) -> Answer:
    return await ask(question, llm=client, source=source, system_prompt=system_prompt,
                     max_turns=args.max_turns, trace=trace, history=history)


async def _run(args: argparse.Namespace, argv: list[str]) -> int:
    client = config.create_llm_client()
    try:
        async with open_stdio_source(argv) as source, open_trace(args.trace) as trace:
            if args.question:
                answer = await _ask_one(client, source, " ".join(args.question),
                                        load_system_prompt(), args, trace)
                _emit(answer, args)
                return 0
            print(f"payback-agent REPL：回车提问，exit 退出"
                  f"（记住最近 {MAX_HISTORY_TURNS} 轮的问答，数字每次都重新取）",
                  file=sys.stderr)
            history: list[dict] = []
            while True:
                line = (await anyio.to_thread.run_sync(_read_line)).strip()
                if line in {"exit", "quit"}:
                    return 0
                if not line:
                    continue
                try:
                    answer = await _ask_one(client, source, line, load_system_prompt(), args,
                                            trace, history)
                except LlmError as exc:
                    # One bad turn must not end the session: print, keep going.
                    # The question is dropped too -- an unanswered turn would
                    # otherwise haunt every later request as a dangling user row.
                    print(f"[网关失败] {exc}", file=sys.stderr)
                    continue
                _emit(answer, args)
                history = append_turn(history, line, answer.content)
    finally:
        if hasattr(client, "aclose"):
            await client.aclose()


def _read_line() -> str:
    try:
        return input("复盘> ")
    except EOFError:
        return "exit"


def _launch_failure_reason(exc: BaseException) -> str:
    """Flatten whatever the MCP transport raises into one line a human can act on.

    The stdio client runs the spawn inside a task group, so a missing ``uv`` on
    Windows arrives as an ExceptionGroup whose leaves are ``FileNotFoundError``.
    Printing the group would show ``unhandled errors in a TaskGroup`` and hide the
    one word that matters (the program name), so the leaves are collected instead.
    """
    names: list[str] = []

    def walk(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for sub in error.exceptions:
                walk(sub)
            return
        if isinstance(error, (FileNotFoundError, PermissionError)):
            names.append(f"{type(error).__name__}: {error}")
            return
        if error.__cause__ is not None and not names:
            walk(error.__cause__)
            return
        names.append(f"{type(error).__name__}: {error}")

    walk(exc)
    return "; ".join(dict.fromkeys(names))


def _serve(args: argparse.Namespace) -> int:
    # Imported here, not at module top: the REPL and one-shot paths must keep
    # working without uvicorn installed being load-bearing, and a --serve typo
    # should not slow every CLI start.
    import uvicorn

    from .serve import create_app
    print(f"payback-agent serve：POST http://{args.host}:{args.port}/chat（SSE），"
          f"GET /healthz，DELETE /sessions/<id>", file=sys.stderr)
    uvicorn.run(create_app(max_turns=args.max_turns), host=args.host,
                port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config.load_env()
    if args.mcp_dir:
        os.environ["PAYBACK_MCP_DIR"] = args.mcp_dir
    try:
        args.max_turns = args.max_turns or config.max_turns()
    except config.ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    launch = config.mcp_launch_args()
    try:
        if args.serve:
            return _serve(args)
        if args.dry_run:
            return anyio.run(_dry_run, args, launch)
        return anyio.run(_run, args, launch)
    except config.ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except LlmError as exc:
        print(f"网关失败: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, PermissionError, BaseExceptionGroup) as exc:
        print(f"启动 payback-mcp 失败: {_launch_failure_reason(exc)}"
              f"（当前命令: {' '.join(launch)}，检查 PAYBACK_MCP_DIR / PAYBACK_MCP_COMMAND）",
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
