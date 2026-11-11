"""素材范围、收益口径、国服日期和单图发送的回归检查。"""
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
import base64
import importlib.util
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gacha.material_image import render_material_report
from gacha.materials import DATA_KEYS, MaterialData, MaterialQueryError, MaterialService, MaterialStore, allowed_material, cache_expiry

package = ModuleType("fgo_material_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".plugin", ROOT / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


def bundle() -> dict:
    quests = [dict(id=i, name=f"quest{i}", warId=100, afterClear="repeatLast", consume=21, phases=[3])
              for i in (1, 2, 3)]
    return {
        "items": [{"id": 6501, "name": "羽根", "icon": "https://example.test/6501.png"}, {"id": 6516, "name": "骨"},
                  {"id": 6503, "name": "英雄之证"}, {"id": 6502, "name": "世界树之种"},
                  {"id": 6505, "name": "虚影之尘"}, {"id": 6514, "name": "人工生命体幼体"},
                  {"id": 6005, "name": "术之辉石"}, {"id": 6105, "name": "术之魔石"},
                  {"id": 6205, "name": "术之秘石"},
                  {"id": 7105, "name": "術金棋"}, {"id": 6999, "name": "传承结晶"},
                  {"id": 2, "name": "圣晶石"}, {"id": 94000001, "name": "活动代币"}],
        "mappingData": {"item_names": {"羽根": {"CN": "凤凰羽毛"}, "骨": {"CN": "凶骨"},
                                      "術金棋": {"CN": "术阶金像"}},
                        "war_release": {"CN": [100]}, "quest_release": {"3": {"CN": 2000000000}}},
        "wars": [{"id": 100, "longName": "地图", "spots": [{"name": "地点", "quests": quests}]},
                 {"id": 9000, "longName": "活动", "spots": [{"name": "任务", "quests": [
                     {"id": 10, "gifts": [{"objectId": 6501, "num": 1}], "phases": [1]},
                     {"id": 11, "flags": ["branch"], "gifts": [{"objectId": 6501, "num": 999}]}]}]}],
        "dropData": {"domusAurea": {"itemIds": [6501], "questIds": [1, 2, 3],
                                   "apCosts": [20, 21, 1], "runs": [100, 200, 300],
                                   "sparseMatrix": {"0": {"0": 20, "1": 50, "2": 100}}},
                     "fixedDrops": {"1001": {"items": {"6501": 2}}}},
        "questPhaseDetails": [{"questId": 10, "phase": 1, "gifts": [{"objectId": 6501, "num": 3}]}],
        "events": [{"id": 42, "name": "活动A", "warIds": [9000],
                    "shop": [{"purchaseType": "item", "targetIds": [6501], "setNum": 2, "limitNum": 20}],
                    "missions": [{"rewardType": "gift", "gifts": [{"objectId": 6501, "num": 5}]},
                                 {"rewardType": "gift", "type": "random", "gifts": [{"objectId": 6501, "num": 99}]}]}],
        "wiki.events": [{"id": 42, "mcLink": "活动_A"}], "constData": {},
        "exchangeTickets": [
            {"id": 202510, "year": 2025, "month": 10, "items": [6501]},
            {"id": 202511, "year": 2025, "month": 11, "items": [6501]},
            {"id": 202512, "year": 2025, "month": 12, "items": [6501], "replaced": {"CN": []}},
            {"id": 202601, "year": 2026, "month": 1, "items": [6501, 6516, 6999], "multiplier": 4}],
    }


def scheduled(start="2026-11-01", end="2026-11-30") -> dict:
    return {"title": "活动A", "href": "https://fgo.wiki/w/活动_A", "start_date": start,
            "end_date": end, "estimated": True, "status": "当前"}


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.data = MaterialData(bundle(), 1794154934)
        self.today = date(2026, 11, 9)

    def test_scope_applies_to_ids_names_and_ticket_options(self):
        for keyword in ("传承结晶", "6999", "圣晶石", "2", "活动代币", "94000001", "QP", "圣杯"):
            with self.subTest(keyword=keyword), self.assertRaises(MaterialQueryError):
                self.data.resolve(keyword)
        for item_id in (6001, 6107, 6205, 7001, 7107, 6501):
            self.assertTrue(allowed_material(item_id))
        for item_id in (6000, 6008, 6301, 6999, 7008, 7199):
            self.assertFalse(allowed_material(item_id))
        self.assertEqual(self.data.tickets(6501, self.today)[0]["items"], ["凤凰羽毛", "凶骨"])

    def test_aliases_and_ambiguity(self):
        for name in ("羽毛", "凤凰羽毛", "6501", " 羽 毛 "):
            self.assertEqual(self.data.resolve(name), 6501)
        self.assertEqual(self.data.resolve("术金棋"), 7105)
        for keyword, item_id in (("井盖", 6503), ("核桃", 6502), ("黑灰", 6505), ("血瓶", 6514), ("骨头", 6516),
                                 ("术蓝饼干", 6005), ("术红饼干", 6105), ("术金饼干", 6205)):
            self.assertEqual(self.data.resolve(keyword), item_id)
        for keyword in ("羽根", "術金棋", "caster金棋子", "凤凰毛", "凤羽", "英雄证", "种子", "灰尘", "术金石", "术蓝石"):
            with self.subTest(keyword=keyword), self.assertRaises(MaterialQueryError):
                self.data.resolve(keyword)
        for keyword in ("饼干", "蓝饼干", "红饼干", "金饼干", "金棋", "银棋", "金棋子", "银棋子"):
            with self.subTest(keyword=keyword), self.assertRaisesRegex(MaterialQueryError, "不存在"):
                self.data.resolve(keyword)
        with self.assertRaises(MaterialQueryError):
            self.data.resolve("凤凰")
        self.data.aliases["重复别名"] = {6501, 6516}
        with self.assertRaisesRegex(MaterialQueryError, "对应多个素材"):
            self.data.resolve("重复别名")

    def test_free_ranking_uses_expected_count_and_filters_unreleased(self):
        rows = self.data.free_quests(6501, self.today)
        self.assertEqual([r["quest_id"] for r in rows], [2, 1])
        self.assertEqual(rows[0]["ap_per_item"], 42)
        self.data.mapping["war_release"]["CN"] = []
        self.assertEqual(self.data.free_quests(6501, self.today), [])

    def test_fixed_reward_breakdown_and_skip_branch_random_mission(self):
        rewards = self.data.event_rewards(self.data.bundle["events"][0], 6501)
        self.assertEqual(rewards["total"], 51)
        self.assertEqual(rewards["parts"], {"商店": 40, "任务": 5, "通关": 4, "固定掉落": 2})

    def test_repeat_lottery_is_per_box_and_finite_lottery_is_total(self):
        event = {"id": 1, "lotteries": [{"limited": False, "boxes": [
            {"boxIndex": 0, "maxNum": 2, "gifts": [{"objectId": 6501, "num": 1}]},
            {"boxIndex": 9, "maxNum": 3, "gifts": [{"objectId": 6501, "num": 1}]}]}]}
        self.assertEqual(self.data.event_rewards(event, 6501),
                         {"total": 0, "parts": {}, "extra": ["无限池：每池 3 个"]})
        event["lotteries"][0]["limited"] = True
        self.assertEqual(self.data.event_rewards(event, 6501)["total"], 5)

    def test_current_future_and_end_day_included_past_and_unknown_skipped(self):
        self.assertEqual(len(self.data.upcoming_events(6501, [scheduled(end="2026-11-09")], self.today)), 1)
        self.assertEqual(self.data.upcoming_events(6501, [scheduled(end="2026-11-08")], self.today), [])
        self.assertEqual(len(self.data.upcoming_events(6501, [scheduled(start="2027-01-01", end=None)], self.today)), 1)
        self.assertEqual(self.data.upcoming_events(6501, [], self.today), [])

    def test_ticket_region_offset_replacements_and_future_only(self):
        tickets = self.data.tickets(6501, self.today)
        self.assertEqual([x["month"] for x in tickets], ["2027-01"])
        self.assertEqual(tickets[0]["maximum"], 124)
        self.assertEqual(tickets[0]["daily"], 4)

    def test_image_has_all_sections_and_handles_long_names_and_empty_results(self):
        report = self.data.report("羽毛", [scheduled()], self.today)
        report["icon_bytes"] = icon_png()
        report["events"][0]["title"] = "未来活动名称" * 16
        for current in (report, dict(report, free=[], events=[], tickets=[])):
            with Image.open(BytesIO(base64.b64decode(render_material_report(current, str(ROOT / "res"))))) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, 1120)
                self.assertGreater(image.height, 800)
                self.assertEqual(image.getpixel((106, 130)), (240, 10, 150))

    def test_icon_download_is_validated_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MaterialService(Path(directory))
            with patch("gacha.materials._fetch_bytes_sync", return_value=icon_png()) as fetch:
                self.assertEqual(service.load_icon(6501, "https://example.test/icon.png"), icon_png())
                service.load_icon(6501, "https://example.test/icon.png")
                fetch.assert_called_once()

    def test_corrupt_cache_and_remote_hash_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            version = {"timestamp": 1, "files": {key: {"key": key, "filename": key + ".json", "size": 2, "hash": "badbad"}
                                                 for key in DATA_KEYS}}
            with patch("gacha.materials._fetch_bytes_sync", side_effect=lambda url:
                       json.dumps(version).encode() if url.endswith("version.json") else b"{}"):
                with self.assertRaisesRegex(ValueError, "文件校验失败"):
                    MaterialStore(Path(directory)).load()

    def test_cache_survives_restart_and_refreshes_after_two_months(self):
        with tempfile.TemporaryDirectory() as directory:
            contents = {key + ".json": json.dumps(value).encode() for key, value in bundle().items()}
            version = {"timestamp": 1, "files": {key: {"key": key, "filename": key + ".json",
                       "size": len(contents[key + ".json"]), "hash": hashlib.md5(contents[key + ".json"]).hexdigest()[:6]}
                       for key in DATA_KEYS}}
            contents["version.json"] = json.dumps(version).encode()
            with patch("gacha.materials._fetch_bytes_sync", side_effect=lambda url: contents[url.rsplit('/', 1)[-1]]) as fetch:
                first = MaterialStore(Path(directory))
                first.load()
                self.assertEqual(fetch.call_count, len(DATA_KEYS) + 1)
                fetch.reset_mock()
                second = MaterialStore(Path(directory))
                second.load()
                fetch.assert_not_called()
                with patch("gacha.materials.time.time", return_value=second.expires_at + 1):
                    second.load()
                fetch.assert_called_once()

    def test_two_calendar_months_clamp_month_end(self):
        start = datetime.fromisoformat("2026-12-31T12:00:00+08:00")
        self.assertEqual(datetime.fromtimestamp(cache_expiry(start.timestamp()), start.tzinfo).isoformat(),
                         "2027-02-28T12:00:00+08:00")


class MaterialCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_commands_send_one_image_and_render_off_event_loop(self):
        handler = plugin.FgoGachaPlugin.cmd_material
        for command in ("/fgo material 羽毛", "/fgo 素材 羽毛"):
            service = SimpleNamespace(query=AsyncMock(return_value={"name": "凤凰羽毛"}))
            fake = SimpleNamespace(_materials=service, res_dir=str(ROOT / "res"),
                                   ctx=SimpleNamespace(send=SimpleNamespace(image=AsyncMock(return_value=True))))
            main = threading.get_ident()
            threads = []

            def render(*args):
                threads.append(threading.get_ident())
                return "one-image"

            with patch.object(plugin, "render_material_report", side_effect=render):
                self.assertEqual(await handler(fake, stream_id="stream", message=command), (True, "", True))
            self.assertNotEqual(threads[0], main)
            service.query.assert_awaited_once_with("羽毛")
            fake.ctx.send.image.assert_awaited_once_with("one-image", "stream")

    async def test_missing_material_and_unsupported_scope_send_no_image(self):
        handler = plugin.FgoGachaPlugin.cmd_material
        fake = SimpleNamespace(_materials=SimpleNamespace(query=AsyncMock(side_effect=plugin.MaterialQueryError("不支持传承结晶"))),
                               ctx=SimpleNamespace(send=SimpleNamespace(image=AsyncMock())))
        result = await handler(fake, stream_id="stream", message="/fgo 素材 传承结晶")
        self.assertIn("不支持", result[1])
        fake.ctx.send.image.assert_not_awaited()
        self.assertIn("用法", (await handler(fake, stream_id="stream", message="/fgo material"))[1])

    async def test_service_keeps_data_work_off_loop_and_reuses_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MaterialService(Path(directory))
            data = MaterialData(bundle(), 1794154934)
            service.store.expires_at = cache_expiry(time.time())
            with patch.object(service.store, "load", return_value=data), patch.object(service, "_prefetch_icons") as prefetch, patch.object(service, "load_icon", return_value=icon_png()), patch("gacha.materials.fetch_events", new=AsyncMock(return_value=[scheduled()])) as fetch:
                await service.query("羽毛")
                await service.query("羽毛")
                fetch.assert_awaited_once()
                prefetch.assert_called_once_with(data)
            restarted = MaterialService(Path(directory))
            restarted.store.expires_at = service.store.expires_at
            with patch.object(restarted.store, "load", return_value=data), patch.object(restarted, "_prefetch_icons") as prefetch, patch("gacha.materials.fetch_events", new=AsyncMock(return_value=[scheduled()])) as fetch:
                await restarted.prepare()
                fetch.assert_not_awaited()
                prefetch.assert_not_called()
                with patch("gacha.materials.time.time", return_value=restarted._expires_at + 1):
                    await restarted.prepare()
                fetch.assert_awaited_once()
                prefetch.assert_called_once_with(data)


def icon_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (100, 100), (240, 10, 150)).save(output, format="PNG")
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
