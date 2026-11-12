"""将本轮标准化涨停梯队绘制为 SVG，避免模型编造图片内容。"""
import base64
import html
import textwrap
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from .panorama import _number


def _first_limit_time(stock: dict) -> time:
    """只按首次封板时间排序，缺失或脏数据置后，不借用最终时间。"""
    first = str(stock.get('daily_time') or '').partition(',')[0].strip()
    try:
        return datetime.strptime(first, '%H:%M:%S').time()
    except ValueError:
        return time.max


def ladder_markdown(snapshot: dict, phase: dict | None = None) -> str:
    """校验工具快照后按板高降序绘制，空数据不生成图片。"""
    if snapshot.get('error') or snapshot.get('found') is False:
        return ''
    quality = snapshot.get('data_quality')
    if isinstance(quality, dict) and (quality.get('ladder_missing') or quality.get('short_term_missing')):
        return ''
    day = snapshot.get('data_as_of')
    try:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            return ''
    except ValueError:
        return ''
    levels = snapshot.get('levels')
    if not isinstance(levels, list):
        return ''
    groups = [group for group in levels if isinstance(group, dict)
              and type(group.get('level')) is int and group['level'] > 0
              and isinstance(group.get('stocks'), list) and group['stocks']]
    esc = lambda value: html.escape(str(value), quote=True)
    # 指标必须来自同日成功快照，不能把跨日或采集缺失的零值展示为市场事实。
    phase = phase if isinstance(phase, dict) else {}
    phase_quality = phase.get('data_quality')
    if (phase.get('data_as_of') != day or phase.get('error') or phase.get('found') is False
            or (isinstance(phase_quality, dict) and phase_quality.get('short_term_missing'))):
        phase = {}
    promotion = phase.get('prev_cohort_promotion_rate')
    valid_promotion = _number(promotion, nonnegative=True, maximum=1) != '未提供'
    promotion_text = _number(Decimal(str(promotion)) * 100) + '%' if valid_promotion else '未提供'
    sealing = _number(phase.get('sealing_rate'), nonnegative=True, maximum=100)
    metrics = [
        ('短线周期', str(phase.get('cycle') or '未提供')),
        ('晋级率', promotion_text),
        ('封板率', sealing + '%' if sealing != '未提供' else sealing),
        ('涨停数', _number(phase.get('limit_up_qty'), places=0, nonnegative=True)),
        ('跌停数', _number(phase.get('down_limit_qty'), places=0, nonnegative=True)),
    ]
    metric_blocks = []
    for index, (label, value) in enumerate(metrics):
        x = 36 + index * 232
        fit = ' textLength="204" lengthAdjust="spacingAndGlyphs"' if len(value) > 9 else ''
        metric_blocks.append(f'<text x="{x}" y="86" fill="#ffe6a1" font-size="14">{label}</text>'
                             f'<text x="{x}" y="112" fill="#ffffff" font-size="20" font-weight="700"{fit}>{esc(value)}</text>')
    blocks, y = [], 180
    # 短文案同排展示，长字段独立换行；整行按最高列增高，避免相邻行覆盖。
    for group in sorted(groups, key=lambda item: item['level'], reverse=True):
        stocks = [stock for stock in group['stocks'] if isinstance(stock, dict) and stock.get('code')]
        if not stocks:
            continue
        # 稳定排序保留同一首次时间的源顺序，不修改工具原始名单。
        stocks.sort(key=_first_limit_time)
        blocks.append(f'<rect x="20" y="{y - 23}" width="1160" height="32" fill="#ffe6a1"/>'
                      f'<text x="36" y="{y}" fill="#a12c22" font-size="17" font-weight="700">{group["level"]}板梯队 · {len(stocks)}家</text>')
        y += 32
        for index, stock in enumerate(stocks):
            columns = [
                ('code', 36, 10, stock['code'], '#c73328'),
                ('name', 166, 7, stock.get('name') or '名称未提供', '#c73328'),
                # 源数据 closing_funds 已按亿元存储，复用数值校验且不重复换算。
                ('funds', 326, 9, _number(stock.get('closing_funds'), nonnegative=True), '#333333'),
                ('time', 436, 24, str(stock.get('daily_time') or '未提供').replace(',', ' / '), '#333333'),
                ('reason', 696, 26, stock.get('reason') or '涨停原因未提供', '#333333'),
            ]
            cells = []
            for key, x, width, value, color in columns:
                # 时间保持单行便于对比首次与最终封板，其余字段按列宽换行。
                lines = [' '.join(str(value).split())] if key == 'time' else [
                    line for part in str(value).splitlines()
                    for line in (textwrap.wrap(part, width=width) or [''])]
                cells.append((key, x, color, lines))
            row_height = max(28, max(len(cell[3]) for cell in cells) * 20 + 8)
            fill = '#ffffff' if index % 2 == 0 else '#fffaf4'
            blocks.append(f'<rect x="20" y="{y - 19}" width="1160" height="{row_height}" fill="{fill}"/>')
            for key, x, color, lines in cells:
                for offset, line in enumerate(lines):
                    # 异常长时间字段压缩到列内，防止覆盖右侧原因且不丢失原文。
                    fit = ' textLength="238" lengthAdjust="spacingAndGlyphs"' if key == 'time' and len(line) > 24 else ''
                    blocks.append(f'<text data-column="{key}" x="{x}" y="{y + offset * 20}" fill="{color}" font-size="15"'
                                  f' font-weight="{600 if key in ("code", "name") else 400}"{fit}>{esc(line)}</text>')
            y += row_height
        y += 10
    if not blocks:
        return ''
    if snapshot.get('truncated'):
        blocks.append(f'<text x="32" y="{y}" fill="#a12c22" font-size="15">名单已截断：展示 {esc(snapshot.get("stocks_returned", "未知"))} / {esc(snapshot.get("stocks_total", "未知"))} 家</text>')
        y += 32
    height = y + 78
    # 单独标记北京时间的图片生成时刻，避免被误认为行情更新时间。
    generated_at = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}" role="img" aria-label="涨停梯队图">'
           '<style>text{font-family:Microsoft YaHei,PingFang SC,sans-serif}</style>'
           f'<rect width="1200" height="{height}" fill="#fff3e7"/>'
           '<rect width="1200" height="128" fill="#c73328"/>'
           '<text x="32" y="36" fill="#ffffff" font-size="29" font-weight="700">短线涨停梯队</text>'
           f'<text x="1168" y="34" text-anchor="end" fill="#ffffff" font-size="20">{esc(day)}</text>'
           f'<text x="1168" y="56" text-anchor="end" fill="#ffe6a1" font-size="12">生成于（北京时间）：{generated_at}</text>'
           '<text x="32" y="58" fill="#ffe6a1" font-size="14">A 股涨停复盘 · 板高降序 / 梯队内首次涨停时间升序 · 实际数据日期如上</text>'
           + ''.join(metric_blocks) +
           '<text x="36" y="148" fill="#7d5545" font-size="14">代码</text>'
           '<text x="166" y="148" fill="#7d5545" font-size="14">名称</text>'
           '<text x="326" y="148" fill="#7d5545" font-size="14">封单额（亿）</text>'
           '<text x="436" y="148" fill="#7d5545" font-size="14">涨停时间</text>'
           '<text x="696" y="148" fill="#7d5545" font-size="14">涨停原因</text>'
           + ''.join(blocks) +
           f'<text x="32" y="{y + 22}" fill="#7d5545" font-size="13">涨停时间：首次 / 最终（源数据有提供时展示）；缺失字段标记为未提供。</text>'
           f'<text x="32" y="{y + 48}" fill="#7d5545" font-size="13">晋级率：昨日涨停股今日晋级连板比例；相邻采集日可能缺采。数据更新时间未核实，不构成投资建议。</text></svg>')
    return '![涨停梯队图](data:image/svg+xml;base64,' + base64.b64encode(svg.encode('utf-8')).decode('ascii') + ')'
