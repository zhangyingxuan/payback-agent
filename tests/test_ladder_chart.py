"""涨停梯队图只使用本轮工具数据，保留日期和截断提示。"""
import base64
import json
import re
import xml.etree.ElementTree as ET

import pytest

from test_loop import FakeLlm, FakeSource, _events
from payback_agent.ladder_chart import ladder_markdown


def test_metrics_are_inside_compact_red_header_with_generation_timestamp():
    svg = ET.fromstring(base64.b64decode(ladder_markdown(snapshot()).split('base64,')[1][:-1]))
    ns = '{http://www.w3.org/2000/svg}'
    header = next(node for node in svg.findall(ns + 'rect') if node.attrib.get('fill') == '#c73328')
    texts = svg.findall(ns + 'text')
    for label in ('短线周期', '晋级率', '封板率', '涨停数', '跌停数'):
        node = next(node for node in texts if node.text == label)
        assert 0 < int(node.attrib['y']) < int(header.attrib['height'])
    timestamp = next(node for node in texts if (node.text or '').startswith('生成于'))
    assert re.fullmatch(r'生成于（北京时间）：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', timestamp.text)
    assert timestamp.attrib['text-anchor'] == 'end'
    code_header = next(node for node in texts if node.text == '代码')
    assert int(code_header.attrib['y']) < 160


def test_header_uses_same_day_market_phase_and_percentage_units():
    phase = {'data_as_of': '2026-11-09', 'cycle': '发酵', 'prev_cohort_promotion_rate': 0.2043,
             'sealing_rate': 81.73, 'limit_up_qty': 85, 'down_limit_qty': 3}
    source = FakeSource({'get_even_board_ladder': json.dumps(snapshot()),
                         'get_market_phase': json.dumps(phase)})
    events = _events('生成2026-11-09涨停梯队图',
                     FakeLlm({'role': 'assistant', 'content': '梯队快照'}), source)
    content = events[-1]['content']
    svg = base64.b64decode(content.split('base64,')[-1].split(')')[0]).decode('utf8')
    for text in ('短线周期', '晋级率', '封板率', '涨停数', '跌停数', '发酵', '20.43%', '81.73%', '85', '3'):
        assert text in svg
    assert ('get_market_phase', {'date': '2026-11-09', 'window_days': 1}) in source.calls


@pytest.mark.parametrize('phase', [
    {'data_as_of': '2026-11-08', 'cycle': '禁止展示'},
    {'data_as_of': '2026-11-09', 'error': '失败', 'cycle': '禁止展示'},
    {'data_as_of': '2026-11-09', 'cycle': '禁止展示', 'data_quality': {'short_term_missing': True}},
])
def test_header_rejects_wrong_date_failed_or_missing_short_term_data(phase):
    svg = base64.b64decode(ladder_markdown(snapshot(), phase).split('base64,')[1][:-1]).decode('utf8')
    assert '禁止展示' not in svg
    assert '短线周期' in svg and '未提供' in svg


def test_header_validates_percentages_and_preserves_observed_zero():
    phase = {'data_as_of': '2026-11-09', 'cycle': '<script>', 'prev_cohort_promotion_rate': 0,
             'sealing_rate': 120, 'limit_up_qty': 0, 'down_limit_qty': -1}
    svg = base64.b64decode(ladder_markdown(snapshot(), phase).split('base64,')[1][:-1]).decode('utf8')
    assert '0.00%' in svg
    assert '<script>' not in svg and '&lt;script&gt;' in svg
    assert '120.00%' not in svg
    assert all(node.text != '-1' for node in ET.fromstring(svg).findall('{http://www.w3.org/2000/svg}text'))


