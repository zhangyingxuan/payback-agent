"""复用可信快照绘制推送双图，不调用模型或修改会话。"""
import json
from datetime import datetime

from .daily_report import _date, _ZONE
from .panorama import panorama_markdown
from .ladder_chart import ladder_markdown


def _require_close(value, trade_date: str, label: str) -> None:
    """拒绝旧日与午盘记录，避免定时推送冒充当日收盘数据。"""
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(_ZONE)
        if stamp.date().isoformat() == trade_date and stamp.hour >= 15:
            return
    except (TypeError, ValueError):
        pass
    raise ValueError(f'{label}当日收盘数据未就绪')


async def build_review_charts(source, trade_date: str) -> dict:
    """读取同日完整证据，所有必需数据通过校验后才返回两张图。"""
    trade_date = _date(trade_date)

    async def snapshot(name: str, **arguments) -> dict:
        """工具失败或日期回退不能成为可推送的图表。"""
        result = json.loads(await source.call_tool(name, {'date': trade_date, **arguments}))
        if (not isinstance(result, dict) or result.get('error') or result.get('found') is False
                or result.get('data_as_of') != trade_date
                or (result.get('data_quality') or {}).get('short_term_missing')):
            raise ValueError(f'{name}当日数据缺失或日期不一致')
        return result

    panorama = await snapshot('get_daily_panorama')
    for label, value in (
        ('大盘', (panorama.get('market') or {}).get('createTime')),
        ('短线', panorama.get('short_term_create_time')),
        ('资金', panorama.get('funds_create_time')),
    ):
        _require_close(value, trade_date, label)
    ladder = await snapshot('get_even_board_ladder', max_stocks=10000)
    _require_close(ladder.get('create_time'), trade_date, '梯队')
    if ladder.get('truncated'):
        raise ValueError('涨停梯队名单不完整，暂不推送')
    try:
        phase = await snapshot('get_market_phase')
        _require_close(phase.get('create_time'), trade_date, '市场阶段')
    except ValueError:
        # 派生表可能尚未重建；不借用午盘晋级率，也不阻断有效收盘原始图表。
        phase = {}
    short = panorama.get('short_term') or {}
    phase = {**phase, 'data_as_of': trade_date,
             'cycle': short.get('cycle'), 'sealing_rate': short.get('sealingRate'),
             'limit_up_qty': short.get('dailyLimitQuantity'),
             'down_limit_qty': short.get('downLimitQuantity')}
    charts = []
    for name, markdown in (('panorama', panorama_markdown(panorama)),
                           ('ladder', ladder_markdown(ladder, phase))):
        if not markdown:
            raise ValueError(f'{name}图表数据不可用')
        charts.append({'name': name, 'svg_base64': markdown.split('base64,', 1)[1][:-1]})
    return {'data_as_of': trade_date, 'charts': charts}
