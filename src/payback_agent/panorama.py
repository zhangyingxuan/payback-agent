"""将全景工具返回的原始快照绘制为可保存的 SVG 图片。"""
from __future__ import annotations

import base64
import html
import textwrap
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def _number(value: Any, *, places: int = 2, nonnegative: bool = False,
            maximum: int | None = None) -> str:
    """只展示有限数值；缺失或脏数据不伪装成零。"""
    if value is None or isinstance(value, bool) or len(str(value)) > 40:
        return '未提供'
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or abs(number) > Decimal('1e12')
                or (nonnegative and number < 0) or (maximum is not None and number > maximum)
                or (places == 0 and number != number.to_integral_value())):
            return '未提供'
        return f'{number:,.{places}f}'
    except (InvalidOperation, ValueError):
        return '未提供'


def _change(value: Any, unit: str = '') -> str:
    """仅对有效涨跌值着色；未知与零区分，外部内容统一转义。"""
    formatted = _number(value)
    color = '#bed1c8'
    if formatted != '未提供':
        number = Decimal(str(value))
        color = '#ff6b6b' if number > 0 else '#5ad99c' if number < 0 else '#ffffff'
        formatted = ('+' if number > 0 else '') + formatted + unit
    return f'<tspan fill="{color}">{html.escape(formatted)}</tspan>'


def _time(value: Any) -> str:
    """创建时间只用于标记记录时间，不推断数据完成状态。"""
    try:
        return datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return '未知'


