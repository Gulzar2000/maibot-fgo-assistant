"""使用固定时钟和模拟账号验证预约，不登录游戏、不发送 QQ 消息。"""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import tempfile
import unittest

from test_pool_query import plugin
from gacha.tree_schedule import CHINA_TZ, TreeScheduleStore, parse_tree_time

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=CHINA_TZ)


class TreeTimeTests(unittest.TestCase):
    def test_strict_time_and_cutoff(self):
        self.assertEqual(parse_tree_time("22：00", "22:00", NOW).hour, 22)
        self.assertEqual(parse_tree_time("21:30", "21:30", NOW).minute, 30)
        for value in ("18:00", "17:59", "22:01", "24:00", "23:60", "9:00", "19:1", "19:00 PM", "１９:００"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tree_time(value, "22:00", NOW)
        self.assertEqual(parse_tree_time("23:59", "23:59", NOW).day, 17)


class TreeScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tree.json"
        self.store = TreeScheduleStore(self.path)
        self.instance = plugin.FgoGachaPlugin()
        self.instance.set_plugin_config({})
        self.instance._tree_schedules = self.store
        self.instance._accounts = SimpleNamespace(
            load=MagicMock(side_effect=lambda qq: {"user": {"uid": "synthetic"}}),
            exchange_saplings=MagicMock(return_value="兑换成功，获得 2 个青铜果实"),
            delete=MagicMock(),
        )
        self.send = SimpleNamespace(text=AsyncMock(return_value=True), hybrid=AsyncMock(return_value=True))
        self.chat = SimpleNamespace(get_stream_by_group_id=AsyncMock(
            side_effect=lambda group, platform: {"session_id": "stream-" + group, "is_group_session": True}))
        self.instance._set_context(SimpleNamespace(send=self.send, chat=self.chat))
        self.clock = patch.object(plugin, "tree_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def command(self, argument, group="100", qq="12345678", alias="tree"):
        previous_sends = self.send.text.await_count
        result = await self.instance.cmd_tree("stream-" + group, qq, "qq", group_id=group,
                                              text=f"/fgo {alias} {argument}")
        # 注册、替换、取消和校验提示都须真正发给 QQ，不能只断言命令返回值。
        self.assertEqual(self.send.text.await_count, previous_sends + 1)
        self.send.text.assert_awaited_with(result[1], "stream-" + group)
        return result

    async def drain(self):
        await asyncio.gather(*list(self.instance._account_jobs.values()))

    async def test_registration_replacement_cross_group_and_cancel(self):
        first = await self.command("20:00")
        self.assertIn("已注册成功", first[1])
        self.assertIn("/fgo tree cancel", first[1])
        self.assertIn("/fgo 种树 取消", first[1])
        self.assertNotIn("已取消前一次", first[1])
        second = await self.command("21：30", group="200", alias="种树")
        self.assertIn("已取消前一次自动种树预约（20:00）", second[1])
        self.assertIn("已注册成功", second[1])
        self.assertEqual(len(self.store.jobs), 1)
        self.assertEqual(self.store.jobs["12345678"]["group_id"], "200")
        self.assertIn("已取消", (await self.command("取消", alias="种树"))[1])
        self.assertIn("没有待执行", (await self.command("cancel"))[1])
        self.assertEqual(self.store.jobs, {})
        self.instance.accounts.exchange_saplings.assert_not_called()

    async def test_invalid_replacement_preserves_existing_and_requires_bound_group(self):
        await self.command("20:00")
        for value in ("17:00", "24:00", "22:01", "21:60", "9:00"):
            self.assertNotIn("已注册成功", (await self.command(value))[1])
            self.assertIn("20:00", self.store.jobs["12345678"]["at"])
        self.assertIn("QQ 群", (await self.command("21:00", group=""))[1])
        self.instance.accounts.load.return_value = None
        self.instance.accounts.load.side_effect = None
        self.assertIn("尚未绑定", (await self.command("21:00"))[1])
        self.assertIn("20:00", self.store.jobs["12345678"]["at"])

    async def test_configurable_cutoff_and_account_isolation(self):
        config = self.instance.build_default_config()
        config["tree"]["latest_time"] = "21:00"
        self.instance.set_plugin_config(config)
        self.assertIn("不能晚于", (await self.command("21:01"))[1])
        await self.command("21:00")
        await self.command("20:00", qq="23456789")
        await self.command("cancel")
        self.assertEqual(list(self.store.jobs), ["23456789"])

    async def test_due_executes_once_and_mentions_owner_in_registered_group(self):
        await self.command("20:00")
        self.assertEqual(await self.store.take_due(NOW), [])
        due = await self.store.take_due(NOW.replace(hour=20))
        self.assertEqual(await self.store.take_due(NOW.replace(hour=20)), [])
        await self.instance._execute_tree_schedule(due[0])
        await self.drain()
        args = self.instance.accounts.exchange_saplings.call_args.args
        self.assertEqual(args[:2], ("12345678", None))
        segments, stream = self.send.hybrid.call_args.args
        self.assertEqual(stream, "stream-100")
        self.assertEqual(segments[0], {"type": "at", "data": {"target_user_id": "12345678"}})
        self.assertIn("兑换成功", segments[1]["content"])

    async def test_busy_and_failed_exchange_still_mention_owner(self):
        await self.command("20:00")
        job = self.store.jobs["12345678"]
        self.instance._account_unbinding.add("12345678")
        await self.instance._execute_tree_schedule(job)
        self.assertIn("本次预约未执行", self.send.hybrid.call_args.args[0][1]["content"])
        self.instance._account_unbinding.clear()
        self.instance.accounts.exchange_saplings.side_effect = plugin.ExportError("PURCHASE_RESULT_UNCONFIRMED")
        await self.instance._execute_tree_schedule(job)
        await self.drain()
        self.assertIn("兑换结果未确认", self.send.hybrid.call_args.args[0][1]["content"])
        self.assertEqual(self.send.hybrid.call_args.args[0][0]["type"], "at")

    async def test_reloads_only_today_future_and_does_not_repeat_consumed_jobs(self):
        await self.command("20:00")
        restored = TreeScheduleStore(self.path)
        await restored.load(NOW)
        self.assertEqual(len(restored.jobs), 1)
        await restored.take_due(NOW.replace(hour=20))
        await self.store.load(NOW)
        self.assertEqual(self.store.jobs, {})
        await self.command("20:00")
        await restored.load(NOW.replace(hour=21))
        self.assertEqual(restored.jobs, {})
        await self.command("20:00")
        self.assertEqual(await self.store.take_due(NOW + timedelta(days=1)), [])
        self.assertEqual(self.store.jobs, {})

    async def test_unbind_removes_pending_schedule(self):
        await self.command("20:00")
        await self.instance.cmd_unbind("stream-100", "12345678", "qq")
        self.assertEqual(self.store.jobs, {})

    async def test_invalid_group_stream_does_not_login(self):
        await self.command("20:00")
        self.chat.get_stream_by_group_id.side_effect = None
        self.chat.get_stream_by_group_id.return_value = None
        await self.instance._execute_tree_schedule(self.store.jobs["12345678"])
        self.instance.accounts.exchange_saplings.assert_not_called()

    async def test_scheduler_loop_dispatches_due_job_and_stops_on_unload(self):
        await self.command("20:00")
        with patch.object(plugin, "tree_now", return_value=NOW.replace(hour=20)):
            self.instance._tree_task = asyncio.create_task(self.instance._run_tree_schedules())
            for _ in range(100):
                if self.send.hybrid.await_count:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.send.hybrid.await_count, 1)
            await self.instance.on_unload()
        self.assertIsNone(self.instance._tree_task)
        self.assertEqual(self.store.jobs, {})

    async def test_failed_persistence_does_not_replace_old_registration(self):
        await self.command("20:00")
        with patch.object(self.store, "_write", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                await self.command("21:00")
        self.assertIn("20:00", self.store.jobs["12345678"]["at"])
        restored = TreeScheduleStore(self.path)
        await restored.load(NOW)
        self.assertIn("20:00", restored.jobs["12345678"]["at"])


if __name__ == "__main__":
    unittest.main()
