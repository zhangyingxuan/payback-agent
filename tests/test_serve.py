"""Serve-mode tests: the real Starlette app and the real AgentService, with only
the LLM and the MCP source faked -- so routing, SSE framing, session history and
the error path are all exercised as production would."""

from __future__ import annotations

import json
from types import SimpleNamespace

from starlette.testclient import TestClient

from payback_agent.llm import ChatResult, LlmError
from payback_agent.serve import AgentService, create_app


def _tool(name):
    return SimpleNamespace(name=name, description=f"{name} desc",
                           input_schema={"type": "object", "properties": {}})


class FakeLlm:
    """Scripted answers; an Exception in the script is raised instead of returned."""

    def __init__(self, *script):
        self.scripted = list(script)
        self.requests: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.requests.append(json.loads(json.dumps(messages, default=str)))
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(message=item, finish_reason="stop",
                          usage={"prompt_tokens": 10, "completion_tokens": 5,
                                 "total_tokens": 15})

    async def aclose(self):
        pass


class FakeSource:
    def __init__(self, results):
        self._results = results

    async def list_tools(self):
        return [_tool(name) for name in sorted(self._results)]

    async def call_tool(self, name, arguments):
        return self._results[name]


def _app(*script, **service_kw):
    from sqlite_memory import MemoryStore as TestMemoryStore
    memory_path = service_kw.pop('memory_path', ':memory:')
    service = AgentService(memory=TestMemoryStore(memory_path), **service_kw)
    llm = FakeLlm(*script)
    service._client = llm
    service._source = FakeSource({"get_market_overview": '{"limit_up_qty": 42}'})
    return create_app(agent=service), llm, service


def _events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[len("data: "):])
            for line in response.text.splitlines() if line.startswith("data: ")]


def _answer(text):
    return {"role": "assistant", "content": text}


def test_healthz_reports_ok():
    app, _, _ = _app()
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["sessions"] == 0


def test_chat_streams_session_then_the_loop_events():
    app, _, _ = _app(_answer("涨停 42 家"))
    with TestClient(app) as client:
        events = _events(client.post("/chat", json={"message": "昨天涨停几家"}))
    kinds = [e["type"] for e in events]
    assert kinds == ["session", "delta", "done"]
    assert events[0]["session_id"]
    assert "证据不足" in events[1]["text"]
    assert "证据不足" in events[2]["content"]
    assert events[2]["usage"]["total_tokens"] == 15


def test_a_returned_session_id_carries_the_visible_history():
    app, llm, _ = _app(_answer("① 涨停 42 家"), _answer("二板晋级率 17%"))
    with TestClient(app) as client:
        first = _events(client.post("/chat", json={"message": "复盘一下昨天"}))
        session_id = first[0]["session_id"]
        second = _events(client.post(
            "/chat", json={"message": "那二板呢", "session_id": session_id}))
    assert "证据不足" in second[-1]["content"]
    assert [m["role"] for m in llm.requests[1]] == ["system", "user", "assistant", "user"]
    assert llm.requests[1][3]["content"] == "那二板呢"
    # and the tool result of turn one is NOT replayed, per append_turn's contract
    assert all(m["role"] != "tool" for m in llm.requests[1])


def test_tool_calls_are_streamed_as_events():
    call = {"id": "c1", "type": "function",
            "function": {"name": "get_market_overview", "arguments": "{}"}}
    app, _, _ = _app({"role": "assistant", "tool_calls": [call]}, _answer("42 家"))
    with TestClient(app) as client:
        events = _events(client.post("/chat", json={"message": "复盘"}))
    kinds = [e["type"] for e in events]
    assert kinds == ["session", "tool_call", "tool_result", "delta", "done"]
    assert events[1]["name"] == "get_market_overview"
    assert events[2]["error"] is None
    # heavy fields stay server-side: the raw tool payload and the message stack
    # are for CLI/trace consumers, not the SSE wire (see _DROP_ON_WIRE)
    assert "result" not in events[2]
    assert "messages" not in events[-1]


def test_a_bad_body_is_a_400_before_any_stream_starts():
    app, _, _ = _app()
    with TestClient(app) as client:
        assert client.post("/chat", json={}).status_code == 400
        assert client.post("/chat", json={"message": "  "}).status_code == 400
        bad_session = client.post("/chat", json={"message": "x", "session_id": 3})
        assert bad_session.status_code == 400


def test_a_gateway_failure_becomes_an_error_event_and_leaves_the_session_clean():
    app, llm, service = _app(LlmError("gateway HTTP 401: invalid api key"),
                             _answer("好了"))
    with TestClient(app) as client:
        broken = _events(client.post("/chat", json={"message": "复盘", "session_id": "s1"}))
        assert broken[0] == {"type": "session", "session_id": "s1"}
        assert broken[-1]["type"] == "error"
        assert "401" in broken[-1]["message"]
        assert service._sessions.get("s1", []) == []  # the failed turn committed nothing
        _events(client.post("/chat", json={"message": "复盘", "session_id": "s1"}))
    assert [m["role"] for m in llm.requests[1]] == ["system", "user"]


