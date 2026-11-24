"""规划费用的独立算例、重复卡选择、账号互斥及成图回归。"""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image
import asyncio
import base64
import hashlib
import json
import re
import tempfile
import threading
import unittest

from test_pool_query import ROOT, plugin
from gacha.accounts import AccountStore
from gacha.account_protocol.errors import ExportError
from gacha.planning import BOARD_URL, DATA_ROOT, PlanDataError, PlanStore, build_plan_report
from gacha.plan_image import GOLD, WIDTH, render_plan_report


def metadata() -> dict:
    servants = {}
    for ident, rarity in ((1, 5), (2, 4), (3, 3)):
        servants[str(ident)] = {"id": ident, "collectionNo": ident, "name": "测试从者", "rarity": rarity,
            "type": "normal",
            "ascensionMaterials": {str(lv): {"items": [{"itemId": 10, "amount": lv + 1}],
                                                     "qp": 100 * (lv + 1)} for lv in range(4)},
            "skillMaterials": {str(lv): {"items": [{"itemId": 11, "amount": lv},
                                       *([{"itemId": 6999, "amount": 1}] if lv == 9 else [])],
                                                 "qp": lv * 10} for lv in range(1, 10)}}
    return {"servants": servants, "timestamp": 1700000000,
            "items": {str(i): {"name": name, "icon": "synthetic"} for i, name in
                      ((1, "QP"), (10, "突破素材"), (11, "技能素材"), (6999, "传承结晶"))}, "item_names": {},
            "boards": [{"id": 1, "squares": [
                {"id": 1, "lock": None, "items": [{"itemId": 10, "amount": 5}]},
                {"id": 2, "lock": {"items": [{"itemId": 10, "amount": 2}]}, "items": []},
                {"id": 3, "lock": None, "flags": ["blank"], "items": [{"itemId": 10, "amount": 999}]}]},
                {"id": 10001, "parentClassBoardBaseId": 1, "squares": [
                    {"id": 1, "lock": None, "items": [{"itemId": 10, "amount": 7}]}]}]}


def card(ident: int, ascension: int, skills: List[int], *, instance: int = 0) -> dict:
    return {"id": instance or ident, "svtId": ident, "limitCount": ascension,
            **{f"skillLv{i + 1}": value for i, value in enumerate(skills)}}


def root() -> dict:
    return {"response": [{"nid": "login", "resCode": "00"}], "cache": {"serverTime": 1700000000,
        "replaced": {"userGame": [{"userId": 123, "name": "合成测试御主", "qp": 50}],
            "userItem": [{"itemId": 10, "num": 3}, {"itemId": 11, "num": 10}],
            "userSvt": [card(1, 3, [8, 10, 10])], "userSvtStorage": [card(2, 4, [7, 8, 10])],
            "userSvtCollection": [{"svtId": 3, "status": 2}],
            "userClassBoardSquare": [{"classBoardBaseId": 1, "classBoardSquareIds": [1],
                                       "classBoardUnlockSquareIds": []}]}}}


class PlanStoreTests(unittest.TestCase):
    def test_refresh_keeps_material_query_cache_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "materials").mkdir()
            old_items = b"material query cached version"
            (base / "materials/items.json").write_bytes(old_items)
            data = metadata()
            servant = data["servants"]["1"]
            payloads = {
                "servants.json": json.dumps([dict(servant, id=i) for i in range(300)]).encode(),
                "items.json": b"[]",
                "mappingData.json": b'{"item_names": {}}',
            }
            files = {name: {"key": name[:-5], "filename": name, "size": len(raw),
                            "hash": hashlib.md5(raw).hexdigest()} for name, raw in payloads.items()}
            version = json.dumps({"timestamp": 1700000000, "files": files}).encode()
            boards = json.dumps([{"id": i, "parentClassBoardBaseId": 0, "squares": []}
                                 for i in range(1, 10)]).encode()

            def fetch(url: str) -> bytes:
                if url == DATA_ROOT + "version.json":
                    return version
                if url == BOARD_URL:
                    return boards
                return payloads[url.removeprefix(DATA_ROOT)]

            with patch("gacha.planning._fetch_bytes_sync", side_effect=fetch):
                result = PlanStore(base).load()
            self.assertEqual(len(result["servants"]), 300)
            self.assertEqual((base / "materials/items.json").read_bytes(), old_items)
            self.assertEqual((base / "planning_data/items.json").read_bytes(), payloads["items.json"])


