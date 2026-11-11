"""同一游戏账号的每日确认使用持久日期记录，测试不访问游戏网络。"""
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import tempfile
import unittest

from test_accounts import METADATA, TOP
from gacha.accounts import CAPTURE_CONFIRMATION, AccountStore
from gacha.account_protocol.errors import ExportError


class CaptureConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        metadata = root / "metadata.json"
        metadata.write_text(json.dumps(METADATA), encoding="utf-8")
        self.store = AccountStore(root / "bindings", root / "private", metadata)
        self.client = SimpleNamespace(server="android", toplogin=MagicMock(return_value=deepcopy(TOP)))
        self.store.client = MagicMock(return_value=self.client)
        day = patch.object(AccountStore, "_capture_day", return_value="2026-11-03")
        self.day = day.start()
        self.addCleanup(day.stop)

    def query(self, qq="12345678", uid="10001", **kwargs):
        return self.store.query_real(qq, "android", {"uid": uid, "access_key": "synthetic"}, Event(), **kwargs)

    def test_repeat_confirmation_can_be_disabled_and_report_has_no_extra_grade_line(self):
        self.query()
        report = self.query(confirm_repeat_capture=False)
        self.assertEqual(len(report.splitlines()), 4)
        self.assertTrue(report.endswith("评级为 Grand 欧皇"))
        self.query(confirm_repeat_capture=False)
        self.assertEqual(self.client.toplogin.call_count, 3)
        self.assertEqual(self.query(), CAPTURE_CONFIRMATION)

    def test_first_capture_then_prompt_then_confirmation_then_prompt_again(self):
        self.assertIn("真实出率", self.query())
        self.assertEqual(self.query(), CAPTURE_CONFIRMATION)
        self.assertEqual(self.client.toplogin.call_count, 1)
        self.assertEqual(self.store.client.call_count, 1)
        self.assertIn("真实出率", self.query())
        self.assertEqual(self.client.toplogin.call_count, 2)
        self.assertEqual(self.query(), CAPTURE_CONFIRMATION)

    def test_same_account_other_qq_still_needs_own_confirmation(self):
        self.query()
        self.assertEqual(self.query(), CAPTURE_CONFIRMATION)
        self.assertEqual(self.query(qq="23456789"), CAPTURE_CONFIRMATION)
        self.assertEqual(self.client.toplogin.call_count, 1)
        self.assertIn("真实出率", self.query(qq="23456789"))
        self.assertIn("真实出率", self.query(uid="20002"))

    def test_history_survives_reload_and_new_day_does_not_need_confirmation(self):
        first = self.query()
        self.assertNotIn("上一次抓包时间", first)
        restored = AccountStore(self.store.directory, self.store.profile_dir, self.store.metadata)
        restored.client = self.store.client
        self.assertEqual(restored.query_real("12345678", "android", {"uid": "10001", "access_key": "synthetic"}, Event()),
                         CAPTURE_CONFIRMATION)
        self.day.return_value = "2026-11-04"
        report = restored.query_real("12345678", "android", {"uid": "10001", "access_key": "synthetic"}, Event())
        self.assertNotIn("上一次抓包时间", report)
        self.assertTrue(report.image_base64)
        history = restored._capture_history()
        points = history["fgo:CN:10001"]["snapshots"]
        self.assertEqual([point["day"] for point in points], ["2026-11-03", "2026-11-04"])
        self.assertEqual(self.client.toplogin.call_count, 2)

    def test_unbind_only_removes_credentials_and_rebinding_same_game_keeps_history(self):
        user = {"uid": "10001", "access_key": "synthetic"}
        self.store.save("12345678", "android", user, Event())
        self.query()
        before = (self.store.directory.parent / "capture_history.json").read_bytes()
        self.store.delete("12345678")
        self.assertIsNone(self.store.load("12345678"))
        self.assertEqual((self.store.directory.parent / "capture_history.json").read_bytes(), before)
        self.store.save("12345678", "android", user, Event())
        self.query(confirm_repeat_capture=False)
        history = self.store._capture_history()
        self.assertEqual(len(history["fgo:CN:10001"]["snapshots"]), 2)

    def test_old_date_only_record_keeps_confirmation_without_inventing_previous_rate(self):
        key = self.store._capture_key("android", {"uid": "10001", "access_key": "synthetic"})
        (self.store.directory.parent / "capture_history.json").write_text(json.dumps({key: "2026-11-03"}), encoding="utf-8")
        self.assertEqual(self.query(), CAPTURE_CONFIRMATION)
        self.assertNotIn("上一次抓包时间", self.query())

    def test_legacy_summary_is_migrated_before_appending_each_capture(self):
        key = self.store._capture_key("android", {"uid": "10001", "access_key": "synthetic"})
        old = {"day": "2026-11-02", "total_draws": 900, "ssr_count": 14, "estimated": True}
        path = self.store.directory.parent / "capture_history.json"
        path.write_text(json.dumps({key: {"day": old["day"], "summary": old}}), encoding="utf-8")
        self.query(confirm_repeat_capture=False)
        self.query(confirm_repeat_capture=False)
        points = self.store._capture_history()["fgo:CN:10001"]["snapshots"]
        self.assertEqual(points[0], old)
        self.assertEqual(len(points), 3)
        self.assertEqual([p["total_draws"] for p in points], [900, 1000, 1000])

    def test_capture_attaches_only_matching_fgo_id_archive_without_duplicate_imports(self):
        old = {"day": "2026-11-01", "total_draws": 900, "ssr_count": 14,
               "estimated": True, "source_sha256": "synthetic"}
        path = self.store.directory.parent / "capture_history.json"
        path.write_text(json.dumps({
            "fgo:CN:10001": {"day": old["day"], "snapshots": [old]},
            "fgo:CN:20002": {"day": old["day"], "snapshots": [{**old, "total_draws": 2000}]},
        }), encoding="utf-8")
        self.query(confirm_repeat_capture=False)
        self.query(confirm_repeat_capture=False)
        key = self.store._capture_key("android", {"uid": "10001", "access_key": "synthetic"})
        entry = self.store._capture_history()[key]
        self.assertEqual(entry["fgo_history_key"], "fgo:CN:10001")
        self.assertEqual([point["total_draws"] for point in self.store._capture_history()["fgo:CN:10001"]["snapshots"]], [900, 1000, 1000])

    def test_same_qq_switching_game_accounts_keeps_each_history_separate(self):
        self.query(confirm_repeat_capture=False)
        self.store.delete("12345678")
        other = deepcopy(TOP)
        other["cache"]["replaced"]["userGame"][0]["userId"] = "20002"
        other["cache"]["replaced"]["userGacha"][0]["num"] = 1990
        self.client.toplogin.return_value = other
        self.query(confirm_repeat_capture=False)
        history = self.store._capture_history()
        self.assertEqual([p["total_draws"] for p in history["fgo:CN:10001"]["snapshots"]], [1000])
        self.assertEqual([p["total_draws"] for p in history["fgo:CN:20002"]["snapshots"]], [2000])
        self.client.toplogin.return_value = deepcopy(TOP)
        self.query(qq="23456789", uid="30003", confirm_repeat_capture=False)
        history = self.store._capture_history()
        self.assertEqual(len(history["fgo:CN:10001"]["snapshots"]), 2)
        self.assertEqual(len(history["fgo:CN:20002"]["snapshots"]), 1)

    def test_failed_login_does_not_count_and_active_account_is_not_requested_again(self):
        self.client.toplogin.side_effect = ExportError("REMOTE_TIMEOUT")
        with self.assertRaises(ExportError):
            self.query()
        self.assertFalse(self.store._capture_active)
        self.client.toplogin.side_effect = None
        self.assertIn("真实出率", self.query())
        key = self.store._capture_key("android", {"uid": "10001", "access_key": "synthetic"})
        self.store._capture_active.add(key)
        self.assertIn("正在抓包", self.query(qq="23456789"))
        self.assertEqual(self.client.toplogin.call_count, 2)

    def test_successful_capture_is_counted_even_if_statistics_fail(self):
        self.store.metadata.write_text("invalid json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.query()
        self.assertEqual(self.query(), CAPTURE_CONFIRMATION)
        self.assertEqual(self.client.toplogin.call_count, 1)