def test_each_tier_sorts_by_first_limit_time_stably_and_puts_unknown_last():
    stocks = [
        {'code': '000001', 'daily_time': '10:00:00,10:01:00'},
        {'code': '000002'},
        {'code': '000003', 'daily_time': '09:31:00,14:50:00'},
        {'code': '000004', 'daily_time': '09:31:00,09:32:00'},
        {'code': '000005', 'daily_time': '无效,09:30:00'},
        {'code': '000006', 'daily_time': '9:25:00'},
    ]
    data = {'data_as_of': '2026-11-11', 'levels': [
        {'level': 1, 'stocks': [{'code': '000007', 'daily_time': '09:00:00'}]},
        {'level': 3, 'stocks': stocks}]}
    svg = ET.fromstring(base64.b64decode(ladder_markdown(data).split('base64,')[1][:-1]))
    codes = [node.text for node in svg.findall('{http://www.w3.org/2000/svg}text')
             if node.attrib.get('data-column') == 'code']
    assert codes == ['000006', '000003', '000004', '000001', '000002', '000005', '000007']
    assert stocks[0]['code'] == '000001'


@pytest.mark.parametrize('funds,expected', [(1.23, '1.23'), (0, '0.00'), (None, '未提供'),
                                           (-1, '未提供'), ('NaN', '未提供')])
def test_funds_column_precedes_time_and_rows_do_not_repeat_tier(funds, expected):
    data = {'data_as_of': '2026-11-11', 'levels': [{'level': 3, 'stocks': [
        {'code': '000001', 'closing_funds': funds, 'daily_time': '09:31:00'}]}]}
    svg = ET.fromstring(base64.b64decode(ladder_markdown(data).split('base64,')[1][:-1]))
    texts = svg.findall('{http://www.w3.org/2000/svg}text')
    assert any(node.text == '封单额（亿）' for node in texts)
    assert not any(node.attrib.get('data-column') == 'level' or node.text == '板高' for node in texts)
    amount = next(node for node in texts if node.attrib.get('data-column') == 'funds')
    time = next(node for node in texts if node.attrib.get('data-column') == 'time')
    assert amount.text == expected
    assert int(amount.attrib['x']) < int(time.attrib['x'])
    assert amount.attrib['y'] == time.attrib['y']


def test_compact_table_keeps_short_stocks_on_one_line():
    data = {'data_as_of': '2026-11-11', 'levels': [{'level': 2, 'stocks': [
        {'name': '示例股份', 'code': f'{index:06d}', 'daily_time': '09:31:09',
         'reason': '机器人+汽车零部件'} for index in range(20)]}]}
    svg = ET.fromstring(base64.b64decode(ladder_markdown(data).split('base64,')[1][:-1]))
    assert int(svg.attrib['height']) < 1000
    texts = svg.findall('{http://www.w3.org/2000/svg}text')
    assert {'代码', '名称', '涨停时间', '涨停原因'} <= {node.text for node in texts}
    row = [next(node for node in texts if node.text == value)
           for value in ('000000', '示例股份', '09:31:09', '机器人+汽车零部件')]
    assert len({node.attrib['y'] for node in row}) == 1
    assert len({node.attrib['x'] for node in row}) == 4


def test_long_columns_wrap_without_losing_source_text():
    reason = '机器人+汽车零部件+' * 12
    data = {'data_as_of': '2026-11-11', 'levels': [{'level': 3, 'stocks': [
        {'name': '超长测试股票名称股份', 'code': '000001', 'daily_time': '09:31:09,14:50:00', 'reason': reason},
        {'name': '下一只', 'code': '000002'}]}]}
    svg = ET.fromstring(base64.b64decode(ladder_markdown(data).split('base64,')[1][:-1]))
    texts = svg.findall('{http://www.w3.org/2000/svg}text')
    reason_lines = [node for node in texts if node.attrib.get('data-column') == 'reason']
    assert ''.join(node.text or '' for node in reason_lines[:-1]) == reason
    time = next(node for node in texts if node.attrib.get('data-column') == 'time')
    assert time.text == '09:31:09 / 14:50:00'
    assert time.attrib['y'] == next(node for node in texts if node.text == '000001').attrib['y']
    last_reason_y = max(int(node.attrib['y']) for node in reason_lines[:-1])
    assert int(next(node for node in texts if node.text == '000002').attrib['y']) > last_reason_y


