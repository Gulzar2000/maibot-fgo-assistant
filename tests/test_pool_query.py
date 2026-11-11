"""国服日程排序、自动刷新和合并转发的回归测试。"""
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import importlib.util
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gacha.pool_fetcher import CHINA_TZ, _parse_schedule_html, fetch_pool_list
from gacha.pool_manager import PoolManager
from gacha.pool_messages import (
    FORWARD_BODY_NODE_LIMIT, NODE_TEXT_LIMIT, batch_forward_messages, build_nested_forward_nodes,
    format_pool_messages, split_message_blocks,
)

# 用独立包名加载带相对导入的插件，直接测试处理器，不启动机器人或发送 QQ 消息。
package = ModuleType("fgo_query_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("fgo_query_test.plugin", ROOT / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


def table(heading: str, rows: list) -> str:
    contents = "".join(
        f'<tr><td><a href="/w/{title}" title="{title}">{title}</a></td><td>{dates}</td></tr>'
        for title, dates in rows
    )
    return f'<div class="mw-parser-output"><table><tr><th>{heading}</th></tr>{contents}</table></div>'


def sample_pool(index: int = 0) -> dict:
    return {
        "id": index, "title": "召唤" + str(index), "server": "国服", "href": "url" + str(index),
        "status": "当前", "start_date": "2026-10-01", "end_date": "2026-10-20",
        "estimated": False, "type": "normal", "available": True, "sub_pool": [],
    }


class PoolQueryTests(unittest.TestCase):
    def test_image_draw_uses_worker_and_removes_temporary_file_even_on_send_failure(self):
        cards = [{"type": "svt", "rarity": "3", "id": "31"}] * 11
        for sent in (True, False):
            with self.subTest(sent=sent), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "result.png"
                main_thread = threading.get_ident()
                render_threads = []

                def render(*args, user_nickname: str):
                    self.assertEqual(user_nickname, "某某人")
                    render_threads.append(threading.get_ident())
                    path.write_bytes(b"test image bytes")
                    return str(path)

                send = SimpleNamespace(image=AsyncMock(return_value=sent), text=AsyncMock())
                fake = SimpleNamespace(
                    _ack=AsyncMock(),
                    pm=SimpleNamespace(get_stream_banner=lambda *args: sample_pool(),
                                       get_gacha_data_for_banner=lambda *args: {}, load_card_names=lambda: {},
                                       draw_for_user=lambda *args, **kwargs: (cards, False)),
                    config=SimpleNamespace(gacha=SimpleNamespace(follow_pool_type="latest", display_mode="image",
                                                                 daily_limit=10000, cooldown_seconds=0)),
                    res_dir="res", icon_dir="icons", ctx=SimpleNamespace(send=send),
                )
                with patch.object(
                    plugin, "ensure_result_cards", new_callable=AsyncMock
                ), patch.object(plugin, "generate_ten_pull_image", side_effect=render):
                    if sent:
                        result = asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "test", "12345678", user_nickname="某某人"))
                        self.assertEqual(result, (True, "", True))
                    else:
                        with self.assertLogs("fgo_query_test.plugin", level="ERROR"):
                            result = asyncio.run(plugin.FgoGachaPlugin._do_gacha(fake, "test", "12345678", user_nickname="某某人"))
                        self.assertIn("发送失败", result[1])
                self.assertNotEqual(render_threads[0], main_thread)
                self.assertFalse(path.exists())
                send.image.assert_awaited_once()
                send.text.assert_not_awaited()

    def test_switch_selects_pool_and_optional_child_and_rejects_invalid_selection(self):
        daily = {**sample_pool(1), "type": "daily pickup",
                 "sub_pool": [{"id": 2, "sub_title": "首个子池"}, {"id": 7, "sub_title": "指定子池"}]}
        for message, selected, child_id in [
            ("/fgo switch 0", 0, None), ("/fgo switch 1", 1, 2), ("/fgo switch 1 7", 1, 7),
            ("/fgo 切换卡池 0", 0, None), ("/fgo 切换卡池 1", 1, 2), ("/fgo 切换卡池 1 7", 1, 7),
            ("/fgo 切换卡池 1 99", None, None), ("/fgo switch 1 99", None, None), ("/fgo switch 0 7", None, None),
            ("/fgo switch 2 7", None, None), ("/fgo switch 1 7 extra", None, None),
        ]:
            with self.subTest(message=message):
                manager = SimpleNamespace(load_pools=lambda: [sample_pool(), daily,
                                          {**daily, "id": 2, "available": False}],
                                          select_user_pool=MagicMock())
                fake = SimpleNamespace(pm=manager, ctx=SimpleNamespace(send=SimpleNamespace(text=AsyncMock())))
                result = asyncio.run(plugin.FgoGachaPlugin.cmd_switch_pool(fake, "test", message, "12345678"))
                if selected is None:
                    manager.select_user_pool.assert_not_called()
                else:
                    user_id, stream_id, banner = manager.select_user_pool.call_args.args
                    self.assertEqual(user_id, "12345678")
                    self.assertEqual(stream_id, "test")
                    self.assertEqual(banner["id"], selected)
                    self.assertEqual(banner.get("s_id"), child_id)
                    if child_id == 7:
                        self.assertIn("指定子池", result[1])
        self.assertFalse(hasattr(plugin.FgoGachaPlugin, "cmd_switch_daily_pool"))

    def test_schedule_filters_expired_jp_and_special_pools(self):
        dates = "2026年10月1日(周四) 19:00 ~ 2026年10月20日(周二) 13:59"
        html = table("国服当前卡池", [("当前池", dates), ("已过期", "2026年9月1日 ~ 2026年9月30日"),
                                    ("福袋召唤", dates), ("Destiny Order召唤", dates)])
        html += table("日服当前卡池", [("日服池", dates)])
        pools = _parse_schedule_html(html, datetime(2026, 10, 7, tzinfo=CHINA_TZ))
        self.assertEqual([pool["title"] for pool in pools], ["当前池"])
        self.assertEqual(pools[0]["status"], "当前")

    def test_predicted_dates_keep_date_precision_and_year_boundary(self):
        html = table("国服未来卡池", [("跨年池", "预计 2026年12月31日(周四) ~ 2027年1月14日(周四)")])
        pool = _parse_schedule_html(html, datetime(2026, 10, 7, tzinfo=CHINA_TZ))[0]
        self.assertTrue(pool["estimated"])
        self.assertEqual(pool["start_date"], "2026-12-31")
        self.assertEqual(pool["end_date"], "2027-01-14")
        self.assertEqual(pool["status"], "未来")

    def test_fetch_deduplicates_and_sorts_by_actual_start_time(self):
        current = table("国服当前卡池", [("当前池", "2026年1月1日 ~ 2099年10月20日")])
        future = table("国服未来卡池", [("较晚", "预计 2099年12月1日 ~ 2099年12月10日"),
                                      ("较早", "预计 2099年11月1日 ~ 2099年11月10日"),
                                      ("当前池", "2026年1月1日 ~ 2099年10月20日")])
        with patch("gacha.pool_fetcher.fetch_text", new_callable=AsyncMock, side_effect=[current, future]):
            pools = asyncio.run(fetch_pool_list())
        self.assertEqual([pool["title"] for pool in pools], ["当前池", "较早", "较晚"])
        self.assertEqual([pool["id"] for pool in pools], [0, 1, 2])

    def test_bad_schedule_date_exposes_error(self):
        with self.assertRaises(ValueError):
            _parse_schedule_html(table("国服未来卡池", [("坏数据", "日期不明")]),
                                 datetime(2026, 10, 7, tzinfo=CHINA_TZ))

    def test_long_text_is_split_without_losing_content(self):
        messages = split_message_blocks(["一" * 40, "二" * 40, "三" * 130], limit=50)
        self.assertTrue(all(len(text) <= 50 for text in messages))
        self.assertEqual("".join(messages).replace("\n", ""), "一" * 40 + "二" * 40 + "三" * 130)

    def test_list_includes_dates_prediction_and_unavailable_marker(self):
        pool = {**sample_pool(), "estimated": True, "available": False}
        message = "\n".join(format_pool_messages([pool], None, "更新成功"))
        self.assertIn("2026-10-01", message)
        self.assertIn("预计", message)
        self.assertIn("不能切换抽取", message)

    def test_refresh_preserves_selection_by_url_after_renumbering(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = PoolManager(folder)
            old = {**sample_pool(7), "href": "keep", "s_id": 2}
            manager.save_banners([{"stream_id": "test", "banner": old}])
            updated = {**sample_pool(1), "href": "keep", "type": "daily pickup",
                       "sub_pool": [{"id": 0}, {"id": 2}]}
            manager.save_pool_snapshot([sample_pool(), updated], [], sample_pool())
            selected = manager.get_stream_banner("test")
            self.assertEqual((selected["id"], selected["s_id"]), (1, 2))

    def test_forward_batches_keep_all_text_in_order(self):
        messages = [(str(index) * NODE_TEXT_LIMIT)[:NODE_TEXT_LIMIT] for index in range(24)]
        batches = batch_forward_messages(messages)
        self.assertEqual([text for batch in batches for text in batch[1:]], messages)
        self.assertTrue(all(len(batch) <= FORWARD_BODY_NODE_LIMIT + 1 for batch in batches))
        self.assertIn("1/6", batches[0][0])
        self.assertIn("6/6", batches[-1][0])

    def test_query_refreshes_then_sends_one_nested_forward(self):
        send = SimpleNamespace(forward=AsyncMock(return_value=True), text=AsyncMock())
        fake = SimpleNamespace(
            _ack=AsyncMock(), _do_fetch_pools=AsyncMock(return_value="更新成功"),
            pm=SimpleNamespace(load_pools=lambda: [sample_pool(i) for i in range(160)],
                               get_stream_banner=lambda *args: sample_pool()),
            config=SimpleNamespace(gacha=SimpleNamespace(follow_pool_type="latest")),
            ctx=SimpleNamespace(send=send, api=SimpleNamespace(call=AsyncMock(return_value={"user_id": 12345678}))),
        )
        result = asyncio.run(plugin.FgoGachaPlugin.cmd_query_pools(fake, "test"))
        fake._do_fetch_pools.assert_awaited_once()
        send.forward.assert_awaited_once()
        send.text.assert_not_awaited()
        nodes, stream_id = send.forward.call_args.args
        self.assertEqual(stream_id, "test")
        self.assertEqual(send.forward.call_args.kwargs, {"storage_message": False})
        self.assertGreater(len(nodes), 1)
        self.assertTrue(all(node["user_id"] == "12345678" for node in nodes))
        for page in nodes:
            segment = page["segments"][0]
            self.assertEqual(segment["type"], "forward")
            self.assertLessEqual(len(segment["data"]), FORWARD_BODY_NODE_LIMIT + 1)
            self.assertTrue(all(len(child["content"][0]["data"]) <= NODE_TEXT_LIMIT
                                for child in segment["data"]))
        fake.ctx.api.call.assert_awaited_once_with("adapter.napcat.system.get_login_info", version="1")
        self.assertEqual(result, (True, "", True))
        self.assertFalse(hasattr(plugin.FgoGachaPlugin, "cmd_fetch_pools"))

    def test_query_failure_does_not_send_stale_cache(self):
        send = SimpleNamespace(forward=AsyncMock())
        fake = SimpleNamespace(_ack=AsyncMock(), _do_fetch_pools=AsyncMock(side_effect=ValueError("网络错误")),
                               ctx=SimpleNamespace(send=send))
        with self.assertLogs("fgo_query_test.plugin", level="ERROR"):
            result = asyncio.run(plugin.FgoGachaPlugin.cmd_query_pools(fake, "test"))
        send.forward.assert_not_awaited()
        self.assertIn("网络错误", result[1])

    def test_failed_simulator_remains_visible_in_downloaded_schedule(self):
        pools = [sample_pool(0), sample_pool(1)]
        child = {"s_id": 0, "sub_title": "默认", "servants": [], "crafts": [], "data_version": 4}
        fake = SimpleNamespace(_pool_refresh_lock=asyncio.Lock(), pm=MagicMock())
        with patch.object(plugin, "fetch_pool_list", new_callable=AsyncMock, return_value=pools), patch.object(
            plugin, "fetch_scheduled_pool_data", new_callable=AsyncMock,
            side_effect=[{"sub_pools": [child]}, ValueError("坏数据")]
        ), patch.object(plugin, "build_card_name_map", return_value={"svt": {}, "ce": {}}), self.assertLogs("fgo_query_test.plugin", level="ERROR"):
            asyncio.run(plugin.FgoGachaPlugin._do_fetch_pools(fake))
        saved, data, default = fake.pm.save_pool_snapshot.call_args.args
        self.assertEqual(len(saved), 2)
        self.assertTrue(saved[0]["available"])
        self.assertFalse(saved[1]["available"])
        self.assertEqual(data[0]["pool_href"], saved[0]["href"])

    def test_data_lookup_tracks_url_even_when_old_display_id_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = PoolManager(folder)
            manager.save_gacha_data([{"p_id": 99, "pool_href": "keep", "data_version": 4, "s_id": 0}])
            self.assertIsNotNone(manager.get_gacha_data_for_banner({"id": 0, "href": "keep"}))
            self.assertIsNone(manager.get_gacha_data_for_banner({"id": 99, "href": "another"}))

    def test_forward_failure_is_reported(self):
        fake = SimpleNamespace(
            _ack=AsyncMock(), _do_fetch_pools=AsyncMock(return_value="更新成功"),
            pm=SimpleNamespace(load_pools=lambda: [sample_pool()], get_stream_banner=lambda *args: sample_pool()),
            config=SimpleNamespace(gacha=SimpleNamespace(follow_pool_type="latest")),
            ctx=SimpleNamespace(send=SimpleNamespace(forward=AsyncMock(return_value=False)),
                                api=SimpleNamespace(call=AsyncMock(return_value={"user_id": 12345678}))),
        )
        with self.assertLogs("fgo_query_test.plugin", level="ERROR"):
            result = asyncio.run(plugin.FgoGachaPlugin.cmd_query_pools(fake, "test"))
        self.assertIn("合并转发发送失败", result[1])
        self.assertIn("卡池数据已更新", result[1])
        self.assertNotIn("卡池列表获取失败", result[1])

    def test_invalid_login_account_is_not_sent_as_forward_sender(self):
        send = SimpleNamespace(forward=AsyncMock())
        fake = SimpleNamespace(
            _ack=AsyncMock(), _do_fetch_pools=AsyncMock(return_value="更新成功"),
            pm=SimpleNamespace(load_pools=lambda: [sample_pool()], get_stream_banner=lambda *args: sample_pool()),
            config=SimpleNamespace(gacha=SimpleNamespace(follow_pool_type="latest")),
            ctx=SimpleNamespace(send=send, api=SimpleNamespace(call=AsyncMock(return_value={"user_id": 0}))),
        )
        with self.assertLogs("fgo_query_test.plugin", level="ERROR"):
            result = asyncio.run(plugin.FgoGachaPlugin.cmd_query_pools(fake, "test"))
        send.forward.assert_not_awaited()
        self.assertIn("登录 QQ 号无效", result[1])
        self.assertIn("卡池数据已更新", result[1])

    def test_nested_forward_encodes_as_napcat_nodes_without_losing_text(self):
        # 使用实际适配器编码器校验嵌套结构，不启动机器人或调用 QQ。
        encoder_path = ROOT.parent / "napcat-adapter/codecs/outbound/segment_encoder.py"
        encoder_spec = importlib.util.spec_from_file_location("fgo_napcat_encoder_test", encoder_path)
        encoder_module = importlib.util.module_from_spec(encoder_spec)
        encoder_spec.loader.exec_module(encoder_module)
        messages = [f"pool {index}" for index in range(24)]
        pages = build_nested_forward_nodes(messages, "12345678")
        raw = [{"user_id": page["user_id"], "user_nickname": page["nickname"],
                "content": page["segments"]} for page in pages]
        wire = encoder_module.NapCatOutboundSegmentEncoder().convert_segments(
            [{"type": "forward", "data": raw}]
        )
        self.assertEqual(len(wire), 6)
        texts = []
        for outer in wire:
            self.assertEqual(outer["type"], "node")
            children = outer["data"]["content"]
            self.assertLessEqual(len(children), FORWARD_BODY_NODE_LIMIT + 1)
            for child in children:
                self.assertEqual(child["type"], "node")
                self.assertEqual(child["data"]["uin"], "12345678")
            texts.extend(child["data"]["content"][0]["data"]["text"] for child in children[1:])
        self.assertEqual(texts, messages)


if __name__ == "__main__":
    unittest.main()
