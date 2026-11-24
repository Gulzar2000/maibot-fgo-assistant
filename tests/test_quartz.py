"""攒石测试使用合成库存和模拟 QQ 发送。"""
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image
import asyncio
import base64
import json
import tempfile
import unittest

from test_accounts import METADATA, TOP
from test_pool_query import ROOT, plugin
from gacha.accounts import AccountStore
from gacha.quartz import CHINA_TZ, merge_quartz_snapshot, quartz_points, read_quartz


def capture(when: datetime, free: int = 25, game_id: str = "10001") -> dict:
    root = deepcopy(TOP)
    root["cache"]["serverTime"] = int(when.timestamp())
    tables = root["cache"]["replaced"]
    tables["userGame"][0].update(userId=game_id, freeStone=free, chargeStone="5", stone=free + 5)
    tables["userItem"] = [{"itemId": 4001, "num": "2"}, {"itemId": "16", "num": "5"}, {"itemId": 11, "num": 900}]
    return root


class QuartzTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.history = self.path / "quartz_history.json"
        self.store = AccountStore(self.path / "bindings", self.path / "private", ROOT / "res/gacha_metadata_cn.json")
        self.now = datetime(2026, 11, 17, 23, 50, tzinfo=CHINA_TZ)
        self.client = SimpleNamespace(server="android", toplogin=MagicMock(return_value=capture(self.now)))
        self.store.client = MagicMock(return_value=self.client)

    def credentials(self):
        return {"uid": "1", "access_key": "synthetic"}

    def test_existing_captures_share_daily_history_and_keep_newest_across_midnight(self):
        metadata = self.path / "test-metadata.json"
        metadata.write_text(json.dumps(METADATA), encoding="utf-8")
        with patch.object(self.store, "metadata", metadata):
            self.store.fetch(self.client, self.credentials(), Event())
            self.client.toplogin.return_value = capture(self.now + timedelta(minutes=4), 45)
            self.store.query_real("12345678", "android", self.credentials(), Event(), confirm_repeat_capture=False)

        self.client.toplogin.return_value = capture(self.now, 65)
        self.store.query_quartz("android", self.credentials(), Event())
        self.client.toplogin.return_value = capture(self.now + timedelta(minutes=10), 55)
        self.store.query_quartz("android", self.credentials(), Event())
        self.client.toplogin.return_value = capture(self.now + timedelta(minutes=10), game_id="10002")
        self.store.query_quartz("android", self.credentials(), Event())

        history = json.loads(self.history.read_bytes())
        self.assertEqual({day: entry["quartz"] for day, entry in history["fgo:CN:10001"].items()},
                         {"2026-11-17": 50, "2026-11-18": 60})
        self.assertEqual(list(history["fgo:CN:10002"]), ["2026-11-18"])

    def test_missing_inventory_preserves_record_but_empty_inventory_is_zero(self):
        self.store.query_quartz("android", self.credentials(), Event())
        before = self.history.read_bytes()
        root = capture(self.now + timedelta(minutes=1), 0)
        tables = root["cache"]["replaced"]
        del tables["userItem"]
        self.client.toplogin.return_value = root
        self.assertIn("库存", self.store.query_quartz("android", self.credentials(), Event()))
        self.assertEqual(self.history.read_bytes(), before)
        tables["userItem"] = []
        self.assertEqual(self.store.query_quartz("android", self.credentials(), Event()),
                         "圣晶石：5 + 3 * 0 + 1/7 * 0 = 5.0")

    def test_reimport_replaces_uncorrected_legacy_counts_and_preserves_latest_valid_capture(self):
        _, older = read_quartz(capture(self.now - timedelta(minutes=5)))
        root = capture(self.now)
        del root["cache"]["replaced"]["userGame"][0]["stone"]
        _, latest = read_quartz(root)
        day = latest.day.isoformat()
        daily = {day: {"captured_at": latest.captured_at, "quartz": 30, "tickets": 2, "fragments": 0}}
        self.assertEqual(quartz_points(daily), [])
        self.assertTrue(merge_quartz_snapshot(daily, older))
        self.assertTrue(merge_quartz_snapshot(daily, latest))
        self.assertFalse(merge_quartz_snapshot(daily, older))
        self.assertFalse(merge_quartz_snapshot(daily, latest))
        self.assertEqual(quartz_points(daily), [latest])
        self.assertEqual(latest.message(), "圣晶石：30 + 3 * 2 + 1/7 * 5 = 36.7")

    def test_bond_plan_and_exchange_record_at_login(self):
        bond_metadata = {"servants": {"1": {}, "2": {}}}
        with patch.object(self.store._bond_store, "load", return_value=bond_metadata), \
                patch("gacha.accounts.build_bond_report", side_effect=ValueError("synthetic report failure")):
            with self.assertRaises(ValueError):
                self.store.query_bond("12345678", "android", self.credentials(), Event())
        self.assertEqual(json.loads(self.history.read_bytes())["fgo:CN:10001"]["2026-11-17"]["quartz"], 30)

        self.client.toplogin.return_value = capture(self.now + timedelta(minutes=1), 35)
        with patch.object(self.store._plan_store, "load", return_value={}), \
                patch("gacha.accounts.build_plan_report", side_effect=ValueError("synthetic report failure")):
            with self.assertRaises(ValueError):
                self.store.query_plan("12345678", "android", self.credentials(), Event())
        self.assertEqual(json.loads(self.history.read_bytes())["fgo:CN:10001"]["2026-11-17"]["quartz"], 40)

        saved = {"server": "android", "user": self.credentials()}
        with patch.object(self.store, "load", return_value=saved), patch.object(self.store, "_profile"), \
                patch("gacha.accounts.SaplingClient") as client:
            def exchange(user, quantity, cancelled, *, on_capture):
                on_capture(capture(self.now + timedelta(minutes=2), 45))
                return "模拟兑换结果"

            client.return_value.exchange.side_effect = exchange
            self.assertEqual(self.store.exchange_saplings("12345678", 1, Event()), "模拟兑换结果")
        self.assertEqual(json.loads(self.history.read_bytes())["fgo:CN:10001"]["2026-11-17"]["quartz"], 50)

    async def test_command_captures_once_and_sends_formula_and_png_in_one_reply(self):
        instance = plugin.FgoGachaPlugin()
        store = plugin.AccountStore(self.store.directory, self.store.profile_dir, self.store.metadata)
        store.client = self.store.client
        instance._accounts = store
        send = SimpleNamespace(text=AsyncMock(return_value=True), hybrid=AsyncMock(return_value=True))
        instance._set_context(SimpleNamespace(send=send))
        saved = {"server": "android", "user": self.credentials()}
        with patch.object(store, "load", return_value=saved):
            self.assertEqual(await instance.cmd_quartz("stream", "12345678", "qq"), (True, "", True))
            await asyncio.gather(*instance._account_jobs.values())
        self.client.toplogin.assert_called_once()
        self.assertEqual(saved["user"], {})
        send.hybrid.assert_awaited_once()
        segments, stream_id = send.hybrid.await_args.args
        self.assertEqual(stream_id, "stream")
        self.assertEqual(segments[0]["content"], "圣晶石：30 + 3 * 2 + 1/7 * 5 = 36.7")
        with Image.open(BytesIO(base64.b64decode(segments[1]["binary_data_base64"]))) as image:
            self.assertEqual(image.format, "PNG")
            image.verify()