def panorama_markdown(snapshot: dict, analysis: str = '') -> str:
    """输出只包含本轮数据的图表；格式无效时不生成图像。"""
    if not isinstance(snapshot, dict) or snapshot.get('error') or snapshot.get('found') is False:
        return ''
    day = snapshot.get('data_as_of')
    try:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            return ''
    except ValueError:
        return ''
    market, short = snapshot.get('market'), snapshot.get('short_term')
    if not isinstance(market, dict) or not isinstance(short, dict):
        return ''
    quality = snapshot.get('data_quality')
    if isinstance(quality, dict) and quality.get('short_term_missing'):
        short = {}
    metrics = [
        ('上证指数', _number(market.get('shangzhengPoint'), nonnegative=True), '点'),
        ('市场成交额', _number(market.get('marketTurnover'), nonnegative=True), '万亿元'),
        ('上涨 / 下跌', _number(market.get('riseAmount'), places=0, nonnegative=True) + ' / ' +
         _number(market.get('fallAmount'), places=0, nonnegative=True), '家'),
        ('涨停 / 跌停', _number(short.get('dailyLimitQuantity'), places=0, nonnegative=True) + ' / ' +
         _number(short.get('downLimitQuantity'), places=0, nonnegative=True), '家'),
        ('封板率', _number(short.get('sealingRate'), nonnegative=True, maximum=100), '%'),
        ('最高连板', _number(short.get('marketHeight'), places=0, nonnegative=True), '板'),
    ]
    if all(value.replace(' / ', '') == '未提供' or all(part == '未提供' for part in value.split(' / '))
           for _, value, _ in metrics):
        return ''
    sectors = snapshot.get('sectors')
    sectors = sectors if isinstance(sectors, dict) else {}
    def sector_line(x: int, y: int, items: Any, field: str = 'quoteChange', unit: str = '') -> str:
        """板块名称保持中性色，数值独立着色，涨跌幅不再附加百分号。"""
        parts, plain = [], []
        if isinstance(items, list):
            for item in items[:3]:
                if not isinstance(item, dict) or not item.get('name'):
                    continue
                name = str(item['name'])[:8]
                parts.append(html.escape(name, quote=True) + ' ' + _change(item.get(field), unit))
                plain.append(name + ' ' + _number(item.get(field)) + unit)
        fallback = '名单为空' if isinstance(items, list) else '未提供'
        text = ' / '.join(plain) if plain else fallback
        markup = ' / '.join(parts) if parts else fallback
        return f'<text x="{x}" y="{y}" class="detail"{fit_line(text)}>{markup}</text>'

    def fit_line(text: str) -> str:
        """按中英文字符宽度估算，仅收缩超宽行以保持两列间距。"""
        width = sum(7 if ord(char) < 128 else 14 for char in text)
        return ' textLength="438" lengthAdjust="spacingAndGlyphs"' if width > 438 else ''

    # 两列各保留一半画布宽度，长名单压缩至列宽，防止跨列覆盖。
    funds = snapshot.get('funds')
    funds = funds if isinstance(funds, dict) else {}
    fund_blocks = []
    for index, (key, label) in enumerate((('concept', '概念'), ('industry', '行业'))):
        group = funds.get(key)
        group = group if isinstance(group, dict) else {}
        y = 573 + index * 58
        for direction, x, title in (('in', 36, '净流入'), ('out', 510, '净流出')):
            fund_blocks.append(f'<text x="{x}" y="{y}" class="label">{label}{title}</text>'
                               + sector_line(x, y + 24, group.get(direction), 'funds', '亿'))

    # 报价可能跨交易日，逐项展示供应商时间，不把最新行情标为历史收盘。
    quotes = snapshot.get('global_quotes')
    quotes = quotes if isinstance(quotes, list) else []
    quote_by_symbol = {item.get('symbol'): item for item in quotes
                       if isinstance(item, dict) and isinstance(item.get('symbol'), str)}
    global_blocks = []
    for index, (symbol, name) in enumerate((('HSI', '恒生指数'), ('SPX', '标普500'),
                                           ('IXIC', '纳斯达克'), ('DJI', '道琼斯'))):
        quote = quote_by_symbol.get(symbol, {})
        if quote.get('error'):
            quote = {}
        x, y = 36 + index % 2 * 474, 790 + index // 2 * 70
        price = _number(quote.get('price'), nonnegative=True)
        stamp = html.escape(str(quote.get('datetime') or '未知')[:30], quote=True)
        source = html.escape(str(quote.get('source') or '未知')[:16], quote=True)
        global_blocks.append(f'<text x="{x}" y="{y}" class="label">{name}'
                             f'<tspan dx="12" class="quote-time">{stamp}</tspan></text>'
                             f'<text x="{x}" y="{y + 23}" class="detail">{price}　'
                             + _change(quote.get('percent_change')) + '</text>'
                             f'<text x="{x + 240}" y="{y + 23}" class="quote-time">{source}</text>')

    ladder = snapshot.get('ladder')
    if isinstance(quality, dict) and (quality.get('short_term_missing') or quality.get('ladder_missing')):
        ladder = None
    groups = []
    if isinstance(ladder, dict):
        for key in sorted((key for key in ladder if isinstance(key, str) and key.isascii()
                           and key.isdigit() and 0 < int(key) <= 30), key=int, reverse=True)[:4]:
            if isinstance(ladder[key], list):
                groups.append(f'{key}板 {len(ladder[key])}家')
    ladder_text = ' / '.join(groups) if groups else '未提供'
    esc = lambda value: html.escape(str(value), quote=True)
    # 标题旁直接显示时间，不附加“记录”“报价”前缀。
    market_time = f'<tspan dx="12" class="quote-time">{esc(_time(market.get("createTime")))}</tspan>'
    short_time = f'<tspan dx="12" class="quote-time">{esc(_time(snapshot.get("short_term_create_time")))}</tspan>'
    funds_time = f'<tspan dx="12" class="quote-time">{esc(_time(snapshot.get("funds_create_time")))}</tspan>'
    turnover_change = '放量 / 缩量 未提供'
    previous = snapshot.get('previous_market')
    if isinstance(previous, dict):
        # 用十进制计算亿元差值，标注比较日期，避免浮点误差或把跨日记录混为当日。
        previous_day = previous.get('data_as_of')
        if (isinstance(previous_day, str) and previous_day < day
                and _number(market.get('marketTurnover'), nonnegative=True) != '未提供'
                and _number(previous.get('marketTurnover'), nonnegative=True) != '未提供'):
            delta = (Decimal(str(market['marketTurnover'])) - Decimal(str(previous['marketTurnover']))) * 10000
            direction = '放量' if delta > 0 else '缩量' if delta < 0 else '持平'
            turnover_change = f'较{previous_day} {direction} {_number(abs(delta))}亿元'
    blocks = []
    for index, (label, value, unit) in enumerate(metrics):
        x, y = 36 + index % 3 * 306, 130 + index // 3 * 110
        value_markup = esc(value)
        # 上涨、下跌家数按语义区分颜色，零家不误用涨跌色。
        if label in ('上涨 / 下跌', '涨停 / 跌停'):
            value_markup = ' / '.join(
                f'<tspan fill="{color if part not in ("0", "未提供") else "#ffffff"}">{esc(part)}</tspan>'
                for part, color in zip(value.split(' / '), ('#ff6b6b', '#5ad99c')))
        blocks.append(f'<rect x="{x}" y="{y}" width="286" height="94" rx="12" fill="#183631" stroke="#34534a"/>'
                      f'<text x="{x + 16}" y="{y + 29}" class="label">{esc(label)}{market_time if index < 3 else short_time}</text>'
                      f'<text x="{x + 16}" y="{y + 65}" class="value">{value_markup}</text>'
                      f'<text x="{x + 270}" y="{y + 65}" text-anchor="end" class="unit">{esc(unit)}</text>')
        if index == 0:
            blocks.append(f'<text x="{x + 16}" y="{y + 83}" class="quote-time">涨跌幅 '
                          + _change(market.get('shangzhengRiseAndFall')) + '</text>')
        elif index == 1:
            blocks.append(f'<text x="{x + 16}" y="{y + 83}" class="quote-time">{esc(turnover_change)}</text>')
    # 按字符限制解析长度并换行，动态下移页脚，防止长回复覆盖图表。
    analysis = ' '.join(analysis.split()) if isinstance(analysis, str) else ''
    analysis = analysis if len(analysis) <= 300 else analysis[:299] + '…'
    lines = textwrap.wrap(analysis, width=62)
    extra_height = 45 + len(lines) * 22 if lines else 0
    analysis_blocks = ('<text x="36" y="925" fill="#d8b983" font-size="15">AI 行情解析 · 基于本次数据快照</text>'
                       + ''.join(f'<text x="36" y="{952 + index * 22}" class="detail">{esc(line)}</text>'
                                 for index, line in enumerate(lines))) if lines else ''
    height = 994 + extra_height
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="990" height="{height}" viewBox="0 0 990 {height}" role="img" aria-label="市场全景数据快照">'
        '<style>text{font-family:Microsoft YaHei,PingFang SC,sans-serif}.label{fill:#bed1c8;font-size:15px}'
        '.value{fill:#f1f6f2;font-size:21px;font-weight:600}.unit{fill:#bdcfc7;font-size:13px}'
        '.detail{fill:#e4eee8;font-size:14px}.quote-time{fill:#b6c9c0;font-size:11px}</style>'
        f'<rect width="990" height="{height}" rx="20" fill="#102623"/>'
        '<text x="36" y="48" fill="#c9a570" font-size="12" letter-spacing="2">PAYBACK / MARKET SNAPSHOT</text>'
        '<text x="36" y="88" fill="#f1f6f2" font-size="23" font-weight="600">市场复盘全景</text>'
        f'<text x="954" y="88" fill="#b9cec4" font-size="14" text-anchor="end">数据日期 {esc(day)}</text>'
        + ''.join(blocks) +
        f'<text x="36" y="389" fill="#d8b983" font-size="15">概念涨幅前列{market_time}</text>'
        + sector_line(36, 417, sectors.get('concept_rise')) +
        f'<text x="510" y="389" fill="#d8b983" font-size="15">概念跌幅前列{market_time}</text>'
        + sector_line(510, 417, sectors.get('concept_fall')) +
        f'<text x="36" y="462" fill="#d8b983" font-size="15">行业涨幅前列{market_time}</text>'
        + sector_line(36, 490, sectors.get('industry_rise')) +
        f'<text x="510" y="462" fill="#d8b983" font-size="15">行业跌幅前列{market_time}</text>'
        + sector_line(510, 490, sectors.get('industry_fall')) +
        f'<text x="36" y="541" fill="#d8b983" font-size="15">板块资金流向 · 亿元（各前三）{funds_time}</text>'
        + ''.join(fund_blocks) +
        f'<text x="36" y="695" fill="#d8b983" font-size="15">连板梯队（前四档）{short_time}</text>'
        f'<text x="36" y="723" class="detail">{esc(ladder_text)}</text>'
        f'<text x="510" y="695" fill="#d8b983" font-size="15">短线周期{short_time}</text>'
        f'<text x="510" y="723" class="detail">{esc(str(short.get("cycle") or "未提供")[:24])}</text>'
        '<text x="36" y="757" fill="#d8b983" font-size="15">外围指数 · 最新可用行情（涨跌幅省略百分号）</text>'
        + ''.join(global_blocks) +
        analysis_blocks +
        f'<text x="36" y="{943 + extra_height}" class="quote-time">外围为生成时最新行情，可能跨交易日，延迟状态未知；涨红、跌绿、零白。</text>'
        f'<text x="36" y="{966 + extra_height}" fill="#b6c9c0" font-size="12">原始数据更新与收盘完整性未核实；不代表实时行情，不构成投资建议。</text>'
        '</svg>'
    )
    encoded = base64.b64encode(svg.encode('utf8')).decode('ascii')
    return '![复盘全景图](data:image/svg+xml;base64,' + encoded + ')'


