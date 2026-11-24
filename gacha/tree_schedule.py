"""一次性预约与每日循环种树的持久化调度。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import asyncio
import json
import re

CHINA_TZ = timezone(timedelta(hours=8))
DEFAULT_TREE_CUTOFF = "22:00"


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


def tree_final_time(cutoff: str, now: datetime) -> datetime:
    hour, minute = map(int, cutoff.split(":"))
    final = now.replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(minutes=10)
    return final + timedelta(days=1) if final.date() < now.date() else final


def next_auto_tree_time(cutoff: str, after: datetime) -> datetime:
    final = tree_final_time(cutoff, after)
    for hour in (8, 17):
        slot = after.replace(hour=hour, minute=0, second=0, microsecond=0)
        if after < slot < final:
            return slot
    if after < final:
        return final
    tomorrow = after + timedelta(days=1)
    morning = tomorrow.replace(hour=8, minute=0, second=0, microsecond=0)
    return min(morning, tree_final_time(cutoff, tomorrow))


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

    async def _commit(self, jobs: Dict[str, Dict[str, str]]) -> None:
        write = asyncio.create_task(asyncio.to_thread(self._write, jobs))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # 卸载或命令取消时，等待后台写入结束再释放锁，避免磁盘与内存状态不同步。
            await write
            self.jobs = jobs
            raise
        self.jobs = jobs

    async def load(self, now: datetime, *, startup_id: str = "") -> None:
        async with self.lock:
            if await asyncio.to_thread(self.path.exists):
                data = json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
                jobs = {}
                for qq, job in data.items():
                    if startup_id and "after" in job and "startup" not in job:
                        # 旧登记迁移时视为已在本次开机期间登记，热更新不会触发登录。
                        job = {**job, "startup": startup_id}
                    if "after" in job:
                        jobs[qq] = job
                    else:
                        at = datetime.fromisoformat(job["at"])
                        if at.date() == now.date() and at > now:
                            jobs[qq] = job
                if jobs != data:
                    await self._commit(jobs)
                else:
                    self.jobs = jobs

    async def register(self, user_id: str, group_id: str, stream_id: str, at: datetime) -> Optional[Dict[str, str]]:
        return await self._register(user_id, group_id, stream_id, {"at": at.isoformat()})

    async def register_auto(self, user_id: str, group_id: str, stream_id: str,
                            now: datetime, *, startup_id: str) -> Optional[Dict[str, str]]:
        return await self._register(user_id, group_id, stream_id, {"after": now.isoformat(), "startup": startup_id})

    async def _register(self, user_id: str, group_id: str, stream_id: str,
                        timing: Dict[str, str]) -> Optional[Dict[str, str]]:
        async with self.lock:
            previous = self.jobs.get(user_id)
            jobs = {**self.jobs, user_id: {
                "user_id": user_id, "group_id": group_id, "stream_id": stream_id, **timing,
            }}
            await self._commit(jobs)
            return previous

    async def cancel(self, user_id: str, *, auto_only: bool = False) -> Optional[Dict[str, str]]:
        async with self.lock:
            job = self.jobs.get(user_id)
            if job is None or (auto_only and "after" not in job):
                return None
            jobs = dict(self.jobs)
            removed = jobs.pop(user_id)
            await self._commit(jobs)
            return removed

    async def pending(self, now: datetime, cutoff: str) -> List[Tuple[datetime, Dict[str, str]]]:
        async with self.lock:
            pending = []
            for job in self.jobs.values():
                if "after" in job:
                    after = max(now, datetime.fromisoformat(job["after"]))
                    pending.append((next_auto_tree_time(cutoff, after), dict(job)))
                else:
                    at = datetime.fromisoformat(job["at"])
                    if at.date() != now.date() or at <= now:
                        continue
                    pending.append((at, dict(job)))
            return sorted(pending, key=lambda item: (item[0], item[1]["user_id"]))

    async def take_due(self, now: datetime, cutoff: str = DEFAULT_TREE_CUTOFF, *,
                       startup_id: str = "") -> List[Dict[str, str]]:
        async with self.lock:
            jobs = {}
            due = []
            # 只看当前分钟及未来班次，跨天或休眠后不补跑错过的班次。
            current_minute = now.replace(second=0, microsecond=0)
            for qq, job in self.jobs.items():
                if "after" in job:
                    after = max(datetime.fromisoformat(job["after"]), current_minute - timedelta.resolution)
                    startup_due = bool(startup_id and job.get("startup") != startup_id)
                    if startup_due or next_auto_tree_time(cutoff, after) <= now:
                        # 开机与固定班次重合时只消费一次，末班之后仍保留登记。
                        due.append(job)
                        job = {**job, "after": now.isoformat()}
                        if startup_due:
                            job["startup"] = startup_id
                    jobs[qq] = job
                else:
                    at = datetime.fromisoformat(job["at"])
                    if at.date() == now.date():
                        if at > now:
                            jobs[qq] = job
                        else:
                            due.append(job)
            if jobs != self.jobs:
                # 先持久推进或删除记录，再启动登录，重启后不会重复执行同一班次。
                await self._commit(jobs)
            return due
