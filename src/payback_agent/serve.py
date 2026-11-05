"""HTTP + SSE serve mode: the agent as a small local web service.

One POST /chat turns into one Server-Sent-Events stream carrying the exact
``ask_events`` events (delta / tool_call / tool_result / done) plus two of our
own (``session`` first, ``error`` instead of a crash). Sessions are in-memory
visible-turn histories -- the same ``append_turn`` contract the REPL uses, so a
web conversation and a REPL conversation cost the same context and obey the same
"数字每次都重新取" discipline. Persistence across restarts is a non-goal.

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
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from . import config
from .llm import LlmError
from .loop import append_turn, ask_events
from .prompt import load_system_prompt
from .tools_source import open_stdio_source

MAX_SESSIONS = 64

# (message, session_id, event_send_stream) -- one per POST /chat
Job = tuple[str, str, anyio.abc.ObjectSendStream]


class AgentService:
    """The sequential agent worker plus the LRU session store.

    The gateway client and MCP source are opened lazily *inside the worker task*
    on the first job: a missing ``.env`` must produce an ``error`` SSE event on
    the first /chat, not a server that refuses to boot -- the web page stays
    reachable and can say what is wrong. A source that dies mid-flight is
    discarded and respawned for the next question.
    """

    def __init__(self, *, max_turns: int | None = None, max_sessions: int = MAX_SESSIONS):
        self._configured_max_turns = max_turns
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, list[dict]] = OrderedDict()
        self._client: Any = None
        self._source: Any = None
        self._stack: AsyncExitStack | None = None
        self._system_prompt = ""
        self._max_turns = max_turns or 8
        self._jobs_send: Any = None
        self._jobs_receive: Any = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self, task_group: anyio.abc.TaskGroup) -> None:
        self._jobs_send, self._jobs_receive = anyio.create_memory_object_stream[Job](
            max_buffer_size=16)
        task_group.start_soon(self._worker_loop)

    async def stop(self) -> None:
        """Close the job queue; the worker drains, disposes and exits."""
        if self._jobs_send is not None:
            await self._jobs_send.aclose()

    async def submit(self, job: Job) -> None:
        await self._jobs_send.send(job)

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

    async def _ensure_ready(self) -> None:
        if self._client is not None:
            return
        client = config.create_llm_client()
        self._max_turns = self._configured_max_turns or config.max_turns()
        self._system_prompt = load_system_prompt()
        launch = config.mcp_launch_args()
        stack = AsyncExitStack()
        try:
            self._source = await stack.enter_async_context(open_stdio_source(launch))
        except BaseException:
            await stack.aclose()
            if hasattr(client, "aclose"):
                await client.aclose()
            raise
        self._stack = stack
        self._client = client

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

    def _history(self, session_id: str) -> list[dict]:
        history = self._sessions.get(session_id, [])
        self._sessions[session_id] = history
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return history

    def delete_session(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    # -- one question ----------------------------------------------------------

    async def _run_job(self, job: Job) -> None:
        message, session_id, events_send = job
        async with events_send:
            try:
                await self._ensure_ready()
                history = self._history(session_id)
                done: dict | None = None
                async for event in ask_events(
                        message, llm=self._client, source=self._source,
                        system_prompt=self._system_prompt,
                        max_turns=self._max_turns, history=history):
                    if event["type"] == "done":
                        done = event
                    await events_send.send(_client_event(event))
                # Commit only on done: a failed or abandoned turn leaves the
                # session untouched -- an unanswered question must not haunt
                # later requests as a dangling user row.
                if done is not None:
                    self._sessions[session_id] = append_turn(
                        history, message, done["content"])
                    self._sessions.move_to_end(session_id)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError,
                    anyio.EndOfStream):
                pass  # the client hung up mid-answer; drop the turn
            except (LlmError, config.ConfigError) as exc:
                await self._send_error(events_send, str(exc))
            except Exception as exc:
                # MCP failure, launch failure, anything else: the source may be
                # broken, so drop it -- the next question respawns payback-mcp.
                await self._dispose()
                await self._send_error(events_send, _error_reason(exc))

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


# Heavy fields the browser never renders: done carries the full message stack
# and tool_result the raw tool payload -- one four-level review is ~29k chars of
# JSON per turn, pointless to push over SSE and pointless to hold in the page.
# ask_events keeps producing them (CLI/trace/tests consume them); only the wire
# copy is slimmed.
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

    async def chat(request: Request) -> Any:
        try:
            body = await request.json()
        except Exception:
            body = None
        message = (body or {}).get("message")
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"error": "message must be a non-empty string"},
                                status_code=400)
        session_id = (body or {}).get("session_id")
        if session_id is not None and (not isinstance(session_id, str)
                                       or not session_id.strip()):
            return JSONResponse({"error": "session_id must be a non-empty string"},
                                status_code=400)
        session_id = session_id or uuid.uuid4().hex

        async def stream() -> AsyncIterator[str]:
            # The session id goes out first so the client can keep the
            # conversation even if this very turn ends in an error event.
            yield _sse({"type": "session", "session_id": session_id})
            send_stream, receive_stream = anyio.create_memory_object_stream[dict](
                max_buffer_size=64)
            try:
                await service.submit((message.strip(), session_id, send_stream))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError,
                    AttributeError):
                yield _sse({"type": "error", "message": "service is shutting down"})
                return
            # This generator deliberately holds no cancel scope: when the client
            # disconnects, closing the receive side is all the cleanup needed --
            # the worker notices on its next send and abandons the turn.
            async with receive_stream:
                async for event in receive_stream:
                    yield _sse(event)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    async def delete_session(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        return JSONResponse({"session_id": session_id,
                             "deleted": service.delete_session(session_id)})

    return Starlette(
        routes=[Route("/healthz", healthz),
                Route("/chat", chat, methods=["POST"]),
                Route("/sessions/{session_id}", delete_session, methods=["DELETE"])],
        lifespan=lambda app: _lifespan(service),
    )


@asynccontextmanager
async def _lifespan(service: AgentService) -> AsyncIterator[None]:
    async with anyio.create_task_group() as task_group:
        await service.start(task_group)
        yield
        await service.stop()
