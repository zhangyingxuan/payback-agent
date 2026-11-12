"""全景图必须来自本轮可信工具结果，缺失值不能显示为零。"""
import base64
import json
import re
from xml.etree import ElementTree

from payback_agent.panorama import panorama_markdown
from test_loop import FakeLlm, FakeSource, _call, _events


def _snapshot():
    return {'data_as_of': '2026-11-09',
            'market': {'shangzhengPoint': 3200.5, 'marketTurnover': 1.55,
                       'riseAmount': 3000, 'fallAmount': 1800, 'createTime': '2026-11-09T15:10:00'},
            'short_term': {'dailyLimitQuantity': 70, 'downLimitQuantity': None,
                           'sealingRate': 75, 'marketHeight': 6},
            'short_term_create_time': '2026-11-09T15:35:00',
            'sectors': {'concept_rise': [{'name': '<script>bad</script>', 'quoteChange': 2.1}]}}


def test_chart_escapes_source_text_and_displays_unknown_as_unknown():
    result = panorama_markdown(_snapshot())
    payload = re.search(r'base64,([A-Za-z0-9+/=]+)', result).group(1)
    svg = base64.b64decode(payload).decode('utf8')
    assert '2026-11-09' in svg
    assert '未提供' in svg
    assert '<script>' not in svg
    assert '&lt;script&gt;' in svg
    assert '完整性' in svg


def test_chart_shows_sector_changes_funds_and_cycle():
    """金额沿用亿元，缺失数值不伪装为零，外部字段必须转义。"""
    snapshot = _snapshot()
    snapshot['short_term']['cycle'] = '分歧<修复>'
    snapshot['funds'] = {'industry': {'in': [{'name': '半导体', 'funds': 12.3}],
                                    'out': [{'name': '银行', 'funds': -5}]}}
    snapshot['sectors']['industry_fall'] = [{'name': '银行', 'quoteChange': -1.5},
                                            {'name': '地产', 'quoteChange': None}]
    svg = base64.b64decode(panorama_markdown(snapshot).split('base64,')[1][:-1]).decode('utf8')
    text = ''.join(ElementTree.fromstring(svg).itertext())
    assert '+2.10' in text and '银行 -1.50' in text
    assert '+2.10%' not in text and '-1.50%' not in text
    assert '地产 未提供' in text
    assert '半导体 +12.30亿' in text and '银行 -5.00亿' in text
    assert '<tspan fill="#ff6b6b">+12.30亿</tspan>' in svg
    assert '<tspan fill="#5ad99c">-5.00亿</tspan>' in svg
    assert '短线周期' in svg and '分歧&lt;修复&gt;' in svg
    snapshot['data_quality'] = {'short_term_missing': True}
    svg = base64.b64decode(panorama_markdown(snapshot).split('base64,')[1][:-1]).decode('utf8')
    assert '分歧' not in svg


def test_chart_constrains_long_rows_and_keeps_invalid_values_unknown():
    """长板块名称不得跨列，零值及无效值保留各自语义。"""
    from xml.etree import ElementTree
    snapshot = _snapshot()
    snapshot['sectors']['concept_rise'] = [
        {'name': '超长概念板块测试', 'quoteChange': value} for value in (0, 'NaN', 123.45)]
    svg = base64.b64decode(panorama_markdown(snapshot).split('base64,')[1][:-1]).decode('utf8')
    ElementTree.fromstring(svg)
    assert '<tspan fill="#ffffff">0.00</tspan>' in svg and 'NaN' not in svg
    assert 'textLength="438"' in svg


def test_global_indices_show_quote_time_signed_colors_and_missing_values():
    """外围与本地复盘日分开标识，个别请求失败不伪造指数。"""
    snapshot = _snapshot()
    snapshot['global_quotes'] = [
        {'symbol': 'HSI', 'price': '26000', 'percent_change': '1.23',
         'source': '腾讯行情', 'datetime': '20261112160000'},
        {'symbol': 'SPX', 'price': '6000', 'percent_change': '-0.25'},
        {'symbol': 'IXIC', 'price': '20000', 'percent_change': 0},
        {'symbol': 'DJI', 'error': '查询失败', 'price': '99999', 'percent_change': 5},
    ]
    svg = base64.b64decode(panorama_markdown(snapshot).split('base64,')[1][:-1]).decode('utf8')
    ElementTree.fromstring(svg)
    assert '恒生指数' in svg and '26,000.00' in svg
    assert '20261112160000' in svg and '腾讯行情' in svg
    assert '<tspan fill="#ff6b6b">+1.23</tspan>' in svg
    assert '<tspan fill="#5ad99c">-0.25</tspan>' in svg
    assert '<tspan fill="#ffffff">0.00</tspan>' in svg
    assert '99,999' not in svg and '未提供' in svg
    assert '可能跨交易日' in svg


