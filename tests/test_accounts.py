"""合成账号测试，不调用官方登录，不使用任何真实用户凭据。"""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image, ImageChops
import asyncio
import base64
import json
import tempfile
import unittest

from test_pool_query import ROOT, plugin
from gacha.accounts import AccountStore, qr_image
from gacha.account_protocol.errors import ExportError
from gacha.account_protocol.protocol import GameClient
from gacha.credentials import protect
from gacha.real_statistics import format_real_statistics


METADATA = {"schemaVersion": 1, "region": "CN", "gachas": [
    {"id": 100, "type": 1}, {"id": 101, "type": 7}, {"id": 102, "type": 3},
], "servants": [{"id": 1, "ssr": True}, {"id": 2, "ssr": False}]}
TOP = {"response": [{"nid": "login", "resCode": "00"}], "cache": {"serverTime": 1700000000, "replaced": {
    "userGame": [{"userId": "10001", "name": "测试御主", "friendCode": "001234567890",
                  "actMax": "147", "actRecoverAt": "1700026101", "carryOverActPoint": "0"}],
    "userGacha": [{"gachaId": 100, "num": 990}, {"gachaId": 101, "num": 11},
                  {"gachaId": 102, "num": 500}, {"gachaId": 1, "num": 200}],
    "userSvtCollection": [{"svtId": 1, "status": 2, "totalGetNum": 16},
                          {"svtId": 2, "status": 2, "totalGetNum": 100}],
}}}


class RealStatisticsTests(unittest.TestCase):
    def test_counts_and_deducts_one_ssr_and_pull_per_lucky_bag(self):
        text = format_real_statistics(TOP, METADATA)
        self.assertTrue(text.startswith("昵称：测试御主\n好友码：001234567890\n当前 AP：59/147\n真实出率："))
        self.assertIn("1.50%（15 ★5/1000 抽）", text)
        self.assertIn("Grand 欧皇", text)
        self.assertNotIn("福袋", text)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertTrue(text.endswith("评级为 Grand 欧皇"))

    def test_capture_local_gacha_types_and_unknown_metadata(self):
        root = deepcopy(TOP)
        root["cache"]["replaced"]["userGacha"].append({"gachaId": 999, "num": 10})
        self.assertTrue(format_real_statistics(root, METADATA).endswith("评级为 Grand 欧皇（暂估）"))
        root["cache"]["replaced"]["mstGacha"] = [{"id": 999, "type": 3}]
        self.assertEqual(format_real_statistics(root, METADATA), format_real_statistics(TOP, METADATA))
        root["cache"]["replaced"]["userSvtCollection"].append({"svtId": 999, "status": 2, "totalGetNum": 1})
        with self.assertLogs("gacha.real_statistics", level="WARNING") as captured:
            self.assertIn("无法计算", format_real_statistics(root, METADATA))
        self.assertIn("svt_ids=[999]", captured.output[0])
        self.assertIn("好友码：001234567890", format_real_statistics(root, METADATA))

    def test_new_craft_essence_is_known_but_does_not_increase_ssr_count(self):
        index = json.loads((ROOT / "res/gacha_metadata_cn.json").read_bytes())
        essence = next(row for row in index["servants"] if row["id"] == 9312600)
        self.assertFalse(essence["ssr"])
        metadata, root = deepcopy(METADATA), deepcopy(TOP)
        metadata["servants"].append(essence)
        root["cache"]["replaced"]["userSvtCollection"].append({"svtId": 9312600, "status": 2, "totalGetNum": 5})
        self.assertEqual(format_real_statistics(root, metadata), format_real_statistics(TOP, METADATA))

    def test_identity_comes_from_current_capture_and_is_kept_for_zero_draws(self):
        root = deepcopy(TOP)
        root["cache"]["replaced"]["userGame"][0].update(name="另一位御主", friendCode="009876543210")
        root["cache"]["replaced"]["userGacha"] = []
        root["cache"]["replaced"]["userSvtCollection"] = []
        self.assertEqual(format_real_statistics(root, METADATA),
                         "昵称：另一位御主\n好友码：009876543210\n当前 AP：59/147\n真实出率：暂无可统计的有效抽卡记录。")

    def test_ap_keeps_overflow_and_omits_line_for_legacy_capture(self):
        root = deepcopy(TOP)
        account = root["cache"]["replaced"]["userGame"][0]
        account.update(actRecoverAt="1699999999", carryOverActPoint="12")
        self.assertIn("当前 AP：159/147", format_real_statistics(root, METADATA))
        del account["actRecoverAt"]
        self.assertNotIn("当前 AP：", format_real_statistics(root, METADATA))

    def test_missing_capture_identity_is_exposed(self):
        root = deepcopy(TOP)
        del root["cache"]["replaced"]["userGame"][0]["friendCode"]
        with self.assertRaises(KeyError):
            format_real_statistics(root, METADATA)

    def test_missing_or_invalid_counts_are_not_reported_as_zero(self):
        root = deepcopy(TOP)
        del root["cache"]["replaced"]["userGacha"]
        with self.assertRaises(ValueError):
            format_real_statistics(root, METADATA)
        root = deepcopy(TOP)
        root["cache"]["replaced"]["userGacha"][0]["num"] = -1
        with self.assertRaises(ValueError):
            format_real_statistics(root, METADATA)


