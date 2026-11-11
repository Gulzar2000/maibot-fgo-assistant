"""下载工具 — 使用 Atlas Academy API 和标准库 HTTP 客户端。"""
from http.client import HTTPException
from io import BytesIO
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Dict, Optional
from urllib.request import Request, urlopen
from PIL import Image
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "MaiBot-FGOGacha/1.0 (Mooncell and Atlas Academy client)"}
_ATLAS_CARDS: Optional[Dict[str, Dict[str, dict]]] = None
_ATLAS_LOCK = Lock()


def _fetch_bytes_sync(url: str) -> bytes:
    with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
        return response.read()


async def fetch_text(url: str) -> str:
    return (await fetch_bytes(url)).decode("utf-8")


async def fetch_bytes(url: str, use_image_headers: bool = False) -> bytes:
    return await asyncio.to_thread(_fetch_bytes_sync, url)


def _valid_image(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, SyntaxError):
        return False


def _save_image(data: bytes, path: str) -> None:
    # 校验实际图片内容，不能用文件大小判定下载成功。
    with Image.open(BytesIO(data)) as image:
        image.verify()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 每次使用独立临时文件，避免并发抽卡下载同一张图片时互相覆盖 .part 文件。
    with NamedTemporaryFile(mode="wb", dir=os.path.dirname(path), delete=False) as file:
        file.write(data)
        temporary = file.name
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


async def download_file(url: str, path: str) -> bool:
    if await asyncio.to_thread(_valid_image, path):
        return True
    try:
        data = await fetch_bytes(url)
        await asyncio.to_thread(_save_image, data, path)
        return True
    except (HTTPException, OSError, ValueError, SyntaxError) as error:
        logger.warning("图片下载失败 %s -> %s: %s", url, path, error)
        return False


def _get_atlas_cards(refresh: bool = False) -> Dict[str, Dict[str, dict]]:
    """合并国服和日服索引；中文名称优先，图片地址取对应数据的 face 字段。"""
    global _ATLAS_CARDS
    with _ATLAS_LOCK:
        if refresh or _ATLAS_CARDS is None:
            cards: Dict[str, Dict[str, dict]] = {"svt": {}, "ce": {}}
            # JP 覆盖尚未实装国服的卡，CN 覆盖已有卡的名称和资源。
            for region in ("JP", "CN"):
                for kind, export in (("svt", "servant"), ("ce", "equip")):
                    url = f"https://api.atlasacademy.io/export/{region}/basic_{export}.json"
                    for card in json.loads(_fetch_bytes_sync(url)):
                        if card["collectionNo"] > 0:
                            cards[kind][str(card["collectionNo"])] = card
            _ATLAS_CARDS = cards
        return _ATLAS_CARDS


async def _download_icon(collection_no: str, dest_dir: str, kind: str) -> bool:
    cards = await asyncio.to_thread(_get_atlas_cards)
    card = cards[kind].get(str(collection_no))
    if card is None:
        logger.warning("Atlas 索引中找不到卡片: %s #%s", kind, collection_no)
        return False
    path = os.path.join(dest_dir, f"{kind}_{collection_no}.png")
    return await download_file(card["face"], path)


async def download_servant_icon(collection_no: str, dest_dir: str) -> bool:
    """从 Atlas Academy 下载从者头像 (collectionNo → svt_{no}.png)。"""
    return await _download_icon(collection_no, dest_dir, "svt")


async def download_ce_icon(collection_no: str, dest_dir: str) -> bool:
    """从 Atlas Academy 下载礼装图标 (collectionNo → ce_{no}.png)。"""
    return await _download_icon(collection_no, dest_dir, "ce")


def build_card_name_map() -> Dict[str, Dict[str, str]]:
    """从 Atlas Academy 构建 ID→名称映射，合并 JP 新卡与 CN 中文名称。"""
    return {
        kind: {number: card["name"] for number, card in cards.items()}
        for kind, cards in _get_atlas_cards(refresh=True).items()
    }