class PlanTests(unittest.TestCase):
    def test_four_targets_consume_inventory_once_and_include_storage_locks_but_no_level10_cost(self):
        report = build_plan_report(root(), metadata(), include_grand=True)
        rows = {row["id"]: row for row in report["items"]}
        self.assertEqual(rows[10]["required"], [4, 13, 13, 13])
        self.assertEqual(rows[10]["missing"], [1, 10, 10, 10])
        self.assertEqual(rows[11]["required"], [0, 0, 8, 31])
        self.assertEqual(rows[11]["missing"], [0, 0, 0, 21])
        self.assertNotIn(6999, rows)
        self.assertEqual(rows[1]["missing"], [350, 350, 430, 660])
        self.assertEqual((report["total"], report["maxed"], report["trained"]), (2, 1, 0))
        self.assertEqual((report["nodes"], report["lit"], report["locks"], report["unlocked"]), (2, 1, 1, 0))

    def test_regular_board_scope_and_separate_unlock_and_enhance_cost(self):
        top = root()
        top["cache"]["replaced"]["userClassBoardSquare"][0]["classBoardUnlockSquareIds"] = [2]
        report = build_plan_report(top, metadata())
        row = next(row for row in report["items"] if row["id"] == 10)
        self.assertEqual(row["required"], [4, 4, 4, 4])
        self.assertEqual((report["nodes"], report["lit"], report["unlocked"]), (1, 1, 1))

    def test_duplicates_use_one_real_card_and_do_not_combine_skill_maxima(self):
        top = root()
        top["cache"]["replaced"]["userSvt"] = [card(1, 4, [10, 8, 10]), card(1, 4, [8, 10, 10], instance=99)]
        report = build_plan_report(top, metadata())
        self.assertEqual((report["total"], report["duplicates"], report["trained"]), (2, 1, 0))
        row = next(row for row in report["items"] if row["id"] == 11)
        self.assertEqual(row["required"][2], 8)

    def test_maxed_skills_need_no_materials_and_mash_story_ascension_is_free(self):
        top, data = root(), metadata()
        top["cache"]["replaced"]["userSvt"] = [card(1, 0, [10, 10, 10])]
        top["cache"]["replaced"]["userSvtStorage"] = []
        data["servants"]["1"].update(type="heroine", ascensionMaterials={})
        report = build_plan_report(top, data)
        self.assertEqual(report["trained"], 1)
        self.assertEqual(report["maxed"], 0)
        self.assertTrue(all(row["required"][1] == row["required"][3] for row in report["items"]))

    def test_missing_data_and_invalid_skill_are_errors(self):
        top, data = root(), metadata()
        del data["servants"]["1"]["skillMaterials"]["8"]
        with self.assertRaises(PlanDataError):
            build_plan_report(top, data)
        top["cache"]["replaced"]["userSvt"][0]["skillLv1"] = 0
        with self.assertRaises(PlanDataError):
            build_plan_report(top, metadata())
        del top["cache"]["replaced"]["userSvtStorage"]
        with self.assertRaises(ValueError):
            build_plan_report(top, metadata())

    def test_full_inventory_produces_zero_shortage(self):
        top = root()
        top["cache"]["replaced"]["userGame"][0]["qp"] = 100000
        top["cache"]["replaced"]["userItem"] = [{"itemId": i, "num": 100000} for i in (10, 11, 6999)]
        report = build_plan_report(top, metadata())
        self.assertEqual(report["missing_types"], [0, 0, 0, 0])
        image = Image.open(BytesIO(base64.b64decode(render_plan_report(report, str(ROOT / "res"), {}))))
        self.assertEqual(image.width, WIDTH)

    def test_large_report_increases_canvas_instead_of_truncating_rows(self):
        report = build_plan_report(root(), metadata())
        report["items"] = [dict(report["items"][0], id=i + 100, name="合成测试素材", missing=[1, 2, 3, 4]) for i in range(160)]
        icon = BytesIO()
        Image.new("RGB", (42, 42), "white").save(icon, "PNG")
        icons = {row["id"]: icon.getvalue() for row in report["items"]}
        image = Image.open(BytesIO(base64.b64decode(render_plan_report(report, str(ROOT / "res"), icons))))
        self.assertGreater(image.height, 5000)


