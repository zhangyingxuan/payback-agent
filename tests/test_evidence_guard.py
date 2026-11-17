import json

from test_loop import FakeLlm, FakeSource, FakeStreamLlm, _call, _events


def test_general_question_with_numbers_needs_no_market_evidence():
    answer = '17 × 23 = 391'
    events = _events('计算17乘23', FakeStreamLlm({'role': 'assistant', 'content': answer}), FakeSource())
    assert events[-1]['content'] == answer
    assert ''.join(e.get('text', '') for e in events if e['type'] == 'delta') == answer


def test_one_failed_tool_keeps_answer_from_available_data():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview'),
                                                    _call('b', 'get_market_phase')]},
                  {'role': 'assistant', 'content': '已取得市场快照，短线阶段数据缺失，先说明成交情况。'})
    source = FakeSource({'get_market_overview': '{"data_as_of":"2026-10-13"}',
                         'get_market_phase': '{"error":"查询失败"}'})
    answer = _events('复盘', llm, source)[-1]['content']
    assert answer.startswith('已取得市场快照')
    assert 'get_market_phase' in answer


def test_stock_question_without_quotes_can_explain_general_conditions():
    answer = '没有该股行情，可以先解释高开、平开、低开时如何观察量价。'
    events = _events('000001明天怎么操作', FakeLlm({'role': 'assistant', 'content': answer}), FakeSource())
    assert events[-1]['content'].startswith(answer)
    assert '个股' in events[-1]['content']


def test_market_question_without_evidence_keeps_explanation_and_warning():
    answer = '暂未查到昨日涨停家数，可以解释涨停统计的口径。'
    events = _events('昨天涨停几家', FakeStreamLlm({'role': 'assistant', 'content': answer}), FakeSource())
    assert events[-1]['content'].startswith(answer)
    assert '未取得本轮行情数据' in events[-1]['content']


def test_tool_business_error_is_visible_without_overwriting_answer():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('c1', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '暂时无法取得家数，可以稍后重试。'})
    events = _events('昨天涨停几家', llm, FakeSource({'get_market_overview': '{"error":"数据库不可用"}'}))
    assert next(e for e in events if e['type'] == 'tool_result')['error'] == '数据库不可用'
    assert events[-1]['content'].startswith('暂时无法取得家数')
    assert 'get_market_overview' in events[-1]['content']


def test_inconsistent_dates_are_reported_in_authoritative_answer():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview'), _call('b', 'get_market_phase')]},
                  {'role': 'assistant', 'content': '市场偏弱'})
    source = FakeSource({'get_market_overview': json.dumps({'data_as_of': '2026-10-13'}),
                         'get_market_phase': json.dumps({'data_as_of': '2026-10-12'})})
    events = _events('复盘', llm, source)
    assert '日期不一致' in events[-1]['content']


def test_no_data_is_reported_as_tool_failure():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '无数据'})
    events = _events('行情', llm, FakeSource({'get_market_overview': '{"found":false,"hint":"无数据"}'}))
    assert next(e for e in events if e['type'] == 'tool_result')['error'] == '无数据'


def test_old_data_for_current_question_is_marked_stale():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '市场偏弱'})
    events = _events('现在能出手吗', llm, FakeSource({'get_market_overview': '{"data_as_of":"2020-01-01"}'}))
    assert '无法据此判断当前盘面' in events[-1]['content']


def test_iteration_cap_cannot_return_unverified_intermediate_answer():
    llm = FakeLlm({'role': 'assistant', 'content': '涨停42家',
                   'tool_calls': [_call('a', 'get_market_overview')]})
    events = _events('复盘', llm, FakeSource({'get_market_overview': '{"error":"失败"}'}), max_turns=1)
    assert '42' not in events[-1]['content']
    assert events[-1]['truncated'] is True


def test_stock_plan_with_only_market_data_reports_missing_stock_evidence():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '可以先观察量价关系，缺少个股价格。'})
    events = _events('000001明日支撑位', llm, FakeSource({'get_market_overview': '{"data_as_of":"2026-10-13"}'}))
    assert events[-1]['content'].startswith('可以先观察量价关系')
    assert '未取得匹配的个股数据' in events[-1]['content']


def test_next_day_stock_plan_is_not_mistaken_for_current_intraday_quote():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_stock_review')]},
                  {'role': 'assistant', 'content': '支撑位10元，明日待观察'})
    result = '{"data_as_of":"2026-10-13","stock":{"thscode":"000001.SZ"}}'
    events = _events('000001明日盘中预案', llm, FakeSource({'get_stock_review': result}))
    assert events[-1]['content'].startswith('支撑位10元')


