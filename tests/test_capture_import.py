"""原始抓包导入必须隔离账号、保留旧摘要，并支持重复执行。"""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from test_accounts import METADATA, TOP
from gacha.capture_import import game_history_key, import_captures


class CaptureImportTests(unittest.TestCase):
    def test_import_is_account_scoped_preserves_legacy_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captures = root / "captures"
            captures.mkdir()
            first = deepcopy(TOP)
            first["cache"]["replaced"]["userGame"][0]["usk"] = "synthetic-secret-not-to-store"
            second = deepcopy(TOP)
            second["cache"]["replaced"]["userGame"][0]["userId"] = "20002"
            for index, capture in enumerate((first, second)):
                (captures / f"capture{index}.json").write_text(json.dumps(capture), encoding="utf-8")
            metadata = root / "metadata.json"
            metadata.write_text(json.dumps(METADATA), encoding="utf-8")
            old = {"day": "2026-11-07", "total_draws": 1100, "ssr_count": 17, "estimated": False}
            history_path = root / "history.json"
            original = json.dumps({"bound": {"day": old["day"], "summary": old}, "other": {"day": "2026-11-06"}}).encode()
            history_path.write_bytes(original)
            mapping = {game_history_key(first): "bound"}
            result = import_captures(captures, metadata, history_path, mapping)
            history = json.loads(history_path.read_bytes())
            self.assertEqual(result["accounts"], 2)
            self.assertEqual(Path(result["backup"]).read_bytes(), original)
            self.assertEqual(history[game_history_key(first)]["snapshots"][-1], old)
            self.assertEqual(len(history[game_history_key(first)]["snapshots"]), 2)
            self.assertNotIn("summary", history["bound"])
            self.assertEqual(history["other"], {"day": "2026-11-06"})
            self.assertNotIn("synthetic-secret", history_path.read_text())
            self.assertEqual(len(history[game_history_key(second)]["snapshots"]), 1)
            import_captures(captures, metadata, history_path, mapping)
            self.assertEqual(json.loads(history_path.read_bytes()), history)
