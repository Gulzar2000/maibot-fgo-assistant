"""用模拟的官方响应验证同一会话里的预检和购买。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import hashlib
import json
import unittest

from test_pool_query import ROOT  # 加载测试使用的插件路径
from gacha.account_protocol.errors import ExportError
from gacha.saplings import SaplingClient


class SaplingExchangeTests(unittest.TestCase):
    def run_exchange(self, requested, *, ap=95, saplings=8, timeout=False, server="android", server_time=1700000000,
                     maintenance_at=None):
        profile = SimpleNamespace(version="test", development_code="test", session_prefix="prefix")
        client = SaplingClient(profile, server)
        host, channel, platform = {
            "android": ("line3-s2-bili-fate.bilibiligame.net", "24", "3"),
            "ios": ("line3-s2-ios-fate.bilibiligame.net", "996", "2"),
            "uo": ("line3-s1-uo-fate.bilibiligame.net", "24", "3"),
        }[server]
        calls = []
        self.calls = calls
        user = {"uid": "1", "access_key": "synthetic"}

        def response(nid, success, **fields):
            return dict(nid=nid, resCode="00", success=success, **fields)

        def request(url, params):
            key = params.get("key", params["type"] if "type" in params else "")
            calls.append((key, dict(params)))
            if key == maintenance_at:
                return b'{"response":[{"nid":"","resCode":"88","fail":{"action":"maintain"}}]}'
            self.assertEqual(urlsplit(url).hostname, host)
            self.assertEqual(params["rkchannel"], channel)
            if key in ("home", "shoppurchase"):
                self.assertEqual(params["cPlat"], "3")
                self.assertEqual(params["uPlat"], platform)
            if url.endswith("logintomembercenter.php"):
                root = {"response": [response("login", dict(rguid="1", rgusk="fake", dateVer="1", dataVer="1"))]}
            elif url.endswith("/login.php"):
                root = {"response": [response("login", dict(sguid="42", sgusk="initial"))]}
            elif key == "toplogin":
                root = {"response": [response("login", {}, usk="top-token")], "cache": {
                    "serverTime": server_time, "replaced": {
                        "userGame": [dict(userId="42", actMax=147, actRecoverAt=server_time + (147 - ap) * 300,
                                          carryOverActPoint=0)],
                        "userItem": [dict(itemId="103", num=saplings), dict(itemId="104", num=5)],
                    },
                }}
            elif key == "home":
                sleep.assert_called_once_with(2)
                root = {"response": [response("other", {}, usk="home-token"), response("home", {})],
                        "cache": {"serverTime": server_time, "updated": {"userGame": [dict(userId="42")]}}}
            else:
                self.assertEqual(key, "shoppurchase")
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["_key"], ["shoppurchase"])
                self.assertEqual(query["_userId"], ["42"])
                self.assertEqual(params["id"], "13000000")
                self.assertEqual(params["usk"], hashlib.md5(b"prefixhome-token").hexdigest())
                self.assertEqual(params["deviceid"], calls[0][1]["deviceid"])
                self.assertNotEqual(params["idempotencyKey"], calls[-2][1]["idempotencyKey"])
                if timeout:
                    raise ExportError("REMOTE_TIMEOUT")
                num = int(params["num"])
                root = {"response": [response("purchase", dict(PurchaseNum=num))], "cache": {
                    "serverTime": server_time, "updated": {
                        "userGame": [dict(userId="42", actRecoverAt=server_time + (147 - ap + 40 * num) * 300)],
                        "userItem": [dict(itemId="103", num=saplings - num), dict(itemId="104", num=5 + num)],
                    },
                }}
            return json.dumps(root).encode()

        with patch("gacha.saplings.time.sleep") as sleep, patch.object(client, "_request", side_effect=request):
            try:
                return client.exchange(user, requested, lambda: False)
            finally:
                self.assertEqual(user, {})
                self.assertFalse(client._session)

    def test_default_uses_ap_and_inventory_in_one_login(self):
        self.assertIn("获得 2 个青铜果实", self.run_exchange(None))
        self.assertEqual([key for key, _ in self.calls], ["token", "login", "toplogin", "home", "shoppurchase"])
        self.assertIn("获得 1 个青铜果实", self.run_exchange(None, saplings=1))

    def test_ios_and_uo_use_their_bound_server_for_the_entire_exchange(self):
        for server in ("ios", "uo"):
            with self.subTest(server=server):
                self.assertIn("获得 2 个青铜果实", self.run_exchange(None, server=server))
                self.assertEqual([key for key, _ in self.calls], ["token", "login", "toplogin", "home", "shoppurchase"])

    def test_explicit_quantity_and_insufficient_ap_or_inventory(self):
        self.assertIn("获得 1 个青铜果实", self.run_exchange(1))
        self.assertIn("AP 不足", self.run_exchange(3))
        self.assertNotIn("shoppurchase", [key for key, _ in self.calls])
        self.assertIn("只有 1 株", self.run_exchange(2, saplings=1))
        self.assertNotIn("shoppurchase", [key for key, _ in self.calls])

    def test_timeout_after_submission_is_not_retried(self):
        with self.assertRaisesRegex(ExportError, "PURCHASE_RESULT_UNCONFIRMED"):
            self.run_exchange(None, timeout=True)
        self.assertEqual([key for key, _ in self.calls].count("shoppurchase"), 1)

    def test_maintenance_response_without_nid_is_recognized_for_game_actions(self):
        for stage in ("home", "shoppurchase"):
            with self.subTest(stage=stage), self.assertRaisesRegex(ExportError, "SERVER_MAINTENANCE"):
                self.run_exchange(None, maintenance_at=stage)

    def test_recovery_time_uses_post_purchase_ap_and_beijing_date(self):
        zone = timezone(timedelta(hours=8))
        morning = int(datetime(2026, 11, 7, 8, 0, tzinfo=zone).timestamp())
        evening = int(datetime(2026, 11, 7, 20, 0, tzinfo=zone).timestamp())
        self.assertIn("预计AP将于今天15:40回满。", self.run_exchange(1, server_time=morning))
        self.assertIn("预计AP将于明天03:40回满。", self.run_exchange(1, server_time=evening))

    def test_insufficient_ap_also_includes_recovery_time_without_purchase(self):
        zone = timezone(timedelta(hours=8))
        evening = int(datetime(2026, 11, 7, 20, 0, tzinfo=zone).timestamp())
        reply = self.run_exchange(None, ap=39, server_time=evening)
        self.assertIn("AP 不足", reply)
        self.assertIn("预计AP将于明天05:00回满。", reply)
        self.assertIn("预计AP将于今天20:05恢复到40AP。", reply)
        self.assertNotIn("shoppurchase", [key for key, _ in self.calls])

    def test_recovery_to_40_uses_remaining_ap_and_rounds_up_across_midnight(self):
        zone = timezone(timedelta(hours=8))
        evening = int(datetime(2026, 11, 7, 23, 0, 10, tzinfo=zone).timestamp())
        reply = self.run_exchange(None, ap=95, server_time=evening)
        self.assertIn("剩余 AP：15", reply)
        self.assertIn("预计AP将于明天01:06恢复到40AP。", reply)
        self.assertIn("当前AP已达到40。", self.run_exchange(1, ap=95, server_time=evening))