def test_chart_is_added_only_for_successful_panorama_result():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_daily_panorama')]},
                  {'role': 'assistant', 'content': '市场复盘'})
    events = _events('生成复盘全景图', llm, FakeSource({'get_daily_panorama': json.dumps(_snapshot())}))
    assert 'data:image/svg+xml;base64,' in events[-1]['content']
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_daily_panorama')]},
                  {'role': 'assistant', 'content': '无法获取数据'})
    events = _events('生成复盘全景图', llm, FakeSource({'get_daily_panorama': '{"error":"失败"}'}))
    assert 'data:image/svg+xml;base64,' not in events[-1]['content']


def test_explicit_panorama_request_fetches_snapshot_even_when_model_skips_tool():
    source = FakeSource({'get_daily_panorama': json.dumps(_snapshot())})
    llm = FakeLlm({'role': 'assistant', 'content': '市场快照'})
    events = _events('请给我2026-11-09复盘全景图', llm, source)
    assert source.calls == [('get_daily_panorama', {'date': '2026-11-09'})]
    assert any(event['type'] == 'tool_result' for event in events)
    assert 'data:image/svg+xml;base64,' in events[-1]['content']


def test_panorama_history_does_not_send_base64_to_model():
    from payback_agent.loop import ask_events
    import anyio
    source = FakeSource()
    llm = FakeLlm({'role': 'assistant', 'content': '好的'})
    async def run():
        return [event async for event in ask_events('继续', llm=llm, source=source, system_prompt='SYS',
                history=[{'role': 'assistant', 'content': '摘要\n' + panorama_markdown(_snapshot())}])]
    anyio.run(run)
    assert 'base64' not in llm.requests[0][0][1]['content']


def test_same_day_snapshot_is_shown_without_claiming_current_completion():
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    snapshot = _snapshot()
    snapshot['data_as_of'] = today
    source = FakeSource({'get_daily_panorama': json.dumps(snapshot)})
    events = _events('生成今日复盘全景图', FakeLlm({'role': 'assistant', 'content': '市场强势'}), source)
    content = events[-1]['content']
    assert '无法判断当前盘面' in content
    assert '3,000' in content and '1,800' in content
    assert '市场强势' not in content
    assert 'data:image/svg+xml;base64,' in content


def test_panorama_not_shown_if_tool_returns_wrong_requested_date():
    source = FakeSource({'get_daily_panorama': json.dumps(_snapshot())})
    events = _events('生成 2026-11-08 复盘全景图', FakeLlm({'role': 'assistant', 'content': '无可用图'}), source)
    assert 'data:image/svg+xml;base64,' not in events[-1]['content']


def test_panorama_is_saved_and_restored_with_session(tmp_path):
    from starlette.testclient import TestClient
    from test_serve import _app, _answer, _events as http_events, FakeSource as HttpSource
    path = tmp_path / 'memory.sqlite3'
    app, _, service = _app(_answer('市场摘要'), memory_path=path)
    service._source = HttpSource({'get_daily_panorama': json.dumps(_snapshot())})
    with TestClient(app) as client:
        events = http_events(client.post('/chat', json={'message': '生成 2026-11-09 复盘全景图'}))
        assert 'data:image/svg+xml;base64,' in events[-1]['content']
    app, _, _ = _app(memory_path=path)
    with TestClient(app) as client:
        saved = client.get('/sessions').json()[0]['messages'][-1]['content']
    assert saved == events[-1]['content']


def test_market_summary_only_compares_present_valid_counts():
    from payback_agent.panorama import panorama_summary
    summary = panorama_summary(_snapshot())
    assert '上涨家数多于下跌家数' in summary
    missing = _snapshot()
    missing['market']['fallAmount'] = None
    assert '上涨家数多于下跌家数' not in panorama_summary(missing)


