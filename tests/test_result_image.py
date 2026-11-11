"""成品缩略图索引、素材校验和十一连结果图的回归测试。"""
from pathlib import Path
from unittest.mock import AsyncMock, patch
from PIL import Image
import asyncio
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gacha import card_assets, image_gen


class ResultImageTests(unittest.TestCase):
    def test_index_reads_real_fields_and_encodes_chinese_filename(self):
        raw = "id,star,icon\n242,5,//media.fgo.wiki/1/12/礼装242.jpg\n"
        index = card_assets._parse_thumbnail_index("raw_str = " + json.dumps(raw), "ce")
        self.assertEqual(index["242"]["star"], 5)
        self.assertEqual(index["242"]["url"], "https://media.fgo.wiki/1/12/%E7%A4%BC%E8%A3%85242.jpg")

    def test_invalid_or_missing_index_is_exposed(self):
        for raw in ("id,star,avatar\n1,5,https://wrong.test/a.png\n",
                    "id,star,avatar\n1,5,//media.fgo.wiki/a.jpg\n1,5,//media.fgo.wiki/b.jpg\n"):
            with self.assertRaises(ValueError):
                card_assets._parse_thumbnail_index("raw_str = " + json.dumps(raw), "svt")
        with self.assertRaises(ValueError):
            card_assets._parse_thumbnail_index("no data", "ce")

    def test_duplicate_draws_download_once_but_same_number_different_kind_stays_separate(self):
        cards = [{"type": "svt", "id": "2", "rarity": "5"}] * 10 + [
            {"type": "cft", "id": "2", "rarity": "ce_3"}]
        index = {"svt": {"2": {"star": 5}}, "ce": {"2": {"star": 3}}}
        with patch.object(card_assets, "load_thumbnail_index", new_callable=AsyncMock, return_value=index), patch.object(
            card_assets, "download_result_card", new_callable=AsyncMock, return_value=True
        ) as download:
            asyncio.run(card_assets.ensure_result_cards(cards, "icons"))
        self.assertEqual(download.await_count, 2)
        self.assertEqual({call.args[:2] for call in download.call_args_list}, {("svt", "2"), ("ce", "2")})

    def test_wrong_star_and_failed_download_are_not_replaced_by_blank_cards(self):
        cards = [{"type": "svt", "id": "2", "rarity": "5"}]
        for star, downloaded, exception in ((3, True, ValueError), (5, False, OSError)):
            with self.subTest(star=star), patch.object(card_assets, "load_thumbnail_index", new_callable=AsyncMock,
                return_value={"svt": {"2": {"star": star}}, "ce": {}}
            ), patch.object(card_assets, "download_result_card", new_callable=AsyncMock, return_value=downloaded):
                with self.assertRaises(exception):
                    asyncio.run(card_assets.ensure_result_cards(cards, "icons"))

    def test_output_keeps_ui_and_all_eleven_cards_in_draw_order(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "cards").mkdir()
            result = []
            colors = []
            for number in range(1, 12):
                kind = "svt" if number % 2 else "ce"
                color = (number * 20, 80, 100)
                colors.append(color)
                Image.new("RGB", (132, 144), color).save(base / "cards" / f"{kind}_{number}.png")
                result.append({"type": "svt" if kind == "svt" else "cft", "id": str(number),
                               "rarity": "3" if kind == "svt" else "ce_4"})
            banner = "很长的召唤标题" * 100
            with patch.object(image_gen, "_draw_wrapped_text", wraps=image_gen._draw_wrapped_text) as draw_title:
                path = image_gen.generate_ten_pull_image(result, str(ROOT / "res"), directory, banner=banner,
                                                        user_nickname="某某人")
            self.assertEqual(draw_title.call_args.args[1], "某某人：" + banner)
            try:
                with Image.open(path) as output:
                    self.assertEqual(output.size, image_gen.CANVAS_SIZE)
                    for position, color in zip(image_gen.card_positions(), colors):
                        self.assertEqual(output.getpixel((position[0] + 99, position[1] + 100)), color)
                        # 原图的顶部斜边与左右底角必须保留，防止大切角遮罩裁进卡框。
                        for offset in ((15, 2), (195, 15), (1, 214), (196, 214)):
                            self.assertEqual(output.getpixel((position[0] + offset[0], position[1] + offset[1])), color)
                    # 上方数量和下方按钮区域不应被结果卡片、长标题等覆盖。
                    with Image.open(ROOT / "res/summon_background.png") as background:
                        resized = image_gen.ImageOps.fit(background.convert("RGB"), image_gen.CANVAS_SIZE,
                                                       method=Image.Resampling.LANCZOS)
                        for position in ((600, 50), (580, 130), (450, 785), (1120, 785)):
                            self.assertEqual(output.getpixel(position), resized.getpixel(position))
            finally:
                Path(path).unlink()

    def test_missing_ce_reports_name_from_ce_index_and_wrong_count_is_rejected(self):
        cards = [{"type": "cft", "id": "242", "rarity": "ce_5"}] * 11
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "ce #242 测试礼装"):
                image_gen.generate_ten_pull_image(cards, str(ROOT / "res"), directory,
                                                 name_map={"ce": {"242": "测试礼装"}})
        with self.assertRaises(ValueError):
            image_gen.generate_ten_pull_image(cards[:10], "res", "icons")


if __name__ == "__main__":
    unittest.main()
