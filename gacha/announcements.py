"""国服公开公告的解析、缓存和日报格式化。"""
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from bs4 import BeautifulSoup
import asyncio
import json
import re
import unicodedata

from .credentials import atomic_write, json_bytes
from .events import CHINA_TZ
from .pool_messages import split_message_blocks

NEWS_SITE = "https://game.bilibili.com/fgo/news.html"
NEWS_API = "https://api.biligame.com/news"
PAGE_SIZE = 20
MAX_PAGES = 12
LOOKBACK = timedelta(days=60)
CACHE_SECONDS = 300
_IN_SCOPE = re.compile(r"限时|活动|纪念|维护|开放|更正|延期|延长|调整")
_LABEL = re.compile(r"维护时间|(?:活动|举办|开放|任务|领取|领奖|兑换|交换)(?:时间|期间|期限)")
_DATE = r"(?:[0-9]{4}年)?[0-9]{1,2}月[0-9]{1,2}日(?:\([^)]{1,12}\))?(?:维护后|维护前|[0-9]{1,2}:[0-9]{2})?"
_RANGE = re.compile(rf"(?P<start>{_DATE})(?:至|到|~|～|[-–—]+)(?P<end>{_DATE})")
_DATE_PARTS = re.compile(
    r"(?:(?P<year>[0-9]{4})年)?(?P<month>[0-9]{1,2})月(?P<day>[0-9]{1,2})日"
    r"(?:\([^)]{1,12}\))?(?P<clock>维护后|维护前|[0-9]{1,2}:[0-9]{2})?"
)


def _boundary(text: str, year: int, previous: Optional[str] = None) -> Tuple[str, str]:
    match = _DATE_PARTS.fullmatch(text)
    if match is None:
        raise ValueError("公告日期无法识别")
    month, day = int(match["month"]), int(match["day"])
    if previous and not match["year"] and (month, day) < (int(previous[5:7]), int(previous[8:10])):
        year += 1
    value = datetime(int(match["year"] or year), month, day)
    clock = match["clock"]
    if not clock or clock.startswith("维护"):
        return value.date().isoformat(), clock or ""
    hour, minute = map(int, clock.split(":"))
    if hour == 24 and minute == 0:
        return (value + timedelta(days=1)).isoformat(timespec="minutes"), ""
    return value.replace(hour=hour, minute=minute).isoformat(timespec="minutes"), ""


def parse_announcement(html: str, published: datetime) -> List[Dict[str, str]]:
    """只提取明确标注的相邻日期范围，跳过说明文字和日期表格里的逐日条目。"""
    soup = BeautifulSoup(html, "html.parser")
    selector = "p,h1,h2,h3,h4,li,td,th"
    blocks = [tag.get_text(" ", strip=True) for tag in soup.select(selector) if not tag.select_one(selector)]
    blocks = [re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)) for text in blocks if text.strip()]
    periods = []
    seen = set()
    for index, block in enumerate(blocks):
        heading = block.strip("◆◇【】[]")
        label = _LABEL.search(heading)
        if label is None:
            continue
        prefix = heading[:label.start()]
        if prefix and not (heading != block and len(prefix) <= 35):
            continue
        after_label = re.sub(r"^\([^)]*\)", "", heading[label.end():]).lstrip("◆◇】:：")
        date_text = after_label or (blocks[index + 1] if index + 1 < len(blocks) else "")
        match = _RANGE.match(date_text)
        if match is None:
            continue
        try:
            year = published.year
            if published.month == 12 and match["start"].startswith("1月"):
                year += 1
            start, start_tag = _boundary(match["start"], year)
            end, end_tag = _boundary(match["end"], int(start[:4]), start)
        except ValueError:
            continue
        if end[:10] < start[:10] or (len(start) > 10 and len(end) > 10 and end < start):
            continue
        label_text = label.group()
        kind = ("维护" if label_text.startswith("维护") else "领取" if label_text.startswith(("领取", "领奖"))
                else "兑换" if label_text.startswith(("兑换", "交换")) else "活动")
        key = (kind, start, end, start_tag, end_tag)
        if key in seen:
            continue
        seen.add(key)
        context = "".join(blocks[max(0, index - 2):index])
        section = ""
        if kind == "活动" and any(item["kind"] == "活动" for item in periods):
            if "每日任务" in context and "追加报酬" in context:
                section = "每日任务追加报酬"
            elif "登录奖励" in context:
                section = "登录奖励"
        periods.append(dict(kind=kind, section=section, start=start, end=end, start_tag=start_tag, end_tag=end_tag))
    return periods


