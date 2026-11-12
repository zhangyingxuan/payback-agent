"""双图推送只允许同日收盘数据，不依赖模型。"""
import json
import base64

import anyio
import pytest

from payback_agent.review_charts import build_review_charts


class Source:
    def __init__(self):
        self.calls = []
        self.panorama = {
            'data_as_of': '2026-11-12',
            'market': {'createTime': '2026-11-12T15:10:00', 'riseAmount': 3000},
            'short_term': {'dailyLimitQuantity': 1},
            'short_term_create_time': '2026-11-12T15:35:00',
            'funds_create_time': '2026-11-12T15:32:00',
        }
        self.ladder = {
            'data_as_of': '2026-11-12', 'create_time': '2026-11-12T15:35:00',
            'levels': [{'level': 2, 'stocks': [{'code': '000001', 'name': '测试'}]}],
        }
        self.phase = {'data_as_of': '2026-11-12', 'create_time': '2026-11-12T15:35:00',
                      'prev_cohort_promotion_rate': 0.8}

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps(self.panorama if name == 'get_daily_panorama' else
                          self.ladder if name == 'get_even_board_ladder' else
                          self.phase)


def test_builds_both_charts_with_full_ladder():
    source = Source()
    result = anyio.run(build_review_charts, source, '2026-11-12')
    assert result['data_as_of'] == '2026-11-12'
    assert [chart['name'] for chart in result['charts']] == ['panorama', 'ladder']
    assert all(chart['svg_base64'].startswith('PHN2Zy') for chart in result['charts'])
    assert ('get_even_board_ladder', {'date': '2026-11-12', 'max_stocks': 10000}) in source.calls


@pytest.mark.parametrize('field,value', [
    ('data_as_of', '2026-11-11'),
    ('short_term_create_time', '2026-11-12T11:36:00'),
    ('funds_create_time', None),
    ('funds_create_time', '2026-11-11T15:32:00'),
    ('data_quality', {'short_term_missing': True}),
])
def test_rejects_old_intraday_or_missing_data(field, value):
    source = Source()
    source.panorama[field] = value
    with pytest.raises(ValueError):
        anyio.run(build_review_charts, source, '2026-11-12')


def test_rejects_truncated_ladder_and_invalid_date():
    source = Source()
    source.ladder['truncated'] = True
    with pytest.raises(ValueError):
        anyio.run(build_review_charts, source, '2026-11-12')
    with pytest.raises(ValueError):
        anyio.run(build_review_charts, source, '../secret')


@pytest.mark.parametrize('phase', [
    {'data_as_of': '2026-11-12', 'create_time': '2026-11-12T11:35:00',
     'cycle': '午盘旧周期', 'prev_cohort_promotion_rate': 0.8},
    {'error': '派生指标尚未生成'},
])
def test_intraday_or_missing_phase_uses_close_facts_without_stale_promotion(phase):
    source = Source()
    source.phase = phase
    source.panorama['short_term']['cycle'] = '收盘周期'
    result = anyio.run(build_review_charts, source, '2026-11-12')
    svg = base64.b64decode(result['charts'][1]['svg_base64']).decode('utf8')
    assert '收盘周期' in svg
    assert '午盘旧周期' not in svg
    assert '80.00%' not in svg


def test_close_phase_supplies_promotion_rate():
    result = anyio.run(build_review_charts, Source(), '2026-11-12')
    svg = base64.b64decode(result['charts'][1]['svg_base64']).decode('utf8')
    assert '80.00%' in svg


def test_http_uses_worker_without_model_or_chat_storage():
    from starlette.testclient import TestClient
    from payback_agent import serve
    from sqlite_memory import MemoryStore

    service = serve.AgentService(memory=MemoryStore(':memory:'))
    service._source = Source()
    with TestClient(serve.create_app(agent=service)) as client:
        assert client.post('/review-charts/2026-11-12').status_code == 200
        assert service._client is None
        assert service.session_count == 0
        assert client.post('/review-charts/invalid').status_code == 400
        service._source.panorama['funds_create_time'] = None
        assert client.post('/review-charts/2026-11-12').status_code == 503
