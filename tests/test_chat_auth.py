import httpx
import pytest
from starlette.testclient import TestClient

from payback_agent.serve import create_app
from test_serve import _answer, _app, _events


@pytest.mark.parametrize(('method', 'path'), [
    ('POST', '/chat'), ('GET', '/sessions'), ('GET', '/sessions?view=summary'),
    ('GET', '/sessions/existing'), ('DELETE', '/sessions/existing'),
    ('GET', '/memories'), ('POST', '/memories'),
    ('PUT', '/memories/existing'), ('DELETE', '/memories/existing'),
])
def test_chat_data_requires_login(monkeypatch, method, path):
    monkeypatch.setenv('AGENT_OWNER_ACCOUNT', 'owner')
    assert TestClient(create_app()).request(method, path).status_code == 401


@pytest.mark.parametrize(('account', 'profile_status', 'owner', 'expected'), [
    ('owner', 200, 'owner', 400),
    ('other', 200, 'owner', 403),
    ('owner', 401, 'owner', 401),
    ('owner', 500, 'owner', 503),
    ('owner', 200, '', 503),
])
def test_chat_accepts_only_configured_owner(monkeypatch, account, profile_status, owner, expected):
    monkeypatch.setenv('AGENT_OWNER_ACCOUNT', owner)
    monkeypatch.setenv('AGENT_AUTH_PROFILE_URL', 'http://nest/blowsysun/auth/profile')
    client_class = httpx.AsyncClient

    def profile(request):
        # 只有 NestJS 返回的账号可以决定授权，客户端自报账号不参与判断。
        assert str(request.url) == 'http://nest/blowsysun/auth/profile'
        assert request.headers['authorization'] == 'Bearer test-token'
        return httpx.Response(profile_status, json={'account': account})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_class(
        transport=httpx.MockTransport(profile), **kwargs))
    response = TestClient(create_app()).post('/chat', json={}, headers={
        'Authorization': 'Bearer test-token', 'X-Agent-Account': 'owner',
    })
    assert response.status_code == expected


def test_new_account_cannot_access_owner_sessions_or_memories(monkeypatch):
    monkeypatch.setenv('AGENT_OWNER_ACCOUNT', 'owner')
    monkeypatch.setenv('AGENT_AUTH_PROFILE_URL', 'http://nest/blowsysun/auth/profile')
    client_class = httpx.AsyncClient
    accounts = {'Bearer owner-token': 'owner'}

    def profile(request):
        account = accounts.get(request.headers.get('authorization'))
        return httpx.Response(200, json={'account': account}) if account else httpx.Response(401)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_class(
        transport=httpx.MockTransport(profile), **kwargs))
    app, llm, _ = _app(_answer('所有者答复'))
    owner = {'Authorization': 'Bearer owner-token'}

    with TestClient(app) as client:
        # 先用所有者身份通过真实 HTTP 路由生成会话、保存记忆。
        events = _events(client.post('/chat', headers=owner, json={'message': '所有者问题'}))
        assert events[-1]['type'] == 'done'
        session_id = events[0]['session_id']
        session_path = f'/sessions/{session_id}'
        created = client.post('/memories', headers=owner, json={'content': '所有者偏好'})
        assert created.status_code == 201
        memory = created.json()
        memory_path = f"/memories/{memory['id']}"
        messages = client.get(session_path, headers=owner).json()['messages']

        # 在测试身份服务中创建新账号。有效凭据和已知对象 ID 仍不授予访问。
        accounts['Bearer new-user-token'] = 'new-user'
        new_user = {'Authorization': 'Bearer new-user-token', 'X-Agent-Account': 'owner'}
        attempts = [
            ('GET', '/sessions', None),
            ('GET', '/sessions?view=summary&q=所有者', None),
            ('GET', session_path, None),
            ('POST', '/chat', {'message': '新问题'}),
            ('POST', '/chat', {'message': '续写', 'session_id': session_id}),
            ('DELETE', session_path, None),
            ('GET', '/memories', None),
            ('POST', '/memories', {'content': '新偏好'}),
            ('PUT', memory_path, {'content': '篡改偏好'}),
            ('DELETE', memory_path, None),
        ]
        for method, path, body in attempts:
            response = client.request(method, path, headers=new_user, json=body)
            assert response.status_code == 403, (method, path)
            assert response.json() == {'error': '当前账号无权访问 Agent 会话'}

        # 回读所有者数据并检查模型调用次数，验证请求在读写和入队前被拦截。
        assert client.get(session_path, headers=owner).json()['messages'] == messages
        assert client.get('/memories', headers=owner).json() == [memory]
        assert len(llm.requests) == 1
