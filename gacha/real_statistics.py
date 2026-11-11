"""与 Capture Kit 相同的国服出率口径，仅处理内存中的 toplogin。"""
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
import logging

from .statistics import format_draw_statistics

logger = logging.getLogger(__name__)

class CaptureRate(NamedTuple):
    total_draws: int
    ssr_count: int
    estimated: bool


def table(cache: Dict[str, Any], name: str, key: str, required: bool = True) -> List[dict]:
    rows = {}
    found = False
    # 与参考工具一致：replaced 是完整表，存在时覆盖 updated。
    for section in ("updated", "replaced"):
        if name not in cache.get(section, {}):
            continue
        found = True
        if section == "replaced":
            rows.clear()
        values = cache[section][name]
        if not isinstance(values, list):
            raise ValueError("数据表无效：" + name)
        for row in values:
            rows[str(row[key])] = row
    if required and not found:
        raise ValueError("缺少统计表：" + name)
    return list(rows.values())


def count(row: dict, key: str) -> int:
    value = row[key]
    if type(value) not in (int, str) or not str(value).isascii() or not str(value).isdigit():
        raise ValueError("无效的统计计数：" + key)
    return int(value)


def calculate_ap(account: dict, cache: dict) -> Tuple[int, int]:
    max_ap = count(account, "actMax")
    seconds_to_full = max(0, count(account, "actRecoverAt") - count(cache, "serverTime"))
    # 未完成的恢复周期尚未到账，溢出 AP 单独加回。
    missing_ap = (seconds_to_full + 299) // 300
    current_ap = max(0, max_ap - missing_ap) + count(account, "carryOverActPoint")
    return current_ap, max_ap


def format_real_statistics(
    root: dict, metadata: dict
) -> str:
    report, _ = build_real_statistics(root, metadata)
    return report


def build_real_statistics(
    root: dict, metadata: dict
) -> Tuple[str, Optional[CaptureRate]]:
    if not any(row.get("nid") == "login" and str(row.get("resCode")) in ("0", "00")
               and not row.get("fail") for row in root["response"]):
        raise ValueError("不是成功的 toplogin 响应")
    if metadata["schemaVersion"] != 1 or metadata["region"] != "CN":
        raise ValueError("不支持的统计元数据")
    cache = root["cache"]
    accounts = table(cache, "userGame", "userId")
    if len(accounts) != 1:
        raise ValueError("抓包账号数量无效")
    account = accounts[0]
    name = account["name"]
    friend_code = account["friendCode"]
    if not isinstance(name, str) or not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ValueError("抓包账号名称无效")
    if not isinstance(friend_code, str) or not friend_code.isascii() or not friend_code.isdigit():
        raise ValueError("抓包账号好友码无效")
    # 使用本次 toplogin 的角色信息，保留好友码原文及前导零，不使用 QQ 或 SDK 昵称。
    account_header = f"昵称：{name}\n好友码：{friend_code}\n"
    # 旧抓包可能缺少 AP 字段。字段齐全时以服务器时间计算抓包瞬间的 AP。
    if "serverTime" in cache and all(key in account for key in ("actMax", "actRecoverAt", "carryOverActPoint")):
        current_ap, max_ap = calculate_ap(account, cache)
        account_header += f"当前 AP：{current_ap}/{max_ap}\n"
    gachas = {str(row["id"]): row for row in metadata["gachas"]}
    gachas.update({str(row["id"]): row for row in table(cache, "mstGacha", "id", False) if "type" in row})
    servants = {str(row["id"]): row for row in metadata["servants"]}
    pulls = ssr = bags = 0
    unknown_gacha = False
    unknown_servant_ids: List[int] = []
    for row in table(cache, "userGacha", "gachaId"):
        ident, number = count(row, "gachaId"), count(row, "num")
        gacha = gachas.get(str(ident))
        if ident < 100 or (gacha is not None and int(gacha["type"]) == 3):
            continue
        pulls += number
        if gacha is None:
            unknown_gacha = True
        elif int(gacha["type"]) == 7:
            bags += 1
    for row in table(cache, "userSvtCollection", "svtId"):
        if count(row, "status") != 2:
            continue
        ident, number = count(row, "svtId"), count(row, "totalGetNum")
        servant = servants.get(str(ident))
        if servant is None:
            unknown_servant_ids.append(ident)
        elif servant["ssr"]:
            ssr += number
    if unknown_servant_ids:
        logger.warning("统计元数据无法识别已获得图鉴：svt_ids=%s", sorted(set(unknown_servant_ids)))
        return account_header + "真实出率：无法计算，存在未知图鉴 ID，请管理员更新统计元数据。", None
    rate = CaptureRate(pulls - bags, ssr - bags, unknown_gacha)
    text = format_draw_statistics(rate.total_draws, rate.ssr_count)
    if rate.total_draws == 0:
        return account_header + "真实出率：暂无可统计的有效抽卡记录。", None
    text = text.replace("你的模拟抽卡五星出率为", "真实出率：", 1)
    if unknown_gacha:
        text += "（暂估）"
    return account_header + text, rate
