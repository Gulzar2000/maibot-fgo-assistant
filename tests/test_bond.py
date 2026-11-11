"""羁绊奖励、快照归属与取消回归；不登录真实游戏账号。"""
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image
import base64
import asyncio
import json
import re
import tempfile
import threading
import unittest

from test_pool_query import ROOT, plugin
from gacha.accounts import AccountStore, CAPTURE_CONFIRMATION
from gacha.account_protocol.errors import ExportError
from gacha.bond import BondDataError, BondStore, add_noncollection_entities, build_bond_report, compare_snapshots
from gacha.bond_image import render_bond_report


def metadata() -> dict:
    entries = {str(i): {"id": i, "no": i, "name": f"测试从者{i}", "rarity": rarity,
                       "growth": [100 * n for n in range(1, 16)], "excluded": False}
               for i, rarity in enumerate((5, 4, 3, 2, 1, 0), 1)}
    entries["99"] = {"excluded": True}
    return {"schema": 1, "region": "CN", "fetched_at": 0, "servants": entries}


def collection(ident: int, rank: int, cap: int = 10) -> dict:
    return {"svtId": ident, "status": 2, "friendshipRank": rank, "friendship": rank * 100,
            "friendshipExceedCount": cap - 10}


def root(*rows: dict) -> dict:
    return {"response": [{"nid": "login", "resCode": "00"}], "cache": {"serverTime": 1794230000,
            "replaced": {"userGame": [{"userId": 123, "name": "合成测试御主"}],
                         "userItem": [{"itemId": 1000, "num": 3}], "userSvtCollection": list(rows)}}}


