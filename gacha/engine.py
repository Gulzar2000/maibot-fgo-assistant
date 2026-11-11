"""FGO 抽卡概率引擎，按 Mooncell 的逐组权重和十一连保底抽取。"""
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import math
import random

GOLDEN_RATES = {"svt5": 1, "svt4": 3, "svt3": 0, "ce5": 4, "ce4": 92, "ce3": 0}
SERVANT_RATES = {"svt5": 1, "svt4": 3, "svt3": 96, "ce5": 0, "ce4": 0, "ce3": 0}
PITY_THRESHOLD = 330


@dataclass
class PityCounter:
    """一次抽卡请求共享的连续未出 UP 次数和确定召唤次数。"""

    misses: int = 0
    triggered: int = 0

    def __post_init__(self) -> None:
        if type(self.misses) is not int or not 0 <= self.misses < PITY_THRESHOLD:
            raise ValueError("保底计数必须是 0 至 329 的整数")


def _single_ssr_pickup(pool: List[dict]) -> Optional[str]:
    """只有恰好一位可抽取的 UP 五星从者时才启用确定召唤。"""
    ids = [number for card in pool
           if card["type"] == "svt" and card["star"] == 5 and card["pickup"] and card["weight"] > 0
           for number in card["ids"]]
    return ids[0] if len(ids) == 1 else None


def gen_servant_icon_line(card_id: str, star: int, name: str, is_pickup: bool) -> str:
    """生成单个从者的文字图标行。"""
    star_icon = "★" * star + "☆" * (5 - star) if star <= 5 else "★" * 5
    pickup_tag = "[UP]" if is_pickup else ""
    return f"{pickup_tag}{star_icon} {name}"


def _build_pool_data(gacha_data: dict) -> List[dict]:
    """保留每一行的权重；拒绝污染数据，不改写 UP 比例或覆盖同名分类。"""
    pool: List[dict] = []
    seen = set()
    for field, kind in (("servants", "svt"), ("crafts", "ce")):
        for card in gacha_data[field]:
            star = int(card["star"])
            key = card["type"]
            if star not in (3, 4, 5) or key not in (f"{kind}_{star}", f"{kind}_pup_{star}"):
                raise ValueError(f"非法卡片分类: {key}")
            weight = float(card["weight"])
            ids = [str(number) for number in card["ids"]]
            if not math.isfinite(weight) or weight < 0 or not ids:
                raise ValueError(f"非法权重或空分类: {key}")
            for number in ids:
                if not number.isascii() or not number.isdigit() or int(number) <= 0:
                    raise ValueError(f"非法卡片 ID: {kind} {number!r}")
                identity = (kind, number)
                if identity in seen:
                    raise ValueError(f"重复卡片: {kind} #{number}，请重新获取卡池")
                seen.add(identity)
            pool.append({"type": kind, "star": star, "pickup": "pup" in key, "weight": weight, "ids": ids})
    if not pool or sum(card["weight"] for card in pool) <= 0:
        raise ValueError("卡池为空或总权重为零")
    return pool


def _roll_once(rate: float, pool: List[dict], rate_group: Optional[Dict[str, int]] = None) -> Tuple[str, str, str]:
    """按逐组累积权重单抽；保底仅调整稀有度总权重，保留各组的 UP 比例。"""
    if not 0 <= rate < 1:
        raise ValueError("随机数必须位于 [0, 1)")
    totals: Dict[str, float] = {}
    for card in pool:
        key = f"{card['type']}{card['star']}"
        totals[key] = totals.get(key, 0.0) + card["weight"]
    weights = []
    for card in pool:
        key = f"{card['type']}{card['star']}"
        coefficient = rate_group[key] / totals[key] if rate_group is not None and totals[key] > 0 else 1.0
        weights.append(card["weight"] * coefficient)
    total = sum(weights)
    if total <= 0:
        raise ValueError("卡池没有满足保底条件的卡片")
    threshold = rate * total
    cumulative = 0.0
    # 忽略零权重分类；最终区间吸收浮点累加误差，不生成 ID 0 占位卡。
    available = [(card, weight) for card, weight in zip(pool, weights) if weight > 0]
    selected = available[-1][0]
    for card, weight in available:
        cumulative += weight
        if threshold < cumulative:
            selected = card
            break
    star = str(selected["star"])
    rarity = ("up" if selected["pickup"] else "") + star
    kind = "svt" if selected["type"] == "svt" else "cft"
    if kind == "cft":
        rarity = "ce_" + rarity
    return kind, rarity, random.choice(selected["ids"])


