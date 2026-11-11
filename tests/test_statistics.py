"""统一指令、累计五星出率与 Chaldea 评级的回归测试。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import re
import tempfile
import unittest

from test_gacha import sample_pool
from test_pool_query import plugin, sample_pool as sample_banner
from gacha import engine, pool_manager
from gacha.pool_manager import PoolManager
from gacha.statistics import format_draw_statistics

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()


class StatisticsTests(unittest.TestCase):
    def test_user_example_and_all_chaldea_grade_boundaries(self):
        self.assertEqual(format_draw_statistics(3300, 49),
                         "你的模拟抽卡五星出率为1.48%（49 ★5/3300 抽），评级为 Grand 欧皇")
        for ssr_count, grade in [
            (1250, "Grand 欧皇"), (1249, "欧皇"), (1150, "欧皇"), (1149, "中庸"),
            (1050, "中庸"), (1049, "略非"), (1000, "略非"), (999, "非酋"),
            (850, "非酋"), (849, "酋长"), (0, "酋长"),
        ]:
            with self.subTest(ssr_count=ssr_count):
                self.assertTrue(format_draw_statistics(100000, ssr_count).endswith("评级为 " + grade))

    def test_eleven_and_hundred_count_all_ssr_servants_but_not_craft_essences(self):
        # 每组十一连包含一张 UP 五星、一张非 UP 五星和一张五星礼装。
        rates = [0, 0.008, 0.45] + [0.5] * 8
        with tempfile.TemporaryDirectory() as folder:
            manager = PoolManager(folder)
            with patch.object(engine.random, "random", side_effect=rates):
                manager.draw_for_user("12345678", sample_banner(), sample_pool(), 11)
            self.assertEqual(manager.get_user_statistics("12345678"), (11, 2))
            with patch.object(engine.random, "random", side_effect=rates * 10):
                manager.draw_for_user("12345678", sample_banner(), sample_pool(), 110)
            self.assertEqual(manager.get_user_statistics("12345678"), (121, 22))

    def test_pity_is_counted_and_stats_survive_switch_day_and_reload_for_each_user(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pool_manager.time, "time", return_value=NOW) as clock, patch.object(
            engine.random, "random", return_value=0.5
        ):
            manager = PoolManager(folder)
            for _ in range(3):
                manager.draw_for_user("12345678", sample_banner(), sample_pool(), 110)
            self.assertEqual(manager.get_user_statistics("12345678"), (330, 1))
            manager.select_user_pool("12345678", "group", sample_banner(1))
            self.assertEqual(manager.get_user_statistics("12345678"), (330, 1))
            clock.return_value = NOW + 24 * 3600
            manager = PoolManager(folder)
            manager.draw_for_user("12345678", sample_banner(1), sample_pool(), 11)
            manager.draw_for_user("23456789", sample_banner(1), sample_pool(), 11)
            self.assertEqual(manager.get_user_statistics("12345678"), (341, 1))
            self.assertEqual(manager.get_user_statistics("23456789"), (11, 0))

    def test_old_history_is_not_invented_and_query_does_not_mutate_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0.5):
            manager = PoolManager(folder)
            manager._save_json(manager.pity_path, {"12345678": {
                "pool": {"server": "国服", "href": sample_banner()["href"], "s_id": 0}, "misses": 329,
            }})
            before = Path(manager.pity_path).read_bytes()
            self.assertEqual(manager.get_user_statistics("12345678"), (0, 0))
            self.assertEqual(Path(manager.pity_path).read_bytes(), before)
            manager.draw_for_user("12345678", sample_banner(), sample_pool(), 11)
            self.assertEqual(manager.get_user_statistics("12345678"), (11, 1))
            self.assertEqual(manager.get_user_statistics("23456789"), (0, 0))

    def test_failed_and_rejected_draws_do_not_affect_statistics(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = PoolManager(folder)
            manager.draw_for_user("12345678", sample_banner(), sample_pool(), 11, daily_limit=11)
            before = Path(manager.pity_path).read_bytes()
            with self.assertRaises(pool_manager.DrawLimitError):
                manager.draw_for_user("12345678", sample_banner(), sample_pool(), 11, daily_limit=11)
            invalid = sample_pool()
            invalid["servants"][0]["ids"] = ["0"]
            with self.assertRaises(ValueError):
                manager.draw_for_user("12345678", sample_banner(), invalid, 11)
            self.assertEqual(Path(manager.pity_path).read_bytes(), before)

    def test_corrupt_statistics_are_exposed(self):
        for total, ssr in [(-1, 0), (1, 2), (11, -1), (11, True), (11.0, 0)]:
            with self.subTest(total=total, ssr=ssr), self.assertRaises(ValueError):
                format_draw_statistics(total, ssr)
        with tempfile.TemporaryDirectory() as folder:
            manager = PoolManager(folder)
            manager._save_json(manager.pity_path, {"12345678": {"total_draws": 11}})
            with self.assertRaises(KeyError):
                manager.get_user_statistics("12345678")

    def test_concurrent_statistics_keep_all_actual_results(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0):
            manager = PoolManager(folder)
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: manager.draw_for_user("12345678", sample_banner(), sample_pool(), 11), range(8)))
            self.assertEqual(manager.get_user_statistics("12345678"), (88, 88))


class UnifiedCommandTests(unittest.TestCase):
    def commands(self) -> dict:
        return {name: method.__maibot_component_info__
                for name, method in vars(plugin.FgoGachaPlugin).items()
                if callable(method) and "__maibot_component_info__" in vars(method)}

    def test_sdk_routes_use_only_fgo_namespace_and_removed_commands_are_absent(self):
        commands = self.commands()
        expected = {
            "/fgo": "cmd_help", "/fgo help": "cmd_help", "/fgo stat": "cmd_stat", "/fgo stat real": "cmd_stat_real",
            "/fgo bind": "cmd_bind", "/fgo bind 1": "cmd_bind", "/fgo bind 2": "cmd_bind", "/fgo bind 3": "cmd_bind", "/fgo unbind": "cmd_unbind",
            "/fgo init": "cmd_data_init", "/fgo pools": "cmd_query_pools",
            "/fgo tree": "cmd_tree", "/fgo tree 2": "cmd_tree",
            "/fgo switch 1": "cmd_switch_pool", "/fgo switch 1 7": "cmd_switch_pool",
            "/fgo 10": "cmd_ten_draw", "/fgo 100": "cmd_hundred_draw", "/fgo download": "cmd_download_res",
        }
        expected.update({
            "/fgo 帮助": "cmd_help", "/fgo 卡池": "cmd_query_pools",
            "/fgo 模拟出率": "cmd_stat", "/fgo 真实出率": "cmd_stat_real", "/fgo 抓包": "cmd_stat_real",
            "/fgo 绑定": "cmd_bind", "/fgo 绑定 1": "cmd_bind", "/fgo 绑定 2": "cmd_bind",
            "/fgo 绑定 3": "cmd_bind", "/fgo 解绑": "cmd_unbind",
            "/fgo 种树": "cmd_tree", "/fgo 种树 2": "cmd_tree",
            "/fgo 切换卡池 1": "cmd_switch_pool", "/fgo 切换卡池 1 7": "cmd_switch_pool",
            "/fgo 活动": "cmd_events", "/fgo 活动 术冠": "cmd_events",
        })
        for message, name in expected.items():
            with self.subTest(message=message):
                matched = [handler for handler, info in commands.items() if re.fullmatch(info.command_pattern, message)]
                self.assertEqual(matched, [name])
        for message in ("/fgo10", "/fgo100", "/fgopools", "/fgoswitch 1", "/fgoinit", "/fgodownload",
                        "/fgo bind android", "/fgo bind ios", "/fgo bind uo", "/fgo bind 0", "/fgo bind 4", "/fgo state", "/fgostyle text", "/fgofollow latest", "/fgo style text", "/fgo follow story", "/fgo 10 extra", "/fgo 十连", "/fgo 十一连", "/fgo 百连", "/fgo 初始化",
                        "/fgo 下载素材", "/fgo 兑换青铜果实", "/fgo 绑定 4", "/fgo 种树2", "/fgo 模拟出率 real"):
            with self.subTest(message=message):
                self.assertFalse(any(re.fullmatch(info.command_pattern, message) for info in commands.values()))
        for name in ("cmd_switch_style", "cmd_follow_latest", "cmd_follow_story"):
            self.assertNotIn(name, vars(plugin.FgoGachaPlugin))

    def test_help_lists_user_commands_and_marks_simulated_draws(self):
        fake = SimpleNamespace(ctx=SimpleNamespace(send=SimpleNamespace(text=AsyncMock())))
        result = asyncio.run(plugin.FgoGachaPlugin.cmd_help(fake, "group"))
        for text in ("/fgo help", "/fgo 10", "/fgo 100", "/fgo pools", "/fgo switch",
                     "/fgo stat", "/fgo stat real", "/fgo bind <区服编号>", "/fgo tree", "模拟十一连",
                     "帮助=help", "卡池=pools", "切换卡池=switch", "模拟出率=stat", "真实出率/抓包=stat real",
                     "绑定=bind", "解绑=unbind", "种树=tree", "活动=events"):
            self.assertIn(text, result[1])
        for text in ("/fgo init", "/fgo download", "本地配置"):
            self.assertNotIn(text, result[1])
        fake.ctx.send.text.assert_awaited_once_with(result[1], "group")

    def test_stat_uses_sender_qq_and_is_read_only_even_without_pool_data(self):
        async def check(folder):
            manager = plugin.PoolManager(folder)
            manager._save_json(manager.pity_path, {"12345678": {"total_draws": 3300, "ssr_count": 49},
                                                  "23456789": {"total_draws": 3300, "ssr_count": 20}})
            before = Path(manager.pity_path).read_bytes()
            instance = plugin.FgoGachaPlugin()
            instance._pool_manager = manager
            instance._accounts = SimpleNamespace(load=MagicMock(return_value=None))
            instance._ctx = SimpleNamespace(send=SimpleNamespace(text=AsyncMock()))
            result = await instance.cmd_stat("group", user_id="12345678", platform="qq")
            await asyncio.gather(*instance._account_jobs.values())
            self.assertEqual(result[0], True)
            text = instance.ctx.send.text.call_args.args[0]
            self.assertEqual(text.splitlines(), [
                "模拟出率：1.48%（49 ★5/3300 抽），评级为 Grand 欧皇",
            ])
            instance.accounts.load.assert_not_called()
            self.assertEqual(Path(manager.pity_path).read_bytes(), before)
        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(check(folder))

    def test_stat_with_no_record_or_missing_sender(self):
        async def check():
            instance = plugin.FgoGachaPlugin()
            instance._pool_manager = SimpleNamespace(get_user_statistics=MagicMock(return_value=(0, 0)))
            instance._accounts = SimpleNamespace(load=MagicMock(return_value=None))
            instance._ctx = SimpleNamespace(send=SimpleNamespace(text=AsyncMock()))
            await instance.cmd_stat("group", user_id="12345678", platform="qq")
            await asyncio.gather(*instance._account_jobs.values())
            self.assertIn("没有可统计", instance.ctx.send.text.call_args.args[0])
            instance.pm.get_user_statistics.assert_called_once_with("12345678")
            instance.pm.get_user_statistics.reset_mock()
            result = await instance.cmd_stat("group", platform="qq")
            self.assertIn("QQ 号", result[1])
            instance.pm.get_user_statistics.assert_not_called()
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
