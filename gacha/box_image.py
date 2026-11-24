"""把按星级和职阶排序的从者画在一张图中。"""
from io import BytesIO
from itertools import groupby
from pathlib import Path
from typing import Dict, List, Tuple
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
import base64

from .box import BoxCard
from .image_gen import _find_font

COLUMNS = 14
CARD_WIDTH = 132
CARD_HEIGHT = 144
GAP = 8
MARGIN = 24
HEADER = 105
SECTION_HEADER = 43
SECTION_GAP = 14
RARITY_COLORS = ("#A881CF", "#C58A64", "#C58A64", "#AABBCB", "#E8C477", "#F4D580")


def _np_icon(res_dir: str) -> Image.Image:
    with Image.open(Path(res_dir) / "icon_nplv.png") as source:
        icon = ImageOps.expand(source.convert("RGBA"), border=3)
    glow = Image.new("RGBA", icon.size, "white")
    glow.putalpha(icon.getchannel("A").filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(1)))
    return Image.alpha_composite(glow, icon)


def render_box(cards: List[BoxCard], artwork: Dict[int, Tuple[Path, int]], res_dir: str) -> str:
    # 用带框图本身的星级分区，保持前面已经排好的职阶和图鉴编号顺序。
    def rarity(card: BoxCard) -> int:
        return artwork[card.number][1]

    groups = [(star, list(group)) for star, group in groupby(sorted(cards, key=rarity, reverse=True), key=rarity)]
    columns = min(COLUMNS, max(len(group) for _, group in groups))
    width = max(640, 2 * MARGIN + columns * (CARD_WIDTH + GAP) - GAP)
    heights = [SECTION_HEADER + ((len(group) + columns - 1) // columns) * (CARD_HEIGHT + GAP) + SECTION_GAP
               for _, group in groups]
    image = Image.new("RGB", (width, HEADER + sum(heights) + MARGIN), "#121923")
    draw = ImageDraw.Draw(image)
    font_path = _find_font(res_dir)
    title, subtitle, heading, detail, compact = (
        ImageFont.truetype(font_path, size) for size in (34, 18, 23, 16, 14)
    )
    np_icon = _np_icon(res_dir)
    draw.text((MARGIN, 18), "持有从者图鉴", font=title, fill="#F5F7FA")
    draw.text((MARGIN, 65), f"{len(cards)} 种从者  ·  宝具与等级 / 再临-主动技能 / 追加技能（- 未解锁）",
              font=subtitle, fill="#BDCBDB")

    top = HEADER
    for (star, group), height in zip(groups, heights):
        color = RARITY_COLORS[star]
        draw.line((MARGIN, top, width - MARGIN, top), fill="#465262")
        draw.text((MARGIN, top + 7), f"{star} 星  ·  {len(group)} 种", font=heading, fill=color)
        for index, card in enumerate(group):
            row, column = divmod(index, columns)
            x = MARGIN + column * (CARD_WIDTH + GAP)
            y = top + SECTION_HEADER + row * (CARD_HEIGHT + GAP)
            with Image.open(artwork[card.number][0]) as portrait:
                face = ImageOps.contain(portrait.convert("RGBA"), (CARD_WIDTH, CARD_HEIGHT), Image.Resampling.LANCZOS)
                image.paste(face, (x + (CARD_WIDTH - face.width) // 2, y + (CARD_HEIGHT - face.height) // 2), face)

            # 和 Chaldea 一样从左下角排版，有追加技能时才占用第三行。
            left = x + 7
            bottom = y + CARD_HEIGHT - 8
            line_height = 17
            has_append = any(card.append)
            first_line = bottom - line_height * (2 if has_append else 1)
            image.paste(np_icon, (left - 3, first_line - np_icon.height + 6), np_icon)
            np_level = str(card.np_level)
            number_x = left + np_icon.width - 3
            level_x = number_x + draw.textlength(np_level, font=detail) + 8
            active = "/".join(map(str, card.skills))
            labels = [
                (number_x, first_line, np_level, detail),
                (level_x, first_line, f"Lv.{card.level}", detail),
                (left, first_line + line_height, f"{card.ascension}-{active}", detail),
            ]
            if has_append:
                append = "/".join(str(level) if level else "-" for level in card.append)
                labels.append((left, bottom, append, compact))
            for label_x, label_y, value, font in labels:
                draw.text((label_x, label_y), value, anchor="lb", font=font,
                          fill="black", stroke_width=2, stroke_fill="white")
        top += height

    output = BytesIO()
    image.save(output, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")
