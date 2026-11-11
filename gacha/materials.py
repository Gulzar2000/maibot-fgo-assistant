"""国服素材来源查询：Chaldea 数据包、Mooncell 排期及每日交换券。"""
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import unquote
from PIL import Image
import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata

from .downloader import _fetch_bytes_sync
from .events import CHINA_TZ, fetch_events

DATA_ROOT = "https://raw.githubusercontent.com/chaldea-center/chaldea-data/main/dist/"
DATA_KEYS = {"items", "events", "wars", "dropData", "questPhaseDetails", "wiki.events",
             "mappingData", "exchangeTickets", "constData"}


def cache_expiry(timestamp: float) -> float:
    """两个自然月后到期，月末按目标月份最后一天截取。"""
    start = datetime.fromtimestamp(timestamp, CHINA_TZ)
    year, month0 = divmod(start.year * 12 + start.month - 1 + 2, 12)
    month = month0 + 1
    return start.replace(year=year, month=month, day=min(start.day, monthrange(year, month)[1])).timestamp()


class MaterialQueryError(ValueError):
    """可直接返回给查询者的参数错误。"""


def normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def page_key(text: str) -> str:
    return normalize(unquote(text).split("/w/")[-1].replace("_", " "))


def allowed_material(item_id: int) -> bool:
    # 普通铜银金素材、七职阶技能石及银/金棋；排除传承结晶和活动道具。
    return (6500 < item_id < 6600 or any(base < item_id <= base + 7
            for base in (6000, 6100, 6200, 7000, 7100)))


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(data)
        temporary = Path(file.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MaterialStore:
    """按清单哈希复用本地文件；更新失败完整报错，不把旧数据伪装成最新。"""

    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = Lock()
        self._loaded: Optional[MaterialData] = None
        self.expires_at = 0.0
        self.fetched_at = 0.0

    def load(self) -> "MaterialData":
        with self._lock:
            if self._loaded is not None and time.time() < self.expires_at:
                return self._loaded
            marker_path = self.directory / "data_cache.json"
            marker = json.loads(marker_path.read_bytes()) if marker_path.exists() else {}
            cached = time.time() < marker.get("expires_at", 0)
            version_bytes = ((self.directory / "version.json").read_bytes() if cached
                             else _fetch_bytes_sync(DATA_ROOT + "version.json"))
            version = json.loads(version_bytes)
            files = [entry for entry in version["files"].values() if entry["key"] in DATA_KEYS]
            if {entry["key"] for entry in files} != DATA_KEYS:
                raise ValueError("Chaldea 数据清单缺少素材查询所需文件")

            def read_file(entry: dict) -> tuple:
                name = entry["filename"]
                if re.fullmatch(r"[A-Za-z0-9.]+\.json", name) is None:
                    raise ValueError("Chaldea 数据文件名无效")
                path = self.directory / name
                data = path.read_bytes() if path.exists() else b""

                def valid(content: bytes) -> bool:
                    return (len(content) == entry["size"] and
                            hashlib.md5(content).hexdigest().startswith(entry["hash"]))

                if not valid(data):
                    data = _fetch_bytes_sync(DATA_ROOT + name)
                    if not valid(data):
                        raise ValueError(f"Chaldea 文件校验失败：{name}")
                    _write_bytes(path, data)
                return entry["key"], json.loads(data)

            bundle: Dict[str, Any] = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for key, value in pool.map(read_file, files):
                    if key not in bundle:
                        bundle[key] = value
                    elif isinstance(value, list):
                        bundle[key].extend(value)
                    else:
                        bundle[key].update(value)
            loaded = MaterialData(bundle, version["timestamp"])
            _write_bytes(self.directory / "version.json", version_bytes)
            if not cached:
                fetched_at = time.time()
                marker = {"fetched_at": fetched_at, "expires_at": cache_expiry(fetched_at)}
                _write_bytes(marker_path, json.dumps(marker).encode("utf-8"))
            self._loaded = loaded
            self.expires_at = marker["expires_at"]
            self.fetched_at = marker["fetched_at"]
            return loaded


class MaterialData:
    def __init__(self, bundle: Dict[str, Any], timestamp: int):
        self.bundle = bundle
        self.timestamp = timestamp
        self.mapping = bundle["mappingData"]
        self.all_items = {x["id"]: x for x in bundle["items"]}
        self.items = {x["id"]: x for x in bundle["items"] if allowed_material(x["id"])}
        self.wars = {x["id"]: x for x in bundle["wars"]}
        self.extras = {x["id"]: x for x in bundle["wiki.events"]}
        self.phases = {x["questId"] * 100 + x["phase"]: x for x in bundle["questPhaseDetails"]}
        self.quests: Dict[int, dict] = {}
        for war in self.wars.values():
            for spot in war.get("spots", []):
                for quest in spot.get("quests", []):
                    self.quests[quest["id"]] = dict(quest, spotName=spot["name"], warName=war["longName"])
        self.aliases: Dict[str, set] = {}
        self.alias_names: Dict[int, List[str]] = {}
        aliases = json.loads((Path(__file__).parents[1] / "res" / "material_aliases.json").read_bytes())
        for item_id, item in self.items.items():
            names = [self.item_name(item_id), str(item_id), *aliases.get(str(item_id), [])]
            if item_id // 100 in (60, 61, 62, 70, 71):
                job = ("剑", "弓", "枪", "骑", "术", "杀", "狂")[item_id % 100 - 1]
                # 玩家称呼采用颜色饼干、金银棋，不生成英文职阶和石头等猜测组合。
                suffixes = {60: ["蓝饼干"], 61: ["红饼干"], 62: ["金饼干"],
                            70: ["银棋", "银棋子"], 71: ["金棋", "金棋子"]}[item_id // 100]
                names.extend(job + suffix for suffix in suffixes)
            self.alias_names[item_id] = list(dict.fromkeys(names))
            for name in names:
                self.aliases.setdefault(normalize(name), set()).add(item_id)

    def translated(self, key: str, name: str) -> str:
        return self.mapping.get(key, {}).get(name, {}).get("CN", name)

    def item_name(self, item_id: int) -> str:
        # Chaldea 当前尚缺该中文映射；名称已核对 Mooncell，不把日文原名注册成查询词。
        if item_id == 6564:
            return "创生的珍珠"
        return self.translated("item_names", self.items[item_id]["name"])

    def resolve(self, keyword: str) -> int:
        key = normalize(keyword)
        matches = self.aliases.get(key, set())
        if len(matches) == 1:
            return next(iter(matches))
        if matches:
            names = "、".join(self.item_name(x) for x in sorted(matches))
            raise MaterialQueryError(f"“{keyword}”对应多个素材，请指定：{names}")
        raise MaterialQueryError(f"素材{keyword}不存在")

    def free_quests(self, item_id: int, today: date, limit: int = 5) -> List[dict]:
        sheet = self.bundle["dropData"]["domusAurea"]
        if item_id not in sheet["itemIds"]:
            return []
        row = sheet["sparseMatrix"].get(str(sheet["itemIds"].index(item_id)), {})
        released = self.mapping["war_release"].get("CN", [])
        quest_releases = self.mapping.get("quest_release", {})
        now = datetime.combine(today, datetime.max.time(), CHINA_TZ).timestamp()
        result = []
        for index, quest_id in enumerate(sheet["questIds"]):
            rate = row.get(str(index), 0) / 100
            quest = self.quests.get(quest_id)
            if rate <= 0 or quest is None or quest["warId"] not in released:
                continue
            if quest.get("afterClear") != "repeatLast" or quest.get("consume", 0) <= 0:
                continue
            if "forceToNoDrop" in quest.get("flags", []) or "dropFirstTimeOnly" in quest.get("flags", []):
                continue
            release = quest_releases.get(str(quest_id), {}).get("CN")
            if release is not None and release > now:
                continue
            ap = sheet["apCosts"][index]
            if ap <= 0:
                continue
            result.append({"quest_id": quest_id, "name": self.translated("quest_names", quest["name"]),
                           "spot": self.translated("spot_names", quest["spotName"]),
                           "war": self.translated("war_names", quest["warName"]),
                           "ap": ap, "rate": rate, "ap_per_item": ap / rate, "runs": sheet["runs"][index],
                           "costs": [f"{self.translated('item_names', self.all_items[c['itemId']]['name'])} ×{c['amount']}"
                                     for c in quest.get("consumeItem", [])]})
        return sorted(result, key=lambda x: (x["ap_per_item"], -x["runs"]))[:limit]

    def event_rewards(self, event: dict, item_id: int) -> dict:
        """移植 Event/War.calcItems 的固定收益口径，随机与无限收益单列。"""
        parts: Dict[str, int] = {}
        extra = self.extras.get(event["id"], {})

        def add(label: str, count: int) -> None:
            if count > 0:
                parts[label] = parts.get(label, 0) + count

        def gifts(values: List[dict]) -> int:
            return sum(x["num"] for x in values
                       if x.get("type", "item") == "item" and x["objectId"] == item_id)

        for shop in event.get("shop", []):
            limit = shop.get("limitNum", 0)
            kind = shop["purchaseType"]
            if kind == "setItem":
                for entry in shop.get("itemSet", []):
                    if entry["purchaseType"] == "item" and entry["targetId"] == item_id:
                        add("商店", entry.get("setNum", 1) * shop.get("setNum", 1) * limit)
            elif kind in {"item", "itemAsPresent", "gift"} and item_id in shop.get("targetIds", []):
                add("商店", shop.get("setNum", 1) * limit)
        add("点数", sum(gifts(x.get("gifts", [])) for x in event.get("rewards", [])))
        add("任务", sum(gifts(x.get("gifts", [])) for x in event.get("missions", [])
                      if x.get("type") != "random" and x.get("rewardType") == "gift"))
        add("爬塔", sum(gifts(r.get("gifts", [])) for t in event.get("towers", []) for r in t.get("rewards", [])))
        add("棋盘", sum(gifts(t.get("gifts", [])) for b in event.get("warBoards", [])
                      for s in b.get("stages", []) for q in s.get("squares", []) for t in q.get("treasures", [])))
        excluded = self.bundle["constData"].get("excludeRewardQuests", [])
        war_ids = event.get("warIds", [])
        if not war_ids:
            war_ids = [int(k) for k, v in self.bundle["constData"].get("extraWarEventMapping", {}).items()
                       if v == event["id"]]
        for war_id in war_ids:
            if war_id < 1000 or war_id not in self.wars:
                continue
            war = self.wars[war_id]
            quests = [q for s in war.get("spots", []) for q in s.get("quests", [])]
            quests.extend(s["quest"] for s in war.get("questSelections", []))
            for quest in quests:
                if (quest["id"] in excluded or quest.get("type") == "friendship"
                        or "branch" in quest.get("flags", [])):
                    continue
                if war_id == 9069 and any(c["type"] == "notQuestClearPhase" for c in quest.get("releaseConditions", [])):
                    continue
                if quest["id"] in (1000825, 3000540):
                    continue
                if war.get("parentWarId") == 8395 and any(x["itemId"] == 49 for x in quest.get("consumeItem", [])):
                    continue
                add("通关", gifts(quest.get("gifts", [])))
                add("通关", sum(gifts(p.get("gifts", [])) for p in quest.get("presents", [])))
                for phase in quest.get("phases", []):
                    key = quest["id"] * 100 + phase
                    add("通关", gifts(self.phases.get(key, {}).get("gifts", [])))
                    if quest.get("type") != "warBoard":
                        fixed = self.bundle["dropData"]["fixedDrops"].get(str(key), {})
                        add("固定掉落", fixed.get("items", {}).get(str(item_id), 0))
        repeat = []
        for lottery in event.get("lotteries", []):
            boxes = lottery.get("boxes", [])
            last = max((x.get("boxIndex", 0) for x in boxes), default=0)
            per_box = sum(gifts(x.get("gifts", [])) * x["maxNum"] for x in boxes
                          if x.get("boxIndex", 0) == last and not x.get("isRare", False))
            if not lottery.get("limited", False) and per_box:
                repeat.append(f"无限池：每池 {per_box} 个")
            else:
                add("有限池", sum(gifts(x.get("gifts", [])) * x["maxNum"] for x in boxes))
        for entry in extra.get("extraFixedItems", []):
            add("其他固定", entry["items"].get(str(item_id), 0))
        if any(str(item_id) in x.get("items", {}) for x in extra.get("extraItems", [])):
            repeat.append("其他额外获取")
        random_gifts = [g for box in event.get("treasureBoxes", []) for group in box.get("treasureBoxGifts", [])
                        for g in group.get("gifts", [])]
        random_gifts.extend(g for r in event.get("recipes", []) for group in r.get("recipeGifts", [])
                            for g in group.get("gifts", []))
        random_gifts.extend(g for r in event.get("digging", {}).get("rewards", []) for g in r.get("gifts", []))
        if gifts(random_gifts):
            repeat.append("宝箱／制作等额外获取，数量不固定")
        return {"total": sum(parts.values()), "parts": parts, "extra": repeat}

    def upcoming_events(self, item_id: int, schedule: List[dict], today: date, limit: int = 5) -> List[dict]:
        # 用国服排期页面匹配活动身份，不能把日服未给 CN 日期的旧活动全部当未来。
        by_page: Dict[str, List[dict]] = {}
        for event in self.bundle["events"]:
            extra = self.extras.get(event["id"], {})
            for title in (extra.get("mcLink", ""), self.translated("event_names", event["name"])):
                if title:
                    by_page.setdefault(page_key(title), []).append(event)
        result = []
        for row in sorted(schedule, key=lambda x: x["start_date"]):
            if row["end_date"] is not None and date.fromisoformat(row["end_date"]) < today:
                continue
            candidates = by_page.get(page_key(row["href"]), by_page.get(page_key(row["title"]), []))
            seen = set()
            combined = {"total": 0, "parts": {}, "extra": []}
            for event in candidates:
                if event["id"] in seen:
                    continue
                seen.add(event["id"])
                rewards = self.event_rewards(event, item_id)
                combined["total"] += rewards["total"]
                for key, count in rewards["parts"].items():
                    combined["parts"][key] = combined["parts"].get(key, 0) + count
                combined["extra"].extend(rewards["extra"])
            if combined["total"] or combined["extra"]:
                result.append(dict(row, **combined, status="当前" if date.fromisoformat(row["start_date"]) <= today else "未来"))
        return result[:limit]

    def tickets(self, item_id: int, today: date, limit: int = 3) -> List[dict]:
        result = []
        for ticket in self.bundle["exchangeTickets"]:
            items = ticket.get("replaced", {}).get("CN", ticket["items"])
            if item_id not in items:
                continue
            # 与 Chaldea ExchangeTicket.monthDiff 一致；不能直接把日服年月当国服。
            offset = 12 if ticket["year"] * 100 + ticket["month"] >= 202209 else 15
            months = ticket["year"] * 12 + ticket["month"] - 1 + offset
            year, month0 = divmod(months, 12)
            start = date(year, month0 + 1, 1)
            if start <= today:
                continue
            multiplier = ticket.get("multiplier", 1)
            result.append({"month": start.strftime("%Y-%m"), "items": [self.item_name(i) for i in items if i in self.items],
                           "daily": multiplier, "maximum": monthrange(start.year, start.month)[1] * multiplier})
        return sorted(result, key=lambda x: x["month"])[:limit]

    def report(self, keyword: str, schedule: List[dict], today: Optional[date] = None) -> dict:
        today = today if today is not None else datetime.now(CHINA_TZ).date()
        item_id = self.resolve(keyword)
        return {"item_id": item_id, "name": self.item_name(item_id), "date": today.isoformat(),
                "icon_url": self.items[item_id]["icon"],
                "updated": datetime.fromtimestamp(self.timestamp, CHINA_TZ).strftime("%Y-%m-%d %H:%M"),
                "free": self.free_quests(item_id, today), "events": self.upcoming_events(item_id, schedule, today),
                "tickets": self.tickets(item_id, today)}


class MaterialService:
    def __init__(self, directory: Path):
        self.store = MaterialStore(directory)
        self._lock = asyncio.Lock()
        self._schedule: List[dict] = []
        self._expires_at = 0.0

    def load_icon(self, item_id: int, url: str, refresh: bool = False) -> bytes:
        path = self.store.directory / "icons" / f"{item_id}.png"
        content = path.read_bytes() if path.exists() and not refresh else _fetch_bytes_sync(url)
        with Image.open(BytesIO(content)) as icon:
            icon.verify()
        if refresh or not path.exists():
            _write_bytes(path, content)
        return content

    def _prefetch_icons(self, data: MaterialData) -> None:
        # 所有支持的素材图标一次获取；只有整批成功才提交整套缓存的完成标记。
        def fetch(item: dict) -> bytes:
            path = self.store.directory / "icons" / f"{item['id']}.png"
            # 中断后只补齐当前批次未完成的图标；到期刷新时旧批次图标会重新获取。
            refresh = not path.exists() or path.stat().st_mtime < self.store.fetched_at
            return self.load_icon(item["id"], item["icon"], refresh=refresh)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, data.items.values()))

    async def _prepare_locked(self) -> MaterialData:
        data = await asyncio.to_thread(self.store.load)
        if time.time() < self._expires_at:
            return data
        path = self.store.directory / "snapshot.json"
        snapshot = await asyncio.to_thread(lambda: json.loads(path.read_bytes()) if path.exists() else {})
        if time.time() < snapshot.get("expires_at", 0) and snapshot["version"] == data.timestamp:
            self._schedule = snapshot["schedule"]
            self._expires_at = snapshot["expires_at"]
            return data
        schedule = await fetch_events()
        await asyncio.to_thread(self._prefetch_icons, data)
        snapshot = {"fetched_at": time.time(), "expires_at": self.store.expires_at,
                    "version": data.timestamp, "schedule": schedule, "icons": sorted(data.items)}
        await asyncio.to_thread(_write_bytes, path, json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        self._schedule = schedule
        self._expires_at = snapshot["expires_at"]
        return data

    async def prepare(self) -> MaterialData:
        async with self._lock:
            return await self._prepare_locked()

    async def query(self, keyword: str) -> dict:
        async with self._lock:
            data = await self._prepare_locked()
            data.resolve(keyword)
            report = await asyncio.to_thread(data.report, keyword, self._schedule)
            report["icon_bytes"] = await asyncio.to_thread(self.load_icon, report["item_id"], report["icon_url"])
            return report
