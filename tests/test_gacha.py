"""抽卡数据、概率、资源下载的定向回归测试。"""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from PIL import Image
import ast
import asyncio
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gacha import downloader, engine, pool_fetcher
from gacha.pool_manager import PoolManager


def sample_pool() -> dict:
    return pool_fetcher._parse_data_page(
        "type\tstar\tweight\tdisplay\tids\n"
        "svt\t5\t0.8\t1\t142\nsvt\t5\t0.2\t0\t2\n"
        "svt\t4\t3\t0\t7\nsvt\t3\t40\t0\t9\n"
        "ce\t5\t4\t0\t31\nce\t4\t4\t1\t2600\n"
        "ce\t4\t8\t0\t7\nce\t3\t40\t0\t89", "test"
    )


class GachaTests(unittest.TestCase):
    def test_fetch_keeps_original_subpool_ids(self):
        raw = "type\tstar\tweight\tdisplay\tids\\nsvt\t5\t1\t1\t142\\nce\t4\t99\t0\t31"
        html = f"var raw_str_list = ['{raw}', '', '{raw.replace('142', '394')}'];"
        with patch.object(pool_fetcher, "fetch_text", new_callable=AsyncMock, return_value=html):
            data = asyncio.run(pool_fetcher.fetch_pool_data("https://fgo.wiki/w/test"))
        self.assertEqual([child["s_id"] for child in data["sub_pools"]], [0, 2])
        self.assertEqual(data["sub_pools"][1]["servants"][0]["ids"], ["394"])

    def test_data_page_reference_is_resolved(self):
        with patch.object(pool_fetcher, "fetch_text", new_callable=AsyncMock, return_value="svt\t3\t100\t0\t7"):
            text = asyncio.run(pool_fetcher._resolve_data_references("[[:test/data1]]"))
        self.assertEqual(pool_fetcher._parse_data_page(text, "")["servants"][0]["ids"], ["7"])
        with self.assertRaises(ValueError):
            asyncio.run(pool_fetcher._resolve_data_references("[[:test/data1]]", ["test/data1"]))

    def test_cache_selects_subpool_and_rejects_old_version(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = PoolManager(folder)
            data = sample_pool()
            manager.save_gacha_data([{**data, "p_id": 5, "s_id": 2, "pool_href": "testpool"}])
            self.assertIsNone(manager.get_gacha_data_for_banner({"id": 5, "s_id": 0, "href": "testpool"}))
            self.assertIsNotNone(manager.get_gacha_data_for_banner({"id": 5, "s_id": 2, "href": "testpool"}))
            data.pop("data_version")
            manager.save_gacha_data([{**data, "p_id": 5, "s_id": 2, "pool_href": "testpool"}])
            self.assertIsNone(manager.get_gacha_data_for_banner({"id": 5, "s_id": 2, "href": "testpool"}))

    def test_multiple_js_strings_stay_separate(self):
        first = "type\tstar\tweight\tdisplay\tids\\nsvt\t5\t1\t1\t142\\nce\t4\t99\t0\t31"
        second = first.replace("142", "394")
        html = f"var raw_str_list = ['{first}', '{second}', '', ''];"
        match = pool_fetcher.RAW_LIST_PATTERN.search(html)
        self.assertIsNotNone(match)
        values = ast.literal_eval(match.group(1))
        self.assertEqual(len(values), 4)
        self.assertEqual(pool_fetcher._parse_js_data(values[1], "")["servants"][0]["ids"], ["394"])

    def test_invalid_ids_and_weights_rejected(self):
        for value in ("", "type", "[[:data2]]", "0"):
            data = sample_pool()
            data["crafts"][-1]["ids"] = [value]
            with self.assertRaises(ValueError):
                engine._build_pool_data(data)
        data = sample_pool()
        data["crafts"][-1]["weight"] = float("nan")
        with self.assertRaises(ValueError):
            engine._build_pool_data(data)

    def test_duplicate_card_rejected_but_distinct_groups_preserved(self):
        data = sample_pool()
        row = deepcopy(data["servants"][0])
        data["servants"].append(row)
        with self.assertRaises(ValueError):
            engine._build_pool_data(data)
        row["ids"] = ["394"]
        pool = engine._build_pool_data(data)
        self.assertEqual(sum(c["weight"] for c in pool if c["type"] == "svt" and c["star"] == 5), 1.8)

    def test_exact_rate_intervals_and_guarantee_up_ratio(self):
        pool = engine._build_pool_data(sample_pool())
        self.assertEqual(engine._roll_once(0, pool)[1:], ("up5", "142"))
        self.assertEqual(engine._roll_once(0.008, pool)[1:], ("5", "2"))
        self.assertEqual(engine._roll_once(0.04, pool)[1:], ("3", "9"))
        # 金卡保底中四星礼装占 92%，其中 UP 占 1/3，不能按两组 ID 均分。
        self.assertEqual(engine._roll_once(0.08, pool, engine.GOLDEN_RATES)[1:], ("ce_up4", "2600"))
        self.assertEqual(engine._roll_once(0.4, pool, engine.GOLDEN_RATES)[1:], ("ce_4", "7"))

    def test_every_eleven_pull_has_both_guarantees(self):
        engine.random.seed(21)
        for _ in range(500):
            cards = engine.simulate_ten_pull(sample_pool())
            self.assertEqual(len(cards), 11)
            self.assertTrue(any(card["type"] == "svt" for card in cards))
            self.assertTrue(any(card["rarity"][-1] in "45" for card in cards))
            self.assertTrue(all(card["id"].isdigit() for card in cards))

    def test_hundred_pull_does_not_merge_same_number(self):
        summary = engine._count_gacha_results([
            {"type": "svt", "rarity": "4", "id": "7"},
            {"type": "cft", "rarity": "ce_4", "id": "7"},
        ], sample_pool())
        self.assertEqual({card["type"] for card in summary}, {"svt_4", "ce_4"})
        self.assertEqual(sum(card["count"] for card in engine.simulate_hundred_pull(sample_pool())), 110)

    def test_new_jp_ce_download_uses_face_url(self):
        with tempfile.TemporaryDirectory() as folder:
            cards = {"svt": {}, "ce": {"2599": {"face": "https://example.test/actual.png"}}}
            with patch.object(downloader, "_get_atlas_cards", return_value=cards), patch.object(
                downloader, "download_file", new_callable=AsyncMock, return_value=True
            ) as download:
                self.assertTrue(asyncio.run(downloader.download_ce_icon("2599", folder)))
                download.assert_awaited_once_with("https://example.test/actual.png", str(Path(folder) / "ce_2599.png"))

    def test_corrupt_image_cache_is_redownloaded(self):
        stream = BytesIO()
        Image.new("RGB", (5, 5)).save(stream, "PNG")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ce_2599.png"
            path.write_bytes(b"not an image" * 100)
            with patch.object(downloader, "fetch_bytes", new_callable=AsyncMock, return_value=stream.getvalue()):
                self.assertTrue(asyncio.run(downloader.download_file("https://example.test/image", str(path))))
            self.assertTrue(downloader._valid_image(str(path)))


if __name__ == "__main__":
    unittest.main()
