"""累计抽数与五星出率快照图；只绘制实际保存的观测点。"""
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageOps
import base64
import math

from .image_gen import _find_font
from .rate_mascot import load_mascot
from .rate_quips import select_quip_presentation
from .statistics import validate_draw_statistics


class CaptureReport(str):
    """保留文字报告接口，同时携带本次历史曲线。"""

    image_base64: Optional[str]

    def __new__(cls, text: str, image_base64: Optional[str]):
        result = super().__new__(cls, text)
        result.image_base64 = image_base64
        return result


def history_points(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """旧摘要作为第一个观测点保留，不凭空补出更早历史。"""
    if "snapshots" in entry:
        return [dict(point) for point in entry["snapshots"]]
    return [dict(entry["summary"])] if "summary" in entry else []


def draw_bounds(draws: List[int]) -> tuple[float, float]:
    low, high = min(draws), max(draws)
    padding = max(3, (high - low) * 0.08)
    return max(0, low - padding), high + padding


def smooth_curve(coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """单调三次 Hermite 插值，穿过观测点且不越过相邻点的出率范围。"""
    def interpolate(run: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if len(run) < 3:
            return run[:]
        widths = [b[0] - a[0] for a, b in zip(run, run[1:])]
        slopes = [(b[1] - a[1]) / width for a, b, width in zip(run, run[1:], widths)]
        tangents = [slopes[0]]
        for i in range(1, len(run) - 1):
            before, after = slopes[i-1], slopes[i]
            if before * after <= 0:
                tangents.append(0.0)
            else:
                # 按横轴间隔加权的调和平均，极值处切线置零以避免过冲。
                w1, w2 = 2 * widths[i] + widths[i-1], widths[i] + 2 * widths[i-1]
                tangents.append((w1 + w2) / (w1 / before + w2 / after))
        tangents.append(slopes[-1])
        result = [run[0]]
        for i, width in enumerate(widths):
            x0, y0 = run[i]
            _, y1 = run[i+1]
            steps = max(4, math.ceil(width / 2))
            for j in range(1, steps):
                t = j / steps
                y = ((2*t**3 - 3*t**2 + 1)*y0 + (t**3 - 2*t**2 + t)*width*tangents[i]
                     + (-2*t**3 + 3*t**2)*y1 + (t**3 - t**2)*width*tangents[i+1])
                result.append((x0 + width*t, y))
            result.append(run[i+1])
        return result

    if not coords:
        return []
    result: List[Tuple[float, float]] = []
    run = [coords[0]]
    for point in coords[1:]:
        if point == run[-1]:
            continue  # 同位置观测只合并绘图路径，原始历史和标记均保留。
        if point[0] <= run[-1][0]:
            result.extend(interpolate(run))
            # 同抽数出率变化、抽数回退仍按原顺序直连，不能伪造横轴间隔。
            run = [point]
        else:
            run.append(point)
    result.extend(interpolate(run))
    return result


def render_rate_chart(points: List[Dict[str, Any]]) -> Optional[str]:
    if not points:
        return None
    for point in points:
        validate_draw_statistics(point["total_draws"], point["ssr_count"])
        if point["total_draws"] == 0:
            raise ValueError("出率曲线不接受零抽数")
    draws = [point["total_draws"] for point in points]
    rates = [point["ssr_count"] / point["total_draws"] * 100 for point in points]
    xmin, xmax = draw_bounds(draws)
    spread = max(rates) - min(rates)
    ymin = max(0, min(rates) - max(0.02, spread * 0.15))
    ymax = max(rates) + max(0.02, spread * 0.15)
    resources = Path(__file__).resolve().parent.parent / "res"
    image = Image.new("RGBA", (800, 520), "#fff5e9")
    # 只取召唤背景中央的魔法阵，避开原截图的余额与按钮。
    with Image.open(resources / "summon_background.png") as source:
        backdrop = source.crop((0, 180, source.width, 660)).convert("RGBA")
    backdrop = ImageOps.fit(backdrop, (800, 175), method=Image.Resampling.LANCZOS)
    backdrop = Image.alpha_composite(backdrop, Image.new("RGBA", backdrop.size, (255, 244, 228, 224)))
    image.alpha_composite(backdrop)
    canvas = ImageDraw.Draw(image)
    font_path = _find_font(str(resources))
    title = ImageFont.truetype(font_path, 27)
    number = ImageFont.truetype(font_path, 29)
    font = ImageFont.truetype(font_path, 17)
    small = ImageFont.truetype(font_path, 14)
    ink, muted, accent = "#59443d", "#927a6b", "#ce755f"

    with Image.open(resources / "rate_chart" / "quartz.png") as source:
        quartz = source.convert("RGBA")
    quartz.thumbnail((45, 50), Image.Resampling.LANCZOS)
    image.alpha_composite(quartz, (28, 23))
    canvas.text((84, 23), "迦勒底 · 召唤手记", font=title, fill=ink)
    canvas.rounded_rectangle((636, 31, 771, 62), radius=15, fill="#fff9ed", outline="#e3cbb4")
    canvas.text((703, 46), f"已记录 {len(points)} 次", font=small, fill=ink, anchor="mm")

    for x, label, value, unit in (
        (28, "累计五星出率", f"{rates[-1]:.3f}", "%"),
        (282, "累计有效抽数", str(draws[-1]), "抽"),
        (536, "累计五星从者", str(points[-1]["ssr_count"]), "骑"),
    ):
        canvas.rounded_rectangle((x, 97, x+236, 169), radius=15, fill="#fffdf7", outline="#ebd9c4")
        canvas.text((x+16, 105), label, font=small, fill=muted)
        canvas.text((x+16, 125), value, font=number, fill=accent)
        width = canvas.textlength(value, font=number)
        canvas.text((x+23+width, 142), unit, font=small, fill=muted)

    canvas.rounded_rectangle((28, 185, 612, 455), radius=18, fill="#fffdf8", outline="#ead7c0")
    canvas.text((46, 197), "五星出率 (%)", font=small, fill=muted)
    left, top, right, bottom = 91, 231, 586, 395
    for i in range(5):
        y = bottom - (bottom - top) * i / 4
        canvas.line((left, y, right, y), fill="#eee2d4")
        canvas.text((left-10, y), f"{ymin+(ymax-ymin)*i/4:.3f}", font=small, fill=muted, anchor="rm")
    # 抽数为离散整数；即使只有一个观测点也不生成小数刻度。
    step = max(1, math.ceil((xmax-xmin)/4))
    for tick in range(math.ceil(xmin/step)*step, math.floor(xmax)+1, step):
        x = left+(tick-xmin)/(xmax-xmin)*(right-left)
        canvas.text((x, bottom+12), str(tick), font=small, fill=muted, anchor="mt")
    canvas.line((left, top, left, bottom, right, bottom), fill="#ceb99f", width=1)
    canvas.text(((left+right)/2, 431), "累计有效抽卡数（抽）", font=small, fill=muted, anchor="mt")
    coords = [(left+(x-xmin)/(xmax-xmin)*(right-left), bottom-(y-ymin)/(ymax-ymin)*(bottom-top))
              for x, y in zip(draws, rates)]
    # 曲线和标记在局部透明层以四倍分辨率绘制，再缩回成品尺寸抗锯齿。
    # 插值也使用放大后的坐标，提高近水平曲线的采样密度。
    scale = 4
    origin = (left-8, top-8)
    layer_size = (right-left+16, bottom-top+16)
    layer = Image.new("RGBA", (layer_size[0]*scale, layer_size[1]*scale))
    line_draw = ImageDraw.Draw(layer)
    scaled = [((x-origin[0])*scale, (y-origin[1])*scale) for x, y in coords]
    if len(coords) > 1:
        line_draw.line(smooth_curve(scaled), fill=accent, width=2*scale, joint="curve")
    for x, y in scaled:
        radius = 2*scale
        line_draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=accent)
    x, y = scaled[-1]
    radius = 4*scale
    line_draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline="#dbab7d", width=scale)
    image.alpha_composite(layer.resize(layer_size, Image.Resampling.LANCZOS), origin)

    canvas.rounded_rectangle((632, 195, 773, 253), radius=16, fill="#fffcf4", outline="#e7c5aa")
    canvas.polygon(((679, 252), (689, 264), (700, 252)), fill="#fffcf4")
    (message, reply), variant = select_quip_presentation(points)
    canvas.text((703, 211), message, font=font, fill=ink, anchor="mt")
    canvas.text((703, 235), reply, font=small, fill=muted, anchor="mt")
    mascot = load_mascot(resources / "rate_chart", points, variant)
    mascot.thumbnail((143, 181), Image.Resampling.LANCZOS)
    image.alpha_composite(mascot, (633+(143-mascot.width)//2, 447-mascot.height))
    canvas.text((30, 470), f"{points[0]['day']} — {points[-1]['day']}  ·  抓包时的累计出率", font=small, fill=muted)
    note = "首个观测点，继续抓包即可连成曲线" if len(points) == 1 else "按抓包顺序连线 · 相同抽数的记录也保留"
    canvas.text((30, 494), note, font=small, fill=muted)
    buffer = BytesIO()
    # 固定小尺寸并压缩调色板，减少 QQ 图片体积；文字仍保留原尺寸清晰度。
    image.convert("RGB").quantize(colors=256).save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")
