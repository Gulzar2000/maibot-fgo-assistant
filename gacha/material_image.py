"""把素材来源汇总绘制为一张适合 QQ 阅读的长图。"""
from io import BytesIO
from typing import List
from PIL import Image, ImageDraw, ImageFont
import base64

from .image_gen import _find_font

WIDTH = 1120
INK = "#192E48"
MUTED = "#62738B"
TEAL = "#087F8C"


def render_material_report(report: dict, res_dir: str) -> str:
    font_path = _find_font(res_dir)
    fonts = {size: ImageFont.truetype(font_path, size) for size in (22, 25, 27, 30, 34, 52)}
    # 先排版再生成实际画布，避免内容多时裁掉活动或交换券。
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    commands = []
    y = 0

    def text(value: str, x: int, top: int, size: int = 27, color: str = INK) -> None:
        commands.append(("text", (x, top), value, size, color))

    def wrap(value: str, max_width: int, size: int = 27) -> List[str]:
        lines = []
        line = ""
        for char in value:
            if char == "\n" or (line and measure.textlength(line + char, font=fonts[size]) > max_width):
                lines.append(line)
                line = "" if char == "\n" else char
            else:
                line += char
        if line:
            lines.append(line)
        return lines

    def paragraph(value: str, x: int, top: int, max_width: int, size: int = 25, color: str = MUTED) -> int:
        for line in wrap(value, max_width, size):
            text(line, x, top, size, color)
            top += size + 12
        return top

    def section(number: str, title: str, subtitle: str) -> None:
        nonlocal y
        y += 34
        text(number, 48, y + 3, 25, TEAL)
        text(title, 105, y, 34)
        y += 51
        y = paragraph(subtitle, 48, y, 1024, 22)
        y += 15

    def card(height: int) -> int:
        nonlocal y
        top = y
        commands.append(("card", (36, y, WIDTH - 36, y + height)))
        y += height + 14
        return top

    commands.append(("header",))
    text("FGO  /  MATERIAL GUIDE", 48, 30, 22, "#A7DDE1")
    text(report["name"], 186, 73, 52, "#FFFFFF")
    text(f"国服 · {report['date']}    素材 #{report['item_id']}", 188, 148, 25, "#D6E5F2")
    y = 204
    section("01", "Free 本 · AP 效率前 5", "按每个素材所需 AP 从低到高排序 · 使用日服统计，筛选国服已开放关卡")
    if not report["free"]:
        top = card(85)
        text("暂无已收录且国服已开放的常驻掉落统计", 60, top + 26, 27, MUTED)
    for index, quest in enumerate(report["free"], 1):
        title = " · ".join(dict.fromkeys(x for x in (quest["spot"], quest["name"]) if x))
        titles = wrap(title, 730, 30)
        location = wrap(quest["war"].replace("\n", " · "), 950, 22)
        costs = "额外消耗：" + " / ".join(quest["costs"]) if quest.get("costs") else ""
        height = 105 + len(titles) * 42 + len(location) * 30 + len(wrap(costs, 970, 22)) * 34
        top = card(height)
        text(f"{index:02}", 59, top + 22, 30, TEAL)
        line_y = top + 23
        for line in titles:
            text(line, 116, line_y, 30)
            line_y += 42
        value = f"{quest['ap_per_item']:.1f}"
        text(value, 1048 - int(measure.textlength(value, font=fonts[34])), top + 22, 34, TEAL)
        text("AP / 个", 943, top + 66, 22, MUTED)
        line_y = paragraph(quest["war"].replace("\n", " · "), 65, line_y + 7, 950, 22)
        text(f"消耗 {quest['ap']} AP    平均 {quest['rate']:.3f} 个 / 周回    样本 {quest['runs']:,} 次",
             65, line_y + 7, 25, MUTED)
        paragraph(costs, 65, line_y + 47, 970, 22, "#A36615")
    section("02", "当前 / 未来活动 · 最近 5 个", "仅列含本素材收益的活动 · 固定收益为总量，周回随机掉落不计入")
    if not report["events"]:
        top = card(85)
        text("已收录的当前及未来活动中，暂无本素材奖励", 60, top + 26, 27, MUTED)
    for event in report["events"]:
        titles = wrap(event["title"], 980, 30)
        parts = "  /  ".join(f"{label} {count}" for label, count in event["parts"].items())
        extras = "；".join(dict.fromkeys(event["extra"]))
        height = 125 + len(titles) * 42 + len(wrap(parts, 970, 25)) * 37 + len(wrap(extras, 970, 25)) * 37
        top = card(height)
        line_y = top + 19
        for line in titles:
            text(line, 62, line_y, 30)
            line_y += 42
        estimated = " · 预估" if event["estimated"] else ""
        dates = f"{event['start_date']} ～ {event['end_date'] or '未定'}"
        text(f"{event['status']}{estimated}    {dates}", 64, line_y + 3, 22, MUTED)
        line_y += 43
        text(f"固定收益  {event['total']} 个", 64, line_y, 30, TEAL)
        line_y += 47
        line_y = paragraph(parts, 64, line_y, 970)
        paragraph(extras, 64, line_y, 970, 25, "#A36615")
    section("03", "未来每日交换券 · 最近 3 期", "仅列可兑换本素材的未来月份 · 国服月份按 Chaldea 对应规则换算")
    if not report["tickets"]:
        top = card(85)
        text("已公布的未来交换券中，暂无本素材", 60, top + 26, 27, MUTED)
    for ticket in report["tickets"]:
        options = "可选：" + " / ".join(ticket["items"])
        height = 111 + len(wrap(options, 970, 25)) * 37
        top = card(height)
        text(ticket["month"], 62, top + 20, 34, TEAL)
        text(f"每日最多 {ticket['daily']} 个 · 整月最多 {ticket['maximum']} 个", 295, top + 27, 27)
        paragraph(options, 64, top + 78, 970)
    y += 18
    for value in ("交换券为多种素材择一；整月上限假设全部选择本素材。",
                  "AP 按常规消耗统计，未计减半；修炼场按对应曜日开放。",
                  "活动预估排期与收益以国服实装为准；固定数量参照 Chaldea 日服资料。",
                  f"数据更新 {report['updated']}（UTC+8） · 查询 {report['date']}",
                  "来源：Chaldea / 效率剧场 / Atlas Academy / Mooncell"):
        y = paragraph(value, 48, y, 1024, 22)
    image = Image.new("RGB", (WIDTH, y + 36), "#EEF3F8")
    draw = ImageDraw.Draw(image)
    for command in commands:
        if command[0] == "header":
            draw.rectangle((0, 0, WIDTH, 203), fill="#162D48")
            draw.rectangle((0, 199, WIDTH, 204), fill="#25B4BD")
            draw.rounded_rectangle((46, 70, 166, 190), radius=16, fill="#F5F8FC")
            with Image.open(BytesIO(report["icon_bytes"])) as source:
                icon = source.convert("RGBA")
                icon.thumbnail((108, 108), Image.Resampling.LANCZOS)
                image.paste(icon, (106 - icon.width // 2, 130 - icon.height // 2), icon)
        elif command[0] == "card":
            draw.rounded_rectangle(command[1], radius=16, fill="white")
        else:
            _, point, value, size, color = command
            draw.text(point, value, font=fonts[size], fill=color)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")
