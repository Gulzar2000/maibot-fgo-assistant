"""固定吐槽应区分出率变化、统计修正和未出货，不越过气泡边界。"""
from PIL import ImageFont
from unittest.mock import patch
import unittest

from test_pool_query import ROOT
from gacha.image_gen import _find_font
from gacha.rate_quips import LUCKY_VARIANTS, QUIPS, quip_context, select_quip, select_quip_context, select_quip_presentation


def point(draws, stars):
    return {"total_draws": draws, "ssr_count": stars}


class RateQuipTests(unittest.TestCase):
    def test_lucky_random_variants_keep_text_and_mascot_paired(self):
        for stars in (12, 15):
            for index, expected in ((-2, LUCKY_VARIANTS[0]), (-1, LUCKY_VARIANTS[1])):
                with patch("gacha.rate_quips.choice", side_effect=lambda items: items[index]):
                    self.assertEqual(select_quip_presentation([point(1000, stars)]), expected)
        with patch("gacha.rate_quips.choice") as pick:
            self.assertIsNone(select_quip_presentation([point(1000, 11)])[1])
            self.assertIsNone(select_quip_presentation([point(1000, 15), point(1100, 15)])[1])
            pick.assert_not_called()

    def test_droughts_and_count_changes_take_precedence_over_luck(self):
        for current, expected in (
            (point(1000, 15), "unchanged"), (point(1000, 16), "same_draws"),
            (point(999, 15), "corrected"), (point(1100, 14), "corrected"),
            (point(1010, 15), "dry_short"), (point(1030, 15), "dry_30"),
            (point(1100, 15), "dry_100"), (point(1330, 15), "dry_330"),
        ):
            with self.subTest(current=current):
                self.assertEqual(quip_context([point(1000, 15), current]), expected)

    def test_rate_trends_use_exact_counts_and_account_for_grade_changes(self):
        for previous, current, expected in (
            (point(1000, 11), point(1100, 13), "promoted"),
            (point(1000, 13), point(1200, 14), "demoted"),
            (point(1000, 15), point(1010, 16), "rise"),
            (point(1000, 15), point(1050, 18), "multi"),
            (point(1000, 15), point(1200, 17), "fall_with_ssr"),
            (point(1000, 15), point(1200, 18), "flat"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(quip_context([previous, current]), expected)

    def test_first_capture_covers_all_grades_and_small_samples(self):
        for stars, expected in ((13, "Grand 欧皇"), (12, "欧皇"), (11, "中庸"),
                                (10, "略非"), (9, "非酋"), (8, "酋长")):
            self.assertEqual(quip_context([point(1000, stars)]), expected)
        self.assertEqual(quip_context([point(10, 1)]), "small_sample")

    def test_fixed_lines_fit_bubble_and_selection_is_stable(self):
        fonts = [ImageFont.truetype(_find_font(str(ROOT / "res")), size) for size in (17, 14)]
        for choices in [*QUIPS.values(), tuple(lines for lines, _ in LUCKY_VARIANTS)]:
            for lines in choices:
                for line, font in zip(lines, fonts):
                    self.assertLessEqual(font.getlength(line), 135, line)
        history = [point(4919, 55)]
        self.assertEqual(select_quip(history), select_quip(history))
        self.assertIn(select_quip(history), QUIPS[quip_context(history)])

    def test_ordinary_changes_rotate_between_luck_and_trend(self):
        history = [point(1000, 15), point(1010, 15), point(1020, 15), point(1030, 15)]
        self.assertEqual([select_quip_context(history[:i]) for i in (2, 3, 4)],
                         ["Grand 欧皇", "dry_short", "Grand 欧皇"])
        repeated = [history[0], history[0], *history[1:]]
        self.assertEqual(select_quip_context(repeated), select_quip_context(history))
        self.assertIn(select_quip(history), QUIPS["Grand 欧皇"])

    def test_major_events_override_rotation_and_drought_accumulates_across_captures(self):
        history = [point(1000, 15), point(1040, 15), point(1080, 15), point(1120, 15)]
        self.assertEqual(select_quip_context(history), "dry_100")
        self.assertEqual(select_quip_context([*history, point(1330, 15)]), "dry_330")
        self.assertEqual(select_quip_context([point(1000, 11), point(1100, 13)]), "promoted")
        self.assertEqual(select_quip_context([point(1000, 13), point(1041, 13)]), "demoted")
        self.assertEqual(select_quip_context([point(1000, 15), point(1000, 15)]), "unchanged")
        self.assertEqual(select_quip_context([point(50, 1), point(60, 1)]), "small_sample")
