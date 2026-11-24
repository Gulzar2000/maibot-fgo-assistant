"""账号养成规划：沿用 Chaldea 的分级费用和星图解锁／点亮口径。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional
import hashlib
import json
import re
import time

from .credentials import atomic_write, json_bytes
from .downloader import _fetch_bytes_sync
from .events import CHINA_TZ
from .materials import DATA_ROOT, cache_expiry
from .real_statistics import count, table

BOARD_URL = "https://api.atlasacademy.io/export/CN/nice_class_board.json"
PLAN_KEYS = {"servants", "items", "mappingData"}
TARGETS = ("全满破", "满破＋全星图", "满破＋全星图＋五星999", "满破＋全星图＋全员999")


class PlanDataError(ValueError):
    """可以展示给用户的规划数据错误。"""


def _board_costs(entries: List[dict]) -> List[dict]:
    return [{"itemId": row["item"]["id"], "amount": row["amount"]} for row in entries]


class PlanStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = Lock()
        self._loaded: Optional[dict] = None

    def load(self, refresh: bool = False) -> dict:
        with self._lock:
            path = self.directory / "plan_metadata_cn.json"
            data = self._loaded
            if data is None and path.exists():
                data = json.loads(path.read_bytes())
            if (not refresh and data is not None and data["schema"] == 2
                    and time.time() < cache_expiry(data["fetched_at"])):
                self._loaded = data
                return data
            version = json.loads(_fetch_bytes_sync(DATA_ROOT + "version.json"))
            files = [row for row in version["files"].values() if row["key"] in PLAN_KEYS]
            if {row["key"] for row in files} != PLAN_KEYS:
                raise PlanDataError("Chaldea 清单缺少养成规划数据。")

            def read(entry: dict) -> tuple:
                name = entry["filename"]
                if re.fullmatch(r"[A-Za-z0-9.]+\.json", name) is None:
                    raise PlanDataError("Chaldea 数据文件名无效。")
                # 两个查询有各自的清单和缓存周期，原始分片必须隔离，不能覆盖素材查询的旧版文件。
                target = self.directory / "planning_data" / name
                raw = target.read_bytes() if target.exists() else b""

                def valid(value: bytes) -> bool:
                    return (len(value) == entry["size"]
                            and hashlib.md5(value).hexdigest().startswith(entry["hash"]))

                if not valid(raw):
                    raw = _fetch_bytes_sync(DATA_ROOT + name)
                    if not valid(raw):
                        raise PlanDataError(f"Chaldea 文件校验失败：{name}")
                    atomic_write(target, raw)
                return entry["key"], json.loads(raw)

            bundle = {"servants": [], "items": [], "mappingData": {}}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for key, value in pool.map(read, files):
                    if isinstance(value, list):
                        bundle[key].extend(value)
                    else:
                        bundle[key].update(value)
            # Chaldea 分发包含日服未来内容；星图使用国服主表，不能用日服列表代替。
            boards = json.loads(_fetch_bytes_sync(BOARD_URL))
            board_rows = []
            for board in boards:
                squares = []
                for square in board["squares"]:
                    lock = square.get("lock")
                    squares.append({"id": square["id"], "flags": square.get("flags", []),
                                    "items": _board_costs(square["items"]),
                                    "lock": None if lock is None else {"items": _board_costs(lock["items"])}})
                board_rows.append({"id": board["id"], "squares": squares,
                                   "parentClassBoardBaseId": board["parentClassBoardBaseId"]})
            servants = {}
            for row in bundle["servants"]:
                info = {key: row[key] for key in
                        ("id", "collectionNo", "name", "rarity", "ascensionMaterials", "skillMaterials")}
                # Chaldea 精简 JSON 省略默认值，BasicServant.type 的默认值为 normal。
                info["type"] = row.get("type", "normal")
                info["appendSkillMaterials"] = row.get("appendSkillMaterials", {})
                # 灵衣进度使用costumeId，费用表使用battleCharaId，预先对齐两个索引。
                costume_materials = row.get("costumeMaterials", {})
                info["costumeCosts"] = {str(costume["id"]): costume_materials.get(str(costume["battleCharaId"]),
                                                     {"items": [], "qp": 0})
                                        for costume in row.get("profile", {}).get("costume", {}).values()}
                servants[str(row["id"])] = info
            if len(servants) < 300 or len(board_rows) < 9:
                raise PlanDataError("养成公共数据不完整。")
            data = {"schema": 2, "fetched_at": time.time(), "timestamp": version["timestamp"],
                    "servants": servants, "boards": board_rows,
                    "items": {str(row["id"]): row for row in bundle["items"]},
                    "item_names": bundle["mappingData"]["item_names"]}
            atomic_write(path, json_bytes(data))
            self._loaded = data
            return data


def build_plan_report(root: dict, metadata: dict, previous: Optional[dict] = None, *, include_grand: bool = False) -> dict:
    """每种实际持有从者规划一张；重复卡取同一实体的完整进度，避免拼出不存在的999。"""
    if not any(row.get("nid") == "login" and str(row.get("resCode")) in ("0", "00")
               and not row.get("fail") for row in root["response"]):
        raise PlanDataError("未取得成功的游戏登录数据。")
    cache = root["cache"]
    games = table(cache, "userGame", "userId")
    if len(games) != 1:
        raise PlanDataError("游戏角色数量无效。")
    game = games[0]
    inventory = {count(row, "itemId"): count(row, "num") for row in table(cache, "userItem", "itemId")}
    inventory[1] = count(game, "qp")
    held: Dict[int, List[dict]] = {}
    snapshot_cards: Dict[str, dict] = {}
    append_levels = {}
    for row in table(cache, "userSvtAppendPassiveSkillLv", "userSvtId", required=False):
        numbers, levels = row["appendPassiveSkillNums"], row["appendPassiveSkillLvs"]
        if len(numbers) != len(levels):
            raise PlanDataError("追加技能编号与等级数量不一致。")
        values = {str(int(number)): int(level) for number, level in zip(numbers, levels)}
        if any(not 0 <= level <= 10 for level in values.values()):
            raise PlanDataError("追加技能等级无效。")
        append_levels[str(count(row, "userSvtId"))] = values
    # 背包与第二保管室均为当前持有；契约图鉴包含已出售从者，不用它推断持有。
    for row in table(cache, "userSvt", "id") + table(cache, "userSvtStorage", "id"):
        info = metadata["servants"].get(str(count(row, "svtId")))
        if info is None or info["collectionNo"] <= 0 or info["type"] not in ("normal", "heroine"):
            continue
        ascension = min(4, count(row, "limitCount"))
        skills = [count(row, f"skillLv{i}") for i in range(1, 4)]
        if any(not 1 <= level <= 10 for level in skills):
            raise PlanDataError(f"从者 {info['id']} 的技能等级无效。")
        instance = str(count(row, "id"))
        snapshot_cards[instance] = {"svt_id": info["id"], "ascension": ascension, "skills": skills,
                                    "append": append_levels.get(instance, {})}
        held.setdefault(info["collectionNo"], []).append({"info": info, "ascension": ascension, "skills": skills})
    if not held:
        raise PlanDataError("账号没有可规划的持有从者。")
    asc_cost: Dict[int, int] = {}
    ssr_cost: Dict[int, int] = {}
    skill_cost: Dict[int, int] = {}

    def add_cost(target: Dict[int, int], entries: List[dict]) -> None:
        for entry in entries:
            item_id, amount = count(entry, "itemId"), count(entry, "amount")
            target[item_id] = target.get(item_id, 0) + amount

    def upgrade_cost(card: dict, with_skills: bool) -> Dict[int, int]:
        info = card["info"]
        result: Dict[int, int] = {}

        def levels(key: str, start: int, stop: int) -> None:
            costs = info[key]
            # 玛修的突破由剧情完成，公共数据为空；没有素材费用，但进度仍按实际突破次数。
            if key == "ascensionMaterials" and not costs and info["type"] == "heroine":
                return
            for level in range(start, stop):
                if str(level) not in costs:
                    raise PlanDataError(f"从者 {info['id']} 缺少 {key} 等级 {level} 的费用。")
                cost = costs[str(level)]
                add_cost(result, cost["items"])
                result[1] = result.get(1, 0) + count(cost, "qp")

        levels("ascensionMaterials", card["ascension"], 4)
        if with_skills:
            for level in card["skills"]:
                levels("skillMaterials", level, 9)
        return result

    maxed = trained = ssr_total = ssr_trained = duplicates = skill_steps = 0
    for cards in held.values():
        # 每个目标选剩余QP最少的一张，保证所有材料与进度来自同一张真实卡。
        # 999与突破的联合目标重新选择，不能将不同副本的各项最高等级拼起来。
        duplicates += len(cards) - 1
        maxed += any(card["ascension"] == 4 for card in cards)
        trained += any(all(level >= 9 for level in card["skills"]) for card in cards)
        skill_steps += max(sum(min(9, level) - 1 for level in card["skills"]) for card in cards)
        rarity = cards[0]["info"]["rarity"]
        ssr_total += rarity == 5
        ssr_trained += rarity == 5 and any(all(level >= 9 for level in card["skills"]) for card in cards)
        base = min((upgrade_cost(card, False) for card in cards), key=lambda cost: cost.get(1, 0))
        full = min((upgrade_cost(card, True) for card in cards), key=lambda cost: cost.get(1, 0))
        for item_id, amount in base.items():
            asc_cost[item_id] = asc_cost.get(item_id, 0) + amount
        # 记录联合费用相对基础费用的差额，四档各自只扣一次库存。
        for item_id in base.keys() | full.keys():
            delta = full.get(item_id, 0) - base.get(item_id, 0)
            skill_cost[item_id] = skill_cost.get(item_id, 0) + delta
            if rarity == 5:
                ssr_cost[item_id] = ssr_cost.get(item_id, 0) + delta
    # 独立保留“仅全满破”一档；星图费用从第二档开始叠加。
    ascension_only = dict(asc_cost)
    progress = {count(row, "classBoardBaseId"): row for row in
                table(cache, "userClassBoardSquare", "classBoardBaseId")}
    snapshot_boards = {str(ident): {"enhanced": [int(value) for value in row["classBoardSquareIds"]],
                                  "unlocked": [int(value) for value in row["classBoardUnlockSquareIds"]]}
                       for ident, row in progress.items()}
    boards = [board for board in metadata["boards"]
              if include_grand or not board.get("parentClassBoardBaseId", 0)]
    total_nodes = lit_nodes = lock_total = unlocked = 0
    for board in boards:
        state = progress.get(board["id"])
        enhanced = set(int(value) for value in state["classBoardSquareIds"]) if state is not None else set()
        released = set(int(value) for value in state["classBoardUnlockSquareIds"]) if state is not None else set()
        for square in board["squares"]:
            if "blank" in square.get("flags", []):
                continue
            # Chaldea item_center 对解锁和强化分别累计，两者不能互相代替。
            if square["lock"] is not None:
                lock_total += 1
                unlocked += square["id"] in released
                if square["id"] not in released:
                    add_cost(asc_cost, square["lock"]["items"])
            else:
                total_nodes += 1
                lit_nodes += square["id"] in enhanced
            if square["id"] not in enhanced:
                add_cost(asc_cost, square["items"])
    needs = [ascension_only, asc_cost, {i: asc_cost.get(i, 0) + ssr_cost.get(i, 0) for i in asc_cost.keys() | ssr_cost.keys()},
             {i: asc_cost.get(i, 0) + skill_cost.get(i, 0) for i in asc_cost.keys() | skill_cost.keys()}]
    rows = []
    for item_id in sorted(set().union(*(cost.keys() for cost in needs))):
        item = metadata["items"].get(str(item_id))
        if item is None:
            raise PlanDataError(f"公共数据缺少素材 {item_id}。")
        name = metadata["item_names"].get(item["name"], {}).get("CN", item["name"])
        if item_id == 6564:
            name = "创生的珍珠"
        stock = inventory.get(item_id, 0)
        required = [cost.get(item_id, 0) for cost in needs]
        missing = [max(0, amount - stock) for amount in required]
        rows.append({"id": item_id, "name": name, "stock": stock, "required": required,
                     "missing": missing, "icon": item["icon"]})
    captured = datetime.fromtimestamp(count(cache, "serverTime"), CHINA_TZ)
    costumes = {str(count(row, "svtId")): [int(value) for value in row.get("costumeIds", [])]
                for row in table(cache, "userSvtCollection", "svtId", required=False)
                if str(count(row, "svtId")) in metadata["servants"]}
    snapshot = {"schema": 1, "account": str(count(game, "userId")), "captured_at": captured.isoformat(),
                "inventory": {str(ident): amount for ident, amount in inventory.items()},
                "cards": snapshot_cards, "boards": snapshot_boards, "costumes": costumes,
                "progress": {"lit": lit_nodes, "maxed": maxed, "skill_steps": skill_steps},
                "include_grand": include_grand}
    comparison = compare_plan_snapshots(snapshot, previous, metadata)
    for row in rows:
        row["gained"] = comparison["gains"].get(str(row["id"]), 0) if comparison is not None else None
    return {"account": str(count(game, "userId")), "name": game["name"],
            "captured_at": captured.isoformat(), "updated": metadata["timestamp"],
            "include_grand": include_grand, "board_count": len(boards), "targets": TARGETS,
            "total": len(held), "maxed": maxed, "trained": trained,
            "skill_steps": skill_steps, "skill_total": len(held) * 24,
            "ssr_total": ssr_total, "ssr_trained": ssr_trained, "duplicates": duplicates,
            "nodes": total_nodes, "lit": lit_nodes, "locks": lock_total, "unlocked": unlocked,
            "items": rows, "missing_types": [sum(row["missing"][i] > 0 for row in rows) for i in range(len(TARGETS))],
            "snapshot": snapshot, "comparison": comparison}


def compare_plan_snapshots(current: dict, previous: Optional[dict], metadata: dict) -> Optional[dict]:
    """按真实卡片和星图节点还原期间花掉的材料，不能用总剩余需求差推算获得量。"""
    if previous is None:
        return None
    if previous["schema"] != 1 or previous["account"] != current["account"]:
        raise PlanDataError("规划快照格式或账号不一致，已停止对比。")
    if previous["include_grand"] != current["include_grand"]:
        raise PlanDataError("规划快照的星图范围不一致，已停止对比。")
    consumed: Dict[str, int] = {}

    def add(entries: List[dict], qp: int = 0) -> None:
        for entry in entries:
            key = str(entry["itemId"])
            consumed[key] = consumed.get(key, 0) + count(entry, "amount")
        consumed["1"] = consumed.get("1", 0) + qp

    for instance, card in current["cards"].items():
        before = previous["cards"].get(instance)
        if before is not None and before["svt_id"] != card["svt_id"]:
            raise PlanDataError("规划快照中的卡片身份冲突。")
        info = metadata["servants"][str(card["svt_id"])]
        old_ascension = before["ascension"] if before is not None else 0
        old_skills = before["skills"] if before is not None else [1, 1, 1]
        changes = [("ascensionMaterials", old_ascension, card["ascension"])]
        changes.extend(("skillMaterials", old, new) for old, new in zip(old_skills, card["skills"]))
        old_append = before["append"] if before is not None else {}
        changes.extend(("appendSkillMaterials", max(1, old_append.get(number, 1)), level)
                       for number, level in card["append"].items())
        for kind, start, stop in changes:
            if kind == "ascensionMaterials" and not info[kind] and info["type"] == "heroine":
                continue
            # 回退不产生负消耗；技能9→10虽不增加999进度，其实际材料费用仍计入。
            for level in range(start, stop):
                if str(level) not in info[kind]:
                    raise PlanDataError(f"从者 {info['id']} 缺少 {kind} 等级 {level} 的费用。")
                cost = info[kind][str(level)]
                add(cost["items"], count(cost, "qp"))
    for ident, costumes in current["costumes"].items():
        new_costumes = set(costumes) - set(previous["costumes"].get(ident, []))
        for costume in new_costumes:
            # 负数ID是已解锁标记，不代表已制作，不能按绝对值累计制作费用。
            if costume < 0:
                continue
            costs = metadata["servants"][ident]["costumeCosts"]
            if str(costume) not in costs:
                # 公共数据未收录的灵衣按用户要求省略这项费用，不中断整份规划。
                continue
            cost = costs[str(costume)]
            add(cost["items"], count(cost, "qp"))
    # 素材可能也花在冠位星图：缺口不包含冠位，但增量还原要识别其真实消耗。
    for board in metadata["boards"]:
        state = current["boards"].get(str(board["id"]), {"enhanced": [], "unlocked": []})
        before = previous["boards"].get(str(board["id"]), {"enhanced": [], "unlocked": []})
        enhanced = set(state["enhanced"]) - set(before["enhanced"])
        unlocked = set(state["unlocked"]) - set(before["unlocked"])
        for square in board["squares"]:
            if "blank" in square.get("flags", []):
                continue
            if square["lock"] is not None and square["id"] in unlocked:
                add(square["lock"]["items"])
            if square["id"] in enhanced:
                add(square["items"])
    gains = {}
    for ident in current["inventory"].keys() | previous["inventory"].keys() | consumed.keys():
        delta = current["inventory"].get(ident, 0) - previous["inventory"].get(ident, 0)
        gains[ident] = max(0, delta + consumed.get(ident, 0))
    return {"since": previous["captured_at"], "gains": gains, "consumed": consumed,
            "progress": {key: max(0, value - previous["progress"][key])
                         for key, value in current["progress"].items()}}
