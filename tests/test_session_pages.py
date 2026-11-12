"""摘要接口仅返回列表所需数据，完整消息通过详情按需读取。"""

import pytest
from starlette.testclient import TestClient
from test_serve import _app


def test_summary_pagination_search_and_detail():
    app, _, service = _app()
    for index in range(5):
        service.memory.save_turn(f's{index}', f'问题{index}', '正文关键词' if index == 1 else '普通正文')
    with TestClient(app) as client:
        first = client.get('/sessions', params={'view': 'summary', 'limit': 2}).json()
        assert [item['id'] for item in first['items']] == ['s4', 's3']
        assert all('messages' not in item for item in first['items'])
        second = client.get('/sessions', params={'view': 'summary', 'limit': 2, 'cursor': first['nextCursor']}).json()
        assert [item['id'] for item in second['items']] == ['s2', 's1']
        last = client.get('/sessions', params={'view': 'summary', 'limit': 2, 'cursor': second['nextCursor']}).json()
        assert [item['id'] for item in last['items']] == ['s0']
        assert last['nextCursor'] is None
        result = client.get('/sessions', params={'view': 'summary', 'q': '关键词'}).json()
        assert [item['id'] for item in result['items']] == ['s1']
        assert '关键词' in result['items'][0]['snippet']
        assert client.get('/sessions/s1').json()['messages'][-1]['content'] == '正文关键词'
        assert client.get('/sessions/missing').status_code == 404
        assert isinstance(client.get('/sessions').json(), list)


@pytest.mark.parametrize('params', [
    {'limit': '0'}, {'limit': '101'}, {'limit': 'x'}, {'cursor': '-1'},
    {'cursor': 'abc'}, {'cursor': '18446744073709551616'}, {'q': 'x' * 201},
])
def test_summary_rejects_invalid_parameters(params):
    app, _, _ = _app()
    with TestClient(app) as client:
        assert client.get('/sessions', params={'view': 'summary', **params}).status_code == 400


def test_search_is_literal_and_returns_latest_order():
    app, _, service = _app()
    service.memory.save_turn('old', '100%_\\测试', '首轮')
    service.memory.save_turn('new', '其他问题', '回答')
    service.memory.save_turn('old', '继续提问', '最新回答')
    with TestClient(app) as client:
        result = client.get('/sessions', params={'view': 'summary', 'q': '%_\\'}).json()
        assert [item['id'] for item in result['items']] == ['old']
        assert result['items'][0]['title'] == '100%_\\测试'
        assert client.get('/sessions', params={'view': 'summary'}).json()['items'][0]['id'] == 'old'
        assert client.delete('/sessions/old').status_code == 200
        assert client.get('/sessions', params={'view': 'summary', 'q': '%_\\'}).json()['items'] == []


def test_mysql_summary_is_one_parameterized_query(monkeypatch):
    from contextlib import contextmanager
    from payback_agent.memory import MemoryStore

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            assert sql.count('%s') == len(params)
            calls.append((sql, params))

        def fetchall(self):
            return [('a', '标题', 9007199254740993, '命中'), ('b', '另一条', 2, '')]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    store = MemoryStore()
    monkeypatch.setattr(store, 'connection', connection)
    result = store.session_page(1, None, "' OR 1=1 --")
    assert len(calls) == 1
    assert "' OR 1=1 --" not in calls[0][0]
    assert "' or 1=1 --" in calls[0][1]
    assert result['nextCursor'] == '9007199254740993'
    assert result['items'][0]['lastTurnId'] == '9007199254740993'
    calls.clear()
    store.session_page(1, 2, '')
    assert len(calls) == 1
    assert 'NULL AS match_id' in calls[0][0]
