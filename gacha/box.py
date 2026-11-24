"""从当前持有卡片和国服从者索引生成图鉴数据。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple
import json
import time

from .credentials import atomic_write
from .downloader import _fetch_bytes_sync
from .real_statistics import count, table

SERVANT_INDEX_URL = "https://api.atlasacademy.io/export/CN/basic_servant.json"
INDEX_MAX_AGE = 86400
INDEX_MIN_AGE = 900
PLAYABLE_TYPES = {"normal", "heroine"}
# 按游戏职阶排序优先级排列。兽阶的不同编号共用一个排序位置。
CLASS_ORDER = {
    1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6,
    9: 7, 11: 8, 10: 9, 23: 10, 25: 11, 28: 12, 33: 13, 38: 13, 40: 13, 8: 14,
}
APPEND_SLOTS = range(100, 105)
WITHDRAWAL = 4


@dataclass(frozen=True)
class BoxCard:
    number: int
    class_id: int
    level: int
    np_level: int
    ascension: int
    skills: Tuple[int, ...]
    append: Tuple[int, ...]


def load_box_index(path: Path, servant_ids: List[int]) -> Dict[int, dict]:
    rows = {}
    if path.is_file():
        rows = {row["id"]: row for row in json.loads(path.read_bytes())}
        age = time.time() - path.stat().st_mtime
        if rows and (age < INDEX_MIN_AGE or (age < INDEX_MAX_AGE and all(i in rows for i in servant_ids))):
            return rows

    raw = _fetch_bytes_sync(SERVANT_INDEX_URL)
    rows = {row["id"]: row for row in json.loads(raw)}
    if not rows:
        raise ValueError("国服从者索引为空")
    atomic_write(path, raw)
    return rows


def owned_servants(root: dict) -> List[dict]:
    cache = root["cache"]
    # 按实体合并背包和第二保管室，图鉴历史表还包含已经出售的从者。
    storage = table(cache, "userSvtStorage", "id", required=False)
    inventory = table(cache, "userSvt", "id")
    instances = {count(row, "id"): row for row in storage + inventory}
    owned = []
    for row in instances.values():
        # 国服正式从者也可能带 16 标志（包括玛修），只排除明确已离队的卡。
        status = row.get("status")
        if status is not None and int(status) & WITHDRAWAL:
            continue
        owned.append(row)
    return owned


def build_box_cards(cache: dict, owned: List[dict], index: Dict[int, dict]) -> List[BoxCard]:
    unlocked = {count(row, "svtId"): {int(slot): 1 for slot in row["unlockNums"]}
                for row in table(cache, "userSvtAppendPassiveSkill", "svtId", required=False)}
    leveled = {}
    for row in table(cache, "userSvtAppendPassiveSkillLv", "userSvtId", required=False):
        leveled[count(row, "userSvtId")] = {
            int(slot): int(level)
            for slot, level in zip(row["appendPassiveSkillNums"], row["appendPassiveSkillLvs"], strict=True)
        }

    # 同图鉴编号只展示一张实际卡。先选宝具等级，再比较同一张卡上的养成进度。
    selected: Dict[int, Tuple[Tuple[int, ...], BoxCard]] = {}
    for row in owned:
        servant_id = count(row, "svtId")
        info = index.get(servant_id)
        if info is None or info["type"] not in PLAYABLE_TYPES or info["collectionNo"] <= 0:
            continue
        instance = count(row, "id")
        levels = unlocked.get(servant_id, {}) | leveled.get(instance, {})
        card = BoxCard(
            number=info["collectionNo"], class_id=info["classId"], level=count(row, "lv"),
            np_level=count(row, "treasureDeviceLv1"),
            ascension=count(row, "limitCount"), skills=tuple(count(row, f"skillLv{i}") for i in range(1, 4)),
            append=tuple(levels.get(slot, 0) for slot in APPEND_SLOTS),
        )
        rank = (card.np_level, card.ascension, sum(card.skills), sum(card.append), card.level, -instance)
        current = selected.get(card.number)
        if current is None or rank > current[0]:
            selected[card.number] = rank, card

    return sorted(
        (card for _, card in selected.values()),
        key=lambda card: (CLASS_ORDER.get(card.class_id, len(CLASS_ORDER)), card.number),
    )
