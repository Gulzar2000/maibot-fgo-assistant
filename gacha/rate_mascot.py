"""完整女主立绘按实际展示的吐槽类别切换。"""
from pathlib import Path
from typing import Any, Dict, List, Optional
from PIL import Image

from .rate_quips import select_quip_context


MASCOTS: Dict[str, str] = {
    "normal": "riyo_gudako.png",
    "whip": "gudako_whip.png",
    "laugh": "gudako_laugh_fullbody.png",
    "shocked": "gudako_shocked_fullbody.png",
    "cry": "gudako_cry_fullbody.png",
    "rage": "gudako_rage_fullbody.png",
}

CONTEXT_MASCOTS: Dict[str, str] = {
    "Grand 欧皇": "whip", "欧皇": "laugh", "中庸": "normal",
    "略非": "cry", "非酋": "cry", "酋长": "rage",
    "small_sample": "normal", "unchanged": "normal",
    "corrected": "shocked", "same_draws": "shocked",
    "dry_330": "rage", "dry_100": "rage", "dry_30": "cry", "dry_short": "cry",
    "multi": "whip", "promoted": "whip", "demoted": "cry",
    "rise": "laugh", "fall_with_ssr": "cry", "flat": "normal",
}


def load_mascot(resources: Path, points: List[Dict[str, Any]], variant: Optional[str] = None) -> Image.Image:
    name = CONTEXT_MASCOTS[select_quip_context(points)] if variant is None else variant
    filename = MASCOTS[name]
    with Image.open(resources / filename) as source:
        mascot = source.convert("RGBA")
    # 原图留白不同；以可见区域统一缩放，避免挥鞭图在右侧显得过小。
    bounds = mascot.getbbox()
    if bounds is None:
        raise ValueError("女主素材为空：" + filename)
    return mascot.crop(bounds)
