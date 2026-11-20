"""爱问财接入测试，按现有 Node 后端协议构造响应，不访问上游。"""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from payback_agent.research import ResearchBackend, ResearchPolicyError
from payback_agent.research_iwencai import IwencaiSource


def response(total=123, rows=None, condition=None):
    """保留后端已使用的 components/data/meta/extra 结构。"""
    return {'status_code': 0, 'data': {'answer': [{'txt': [{'content': {'components': [{
        'cid': 1, 'puuid': 'uuid', 'data': {
            'datas': [{'股票代码': '600000', '股票简称': '示例'}] if rows is None else rows,
            'meta': {'extra': {'row_count': total, 'condition': condition or [{'指标': '最新价'}]}}}
    }]}}]}]}}


def ready_source(monkeypatch, tmp_path):
    """只伪造共享库位置和令牌生成，网络请求必须由假 fetch 接收。"""
    module = tmp_path / 'node_modules' / 'pay-back-core' / 'lib'
    module.mkdir(parents=True)
    (module / 'index.cjs').write_text('// fixture', encoding='utf-8')
    source = IwencaiSource(server_dir=tmp_path)
    monkeypatch.setattr('payback_agent.research_iwencai.shutil.which', lambda name: 'node')
    monkeypatch.setattr('payback_agent.research_iwencai.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=b'x' * 80))
    monkeypatch.setattr('payback_agent.research_iwencai.REQUEST_INTERVAL', 0)
    return source


def test_reuses_backend_url_form_and_shared_create_v(monkeypatch, tmp_path):
    source = ready_source(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr('payback_agent.research_iwencai.fetch', lambda url, **kwargs:
        calls.append((url, kwargs)) or {'content_type': 'application/json', 'text': json.dumps(response())})
    result = source.query('A股最新价低于2024年9月24日收盘价', per_page=20)
    url, request = calls[0]
    assert url == 'https://www.iwencai.com/unifiedwap/unified-wap/v2/result/get-robot-data'
    assert request['headers']['hexin-v'] == 'x' * 80
    assert request['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    form = parse_qs(request['body'].decode())
    assert form['question'] == ['A股最新价低于2024年9月24日收盘价']
    assert form['perpage'] == ['20'] and form['secondary_intent'] == ['stock']
    payload = json.loads(result['text'])
    assert payload['reported_total'] == 123 and payload['returned_count'] == 1
    assert payload['complete'] is False and payload['condition_verified'] is False
    assert payload['parsed_conditions'] == [{'指标': '最新价'}]
    assert result['domain_verified'] is False


def test_valid_zero_is_not_a_failure(monkeypatch, tmp_path):
    source = ready_source(monkeypatch, tmp_path)
    monkeypatch.setattr('payback_agent.research_iwencai.fetch', lambda *args, **kwargs:
        {'content_type': 'application/json', 'text': json.dumps(response(0, []))})
    payload = json.loads(source.query('A股，价格低于0')['text'])
    assert payload['reported_total'] == 0 and payload['complete'] is True


@pytest.mark.parametrize('payload', [
    {}, {'status_code': 403}, {'status_code': 0, 'data': {'answer': []}},
    response(None), response(-1), response(True), response(0),
    response(2, [{'股票代码': '600000'}, {'股票代码': '600000'}]),
])
def test_errors_and_invalid_tables_never_become_zero(monkeypatch, tmp_path, payload):
    source = ready_source(monkeypatch, tmp_path)
    monkeypatch.setattr('payback_agent.research_iwencai.fetch', lambda *args, **kwargs:
        {'content_type': 'application/json', 'text': json.dumps(payload)})
    with pytest.raises(ResearchPolicyError):
        source.query('A股')


@pytest.mark.parametrize('question,per_page', [('', 20), ('x' * 1001, 20), ('A股', True), ('A股', 101)])
def test_invalid_input_never_executes_node(monkeypatch, tmp_path, question, per_page):
    source = ready_source(monkeypatch, tmp_path)
    monkeypatch.setattr('payback_agent.research_iwencai.subprocess.run', lambda *args, **kwargs:
                        pytest.fail('无效参数不能运行 Node'))
    with pytest.raises(ResearchPolicyError):
        source.query(question, per_page=per_page)


def test_missing_node_or_core_fails_without_fallback(monkeypatch, tmp_path):
    source = IwencaiSource(server_dir=tmp_path)
    monkeypatch.setattr('payback_agent.research_iwencai.shutil.which', lambda name: None)
    assert source.available is False
    with pytest.raises(ResearchPolicyError, match='共享库'):
        source.query('A股')


def test_backend_routes_builtin_iwencai(monkeypatch):
    backend = ResearchBackend()
    monkeypatch.setattr(backend.iwencai, 'query', lambda question, per_page=20:
                        {'text': json.dumps({'question': question, 'reported_total': 7})})
    monkeypatch.setattr(type(backend.iwencai), 'available', property(lambda self: True))
    result = backend.run('request', {'source': 'aiwencai', 'operation': 'query',
                                     'parameters': {'question': 'A股', 'per_page': 20}})
    assert json.loads(result['text'])['reported_total'] == 7
    assert backend.sources()['aiwencai_configured'] is True
    assert any(item['id'] == 'aiwencai' for item in backend.sources()['sources'])


def test_admin_registered_source_has_priority_over_builtin(monkeypatch, tmp_path):
    path = tmp_path / 'sources.json'
    path.write_text(json.dumps({'aiwencai': {'origin': 'https://api.example.com', 'operations': {
        'query': {'method': 'POST', 'path': '/readonly', 'read_only': True,
                  'parameters': {'question': {'type': 'string', 'required': True}}}}}}), encoding='utf-8')
    backend = ResearchBackend(path)
    monkeypatch.setattr(backend.iwencai, 'query', lambda **kwargs: pytest.fail('不能绕过管理员来源配置'))
    calls = []
    monkeypatch.setattr('payback_agent.research.fetch', lambda url, **kwargs:
        calls.append(url) or {'content_type': 'application/json', 'text': '{}'})
    backend.run('request', {'source': 'aiwencai', 'operation': 'query', 'parameters': {'question': 'A股'}})
    assert calls == ['https://api.example.com/readonly']


def test_shareholder_table_without_screening_conditions_is_rejected(monkeypatch, tmp_path):
    source = ready_source(monkeypatch, tmp_path)
    payload = response(10)
    payload['data']['answer'][0]['txt'][0]['content']['components'][0]['data']['meta']['extra'].pop('condition')
    monkeypatch.setattr('payback_agent.research_iwencai.fetch', lambda *args, **kwargs:
        {'content_type': 'application/json', 'text': json.dumps(payload)})
    with pytest.raises(ResearchPolicyError):
        source.query('600000', per_page=1)


def test_pages_use_existing_gateway_protocol_and_report_complete(monkeypatch, tmp_path):
    source = ready_source(monkeypatch, tmp_path)
    payload = response(2, [{'股票代码': '600000'}], condition='[{"indexName":"股票市场类型"}]')
    calls = []

    def fake(url, **kwargs):
        calls.append((url, parse_qs(kwargs['body'].decode())))
        data = payload if len(calls) == 1 else {'answer': {'components': [{'data': {
            'datas': [{'股票代码': '600001'}]}}]}}
        return {'content_type': 'application/json', 'text': json.dumps(data)}

    monkeypatch.setattr('payback_agent.research_iwencai.fetch', fake)
    result = json.loads(source.query('A股', per_page=1, max_pages=2)['text'])
    assert calls[1][0] == 'https://www.iwencai.com/gateway/urp/v7/landing/getDataList'
    assert calls[1][1]['page'] == ['2'] and calls[1][1]['comp_id'] == ['1']
    assert calls[1][1]['uuid'] == ['uuid']
    assert json.loads(calls[1][1]['condition'][0]) == [{'indexName': '股票市场类型'}]
    assert result['returned_count'] == 2 and result['complete'] is True
    assert result['pages_fetched'] == 2 and result['condition_verified'] is False


def test_cancellation_stops_following_pages(monkeypatch, tmp_path):
    import threading
    source = ready_source(monkeypatch, tmp_path)
    cancelled = threading.Event()
    calls = []

    def fake(*args, **kwargs):
        calls.append(True)
        cancelled.set()
        return {'content_type': 'application/json', 'text': json.dumps(response(2))}

    monkeypatch.setattr('payback_agent.research_iwencai.fetch', fake)
    with pytest.raises(ResearchPolicyError, match='取消'):
        source.query('A股', per_page=1, max_pages=2, cancelled=cancelled)
    assert len(calls) == 1


def test_nested_paging_error_keeps_first_page_but_never_reports_complete(monkeypatch, tmp_path):
    source = ready_source(monkeypatch, tmp_path)
    replies = iter([response(2), {'status_code': '0', 'answer': {'components': [{'data': {
        'datas': [], 'meta': {'extra': {'status_code': -1201, 'row_count': 0}}}}]}}])
    monkeypatch.setattr('payback_agent.research_iwencai.fetch', lambda *args, **kwargs:
        {'content_type': 'application/json', 'text': json.dumps(next(replies))})
    payload = json.loads(source.query('A股', per_page=1, max_pages=2)['text'])
    assert payload['reported_total'] == 2 and payload['returned_count'] == 1
    assert payload['complete'] is False and payload['stop_reason'] == 'upstream_paging_error'
    assert payload['paging_error_code'] == -1201
    assert payload['pages_requested'] == 2 and payload['pages_fetched'] == 1


@pytest.mark.parametrize('code', [-1201, '-1201', False, None])
def test_nested_first_page_error_is_never_a_valid_zero(monkeypatch, tmp_path, code):
    source = ready_source(monkeypatch, tmp_path)
    payload = response(0, [])
    payload['data']['answer'][0]['txt'][0]['content']['components'][0]['data']['meta']['extra']['status_code'] = code
    monkeypatch.setattr('payback_agent.research_iwencai.fetch', lambda *args, **kwargs:
        {'content_type': 'application/json', 'text': json.dumps(payload)})
    with pytest.raises(ResearchPolicyError):
        source.query('A股，最新价小于0')
