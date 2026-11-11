"""330 抽确定召唤、QQ 用户持久化与 110 抽百连的回归测试。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json
import tempfile
import unittest

from test_gacha import sample_pool
from test_pool_query import plugin, sample_pool as sample_banner
from gacha import engine
from gacha.pool_manager import PoolManager


class PityEngineTests(unittest.TestCase):
    def test_first_329_misses_are_normal_and_next_draw_is_forced(self):
        pity = engine.PityCounter(318)
        with patch.object(engine.random, "random", return_value=0.5), patch.object(engine.random, "shuffle"):
            first = engine.simulate_ten_pull(sample_pool(), pity)
            self.assertEqual(pity.misses, 329)
            self.assertFalse(any(card["rarity"] == "up5" for card in first))
            second = engine.simulate_ten_pull(sample_pool(), pity)
        self.assertEqual(second[0], {"type": "svt", "rarity": "up5", "id": "142"})
        self.assertEqual((pity.misses, pity.triggered), (10, 1))

    def test_forced_draw_can_be_either_guarantee_slot(self):
        for start, position, remaining in [(320, 9, 1), (319, 10, 0)]:
            with self.subTest(start=start):
                pity = engine.PityCounter(start)
                with patch.object(engine.random, "random", return_value=0.5), patch.object(engine.random, "shuffle"):
                    cards = engine.simulate_ten_pull(sample_pool(), pity)
                self.assertEqual(cards[position]["rarity"], "up5")
                self.assertEqual((pity.misses, pity.triggered), (remaining, 1))
                self.assertTrue(any(card["type"] == "svt" for card in cards))
                self.assertTrue(any(card["rarity"][-1] in "45" for card in cards))

    def test_early_pickup_resets_before_display_shuffle(self):
        pity = engine.PityCounter(328)
        with patch.object(engine.random, "random", side_effect=[0] + [0.5] * 10):
            cards = engine.simulate_ten_pull(sample_pool(), pity)
        self.assertEqual(sum(card["rarity"] == "up5" for card in cards), 1)
        self.assertEqual((pity.misses, pity.triggered), (10, 0))

    def test_off_banner_ssr_and_five_star_ce_do_not_reset(self):
        pity = engine.PityCounter(10)
        with patch.object(engine.random, "random", side_effect=[0.008, 0.45] + [0.5] * 9), patch.object(
            engine.random, "shuffle"
        ):
            cards = engine.simulate_ten_pull(sample_pool(), pity)
        self.assertEqual([card["rarity"] for card in cards[:2]], ["5", "ce_5"])
        self.assertEqual((pity.misses, pity.triggered), (21, 0))

    def test_no_ssr_pickup_and_multiple_ssr_pickups_disable_pity(self):
        absent = sample_pool()
        absent["servants"][0]["type"] = "svt_5"
        grouped = sample_pool()
        grouped["servants"][0]["ids"].append("394")
        separate = sample_pool()
        separate["servants"].append({**deepcopy(separate["servants"][0]), "ids": ["394"]})
        zero_weight = sample_pool()
        zero_weight["servants"][0]["weight"] = 0
        for data in (absent, grouped, separate, zero_weight):
            with self.subTest(data=data), patch.object(engine.random, "random", return_value=0.5):
                pity = engine.PityCounter(329)
                cards = engine.simulate_ten_pull(data, pity)
                self.assertFalse(any(card["rarity"] == "up5" for card in cards))
                self.assertEqual((pity.misses, pity.triggered), (0, 0))

    def test_three_hundred_commands_force_exactly_the_330th_draw(self):
        pity = engine.PityCounter()
        with patch.object(engine.random, "random", return_value=0.5):
            for expected in (110, 220):
                summary = engine.simulate_hundred_pull(sample_pool(), pity)
                self.assertEqual(sum(card["count"] for card in summary), 110)
                self.assertEqual((pity.misses, pity.triggered), (expected, 0))
            summary = engine.simulate_hundred_pull(sample_pool(), pity)
        self.assertEqual(sum(card["count"] for card in summary), 110)
        self.assertEqual(next(card["count"] for card in summary if card["type"] == "upsvt_5"), 1)
        self.assertEqual((pity.misses, pity.triggered), (0, 1))

    def test_each_hundred_command_contains_ten_eleven_pulls(self):
        with patch.object(engine, "simulate_ten_pull", wraps=engine.simulate_ten_pull) as draw:
            engine.simulate_hundred_pull(sample_pool())
        self.assertEqual(draw.call_count, 10)


class PityPersistenceTests(unittest.TestCase):
    def read_states(self, manager: PoolManager) -> dict:
        return json.loads(Path(manager.pity_path).read_text(encoding="utf-8"))

    def test_users_are_independent_and_counts_survive_reload(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0.5):
            manager = PoolManager(folder)
            manager.draw_for_user("12345678", sample_banner(), sample_pool(), 110)
            manager.draw_for_user("23456789", sample_banner(), sample_pool(), 11)
            manager = PoolManager(folder)
            manager.draw_for_user("12345678", sample_banner(), sample_pool(), 110)
            states = self.read_states(manager)
            self.assertEqual(states["12345678"]["misses"], 220)
            self.assertEqual(states["23456789"]["misses"], 11)
            _, triggered = manager.draw_for_user("12345678", sample_banner(), sample_pool(), 110)
            self.assertTrue(triggered)
            self.assertEqual(self.read_states(manager)["12345678"]["misses"], 0)

    def test_same_pool_and_renumbering_preserve_but_switching_and_switching_back_reset(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0.5):
            manager = PoolManager(folder)
            banner = sample_banner()
            manager.draw_for_user("12345678", banner, sample_pool(), 110)
            manager.select_user_pool("12345678", "group1", {**banner, "id": 99})
            self.assertEqual(self.read_states(manager)["12345678"]["misses"], 110)
            manager.select_user_pool("12345678", "group1", sample_banner(1))
            manager.select_user_pool("12345678", "group1", banner)
            self.assertEqual(self.read_states(manager)["12345678"]["misses"], 0)
            manager.draw_for_user("12345678", banner, sample_pool(), 11)
            self.assertEqual(self.read_states(manager)["12345678"]["misses"], 11)

    def test_subpool_or_effective_pool_change_resets(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0.5):
            manager = PoolManager(folder)
            manager.draw_for_user("12345678", {**sample_banner(), "s_id": 2}, sample_pool(), 110)
            manager.select_user_pool("12345678", "group1", {**sample_banner(), "s_id": 7})
            self.assertEqual(self.read_states(manager)["12345678"]["misses"], 0)
            manager.draw_for_user("12345678", sample_banner(1), sample_pool(), 11)
            self.assertEqual(self.read_states(manager)["12345678"]["misses"], 11)

    def test_failed_draw_and_invalid_user_do_not_save_counts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0.5):
            manager = PoolManager(folder)
            manager.draw_for_user("12345678", sample_banner(), sample_pool(), 11)
            before = Path(manager.pity_path).read_bytes()
            invalid = sample_pool()
            invalid["servants"][0]["ids"] = ["0"]
            with self.assertRaises(ValueError):
                manager.draw_for_user("12345678", sample_banner(), invalid, 110)
            for user in ("", "group", "0"):
                with self.assertRaises(ValueError):
                    manager.draw_for_user(user, sample_banner(), sample_pool(), 11)
            self.assertEqual(Path(manager.pity_path).read_bytes(), before)

    def test_concurrent_requests_keep_all_users_and_same_user_increments(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(engine.random, "random", return_value=0.5):
            manager = PoolManager(folder)
            users = ["12345678"] * 10 + ["23456789"] * 10
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda user: manager.draw_for_user(user, sample_banner(), sample_pool(), 11), users))
            states = self.read_states(manager)
            self.assertEqual({user: state["misses"] for user, state in states.items()},
                             {"12345678": 110, "23456789": 110})


class PityCommandTests(unittest.TestCase):
    def make_fake(self, count: int, triggered: bool, display_mode: str, normal_pickups: int = 0) -> SimpleNamespace:
        pickup_count = normal_pickups + int(triggered)
        if count == 11:
            cards = [{"type": "svt", "rarity": "up5", "id": "142"}] * pickup_count + [
                {"type": "svt", "rarity": "5", "id": "2"},
                {"type": "cft", "rarity": "ce_up5", "id": "31"},
                {"type": "svt", "rarity": "up4", "id": "7"},
            ] + [{"type": "cft", "rarity": "ce_4", "id": "7"}] * (11 - pickup_count - 3)
        else:
            cards = [{"card_id": "142", "type": "upsvt_5", "count": pickup_count}] if pickup_count else []
            cards.extend([
                {"card_id": "2", "type": "svt_5", "count": 1},
                {"card_id": "31", "type": "upce_5", "count": 1},
                {"card_id": "7", "type": "upsvt_4", "count": 1},
                {"card_id": "7", "type": "ce_4", "count": 110 - pickup_count - 3},
            ])
        return SimpleNamespace(
            _ack=AsyncMock(),
            pm=SimpleNamespace(get_stream_banner=lambda *args: sample_banner(),
                               get_gacha_data_for_banner=lambda *args: sample_pool(), load_card_names=lambda: {},
                               draw_for_user=MagicMock(return_value=(cards, triggered))),
            config=SimpleNamespace(gacha=SimpleNamespace(follow_pool_type="latest", display_mode=display_mode,
                                                         daily_limit=10000, cooldown_seconds=0)),
            res_dir="res", icon_dir="icons",
            ctx=SimpleNamespace(send=SimpleNamespace(text=AsyncMock(return_value=True), image=AsyncMock(return_value=True),
                                                    hybrid=AsyncMock(return_value=True))),
        )

    def test_text_results_distinguish_normal_pickups_and_pity(self):
        for count in (11, 110):
            for triggered, normal_pickups in [(False, 0), (False, 1), (False, 2), (True, 0), (True, 1)]:
                with self.subTest(count=count, triggered=triggered, normal_pickups=normal_pickups):
                    fake = self.make_fake(count, triggered, "text", normal_pickups)
                    asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "12345678", count))
                    fake.pm.draw_for_user.assert_called_once_with("12345678", sample_banner(), sample_pool(), count,
                                                                 daily_limit=10000, cooldown_seconds=0)
                    messages = [call.args[0] for call in fake.ctx.send.text.call_args_list]
                    self.assertEqual(messages.count("很遗憾，你保底了~"), int(triggered))
                    self.assertEqual(messages.count("恭喜你，你出货了~！"), int(normal_pickups > 0))
                    if count == 110:
                        fake.ctx.send.hybrid.assert_awaited_once()
                        segments, stream_id = fake.ctx.send.hybrid.call_args.args
                        self.assertEqual(stream_id, "group1")
                        self.assertEqual(segments[0], {"type": "at", "data": {"target_user_id": "12345678"}})
                        self.assertEqual(segments[1]["type"], "text")
                        self.assertTrue(segments[1]["content"].startswith("\n"))
                        self.assertIn("110 抽", segments[1]["content"])
                        self.assertEqual(len(messages), int(triggered) + int(normal_pickups > 0))
                    else:
                        fake.ctx.send.hybrid.assert_not_awaited()

    def test_image_result_sends_notice_after_image_even_if_image_send_fails(self):
        for sent, triggered, normal_pickups in [
            (sent, triggered, normal_pickups) for sent in (False, True)
            for triggered, normal_pickups in [(True, 0), (False, 1), (True, 1)]
        ]:
            with self.subTest(sent=sent, triggered=triggered, normal_pickups=normal_pickups), tempfile.TemporaryDirectory() as folder:
                image = Path(folder) / "result.png"
                image.write_bytes(b"test image")
                fake = self.make_fake(11, triggered, "image", normal_pickups)
                fake.ctx.send.image.return_value = sent
                calls = MagicMock()
                calls.attach_mock(fake.ctx.send.image, "image")
                calls.attach_mock(fake.ctx.send.text, "text")
                with patch.object(plugin, "ensure_result_cards", new_callable=AsyncMock), patch.object(
                    plugin, "generate_ten_pull_image", return_value=str(image)
                ) as generate:
                    if sent:
                        asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "12345678", user_nickname="某某人"))
                    else:
                        with self.assertLogs("fgo_query_test.plugin", level="ERROR"):
                            asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1", "12345678", user_nickname="某某人"))
                self.assertEqual(generate.call_args.kwargs["user_nickname"], "某某人")
                expected = []
                if normal_pickups:
                    expected.append("恭喜你，你出货了~！")
                if triggered:
                    expected.append("很遗憾，你保底了~")
                self.assertEqual([call[0] for call in calls.mock_calls], ["image"] + ["text"] * len(expected))
                self.assertEqual([call.args for call in fake.ctx.send.text.call_args_list],
                                 [(message, "group1") for message in expected])
                self.assertFalse(image.exists())

    def test_commands_forward_actual_user_and_draw_counts(self):
        fake = SimpleNamespace(_do_gacha=AsyncMock(return_value=(True, "", True)))
        message = {"message_info": {"user_info": {"user_nickname": "某某人", "user_cardname": "群名片"}}}
        asyncio.run(plugin.FgoGachaPlugin.cmd_ten_draw(fake, "group1", user_id="12345678", message=message))
        fake._do_gacha.assert_awaited_once_with("group1", "12345678", count=11, user_nickname="某某人")
        fake._do_gacha.reset_mock()
        asyncio.run(plugin.FgoGachaPlugin.cmd_hundred_draw(fake, "group1", user_id="12345678"))
        fake._do_gacha.assert_awaited_once_with("group1", "12345678", count=110)

    def test_missing_user_never_draws(self):
        fake = self.make_fake(11, False, "text")
        result = asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "group1"))
        self.assertIn("QQ 号", result[1])
        fake.pm.draw_for_user.assert_not_called()
        fake._ack.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
