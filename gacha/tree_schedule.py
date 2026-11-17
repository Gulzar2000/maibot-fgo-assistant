"""当天种树预约；只存 QQ、群号和真实聊天流，不存登录凭据。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
import asyncio
import json
import re

CHINA_TZ = timezone(timedelta(hours=8))


def tree_now() -> datetime:
    return datetime.now(CHINA_TZ)


def parse_tree_time(value: str, cutoff: str, now: datetime) -> datetime:
    if re.fullmatch(r"(?:[01][0-9]|2[0-3])[:：][0-5][0-9]", value) is None:
        raise ValueError("时间须为24小时制 HH:MM，例如 21:30（也支持中文冒号）。")
    hour, minute = map(int, value.replace("：", ":").split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    limit_hour, limit_minute = map(int, cutoff.replace("：", ":").split(":"))
    if target <= now:
        raise ValueError("仅可预约当天尚未到达的时间，不会顺延到明天。")
    if (hour, minute) > (limit_hour, limit_minute):
        raise ValueError(f"预约时间不能晚于北京时间 {cutoff}。")
    return target


class TreeScheduleStore:
    def __init__(self, path: Path):
        self.path = path
        self.jobs: Dict[str, Dict[str, str]] = {}
        self.lock = asyncio.Lock()

    def _write(self, jobs: Dict[str, Dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    async def load(self, now: datetime) -> None:
        async with self.lock:
            if await asyncio.to_thread(self.path.exists):
                data = json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
                # 重启只恢复当天未来预约，错过的时间不补登录，也不跨天执行。
                jobs = {qq: job for qq, job in data.items()
                        if datetime.fromisoformat(job["at"]).date() == now.date()
                        and datetime.fromisoformat(job["at"]) > now}
                await asyncio.to_thread(self._write, jobs)
                self.jobs = jobs

    async def register(self, user_id: str, group_id: str, stream_id: str, at: datetime) -> Optional[Dict[str, str]]:
        async with self.lock:
            previous = self.jobs.get(user_id)
            jobs = {**self.jobs, user_id: {"user_id": user_id, "group_id": group_id,
                                         "stream_id": stream_id, "at": at.isoformat()}}
            await asyncio.to_thread(self._write, jobs)
            self.jobs = jobs
            return previous

    async def cancel(self, user_id: str) -> Optional[Dict[str, str]]:
        async with self.lock:
            if user_id not in self.jobs:
                return None
            jobs = dict(self.jobs)
            removed = jobs.pop(user_id)
            await asyncio.to_thread(self._write, jobs)
            self.jobs = jobs
            return removed

    async def take_due(self, now: datetime) -> List[Dict[str, str]]:
        async with self.lock:
            expired = {qq: job for qq, job in self.jobs.items() if datetime.fromisoformat(job["at"]) <= now}
            if not expired:
                return []
            jobs = {qq: job for qq, job in self.jobs.items() if qq not in expired}
            # 先持久删除再启动登录；异常或重启不会重复消费 AP。
            await asyncio.to_thread(self._write, jobs)
            self.jobs = jobs
            return [job for job in expired.values() if datetime.fromisoformat(job["at"]).date() == now.date()]