def panorama_summary(snapshot: dict) -> str:
    """当完成状态未知时，只输出可核对的源数据快照，不输出盘面判断。"""
    market, short = snapshot.get('market'), snapshot.get('short_term')
    if not isinstance(market, dict) or not isinstance(short, dict):
        return ''
    quality = snapshot.get('data_quality')
    if isinstance(quality, dict) and quality.get('short_term_missing'):
        short = {}
    rise = _number(market.get('riseAmount'), places=0, nonnegative=True)
    fall = _number(market.get('fallAmount'), places=0, nonnegative=True)
    up = _number(short.get('dailyLimitQuantity'), places=0, nonnegative=True)
    down = _number(short.get('downLimitQuantity'), places=0, nonnegative=True)
    breadth = ''
    if rise != '未提供' and fall != '未提供':
        # 只比较已提供的家数，避免把缺失值或零样本解释为市场方向。
        r, f = int(rise.replace(',', '')), int(fall.replace(',', ''))
        if r or f:
            relation = '多于' if r > f else '少于' if r < f else '等于'
            breadth = f'本次记录中上涨家数{relation}下跌家数。\n'
    return (f'**原始记录快照**（数据日期：{snapshot.get("data_as_of")}）：\n'
            f'- 上涨 / 下跌：{rise} / {fall} 家。\n'
            f'- 涨停 / 跌停：{up} / {down} 家。\n'
            f'- 市场成交额：{_number(market.get("marketTurnover"), nonnegative=True)} 万亿元。\n'
            + breadth +
            '记录创建时间不等于最后更新时间，数据可能不完整。')
