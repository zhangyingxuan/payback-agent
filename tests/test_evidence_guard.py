import json

from test_loop import FakeLlm, FakeSource, FakeStreamLlm, _call, _events


def test_market_number_without_evidence_is_never_streamed():
    events = _events('昨天涨停几家', FakeStreamLlm({'role': 'assistant', 'content': '涨停42家'}), FakeSource())
    assert '42' not in ''.join(e.get('text', '') for e in events)
    assert '证据不足' in events[-1]['content']


def test_tool_business_error_is_visible_and_cannot_support_a_number():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('c1', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '涨停42家'})
    events = _events('昨天涨停几家', llm, FakeSource({'get_market_overview': '{"error":"数据库不可用"}'}))
    assert next(e for e in events if e['type'] == 'tool_result')['error'] == '数据库不可用'
    assert '证据不足' in events[-1]['content']


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


def test_stock_plan_cannot_use_market_data_as_stock_evidence():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_market_overview')]},
                  {'role': 'assistant', 'content': '支撑位10元'})
    events = _events('000001明日支撑位', llm, FakeSource({'get_market_overview': '{"data_as_of":"2026-10-13"}'}))
    assert '证据不足' in events[-1]['content']


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
    llm = FakeLlm({'role': 'assistant', 'content': '明天可以直接买入'})
    events = _events('000001明天怎么操作', llm, FakeSource())
    assert '证据不足' in events[-1]['content']
    assert '买入' not in events[-1]['content']


def test_stock_strategy_rejects_evidence_for_different_stock():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_stock_review')]},
                  {'role': 'assistant', 'content': '继续观察'})
    source = FakeSource({'get_stock_review': '{"data_as_of":"2026-10-13","stock":{"thscode":"600000.SH"}}'})
    events = _events('000001明天怎么操作', llm, source)
    assert '证据不足' in events[-1]['content']


def test_named_stock_action_requires_stock_evidence():
    llm = FakeLlm({'role': 'assistant', 'content': '明天开盘可以直接买入'})
    events = _events('平安银行明天怎么操作', llm, FakeSource())
    assert '证据不足' in events[-1]['content']
    assert '买入' not in events[-1]['content']


def test_stock_evidence_requires_a_resolved_stock_code():
    llm = FakeLlm({'role': 'assistant', 'tool_calls': [_call('a', 'get_stock_review')]},
                  {'role': 'assistant', 'content': '继续观察'})
    source = FakeSource({'get_stock_review': '{"data_as_of":"2026-10-13"}'})
    events = _events('这只股票明日操作策略', llm, source)
    assert '证据不足' in events[-1]['content']


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
    # 同样的双日数据只能支持快照比较，不能绕过实时买卖判断的校验。
    action = _events('与昨日相比今日短线生态如何，现在能出手吗',
                     FakeLlm({'role': 'assistant', 'content': '现在可以买入'}), source)
    assert '证据不足' in action[-1]['content']
    assert '现在可以买入' not in action[-1]['content']


def test_comparison_cannot_use_yesterday_as_todays_snapshot():
    llm = FakeLlm({'role': 'assistant', 'content': '今日涨停37家'})
    source = FakeSource({'get_market_phase': '{"data_as_of":"2020-01-01"}'})
    events = _events('与昨日相比今日短线生态如何', llm, source)
    assert '证据不足' in events[-1]['content']
    assert '今日涨停37家' not in events[-1]['content']


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
