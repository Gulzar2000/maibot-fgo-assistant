"""Mooncell 图鉴中的成品召唤卡片缩略图，独立于 Atlas 方形头像缓存。"""
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit
import asyncio
import csv
import json
import re

from .downloader import download_file, fetch_text

INDEX_PAGES = {"svt": "英灵图鉴", "ce": "礼装图鉴"}
INDEX_VERSION = 1


def _parse_thumbnail_index(html: str, kind: str) -> Dict[str, dict]:
    """读取图鉴嵌入的 CSV 字符串，不执行页面 JavaScript 或拼猜图片文件名。"""
    match = re.search(r'\braw_str\s*=\s*("(?:\\.|[^"\\])*")', html)
    if match is None:
        raise ValueError(f"Mooncell {INDEX_PAGES[kind]} 缺少 raw_str 数据")
    rows = csv.DictReader(StringIO(json.loads(match.group(1))))
    field = "avatar" if kind == "svt" else "icon"
    cards: Dict[str, dict] = {}
    for row in rows:
        number = str(int(row["id"]))
        if int(number) <= 0 or number in cards:
            raise ValueError(f"Mooncell {kind} 缩略图编号非法或重复: {number}")
        source = row[field]
        parts = urlsplit("https:" + source if source.startswith("//") else source)
        if parts.scheme != "https" or parts.netloc != "media.fgo.wiki":
            raise ValueError(f"Mooncell {kind} #{number} 缩略图地址非法: {source}")
        url = urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%"), parts.query, ""))
        cards[number] = {"url": url, "star": int(row["star"])}
    if not cards:
        raise ValueError(f"Mooncell {INDEX_PAGES[kind]} 缩略图列表为空")
    return cards


def _read_index(path: Path) -> Optional[Dict[str, Dict[str, dict]]]:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["version"] != INDEX_VERSION:
        return None
    return data["cards"]


def _write_index(path: Path, cards: Dict[str, Dict[str, dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 并发首次抽卡允许同时刷新；每次使用独立临时文件并原子替换完整索引。
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump({"version": INDEX_VERSION, "cards": cards}, file, ensure_ascii=False)
        temporary = Path(file.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def load_thumbnail_index(icon_dir: str, refresh: bool = False) -> Dict[str, Dict[str, dict]]:
    """缓存两个完整图鉴索引；只在首次使用、主动下载或遇到新卡时刷新。"""
    path = Path(icon_dir) / "cards" / "index.json"
    if not refresh:
        cached = await asyncio.to_thread(_read_index, path)
        if cached is not None:
            return cached
    kinds = list(INDEX_PAGES)
    pages = await asyncio.gather(*(fetch_text("https://fgo.wiki/w/" + quote(INDEX_PAGES[kind])) for kind in kinds))
    cards = dict(zip(kinds, await asyncio.gather(*(
        asyncio.to_thread(_parse_thumbnail_index, html, kind) for kind, html in zip(kinds, pages)
    ))))
    await asyncio.to_thread(_write_index, path, cards)
    return cards


def result_card_path(icon_dir: str, kind: str, number: str) -> Path:
    """从者和礼装即使编号相同，也使用不同文件。"""
    return Path(icon_dir) / "cards" / f"{kind}_{number}.png"


async def download_result_card(kind: str, number: str, icon_dir: str, index: Dict[str, Dict[str, dict]]) -> bool:
    card = index[kind].get(number)
    if card is None:
        raise ValueError(f"Mooncell 图鉴中找不到 {kind} #{number}")
    return await download_file(card["url"], str(result_card_path(icon_dir, kind, number)))


async def ensure_result_cards(result: List[dict], icon_dir: str) -> None:
    """按十一连实际结果补齐小图，网络下载与文件校验均不占用主事件循环。"""
    index = await load_thumbnail_index(icon_dir)
    requested = {("svt" if card["type"] == "svt" else "ce", str(card["id"])) for card in result}
    if any(number not in index[kind] for kind, number in requested):
        index = await load_thumbnail_index(icon_dir, refresh=True)
    # 成品卡片已带星级，先校验抽取结果与图鉴一致，避免错误图片误导结果。
    for card in result:
        kind = "svt" if card["type"] == "svt" else "ce"
        number = str(card["id"])
        if number not in index[kind]:
            raise ValueError(f"Mooncell 图鉴中找不到 {kind} #{number}")
        if index[kind][number]["star"] != int(card["rarity"][-1]):
            raise ValueError(f"{kind} #{number} 的结果星级与图鉴图片不一致")
    semaphore = asyncio.Semaphore(4)

    async def download(kind: str, number: str) -> None:
        async with semaphore:
            if not await download_result_card(kind, number, icon_dir, index):
                raise OSError(f"召唤卡片图片下载失败: {kind} #{number}")

    await asyncio.gather(*(download(kind, number) for kind, number in sorted(requested)))
