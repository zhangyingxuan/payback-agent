"""Tool-source tests: the session guard and argv plumbing only.

Spawning the real payback-mcp subprocess is a cross-repo integration step and lives
in ``scripts/smoke_agent.py`` -- same reason payback-mcp keeps its live checks out of
pytest: the unit suite must not need a second checkout, uv, or a database.
"""

from __future__ import annotations

import anyio
import pytest

from payback_agent import tools_source
from payback_agent.tools_source import McpStdioSource, open_stdio_source


def test_using_the_source_without_opening_it_fails_loudly():
    async def go():
        source = McpStdioSource("uv", ["--directory", ".", "run", "payback-mcp"])
        with pytest.raises(RuntimeError, match="context manager"):
            await source.list_tools()
        with pytest.raises(RuntimeError, match="context manager"):
            await source.call_tool("get_market_overview", {})
        # closing an unopened source must not raise on top of the real error
        await source.__aexit__()

    anyio.run(go)


def test_open_stdio_source_splits_command_from_args(monkeypatch):
    captured = {}

    class Recording(McpStdioSource):
        def __init__(self, command, args, env=None):
            captured["argv"] = [command, *args]
            captured["env"] = env
            super().__init__(command, args, env)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(tools_source, "McpStdioSource", Recording)

    async def go():
        async with open_stdio_source(["uv", "--directory", "D:/x/payback-mcp",
                                      "run", "payback-mcp"]) as source:
            assert isinstance(source, Recording)

    anyio.run(go)
    assert captured["argv"] == ["uv", "--directory", "D:/x/payback-mcp", "run", "payback-mcp"]
    assert captured["env"] is None


def test_research_source_merges_tools_and_delegates_existing_calls(tmp_path, monkeypatch):
    """研究任务不能覆盖原行情工具，也不能把取数成功包装为统计验证。"""
    from types import SimpleNamespace
    from payback_agent.research import ResearchBackend
    from payback_agent.research_jobs import ResearchJobs
    from payback_agent.tools_source import ResearchSource
    from payback_agent.research_iwencai import IwencaiSource
    import json
    monkeypatch.setattr(IwencaiSource, 'available', property(lambda self: False))

    class Existing:
        async def list_tools(self):
            return [SimpleNamespace(name='get_market_overview')]

        async def call_tool(self, name, args):
            return 'existing-result'

    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'data'})
    source = ResearchSource(Existing(), ResearchBackend(), jobs)

    async def go():
        names = [tool.name for tool in await source.list_tools()]
        assert 'get_market_overview' in names and 'research_start' in names
        assert await source.call_tool('get_market_overview', {}) == 'existing-result'
        catalog = json.loads(await source.call_tool('research_sources', {}))
        assert catalog['aiwencai_configured'] is False
        unknown = json.loads(await source.call_tool('research_result', {'task_id': '../secret'}))
        assert unknown['error']
        invalid = json.loads(await source.call_tool('research_start', {'kind': 'python', 'arguments': {}}))
        assert invalid['error']
        extra = json.loads(await source.call_tool('research_sources', {'extra': 'not allowed'}))
        assert extra['error']

    try:
        anyio.run(go)
    finally:
        jobs.close()


def test_research_source_refuses_name_collisions(tmp_path):
    from types import SimpleNamespace
    from payback_agent.research import ResearchBackend
    from payback_agent.research_jobs import ResearchJobs
    from payback_agent.tools_source import ResearchSource

    class Existing:
        async def list_tools(self):
            return [SimpleNamespace(name='research_start')]

    jobs = ResearchJobs(tmp_path, lambda *args: {})

    async def go():
        with pytest.raises(ValueError, match='同名'):
            await ResearchSource(Existing(), ResearchBackend(), jobs).list_tools()

    try:
        anyio.run(go)
    finally:
        jobs.close()


def test_opt_in_research_source_lifecycle(monkeypatch, tmp_path):
    """启用才聚合工具，退出时关闭研究队列；原 MCP 生命周期不改变。"""
    from payback_agent.tools_source import ResearchSource

    class Existing(McpStdioSource):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(tools_source, 'McpStdioSource', Existing)
    monkeypatch.setenv('AGENT_RESEARCH_ENABLED', 'true')
    monkeypatch.setenv('AGENT_RESEARCH_DIR', str(tmp_path))

    async def go():
        async with open_stdio_source(['uv']) as source:
            assert isinstance(source, ResearchSource)
            jobs = source.jobs
        with pytest.raises(ValueError, match='关闭'):
            jobs.start('fetch', {})

    anyio.run(go)