class PlanComparisonTests(unittest.TestCase):
    def test_skill_progress_counts_each_level_and_caps_at_nine(self):
        before = build_plan_report(root(), metadata())
        self.assertEqual((before["skill_steps"], before["skill_total"]), (44, 48))
        top = root()
        top["cache"]["replaced"]["userSvt"][0]["skillLv1"] = 9
        after = build_plan_report(top, metadata(), before["snapshot"])
        self.assertEqual(after["skill_steps"], 45)
        self.assertEqual(after["comparison"]["progress"]["skill_steps"], 1)

    def test_stock_minus_one_plus_eleven_spent_shows_ten_obtained(self):
        data, top = metadata(), root()
        data["servants"]["1"]["skillMaterials"]["8"]["items"][0]["amount"] = 11
        before = build_plan_report(top, data)
        top["cache"]["replaced"]["userSvt"][0]["skillLv1"] = 9
        top["cache"]["replaced"]["userItem"][1]["num"] = 9
        after = build_plan_report(top, data, before["snapshot"])
        row = next(row for row in after["items"] if row["id"] == 11)
        self.assertEqual((row["stock"], row["gained"]), (9, 10))
        self.assertEqual(after["comparison"]["consumed"]["11"], 11)

    def test_new_servant_can_reduce_percentage_but_still_has_gold_increase(self):
        top = root()
        before = build_plan_report(top, metadata())
        top["cache"]["replaced"]["userSvt"][0]["skillLv1"] = 9
        top["cache"]["replaced"]["userSvt"].append(card(3, 0, [1, 1, 1]))
        after = build_plan_report(top, metadata(), before["snapshot"])
        self.assertLess(after["skill_steps"] / after["skill_total"], before["skill_steps"] / before["skill_total"])
        self.assertEqual(after["comparison"]["progress"]["skill_steps"], 1)
        icon = BytesIO()
        Image.new("RGB", (42, 42), "white").save(icon, "PNG")
        icons = {row["id"]: icon.getvalue() for row in after["items"]}
        image = Image.open(BytesIO(base64.b64decode(render_plan_report(after, str(ROOT / "res"), icons))))
        gold = tuple(bytes.fromhex(GOLD[1:]))
        bar = image.crop((40 + 2 * ((WIDTH - 60) // 3), 301, WIDTH - 40, 314))
        self.assertTrue(any(color == gold for _, color in bar.getcolors(bar.width * bar.height)))

    def test_decreases_are_not_progress_and_nine_to_ten_still_counts_consumption(self):
        top = root()
        top["cache"]["replaced"]["userSvt"][0]["skillLv1"] = 9
        before = build_plan_report(top, metadata())
        top["cache"]["replaced"]["userSvt"][0]["skillLv1"] = 10
        top["cache"]["replaced"]["userClassBoardSquare"][0]["classBoardSquareIds"] = []
        after = build_plan_report(top, metadata(), before["snapshot"])
        self.assertEqual(after["comparison"]["progress"], {"lit": 0, "maxed": 0, "skill_steps": 0})
        self.assertEqual(after["comparison"]["consumed"]["6999"], 1)

    def test_board_unlock_and_enhance_spending_is_restored_and_no_gap_row_stays_hidden(self):
        top = root()
        top["cache"]["replaced"]["userClassBoardSquare"][0]["classBoardSquareIds"] = []
        before = build_plan_report(top, metadata())
        top["cache"]["replaced"]["userClassBoardSquare"][0].update(classBoardSquareIds=[1], classBoardUnlockSquareIds=[2])
        after = build_plan_report(top, metadata(), before["snapshot"])
        self.assertEqual(after["comparison"]["consumed"]["10"], 7)
        self.assertEqual(after["comparison"]["progress"]["lit"], 1)
        for row in after["items"]:
            row["missing"] = [0, 0, 0, 0]
        original = render_plan_report(after, str(ROOT / "res"), {})
        for row in after["items"]:
            row["gained"] = 999
        self.assertEqual(original, render_plan_report(after, str(ROOT / "res"), {}))

    def test_first_snapshot_and_account_scope(self):
        first = build_plan_report(root(), metadata())
        self.assertIsNone(first["comparison"])
        previous = deepcopy(first["snapshot"])
        previous["account"] = "other"
        with self.assertRaises(PlanDataError):
            build_plan_report(root(), metadata(), previous)

    def test_append_and_costume_spending_also_counts_toward_material_gain(self):
        data, top = metadata(), root()
        data["servants"]["1"]["appendSkillMaterials"] = {"1": {"items": [{"itemId": 11, "amount": 3}], "qp": 0}}
        data["servants"]["1"]["costumeCosts"] = {"100": {"items": [{"itemId": 11, "amount": 4}], "qp": 0}}
        top["cache"]["replaced"]["userSvtAppendPassiveSkillLv"] = [
            {"userSvtId": 1, "appendPassiveSkillNums": [100], "appendPassiveSkillLvs": [1]}]
        top["cache"]["replaced"]["userSvtCollection"] = [{"svtId": 1, "costumeIds": []}]
        before = build_plan_report(top, data)
        top["cache"]["replaced"]["userSvtAppendPassiveSkillLv"][0]["appendPassiveSkillLvs"] = [2]
        top["cache"]["replaced"]["userSvtCollection"][0]["costumeIds"] = [100]
        top["cache"]["replaced"]["userItem"][1]["num"] = 8
        after = build_plan_report(top, data, before["snapshot"])
        self.assertEqual(after["comparison"]["gains"]["11"], 5)
        self.assertEqual(after["comparison"]["consumed"]["11"], 7)


    def test_unknown_costumes_are_skipped_without_losing_known_costs(self):
        data, top = metadata(), root()
        data["servants"]["1"]["costumeCosts"] = {
            "100": {"items": [{"itemId": 11, "amount": 4}], "qp": 30},
            "11": {"items": [{"itemId": 11, "amount": 999}], "qp": 999},
        }
        top["cache"]["replaced"]["userSvtCollection"] = [{"svtId": 1, "costumeIds": []}]
        before = build_plan_report(top, data)
        top["cache"]["replaced"]["userSvtCollection"][0]["costumeIds"] = [-11, 100, 999]
        after = build_plan_report(top, data, before["snapshot"])
        self.assertEqual(after["comparison"]["consumed"]["11"], 4)
        self.assertEqual(after["comparison"]["consumed"]["1"], 30)
        self.assertEqual(after["comparison"]["gains"]["11"], 4)
        self.assertEqual(after["snapshot"]["costumes"]["1"], [-11, 100, 999])


class PlanAccountTests(unittest.TestCase):
    def test_repeat_query_without_confirmation_shared_mutex_cancel_and_no_raw_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = AccountStore(base / "bindings", base / "private", ROOT / "res/gacha_metadata_cn.json")
            store._plan_store.load = MagicMock(return_value=metadata())
            client = MagicMock()
            client.toplogin.return_value = root()
            user = {"uid": 123, "access_key": "synthetic"}
            with patch.object(store, "client", return_value=client), patch(
                "gacha.accounts.render_plan_report", return_value="image"
            ), patch("gacha.accounts.MaterialService.load_icon", return_value=b"image"):
                result = store.query_plan("12345", "android", dict(user), Event())
                self.assertEqual(result.image_base64, "image")
                repeated = store.query_plan("12345", "android", dict(user), Event())
                self.assertEqual(repeated.image_base64, "image")
                self.assertEqual(client.toplogin.call_count, 2)
                key = store._capture_key("android", user)
                store._capture_active.add(key)
                self.assertIn("其他操作", store.query_plan("12345", "android", dict(user), Event()))
                store._capture_active.remove(key)
                cancel = Event()
                client.toplogin.side_effect = lambda *args: (cancel.set() or root())
                credentials = dict(user)
                with self.assertRaises(ExportError):
                    store.query_plan("12345", "android", credentials, cancel)
                self.assertEqual(credentials, {})
                self.assertEqual(store._capture_active, set())
                self.assertEqual([path.name for path in base.iterdir()], ["plan_history.json"])
                history = json.loads((base / "plan_history.json").read_bytes())
                self.assertEqual(list(history), ["fgo:CN:123"])
                self.assertNotIn("synthetic", json.dumps(history))
                self.assertNotIn("response", json.dumps(history))


class PlanCommandTests(unittest.TestCase):
    def test_aliases_and_worker_credentials_cleanup(self):
        pattern = plugin.FgoGachaPlugin.cmd_plan.__maibot_component_info__.command_pattern
        for value in ("/fgo plan", "/fgo 规划", "/fgo plan  "):
            self.assertIsNotNone(re.fullmatch(pattern, value))
        for value in ("/fgo plan 123", "/fgo planning", "/fgo规划"):
            self.assertIsNone(re.fullmatch(pattern, value))
        saved = {"server": "android", "user": {"uid": 123, "access_key": "synthetic"}}
        workers, jobs = [], []

        def query(*args, **kwargs):
            workers.append(threading.get_ident())
            return "合成报告"

        fake = SimpleNamespace(accounts=SimpleNamespace(load=MagicMock(return_value=saved), query_plan=query),
            config=SimpleNamespace(real_statistics=SimpleNamespace(confirm_repeat_capture=True)),
            _start_account_job=lambda user, stream, operation: jobs.append(operation) or True,
            _send_capture_report=AsyncMock())

        async def run():
            await plugin.FgoGachaPlugin.cmd_plan(fake, "stream", "12345", "qq")
            await jobs[0](Event())

        asyncio.run(run())
        self.assertTrue(workers and all(worker != threading.get_ident() for worker in workers))
        self.assertEqual(saved["user"], {})
        fake._send_capture_report.assert_awaited_once_with("合成报告", "stream")


if __name__ == "__main__":
    unittest.main()