def snapshot():
    return {'data_as_of': '2026-11-09', 'stocks_total': 3, 'stocks_returned': 2,
            'truncated': True, 'levels': [
                {'level': 1, 'stocks': [{'name': '首板', 'code': '000001', 'reason': '原因'}]},
                {'level': 6, 'stocks': [{'name': '<高标>', 'code': '000002', 'reason': '科技' * 80}]}]}


def test_request_prefetches_and_renders_sorted_escaped_ladder():
    source = FakeSource({'get_even_board_ladder': json.dumps(snapshot())})
    events = _events('生成2026-11-09短线涨停梯队图片',
                     FakeLlm({'role': 'assistant', 'content': '梯队快照'}), source)
    assert source.calls == [('get_even_board_ladder', {'date': '2026-11-09'})]
    content = events[-1]['content']
    assert '![涨停梯队图](data:image/svg+xml;base64,' in content
    svg = base64.b64decode(content.split('base64,')[-1].split(')')[0]).decode('utf8')
    ET.fromstring(svg)
    assert svg.index('6板') < svg.index('1板')
    assert '&lt;高标&gt;' in svg and '000002' in svg
    assert '名单已截断' in svg and '2026-11-09' in svg


@pytest.mark.parametrize('payload', [
    {'error': '失败'}, {'found': False}, {'data_as_of': '2026-11-09', 'levels': []},
    {**snapshot(), 'data_as_of': '2026-11-08'},
    {**snapshot(), 'data_quality': {'ladder_missing': True}},
])
def test_unusable_ladder_does_not_render(payload):
    events = _events('生成2026-11-09涨停梯队图', FakeLlm({'role': 'assistant', 'content': '数据缺失'}),
                     FakeSource({'get_even_board_ladder': json.dumps(payload)}))
    assert 'data:image' not in events[-1]['content']


def test_ladder_history_removes_image_from_model_context():
    source = FakeSource({'get_even_board_ladder': json.dumps(snapshot())})
    content = _events('生成2026-11-09涨停梯队图',
                      FakeLlm({'role': 'assistant', 'content': '梯队快照'}), source)[-1]['content']
    llm = FakeLlm({'role': 'assistant', 'content': '好的'})
    _events('继续', llm, FakeSource(), history=[{'role': 'assistant', 'content': content}])
    assert 'base64' not in llm.requests[0][0][1]['content']


def test_plain_ladder_question_does_not_prefetch_image():
    source = FakeSource({'get_even_board_ladder': json.dumps(snapshot())})
    events = _events('昨天涨停梯队如何', FakeLlm({'role': 'assistant', 'content': '需要查询'}), source)
    assert not source.calls
    assert 'data:image' not in events[-1]['content']


@pytest.mark.parametrize('question,expected', [
    ('生成昨天连板梯队图', 'yesterday'), ('生成今天短线梯队图片', 'today'),
])
def test_relative_date_is_forwarded(question, expected):
    source = FakeSource({'get_even_board_ladder': json.dumps(snapshot())})
    events = _events(question, FakeLlm({'role': 'assistant', 'content': '快照'}), source)
    assert source.calls == [('get_even_board_ladder', {'date': expected})]
    assert '![涨停梯队图]' in events[-1]['content']


def test_ambiguous_date_is_resolved_by_model_tool_call():
    from test_loop import _call
    source = FakeSource({'get_even_board_ladder': json.dumps(snapshot())})
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [
        _call('a', 'get_even_board_ladder', '{"date":"2026-11-09"}')]},
        {'role': 'assistant', 'content': '快照'})
    events = _events('生成上周涨停梯队图', llm, source)
    assert len(source.calls) == 1
    assert '![涨停梯队图]' in events[-1]['content']
