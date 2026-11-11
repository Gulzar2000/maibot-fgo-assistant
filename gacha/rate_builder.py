"""从召唤公告的 UP 编号、逐张概率和分时常驻名单构建卡池。"""
from pathlib import Path
from typing import Dict, List
import asyncio
import json
import logging
import math
import re

from .downloader import _get_atlas_cards, fetch_text
from .engine import _build_pool_data
from .pool_fetcher import DATA_VERSION

TOTALS = {"svt5": 1.0, "svt4": 3.0, "svt3": 40.0, "ce5": 4.0, "ce4": 12.0, "ce3": 40.0}
logger = logging.getLogger(__name__)


def _templates(raw: str) -> List[Dict[str, str]]:
    """保持嵌套模板边界，分别解析多个推荐召唤表。"""
    tables = []
    for match in re.finditer(r"\{\{推荐召唤情况\s*\n", raw):
        depth, cursor = 1, match.end()
        for token in re.finditer(r"\{\{|\}\}", raw[cursor:]):
            depth += 1 if token.group() == "{{" else -1
            if depth == 0:
                body = raw[cursor:cursor + token.start()]
                fields = re.split(r"^\|([^=\n]+)=", body, flags=re.M)
                tables.append({fields[i].strip(): fields[i + 1].strip() for i in range(1, len(fields), 2)})
                break
        else:
            raise ValueError("推荐召唤情况模板未闭合")
    return tables


def _lines(fields: Dict[str, str], key: str) -> List[str]:
    # Mooncell 模板明确约定：国服未填写时使用日服同一召唤的公开数据。
    value = fields.get(key + "cn", "").strip() or fields.get(key, "").strip()
    return [line.strip().lstrip("*").strip() for line in value.splitlines() if line.strip()]


def _at(lines: List[str], index: int) -> str:
    return lines[index] if index < len(lines) else (lines[0] if lines else "")


def _ids(value: str) -> List[str]:
    if not value:
        return []
    values = [x.strip() for x in value.split(",")]
    if any(not x.isascii() or not x.isdigit() for x in values):
        raise ValueError(f"无法解析推荐召唤编号: {value}")
    return [str(int(x)) for x in values]


def _row(kind: str, star: int, weight: float, ids: List[str], pickup: bool) -> dict:
    return {"type": f"{kind}{'_pup' if pickup else ''}_{star}", "star": star,
            "weight": weight, "display": int(pickup), "ids": ids}


def _roster(date: str) -> Dict[str, List[str]]:
    resource = json.loads((Path(__file__).parent.parent / "res" / "summon_roster.json").read_text(encoding="utf8"))
    if date < resource["base_date"]:
        raise ValueError(f"卡池早于常驻名单基准日期: {date}")
    roster = {key: [str(x) for x in ids] for key, ids in resource["base"].items()}
    for change in resource["changes"]:
        if change["date"] > date:
            continue
        for key, ids in change["remove"].items():
            roster[key] = [x for x in roster[key] if int(x) not in ids]
        for key, ids in change["add"].items():
            roster[key].extend(str(x) for x in ids)
    return roster


