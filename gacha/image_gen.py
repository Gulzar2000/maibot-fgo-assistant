"""Pillow 图片合成：使用 Mooncell 成品卡片还原游戏十一连结果界面。"""
from pathlib import Path
from tempfile import mkstemp
from typing import Dict, List, Optional, Tuple
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
import os

from .card_assets import result_card_path

CANVAS_SIZE = (1920, 864)
CARD_SIZE = (198, 216)
CARD_GAP = 26
ROW_Y = (184, 436)


def card_positions() -> List[Tuple[int, int]]:
    """六张在上、五张在下，每排独立居中，位置与参考截图一致。"""
    positions: List[Tuple[int, int]] = []
    for count, y in zip((6, 5), ROW_Y):
        start = (CANVAS_SIZE[0] - count * CARD_SIZE[0] - (count - 1) * CARD_GAP) // 2
        positions.extend((start + column * (CARD_SIZE[0] + CARD_GAP), y) for column in range(count))
    return positions


def generate_ten_pull_image(
    result: List[dict],
    res_dir: str,
    icon_dir: str,
    server: str = "国服",
    banner: str = "",
    name_map: Optional[Dict[str, Dict[str, str]]] = None,
    user_nickname: str = "",
) -> str:
    """生成完整十一连结果图片；素材缺失或损坏时抛出带卡片编号的错误。"""
    if len(result) != 11:
        raise ValueError(f"十一连结果必须包含 11 张卡片，实际为 {len(result)}")
    background_path = Path(res_dir) / "summon_background.png"
    with Image.open(background_path) as background:
        image = ImageOps.fit(background.convert("RGBA"), CANVAS_SIZE, method=Image.Resampling.LANCZOS)
    font_path = _find_font(res_dir)
    label_font = _load_font(font_path, 15)
    title_font = _load_font(font_path, 17)

    for card, (x, y) in zip(result, card_positions()):
        if card["type"] not in ("svt", "cft"):
            raise ValueError(f"不支持的卡片类型: {card['type']}")
        kind = "svt" if card["type"] == "svt" else "ce"
        number = str(card["id"])
        # 名字或 ID 用于明确报告缺失素材；礼装名称使用索引中的 ce 键。
        name = name_map.get(kind, {}).get(number, "") if name_map is not None else ""
        path = result_card_path(icon_dir, kind, number)
        try:
            with Image.open(path) as source:
                if source.width <= source.height * 0.75 or source.width >= source.height:
                    raise ValueError(f"图片不是图鉴带框卡片: {source.size}")
                framed = source.convert("RGBA")
                # 只清除顶部极小的白角；原图已有卡框切角，不能再次按大三角裁掉边框。
                width, height = framed.size
                cut = round(width * 4 / 132)
                mask = Image.new("L", framed.size)
                ImageDraw.Draw(mask).polygon(
                    [(0, cut), (cut, 0), (width - 1 - cut, 0), (width - 1, cut),
                     (width - 1, height - 1), (0, height - 1)], fill=255
                )
                framed.putalpha(ImageChops.multiply(framed.getchannel("A"), mask))
                thumbnail = ImageOps.contain(framed, CARD_SIZE, method=Image.Resampling.LANCZOS)
        except (OSError, ValueError) as error:
            raise ValueError(f"召唤卡片素材不可用: {kind} #{number} {name}: {error}") from error

        # 成品图已经带卡框、职阶和星级，只替换底部英文类别条，不重复叠加边框或星级。
        strip_top = round(thumbnail.height * 131 / 144)
        strip_bottom = round(thumbnail.height * 142 / 144)
        rarity = int(card["rarity"][-1])
        if rarity not in (3, 4, 5):
            raise ValueError(f"不支持的召唤星级: {card['rarity']}")
        label_color = (220, 181, 47, 255) if rarity >= 4 else (187, 189, 191, 255)
        card_draw = ImageDraw.Draw(thumbnail)
        card_draw.rectangle((5, strip_top, thumbnail.width - 6, strip_bottom), fill=label_color)
        _draw_centered(card_draw, "从者" if kind == "svt" else "礼装", (5, strip_top, thumbnail.width - 5, strip_bottom),
                       label_font, (32, 31, 28, 255))
        # 等比缩放后居中粘贴，抽取顺序保持不变。
        shadow = Image.new("RGBA", (thumbnail.width + 24, thumbnail.height + 24))
        shadow.paste((0, 0, 0, 170), (12, 12), thumbnail.getchannel("A"))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(4)), (x - 9, y - 7))
        image.alpha_composite(thumbnail, (x + (CARD_SIZE[0] - thumbnail.width) // 2,
                                          y + (CARD_SIZE[1] - thumbnail.height) // 2))

    # 标题置于按钮下方的独立区域；长度受限，避免自动换行溢出覆盖游戏界面。
    title = f"{server} · {banner}" if banner else f"FGO {server} 十一连召唤"
    if user_nickname:
        pool_title = banner if banner else f"FGO {server} 十一连召唤"
        title = f"{user_nickname}：{pool_title}"
    title_layer = Image.new("RGBA", CANVAS_SIZE)
    title_draw = ImageDraw.Draw(title_layer)
    _draw_wrapped_text(title_draw, title, (260, 837, 1660, 863), title_font)
    image.alpha_composite(title_layer)
    return _save_image(image)


def _draw_wrapped_text(draw: ImageDraw.ImageDraw, text: str, box: Tuple[int, int, int, int],
                       font: ImageFont.FreeTypeFont) -> None:
    """标题限制在指定区域，超长时使用省略号，保持单行居中。"""
    width = box[2] - box[0] - 18
    if draw.textlength(text, font=font) > width:
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        text += "…"
    draw.rounded_rectangle(box, radius=8, fill=(0, 12, 22, 175))
    _draw_centered(draw, text, box, font, (226, 245, 255, 255))


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, box: Tuple[int, int, int, int],
                   font: ImageFont.FreeTypeFont, color: Tuple[int, int, int, int]) -> None:
    """水平与垂直居中绘制文本，同时考虑字体基线偏移。"""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = (box[0] + box[2] - right + left) / 2 - left
    y = (box[1] + box[3] - bottom + top) / 2 - top
    draw.text((x, y), text, font=font, fill=color)


def _find_font(res_dir: str) -> str:
    candidates = [Path(res_dir) / name for name in ("SourceHanSansSC-Regular.otf", "SourceHanSansSC-Regular.ttf")]
    candidates.extend(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
                      for name in ("msyh.ttc", "simhei.ttf"))
    for path in candidates:
        if path.is_file():
            return str(path)
    raise FileNotFoundError("缺少中文字体：请在插件 res 目录安装 SourceHanSansSC-Regular.otf")


def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, size)


def _save_image(image: Image.Image) -> str:
    fd, path = mkstemp(suffix=".png", prefix="fgogacha_")
    os.close(fd)
    try:
        image.convert("RGB").save(path, "PNG")
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path
