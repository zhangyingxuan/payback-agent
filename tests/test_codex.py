import json
import sys
from types import SimpleNamespace

import anyio
import pytest

from payback_agent.codex import CodexClient
from payback_agent.llm import LlmError


def test_windows_npm_codex_uses_node_entrypoint(monkeypatch, tmp_path):
    from payback_agent import codex

    entry = tmp_path / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    entry.parent.mkdir(parents=True)
    entry.write_text('', encoding='utf-8')
    shim = tmp_path / 'codex.cmd'
    shim.touch()
    node = tmp_path / 'node.exe'
    node.touch()
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(codex.shutil, 'which', lambda name: str(node if name == 'node' else shim))
    monkeypatch.delenv('AGENT_CODEX_COMMAND', raising=False)

    async def run(command, **kwargs):
        assert command[:3] == [str(node), str(entry), 'exec']
        event = {'type': 'item.completed', 'item': {'type': 'agent_message',
                 'text': json.dumps({'content': 'ok', 'tool_calls': []})}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(event).encode(), stderr=b'')

    monkeypatch.setattr(anyio, 'run_process', run)
    assert anyio.run(CodexClient().chat, []).message['content'] == 'ok'


def test_codex_response_can_request_a_database_tool(monkeypatch):
    async def run(command, **kwargs):
        payload = json.loads(kwargs['input'].decode())
        assert payload['messages'][-1]['content'] == '昨天涨停多少'
        assert payload['tools'][0]['function']['name'] == 'get_limit_up_structure'
        message = {'content': None, 'tool_calls': [{'name': 'get_limit_up_structure', 'arguments': {'date': 'yesterday'}}]}
        event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(message)}}
        return SimpleNamespace(returncode=0, stdout=(json.dumps(event) + '\n').encode(), stderr=b'')
    monkeypatch.setattr(anyio, 'run_process', run)
    result = anyio.run(CodexClient().chat, [{'role': 'user', 'content': '昨天涨停多少'}], [{'function': {'name': 'get_limit_up_structure'}}])
    assert result.message['tool_calls'][0]['function'] == {'name': 'get_limit_up_structure', 'arguments': '{"date": "yesterday"}'}


def test_codex_failure_is_reported_without_exposing_stderr(monkeypatch):
    async def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'secret')
    monkeypatch.setattr(anyio, 'run_process', run)
    with pytest.raises(LlmError, match='Codex') as error:
        anyio.run(CodexClient().chat, [])
    assert 'secret' not in str(error.value)


def test_codex_backend_does_not_require_gateway_credentials(monkeypatch):
    from payback_agent import config
    monkeypatch.setenv('AGENT_LLM_BACKEND', 'codex')
    for key in ('AGENT_LLM_BASE_URL', 'AGENT_LLM_API_KEY', 'AGENT_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    assert isinstance(config.create_llm_client(), CodexClient)


def test_codex_output_must_be_valid_json(monkeypatch):
    async def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b'{"type":"item.completed","item":{"type":"agent_message","text":"bad"}}\n', stderr=b'')
    monkeypatch.setattr(anyio, 'run_process', run)
    with pytest.raises(LlmError, match='无法解析'):
        anyio.run(CodexClient().chat, [])


def test_web_chat_uses_codex_tool_results(monkeypatch):
    from starlette.testclient import TestClient
    from payback_agent.serve import AgentService, create_app
    from test_serve import FakeSource, _events

    async def run(command, **kwargs):
        payload = json.loads(kwargs['input'].decode())
        if any(message['role'] == 'tool' for message in payload['messages']):
            response = {'content': '涨停 42 家', 'tool_calls': []}
        else:
            response = {'content': '', 'tool_calls': [{'name': 'get_market_overview', 'arguments': '{}'}]}
        event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(response)}}
        return SimpleNamespace(returncode=0, stdout=(json.dumps(event) + '\n').encode(), stderr=b'')

    monkeypatch.setattr(anyio, 'run_process', run)
    service = AgentService()
    service._client = CodexClient()
    service._source = FakeSource({'get_market_overview': '{"limit_up_qty": 42}'})
    service._system_prompt = '使用工具数据回答'
    with TestClient(create_app(agent=service)) as client:
        events = _events(client.post('/chat', json={'message': '昨天涨停几家'}))
    assert [event['type'] for event in events] == ['session', 'tool_call', 'tool_result', 'delta', 'done']
    assert events[-1]['content'] == '涨停 42 家'
