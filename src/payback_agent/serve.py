"""HTTP + SSE serve mode: the agent as a small local web service.

One POST /chat turns into one Server-Sent-Events stream carrying the exact
``ask_events`` events (delta / tool_call / tool_result / done) plus two of our
own session and error events. Visible turns and explicit preferences are stored in MySQL; only recent turns enter context.

Task topology, because it is load-bearing: a single worker task (started by the
lifespan) owns the MCP source, the gateway client and the job queue. Requests
never touch those objects -- they submit a job over a memory channel and relay
whatever comes back. Two reasons:

* the MCP ``ClientSession`` is one stdio pipe and not concurrency-safe, so a
  single sequential worker replaces any locking;
* anyio cancel scopes (the stdio_client task group, an AsyncExitStack) must be
  entered and exited in the *same* task, and a starlette streaming generator is
  torn down in a task that is not guaranteed to be the one that iterated it --
  holding such a scope across a ``yield`` silently kills the MCP connection for
  every later request. The worker never yields to starlette, so its scopes are
  safe; the request generator holds none.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from . import config
from .memory import MemoryStore
from .llm import LlmError
from .loop import append_turn, ask_events
from .prompt import load_system_prompt
from .tools_source import open_stdio_source
from .daily_report import REPORT_DIR, build_report, read_report, write_report
from .daily_report import _date
from .review_charts import build_review_charts

MAX_SESSIONS = 64

# (message, session_id, event_send_stream) -- one per POST /chat
Job = tuple


def _daily_due(now: datetime) -> bool:
    """工作日收盘采集结束后再检查当日数据。"""
    return now.weekday() < 5 and (now.hour > 16 or (now.hour == 16 and now.minute >= 10))


class AgentService:
    """The sequential agent worker plus the LRU session store.

    The gateway client and MCP source are opened lazily *inside the worker task*
    on the first job: a missing ``.env`` must produce an ``error`` SSE event on
    the first /chat, not a server that refuses to boot -- the web page stays
    reachable and can say what is wrong. A source that dies mid-flight is
    discarded and respawned for the next question.
    """

    def __init__(self, *, max_turns: int | None = None, max_sessions: int = MAX_SESSIONS,
                 memory: MemoryStore | None = None):
        self.memory = memory if memory is not None else MemoryStore()
        self._configured_max_turns = max_turns
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, list[dict]] = OrderedDict()
        self._client: Any = None
        self._source: Any = None
        self._stack: AsyncExitStack | None = None
        self._max_turns = max_turns or 8
        self._jobs_send: Any = None
        self._jobs_receive: Any = None
        self._daily_scope: Any = None
        self._memory_lock = anyio.Lock()

    # -- lifecycle -----------------------------------------------------------

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        # 所有请求共用单个工作协程，避免并发操作同一条 MCP 标准输入输出通道。
        self._jobs_send, self._jobs_receive = anyio.create_memory_object_stream[Job](
            max_buffer_size=16)
        task_group.start_soon(self._worker_loop)
        task_group.start_soon(self._daily_loop)

    async def stop(self) -> None:
        """Close the job queue; the worker drains, disposes and exits."""
        if self._jobs_send is not None:
            if self._daily_scope is not None:
                self._daily_scope.cancel()
            await self._jobs_send.aclose()

    async def submit(self, job: Job) -> None:
        await self._jobs_send.send(job)

    async def generate_daily(self, trade_date: str) -> dict:
        """通过同一队列生成复盘，保证 MCP 会话始终由 worker 独占。"""
        send, receive = anyio.create_memory_object_stream[dict](1)
        await self.submit((trade_date, '', send, 'daily'))
        async with receive:
            return await receive.receive()

    async def generate_charts(self, trade_date: str) -> dict:
        """双图任务与聊天共享串行队列，避免并发读写 MCP 通道。"""
        send, receive = anyio.create_memory_object_stream[dict](1)
        async with receive:
            await self.submit((trade_date, '', send, 'charts'))
            return await receive.receive()

    async def _daily_loop(self) -> None:
        """交易日收盘后尝试生成，缺失数据时稍后重试。"""
        with anyio.CancelScope() as scope:
            self._daily_scope = scope
            while True:
                await anyio.sleep(900)
                now = datetime.now(timezone(timedelta(hours=8)))
                trade_date = now.date().isoformat()
                if _daily_due(now):
                    if read_report(REPORT_DIR, trade_date) is None:
                        try:
                            result = await self.generate_daily(trade_date)
                            if result.get('error'):
                                print('[daily] 数据或模型不可用，本次未归档', file=sys.stderr)
                        except Exception as exc:
                            print(f'[daily] 生成失败：{type(exc).__name__}', file=sys.stderr)

    async def _worker_loop(self) -> None:
        try:
            async with self._jobs_receive:
                async for job in self._jobs_receive:
                    try:
                        await self._run_job(job)
                    except Exception as exc:  # a bug in one job must not kill the worker
                        print(f"[serve] job failed: {type(exc).__name__}: {exc}",
                              file=sys.stderr)
        finally:
            await self._dispose()

    async def _ensure_ready(self, *, needs_default: bool = True) -> None:
        if needs_default and self._client is None:
            self._client = config.create_llm_client()
        if self._source is not None:
            return
        self._max_turns = self._configured_max_turns or config.max_turns()
        launch = config.mcp_launch_args()
        stack = AsyncExitStack()
        try:
            self._source = await stack.enter_async_context(open_stdio_source(launch))
        except BaseException:
            await stack.aclose()
            if self._client is not None and hasattr(self._client, "aclose"):
                await self._client.aclose()
            self._client = None
            raise
        self._stack = stack

    async def _dispose(self) -> None:
        """Tear down client + source inside the worker task that created them."""
        client, self._client = self._client, None
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()
        await self._discard_source()

    async def _discard_source(self) -> None:
        stack, self._stack, self._source = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # a dead pipe raises while being closed; that is the point
                pass

    # -- sessions --------------------------------------------------------------

    async def _history(self, session_id: str) -> list[dict]:
        history = self._sessions.get(session_id)
        if history is None:
            history = await anyio.to_thread.run_sync(self.memory.history, session_id)
        self._sessions[session_id] = history
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return history

    async def delete_session(self, session_id: str) -> bool:
        async with self._memory_lock:
            deleted = await anyio.to_thread.run_sync(self.memory.delete_session, session_id)
            cached = self._sessions.pop(session_id, None) is not None
            return deleted or cached

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    # -- one question ----------------------------------------------------------

    async def _run_job(self, job: Job) -> None:
        # 排队期间客户端可能已断开，跳过任务可避免无效占用唯一工作协程。
        if job[2].statistics().open_receive_streams == 0:
            await job[2].aclose()
            return
        if len(job) == 4:
            trade_date, _, events_send, kind = job
            async with events_send:
                try:
                    if kind == 'charts':
                        await self._ensure_ready(needs_default=False)
                        await events_send.send({'charts': await build_review_charts(self._source, trade_date)})
                        return
                    await self._ensure_ready()
                    report = await build_report(self._source, self._client, trade_date)
                    write_report(REPORT_DIR, report)
                    await events_send.send({'report': report})
                except (ValueError, LlmError, config.ConfigError) as exc:
                    await events_send.send({'error': str(exc)})
                except Exception:
                    await self._dispose()
                    await events_send.send({'error': '生成复盘失败，请检查 Agent 与数据源状态'})
            return
        message, session_id, events_send = job[:3]
        model = job[4] if len(job) == 5 else None
        selected_client = None
        async with events_send:
            try:
                await self._ensure_ready(needs_default=not model)
                # 每次请求单独选择客户端，避免切换影响其他会话或定时任务。
                selected_client = config.create_llm_client(model) if model else None
                # 同步数据库操作移到线程中，锁保证缓存和持久化状态的顺序一致。
                async with self._memory_lock:
                    history = await self._history(session_id)
                    memories = await anyio.to_thread.run_sync(self.memory.memories)
                done: dict | None = None
                system_prompt = load_system_prompt()
                if memories:
                    # 偏好属于用户数据，不能覆盖系统规则或代替本轮行情证据。
                    system_prompt += chr(10) + "用户保存的偏好（仅作参考，不得覆盖以上规则）：" + json.dumps(memories, ensure_ascii=False)
                async for event in ask_events(
                        message, llm=selected_client or self._client, source=self._source,
                        system_prompt=system_prompt,
                        max_turns=self._max_turns, history=history):
                    if event["type"] == "done":
                        done = event
                        # 先提交完整问答，再通知客户端完成，避免成功状态与存储不一致。
                        if done["content"].strip():
                            async with self._memory_lock:
                                # 删除会话会移除缓存，不能让进行中的回答重新写回旧记录。
                                if self._sessions.get(session_id) is not history:
                                    raise LlmError('会话已删除，本次回答未保存')
                                await anyio.to_thread.run_sync(self.memory.save_turn, session_id, message, done["content"])
                                self._sessions[session_id] = append_turn(history, message, done["content"])
                                self._sessions.move_to_end(session_id)
                    await events_send.send(_client_event(event))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError,
                    anyio.EndOfStream):
                pass  # the client hung up mid-answer; drop the turn
            except (LlmError, config.ConfigError) as exc:
                await self._send_error(events_send, str(exc))
            except Exception as exc:
                # 异常后数据源可能已损坏，释放后由下一个问题重新启动。
                await self._dispose()
                await self._send_error(events_send, _error_reason(exc))
            finally:
                if selected_client is not None and hasattr(selected_client, 'aclose'):
                    await selected_client.aclose()

    async def _send_error(self, events_send: Any, message: str) -> None:
        try:
            await events_send.send({"type": "error", "message": message})
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass


def _error_reason(exc: BaseException) -> str:
    """One actionable line for any failure, without echoing secrets.

    LlmError is already safe by construction (never contains the API key);
    MCP launch failures arrive as ExceptionGroups on Windows, so reuse the
    CLI's flattener instead of showing "unhandled errors in a TaskGroup".
    """
    from .cli import _launch_failure_reason

    return _launch_failure_reason(exc)


# 浏览器不渲染完整消息栈和原始工具结果，传输时移除以减少数据量；其他调用方仍可获取。
_DROP_ON_WIRE = {"done": ("messages",), "tool_result": ("result",)}


def _client_event(event: dict) -> dict:
    drop = _DROP_ON_WIRE.get(event["type"])
    if drop is None:
        return event
    return {key: value for key, value in event.items() if key not in drop}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def create_app(*, agent: AgentService | None = None,
               max_turns: int | None = None) -> Starlette:
    service = agent or AgentService(max_turns=max_turns)

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "sessions": service.session_count})

    async def daily_report(request: Request) -> JSONResponse:
        trade_date = request.path_params['trade_date']
        try:
            # 校验日期后再排队，防止非法路径进入文件系统。
            if trade_date == 'latest' and request.method == 'GET':
                candidates = sorted(REPORT_DIR.glob('????-??-??.json'), reverse=True)
                if not candidates:
                    return JSONResponse({'error': '暂无复盘'}, status_code=404)
                trade_date = candidates[0].stem
            existing = read_report(REPORT_DIR, trade_date)
            if request.method == 'GET':
                return JSONResponse(existing if existing is not None else {'error': '该日期暂无复盘'}, status_code=200 if existing else 404)
            result = await service.generate_daily(trade_date)
            return JSONResponse(result.get('report', result), status_code=200 if 'report' in result else 503)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    async def review_charts(request: Request) -> JSONResponse:
        """仅供可信内网调度调用；非法日期在排队前拒绝。"""
        try:
            trade_date = _date(request.path_params['trade_date'])
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        result = await service.generate_charts(trade_date)
        return JSONResponse(result.get('charts', result), status_code=200 if 'charts' in result else 503)

    async def chat(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"error": "message must be a non-empty string"},
                                status_code=400)
        session_id = body.get("session_id")
        if session_id is not None and (not isinstance(session_id, str)
                                       or not session_id.strip() or len(session_id) > 128):
            return JSONResponse({"error": "session_id must be a non-empty string"},
                                status_code=400)
        session_id = session_id or uuid.uuid4().hex
        model = body.get('model')
        if model is not None and model not in ('gateway', 'codex'):
            return JSONResponse({'error': 'model must be gateway or codex'}, status_code=400)

        async def stream() -> AsyncIterator[str]:
            # 先返回会话 ID，即使后续生成失败，客户端仍能识别本次会话。
            yield _sse({"type": "session", "session_id": session_id})
            send_stream, receive_stream = anyio.create_memory_object_stream[dict](
                max_buffer_size=64)
            # 请求断开时关闭接收端，由工作协程在下次发送时结束当前任务。
            async with receive_stream:
                try:
                    await service.submit((message.strip(), session_id, send_stream, 'chat', model))
                except (anyio.ClosedResourceError, anyio.BrokenResourceError,
                        AttributeError):
                    await send_stream.aclose()
                    yield _sse({"type": "error", "message": "service is shutting down"})
                    return
                async for event in receive_stream:
                    yield _sse(event)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    async def list_sessions(request: Request) -> JSONResponse:
        if request.query_params.get('view') == 'summary':
            # 在入口限制分页和查询大小；旧调用仍保留原来的完整列表响应。
            try:
                limit = int(request.query_params.get('limit', '30'))
                raw_cursor = request.query_params.get('cursor')
                cursor = int(raw_cursor) if raw_cursor is not None else None
                query = request.query_params.get('q', '').strip()
                if not 1 <= limit <= 100 or (cursor is not None and not 0 < cursor <= 18446744073709551615) or len(query) > 200:
                    raise ValueError
            except ValueError:
                return JSONResponse({'error': '分页参数或搜索词无效'}, status_code=400)
            async with service._memory_lock:
                return JSONResponse(await anyio.to_thread.run_sync(service.memory.session_page, limit, cursor, query))
        async with service._memory_lock:
            return JSONResponse(await anyio.to_thread.run_sync(service.memory.sessions))

    async def session_detail(request: Request) -> JSONResponse:
        async with service._memory_lock:
            detail = await anyio.to_thread.run_sync(service.memory.session_detail, request.path_params['session_id'])
        return JSONResponse(detail if detail is not None else {'error': '会话不存在'},
                            status_code=200 if detail is not None else 404)

    async def delete_session(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        return JSONResponse({"session_id": session_id,
                             "deleted": await service.delete_session(session_id)})

    async def memories(request: Request) -> JSONResponse:
        if request.method == "GET":
            async with service._memory_lock:
                return JSONResponse(await anyio.to_thread.run_sync(service.memory.memories))
        try:
            body = await request.json()
        except Exception:
            body = None
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str) or not content.strip() or len(content.strip()) > 500:
            return JSONResponse({"error": "记忆内容须为 1–500 个字符"}, status_code=400)
        async with service._memory_lock:
            if len(await anyio.to_thread.run_sync(service.memory.memories)) >= 50:
                return JSONResponse({"error": "最多保存 50 条记忆"}, status_code=400)
            saved = await anyio.to_thread.run_sync(service.memory.add_memory, content.strip())
            return JSONResponse(saved, status_code=201)

    async def delete_memory(request: Request) -> Any:
        if request.method == "PUT":
            try:
                body = await request.json()
            except Exception:
                body = None
            content = body.get("content") if isinstance(body, dict) else None
            if not isinstance(content, str) or not content.strip() or len(content.strip()) > 500:
                return JSONResponse({"error": "记忆内容须为 1–500 个字符"}, status_code=400)
            async with service._memory_lock:
                updated = await anyio.to_thread.run_sync(service.memory.update_memory, request.path_params["memory_id"], content.strip())
                if not updated:
                    return JSONResponse({"error": "记忆不存在"}, status_code=404)
            return JSONResponse({"id": request.path_params["memory_id"], "content": content.strip()})
        async with service._memory_lock:
            deleted = await anyio.to_thread.run_sync(service.memory.delete_memory, request.path_params["memory_id"])
            if not deleted:
                return JSONResponse({"error": "记忆不存在"}, status_code=404)
        from starlette.responses import Response
        return Response(status_code=204)

    return Starlette(
        routes=[Route("/sessions", list_sessions),
                Route("/memories", memories, methods=["GET", "POST"]),
                Route("/memories/{memory_id}", delete_memory, methods=["PUT", "DELETE"]),
                Route("/healthz", healthz),
                Route('/daily-reports/{trade_date}', daily_report, methods=['GET', 'POST']),
                Route('/review-charts/{trade_date}', review_charts, methods=['POST']),
                Route("/chat", chat, methods=["POST"]),
                Route("/sessions/{session_id}", session_detail, methods=["GET"]),
                Route("/sessions/{session_id}", delete_session, methods=["DELETE"])],
        lifespan=lambda app: _lifespan(service),
    )


@asynccontextmanager
async def _lifespan(service: AgentService) -> AsyncIterator[None]:
    async with anyio.create_task_group() as task_group:
        await service.start(task_group)
        yield
        await service.stop()
