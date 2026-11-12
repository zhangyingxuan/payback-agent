import json
import sys
from types import SimpleNamespace

import anyio
import pytest

from payback_agent.codex import CodexClient
from payback_agent.llm import LlmError



def _mock_process(monkeypatch, run):
    # 复用现有请求断言，同时模拟逐行读取子进程输出。
    class Process:
        def __init__(self, command):
            self.command = command
            self.stdin = self
            self.stdout = self
            self.data = b""
            self.result = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, data):
            self.data = data

        async def aclose(self):
            pass

        def __aiter__(self):
            return self.read()

        async def read(self):
            self.result = await run(self.command, input=self.data, check=False)
            if self.result.stdout:
                yield self.result.stdout + bytes([10])

        async def wait(self):
            return self.result.returncode

    async def open_process(command, **kwargs):
        return Process(command)

    monkeypatch.setattr(anyio, 'open_process', open_process)

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

    _mock_process(monkeypatch, run)
    assert anyio.run(CodexClient().chat, []).message['content'] == 'ok'


def test_codex_response_can_request_a_database_tool(monkeypatch):
    async def run(command, **kwargs):
        payload = json.loads(kwargs['input'].decode())
        assert payload['messages'][-1]['content'] == '昨天涨停多少'
        assert payload['tools'][0]['function']['name'] == 'get_limit_up_structure'
        message = {'content': None, 'tool_calls': [{'name': 'get_limit_up_structure', 'arguments': {'date': 'yesterday'}}]}
        event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(message)}}
        return SimpleNamespace(returncode=0, stdout=(json.dumps(event) + '\n').encode(), stderr=b'')
    _mock_process(monkeypatch, run)
    result = anyio.run(CodexClient().chat, [{'role': 'user', 'content': '昨天涨停多少'}], [{'function': {'name': 'get_limit_up_structure'}}])
    assert result.message['tool_calls'][0]['function'] == {'name': 'get_limit_up_structure', 'arguments': '{"date": "yesterday"}'}


def test_codex_failure_is_reported_without_exposing_stderr(monkeypatch):
    async def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'secret')
    _mock_process(monkeypatch, run)
    with pytest.raises(LlmError, match='Codex') as error:
        anyio.run(CodexClient().chat, [])
    assert 'secret' not in str(error.value)


def test_codex_backend_does_not_require_gateway_credentials(monkeypatch):
    from payback_agent import config
    monkeypatch.setenv('AGENT_LLM_BACKEND', 'codex')
    for key in ('AGENT_LLM_BASE_URL', 'AGENT_LLM_API_KEY', 'AGENT_LLM_MODEL'):
        monkeypatch.delenv(key, raising=False)
    assert isinstance(config.create_llm_client(), CodexClient)


def test_codex_stream_forwards_reasoning_and_final_answer(monkeypatch):
    async def run(command, **kwargs):
        # 默认模型可能关闭摘要，必须在独立 CLI 调用中显式请求。
        assert 'model_reasoning_summary="detailed"' in command
        events = [
            {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': '先核对数据'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message',
             'text': json.dumps({'content': '## 结论', 'tool_calls': []})}},
        ]
        return SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, events)).encode())

    _mock_process(monkeypatch, run)

    async def collect():
        return [event async for event in CodexClient().chat_stream([])]

    events = anyio.run(collect)
    assert events[0] == ('reasoning_delta', '先核对数据\n\n')
    assert events[1] == ('delta', '## 结论')
    assert events[-1][1].message['content'] == '## 结论'


def test_codex_stream_reads_real_subprocess_output(monkeypatch, tmp_path):
    from payback_agent import codex

    script = tmp_path / 'fake_codex.py'
    script.write_text(
        'import json, sys\n'
        'sys.stdin.buffer.read()\n'
        'print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"分析"}}), flush=True)\n'
        'print(json.dumps({"type":"item.completed","item":{"type":"agent_message",'
        '"text":json.dumps({"content":"结论","tool_calls":[]})}}), flush=True)\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(codex, '_codex_command', lambda: [sys.executable, str(script)])

    async def collect():
        return [event async for event in CodexClient().chat_stream([])]

    events = anyio.run(collect)
    assert events[0] == ('reasoning_delta', '分析\n\n')
    assert events[-1][1].message['content'] == '结论'


def test_codex_output_must_be_valid_json(monkeypatch):
    async def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=b'{"type":"item.completed","item":{"type":"agent_message","text":"bad"}}\n', stderr=b'')
    _mock_process(monkeypatch, run)
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

    _mock_process(monkeypatch, run)
    from sqlite_memory import MemoryStore as TestMemoryStore
    service = AgentService(memory=TestMemoryStore(":memory:"))
    service._client = CodexClient()
    service._source = FakeSource({'get_market_overview': '{"data_as_of":"2026-10-13","limit_up_qty":42}'})
    with TestClient(create_app(agent=service)) as client:
        events = _events(client.post('/chat', json={'message': '昨天涨停几家'}))
    assert [event['type'] for event in events] == ['session', 'tool_call', 'tool_result', 'delta', 'done']
    assert events[-1]['content'].startswith('涨停 42 家')
