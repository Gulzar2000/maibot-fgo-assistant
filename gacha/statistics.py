"""累计五星从者出率与 Chaldea 欧非评级。"""
from typing import List, Tuple

# 对照 Chaldea 的 GachaLuckyGrade.fromRate 与 intl_zh.arb；以万分比整数判断边界。
# https://github.com/chaldea-center/chaldea/blob/main/lib/app/modules/import_data/sniff_details/report/report_data.dart
LUCK_GRADES: List[Tuple[int, str]] = [
    (125, "Grand 欧皇"), (115, "欧皇"), (105, "中庸"),
    (100, "略非"), (85, "非酋"), (0, "酋长"),
]


def validate_draw_statistics(total_draws: int, ssr_count: int) -> None:
    """拒绝损坏或不完整的累计计数，避免显示错误出率。"""
    if type(total_draws) is not int or type(ssr_count) is not int or not 0 <= ssr_count <= total_draws:
        raise ValueError("用户累计抽卡统计无效")


def format_draw_statistics(total_draws: int, ssr_count: int) -> str:
    """统计包含 UP、非 UP 和确定召唤的五星从者；礼装不计入五星数量。"""
    validate_draw_statistics(total_draws, ssr_count)
    if total_draws == 0:
        return "你还没有可统计的模拟抽卡记录，请先使用 /fgo 10 或 /fgo 100。"
    # 评级使用未四舍五入的实际比例，防止 1.249% 显示 1.25% 后被误判为 Grand。
    grade = next(name for threshold, name in LUCK_GRADES if ssr_count * 10000 >= threshold * total_draws)
    percent = ssr_count / total_draws * 100
    return f"你的模拟抽卡五星出率为{percent:.2f}%（{ssr_count} ★5/{total_draws} 抽），评级为 {grade}"
