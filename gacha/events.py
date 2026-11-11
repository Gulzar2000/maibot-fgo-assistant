"""Mooncell 国服当前及未来活动日程，只展示日期，不自行推算排期。"""
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import quote
from bs4 import BeautifulSoup
import asyncio
import re
import unicodedata

from .downloader import fetch_text
from .pool_messages import split_message_blocks

EVENTS_PAGE = "https://fgo.wiki/w/" + quote("活动总览")
CHINA_TZ = timezone(timedelta(hours=8))
DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

# 只维护明确的名称片段，避免用“夏”“海”等单字误收无关活动。
DEFAULT_ALIAS_RULES = [
    {"aliases": ["剑冠", "剑阶冠位战"], "titles": ["冠位戴冠战：Saber"]},
    {"aliases": ["弓冠", "弓阶冠位战"], "titles": ["冠位戴冠战：Archer"]},
    {"aliases": ["枪冠", "枪阶冠位战"], "titles": ["冠位戴冠战：Lancer"]},
    {"aliases": ["骑冠", "骑阶冠位战"], "titles": ["冠位戴冠战：Rider"]},
    {"aliases": ["术冠", "术阶冠位战"], "titles": ["冠位戴冠战：Caster"]},
    {"aliases": ["杀冠", "杀阶冠位战"], "titles": ["冠位戴冠战：Assassin"]},
    {"aliases": ["狂冠", "狂阶冠位战"], "titles": ["冠位戴冠战：Berserker"]},
    {"aliases": ["额外冠", "EX冠", "额外职阶冠位战"], "titles": ["冠位戴冠战：Extra"]},
    {"aliases": ["冠位战", "戴冠战"], "titles": ["冠位戴冠战"]},
    {"aliases": ["泳装", "夏活", "泳装活动"],
     "titles": ["泳装", "Servant Summer", "迦勒底·U－夏日岛", "迦勒底南海大决战"]},
    {"aliases": ["万圣节", "万圣"], "titles": ["万圣节", "南瓜农场屠杀"]},
    {"aliases": ["圣诞", "圣诞节", "圣诞活动"], "titles": ["圣诞", "Christmas"]},
    {"aliases": ["情人节", "情人节活动"], "titles": ["情人节", "Valentine"]},
    {"aliases": ["白情", "白色情人节", "男性精选"], "titles": ["CBC", "迦勒底男性精选"]},
    {"aliases": ["周年", "周年庆"], "titles": ["Anniversary", "周年"]},
    {"aliases": ["新年", "元旦", "新年活动"], "titles": ["新年活动"]},
    {"aliases": ["咕哒咕哒", "咕哒", "唠唠叨叨", "唠唠"], "titles": ["唠唠叨叨", "咕哒咕哒", "GUDAGUDA"]},
    {"aliases": ["巡灵", "巡灵祝祭"], "titles": ["巡灵的祝祭"]},
    {"aliases": ["水怪", "水怪复刻"], "titles": ["水怪危机"]},
    {"aliases": ["阎魔亭", "雀旅馆"], "titles": ["雀之旅馆", "阎魔亭繁盛记"]},
    {"aliases": ["FSF", "FSF联动", "伪街联动"], "titles": ["Fate/strange Fake", "剽灭十字前线", "伪典共鸣幻想"]},
    {"aliases": ["FSR", "FSR联动", "盈月"], "titles": ["盈月剑风帖"]},
]


