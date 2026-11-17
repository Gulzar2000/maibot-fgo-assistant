"""四档目标共用素材表，动态计算画布高度，不裁切缺口条目。"""
from datetime import datetime
from io import BytesIO
from math import ceil
from typing import Dict
from PIL import Image, ImageDraw, ImageFont
import base64

from .image_gen import _find_font
from .events import CHINA_TZ

WIDTH = 2200
INK = "#20354D"
MUTED = "#697A90"
TEAL = "#087F8C"
RED = "#BC4B41"
GREEN = "#21864D"
GOLD = "#D4A32D"


def render_plan_report(report: dict, res_dir: str, icons: Dict[int, bytes]) -> str:
    rows = [row for row in report["items"] if row["id"] != 1 and any(row["missing"])]
    # 两栏按先左后右阅读；表头和所有行均按实际数量预留高度。
    per_column = ceil(len(rows) / 2)
    table_top = 705
    footer_top = table_top + 63 + max(1, per_column) * 53 + 26
    image = Image.new("RGB", (WIDTH, footer_top + 300), "#EEF3F8")
    draw = ImageDraw.Draw(image)
    font_path = _find_font(res_dir)
    fonts = {size: ImageFont.truetype(font_path, size) for size in (21, 23, 25, 27, 30, 34, 48)}

    def text(value: str, x: int, y: int, size: int = 25, color: str = INK) -> None:
        draw.text((x, y), value, font=fonts[size], fill=color)

    def right(value: str, edge: int, y: int, size: int = 25, color: str = INK) -> None:
        text(value, edge - int(draw.textlength(value, font=fonts[size])), y, size, color)

    def fitted(value: str, x: int, y: int, width: int, size: int = 25) -> None:
        for candidate in (size, 23, 21):
            if draw.textlength(value, font=fonts[candidate]) <= width:
                text(value, x, y, candidate)
                return
        # 极长的活动专用素材名用两行完整展示，不能静默截断名称。
        split = 1
        while split < len(value) and draw.textlength(value[:split + 1], font=fonts[21]) <= width:
            split += 1
        text(value[:split], x, y - 9, 21)
        text(value[split:], x, y + 15, 21)

    draw.rectangle((0, 0, WIDTH, 174), fill="#162D48")
    text("FGO  /  DEVELOPMENT PLAN", 44, 23, 23, "#9EDADD")
    text("迦勒底养成规划", 44, 63, 48, "white")
    right(f"{report['name']}  ·  {report['account']}", WIDTH - 44, 79, 27, "white")
    text(f"账号快照  {report['captured_at'][:19].replace('T', ' ')}  UTC+8", 45, 135, 23, "#CDDEEA")
    comparison = report["comparison"]
    progress_step = (WIDTH - 60) // 3
    for i, (key, label, current, total) in enumerate((
        ("lit", "已点亮星图节点", report["lit"], report["nodes"]),
        ("maxed", "已满破英灵", report["maxed"], report["total"]),
        ("skill_steps", "技能进度 · 目标999", report["skill_steps"], report["skill_total"]),
    )):
        x = 40 + i * progress_step
        card_width = progress_step - 20
        draw.rounded_rectangle((x, 198, x + card_width, 334), radius=18, fill="white")
        text(label, x + 22, 213, 27)
        percent = current / total if total else 0
        gained = comparison["progress"][key] if comparison is not None else 0
        suffix = f"  (+{gained:,})" if gained else ""
        right(f"{current:,} / {total:,}  ·  {percent:.1%}{suffix}", x + card_width - 24, 254, 25, TEAL)
        bar_width = card_width - 46
        draw.rounded_rectangle((x + 22, 301, x + card_width - 24, 313), radius=6, fill="#E3ECF0")
        if current:
            end = x + 22 + max(1, round(bar_width * percent))
            draw.rectangle((x + 22, 301, end, 313), fill=TEAL)
            # 用当前总量作分母画“本次数量增量”，新增从者导致比例下降也不会抹掉金色段。
            if gained:
                start = x + 22 + round(bar_width * max(0, current - gained) / total)
                draw.rectangle((min(start, end - 1), 301, end, 313), fill=GOLD)
    scope = "含冠位" if report["include_grand"] else "仅普通"
    text(f"国服星图 {report['board_count']} 张（{scope}） · 已解锁锁节点 {report['unlocked']}/{report['locks']}"
         f"    五星999：{report['ssr_trained']}/{report['ssr_total']}", 46, 353, 25, MUTED)
    qp = next((row for row in report["items"] if row["id"] == 1), None)
    target_count = len(report["targets"])
    card_step = (WIDTH - 60) // target_count
    ascended = report["maxed"] == report["total"]
    board_complete = report["lit"] == report["nodes"] and report["unlocked"] == report["locks"]
    # 按实际养成进度判断达成；库存充足但尚未养成，仍展示剩余需求。
    completed = (ascended, ascended and board_complete,
                 ascended and board_complete and report["ssr_trained"] == report["ssr_total"],
                 ascended and board_complete and report["trained"] == report["total"])
    for i, title in enumerate(report["targets"]):
        x = 40 + i * card_step
        draw.rounded_rectangle((x, 404, x + card_step - 20, 609), radius=18, fill="white")
        text(f"{chr(65 + i)}  {title}", x + 20, 421, 23, TEAL)
        if completed[i]:
            text("已达成！＼(≧", x + 20, 502, 30, TEAL)
            # 中文字体缺少 ∇ 字形，直接绘制同形数学符号，避免颜文字出现方框。
            symbol_x = x + 20 + int(draw.textlength("已达成！＼(≧", font=fonts[30]))
            draw.polygon(((symbol_x + 2, 512), (symbol_x + 22, 512), (symbol_x + 12, 534)),
                         outline=TEAL, width=2)
            text("≦)／", symbol_x + 25, 502, 30, TEAL)
            continue
        text(f"缺口 {report['missing_types'][i]} 种资源", x + 20, 467, 30)
        text(f"剩余所需 QP：{qp['required'][i]:,}" if qp else "剩余所需 QP：0", x + 20, 519, 25, MUTED)
        text(f"QP 缺口：{qp['missing'][i]:,}" if qp else "QP 缺口：0", x + 20, 560, 25,
             RED if qp and qp["missing"][i] else TEAL)
    text("素材缺口对照", 45, 632, 34)
    right(f"QP 库存：{qp['stock']:,}" if qp else "养成 QP 已充足", WIDTH - 45, 642, 25, MUTED)
    text("每档独立扣除一次库存 · 0 表示充足 · 绿色为期间获得量（含已消耗部分）· 金色为期间养成进度", 46, 677, 23, MUTED)
    stock_edge = 430
    gained_edge = 540
    column_width = (WIDTH - 120) // 2
    missing_edges = [round(gained_edge + (column_width - 18 - gained_edge) * (i + 1) / target_count) for i in range(target_count)]
    for column in range(2):
        x = 40 + column * (column_width + 40)
        draw.rounded_rectangle((x, table_top + 41, x + column_width, footer_top - 15), radius=14, fill="white")
        text("素材", x + 18, table_top + 14, 25, MUTED)
        headings = [("库存", stock_edge), ("增量", gained_edge)] + [(f"{chr(65 + i)} 缺少", edge) for i, edge in enumerate(missing_edges)]
        for label, edge in headings:
            right(label, x + edge, table_top + 14, 23, MUTED)
        selected = rows[column * per_column:(column + 1) * per_column]
        for index, row in enumerate(selected):
            y = table_top + 63 + index * 53
            if index % 2:
                draw.rectangle((x + 3, y - 3, x + column_width - 3, y + 49), fill="#F5F8FA")
            with Image.open(BytesIO(icons[row["id"]])) as source:
                icon = source.convert("RGBA")
                icon.thumbnail((42, 42), Image.Resampling.LANCZOS)
                image.paste(icon, (x + 12, y + 1), icon)
            fitted(row["name"], x + 63, y + 5, 250)
            right(f"{row['stock']:,}", x + stock_edge, y + 5, 25, MUTED)
            gained = row["gained"]
            right(f"+{gained:,}" if gained else "—", x + gained_edge, y + 5, 25, GREEN if gained else "#A1ADB7")
            for target, edge in enumerate(missing_edges):
                amount = row["missing"][target]
                right(f"{amount:,}", x + edge, y + 5, 25, RED if amount else "#A1ADB7")
    if not rows:
        text("全部目标的素材库存均已充足。", 65, table_top + 77, 27, TEAL)
    notes = [
        ("首次规划快照：本次建立对比基线。" if comparison is None else
         "对比快照  " + comparison["since"][:19].replace("T", " ") + "  UTC+8；负向变化不标金色。"),
        "技能进度＝每名从者三个主动技能从1级到9级的累计升级数；10级按9级封顶，总量24×持有数。",
        "满破＝最终灵基突破；999＝三个主动技能均≥9级。不含追加技能、灵衣、圣杯与升级狗粮。",
        "只统计背包及第二保管室的持有从者，同图鉴每种一名；重复卡按各目标剩余QP最少的一张计算。",
        "星图点亮进度不含空白／锁节点，锁另列；费用包含未解锁锁节点。剧情与任务条件仍需自行完成。",
        "素材增量按库存差＋突破／主技能／追加／灵衣／星图消耗推算；其他用途和已消失卡片的期间消耗无法还原。",
        "费用与中文素材：Chaldea 数据包；国服星图：Atlas Academy。数据更新 "
        + datetime.fromtimestamp(report["updated"], CHINA_TZ).strftime("%Y-%m-%d"),
    ]
    for index, note in enumerate(notes):
        text(note, 46, footer_top + index * 37, 23, MUTED)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")
