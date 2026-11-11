"""历史迁移、局部坐标与 QQ 图文发送的离线验证。"""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image
import asyncio
import base64
import unittest

from test_pool_query import plugin
from test_pool_query import ROOT
from gacha.rate_mascot import CONTEXT_MASCOTS, load_mascot
from gacha.rate_quips import QUIPS
from gacha.rate_chart import draw_bounds, history_points, render_rate_chart, smooth_curve


class RateChartTests(unittest.TestCase):
    def test_mascots_cover_quip_categories_and_load_visible_transparent_sprites(self):
        self.assertEqual(set(CONTEXT_MASCOTS), set(QUIPS))
        cases = [[(1000, stars)] for stars in (15, 12, 11, 9, 8)]
        cases.append([(1000, 15), (1000, 16)])
        for case in cases:
            points = [{"total_draws": draws, "ssr_count": stars} for draws, stars in case]
            with self.subTest(case=case):
                sprite = load_mascot(ROOT / "res" / "rate_chart", points)
                self.assertEqual(sprite.mode, "RGBA")
                self.assertIsNotNone(sprite.getbbox())
                self.assertEqual(sprite.getextrema()[3], (0, 255))
                if case in [[(1000, 12)], [(1000, 9)], [(1000, 8)], [(1000, 15), (1000, 16)]]:
                    self.assertGreater(sprite.height, sprite.width * 1.15)

    def test_smoothing_preserves_observations_without_overshoot(self):
        points = [(0, 2), (20, 5), (21, 1), (80, 1), (100, 4)]
        curve = smooth_curve(points)
        self.assertGreater(len(curve), len(points))
        for point in points:
            self.assertIn(point, curve)
        for a, b in zip(points, points[1:]):
            for x, y in curve:
                if a[0] <= x <= b[0]:
                    self.assertGreaterEqual(y, min(a[1], b[1]) - 1e-10)
                    self.assertLessEqual(y, max(a[1], b[1]) + 1e-10)
        repeated = [(10, 2), (10, 2), (10, 3), (9, 1)]
        self.assertEqual(smooth_curve(repeated), [(10, 2), (10, 3), (9, 1)])
        self.assertEqual(smooth_curve([(10, 2)]), [(10, 2)])

    def test_legacy_point_is_preserved_and_not_aliased(self):
        old = {"day": "2026-11-07", "total_draws": 4919, "ssr_count": 55, "estimated": True}
        entry = {"summary": old}
        points = history_points(entry)
        self.assertEqual(points, [old])
        points[0]["ssr_count"] = 56
        self.assertEqual(old["ssr_count"], 55)
        self.assertEqual(history_points({"summary": old, "snapshots": [old, old]}), [old, old])

    def test_single_and_multiple_points_render_with_local_x_range(self):
        first = {"day": "2026-11-07", "total_draws": 4919, "ssr_count": 55, "estimated": True}
        second = {**first, "total_draws": 5029, "ssr_count": 57}
        for points in ([first], [first, first, second], [second, first]):
            low, high = draw_bounds([p["total_draws"] for p in points])
            self.assertGreater(low, 4900)
            self.assertGreater(high, max(p["total_draws"] for p in points))
            with Image.open(BytesIO(base64.b64decode(render_rate_chart(points)))) as image:
                self.assertEqual(image.size, (800, 520))

    def test_success_sends_text_and_image_together_prompt_sends_only_text(self):
        async def run():
            send = SimpleNamespace(text=AsyncMock(return_value=True), hybrid=AsyncMock(return_value=True))
            fake = SimpleNamespace(ctx=SimpleNamespace(send=send))
            await plugin.FgoGachaPlugin._send_capture_report(fake, plugin.CaptureReport("report", "png"), "stream")
            send.hybrid.assert_awaited_once_with([
                {"type": "text", "content": "report"},
                {"type": "image", "binary_data_base64": "png"},
            ], "stream")
            await plugin.FgoGachaPlugin._send_capture_report(fake, "confirm", "stream")
            send.text.assert_awaited_once_with("confirm", "stream")
        asyncio.run(run())