def normalize_event_keyword(text: str) -> str:
    """统一全半角、空白及英文大小写，同时保留名称中的其他标点。"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def parse_events(html: str, today: date) -> List[dict]:
    """只读取国服两张表；结束当天仍展示，保留未知结束日期和来源预估标记。"""
    soup = BeautifulSoup(html, "html.parser")
    sections = set()
    events = {}
    for table in soup.select(".mw-parser-output table"):
        headers = [cell.get_text(strip=True) for cell in table.select("tr:first-child > th")]
        section = next((name for name in ("国服当前活动", "国服未来活动") if name in headers), None)
        if section is None:
            continue
        sections.add(section)
        for row in table.find_all("tr", recursive=False) + table.select("tbody > tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            if len(cells) < 3:
                raise ValueError("国服活动表格列数不足")
            # 名称格先放横幅图片链接，再放文字链接，不能把图片链接的空文字当名称。
            link = next((link for link in cells[1].find_all("a", href=True)
                         if link.get_text(strip=True)), None)
            if link is None or not link["href"].startswith("/w/"):
                raise ValueError("国服活动行缺少有效名称或页面链接")
            title = link.get_text(strip=True)
            time_text = cells[2].get_text(" ", strip=True)
            dates = [date(*(int(value) for value in match.groups()))
                     for match in DATE_PATTERN.finditer(time_text)]
            unknown_end = "无明确结束时间" in time_text
            if len(dates) != (1 if unknown_end else 2):
                raise ValueError(f"活动日期格式无法识别: {title}: {time_text}")
            start = dates[0]
            end = None if unknown_end else dates[1]
            if end is not None and end < start:
                raise ValueError(f"活动结束日期早于开始日期: {title}")
            if end is not None and end < today:
                continue
            href = "https://fgo.wiki" + link["href"]
            events.setdefault(href, {
                "title": title, "href": href, "start_date": start.isoformat(),
                "end_date": end.isoformat() if end is not None else None,
                "estimated": "预估" in time_text or "预计" in time_text,
                "status": "当前" if start <= today else "未来",
            })
    if sections != {"国服当前活动", "国服未来活动"}:
        raise ValueError("Mooncell 页面缺少国服当前或未来活动表格")
    if not events:
        raise ValueError("Mooncell 页面没有有效的国服当前或未来活动")
    return sorted(events.values(), key=lambda event: (event["start_date"], event["title"]))


async def fetch_events() -> List[dict]:
    html = await fetch_text(EVENTS_PAGE)
    return await asyncio.to_thread(parse_events, html, datetime.now(CHINA_TZ).date())


def format_event_messages(
    events: List[dict], keyword: str = "", today: Optional[date] = None,
    alias_rules: Optional[List[dict]] = None,
) -> List[str]:
    """保留名称包含搜索；完整命中别名时扩展名称片段，分段保留全部结果。"""
    keyword = keyword.strip()
    query = normalize_event_keyword(keyword)
    terms = {query}
    rules = DEFAULT_ALIAS_RULES if alias_rules is None else alias_rules
    if query:
        for rule in rules:
            if query in {normalize_event_keyword(alias) for alias in rule["aliases"]}:
                terms.update(normalize_event_keyword(title) for title in rule["titles"])
        # 空片段属于配置错误，不能变成匹配全部活动的通配符。
        if "" in terms:
            raise ValueError("活动别名的目标名称片段不能为空")
    matches = [event for event in events
               if any(term in normalize_event_keyword(event["title"]) for term in terms)]
    if not matches:
        return [f"未找到与“{keyword}”匹配的国服当前或未来活动（含名称及别名）。"]
    query_date = today if today is not None else datetime.now(CHINA_TZ).date()
    header = [f"FGO 国服活动日程（截至 {query_date.isoformat()}，共 {len(matches)} 项）"]
    if keyword:
        header.append(f"关键词：{keyword}")
    header.extend(["预估日期来自 Mooncell，以国服实际公告为准。", "来源：https://fgo.wiki/w/活动总览"])
    blocks = ["\n".join(header)]
    for event in matches:
        end = event["end_date"] if event["end_date"] is not None else "无明确结束日期"
        estimated = "（预估）" if event["estimated"] else ""
        blocks.append(f"[{event['status']}] {event['title']}\n{event['start_date']} ～ {end}{estimated}")
    return split_message_blocks(blocks)