class BondTests(unittest.TestCase):
    def test_noble_phantasm_entities_are_not_independent_bond_servants(self):
        data = metadata()
        add_noncollection_entities(data["servants"], [
            {"id": 104001, "collectionNo": 0, "type": 12},
            {"id": 104000, "collectionNo": 264, "type": 1},
            {"id": 1, "collectionNo": 0, "type": 12},
        ])
        report = build_bond_report(root(collection(1, 7), collection(104001, 0)), data)
        self.assertEqual(report["remaining"], 6)
        self.assertEqual(len(report["servants"]), 1)
        self.assertFalse(data["servants"]["1"]["excluded"])
        self.assertNotIn("104000", data["servants"])
        with self.assertRaises(BondDataError):
            build_bond_report(root(collection(104000, 7)), data)

    def test_old_metadata_schema_refreshes_before_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            old = metadata()
            old["fetched_at"] = 9999999999
            path.write_text(json.dumps(old), encoding="utf-8")
            fresh = dict(old, schema=2)
            with patch("gacha.bond.download_metadata", return_value=fresh) as refresh:
                self.assertEqual(BondStore(path).load()["schema"], 2)
                refresh.assert_called_once()

    def test_star_rewards_zero_and_dream_boundaries(self):
        data = metadata()
        report = build_bond_report(root(*(collection(i, 5) for i in range(1, 7))), data)
        self.assertEqual([row["remaining"] for row in report["servants"]], [12, 8, 4, 2, 1, 8])
        for rank, cap, expected in ((7, 10, 6), (10, 10, 0), (10, 13, 90), (11, 13, 60), (15, 15, 0)):
            with self.subTest(rank=rank, cap=cap):
                report = build_bond_report(root(collection(1, rank, cap)), data)
                self.assertEqual(report["remaining"], expected)
                self.assertEqual(report["earned"], 12 - report["normal"] + max(0, rank - 10) * 30)
                self.assertEqual(report["dreamfire_pending"] * 30, report["dream"])
                self.assertEqual(report["dreamfire_used"], report["dreamfire_pending"] + report["dreamfire_rewarded"])
                self.assertEqual(report["dreamfire_inventory"], 3)
                self.assertEqual(report["inventory_quartz"], min(3, 15 - cap) * 30)
                self.assertEqual(report["potential"], report["remaining"] + report["inventory_quartz"])

    def test_inventory_is_actual_stock_not_full_team_capacity(self):
        top = root(collection(1, 11, 13), collection(2, 5))
        top["cache"]["replaced"]["userItem"] = [{"itemId": 1000, "num": 1}]
        report = build_bond_report(top, metadata())
        self.assertEqual(report["dreamfire_pending"], 2)
        self.assertEqual(report["dreamfire_inventory"], 1)
        self.assertEqual(report["inventory_quartz"], 30)
        top["cache"]["replaced"]["userItem"] = []
        self.assertEqual(build_bond_report(top, metadata())["inventory_quartz"], 0)
        del top["cache"]["replaced"]["userItem"]
        with self.assertRaises(ValueError):
            build_bond_report(top, metadata())

    def test_shapes_deduplicate_and_conflicting_progress_fails(self):
        data = metadata()
        data["servants"]["7"] = dict(data["servants"]["1"], id=7)
        report = build_bond_report(root(collection(1, 7), collection(7, 7), collection(99, 0)), data)
        self.assertEqual(len(report["servants"]), 1)
        self.assertEqual(report["remaining"], 6)
        with self.assertRaisesRegex(BondDataError, "冲突"):
            build_bond_report(root(collection(1, 7), collection(7, 8)), data)

    def test_missing_fields_unknown_ids_and_invalid_progress_fail(self):
        for row in (collection(1000, 5), collection(1, 14, 13), collection(1, 5, 16),
                    dict(collection(1, 5), friendship=999999)):
            with self.subTest(row=row), self.assertRaises(BondDataError):
                build_bond_report(root(row), metadata())
        row = collection(1, 5)
        del row["friendshipExceedCount"]
        with self.assertRaises(KeyError):
            build_bond_report(root(row), metadata())

    def test_compare_does_not_treat_net_change_as_earned(self):
        data = metadata()
        old = build_bond_report(root(collection(1, 10, 11)), data)
        new = build_bond_report(root(collection(1, 11, 13), collection(2, 5)), data, old)
        comparison = new["comparison"]
        self.assertEqual(comparison["gained"], 30)
        self.assertEqual(comparison["unlocked"], 60)
        self.assertEqual(comparison["added"], 1)
        self.assertEqual(comparison["delta"], 38)
        old["account"] = "456"
        with self.assertRaisesRegex(BondDataError, "账号不一致"):
            build_bond_report(root(collection(1, 11, 13)), data, old)

    def test_rank_regression_is_not_hidden(self):
        old = build_bond_report(root(collection(1, 11, 13)), metadata())
        new = build_bond_report(root(collection(1, 10, 13)), metadata())
        with self.assertRaisesRegex(BondDataError, "回退"):
            compare_snapshots(new, old)

    def test_image_contains_valid_large_report(self):
        report = build_bond_report(root(collection(1, 11, 13), collection(6, 5)), metadata())
        encoded = render_bond_report(report, str(ROOT / "res"))
        with Image.open(BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.width, 1024)
            self.assertGreater(image.height, 1000)

    def test_metadata_refresh_failure_does_not_silently_use_expired_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(json.dumps(metadata()), encoding="utf-8")
            original = path.read_bytes()
            with patch("gacha.bond.download_metadata", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    BondStore(path).load()
            self.assertEqual(path.read_bytes(), original)


class BondAccountTests(unittest.TestCase):
    def test_daily_prompts_and_pending_confirmations_are_separate(self):
        for real_first in (False, True):
            with self.subTest(real_first=real_first), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                store = AccountStore(base / "bindings", base / "private", ROOT / "res/gacha_metadata_cn.json")
                store._bond_store.load = MagicMock(return_value=metadata())
                client = MagicMock()
                client.toplogin.return_value = root(collection(1, 11, 13))
                user = {"uid": 123, "access_key": "synthetic"}
                key = store._capture_key("android", user)
                real_history = {key: {"day": store._capture_day()}} if real_first else {}
                (base / "capture_history.json").write_text(json.dumps(real_history), encoding="utf-8")
                with patch.object(store, "client", return_value=client), patch.object(
                    store, "_fetch", return_value="real report"
                ) as real_fetch, patch("gacha.accounts.render_bond_report", return_value="image"):
                    result = store.query_bond("12345", "android", dict(user), Event())
                    self.assertNotEqual(result, CAPTURE_CONFIRMATION)
                    self.assertEqual(json.loads((base / "capture_history.json").read_bytes()), real_history)
                    real_result = store.query_real("12345", "android", dict(user), Event())
                    self.assertEqual(real_result, CAPTURE_CONFIRMATION if real_first else "real report")
                    self.assertEqual(store.query_bond("12345", "android", dict(user), Event()), CAPTURE_CONFIRMATION)
                    if real_first:
                        self.assertIn("12345", store._capture_pending)
                        self.assertIn("12345", store._bond_pending)
                        self.assertEqual(store.query_real("12345", "android", dict(user), Event()), "real report")
                        self.assertIn("12345", store._bond_pending)
                        self.assertNotEqual(store.query_bond("12345", "android", dict(user), Event()), CAPTURE_CONFIRMATION)
                    real_fetch.assert_called_once()

    def test_account_snapshot_confirmation_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = AccountStore(base / "bindings", base / "private", ROOT / "res/gacha_metadata_cn.json")
            store._bond_store.load = MagicMock(return_value=metadata())
            client = MagicMock()
            client.toplogin.return_value = root(collection(1, 11, 13))
            user = {"uid": 123, "access_key": "synthetic"}
            with patch.object(store, "client", return_value=client), patch(
                "gacha.accounts.render_bond_report", return_value="image"
            ):
                result = store.query_bond("12345", "android", dict(user), Event(), confirm_repeat_capture=False)
                self.assertIn("60", result)
                saved = json.loads((base / "bond_history.json").read_bytes())
                self.assertEqual(list(saved), ["fgo:CN:123"])
                self.assertNotIn("access_key", json.dumps(saved))
                self.assertNotIn("synthetic", json.dumps(saved))
                original = (base / "bond_history.json").read_bytes()
                result = store.query_bond("12345", "android", dict(user), Event())
                self.assertEqual(result, CAPTURE_CONFIRMATION)
                self.assertEqual(client.toplogin.call_count, 1)
                cancel = Event()
                client.toplogin.side_effect = lambda *args: (cancel.set() or root(collection(1, 12, 13)))
                credentials = dict(user)
                with self.assertRaises(ExportError):
                    store.query_bond("12345", "android", credentials, cancel)
                self.assertEqual(credentials, {})
                self.assertEqual((base / "bond_history.json").read_bytes(), original)
                self.assertEqual(store._capture_active, set())


class BondCommandTests(unittest.TestCase):
    def test_only_requested_command_aliases_match(self):
        pattern = plugin.FgoGachaPlugin.cmd_bond.__maibot_component_info__.command_pattern
        for text in ("/fgo 羁绊计算", "/fgo 挖矿", "/fgo 挖矿  "):
            self.assertIsNotNone(re.fullmatch(pattern, text))
        for text in ("/fgo 羁绊", "/fgo 挖矿别人", "/fgo 挖矿 123"):
            self.assertIsNone(re.fullmatch(pattern, text))

    def test_handler_uses_worker_and_clears_loaded_credentials(self):
        main_thread = threading.get_ident()
        workers = []
        saved = {"server": "android", "user": {"uid": 123, "access_key": "synthetic"}}

        def query(*args, **kwargs):
            workers.append(threading.get_ident())
            return "合成报告"

        jobs = []
        fake = SimpleNamespace(
            accounts=SimpleNamespace(load=MagicMock(return_value=saved), query_bond=query),
            config=SimpleNamespace(real_statistics=SimpleNamespace(confirm_repeat_capture=True)),
            _start_account_job=lambda user, stream, operation: jobs.append(operation) or True,
            _send_capture_report=AsyncMock(),
        )

        async def run():
            await plugin.FgoGachaPlugin.cmd_bond(fake, "stream", "12345", "qq")
            await jobs[0](Event())

        asyncio.run(run())
        self.assertTrue(all(worker != main_thread for worker in workers))
        self.assertEqual(len(workers), 1)
        self.assertEqual(saved["user"], {})
        fake._send_capture_report.assert_awaited_once_with("合成报告", "stream")


if __name__ == "__main__":
    unittest.main()