def _load_json(path: Path) -> Dict:
    return json.loads(path.read_bytes()) if path.exists() else {}


def _get_news_json(url: str) -> Dict:
    request = Request(url, headers={
        "User-Agent": "MaiBot-FGOGacha/1.0",
        "Accept": "application/json",
        "Referer": NEWS_SITE,
    })
    with urlopen(request, timeout=12) as response:
        result = json.load(response)
    if result["code"] != 0:
        raise ValueError(f"公告接口返回错误 {result['code']}")
    return result


def _title(text: str) -> str:
    title = re.sub(r"^【限时】\s*", "", text).strip(" ！!")
    return re.sub(r"(?<=[」』])举办$", "", title)


def _updated_periods(old: List[Dict], new: List[Dict]) -> List[Tuple[Dict, Dict]]:
    old_only = [period for period in old if period not in new]
    new_only = [period for period in new if period not in old]
    changes = []
    for section in {(period["kind"], period["section"]) for period in old_only}:
        before = [period for period in old_only if (period["kind"], period["section"]) == section]
        after = [period for period in new_only if (period["kind"], period["section"]) == section]
        # 只有能一对一对应的时段才报告改期。
        if len(before) == len(after) == 1:
            changes.append((before[0], after[0]))
    return sorted(changes, key=lambda change: change[0]["start"])


class AnnouncementService:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.cache: Optional[Dict] = None
        self.next_refresh = 0.0

    async def report(self, now: datetime) -> List[str]:
        async with self.lock:
            if self.cache is None:
                self.cache = await asyncio.to_thread(_load_json, self.path)
            if monotonic() >= self.next_refresh:
                self.cache = await self._refresh(now, self.cache)
                self.next_refresh = monotonic() + CACHE_SECONDS
            return format_announcement_report(self.cache, now)

    async def _refresh(self, now: datetime, previous: Dict) -> Dict:
        sources = {}
        earliest = (now - LOOKBACK).date()
        for page in range(1, MAX_PAGES + 1):
            query = urlencode(dict(gameExtensionId=45, positionId=2, pageNum=page, pageSize=PAGE_SIZE, typeId=""))
            reply = await asyncio.to_thread(_get_news_json, f"{NEWS_API}/list.action?{query}")
            entries = reply["data"]
            for row in entries:
                published = datetime.fromisoformat(row["createTime"])
                revision = row["modifyTime"]
                if (max(published.date(), datetime.fromisoformat(revision).date()) >= earliest
                        and _IN_SCOPE.search(row["title"])
                        and ("召唤" not in row["title"] or "活动" in row["title"])):
                    sources[str(int(row["id"]))] = (published, revision)
            # 第一页可能置顶多年以前的公告，只看尾部发布时间判断是否继续分页。
            if len(entries) < PAGE_SIZE or datetime.fromisoformat(entries[-1]["createTime"]).date() < earliest:
                break

        if not sources and not previous:
            raise ValueError("公告列表未包含可解析的近期公告")
        prior = previous.get("articles", {})
        changes = [item for item in previous.get("changes", []) if item["day"] == now.date().isoformat()]
        articles = {
            key: value for key, value in prior.items()
            if key in sources or any(period["end"][:10] >= now.date().isoformat() for period in value["periods"])
        }
        pending = [key for key, (_, revision) in sources.items()
                   if key not in articles or articles[key]["revision"] != revision]

        async def detail(key: str) -> Tuple[str, Dict]:
            published, revision = sources[key]
            result = await asyncio.to_thread(_get_news_json, f"{NEWS_API}/{key}.action")
            row = result["data"]
            periods = await asyncio.to_thread(parse_announcement, row["content"], published)
            return key, dict(title=_title(row["title"]), revision=revision, periods=periods)

        for offset in range(0, len(pending), 4):
            for key, article in await asyncio.gather(*(detail(key) for key in pending[offset:offset + 4])):
                if key in prior:
                    for before, after in _updated_periods(prior[key]["periods"], article["periods"]):
                        changes.append(dict(day=now.date().isoformat(), article=key, before=before, after=after))
                articles[key] = article
        result = dict(articles=articles, changes=changes)
        if result != previous:
            await asyncio.to_thread(atomic_write, self.path, json_bytes(result))
        return result


