"""QQ 独立额度、实际抽数、冷却和并发记账的回归测试。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, patch
import asyncio
import json
import tempfile
import unittest

from test_gacha import sample_pool
from test_pool_query import plugin, sample_pool as sample_banner
from gacha import engine, pool_manager
from gacha.pool_manager import DrawLimitError, PoolManager

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()


class DrawLimitTests(unittest.TestCase):
    def read_state(self, manager: PoolManager, user_id: str = "12345678") -> dict:
        return json.loads(Path(manager.pity_path).read_text(encoding="utf-8"))[user_id]

    def draw(self, manager: PoolManager, count: int = 11, user_id: str = "12345678",
             limit: int = 121, cooldown: int = 0, banner: Optional[dict] = None) -> Tuple[List[dict], bool]:
        return manager.draw_for_user(user_id, sample_banner() if banner is None else banner, sample_pool(), count,
                                     daily_limit=limit, cooldown_seconds=cooldown)

    def test_actual_counts_are_independent_per_qq_and_survive_reload(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW), patch.object(
            engine.random, "random", return_value=0.5
        ):
            manager = PoolManager(folder)
            self.draw(manager, 11)
            self.draw(manager, 110, "23456789")
            manager = PoolManager(folder)
            self.draw(manager, 110)
            self.assertEqual(self.read_state(manager)["daily_count"], 121)
            self.assertEqual(self.read_state(manager, "23456789")["daily_count"], 110)
            before = Path(manager.pity_path).read_bytes()
            with self.assertRaisesRegex(DrawLimitError, "已达到每日抽卡上限（121次），别太贪心哦~"):
                self.draw(manager)
            self.assertEqual(Path(manager.pity_path).read_bytes(), before)

    def test_insufficient_quota_rejects_entire_batch_without_drawing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW):
            manager = PoolManager(folder)
            with patch.object(pool_manager, "simulate_hundred_pull") as draw:
                with self.assertRaises(DrawLimitError):
                    self.draw(manager, 110, limit=100)
                draw.assert_not_called()
            self.assertFalse(Path(manager.pity_path).exists())
            for _ in range(9):
                self.draw(manager, limit=100)
            self.assertEqual(self.read_state(manager)["daily_count"], 99)
            with patch.object(pool_manager, "simulate_ten_pull") as draw:
                with self.assertRaises(DrawLimitError):
                    self.draw(manager, limit=100)
                draw.assert_not_called()

    def test_switching_preserves_quota_and_cooldown_and_limit_has_priority(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW) as clock:
            manager = PoolManager(folder)
            self.draw(manager, limit=22, cooldown=60)
            manager.select_user_pool("12345678", "group", sample_banner(1))
            self.assertEqual(self.read_state(manager)["daily_count"], 11)
            self.assertEqual(self.read_state(manager)["misses"], 0)
            with self.assertRaisesRegex(DrawLimitError, "抽卡还在冷却中，太频繁啦！"):
                self.draw(manager, limit=22, cooldown=60, banner=sample_banner(1))
            clock.return_value = NOW + 60
            self.draw(manager, limit=22, cooldown=60, banner=sample_banner(1))
            manager.select_user_pool("12345678", "group", sample_banner())
            clock.return_value = NOW + 61
            with self.assertRaisesRegex(DrawLimitError, "已达到每日抽卡上限（22次），别太贪心哦~"):
                self.draw(manager, limit=22, cooldown=60)
            self.assertEqual(self.read_state(manager)["daily_count"], 22)

    def test_beijing_midnight_resets_quota_but_not_cooldown_or_pity(self):
        before_midnight = datetime(2026, 10, 8, 23, 59, 59, tzinfo=timezone(timedelta(hours=8))).timestamp()
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=before_midnight) as clock, patch.object(
            engine.random, "random", return_value=0.5
        ):
            manager = PoolManager(folder)
            self.draw(manager, limit=11, cooldown=5)
            clock.return_value = before_midnight + 1
            with self.assertRaisesRegex(DrawLimitError, "抽卡还在冷却中，太频繁啦！"):
                self.draw(manager, limit=11, cooldown=5)
            clock.return_value = before_midnight + 5
            self.draw(manager, limit=11, cooldown=5)
            state = self.read_state(manager)
            self.assertEqual((state["daily_date"], state["daily_count"], state["misses"]), ("2026-10-09", 11, 22))

    def test_invalid_gacha_does_not_consume_quota_or_restart_cooldown(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW) as clock:
            manager = PoolManager(folder)
            self.draw(manager, limit=33, cooldown=60)
            before = Path(manager.pity_path).read_bytes()
            clock.return_value = NOW + 60
            invalid = sample_pool()
            invalid["servants"][0]["ids"] = ["0"]
            with self.assertRaises(ValueError):
                manager.draw_for_user("12345678", sample_banner(), invalid, 11, daily_limit=33, cooldown_seconds=60)
            self.assertEqual(Path(manager.pity_path).read_bytes(), before)
            self.draw(manager, limit=33, cooldown=60)
            self.assertEqual(self.read_state(manager)["daily_count"], 22)

    def test_concurrent_requests_cannot_bypass_quota_or_cooldown(self):
        for limit, cooldown, successful, message in [
            (33, 0, 3, "已达到每日抽卡上限（33次），别太贪心哦~"),
            (10000, 60, 1, "抽卡还在冷却中，太频繁啦！"),
        ]:
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as folder, patch.object(
                pool_manager.time, "time", return_value=NOW
            ):
                manager = PoolManager(folder)

                def request(_):
                    try:
                        self.draw(manager, limit=limit, cooldown=cooldown)
                        return "成功"
                    except DrawLimitError as error:
                        return str(error)

                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(request, range(8)))
                self.assertEqual(results.count("成功"), successful)
                self.assertEqual(results.count(message), 8 - successful)
                self.assertEqual(self.read_state(manager)["daily_count"], successful * 11)

    def test_existing_pity_state_is_preserved_when_daily_counting_starts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW), patch.object(
            engine.random, "random", return_value=0.5
        ):
            manager = PoolManager(folder)
            manager._save_json(manager.pity_path, {"12345678": {
                "pool": {"server": "国服", "href": sample_banner()["href"], "s_id": 0}, "misses": 220,
            }})
            _, triggered = self.draw(manager, 110, limit=110)
            self.assertTrue(triggered)
            state = self.read_state(manager)
            self.assertEqual((state["misses"], state["daily_count"]), (0, 110))

    def test_command_sends_exact_separate_messages_and_users_do_not_block_each_other(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW) as clock, patch.object(
            engine.random, "random", return_value=0.5
        ):
            # 使用插件实际导入的管理器与异常类，验证处理器能正确识别并发送限制消息。
            manager = plugin.PoolManager(folder)
            manager.save_pools([sample_banner()])
            manager.save_gacha_data([{**sample_pool(), "pool_href": sample_banner()["href"], "s_id": 0}])
            send = SimpleNamespace(text=AsyncMock(return_value=True))
            fake = SimpleNamespace(pm=manager, _ack=AsyncMock(), ctx=SimpleNamespace(send=send),
                                   config=SimpleNamespace(gacha=plugin.GachaConfig(
                                       display_mode="text", daily_limit=22, cooldown_seconds=60)))
            asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "12345678"))
            asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "23456789"))
            self.assertEqual(self.read_state(manager)["daily_count"], 11)
            self.assertEqual(self.read_state(manager, "23456789")["daily_count"], 11)
            send.text.reset_mock()
            fake._ack.reset_mock()
            clock.return_value = NOW + 1
            result = asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "private1", "12345678"))
            self.assertEqual(result, (True, "抽卡还在冷却中，太频繁啦！", True))
            send.text.assert_awaited_once_with("抽卡还在冷却中，太频繁啦！", "private1")
            fake._ack.assert_not_awaited()
            clock.return_value = NOW + 60
            asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "12345678"))
            send.text.reset_mock()
            fake._ack.reset_mock()
            clock.return_value = NOW + 61
            result = asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "12345678"))
            self.assertEqual(result, (True, "已达到每日抽卡上限（22次），别太贪心哦~", True))
            send.text.assert_awaited_once_with("已达到每日抽卡上限（22次），别太贪心哦~", "group1")
            fake._ack.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