class CredentialTests(unittest.TestCase):
    def test_maintenance_marker_stops_login_at_first_request(self):
        client = GameClient(SimpleNamespace(version="test", development_code="test"), "android")
        raw = b'{"response":[{"nid":"","resCode":"88","fail":{"action":"maintain"}}]}'
        user = {"uid": "1", "access_key": "synthetic"}
        with patch.object(client, "_request", return_value=raw) as request:
            with self.assertRaisesRegex(ExportError, "SERVER_MAINTENANCE"):
                client.toplogin(user, lambda phase: None, lambda: False)
            request.assert_called_once()
        self.assertEqual(user, {})
        # 同一个 resCode 缺少维护标记时，保留原错误，不误报维护。
        raw = b'{"response":[{"nid":"","resCode":"88","fail":{"action":"other"}}]}'
        with patch.object(client, "_request", return_value=raw):
            with self.assertRaisesRegex(ExportError, "GAME_REJECTED_88"):
                client._game("/test", {})

    def test_game_handoff_returns_raw_toplogin_and_clears_session_credentials(self):
        profile = SimpleNamespace(version="2.129.0", development_code="synthetic", session_prefix="synthetic")
        client = GameClient(profile, "android")
        member = {"dateVer": "1", "dataVer": "1", "rguid": "2", "rgusk": "synthetic"}
        login = {"sguid": "3", "sgusk": "synthetic"}
        client._game = MagicMock(side_effect=[({}, member), ({}, login), (deepcopy(TOP), {})])
        user = {"uid": "1", "access_key": "synthetic"}
        root = client.toplogin(user, lambda phase: None, lambda: False)
        self.assertEqual(root, TOP)
        self.assertEqual(client._game.call_count, 3)
        self.assertEqual(user, {})
        self.assertEqual(member, {})
        self.assertEqual(login, {})

    def test_real_dpapi_roundtrip_isolation_tampering_delete_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(Path(directory), ROOT / "private", ROOT / "res/gacha_metadata_cn.json")
            user = {"uid": "10001", "access_key": "synthetic-test-token"}
            store.save("12345678", "android", user, Event())
            self.assertNotIn(b"synthetic-test-token", store.path("12345678").read_bytes())
            self.assertEqual(store.load("12345678")["user"], user)
            self.assertIsNone(store.load("23456789"))
            raw = bytearray(store.path("12345678").read_bytes())
            raw[-1] ^= 1
            with self.assertRaises(ExportError):
                protect(bytes(raw), True)
            store.delete("12345678")
            self.assertIsNone(store.load("12345678"))
            cancel = Event()
            cancel.set()
            with self.assertRaises(ExportError):
                store.save("12345678", "android", user, cancel)
            self.assertIsNone(store.load("12345678"))

    def test_qr_generated_in_memory(self):
        import qrcode

        payload = "https://example.com/synthetic-login"
        value = qr_image(payload, "测试 QQ 昵称")
        with Image.open(BytesIO(base64.b64decode(value))) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 100)
            baseline = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
            baseline.add_data(payload)
            baseline.make(fit=True)
            code = baseline.make_image(fill_color="black", back_color="white").convert("RGB")
            crop = image.crop((64, 128, 64 + code.width, 128 + code.height)).convert("RGB")
            self.assertIsNone(ImageChops.difference(crop, code).getbbox())
            self.assertEqual(crop.crop((0, 0, code.width, 40)).getextrema(), ((255, 255),) * 3)


class AccountCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = plugin.FgoGachaPlugin()
        self.instance.set_plugin_config({
            "plugin": {"config_version": "1.0.4"},
            "real_statistics": {"confirm_repeat_capture": False},
        })
        self.client = SimpleNamespace(generate=MagicMock(return_value=("synthetic-ticket", "synthetic-url")),
                                      poll=MagicMock(return_value=("authorized", {"uid": "1", "access_key": "test"})))
        self.store = SimpleNamespace(client=MagicMock(return_value=self.client), fetch=MagicMock(return_value="真实出率：1.50%"),
                                     save=MagicMock(), delete=MagicMock(), load=MagicMock(return_value=None),
                                     query_real=MagicMock(return_value="真实出率：1.50%"))
        self.instance._accounts = self.store
        self.instance._pool_manager = SimpleNamespace(get_user_statistics=MagicMock(return_value=(100, 1)))
        self.send = SimpleNamespace(text=AsyncMock(return_value=True), image=AsyncMock(return_value=True), hybrid=AsyncMock(return_value=True))
        self.instance._ctx = SimpleNamespace(send=self.send, chat=SimpleNamespace(
            get_stream_by_user_id=AsyncMock(return_value={"session_id": "private", "is_group_session": False}),
            get_stream_by_group_id=AsyncMock(return_value={"session_id": "group", "is_group_session": True})))

    async def drain(self):
        await asyncio.gather(*list(self.instance._account_jobs.values()))

    async def test_maintenance_message_is_used_for_bind_real_and_tree(self):
        self.store.load.return_value = {"server": "android", "user": {"uid": "1", "access_key": "test"}}
        self.store.query_real.side_effect = plugin.ExportError("SERVER_MAINTENANCE")
        await self.instance.cmd_stat_real("group", "12345678", platform="qq")
        await self.drain()
        self.assertEqual(self.send.text.call_args.args[0], plugin.MAINTENANCE_MESSAGE)
        self.store.exchange_saplings = MagicMock(side_effect=plugin.ExportError("SERVER_MAINTENANCE"))
        await self.instance.cmd_tree("group", "12345678", "qq", text="/fgo tree")
        await self.drain()
        self.assertEqual(self.send.text.call_args.args[0], plugin.MAINTENANCE_MESSAGE)
        self.store.fetch.side_effect = plugin.ExportError("SERVER_MAINTENANCE")
        with patch.object(plugin, "qr_image", return_value="synthetic-png"):
            await self.bind()
            await self.drain()
        self.assertEqual(self.send.text.call_args.args[0], plugin.MAINTENANCE_MESSAGE)
        self.store.save.assert_not_called()

    async def test_tree_passes_optional_quantity_from_qq_command(self):
        self.store.exchange_saplings = MagicMock(return_value="兑换成功")
        for command, quantity in (("/fgo tree", None), ("/fgo tree 2", 2),
                                  ("/fgo 种树", None), ("/fgo 种树 3", 3)):
            await self.instance.cmd_tree("group", "12345678", "qq", text=command)
            await self.drain()
            self.assertEqual(self.store.exchange_saplings.call_args.args[:2], ("12345678", quantity))
            self.assertEqual(self.send.text.call_args.args, ("兑换成功", "group"))
        self.store.exchange_saplings.reset_mock()
        result = await self.instance.cmd_tree("group", "12345678", "qq", text="/fgo tree 0")
        self.assertIn("正整数", result[1])
        self.store.exchange_saplings.assert_not_called()

    async def bind(self, **extra):
        args = dict(stream_id="private", user_id="12345678", platform="qq", text="/fgo bind 1",
                    message={"message_info": {"user_info": {"user_nickname": "测试 QQ 昵称"}}})
        args.update(extra)
        return await self.instance.cmd_bind(**args)

    async def test_unsupported_platform_and_unconfirmed_stream_never_generate_qr(self):
        self.assertIn("私聊", (await self.bind(platform="other"))[1])
        self.instance.ctx.chat.get_stream_by_user_id.return_value = None
        self.assertIn("未开始", (await self.bind())[1])
        self.store.client.assert_not_called()

    async def test_bind_without_server_only_shows_choices_and_never_starts_login(self):
        for stream_id, group_id in (("private", ""), ("group", "100")):
            result = await self.bind(stream_id=stream_id, group_id=group_id, text="/fgo bind")
            for command, name in (("1", "哔哩安卓"), ("2", "iOS"), ("3", "享游安卓")):
                self.assertIn("/fgo bind " + command + " — " + name, result[1])
            self.assertFalse(self.instance._account_jobs)
        self.store.client.assert_not_called()
        self.send.hybrid.assert_not_awaited()
        self.instance.ctx.chat.get_stream_by_user_id.assert_not_awaited()
        self.instance.ctx.chat.get_stream_by_group_id.assert_not_awaited()

    async def test_bind_saves_sender_only_after_success_and_no_qr_storage(self):
        with patch.object(plugin, "qr_image", return_value="synthetic-png") as render:
            await self.bind(text="/fgo bind 2")
            await self.drain()
        render.assert_called_once_with("synthetic-url", "测试 QQ 昵称")
        self.store.client.assert_called_once_with("ios")
        self.store.save.assert_called_once()
        self.assertEqual(self.store.save.call_args.args[:2], ("12345678", "ios"))
        segments = self.send.hybrid.call_args.args[0]
        self.assertEqual([segment["type"] for segment in segments], ["text", "image"])
        self.assertEqual(segments[1]["binary_data_base64"], "synthetic-png")
        self.assertEqual(self.send.hybrid.call_args.args[1], "private")
        self.assertEqual(self.send.hybrid.call_args.kwargs, {"storage_message": False})
        self.send.image.assert_not_awaited()
        self.assertEqual(self.send.text.call_args.args[0], "FGO 账号绑定成功。\n真实出率：1.50%")
        self.store.fetch.assert_called_once()
        self.assertNotIn("grade_messages", self.store.fetch.call_args.kwargs)

    async def test_chinese_bind_parses_all_servers_and_missing_server_only_shows_choices(self):
        result = await self.bind(text="/fgo 绑定")
        self.assertIn("请选择", result[1])
        self.store.client.assert_not_called()
        for number, server in ((1, "android"), (2, "ios"), (3, "uo")):
            with self.subTest(server=server), patch.object(plugin, "qr_image", return_value="synthetic-png"):
                self.client.poll.return_value = ("authorized", {"uid": "1", "access_key": "test"})
                await self.bind(text=f"/fgo 绑定 {number}")
                await self.drain()
                self.assertEqual(self.store.client.call_args.args, (server,))
                self.assertEqual(self.store.save.call_args.args[:2], ("12345678", server))

    async def test_failed_game_login_or_qr_delivery_does_not_replace_binding(self):
        with patch.object(plugin, "qr_image", return_value="png"):
            self.store.fetch.side_effect = plugin.ExportError("GAME_REJECTED")
            await self.bind()
            await self.drain()
            self.store.save.assert_not_called()
            self.send.hybrid.return_value = False
            self.store.fetch.reset_mock()
            await self.bind()
            await self.drain()
            self.store.fetch.assert_not_called()
            self.store.save.assert_not_called()

    async def test_unbind_before_first_job_tick_cleans_up_and_allows_another_job(self):
        await self.bind()
        await self.instance.cmd_unbind("private", "12345678", platform="qq")
        self.store.delete.assert_called_once_with("12345678")
        self.store.save.assert_not_called()
        self.assertFalse(self.instance._account_jobs)
        self.assertFalse(self.instance._account_cancels)

    async def test_unbind_during_fetch_prevents_late_save(self):
        started, release = Event(), Event()

        def fetch(*args, **kwargs):
            started.set()
            release.wait(5)
            return "真实出率：1.50%"

        self.store.fetch.side_effect = fetch
        with patch.object(plugin, "qr_image", return_value="png"):
            await self.bind()
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                duplicate = await self.bind()
                self.assertIn("正在进行", duplicate[1])
                await self.instance.cmd_unbind("private", "12345678", platform="qq")
            finally:
                release.set()
        await asyncio.sleep(0)
        self.store.save.assert_not_called()

    async def test_expired_qr_never_saves(self):
        self.client.poll.return_value = ("expired", {})
        with patch.object(plugin, "qr_image", return_value="png"):
            await self.bind()
            await self.drain()
        self.store.save.assert_not_called()
        self.assertIn("过期", self.send.text.call_args.args[0])

    async def test_group_qr_mentions_sender_in_same_message_and_binds_sender(self):
        with patch.object(plugin, "qr_image", return_value="png"):
            await self.bind(stream_id="group", group_id="100", text="/fgo bind 3")
            await self.drain()
        self.instance.ctx.chat.get_stream_by_group_id.assert_awaited_once_with("100", "qq")
        segments = self.send.hybrid.call_args.args[0]
        self.assertEqual([segment["type"] for segment in segments], ["at", "text", "image"])
        self.assertEqual(segments[0]["data"]["target_user_id"], "12345678")
        self.assertEqual(segments[2]["binary_data_base64"], "png")
        self.assertEqual(self.send.hybrid.call_args.args[1], "group")
        self.assertEqual(self.send.hybrid.call_args.kwargs, {"storage_message": False})
        self.assertEqual(self.store.save.call_args.args[:2], ("12345678", "uo"))

    async def test_simulated_query_never_loads_credentials_or_calls_game(self):
        self.store.load.side_effect = AssertionError("模拟查询不能读取真实账号")
        await self.instance.cmd_stat("group", "12345678", platform="qq")
        text = self.send.text.call_args.args[0]
        self.assertTrue(text.startswith("模拟出率："))
        self.assertNotIn("真实", text)
        self.store.client.assert_not_called()
        self.store.fetch.assert_not_called()
        self.store.query_real.assert_not_called()

    async def test_real_query_only_returns_real_and_reports_errors(self):
        self.store.load.return_value = {"server": "android", "user": {"uid": "1", "access_key": "test"}}
        await self.instance.cmd_stat_real("group", "12345678", platform="qq")
        await self.drain()
        self.assertEqual(self.send.text.call_args.args[0], "真实出率：1.50%")
        self.assertFalse(self.store.query_real.call_args.kwargs["confirm_repeat_capture"])
        self.assertNotIn("grade_messages", self.store.query_real.call_args.kwargs)
        self.instance.pm.get_user_statistics.assert_not_called()
        self.store.query_real.side_effect = plugin.ExportError("GAME_REJECTED")
        await self.instance.cmd_stat_real("group", "12345678", platform="qq")
        await self.drain()
        text = self.send.text.call_args.args[0]
        self.assertIn("GAME_REJECTED", text)
        self.assertNotIn("模拟出率", text)
        self.store.load.side_effect = plugin.ExportError("LOCAL_CREDENTIAL_PROTECTION_FAILED")
        await self.instance.cmd_stat_real("group", "12345678", platform="qq")
        await self.drain()
        self.assertIn("LOCAL_CREDENTIAL_PROTECTION_FAILED", self.send.text.call_args.args[0])

    async def test_real_query_unbound_reminds_bind_without_simulated_output(self):
        await self.instance.cmd_stat_real("group", "12345678", platform="qq")
        await self.drain()
        self.assertEqual(self.send.text.call_args.args[0], "真实出率：尚未绑定，请使用 /fgo bind 扫码绑定。")
        self.instance.pm.get_user_statistics.assert_not_called()

    async def test_unload_cancels_pending_binding(self):
        await self.bind()
        await self.instance.on_unload()
        self.assertFalse(self.instance._account_jobs)
        self.store.save.assert_not_called()
