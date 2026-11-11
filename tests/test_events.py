"""活动来源筛选、日期边界、名称搜索及命令发送验证，不启动机器人。"""
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import importlib.util
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gacha.events import format_event_messages, parse_events
from gacha.pool_messages import NODE_TEXT_LIMIT

package = ModuleType("fgo_events_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("fgo_events_test.plugin", ROOT / "plugin.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


def table(name: str, rows: list) -> str:
    body = "".join(
        f'<tr><td>Event</td><td><a href="/w/{title}"><img /></a>'
        f'<a href="/w/{title}">{title}</a></td><td>{dates}</td><td>无</td></tr>'
        for title, dates in rows
    )
    return f'<table><tbody><tr><th>类型</th><th>{name}</th><th>时间</th></tr>{body}</tbody></table>'


def page(current: list, future: list) -> str:
    return '<div class="mw-parser-output">' + table("国服当前活动", current) + table("国服未来活动", future) + '</div>'


class EventsTests(unittest.TestCase):
    def test_aliases_normalization_custom_rules_and_no_duplicate_results(self):
        events = parse_events(page([], [
            ("冠位戴冠战：Caster", "预估 2027年1月1日 ~ 无明确结束时间"),
            ("「冠位戴冠战：Caster」开幕前夕纪念活动", "预估 2027年1月1日 ~ 2027年1月10日"),
            ("冠位戴冠战：Archer", "预估 2027年1月1日 ~ 无明确结束时间"),
            ("迦勒底南海大决战", "预估 2027年1月1日 ~ 2027年1月10日"),
            ("见鬼去吧！ 南瓜农场屠杀", "预估 2027年1月1日 ~ 2027年1月10日"),
        ]), date(2026, 10, 19))
        text = '\n'.join(format_event_messages(events, "术冠"))
        self.assertEqual(text.count('[未来]'), 2)
        self.assertNotIn('Archer', text)
        self.assertIn('南海大决战', '\n'.join(format_event_messages(events, "泳装")))
        self.assertIn('南瓜农场', '\n'.join(format_event_messages(events, "万圣节")))
        self.assertEqual('\n'.join(format_event_messages(events, "冠位戴冠战: caster")).count('[未来]'), 2)
        rules = [{"aliases": ["自定义", "自定义别名"], "titles": ["Caster", "冠位戴冠战：Caster"]}]
        self.assertEqual('\n'.join(format_event_messages(events, "自定义", alias_rules=rules)).count('[未来]'), 2)
        self.assertIn('未找到', format_event_messages(events, "术冠", alias_rules=[])[0])
        self.assertIn('未找到', format_event_messages(events, "术冠活动")[0])
        self.assertEqual('\n'.join(format_event_messages(events, alias_rules=rules)).count('[未来]'), 5)

    def test_regions_end_day_unknown_end_and_chronological_order(self):
        html = page([
            ("今天结束", "2026年10月1日 19:00 ~ 2026年10月19日 13:59"),
            ("过期", "2026年10月1日 ~ 2026年10月18日"),
            ("冠位战", "2026年10月15日 ~ 无明确结束时间"),
        ], [
            ("跨年CBC", "预估 2026年12月31日 ~ 2027年1月14日"),
            ("万圣节", "预估 2026年10月22日 ~ 2026年11月12日"),
        ])
        html = html.replace('</div>', table("日服当前活动", [("日服", "2026年10月1日 ~ 2026年11月1日")]) + '</div>')
        events = parse_events(html, date(2026, 10, 19))
        self.assertEqual([e['title'] for e in events], ["今天结束", "冠位战", "万圣节", "跨年CBC"])
        self.assertIsNone(events[1]['end_date'])
        self.assertTrue(events[2]['estimated'])
        self.assertEqual(events[2]['status'], "未来")
        self.assertEqual(events[3]['end_date'], "2027-01-14")
        text = '\n'.join(format_event_messages(events, "cbc"))
        self.assertIn("跨年CBC", text)
        self.assertNotIn("万圣节", text)
        self.assertNotRegex(text, r'\d{2}:\d{2}')
        self.assertIn("未找到", format_event_messages(events, "不存在")[0])

    def test_structure_and_dates_fail_visibly(self):
        bad_pages = ["<html>blocked</html>", page([], [("错误", "待定")]),
                     page([], [("错误", "2026年10月31日 ~ 2026年10月1日")])]
        for html in bad_pages:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_events(html, date(2026, 10, 19))

    def test_complete_list_splits_without_truncation(self):
        events = parse_events(page([], [(f"活动{index}", "预估 2027年1月1日 ~ 2027年1月31日")
                                       for index in range(150)]), date(2026, 10, 19))
        messages = format_event_messages(events)
        self.assertGreater(len(messages), 4)
        self.assertTrue(all(len(message) <= NODE_TEXT_LIMIT for message in messages))
        self.assertEqual('\n'.join(messages).count('[未来]'), 150)

    def test_command_search_text_and_full_list_forward(self):
        events = parse_events(page([], [(f"活动{index}", "预估 2027年1月1日 ~ 2027年1月31日")
                                       for index in range(150)]), date(2026, 10, 19))
        for command, forward in [("/fgo events 活动149", False), ("/fgo events", True),
                                 ("/fgo 活动 活动149", False), ("/fgo 活动", True)]:
            with self.subTest(command=command):
                send = SimpleNamespace(text=AsyncMock(return_value=True), forward=AsyncMock(return_value=True))
                fake = SimpleNamespace(config=plugin.FgoGachaConfig(), ctx=SimpleNamespace(send=send, api=SimpleNamespace(
                    call=AsyncMock(return_value={"user_id": 123456}))))
                with patch.object(plugin, "fetch_events", AsyncMock(return_value=events)):
                    result = asyncio.run(plugin.FgoGachaPlugin.cmd_events(fake, stream_id="stream", message=command))
                self.assertEqual(result, (True, "", True))
                if forward:
                    send.forward.assert_awaited_once()
                    self.assertFalse(send.forward.call_args.kwargs['storage_message'])
                    self.assertIn("活动日程", str(send.forward.call_args))
                else:
                    send.text.assert_awaited_once()
                    self.assertIn("活动149", send.text.call_args.args[0])
                    send.forward.assert_not_awaited()

    def test_command_reports_fetch_and_send_failures(self):
        fake = SimpleNamespace(config=plugin.FgoGachaConfig(), ctx=SimpleNamespace(
            send=SimpleNamespace(text=AsyncMock(return_value=False))))
        with patch.object(plugin, "fetch_events", AsyncMock(side_effect=ValueError("页面结构变化"))):
            result = asyncio.run(plugin.FgoGachaPlugin.cmd_events(fake, message="/fgo events"))
        self.assertIn("页面结构变化", result[1])
        with patch.object(plugin, "fetch_events", AsyncMock(return_value=[])):
            result = asyncio.run(plugin.FgoGachaPlugin.cmd_events(fake, stream_id="stream", message="/fgo events 无结果"))
        self.assertIn("发送失败", result[1])


if __name__ == "__main__":
    unittest.main()
