"""Prompt tests: the shipped system prompt must actually carry the four levels and
the data-discipline rules it is supposed to enforce."""

from __future__ import annotations

from datetime import date

import pytest

from payback_agent import prompt


@pytest.mark.parametrize("today,expected", [(date(2026, 10, 15), "2026-10-15"),
                                           (date(2023, 1, 9), "2023-01-09")])
def test_today_is_filled_from_the_server_clock(today, expected):
    text = prompt.load_system_prompt(today=today)
    assert f"服务端今天是 {expected}" in text
    assert "{today}" not in text


def test_default_today_is_not_blank():
    assert "服务端今天是 20" in prompt.load_system_prompt()


def test_the_shipped_prompt_locks_the_four_levels_and_the_disclaimer():
    """These strings are the product. If someone trims them out of system.md, the
    agent silently stops doing the four-level chain, and only this test notices."""
    text = prompt.load_system_prompt(today=date(2026, 10, 15))
    for marker in ("①", "②", "③", "④"):
        assert marker in text
    for tool in ("get_market_overview", "get_limit_up_structure", "get_market_phase",
                 "get_position_outcomes", "get_similar_days", "get_even_board_ladder"):
        assert tool in text
    for discipline in ("data_as_of", "pooled_promotion_rate", "mean_daily_promotion_rate",
                       "share_positive", "observations", "ladder_missing_days",
                       "万亿元", "`null` ≠ `0`", "features_compared", "条件统计",
                       "open_times", "turnover_type", "上一轮的数字不是本轮的数据源"):
        assert discipline in text, discipline
    for boundary in ("不构成投资建议", "不推荐个股", "不给仓位"):
        assert boundary in text


def test_the_prompt_file_is_found_from_the_repo_root():
    assert prompt.SYSTEM_PROMPT_PATH.is_file()


def test_review_output_guides_summary_evidence_and_compact_markdown():
    text = prompt.load_system_prompt(today=date(2026, 10, 15))
    for item in ('复盘摘要', '市场全景', '短线生态', '位置与收益', '相似日与观察条件',
                 '简短问题', '不要编造', '不超过 4 列'):
        assert item in text
