"""国服青铜树苗兑换，沿用 Capture Kit 的会话及购买协议。"""
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor
from uuid import uuid4
import hashlib
import time

from .account_protocol.errors import ExportError, check_maintenance
from .account_protocol.game_profile import GameProfile
from .account_protocol.protocol import GameClient
from .account_protocol.statistics import decode_response
from .real_statistics import calculate_ap, count

SAPLING_ID = "103"
FRUIT_ID = "104"
SHOP_ID = "13000000"
AP_COST = 40
ACTION_PATH = "/rongame_beta/rgfate/60_1001/ac.php?"


class SaplingClient(GameClient):
    def __init__(self, profile: GameProfile, server: str):
        super().__init__(profile, server)
        self._opener.add_handler(HTTPCookieProcessor())
        self._account_channel = "996" if server == "ios" else "24"
        self._account_platform = "2" if server == "ios" else "3"
        self._started = time.monotonic()
        self._session: Dict[str, Any] = {}
        self._inventory_cache: Dict[str, Any] = {}

    def _game(self, path: str, params: dict) -> Tuple[dict, dict]:
        params = dict(params, rkchannel=self._account_channel)
        root, result = super()._game(path, params)
        if path.startswith(ACTION_PATH) and params.get("key") == "toplogin":
            self._session = {
                key: value for key, value in params.items() if key not in {"nickname", "sgtype", "sgtag", "try"}
            }
            self._update_session(root["response"][0]["usk"])
            self._update_inventory(root["cache"])
        return root, result

    def _update_session(self, token: Optional[str]) -> None:
        if not isinstance(token, str) or not token:
            raise ExportError("GAME_SESSION_MISSING")
        self._session["usk"] = hashlib.md5((self.profile.session_prefix + token).encode()).hexdigest()

    def _update_inventory(self, cache: dict) -> None:
        # 完整表先替换，再把增量字段合并进对应行。
        for section in ("replaced", "updated"):
            tables = cache.get(section, {})
            for table, key in (("userGame", "userId"), ("userItem", "itemId")):
                if table not in tables:
                    continue
                if section == "replaced":
                    self._inventory_cache[table] = {}
                target = self._inventory_cache.setdefault(table, {})
                for row in tables[table]:
                    target.setdefault(str(row[key]), {}).update(row)
        self._inventory_cache["serverTime"] = cache["serverTime"]

    def _action(self, key: str, **fields: str) -> dict:
        query = urlencode({
            "_userId": self._session["userId"], "_key": key,
            "_clientLocalTime": f"{time.monotonic() - self._started:.5f}",
        })
        # 登录模拟的是安卓设备，所以 cPlat 固定为 3，uPlat 则跟随绑定区服。
        params = dict(self._session, key=key, cPlat="3", uPlat=self._account_platform,
                      lastAccessTime=str(int(time.time())), idempotencyKey=str(uuid4()), **fields)
        root = decode_response(self._request(self.game_url + ACTION_PATH + query, params))
        rows = root["response"]
        for row in rows:
            check_maintenance(row)
        nid = "home" if key == "home" else "purchase"
        matches = [row for row in rows if row.get("nid") == nid]
        if len(matches) != 1:
            raise ExportError("GAME_ACTION_RESPONSE_INVALID")
        row = matches[0]
        code = str(row.get("resCode"))
        if not (code.isascii() and code.isdecimal()):
            raise ExportError("GAME_ACTION_RESPONSE_INVALID")
        if code not in ("0", "00") or row.get("fail"):
            raise ExportError("GAME_ACTION_REJECTED_" + code)
        result = row.get("success")
        if not isinstance(result, dict):
            raise ExportError("GAME_ACTION_RESPONSE_INVALID")
        if key == "home":
            token = row.get("usk") or next((entry["usk"] for entry in rows if entry.get("usk")), None)
            self._update_session(token)
        self._update_inventory(root["cache"])
        return result

    def _inventory(self) -> Tuple[int, int, int]:
        game = self._inventory_cache["userGame"][self._session["userId"]]
        items = self._inventory_cache["userItem"]
        ap, _ = calculate_ap(game, self._inventory_cache)
        return ap, count(items.get(SAPLING_ID, {"num": 0}), "num"), count(items.get(FRUIT_ID, {"num": 0}), "num")

    def _ap_recovery_message(self) -> str:
        game = self._inventory_cache["userGame"][self._session["userId"]]
        server_time = count(self._inventory_cache, "serverTime")
        recover_at = count(game, "actRecoverAt")
        current_ap, _ = calculate_ap(game, self._inventory_cache)
        zone = timezone(timedelta(hours=8))
        now = datetime.fromtimestamp(server_time, zone)

        def time_label(timestamp: int) -> str:
            # 向后取整到整分钟，避免显示的预计时间早于实际恢复时间。
            recovery = datetime.fromtimestamp((timestamp + 59) // 60 * 60, zone)
            days = (recovery.date() - now.date()).days
            if days == 0:
                day_label = "今天"
            elif days == 1:
                day_label = "明天"
            else:
                day_label = f"{recovery.month}月{recovery.day}日"
            return f"{day_label}{recovery:%H:%M}"

        if recover_at <= server_time:
            full_message = "当前AP已回满。"
        else:
            full_message = f"预计AP将于{time_label(recover_at)}回满。"
        if current_ap >= AP_COST:
            threshold_message = "当前AP已达到40。"
        else:
            # 按每点 5 分钟粗估，不扣除当前未结束的恢复周期。
            recover_to_40 = server_time + (AP_COST - current_ap) * 300
            threshold_message = f"预计AP将于{time_label(recover_to_40)}恢复到40AP。"
        return full_message + "\n" + threshold_message

    def exchange(self, user: dict, requested: Optional[int], cancelled: Callable[[], bool]) -> str:
        try:
            if requested is not None and (type(requested) is not int or requested < 1):
                raise ValueError("兑换数须为正整数")
            self.toplogin(user, lambda phase: None, cancelled)
            if cancelled():
                raise ExportError("CANCELLED")
            # 等待后再读取 AP 和库存，兑换使用同一会话的最新数据。
            time.sleep(2)
            if cancelled():
                raise ExportError("CANCELLED")
            self._action("home")
            ap, saplings, fruit = self._inventory()
            max_by_ap = ap // AP_COST
            quantity = min(max_by_ap, saplings) if requested is None else requested
            if quantity > max_by_ap or max_by_ap == 0:
                return (f"兑换失败，AP 不足。当前 AP 为 {ap}，按 AP 最多可兑换 {max_by_ap} 个。\n"
                        + self._ap_recovery_message())
            if quantity > saplings or quantity == 0:
                return f"兑换失败，当前只有 {saplings} 株青铜树苗。"
            if cancelled():
                raise ExportError("CANCELLED")
            # 购买只提交一次，超时或回包缺失时避免重复扣除 AP 和库存。
            try:
                result = self._action("shoppurchase", id=SHOP_ID, num=str(quantity))
                after_ap, after_saplings, after_fruit = self._inventory()
                recovery_message = self._ap_recovery_message()
                confirmed = (count(result, "PurchaseNum") == quantity and saplings - after_saplings == quantity
                             and after_fruit - fruit == quantity)
            except ExportError as error:
                if error.code == "SERVER_MAINTENANCE":
                    raise
                if error.code.startswith("GAME_ACTION_REJECTED"):
                    return f"兑换失败，游戏服务器拒绝了请求（{error.code}）。"
                raise ExportError("PURCHASE_RESULT_UNCONFIRMED") from None
            except (AttributeError, KeyError, TypeError, ValueError):
                raise ExportError("PURCHASE_RESULT_UNCONFIRMED") from None
            if not confirmed:
                raise ExportError("PURCHASE_RESULT_UNCONFIRMED")
            return (f"兑换成功，获得 {quantity} 个青铜果实，消耗 {quantity * AP_COST} AP。"
                    f"剩余 AP：{after_ap}，青铜树苗：{after_saplings} 株。\n{recovery_message}")
        finally:
            user.clear()
            self._session.clear()
            self._inventory_cache.clear()