def build_pickup(raw: str, title: str, cards: dict) -> List[dict]:
    """每张 UP 概率乘以同星 UP 数量，剩余概率均分给对应常驻卡。"""
    date_match = re.search(r"^\|开始时间jp=(\d{4}-\d{2}-\d{2})", raw, re.M)
    if date_match is None:
        raise ValueError("召唤页缺少日服原始开始日期，无法确定常驻名单")
    roster = _roster(date_match[1])
    children, seen = [], set()
    for fields in _templates(raw):
        servants, crafts = _lines(fields, "从者序号"), _lines(fields, "礼装序号")
        svt_rates, ce_rates = _lines(fields, "从者概率"), _lines(fields, "礼装概率")
        for index in range(max(len(servants), len(crafts), 1)):
            rows = []
            corrections = []
            picked = {"svt": _ids(_at(servants, index)), "ce": _ids(_at(crafts, index))}
            for kind, rate_lines in (("svt", svt_rates), ("ce", ce_rates)):
                values = _at(rate_lines, index).split(",")
                stars = [star for star in (5, 4, 3) if any(cards[kind][x]["rarity"] == star for x in picked[kind])]
                for star in (5, 4, 3):
                    up = [number for number in picked[kind] if cards[kind][number]["rarity"] == star]
                    probability = 0.0
                    if up:
                        # 实际页面既有完整三列，也有省略未 UP 稀有度的紧凑列。
                        offset = (5 - star) if len(values) == 3 else stars.index(star)
                        value = values[offset] if offset < len(values) else ""
                        match = re.fullmatch(r"(?:各)?\s*(\d+(?:\.\d+)?)\s*%", value.strip())
                        if match is None:
                            raise ValueError(f"{title}: {kind} ★{star} UP 概率缺失或无法解析: {value!r}")
                        probability = float(match[1]) * len(up)
                        # 两处 Wiki 表格复用了不适用的概率，按对应召唤的标准规则修正。
                        if (title == "白色情人节回忆推荐召唤(2026)" and kind == "ce" and star == 5
                                and len(up) == 3 and float(match[1]) == 1.4):
                            probability = 3.0
                            corrections.append("Wiki 三张五星礼装各 1.4% 超过五星总概率；按三 UP 规则修正为各 1.0%")
                        if (title == "「Fate/Grand Order展 -占星回廊-」举办纪念推荐召唤" and kind == "svt"
                                and star == 4 and float(match[1]) == 4.0):
                            probability = 2.1 * len(up)
                            corrections.append("Wiki 四星从者 4.0% 超过四星总概率；按单 UP 规则修正为 2.1%")
                        rows.append(_row(kind, star, probability, up, True))
                    remaining = TOTALS[kind + str(star)] - probability
                    ordinary = [number for number in roster[kind + str(star)] if number not in up]
                    if remaining < -1e-8 or (remaining > 1e-8 and not ordinary):
                        raise ValueError(f"{title}: {kind} ★{star} 概率或常驻名单无效")
                    if remaining > 1e-8:
                        rows.append(_row(kind, star, remaining, ordinary, False))
            fingerprint = json.dumps(rows, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            names = [cards["svt"][number]["name"] for number in picked["svt"]]
            name = " / ".join(names) if names else "礼装推荐召唤"
            if any(child["sub_title"] == name for child in children):
                name += " / " + "、".join(cards["ce"][number]["name"] for number in picked["ce"])
            data = {"banner": title, "sub_title": name, "s_id": len(children), "data_version": DATA_VERSION,
                    "servants": [r for r in rows if r["type"].startswith("svt")],
                    "crafts": [r for r in rows if r["type"].startswith("ce")], "source": "召唤公告概率表"}
            if corrections:
                data["rate_corrections"] = corrections
                logger.warning("修正召唤表: %s: %s", title, "; ".join(corrections))
            _build_pool_data(data)
            if not math.isclose(sum(r["weight"] for r in rows), 100):
                raise ValueError(f"{title}: 总概率不等于 100%")
            children.append(data)
    return children


def build_ce_only(raw: str, title: str, cards: dict) -> List[dict]:
    """英灵星行、英灵极点：从者无 UP，礼装仅四星，按各稀有度均分。"""
    svt_names = re.search(r"^\|推荐召唤从者=(.*)$", raw, re.M)
    ce_names = re.search(r"^\|推荐召唤礼装=(.*)$", raw, re.M)
    if svt_names is None or ce_names is None:
        raise ValueError("礼装限定召唤缺少排出名单")
    resource = json.loads((Path(__file__).parent.parent / "res" / "ce_only_pools.json").read_text(encoding="utf8"))
    config = resource[title]
    svt_ids = [str(x) for x in config["servants"]]
    # 英灵极点另含全部常驻三星；星行只出公告中的指定从者。
    if config.get("permanent_three"):
        svt_ids.extend(_roster(config["date"])["svt3"])
    children = []
    for name, ids in config["craft_groups"].items():
        rows = [_row("svt", star, TOTALS["svt" + str(star)],
                     [x for x in svt_ids if cards["svt"][x]["rarity"] == star], False) for star in (5, 4, 3)]
        rows.append(_row("ce", 4, 56.0, [str(x) for x in ids], True))
        data = {"banner": title, "sub_title": name, "s_id": len(children), "data_version": DATA_VERSION,
                "servants": rows[:-1], "crafts": rows[-1:], "source": config["source"],
                "golden_rates": {"svt5": 1, "svt4": 3, "svt3": 0, "ce5": 0, "ce4": 96, "ce3": 0}}
        _build_pool_data(data)
        children.append(data)
    return children


async def fetch_scheduled_pool_data(pool: dict) -> dict:
    url = pool["href"].rsplit("/", 1)[0]
    raw, cards = await asyncio.gather(fetch_text(url + "?action=raw"), asyncio.to_thread(_get_atlas_cards))
    children = await asyncio.to_thread(build_pickup, raw, pool["title"], cards)
    if not children:
        children = await asyncio.to_thread(build_ce_only, raw, pool["title"], cards)
    return {**children[0], "sub_pools": children}