def _maintenance_windows(articles: Dict) -> Dict[str, Optional[Dict]]:
    result = {}
    for article in articles.values():
        for period in article["periods"]:
            if period["kind"] != "维护" or len(period["start"]) <= 10 or len(period["end"]) <= 10:
                continue
            day = period["start"][:10]
            if day not in result:
                result[day] = period
            elif result[day] != period:
                # 同一天有多份不同的维护时间，缺少足够信息时保留「维护后」原文。
                result[day] = None
    return result


def _display_time(value: str, tag: str, maintenance: Dict) -> Tuple[datetime, str, bool]:
    dt = datetime.fromisoformat(value).replace(tzinfo=CHINA_TZ)
    if len(value) > 10:
        return dt, dt.strftime("%m-%d %H:%M"), True
    label = dt.strftime("%m-%d") + (f" {tag}" if tag else "（时间未公布）")
    schedule = maintenance.get(value)
    if tag and schedule:
        endpoint = "end" if tag == "维护后" else "start"
        dt = datetime.fromisoformat(schedule[endpoint]).replace(tzinfo=CHINA_TZ)
        label += f"（按维护公告预计 {dt:%H:%M}）"
    return dt, label, bool(tag and schedule)


def format_announcement_report(state: Dict, now: datetime) -> List[str]:
    articles = state["articles"]
    maintenance = _maintenance_windows(articles)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    today_maintenance = []
    starts = []
    current = []
    ends = []
    changes = []
    for article in articles.values():
        main_activity = next((period for period in article["periods"] if period["kind"] == "活动"), None)
        for period in article["periods"]:
            if period["kind"] == "维护":
                start, start_text, _ = _display_time(period["start"], period["start_tag"], {})
                end, end_text, end_known = _display_time(period["end"], period["end_tag"], {})
                if start < day_end and (end > day_start or (not end_known and end == day_start)):
                    today_maintenance.append((start, f"{start_text} 至 {end_text}（公告安排）"))
                continue
            kind = period["kind"]
            if kind == "活动" and period is not main_activity and not period["section"]:
                # 没有可辨认名称的附属时段容易被误当作主活动，不独立推送。
                continue
            start, start_text, start_known = _display_time(period["start"], period["start_tag"], maintenance)
            end, end_text, end_known = _display_time(period["end"], period["end_tag"], maintenance)
            title = article["title"] + (f" · {period['section']}" if period["section"] else "")
            if kind == "活动" and start <= now and (now <= end or (not end_known and end.date() == now.date())):
                if start_known or start.date() < now.date():
                    current.append((end, f"{title}｜截至 {end_text}"))
            if kind == "活动" and (now <= start <= now + timedelta(hours=72)
                    or (not start_known and start.date() == now.date())):
                starts.append((start, f"{start_text}｜{title}"))
            if now <= end <= now + timedelta(hours=48) or (
                not end_known and end.date() == now.date()
            ):
                deadline = f"（{kind}截止）" if kind != "活动" else ""
                ends.append((end, f"{end_text}｜{title}{deadline}"))
    for change in state.get("changes", []):
        key = change["article"]
        if change["day"] != now.date().isoformat() or key not in articles:
            continue
        before, after = change["before"], change["after"]
        if after["kind"] == "维护" or after["end"][:10] < now.date().isoformat():
            continue
        old_times, new_times = (
            " 至 ".join(_display_time(period[field], period[f"{field}_tag"], maintenance)[1] for field in ("start", "end"))
            for period in (before, after)
        )
        changes.append(f"{articles[key]['title']}（{after['section'] or after['kind']}）\n"
                       f"原 {old_times}\n现 {new_times}")
    reminders = []
    if now.weekday() == 6:
        reminders.append("今天是周日，请确认周常任务是否已完成。")
    if day_end.month != now.month:
        reminders.append("今天是月末，请确认商店圣杯、梦火、呼符是否已兑换。")
        if now.month in (2, 5, 8, 11):
            reminders.append("今天是季末，请确认商店羁绊加成礼装是否已兑换。")
    blocks = ["\n".join(reminders)] if reminders else []
    for title, rows in (
        ("今日维护", [line for _, line in sorted(today_maintenance)]),
        ("当前活动", [line for _, line in sorted(current)]),
        ("即将开启（未来 72 小时）", [line for _, line in sorted(starts)]),
        ("即将结束（未来 48 小时）", [line for _, line in sorted(ends)]),
        ("时间变更", changes),
    ):
        if rows:
            blocks.append(title + "\n" + "\n".join(dict.fromkeys(rows)))
    if not blocks:
        return []
    return split_message_blocks([f"FGO 国服活动日报 · {now:%m-%d}（北京时间）", *blocks], limit=2800)
