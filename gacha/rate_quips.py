"""按实际统计选择固定吐槽，不调用模型。每句两行以适配气泡。"""
from random import choice
from typing import Any, Dict, List, Optional, Tuple

from .statistics import LUCK_GRADES


QUIPS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "Grand 欧皇": (("你这出率", "偷偷改过吧？"), ("石头归你", "欧气分我点！"), ("召唤阵那边", "有你亲戚？")),
    "欧皇": (("又是欧洲人", "让我吸一口！"), ("这出率", "我先酸为敬"), ("你负责出货", "我负责眼红")),
    "中庸": (("这份成绩", "还挺能打的"), ("不声不响", "已经赚到了"), ("这个出率", "可以偷着乐")),
    "略非": (("差那么一点", "就能抬头了"), ("欧气在门口", "还没进来呢"), ("出率卡这儿", "挺会吊胃口")),
    "非酋": (("石头没少花", "五星挺矜持"), ("召唤阵", "是不是没电？"), ("先把手洗洗", "玄学得做足")),
    "酋长": (("你这召唤阵", "该报修了吧"), ("五星的门", "焊死了是吧"), ("这份战绩", "看得我沉默")),
    "small_sample": (("才抽这么点", "先别急着封神"), ("样本还少呢", "先看个热闹")),
    "unchanged": (("没抽也来查", "盯着能出货？"), ("数都没动", "石头捂挺紧"), ("查了又查", "五星不会长的")),
    "corrected": (("账本变了？", "这笔另算吧"), ("数字对不上", "先别论欧非")),
    "same_draws": (("抽数没动", "五星倒多了？"), ("这次加五星", "不是靠新抽数")),
    "dry_330": (("三百多抽了", "五星睡着了？"), ("这么多石头", "连个响都没？")),
    "dry_100": (("又一百抽", "五星人呢？"), ("石头少一堆", "五星没挪窝"), ("百抽没动静", "召唤阵罢工？")),
    "dry_30": (("三十抽起步", "还在垫是吧"), ("石头下去了", "五星没上来")),
    "dry_short": (("还没见五星", "这笔先记着"), ("出率掉一点", "我可看见了")),
    "multi": (("一次多了几骑", "你搁这进货？"), ("五星扎堆来", "给别人留点"), ("你这不是抽卡", "是批发五星")),
    "promoted": (("评级升了！", "今天你坐主桌"), ("成功上岸", "先把欧气收好"), ("跨过这条线", "说话都硬气了")),
    "demoted": (("评级掉了", "欧洲签证到期"), ("刚才还挺欧", "怎么掉下来了")),
    "rise": (("出率抬头了", "召唤阵懂事了"), ("涨了涨了", "这笔看着舒坦"), ("五星到账", "腰杆直起来了")),
    "fall_with_ssr": (("出了还掉率", "这五星有点贵"), ("五星是来了", "石头也花多了"), ("出货归出货", "账可不便宜")),
    "flat": (("抽了也出了", "出率原地踏步"), ("忙活了一圈", "比例纹丝不动")),
}

# 新增图文成对抽取，避免气泡选到挥鞭文案却显示其他表情。
LUCKY_VARIANTS: Tuple[Tuple[Tuple[str, str], str], ...] = (
    (("求求你别抽了", "我跟不上了"), "rage"),
    (("欧气来！", "欧气来！"), "whip"),
)


def _grade(point: Dict[str, Any]) -> str:
    return next(name for threshold, name in LUCK_GRADES
                if point["ssr_count"] * 10000 >= threshold * point["total_draws"])


def quip_context(points: List[Dict[str, Any]]) -> str:
    """识别最近观测事件；普通事件是否展示由 select_quip_context 决定。"""
    current = points[-1]
    if len(points) == 1:
        return "small_sample" if current["total_draws"] < 100 else _grade(current)
    previous = points[-2]
    draws = current["total_draws"] - previous["total_draws"]
    stars = current["ssr_count"] - previous["ssr_count"]
    if draws < 0 or stars < 0:
        return "corrected"
    if draws == 0:
        return "unchanged" if stars == 0 else "same_draws"
    if stars == 0:
        # 连续多次抓包的未出货抽数累计计算；五星计数变化或抽数回退即停止。
        dry_draws = 0
        for before, after in zip(reversed(points[:-1]), reversed(points[1:])):
            if before["ssr_count"] != after["ssr_count"] or after["total_draws"] < before["total_draws"]:
                break
            dry_draws += after["total_draws"] - before["total_draws"]
        if dry_draws >= 100:
            return "dry_330" if dry_draws >= 330 else "dry_100"
        if _grade(current) != _grade(previous):
            return "demoted"
        return "dry_30" if dry_draws >= 30 else "dry_short"
    delta = current["ssr_count"] * previous["total_draws"] - previous["ssr_count"] * current["total_draws"]
    if delta < 0:
        return "demoted" if _grade(current) != _grade(previous) else "fall_with_ssr"
    if delta == 0:
        return "flat"
    if _grade(current) != _grade(previous):
        return "promoted"
    return "multi" if stars >= 2 else "rise"


def select_quip_context(points: List[Dict[str, Any]]) -> str:
    context = quip_context(points)
    if context in {"corrected", "same_draws", "unchanged", "promoted", "demoted", "dry_100", "dry_330", "multi"}:
        return context
    if points[-1]["total_draws"] < 100:
        return "small_sample"
    # 普通升降与累计评级各占一轮。重复抓包不推进轮次，也不靠数据奇偶碰运气。
    changes = sum(
        (before["total_draws"], before["ssr_count"]) != (after["total_draws"], after["ssr_count"])
        for before, after in zip(points, points[1:])
    )
    return _grade(points[-1]) if changes % 2 else context


def select_quip(points: List[Dict[str, Any]]) -> Tuple[str, str]:
    choices = QUIPS[select_quip_context(points)]
    # 同一份历史稳定显示同一句；后续观测从固定句库选取，不生成新文案。
    current = points[-1]
    return choices[(len(points) + current["total_draws"] + current["ssr_count"]) % len(choices)]


def select_quip_presentation(points: List[Dict[str, Any]]) -> Tuple[Tuple[str, str], Optional[str]]:
    context = select_quip_context(points)
    if context in {"欧皇", "Grand 欧皇"}:
        candidates: List[Tuple[Tuple[str, str], Optional[str]]] = [(lines, None) for lines in QUIPS[context]]
        candidates.extend(LUCKY_VARIANTS)
        return choice(candidates)
    return select_quip(points), None
