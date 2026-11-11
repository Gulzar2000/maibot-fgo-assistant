"""Mooncell wiki 卡池数据抓取"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import quote
from bs4 import BeautifulSoup
import ast
import asyncio
import json
import logging
import math
import os
import re

from .downloader import fetch_text
from .engine import _build_pool_data

logger = logging.getLogger(__name__)

SCHEDULE_PAGE = "https://fgo.wiki/w/" + quote("卡池一览")
FUTURE_PAGE = "https://fgo.wiki/w/" + quote("卡池一览/未来视卡池")
CHINA_TZ = timezone(timedelta(hours=8))
DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\([^)]*\))?(?:\s+(\d{1,2}):(\d{2}))?")

JS_STRING = r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\""
RAW_LIST_PATTERN = re.compile(r"raw_str_list\s*=\s*(\[(?:\s*(?:" + JS_STRING + r")\s*,?)*\s*\])")
NAME_LIST_PATTERN = re.compile(r"name_list\s*=\s*(\[(?:\s*(?:" + JS_STRING + r")\s*,?)*\s*\])")
DATA_VERSION = 4


def _parse_schedule_html(html: str, now: datetime) -> List[dict]:
    """解析国服当前与未来日程；时间直接取 Wiki 表格，不按日服日期自行推算。"""
    soup = BeautifulSoup(html, "html.parser")
    pools: List[dict] = []
    for table in soup.select(".mw-parser-output table"):
        heading = table.find("th")
        if heading is None or heading.get_text(strip=True) not in ("国服当前卡池", "国服未来卡池"):
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            link = cells[0].find("a", href=True, title=True)
            if link is None:
                raise ValueError("国服日程行缺少卡池链接")
            title = link["title"]
            # 普通抽卡引擎不处理福袋、Destiny Order 和 Beast Order 特殊召唤。
            if re.search(r"福袋|Destiny Order|Beast Order", title):
                continue
            time_text = cells[1].get_text(" ", strip=True)
            matches = list(DATE_PATTERN.finditer(time_text))
            if len(matches) != 2:
                raise ValueError(f"卡池时间格式无法识别: {title}: {time_text}")
            times = []
            for index, match in enumerate(matches):
                year, month, day, hour, minute = match.groups()
                # Wiki 预计日期通常无时分，保存日期精度，筛选结束日时覆盖整日。
                times.append(datetime(int(year), int(month), int(day),
                                      int(hour) if hour else (23 if index else 0),
                                      int(minute) if minute else (59 if index else 0), tzinfo=CHINA_TZ))
            start, end = times
            if end < start:
                raise ValueError(f"卡池结束时间早于开始时间: {title}")
            if end < now:
                continue
            base_url = link["href"]
            if not base_url.startswith("/w/"):
                raise ValueError(f"卡池页面链接格式错误: {base_url}")
            pools.append({
                "title": title, "href": "https://fgo.wiki" + base_url + "/" + quote("模拟器"),
                "banner": title, "server": "国服", "type": "normal",
                "start_at": start.isoformat(), "end_at": end.isoformat(),
                "start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d"),
                "estimated": "预计" in time_text,
                "status": "当前" if start <= now else "未来",
            })
    return pools


async def fetch_pool_list() -> List[dict]:
    """下载国服当前与完整未来日程，去重后按开始时间升序编号。"""
    now = datetime.now(CHINA_TZ)
    pages = await asyncio.gather(fetch_text(SCHEDULE_PAGE), fetch_text(FUTURE_PAGE))
    parsed = await asyncio.gather(*(asyncio.to_thread(_parse_schedule_html, html, now) for html in pages))
    by_url = {}
    for pools in parsed:
        for pool in pools:
            by_url.setdefault(pool["href"], pool)
    pools = sorted(by_url.values(), key=lambda pool: (pool["start_at"], pool["end_at"], pool["title"]))
    if not pools:
        raise ValueError("Wiki 日程中没有有效的国服当前或未来卡池")
    for index, pool in enumerate(pools):
        pool["id"] = index
    return pools


async def fetch_pool_data(pool_url: str) -> Optional[dict]:
    """独立解析每个子池，返回首池及带原始子编号的完整子池列表。"""
    html = await fetch_text(pool_url)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(id="firstHeading")
    banner = heading.get_text(strip=True) if heading else ""
    match = RAW_LIST_PATTERN.search(html)
    names_match = NAME_LIST_PATTERN.search(html)
    names = ast.literal_eval(names_match.group(1)) if names_match else []
    raw_list = ast.literal_eval(match.group(1)) if match else [await fetch_text(pool_url + "/data1?action=raw")]
    sub_pools: List[dict] = []
    for index, raw in enumerate(raw_list):
        if not raw.strip():
            continue
        raw = await _resolve_data_references(raw)
        data = _parse_js_data(raw, banner)
        data["s_id"] = index
        data["sub_title"] = names[index] if index < len(names) and names[index] else str(index)
        sub_pools.append(data)
    if not sub_pools:
        raise ValueError(f"模拟器没有有效子池: {pool_url}")
    return {**sub_pools[0], "sub_pools": sub_pools}


async def _resolve_data_references(raw: str, visited: Optional[List[str]] = None) -> str:
    """解析旧卡池中的 data 页面引用，禁止把 Wiki 链接当作卡片编号。"""
    visited = [] if visited is None else visited
    pattern = re.compile(r"\[\[:([^]\n]+)\]\]")
    for match in list(pattern.finditer(raw)):
        title = match.group(1)
        if title in visited or len(visited) >= 5:
            raise ValueError(f"卡池 data 页面引用循环或过深: {title}")
        text = await fetch_text("https://fgo.wiki/w/" + quote(title, safe="/") + "?action=raw")
        text = await _resolve_data_references(text, visited + [title])
        raw = raw.replace(match.group(0), text)
    return raw


def _parse_data_page(raw_data: str, banner: Optional[str]) -> dict:
    """严格解析 TSV 数据；每行按实际 type 分类，非法行完整暴露。"""
    servants: List[dict] = []
    crafts: List[dict] = []
    for line_number, line in enumerate(raw_data.strip().splitlines(), 1):
        if not line.strip():
            continue
        parts = line.strip().split("\t")
        if parts == ["type", "star", "weight", "display", "ids"]:
            continue
        if len(parts) != 5 or parts[0] not in ("svt", "ce"):
            raise ValueError(f"卡池第 {line_number} 行格式错误: {line!r}")
        kind, star_text, weight_text, display, ids = parts
        star, weight = int(star_text), float(weight_text)
        numbers = [number.strip() for number in ids.split(",")]
        if star not in (3, 4, 5) or not math.isfinite(weight) or weight < 0 or display not in ("0", "1"):
            raise ValueError(f"卡池第 {line_number} 行属性错误: {line!r}")
        key = kind + ("_pup" if display == "1" else "")
        card = {"type": f"{key}_{star}", "star": star, "weight": weight, "display": int(display), "ids": numbers}
        (servants if kind == "svt" else crafts).append(card)
    data = {"banner": banner or "", "servants": servants, "crafts": crafts, "data_version": DATA_VERSION}
    _build_pool_data(data)
    return data


def _parse_js_data(raw_str: str, banner: Optional[str]) -> dict:
    """兼容 JS 解码后的换行，以及旧调用传入的转义换行。"""
    return _parse_data_page(raw_str.replace("\\n", "\n").replace("\\t", "\t"), banner)


def save_pools(pools: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pools, f, indent=2, ensure_ascii=False)


def load_pools(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