@pytest.mark.parametrize('retry', [False, True])
def test_real_agent_loop_can_submit_and_read_research_task(tmp_path, monkeypatch, retry):
    """真实工具循环经过来源、后台提交、状态和分段结果，保留原始证据。"""
    import json
    from payback_agent.llm import ChatResult
    from payback_agent.loop import ask
    from payback_agent.research import ResearchBackend, ResearchPolicyError
    from payback_agent.research_jobs import ResearchJobs
    from payback_agent.tools_source import ResearchSource

    def fetch(url, **kwargs):
        if retry and url == 'https://example.com':
            raise ResearchPolicyError('首次取数失败')
        return {'content_type': 'text/plain', 'text': '公开文档示例'}

    monkeypatch.setattr('payback_agent.research.fetch', fetch)

    class Existing:
        async def list_tools(self):
            return []

    class Llm:
        def __init__(self):
            self.step = 0

        async def chat(self, messages, tools=None):
            self.step += 1
            if self.step == 1:
                name, arguments = 'research_sources', {}
            elif self.step == 2:
                name, arguments = 'research_start', {
                    'kind': 'fetch', 'arguments': {'url': 'https://example.com'}, 'wait_seconds': 2}
            elif self.step == 3 and retry:
                assert json.loads(messages[-1]['content'])['status'] == 'failed'
                name, arguments = 'research_start', {
                    'kind': 'fetch', 'arguments': {'url': 'https://example.org'}, 'wait_seconds': 2}
            else:
                assert json.loads(messages[-1]['content'])['status'] == 'succeeded'
                return ChatResult(message={'role': 'assistant', 'content': '公开资料已读取；统计口径未经验证。'},
                                  finish_reason='stop', usage={})
            return ChatResult(message={'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'call-{self.step}', 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}]}, finish_reason='tool_calls', usage={})

    backend = ResearchBackend()
    jobs = ResearchJobs(tmp_path, backend.run)

    async def go():
        answer = await ask('读取公开示例文档', llm=Llm(),
                           source=ResearchSource(Existing(), backend, jobs), system_prompt='测试')
        assert [call.name for call in answer.tool_calls] == ['research_sources'] + ['research_start'] * (1 + retry)
        evidence = json.loads(answer.tool_calls[-1].result)
        assert evidence['result']['text'] == '公开文档示例'
        assert evidence['result']['domain_verified'] is False and evidence['result']['fetched_at']
        assert not answer.truncated

    try:
        anyio.run(go)
    finally:
        jobs.close()


def test_borrowed_research_runtime_survives_mcp_context_restart(tmp_path, monkeypatch):
    from payback_agent.research_runtime import ResearchRuntime
    from payback_agent.research import ResearchBackend
    from payback_agent.research_jobs import ResearchJobs

    class Existing(McpStdioSource):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(tools_source, 'McpStdioSource', Existing)
    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'data'})
    runtime = ResearchRuntime(ResearchBackend(), jobs)

    async def go():
        async with open_stdio_source(['uv'], research=runtime) as first:
            assert first.jobs is jobs
        async with open_stdio_source(['uv'], research=runtime) as second:
            assert second.jobs is jobs
            assert second.jobs.start('fetch', {})['task_id']

    try:
        anyio.run(go)
    finally:
        runtime.close()


def test_invalid_python_image_only_disables_code_execution(monkeypatch, tmp_path):
    from payback_agent.research_runtime import create_research_runtime
    monkeypatch.setenv('AGENT_RESEARCH_ENABLED', 'true')
    monkeypatch.setenv('AGENT_RESEARCH_DIR', str(tmp_path))
    monkeypatch.setenv('AGENT_RESEARCH_PYTHON_IMAGE', 'python:latest')
    runtime = create_research_runtime()
    try:
        assert runtime.jobs.sandbox is None
        assert '摘要' in runtime.jobs.sandbox_error
        assert runtime.backend.sources()['preferred_a_share_source'] == 'aiwencai'
    finally:
        runtime.close()


def test_research_tool_interface_runs_python_and_exports_draft(tmp_path):
    import json
    from payback_agent.research import ResearchBackend
    from payback_agent.research_jobs import ResearchJobs
    from payback_agent.tools_source import ResearchSource

    class Existing:
        async def list_tools(self):
            return []

    class Sandbox:
        def run(self, code, inputs, cancelled):
            assert json.loads(inputs['prices']) == [1, 2]
            return {'text': '{"count":2}', 'source': 'python-sandbox'}

    jobs = ResearchJobs(tmp_path, lambda *args: {'text': '[1,2]'}, sandbox=Sandbox())
    source = ResearchSource(Existing(), ResearchBackend(), jobs)

    async def completed(task_id):
        for _ in range(100):
            if jobs.status(task_id)['status'] not in ('queued', 'running'):
                return
            await anyio.sleep(.01)
        raise AssertionError('后台任务未结束')

    async def go():
        fetched = json.loads(await source.call_tool('research_start', {'kind': 'fetch', 'arguments': {}}))
        await completed(fetched['task_id'])
        computed = json.loads(await source.call_tool('research_start', {'kind': 'python', 'arguments': {
            'code': 'result = {"count":2}', 'inputs': {'prices': fetched['task_id']}}}))
        await completed(computed['task_id'])
        result = json.loads(await source.call_tool('research_result', {'task_id': computed['task_id']}))
        assert json.loads(result['text'])['count'] == 2 and result['domain_verified'] is False
        draft = json.loads(await source.call_tool('research_export_tool', {
            'task_id': computed['task_id'], 'name': 'count_prices'}))
        assert draft['status'] == 'review_required'

    try:
        anyio.run(go)
    finally:
        jobs.close()
