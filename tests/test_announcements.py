"""官方公告时间解析和 QQ 日报发送状态的回归测试。"""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from test_pool_query import plugin
from gacha.announcements import CHINA_TZ, _updated_periods, format_announcement_report, parse_announcement

NOW = datetime(2026, 11, 22, 9, 30, tzinfo=CHINA_TZ)
ACTIVITY = """
<p><span>◆ 活动时间 ◆</span></p>
<p>2026年11月24日（周二）维护后～12月10日（周四）13:59为止</p>
<p>在以下时间段内，每日任务中追加报酬！</p><p>完成每日任务即可获得奖励。</p>
<p>◆活动时间（为期2周）◆</p><p>2026年11月25日0:00～12月8日23:59为止</p>
<p>◆ 领取时间 ◆</p><p>2026年11月25日0:00～12月15日23:59</p>
<p>◆ 「魔术师养成兑换券」交换期限（为期4周） ◆</p><p>2026年11月25日0:00～12月22日23:59</p>
<p>◆ 活动时间 ◆</p><p>2026年11月24日维护后～12月10日13:59</p>
"""


class AnnouncementParserTests(unittest.TestCase):
    def test_distinguishes_main_activity_subperiod_and_deadlines(self):
        periods = parse_announcement(ACTIVITY, NOW)
        self.assertEqual([period["kind"] for period in periods], ["活动", "活动", "领取", "兑换"])
        self.assertEqual(periods[0]["start_tag"], "维护后")
        self.assertEqual(periods[0]["end"], "2026-12-10T13:59")
        self.assertEqual(periods[1]["section"], "每日任务追加报酬")
        self.assertEqual(periods[1]["start"], "2026-11-25T00:00")

    def test_handles_incomplete_dates_and_year_boundary(self):
        html = "<p>◆活动时间◆ 12月31日20:00至1月3日</p><p>在1月4日至1月5日说明其他事项</p>"
        rows = parse_announcement(html, NOW.replace(month=12))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start"], "2026-12-31T20:00")
        self.assertEqual(rows[0]["end"], "2027-01-03")

    def test_report_includes_current_opening_closing_and_changes(self):
        maintenance = parse_announcement("<p>【维护时间】</p><p>2026年11月24日14:00至11月24日19:00</p>", NOW)
        opening = parse_announcement(ACTIVITY, NOW)
        old = parse_announcement("<p>◆活动时间◆ 11月20日19:00至11月23日12:59</p>", NOW)
        new = parse_announcement("<p>◆活动时间◆ 11月20日19:00至11月23日13:59</p>", NOW)
        before, after = _updated_periods(old, new)[0]
        report = "\n".join(format_announcement_report({
            "articles": {
                "1": {"title": "维护公告", "periods": maintenance},
                "2": {"title": "术阶活动", "periods": opening},
                "3": {"title": "正在进行的活动", "periods": new},
            },
            "changes": [{"day": "2026-11-22", "article": "3", "before": before, "after": after}],
        }, NOW))
        self.assertEqual(report.split("\n\n")[1], "今天是周日，请确认周常任务是否已完成。")
        self.assertIn("当前活动", report)
        self.assertIn("即将开启", report)
        self.assertIn("即将结束", report)
        self.assertIn("按维护公告预计 19:00", report)
        self.assertIn("原 11-20 19:00 至 11-23 12:59", report)
        self.assertIn("现 11-20 19:00 至 11-23 13:59", report)
        self.assertNotIn("https://", report)
        self.assertNotIn("今日维护", report)

    def test_reports_maintenance_on_its_date_and_across_midnight(self):
        periods = parse_announcement("<p>【维护时间】</p><p>11月24日23:00至11月25日01:00</p>", NOW)
        state = {"articles": {"1": {"title": "维护公告", "periods": periods}}}
        self.assertEqual(format_announcement_report(state, NOW.replace(day=23)), [])
        for day in (24, 25):
            report = "\n".join(format_announcement_report(state, NOW.replace(day=day)))
            self.assertIn("今日维护", report)
            self.assertIn("11-24 23:00 至 11-25 01:00", report)
            self.assertNotIn("https://", report)
        self.assertEqual(format_announcement_report(state, NOW.replace(day=26)), [])

    def test_calendar_reminders_send_without_announcements(self):
        sunday = "今天是周日，请确认周常任务是否已完成。"
        month_end = "今天是月末，请确认商店圣杯、梦火、呼符是否已兑换。"
        season_end = "今天是季末，请确认商店羁绊加成礼装是否已兑换。"
        for date, expected in (
            ("2026-11-29", [sunday]),
            ("2026-11-30", [month_end, season_end]),
            ("2026-12-31", [month_end]),
            ("2027-02-28", [sunday, month_end, season_end]),
            ("2028-02-28", []),
            ("2028-02-29", [month_end, season_end]),
            ("2027-05-31", [month_end, season_end]),
            ("2027-08-31", [month_end, season_end]),
        ):
            with self.subTest(date=date):
                now = datetime.fromisoformat(date).replace(hour=9, minute=30, tzinfo=CHINA_TZ)
                pages = format_announcement_report({"articles": {}}, now)
                self.assertEqual(bool(pages), bool(expected))
                self.assertEqual("\n".join(pages).partition("\n\n")[2].splitlines(), expected)


class AnnouncementDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.instance = plugin.FgoGachaPlugin()
        config = self.instance.build_default_config()
        config["tree"]["admin_qqs"] = ["12345678"]
        config["announcements"]["group_ids"] = ["100", "200", "100"]
        self.instance.set_plugin_config(config)
        self.send = AsyncMock(return_value=True)
        self.lookup = AsyncMock(side_effect=lambda group, _: {
            "session_id": f"stream-{group}", "is_group_session": True,
        })
        self.instance._set_context(SimpleNamespace(
            paths=SimpleNamespace(data_dir=Path(self.folder.name)),
            chat=SimpleNamespace(get_stream_by_group_id=self.lookup),
            send=SimpleNamespace(text=self.send),
        ))
        self.report = AsyncMock(return_value=["测试日报"])
        self.instance._announcements = SimpleNamespace(report=self.report)
        clock = patch.object(plugin, "tree_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    async def test_scheduled_send_retries_failed_group_and_remembers_success(self):
        await self.instance._send_daily_announcements(NOW.replace(minute=29))
        self.lookup.assert_not_awaited()
        self.send.side_effect = [True, False, True]
        await self.instance._send_daily_announcements(NOW)
        self.assertEqual(self.instance._load_announcement_deliveries(), {"100": "2026-11-22"})
        await self.instance._send_daily_announcements(NOW)
        self.assertEqual(self.instance._load_announcement_deliveries(), {"100": "2026-11-22", "200": "2026-11-22"})
        await self.instance._send_daily_announcements(NOW)
        self.assertEqual([call.args[1] for call in self.send.await_args_list], [
            "stream-100", "stream-200", "stream-200",
        ])

    async def test_manual_command_sends_to_current_chat_without_recording_daily_delivery(self):
        await self.instance.cmd_news("private", "87654321", "qq")
        self.report.assert_not_awaited()
        await self.instance.cmd_news("private", "12345678", "qq")
        self.send.assert_awaited_with("测试日报", "private")
        self.assertEqual(self.instance._load_announcement_deliveries(), {})