def test_analysis_is_limited_escaped_wrapped_and_kept_above_footer():
    """图内解析严格限制300字符，外部文本不可变为SVG节点。"""
    chart = panorama_markdown(_snapshot(), '<script>解析</script>' + '行情分化。' * 100)
    root = ElementTree.fromstring(base64.b64decode(chart.split('base64,')[1][:-1]))
    texts = root.findall('{http://www.w3.org/2000/svg}text')
    title = next(i for i, node in enumerate(texts) if (node.text or '').startswith('AI 行情解析'))
    footer = next(i for i, node in enumerate(texts) if (node.text or '').startswith('外围为生成时'))
    lines = texts[title + 1:footer]
    assert len(''.join(node.text or '' for node in lines)) == 300
    assert len(lines) > 1
    assert float(lines[-1].get('y')) < float(texts[footer].get('y'))
    assert root.find('.//{http://www.w3.org/2000/svg}script') is None


def test_panorama_generates_analysis_from_current_snapshot_and_counts_usage():
    """解析独立使用本轮证据，并计入这次回答的模型用量。"""
    llm = FakeLlm({'role': 'assistant', 'content': '市场摘要'},
                  {'role': 'assistant', 'content': '快照中上涨家数多于下跌家数，板块表现存在分化。'})
    events = _events('生成2026-11-09复盘全景图', llm,
                     FakeSource({'get_daily_panorama': json.dumps(_snapshot())}))
    chart = events[-1]['content'].split('base64,')[1].split(')')[0]
    svg = base64.b64decode(chart).decode('utf8')
    assert 'AI 行情解析' in svg and '板块表现存在分化' in svg
    assert len(llm.requests) == 2
    assert json.loads(llm.requests[-1][0][-1]['content']) == _snapshot()
    assert events[-1]['usage']['total_tokens'] == 30


def test_analysis_failure_preserves_chart_with_explicit_unavailable_message():
    """附加解析失败时仍展示可信的行情数据。"""
    events = _events('生成2026-11-09复盘全景图', FakeLlm({'role': 'assistant', 'content': '摘要'}),
                     FakeSource({'get_daily_panorama': json.dumps(_snapshot())}))
    svg = base64.b64decode(events[-1]['content'].split('base64,')[1].split(')')[0]).decode('utf8')
    assert '解析暂不可用' in svg and '3,200.50' in svg


def test_times_belong_to_module_titles_instead_of_footer():
    """每个标题使用所属数据源的时间，避免资金和短线误用大盘时间。"""
    snapshot = _snapshot()
    snapshot['funds_create_time'] = '2026-11-09T15:45:00'
    snapshot['global_quotes'] = [{'symbol': 'HSI', 'datetime': '2026-11-09 16:00:00'}]
    svg = base64.b64decode(panorama_markdown(snapshot).split('base64,')[1][:-1]).decode('utf8')
    texts = ElementTree.fromstring(svg).findall('{http://www.w3.org/2000/svg}text')
    for label, stamp in [('上证指数', '15:10'), ('概念涨幅前列', '15:10'),
                          ('封板率', '15:35'), ('连板梯队（前四档）', '15:35'),
                          ('短线周期', '15:35'), ('板块资金流向 · 亿元（各前三）', '15:45'),
                          ('恒生指数', '16:00')]:
        node = next(node for node in texts if node.text == label)
        assert stamp in ''.join(node.itertext())
    assert '大盘记录创建' not in svg and '资金记录创建' not in svg


def test_index_change_and_turnover_comparison_are_in_metric_cards():
    """指数沿用涨跌色，成交额按万亿元转换为亿元且缺失不补零。"""
    snapshot = _snapshot()
    snapshot['market']['shangzhengRiseAndFall'] = -1.23
    for previous, expected in [(1.5, '放量 500.00亿元'), (1.7, '缩量 1,500.00亿元'),
                                (1.55, '持平 0.00亿元'), (None, '放量 / 缩量 未提供')]:
        snapshot['previous_market'] = {'data_as_of': '2026-11-06', 'marketTurnover': previous}
        svg = base64.b64decode(panorama_markdown(snapshot).split('base64,')[1][:-1]).decode('utf8')
        assert '<tspan fill="#5ad99c">-1.23</tspan>' in svg
        assert expected in svg
        assert '报价' not in svg and '记录' not in svg
        if previous is not None:
            assert '较2026-11-06' in svg
