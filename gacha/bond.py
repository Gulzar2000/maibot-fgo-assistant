"""国服羁绊挖矿：公共从者数据、逐从者收益及快照对比。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
import gzip
import json
import time

from .credentials import atomic_write, json_bytes
from .downloader import _fetch_bytes_sync
from .materials import cache_expiry
from .real_statistics import count, table

DATA_URL = "https://api.atlasacademy.io/export/CN/nice_servant.json"
ENTITY_URL = "https://api.atlasacademy.io/repo/CN/master/mstSvt.json"
# 国服普通羁绊奖励。安哥拉曼纽实际为 6~9 绊各 2 石，不能套用一星。
REWARDS = {0: {6: 2, 7: 2, 8: 2, 9: 2}, 1: {9: 1}, 2: {8: 1, 9: 1},
           3: {6: 1, 7: 1, 8: 1, 9: 1}, 4: {6: 2, 7: 2, 8: 2, 9: 2},
           5: {6: 3, 7: 3, 8: 3, 9: 3}}


class BondDataError(ValueError):
    """可以安全展示给用户的数据错误，不包含登录原文。"""


def add_noncollection_entities(servants: Dict[str, Any], entities: List[dict]) -> None:
    """完整主表覆盖宝具演出等无图鉴实体；可养成图鉴仍必须具有完整羁绊数据。"""
    for row in entities:
        ident = str(count(row, "id"))
        if count(row, "collectionNo") == 0 and ident not in servants:
            servants[ident] = {"excluded": True}


def download_metadata() -> dict:
    """批量取国服从者，压缩传输后仅缓存统计所需字段。"""
    request = Request(DATA_URL, headers={"Accept-Encoding": "gzip", "User-Agent": "MaiBot-FGOGacha/1.0"})
    with urlopen(request, timeout=90) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    source = json.loads(raw)
    servants = {}
    for row in source:
        ident = str(row["id"])
        if row["collectionNo"] <= 0 or row["type"] not in ("normal", "heroine"):
            servants[ident] = {"excluded": True}
            continue
        servants[ident] = {"id": row["id"], "no": row["collectionNo"], "name": row["name"],
                           "rarity": row["rarity"], "growth": row["bondGrowth"],
                           "excluded": row["type"] == "heroine"}
    # 礼装也可能出现在图鉴表中；必须按真实索引排除，不能按 ID 范围猜测。
    for row in json.loads(_fetch_bytes_sync("https://api.atlasacademy.io/export/CN/basic_equip.json")):
        servants[str(row["id"])] = {"excluded": True}
    add_noncollection_entities(servants, json.loads(_fetch_bytes_sync(ENTITY_URL)))
    if len([row for row in servants.values() if not row["excluded"]]) < 300:
        raise BondDataError("国服羁绊数据不完整，请联系管理员更新。")
    return {"schema": 2, "region": "CN", "fetched_at": time.time(), "servants": servants}


class BondStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def load(self, refresh: bool = False) -> dict:
        with self._lock:
            data = json.loads(self.path.read_bytes()) if self.path.exists() else None
            if data is not None:
                if data["schema"] not in (1, 2) or data["region"] != "CN":
                    raise BondDataError("羁绊缓存格式无效，请联系管理员。")
                if data["schema"] == 2 and not refresh and time.time() < cache_expiry(data["fetched_at"]):
                    return data
            data = download_metadata()
            atomic_write(self.path, json_bytes(data))
            return data


def rewards(rarity: int) -> Dict[int, int]:
    if rarity not in REWARDS:
        raise BondDataError("出现未知星级，请联系管理员更新羁绊数据。")
    return dict(REWARDS[rarity]) | {level: 30 for level in range(11, 16)}


def build_bond_report(root: dict, metadata: dict, previous: Optional[dict] = None) -> dict:
    """只以契约图鉴计算；重复卡片不增加奖励，同图鉴形态只计一次。"""
    if not any(row.get("nid") == "login" and str(row.get("resCode")) in ("0", "00")
               and not row.get("fail") for row in root["response"]):
        raise BondDataError("未取得成功的游戏登录数据。")
    games = table(root["cache"], "userGame", "userId")
    if len(games) != 1:
        raise BondDataError("游戏角色数量无效。")
    game = games[0]
    account = str(count(game, "userId"))
    inventory = table(root["cache"], "userItem", "itemId")
    dreamfire_inventory = sum(count(item, "num") for item in inventory if count(item, "itemId") == 1000)
    if previous is not None and previous["account"] != account:
        raise BondDataError("羁绊快照账号不一致，已停止对比。")
    servants: Dict[str, Any] = {}
    excluded = 0
    for row in table(root["cache"], "userSvtCollection", "svtId"):
        if count(row, "status") != 2:
            continue
        ident = str(count(row, "svtId"))
        if ident not in metadata["servants"]:
            raise BondDataError(f"羁绊数据缺少图鉴 ID {ident}，请联系管理员更新。")
        info = metadata["servants"][ident]
        if info["excluded"]:
            excluded += 1
            continue
        rank = count(row, "friendshipRank")
        cap = 10 + count(row, "friendshipExceedCount")
        exp = count(row, "friendship")
        if not 0 <= rank <= cap <= 15:
            raise BondDataError(f"从者 {ident} 的羁绊等级或上限无效。")
        growth = info["growth"]
        if len(growth) < cap or (rank < cap and exp > growth[rank]):
            raise BondDataError(f"从者 {ident} 的羁绊经验与数据表不一致。")
        schedule = rewards(info["rarity"])
        normal = sum(value for level, value in schedule.items() if rank < level <= 9)
        dream = max(0, cap - max(10, rank)) * 30
        earned = sum(value for level, value in schedule.items() if level <= rank)
        current = {"key": str(info["no"]), "name": info["name"], "rarity": info["rarity"],
                   "rank": rank, "cap": cap, "exp": exp,
                   "next": max(0, growth[rank] - exp) if rank < cap else None,
                   "normal": normal, "dream": dream, "earned": earned,
                   "remaining": normal + dream}
        key = current["key"]
        if key in servants:
            # 相同图鉴的不同形态必须共享进度；冲突不能靠取最大值掩盖。
            old = servants[key]
            if any(old[field] != current[field] for field in ("rank", "cap", "exp", "rarity")):
                raise BondDataError(f"图鉴 {key} 的共享羁绊进度冲突。")
        else:
            servants[key] = current
    rows = list(servants.values())
    captured = datetime.fromtimestamp(count(root["cache"], "serverTime"), timezone(timedelta(hours=8)))
    report = {"account": account, "name": game["name"], "captured_at": captured.isoformat(),
              "servants": rows, "excluded": excluded,
              "normal": sum(row["normal"] for row in rows), "dream": sum(row["dream"] for row in rows),
              "earned": sum(row["earned"] for row in rows),
              "dreamfire_used": sum(row["cap"] - 10 for row in rows),
              "dreamfire_rewarded": sum(max(0, row["rank"] - 10) for row in rows),
              "dreamfire_pending": sum(max(0, row["cap"] - max(10, row["rank"])) for row in rows),
              "dreamfire_inventory": dreamfire_inventory}
    # 只按背包真实数量估算额外潜力；投入空间只用于防止超出所有从者 15 绊上限。
    available_slots = sum(15 - row["cap"] for row in rows)
    report["dreamfire_usable"] = min(dreamfire_inventory, available_slots)
    report["inventory_quartz"] = report["dreamfire_usable"] * 30
    report["remaining"] = report["normal"] + report["dream"]
    report["potential"] = report["remaining"] + report["inventory_quartz"]
    report["comparison"] = compare_snapshots(report, previous)
    return report


def compare_snapshots(current: dict, previous: Optional[dict]) -> Optional[dict]:
    if previous is None:
        return None
    old = {row["key"]: row for row in previous["servants"]}
    new = {row["key"]: row for row in current["servants"]}
    changes = []
    gained = unlocked = 0
    added = 0
    for key, row in new.items():
        if key not in old:
            added += 1
            changes.append(f"新增：{row['name']}（{row['rank']}/{row['cap']} 绊）")
            continue
        before = old[key]
        if row["rank"] < before["rank"] or row["cap"] < before["cap"]:
            raise BondDataError("同一角色羁绊进度发生回退，已停止对比，请管理员检查快照。")
        schedule = rewards(row["rarity"])
        gained += sum(value for level, value in schedule.items() if before["rank"] < level <= row["rank"])
        unlocked += (row["cap"] - before["cap"]) * 30
        if row["rank"] != before["rank"] or row["cap"] != before["cap"]:
            changes.append(f"{row['name']}：{before['rank']}/{before['cap']} → {row['rank']}/{row['cap']} 绊")
    return {"since": previous["captured_at"], "gained": gained, "unlocked": unlocked,
            "added": added, "missing": len(old.keys() - new.keys()), "changes": changes,
            "delta": current["remaining"] - previous["remaining"]}
