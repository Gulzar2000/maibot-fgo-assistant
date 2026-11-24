"""从完整登录库存读取召唤资源，按北京时间生成每日观测。"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Tuple

from .capture_import import game_history_key
from .real_statistics import count, table

SUMMON_TICKET_ID = "4001"
# 对齐 Chaldea 的 Items.quartzFragmentId，兑换库存读取道具 16。
QUARTZ_FRAGMENT_ID = "16"
QUARTZ_HISTORY_VERSION = 2
CHINA_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class QuartzSnapshot:
    captured_at: int
    quartz: int
    tickets: int
    fragments: int

    @property
    def day(self) -> date:
        return datetime.fromtimestamp(self.captured_at, CHINA_TZ).date()

    @property
    def total(self) -> float:
        return self.quartz + 3 * self.tickets + self.fragments / 7

    def message(self) -> str:
        return f"圣晶石：{self.quartz} + 3 * {self.tickets} + 1/7 * {self.fragments} = {self.total:.1f}"


def read_quartz(root: dict) -> Tuple[str, QuartzSnapshot]:
    cache = root["cache"]
    # 只有完整背包表内缺席的道具才记为零，缺表或仅收到增量时不覆盖旧观测。
    inventory = table({"replaced": cache["replaced"]}, "userItem", "itemId")
    counts = {str(row["itemId"]): count(row, "num") for row in inventory
              if str(row["itemId"]) in (SUMMON_TICKET_ID, QUARTZ_FRAGMENT_ID)}
    game_key = game_history_key(root)
    game = table(cache, "userGame", "userId")[0]
    # 部分国服登录响应只提供付费与免费分项，stone 有值时已经是总数。
    total_stone = count(game, "stone") if "stone" in game else 0
    return game_key, QuartzSnapshot(
        captured_at=count(cache, "serverTime"),
        quartz=total_stone or count(game, "freeStone") + count(game, "chargeStone"),
        tickets=counts.get(SUMMON_TICKET_ID, 0),
        fragments=counts.get(QUARTZ_FRAGMENT_ID, 0),
    )


def merge_quartz_snapshot(daily: Dict[str, dict], snapshot: QuartzSnapshot) -> bool:
    day = snapshot.day.isoformat()
    previous = daily.get(day)
    # 旧版读取了错误的道具 ID。允许有效原始抓包修复同一天，随后再按时间取最新。
    if previous is not None and previous.get("version") == QUARTZ_HISTORY_VERSION:
        if previous["captured_at"] > snapshot.captured_at:
            return False
    record = {**asdict(snapshot), "version": QUARTZ_HISTORY_VERSION}
    if previous == record:
        return False
    daily[day] = record
    return True


def quartz_points(daily: Dict[str, dict]) -> List[QuartzSnapshot]:
    return [QuartzSnapshot(**{field: value for field, value in row.items() if field != "version"})
            for _, row in sorted(daily.items()) if row.get("version") == QUARTZ_HISTORY_VERSION]
