"""卡池缓存管理与配置持久化"""
from datetime import datetime, timedelta, timezone
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Dict, List, Optional, Tuple
import json
import math
import os
import time

from .engine import PityCounter, simulate_hundred_pull, simulate_ten_pull
from .statistics import validate_draw_statistics

DAILY_RESET_TZ = timezone(timedelta(hours=8))


class DrawLimitError(Exception):
    """每日抽数不足或仍在冷却中，不进行抽取或修改已保存的状态。"""


def normalize_qq_user_id(user_id: str) -> str:
    """只能使用命令上下文中的实际 QQ 号，不能以群聊或会话编号代替。"""
    number = str(user_id).strip()
    if not number.isascii() or not number.isdigit() or int(number) <= 0:
        raise ValueError("无法读取有效的发送者 QQ 号")
    return str(int(number))


class PoolManager:
    """管理卡池缓存、用户配置和抽卡状态"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        # 所有用户共用一个文件，额度检查、抽取和计数写回必须作为同一临界区。
        self._pity_lock = Lock()
        os.makedirs(data_dir, exist_ok=True)

    def _save_json(self, path: str, value: object) -> None:
        """先完整写入同目录临时文件再原子替换，防止抽卡期间读到截断 JSON。"""
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_dir, delete=False) as file:
            temporary_path = file.name
            try:
                json.dump(value, file, indent=2, ensure_ascii=False)
            except Exception:
                file.close()
                os.remove(temporary_path)
                raise
        try:
            os.replace(temporary_path, path)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

    @property
    def pools_path(self) -> str:
        return os.path.join(self.data_dir, "pools.json")

    @property
    def gacha_data_path(self) -> str:
        return os.path.join(self.data_dir, "gacha_data.json")

    @property
    def banners_path(self) -> str:
        return os.path.join(self.data_dir, "banners.json")

    @property
    def pity_path(self) -> str:
        return os.path.join(self.data_dir, "pity_state.json")

    def _load_pity_states(self) -> Dict[str, dict]:
        if not os.path.exists(self.pity_path):
            return {}
        with open(self.pity_path, "r", encoding="utf-8") as file:
            states = json.load(file)
        if not isinstance(states, dict):
            raise ValueError("保底状态文件格式错误")
        return states

    def _pity_state_for_banner(self, previous: Optional[dict], banner: dict) -> dict:
        """按稳定链接和子池识别池；只保留当前池，切回旧池也从零开始。"""
        pool = {"server": banner["server"], "href": banner["href"], "s_id": banner.get("s_id", 0)}
        if previous is not None:
            if not isinstance(previous, dict):
                raise ValueError("用户保底状态格式错误")
            PityCounter(previous["misses"])
            if previous["pool"] == pool:
                return dict(previous)
        # 切池只清空保底，QQ 用户的当天用量与上次抽卡时间仍然保留。
        state = dict(previous) if previous is not None else {}
        state.update({"pool": pool, "misses": 0})
        return state

    def _check_and_update_draw_limits(
        self, state: dict, count: int, daily_limit: Optional[int], cooldown_seconds: int,
    ) -> None:
        """在状态副本上按北京时间自然日记账；抽取成功后才与保底一起保存。"""
        if daily_limit is not None and (type(daily_limit) is not int or daily_limit <= 0):
            raise ValueError("每日抽卡上限必须是正整数")
        if type(cooldown_seconds) is not int or cooldown_seconds < 0:
            raise ValueError("抽卡冷却时间必须是非负整数")
        now = time.time()
        today = datetime.fromtimestamp(now, DAILY_RESET_TZ).date().isoformat()
        # 旧版文件只有保底数据，第一次使用实际抽数记账时从零开始。
        daily_count = 0
        last_draw_at = None
        if "daily_date" in state:
            datetime.strptime(state["daily_date"], "%Y-%m-%d")
            stored_count = state["daily_count"]
            last_draw_at = state["last_draw_at"]
            if type(stored_count) is not int or stored_count < 0:
                raise ValueError("用户每日抽卡计数无效")
            if type(last_draw_at) not in (int, float) or not math.isfinite(last_draw_at) or last_draw_at < 0:
                raise ValueError("用户上次抽卡时间无效")
            if state["daily_date"] == today:
                daily_count = stored_count
        # 先检查本次全部抽数是否有额度；两个限制同时触发时优先提示每日上限。
        if daily_limit is not None and daily_count + count > daily_limit:
            raise DrawLimitError(f"已达到每日抽卡上限（{daily_limit}次），别太贪心哦~")
        if last_draw_at is not None and now - last_draw_at < cooldown_seconds:
            raise DrawLimitError("抽卡还在冷却中，太频繁啦！")
        state.update({"daily_date": today, "daily_count": daily_count + count, "last_draw_at": now})

    def select_user_pool(self, user_id: str, stream_id: str, banner: dict) -> None:
        """切换会话卡池，并立即重置执行切换的 QQ 用户在不同池的计数。"""
        user_id = normalize_qq_user_id(user_id)
        with self._pity_lock:
            states = self._load_pity_states()
            states[user_id] = self._pity_state_for_banner(states.get(user_id), banner)
            self.set_stream_banner(stream_id, banner)
            self._save_json(self.pity_path, states)

    def _statistics_for_state(self, state: dict) -> Tuple[int, int]:
        """旧版未记录累计五星，统计从新增字段后的抽取开始，不推测历史结果。"""
        if "total_draws" not in state and "ssr_count" not in state:
            return 0, 0
        total_draws, ssr_count = state["total_draws"], state["ssr_count"]
        validate_draw_statistics(total_draws, ssr_count)
        return total_draws, ssr_count

    def get_user_statistics(self, user_id: str) -> Tuple[int, int]:
        """只读查询指定 QQ 的累计抽数与五星从者数量，不消耗额度或触发冷却。"""
        user_id = normalize_qq_user_id(user_id)
        with self._pity_lock:
            state = self._load_pity_states().get(user_id)
            if state is None:
                return 0, 0
            if not isinstance(state, dict):
                raise ValueError("用户抽卡状态格式错误")
            return self._statistics_for_state(state)

    def draw_for_user(
        self, user_id: str, banner: dict, gacha_data: dict, count: int,
        daily_limit: Optional[int] = None, cooldown_seconds: int = 0,
    ) -> Tuple[List[dict], bool]:
        """按 QQ 用户限制实际抽数、累计保底并持久化，并发请求不会超额或丢失计数。"""
        user_id = normalize_qq_user_id(user_id)
        if count not in (11, 110):
            raise ValueError("只支持十一连或百连（110 抽）")
        with self._pity_lock:
            states = self._load_pity_states()
            state = self._pity_state_for_banner(states.get(user_id), banner)
            self._check_and_update_draw_limits(state, count, daily_limit, cooldown_seconds)
            total_draws, ssr_count = self._statistics_for_state(state)
            pity = PityCounter(state["misses"])
            result = simulate_ten_pull(gacha_data, pity) if count == 11 else simulate_hundred_pull(gacha_data, pity)
            if count == 11:
                new_ssr_count = sum(card["type"] == "svt" and card["rarity"] in ("5", "up5") for card in result)
            else:
                new_ssr_count = sum(card["count"] for card in result if card["type"] in ("svt_5", "upsvt_5"))
            # 与保底和每日额度一起保存，切池、跨日和重启都不清空累计出率。
            state.update({"total_draws": total_draws + count, "ssr_count": ssr_count + new_ssr_count})
            state["misses"] = pity.misses
            states[user_id] = state
            self._save_json(self.pity_path, states)
            return result, pity.triggered > 0

    def load_pools(self) -> list[dict]:
        if not os.path.exists(self.pools_path):
            return []
        with open(self.pools_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_pools(self, pools: list[dict]) -> None:
        self._save_json(self.pools_path, pools)

    def load_gacha_data(self) -> list[dict]:
        if not os.path.exists(self.gacha_data_path):
            return []
        with open(self.gacha_data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_gacha_data(self, data: list[dict]) -> None:
        self._save_json(self.gacha_data_path, data)

    def load_banners(self) -> list[dict]:
        if not os.path.exists(self.banners_path):
            return []
        with open(self.banners_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_banners(self, banners: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.banners_path), exist_ok=True)
        self._save_json(self.banners_path, banners)

    def get_stream_banner(self, stream_id: str, follow_latest: bool = True) -> Optional[dict]:
        """获取指定stream的当前卡池配置"""
        banners = self.load_banners()
        for b in banners:
            if b.get("stream_id") == stream_id:
                return b.get("banner")
        pools = self.load_pools()
        if not pools:
            return None

        if follow_latest:
            default = next((p for p in pools if p["server"] == "国服" and p.get("available", False)), None)
        else:
            default = next((p for p in pools if p["title"] == "剧情召唤/国服"), None)
        if default is None:
            return None
        default = dict(default)
        if default["type"] == "daily pickup":
            default["s_id"] = default["sub_pool"][0]["id"]

        banners.append({
            "stream_id": stream_id,
            "banner": default,
        })
        self.save_banners(banners)
        return default

    def set_stream_banner(self, stream_id: str, banner: dict) -> None:
        banners = self.load_banners()
        found = False
        for b in banners:
            if b.get("stream_id") == stream_id:
                b["banner"] = banner
                found = True
                break
        if not found:
            banners.append({
                "stream_id": stream_id,
                "banner": banner,
            })
        self.save_banners(banners)

    def save_pool_snapshot(self, pools: List[dict], data: List[dict], default: dict) -> None:
        """重新编号后按原链接和子池编号保留选择，避免一次查询重置所有会话。"""
        banners = self.load_banners()
        by_url = {pool["href"]: pool for pool in pools if pool["available"]}
        for entry in banners:
            previous = entry["banner"]
            updated = by_url.get(previous["href"])
            if updated is None:
                entry["banner"] = dict(default)
                continue
            selected = dict(updated)
            if selected["type"] == "daily pickup":
                child_ids = {child["id"] for child in selected["sub_pool"]}
                previous_id = previous.get("s_id", 0)
                selected["s_id"] = previous_id if previous_id in child_ids else selected["sub_pool"][0]["id"]
            entry["banner"] = selected
        self.save_gacha_data(data)
        self.save_pools(pools)
        self.save_banners(banners)

    def reset_all_banners(self, new_default: dict) -> None:
        banners = self.load_banners()
        for b in banners:
            b["banner"] = new_default
        self.save_banners(banners)

    def get_gacha_data_for_banner(self, banner: dict) -> Optional[dict]:
        """根据banner配置获取对应的抽卡概率数据"""
        gacha_data = self.load_gacha_data()
        for data in gacha_data:
            # 使用页面链接而非可变的显示编号，刷新过程中旧选择仍对应同一个池。
            if data.get("data_version") == 4 and data["pool_href"] == banner["href"]:
                if "s_id" in data:
                    if data.get("s_id") == banner.get("s_id", 0):
                        return data
                else:
                    return data
        return None

    @property
    def card_names_path(self) -> str:
        return os.path.join(self.data_dir, "card_names.json")

    def load_card_names(self) -> dict[str, dict[str, str]]:
        if not os.path.exists(self.card_names_path):
            return {"svt": {}, "ce": {}}
        with open(self.card_names_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_card_names(self, names: dict[str, dict[str, str]]) -> None:
        self._save_json(self.card_names_path, names)