def test_sessions_can_be_deleted():
    app, _, _ = _app(_answer("好了"))
    with TestClient(app) as client:
        events = _events(client.post("/chat", json={"message": "复盘"}))
        session_id = events[0]["session_id"]
        assert client.delete(f"/sessions/{session_id}").json()["deleted"] is True
        assert client.delete(f"/sessions/{session_id}").json()["deleted"] is False


def test_the_session_store_is_lru_capped():
    app, _, service = _app(*[_answer("好了")] * 3, max_sessions=2)
    with TestClient(app) as client:
        for _ in range(3):
            _events(client.post("/chat", json={"message": "复盘"}))
    assert service.session_count == 2


def test_system_date_refreshes_for_each_turn_in_same_session(monkeypatch):
    from datetime import date
    from payback_agent import prompt

    class Clock(date):
        current = date(2026, 11, 4)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(prompt, "date", Clock)
    app, llm, _ = _app(_answer("第一天"), _answer("第二天"))
    with TestClient(app) as client:
        first = _events(client.post("/chat", json={"message": "今天几号"}))
        Clock.current = date(2026, 11, 6)
        _events(client.post("/chat", json={"message": "今天几号", "session_id": first[0]["session_id"]}))
    assert "服务端今天是 2026-11-04" in llm.requests[0][0]["content"]
    assert "服务端今天是 2026-11-06" in llm.requests[1][0]["content"]


def test_session_history_survives_service_restart(tmp_path):
    path = tmp_path / 'memory.db'
    app, _, _ = _app(_answer('先看风险'), memory_path=path)
    with TestClient(app) as client:
        first = _events(client.post('/chat', json={'message': '请先看风险'}))
    app, llm, _ = _app(_answer('继续'), memory_path=path)
    with TestClient(app) as client:
        _events(client.post('/chat', json={'message': '接着说', 'session_id': first[0]['session_id']}))
    assert [m['content'] for m in llm.requests[0][1:]] == ['请先看风险', '先看风险', '接着说']


def test_long_term_memory_is_explicit_and_survives_restart(tmp_path):
    path = tmp_path / 'memory.db'
    app, _, _ = _app(memory_path=path)
    with TestClient(app) as client:
        saved = client.post('/memories', json={'content': '回答先看风险'}).json()
    app, llm, _ = _app(_answer('好的'), memory_path=path)
    with TestClient(app) as client:
        assert client.get('/memories').json()[0]['content'] == '回答先看风险'
        _events(client.post('/chat', json={'message': '你好'}))
        assert client.delete('/memories/' + saved['id']).status_code == 204
        assert client.get('/memories').json() == []
    assert '回答先看风险' in llm.requests[0][0]['content']


def test_session_delete_removes_persisted_history(tmp_path):
    path = tmp_path / 'memory.db'
    app, _, _ = _app(_answer('好的'), memory_path=path)
    with TestClient(app) as client:
        first = _events(client.post('/chat', json={'message': '你好'}))
        sid = first[0]['session_id']
        assert client.delete('/sessions/' + sid).json()['deleted']
    app, llm, _ = _app(_answer('好的'), memory_path=path)
    with TestClient(app) as client:
        _events(client.post('/chat', json={'message': '再见', 'session_id': sid}))
    assert [m['role'] for m in llm.requests[0]] == ['system', 'user']


def test_saved_sessions_and_messages_can_be_read_after_restart(tmp_path):
    path = tmp_path / 'memory.db'
    app, _, _ = _app(_answer('你好'), memory_path=path)
    with TestClient(app) as client:
        events = _events(client.post('/chat', json={'message': '问候'}))
    app, _, _ = _app(memory_path=path)
    with TestClient(app) as client:
        sessions = client.get('/sessions').json()
    assert sessions[0]['id'] == events[0]['session_id']
    assert sessions[0]['messages'] == [
        {'role': 'user', 'content': '问候'}, {'role': 'assistant', 'content': '你好'}]


def test_update_memory_and_validate_content(tmp_path):
    app, _, _ = _app(memory_path=tmp_path / 'memory.db')
    with TestClient(app) as client:
        saved = client.post('/memories', json={'content': '先看风险'}).json()
        assert client.put('/memories/' + saved['id'], json={'content': '使用表格'}).status_code == 200
        assert client.get('/memories').json()[0]['content'] == '使用表格'
        assert client.put('/memories/' + saved['id'], json={'content': '  '}).status_code == 400
        assert client.put('/memories/missing', json={'content': '有效'}).status_code == 404


def test_storage_failure_does_not_report_completion(tmp_path, monkeypatch):
    app, _, service = _app(_answer('你好'), memory_path=tmp_path / 'memory.db')
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(service.memory, 'save_turn', fail)
    with TestClient(app) as client:
        events = _events(client.post('/chat', json={'message': '问候'}))
        assert events[-1]['type'] == 'error'
        assert not any(event['type'] == 'done' for event in events)
        assert client.get('/sessions').json() == []
