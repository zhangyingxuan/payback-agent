"""每日复盘必须在同一交易日的数据上生成并可安全存档。"""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import anyio
import pytest

from payback_agent.daily_report import build_report, read_report, write_report


class Source:
    async def call_tool(self, name, arguments):
        if name == 'get_daily_panorama':
            return json.dumps({'data_as_of': arguments['date'], 'market': {'createTime': arguments['date'] + 'T15:10:00'}, 'short_term_create_time': arguments['date'] + 'T15:35:00', 'sectors': {}, 'ladder': {}, 'down_stocks': []})
        return json.dumps({'symbol': arguments['symbol'], 'price': '42', 'datetime': '20261013150000'})


class Llm:
    async def chat(self, messages, tools=None):
        return SimpleNamespace(message={'content': '市场复盘'}, finish_reason='stop')


def test_daily_report_collects_snapshot_and_stores_it(tmp_path):
    report = anyio.run(build_report, Source(), Llm(), '2026-10-13')
    assert report['data_as_of'] == '2026-10-13'
    assert report['analysis'] == '市场复盘'
    assert len(report['global_quotes']) == 4
    write_report(tmp_path, report)
    assert read_report(tmp_path, '2026-10-13') == report


def test_report_rejects_missing_or_wrong_date(tmp_path):
    class BadSource(Source):
        async def call_tool(self, name, arguments):
            return json.dumps({'data_as_of': '2026-10-12'})
    with pytest.raises(ValueError):
        anyio.run(build_report, BadSource(), Llm(), '2026-10-13')
    with pytest.raises(ValueError):
        read_report(tmp_path, '../secret')


def test_failed_model_does_not_create_successful_report():
    class BadLlm(Llm):
        async def chat(self, messages, tools=None):
            return SimpleNamespace(message={'content': '未完成'}, finish_reason='length')
    with pytest.raises(ValueError):
        anyio.run(build_report, Source(), BadLlm(), '2026-10-13')


def test_crawler_gap_never_generates_a_report():
    class GapSource(Source):
        async def call_tool(self, name, arguments):
            return json.dumps({'data_as_of': arguments['date'], 'data_quality': {'short_term_missing': True}})
    with pytest.raises(ValueError, match='短线数据缺失'):
        anyio.run(build_report, GapSource(), Llm(), '2026-10-13')


def test_midday_data_is_not_saved_as_closing_review():
    class MiddaySource(Source):
        async def call_tool(self, name, arguments):
            return json.dumps({'data_as_of': arguments['date'], 'market': {'createTime': arguments['date'] + 'T11:31:00'}, 'short_term_create_time': arguments['date'] + 'T15:35:00'})
    with pytest.raises(ValueError, match='大盘.*11:31.*更新'):
        anyio.run(build_report, MiddaySource(), Llm(), '2026-10-13')


def test_missing_short_term_timestamp_explains_which_data_needs_refresh():
    class MissingShortSource(Source):
        async def call_tool(self, name, arguments):
            return json.dumps({'data_as_of': arguments['date'], 'market': {'createTime': arguments['date'] + 'T15:10:00'}, 'short_term_create_time': None})
    with pytest.raises(ValueError, match='短线.*未知.*更新'):
        anyio.run(build_report, MissingShortSource(), Llm(), '2026-10-13')


def test_report_http_generate_and_read(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from payback_agent import serve
    monkeypatch.setattr(serve, 'REPORT_DIR', tmp_path)
    from sqlite_memory import MemoryStore as TestMemoryStore
    service = serve.AgentService(memory=TestMemoryStore(":memory:"))
    service._client = Llm()
    service._source = Source()
    with TestClient(serve.create_app(agent=service)) as client:
        response = client.post('/daily-reports/2026-10-13')
        assert response.status_code == 200
        assert response.json()['analysis'] == '市场复盘'
        assert client.get('/daily-reports/2026-10-13').json()['data_as_of'] == '2026-10-13'
        assert client.get('/daily-reports/latest').json()['data_as_of'] == '2026-10-13'


def test_automatic_generation_only_after_weekday_close():
    from payback_agent.serve import _daily_due
    zone = timezone(timedelta(hours=8))
    assert not _daily_due(datetime(2026, 11, 9, 16, 9, tzinfo=zone))
    assert _daily_due(datetime(2026, 11, 9, 16, 10, tzinfo=zone))
    assert not _daily_due(datetime(2026, 11, 8, 18, 0, tzinfo=zone))


def test_daily_report_prompt_requests_readable_plain_text_and_quote_timestamps():
    class RecordingLlm(Llm):
        async def chat(self, messages, tools=None):
            self.messages = messages
            return await super().chat(messages, tools)

    llm = RecordingLlm()
    anyio.run(build_report, Source(), llm, '2026-10-13')
    instructions = llm.messages[0]['content']
    for item in ('纯文本', '市场概览', '板块与梯队', '风险观察', '报价时间', '不是历史交易日'):
        assert item in instructions