def test_current_data_warning_mentions_source_freshness():
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '市场情况'})
    events = _events('今天市场', llm, FakeSource({'get_market_overview': json.dumps({'data_as_of': today})}))
    assert '原始数据更新时间未知' in events[-1]['content']
    assert '派生数据' not in events[-1]['content']


def test_stock_next_day_operation_requires_matching_stock_evidence():
    llm = FakeLlm({'role': 'assistant', 'content': '明天先观察成交量与趋势，不设具体价位。'})
    events = _events('000001明天怎么操作', llm, FakeSource())
    assert events[-1]['content'].startswith('明天先观察成交量')
    assert '未取得匹配的个股数据' in events[-1]['content']
    assert '买入' not in events[-1]['content']


def test_stock_strategy_rejects_evidence_for_different_stock():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_stock_review')]},
                  {'role': 'assistant', 'content': '继续观察'})
    source = FakeSource({'get_stock_review': '{"data_as_of":"2026-10-13","stock":{"thscode":"600000.SH"}}'})
    events = _events('000001明天怎么操作', llm, source)
    assert events[-1]['content'].startswith('继续观察')
    assert '未取得匹配的个股数据' in events[-1]['content']


def test_named_stock_action_requires_stock_evidence():
    llm = FakeLlm({'role': 'assistant', 'content': '明天开盘先观察量价，尚无具体行情。'})
    events = _events('平安银行明天怎么操作', llm, FakeSource())
    assert events[-1]['content'].startswith('明天开盘先观察量价')
    assert '未取得匹配的个股数据' in events[-1]['content']
    assert '买入' not in events[-1]['content']


def test_stock_evidence_requires_a_resolved_stock_code():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_stock_review')]},
                  {'role': 'assistant', 'content': '继续观察'})
    source = FakeSource({'get_stock_review': '{"data_as_of":"2026-10-13"}'})
    events = _events('这只股票明日操作策略', llm, source)
    assert events[-1]['content'].startswith('继续观察')
    assert '未取得匹配的个股数据' in events[-1]['content']


def test_today_yesterday_comparison_fetches_and_keeps_both_snapshots():
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=8))).date()
    yesterday = today - timedelta(days=1)

    class DailySource(FakeSource):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            day = today if arguments['date'] == 'today' else yesterday
            return json.dumps({'data_as_of': day.isoformat(), 'requested': day.isoformat(),
                               'limit_up_qty': 37 if day == today else 85,
                               'create_time': day.isoformat() + 'T15:35:00'})

    source = DailySource({'get_market_phase': '{}'})
    llm = FakeLlm({'role': 'assistant', 'content': '今日涨停37家，昨日85家，短线活跃度下降。'})
    events = _events('与昨日相比今日短线生态如何', llm, source)
    assert {args['date'] for _, args in source.calls} == {'today', 'yesterday'}
    answer = events[-1]['content']
    assert '今日涨停37家' in answer
    assert today.isoformat() in answer and yesterday.isoformat() in answer
    assert '历史数据不能用于判断当前盘面' not in answer
    assert '数据日期不一致' not in answer
    assert '实时' in answer
    # 操作类问题同样保留条件解释，但必须附加快照时效提示。
    action = _events('与昨日相比今日短线生态如何，现在能出手吗',
                     FakeLlm({'role': 'assistant', 'content': '先观察量价是否确认，不应仅凭快照决定。'}), source)
    assert action[-1]['content'].startswith('先观察量价是否确认')
    assert '不可据此认定为实时' in action[-1]['content']


def test_comparison_with_old_snapshots_keeps_explanation_and_stale_warning():
    llm = FakeLlm({'role': 'assistant', 'content': '仅有旧快照，暂时无法核实两日家数。'})
    source = FakeSource({'get_market_phase': '{"data_as_of":"2020-01-01"}'})
    events = _events('与昨日相比今日短线生态如何', llm, source)
    assert events[-1]['content'].startswith('仅有旧快照')
    assert '所用数据并非今日数据' in events[-1]['content']


def test_same_tool_keeps_dates_for_each_successful_call():
    class DailySource(FakeSource):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return json.dumps({'data_as_of': arguments['date']})

    calls = [_call('a', 'get_market_phase', '{"date":"2026-11-12"}'),
             _call('b', 'get_market_phase', '{"date":"2026-11-11"}')]
    llm = FakeLlm({'role': 'assistant', 'tool_calls': calls},
                  {'role': 'assistant', 'content': '两日对比'})
    events = _events('复盘两日变化', llm, DailySource({'get_market_phase': '{}'}))
    assert '2026-11-12' in events[-1]['content']
    assert '2026-11-11' in events[-1]['content']
