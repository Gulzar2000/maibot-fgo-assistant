"""绘制迦勒底风格的每日圣晶石折算曲线。"""
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Sequence
from PIL import Image, ImageDraw, ImageFont
import base64
import math

from .image_gen import _find_font
from .quartz import QuartzSnapshot

QUARTZ_PER_PITY = 900


def render_quartz_chart(points: Sequence[QuartzSnapshot], resources: Path) -> str:
    width, height = 1000, 640
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line((0, y, width, y), fill=(17 - y * 8 // height, 23 - y * 9 // height, 49 - y * 18 // height))
    font_path = _find_font(str(resources))
    title, number, label, tick = (ImageFont.truetype(font_path, size) for size in (37, 48, 21, 17))
    white, muted, accent = "#F4F0FF", "#B3BCDC", "#C5ADFF"
    pity_color = "#EAC17A"

    def draw_pity_line(left: int, right: int, y: float) -> None:
        for x in range(left, right, 16):
            draw.line((x, y, min(x + 8, right), y), fill=pity_color, width=2)

    # 用星盘线条衬托插件自带的圣晶石图标。
    cx, cy = 100, 89
    for radius in (44, 55):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline="#53577D")
    for angle in range(0, 360, 45):
        dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        draw.line((cx + 50 * dx, cy + 50 * dy, cx + 60 * dx, cy + 60 * dy), fill="#A89ADA", width=2)
    with Image.open(resources / "rate_chart" / "quartz.png") as source:
        icon = source.convert("RGBA")
    icon.thumbnail((76, 76), Image.Resampling.LANCZOS)
    image.paste(icon, (cx - icon.width // 2, cy - icon.height // 2), icon)
    draw.text((179, 48), "攒石记录", fill=white, font=title)
    draw.text((181, 112), f"{points[0].day:%Y-%m-%d}  至  {points[-1].day:%Y-%m-%d}", fill=muted, font=label)
    draw.text((942, 41), "最新折算总数", fill=muted, font=label, anchor="rt")
    draw.text((942, 76), f"{points[-1].total:,.1f}", fill=accent, font=number, anchor="rt")

    draw.rounded_rectangle((40, 169, 960, 595), radius=25, fill="#151E39", outline="#3B456A", width=2)
    draw.text((67, 193), "折合圣晶石（石）", fill=white, font=label)
    draw.text((934, 196), f"{len(points)} 天有记录", fill=muted, font=tick, anchor="rt")
    left, top, right, bottom = 132, 245, 892, 513
    values = [point.total for point in points]
    # 纵轴从零起，按最大库存选刻度，低库存时也能看清变化。
    span = max(1.0, max(values))
    magnitude = 10 ** math.floor(math.log10(span / 4))
    step = next(factor * magnitude for factor in (1, 2, 5, 10) if factor * magnitude >= span / 4)
    tick_count = math.ceil((span + step / 4) / step)
    upper = tick_count * step
    start = points[0].day
    offsets = [(point.day - start).days for point in points]
    duration = offsets[-1]

    def y_at(value: float) -> float:
        return bottom - (bottom - top) * value / upper

    # 日期按自然日等距定位，间隔没有取数的日期保留原有间距。
    coords = [(left + (right - left) * (offset / duration if duration else 0.5), y_at(value))
              for offset, value in zip(offsets, values)]
    if len(coords) > 1:
        draw.polygon([*coords, (coords[-1][0], bottom), (coords[0][0], bottom)], fill="#292B53")

    for level in range(tick_count + 1):
        value = level * step
        y = y_at(value)
        draw.line((left, y, right, y), fill="#354363")
        text = f"{value:,.0f}" if step >= 1 or level == 0 else f"{value:,.1f}"
        draw.text((left - 12, y), text, fill=muted, font=tick, anchor="rm")

    if upper >= QUARTZ_PER_PITY:
        label_spacing = (bottom - top) * QUARTZ_PER_PITY / upper
        for value in range(QUARTZ_PER_PITY, int(upper) + 1, QUARTZ_PER_PITY):
            y = y_at(value)
            draw_pity_line(left, right, y)
            if label_spacing >= 21:
                draw.text((right + 10, y), f"{value:,}", fill=pity_color, font=tick, anchor="lm")

    if len(coords) > 1:
        draw.line(coords, fill="#59498F", width=9, joint="curve")
        draw.line(coords, fill=accent, width=4, joint="curve")
    for x, y in coords:
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#F7F1FF")
    x, y = coords[-1]
    draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill="#BAA2FF", outline="#F7F1FF", width=2)

    num_labels = min(duration, 5)
    for index in range(num_labels + 1):
        offset = round(duration * index / num_labels) if num_labels else 0
        x = left + (right - left) * (offset / duration if duration else 0.5)
        draw.line((x, bottom, x, bottom + 6), fill=muted)
        draw.text((x, 530), (start + timedelta(days=offset)).isoformat(), fill=muted, font=tick, anchor="mt")
    draw.text(((left + right) / 2, 560), "日期（北京时间）", fill=muted, font=tick, anchor="mt")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
