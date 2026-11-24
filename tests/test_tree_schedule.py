"""使用固定时钟和模拟账号验证预约，不登录游戏、不发送 QQ 消息。"""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import tempfile
import unittest

from test_pool_query import plugin
from gacha.tree_schedule import CHINA_TZ, TreeScheduleStore, next_auto_tree_time, parse_tree_time

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=CHINA_TZ)


class TreeTimeTests(unittest.TestCase):
    def test_strict_time_and_cutoff(self):
        self.assertEqual(parse_tree_time("22：00", "22:00", NOW).hour, 22)
        self.assertEqual(parse_tree_time("21:30", "21:30", NOW).minute, 30)
        for value in ("18:00", "17:59", "22:01", "24:00", "23:60", "9:00", "19:1", "19:00 PM", "１９:００"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tree_time(value, "22:00", NOW)
        self.assertEqual(parse_tree_time("23:59", "23:59", NOW).day, 17)

    def test_auto_uses_fixed_slots_and_ends_before_cutoff(self):
        cutoff = "22:01"
        first = next_auto_tree_time(cutoff, NOW.replace(hour=0))
        self.assertEqual(first.strftime("%H:%M"), "08:00")
        next_slot = next_auto_tree_time(cutoff, first)
        self.assertEqual(next_slot.strftime("%H:%M"), "17:00")
        final = next_auto_tree_time(cutoff, NOW)
        self.assertEqual(final.strftime("%H:%M"), "21:51")
        self.assertEqual(next_auto_tree_time(cutoff, final), first + timedelta(days=1))
        self.assertEqual(next_auto_tree_time("17:10", first).strftime("%H:%M"), "17:00")
        self.assertEqual(next_auto_tree_time("16:30", first).strftime("%H:%M"), "16:20")


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
            exchange_saplings=MagicMock(return_value=plugin.SaplingExchangeSuccess("兑换成功，获得 2 个青铜果实")),
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
        await asyncio.gather(*self.instance._tree_batches)
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

    async def test_auto_registration_attempts_once_and_only_notifies_on_success(self):
        exchange = self.instance.accounts.exchange_saplings
        success = exchange.return_value
        exchange.side_effect = [success, "兑换失败，AP 不足。"]
        await self.command("自动", alias="种树")
        exchange.assert_called_once()
        self.assertEqual(exchange.call_args.args[:2], ("12345678", None))
        self.send.hybrid.assert_awaited_once()
        await self.command("auto", qq="23456789")
        self.assertEqual(exchange.call_count, 2)
        self.send.hybrid.assert_awaited_once()
        self.assertEqual(len(self.store.jobs), 2)

    async def test_auto_uses_shared_slots_and_continues_after_final(self):
        with patch.object(plugin, "tree_now", return_value=NOW.replace(hour=12)):
            self.assertIn("17:00", (await self.command("auto"))[1])
        with patch.object(plugin, "tree_now", return_value=NOW.replace(hour=13)):
            self.assertIn("17:00", (await self.command("自动", qq="23456789", alias="种树"))[1])
        first = NOW.replace(hour=17)
        self.assertEqual(len(await self.store.take_due(first)), 2)
        self.assertEqual(await self.store.take_due(first), [])

        restored = TreeScheduleStore(self.path)
        await restored.load(NOW)
        pending = await restored.pending(NOW, "22:00")
        # 17:00 已执行，只保留当天末班。
        self.assertEqual([at.strftime("%H:%M") for at, _ in pending], ["21:50", "21:50"])
        self.assertEqual(len(await restored.take_due(NOW.replace(hour=21, minute=50))), 2)
        next_day = NOW.replace(hour=8) + timedelta(days=1)
        await restored.load(next_day.replace(hour=7))
        self.assertEqual(len(await restored.take_due(next_day)), 2)
        self.assertEqual(await restored.take_due(next_day), [])

    async def test_auto_accepts_registration_and_attempts_on_restart_after_final(self):
        evening = NOW.replace(hour=22)
        with patch.object(plugin, "tree_now", return_value=evening):
            self.assertIn("11-18 08:00", (await self.command("auto"))[1])
        restarted = TreeScheduleStore(self.path)
        next_evening = evening + timedelta(days=1)
        await restarted.load(next_evening, startup_id="new-bot")
        self.assertEqual(await restarted.take_due(next_evening, startup_id=self.instance._tree_startup_id), [])
        self.assertEqual(len(await restarted.take_due(next_evening, startup_id="new-bot")), 1)
        self.assertEqual(await restarted.take_due(next_evening, startup_id="new-bot"), [])

    async def test_auto_replaces_one_time_and_can_be_cancelled_or_unbound(self):
        await self.command("20:00")
        self.assertIn("没有待执行", (await self.command("auto cancel"))[1])
        self.assertIn("已取消前一次", (await self.command("自动", alias="种树"))[1])
        self.assertIn("已取消自动种树", (await self.command("自动 取消", alias="种树"))[1])
        await self.command("auto")
        self.assertIn("已取消自动种树", (await self.command("cancel"))[1])
        await self.command("auto")
        await self.instance.cmd_unbind("stream-100", "12345678", "qq")
        self.assertEqual(self.store.jobs, {})

    async def test_scheduler_waits_five_minutes_for_startup_and_still_checks_fixed_slots(self):
        with (
            patch.object(plugin, "monotonic", side_effect=[0, 0, 299, 300]),
            patch.object(plugin.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(self.store, "take_due", side_effect=[[], [], asyncio.CancelledError]) as take_due,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.instance._run_tree_schedules()
        self.assertEqual(
            [call.kwargs["startup_id"] for call in take_due.await_args_list],
            ["", "", self.instance._tree_startup_id],
        )

    async def test_startup_attempts_once_per_bot_process_and_coalesces_with_fixed_slot(self):
        with patch.object(plugin, "tree_now", return_value=NOW.replace(hour=7)):
            await self.command("auto")
        restarted = TreeScheduleStore(self.path)
        morning = NOW.replace(hour=8)
        await restarted.load(morning, startup_id="new-bot")
        self.assertEqual(len(await restarted.take_due(morning, startup_id="new-bot")), 1)
        await restarted.load(morning, startup_id="new-bot")
        self.assertEqual(await restarted.take_due(morning, startup_id="new-bot"), [])
        noon = NOW.replace(hour=12)
        self.assertEqual(len(await restarted.take_due(noon, startup_id="another-bot")), 1)
        self.assertEqual((await restarted.pending(noon, "22:00"))[0][0], NOW.replace(hour=17))

    async def test_auto_round_sends_one_notification_per_group_with_only_successes(self):
        await self.command("auto")
        await self.command("auto", qq="23456789")
        await self.command("auto", qq="34567890")
        await self.command("auto", group="200", qq="45678901")
        exchange = self.instance.accounts.exchange_saplings
        exchange.reset_mock()
        self.send.hybrid.reset_mock()
        exchange.side_effect = lambda qq, *_: (
            exchange.return_value if qq in {"12345678", "23456789"} else "兑换失败，AP 不足。"
        )
        with patch.object(plugin, "tree_now", return_value=NOW.replace(hour=21, minute=50)):
            self.instance._tree_task = asyncio.create_task(self.instance._run_tree_schedules())
            for _ in range(100):
                if self.send.hybrid.await_count and not self.instance._tree_batches:
                    break
                await asyncio.sleep(0.01)
            await self.instance.on_unload()
        notices = {call.args[1]: call.args[0] for call in self.send.hybrid.await_args_list}
        self.assertEqual(self.send.hybrid.await_count, 1)
        self.assertEqual(set(notices), {"stream-100"})
        self.assertEqual(
            [part["data"]["target_user_id"] for part in notices["stream-100"] if part["type"] == "at"],
            ["12345678", "23456789"],
        )
        self.assertIn("兑换成功", "".join(part.get("content", "") for part in notices["stream-100"]))
        self.assertNotIn("AP 不足", "".join(part.get("content", "") for part in notices["stream-100"]))
        self.assertEqual(exchange.call_count, 4)
        self.assertEqual(len(self.store.jobs), 4)

    async def test_admin_list_uses_config_and_sends_all_groups_in_private(self):
        await self.command("20:00")
        await self.command("auto", group="200", qq="23456789")
        config = self.instance.build_default_config()
        config["tree"]["admin_qqs"] = ["3129372199"]
        self.instance.set_plugin_config(config)
        self.chat.get_stream_by_user_id = AsyncMock(return_value={
            "session_id": "stream-admin", "is_group_session": False,
        })
        await self.instance.cmd_tree_list("stream-100", "12345678", "qq", group_id="100")
        self.assertIn("仅配置中的管理员", self.send.text.await_args.args[0])
        self.chat.get_stream_by_user_id.assert_not_awaited()
        await self.instance.cmd_tree_list("stream-100", "3129372199", "qq", group_id="100")
        self.chat.get_stream_by_user_id.assert_awaited_once_with("3129372199", "qq")
        message, target = self.send.text.await_args.args
        self.assertEqual(target, "stream-admin")
        self.assertEqual(self.send.text.await_args.kwargs, {"storage_message": False})
        self.assertIn("预约 20:00｜QQ 12345678｜群 100", message)
        self.assertIn("自动种树｜下次 21:50，末班 21:50｜QQ 23456789｜群 200", message)

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
