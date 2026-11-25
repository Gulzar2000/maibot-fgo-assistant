"""真实出率排行榜的数据排序、头像加载与图片渲染。"""
from fractions import Fraction
from io import BytesIO
from typing import Dict, List, NamedTuple, Optional
from aiohttp import ClientError, ClientSession, ClientTimeout
from PIL import Image, ImageDraw, ImageFont, ImageOps
import asyncio
import base64
import logging

from .image_gen import _find_font
from .real_statistics import CaptureRate

logger = logging.getLogger(__name__)
MIN_RANK_DRAWS = 500


class RankRow(NamedTuple):
    user_id: str
    nickname: str
    rate: CaptureRate
    change: Optional[int]


def build_ranking(
    rates: Dict[str, CaptureRate], nicknames: Dict[str, str], previous: List[str],
) -> List[RankRow]:
    def sort_key(user_id: str):
        total_draws, ssr_count, _ = rates[user_id]
        return -Fraction(ssr_count, total_draws), -total_draws, int(user_id)

    old_ranks = {user_id: place for place, user_id in enumerate(previous, 1)}
    eligible = (user_id for user_id, rate in rates.items() if rate.total_draws >= MIN_RANK_DRAWS)
    top = sorted(eligible, key=sort_key)[:5]
    return [
        RankRow(user_id, nicknames[user_id], rates[user_id],
                old_ranks[user_id] - place if user_id in old_ranks else None)
        for place, user_id in enumerate(top, 1)
    ]


async def fetch_avatars(rows: List[RankRow]) -> List[Optional[bytes]]:
    if not rows:
        return []
    async with ClientSession(timeout=ClientTimeout(total=4)) as session:
        async def fetch(user_id: str) -> Optional[bytes]:
            try:
                url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
                async with session.get(url, raise_for_status=True) as response:
                    return await response.read()
            except (ClientError, TimeoutError) as error:
                logger.warning("FGO 排行榜头像请求失败：%s", type(error).__name__)
                return None

        return list(await asyncio.gather(*(fetch(row.user_id) for row in rows)))


def _fit_name(draw: ImageDraw.ImageDraw, name: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    name = " ".join(name.split()) or "未设置昵称"
    if draw.textlength(name, font=font) <= width:
        return name
    for length in range(1, len(name) + 1):
        if draw.textlength(name[:length] + "…", font=font) > width:
            return name[:length - 1].rstrip() + "…"
    return name


def _paste_avatar(image: Image.Image, payload: Optional[bytes], position: tuple[int, int]) -> None:
    size = (56, 56)
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).ellipse((0, 0, 55, 55), fill=255)
    if payload:
        try:
            with Image.open(BytesIO(payload)) as source:
                avatar = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
            image.paste(avatar, position, mask)
            return
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            logger.warning("FGO 排行榜头像解码失败：%s", type(error).__name__)
    avatar = Image.new("RGB", size, "#384656")
    draw = ImageDraw.Draw(avatar)
    draw.ellipse((20, 10, 36, 26), fill="#B3C2D0")
    draw.rounded_rectangle((13, 30, 43, 50), radius=9, fill="#B3C2D0")
    image.paste(avatar, position, mask)


def render_ranking(rows: List[RankRow], avatars: List[Optional[bytes]], res_dir: str) -> str:
    font_path = _find_font(res_dir)
    title, name_font, rate_font, detail = (ImageFont.truetype(font_path, size) for size in (36, 24, 29, 17))
    image = Image.new("RGB", (840, 128 + 82 * max(1, len(rows))), "#101925")
    draw = ImageDraw.Draw(image)
    draw.text((28, 23), "本群五星从者出率榜", font=title, fill="#F8E6BD")
    draw.text((30, 74), f"最近一次真实出率快照 · 至少 {MIN_RANK_DRAWS} 抽", font=detail, fill="#B4C1D3")
    draw.text((790, 87), "名次变化", anchor="rm", font=detail, fill="#B4C1D3")

    if not rows:
        draw.text((32, 145), "本群暂无可上榜的真实出率快照", font=name_font, fill="#E6EDF5")

    for place, (row, avatar) in enumerate(zip(rows, avatars, strict=True), 1):
        top = 106 + (place - 1) * 82
        draw.rounded_rectangle((26, top, 814, top + 72), radius=16, fill="#1F2D3D", outline="#354456")
        draw.text((59, top + 36), str(place), anchor="mm", font=rate_font, fill="#F8D28F")
        _paste_avatar(image, avatar, (92, top + 8))
        draw.text((166, top + 36), _fit_name(draw, row.nickname, name_font, 346), anchor="lm",
                  font=name_font, fill="#F0F4F8")

        total_draws, ssr_count, estimated = row.rate
        percent = f"{ssr_count / total_draws:.2%}"
        approx = "暂估 · " if estimated else ""
        draw.text((672, top + 25), percent, anchor="rm", font=rate_font, fill="#F0F4F8")
        draw.text((672, top + 53), f"{approx}{ssr_count} ★5 / {total_draws} 抽", anchor="rm",
                  font=detail, fill="#B4C1D3")

        if row.change is None:
            label, color = "new", "#FFD289"
        elif row.change > 0:
            label, color = f"↑ {row.change}", "#71E2A6"
        elif row.change < 0:
            label, color = f"↓ {-row.change}", "#FF8B8B"
        else:
            label, color = "=", "#B9C2CD"
        draw.text((760, top + 36), label, anchor="mm", font=name_font, fill=color)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