def _roll_with_pity(
    pool: List[dict], pickup_id: Optional[str], pity: Optional[PityCounter],
    rate_group: Optional[Dict[str, int]] = None,
) -> dict:
    """先按实际抽取顺序判断第 330 抽，再累计或重置连续未出 UP 次数。"""
    if pity is not None and pickup_id is not None and pity.misses == PITY_THRESHOLD - 1:
        pity.misses = 0
        pity.triggered += 1
        return {"type": "svt", "rarity": "up5", "id": pickup_id}

    kind, rarity, number = _roll_once(random.random(), pool, rate_group)
    if pity is not None:
        if pickup_id is None or (kind == "svt" and number == pickup_id):
            pity.misses = 0
        else:
            pity.misses += 1
    return {"type": kind, "rarity": rarity, "id": number}


def simulate_ten_pull(gacha_data: dict, pity: Optional[PityCounter] = None) -> List[dict]:
    """十一连 = 九次普通抽取 + 一次四星以上保底 + 一次从者保底，再打乱顺序。"""
    pool = _build_pool_data(gacha_data)
    pickup_id = _single_ssr_pickup(pool)
    result: List[dict] = []
    for group in [None] * 9 + [gacha_data.get("golden_rates", GOLDEN_RATES), SERVANT_RATES]:
        result.append(_roll_with_pity(pool, pickup_id, pity, group))
    # 展示打乱发生在计数之后，避免把十连中的出货和保底顺序颠倒。
    random.shuffle(result)
    return result


def simulate_hundred_pull(gacha_data: dict, pity: Optional[PityCounter] = None) -> List[dict]:
    """百连 = 十次十一连，共 110 抽，每组保留金卡及从者保底。"""
    results: List[dict] = []
    for _ in range(10):
        results.extend(simulate_ten_pull(gacha_data, pity))
    return _count_gacha_results(results, gacha_data)


def _count_gacha_results(results: List[dict], gacha_data: dict) -> List[dict]:
    """按类型、编号和稀有度统计，避免从者和礼装编号相同而误合并。"""
    counter = Counter((card["type"], card["id"], card["rarity"]) for card in results)
    summary = []
    for (kind, number, rarity), count in counter.most_common():
        prefix = "svt" if kind == "svt" else "ce"
        pickup = "up" if "up" in rarity else ""
        summary.append({"card_id": number, "count": count, "type": f"{pickup}{prefix}_{rarity[-1]}"})
    return summary


def format_ten_pull_result_text(result: list[dict], server: str = "国服", name_map: dict[str, dict[str, str]] | None = None, banner: str = "") -> str:
    """将11连结果格式化为文字版消息"""
    title = f"=== FGO {server} 十连抽卡 ==="
    if banner:
        title += f" [{banner}]"
    lines = [title]
    for i, card in enumerate(result):
        card_type = card.get("type", "")
        is_svt = "svt" in str(card_type)
        card_label = "从者" if is_svt else "礼装"
        rarity = str(card.get("rarity", "?"))
        rarity_display = {
            "up5": "★★★★★ [UP]", "5": "★★★★★",
            "up4": "★★★★ [UP]", "4": "★★★★",
            "up3": "★★★ [UP]", "3": "★★★",
            "ce_up5": "★★★★★ [UP]", "ce_5": "★★★★★",
            "ce_up4": "★★★★ [UP]", "ce_4": "★★★★",
            "ce_up3": "★★★ [UP]", "ce_3": "★★★",
            "4 or 5": "保底四星以上", "3 or 4 or 5": "保底从者",
        }.get(rarity, rarity)
        card_id = str(card.get("id", "?"))
        name = _lookup_name(card_id, card_type, name_map or {})
        lines.append(f"  {i+1:2d}. {rarity_display} {card_label} {name}")
    return "\n".join(lines)


def format_hundred_pull_result_text(summary: list[dict], server: str = "国服", name_map: dict[str, dict[str, str]] | None = None, banner: str = "") -> str:
    """将百连（110 抽）结果格式化为文字版摘要"""
    nm = name_map or {}
    title = f"=== FGO {server} 百连抽卡（110 抽） ==="
    if banner:
        title += f" [{banner}]"
    lines = [title, ""]
    for label, type_filter in [("五星从者", "svt_5"), ("四星从者", "svt_4"), ("五星礼装", "ce_5"), ("四星礼装", "ce_4")]:
        cards = [s for s in summary if type_filter in s.get("type", "")]
        if cards:
            lines.append(f"  {label}:")
            for s in cards:
                ctype = s.get("type", "")
                name = _lookup_name(str(s["card_id"]), ctype, nm)
                lines.append(f"    {name} x{s['count']}")
    if not [l for l in lines[2:] if not l.startswith("  ★")]:
        return lines[0] + "\n(无高星卡)"
    return "\n".join(lines)


def _lookup_name(card_id: str, card_type: str, name_map: dict[str, dict[str, str]]) -> str:
    """根据卡片类型查找名称"""
    if "svt" in str(card_type):
        name = name_map.get("svt", {}).get(card_id, "")
    else:
        name = name_map.get("ce", {}).get(card_id, "")
    return f"#{card_id} {name}" if name else f"#{card_id}"
