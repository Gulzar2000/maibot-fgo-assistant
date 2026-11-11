"""QQ 账号绑定、官方扫码和真实统计；阻塞方法由调用方放入线程执行。"""
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, Optional, Set, Tuple
from uuid import uuid4
from PIL import Image, ImageDraw, ImageFont
import base64
import hashlib
import json
import os

from .account_protocol.errors import ExportError
from .account_protocol.game_profile import GameProfile, load_profile
from .account_protocol.protocol import SERVERS, GameClient
from .credentials import atomic_write, json_bytes, protect, validate_credentials
from .binding_transfer import open_binding
from .capture_import import game_history_key, merge_points, migrate_game_history
from .pool_manager import normalize_qq_user_id
from .image_gen import _find_font
from .real_statistics import build_real_statistics, count, table
from .rate_chart import CaptureReport, history_points, render_rate_chart
from .bond import BondStore, build_bond_report
from .bond_image import render_bond_report
from .saplings import SaplingClient

CAPTURE_CONFIRMATION = "你今天已经抓包过一次了，确定还要再抓包吗？"


class AccountStore:
    def __init__(self, directory: Path, profile_dir: Path, metadata: Path):
        self.directory = directory
        self.profile_dir = profile_dir
        self.metadata = metadata
        self.import_dir = profile_dir.parent / "bind-import"
        self._lock = Lock()
        self._capture_pending: Dict[str, Tuple[str, str]] = {}
        self._bond_pending: Dict[str, Tuple[str, str]] = {}
        # 同一游戏账号的登录取数和兑换共用占用状态，即使由不同 QQ 触发。
        self._capture_active: Set[str] = set()
        self._bond_store = BondStore(directory.parent / "bond_metadata_cn.json")

    @staticmethod
    def _capture_day() -> str:
        return datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    @staticmethod
    def _capture_key(server: str, user: dict) -> str:
        validate_credentials(user)
        if server not in SERVERS:
            raise ExportError("SERVER_SELECTION_REQUIRED")
        # 按游戏账号及区服识别，同账号绑定到不同 QQ 也共享当天记录。
        return hashlib.sha256(f"{server}:{int(user['uid'])}".encode()).hexdigest()

    def _capture_history(self) -> Dict[str, Dict[str, Any]]:
        path = self.directory.parent / "capture_history.json"
        if not path.exists():
            return {}
        history = json.loads(path.read_bytes())
        if not isinstance(history, dict):
            raise ValueError("抓包日期记录格式无效")
        for key, value in history.items():
            # 旧版本仅存当天抓包日期，缺少抽数时仍保留重复确认状态。
            if isinstance(value, str):
                value = history[key] = {"day": value}
            date.fromisoformat(value["day"])
        migrate_game_history(history)
        return history

    def query_real(
        self, qq: str, server: str, user: dict, cancelled: Event, *,
        confirm_repeat_capture: bool = True,
    ) -> str:
        """确认、并发占用和释放在线程中完成，协程取消不会提前释放仍在取数的账号。"""
        key = self._capture_key(server, user)
        today = self._capture_day()
        try:
            with self._lock:
                if cancelled.is_set():
                    raise ExportError("CANCELLED")
                if key in self._capture_active:
                    return "此 FGO 账号正在抓包，请稍后再试。"
                if (confirm_repeat_capture and self._capture_pending.get(qq) != (key, today)
                        and self._capture_history().get(key, {}).get("day") == today):
                    self._capture_pending[qq] = (key, today)
                    return CAPTURE_CONFIRMATION
                self._capture_pending.pop(qq, None)
                self._capture_active.add(key)
            try:
                return self._fetch(self.client(server), user, cancelled)
            finally:
                with self._lock:
                    self._capture_active.remove(key)
        finally:
            user.clear()

    def _bond_capture_days(self) -> Dict[str, str]:
        """羁绊查询的每日记录独立于真实出率抓包。调用方持有账号锁。"""
        path = self.directory.parent / "bond_capture_days.json"
        days = json.loads(path.read_bytes()) if path.exists() else {}
        if not isinstance(days, dict):
            raise ValueError("羁绊抓包日期记录格式无效")
        for day in days.values():
            date.fromisoformat(day)
        return days

    def query_bond(
        self, qq: str, server: str, user: dict, cancelled: Event, *,
        confirm_repeat_capture: bool = True,
    ) -> str:
        """羁绊取数沿用同账号互斥；快照只保留计算所需的角色进度。"""
        key = self._capture_key(server, user)
        today = self._capture_day()
        try:
            with self._lock:
                if cancelled.is_set():
                    raise ExportError("CANCELLED")
                if key in self._capture_active:
                    return "此 FGO 账号正在执行其他操作，请稍后再试。"
                pending = (key, today)
                if (confirm_repeat_capture and self._bond_pending.get(qq) != pending
                        and self._bond_capture_days().get(key) == today):
                    self._bond_pending[qq] = pending
                    return CAPTURE_CONFIRMATION
                self._bond_pending.pop(qq, None)
                self._capture_active.add(key)
            try:
                # 先备齐公共数据，下载失败时不登录游戏。
                metadata = self._bond_store.load()
                if cancelled.is_set():
                    raise ExportError("CANCELLED")
                root = self.client(server).toplogin(dict(user), lambda phase: None, cancelled.is_set)
                with self._lock:
                    days = self._bond_capture_days()
                    days[key] = self._capture_day()
                    atomic_write(self.directory.parent / "bond_capture_days.json", json_bytes(days))
                game_key = game_history_key(root)
                required_ids = {str(count(row, "svtId")) for row in
                                table(root["cache"], "userSvtCollection", "svtId") if count(row, "status") == 2}
                if required_ids - metadata["servants"].keys():
                    # 新实装从者/礼装不必等两个月；发现未知图鉴时立即更新公共索引一次。
                    metadata = self._bond_store.load(refresh=True)
                path = self.directory.parent / "bond_history.json"
                with self._lock:
                    history = json.loads(path.read_bytes()) if path.exists() else {}
                    previous = history.get(game_key)
                report = build_bond_report(root, metadata, previous)
                image = render_bond_report(report, str(self.metadata.parent))
                # 解绑和卸载的迟到请求不得写回快照。
                with self._lock:
                    if cancelled.is_set():
                        raise ExportError("CANCELLED")
                    history = json.loads(path.read_bytes()) if path.exists() else {}
                    history[game_key] = {field: report[field] for field in
                                         ("account", "captured_at", "servants", "remaining")}
                    atomic_write(path, json_bytes(history))
                return CaptureReport(f"羁绊计算：当前上限内继续提升羁绊可获得 {report['remaining']} 石。", image)
            finally:
                with self._lock:
                    self._capture_active.remove(key)
        finally:
            user.clear()

    def path(self, user_id: str) -> Path:
        return self.directory / (normalize_qq_user_id(user_id) + ".dpapi")

    def load(self, user_id: str) -> Optional[dict]:
        with self._lock:
            self._import_locked(user_id)
            path = self.path(user_id)
            if not path.exists():
                return None
            value = json.loads(protect(path.read_bytes(), True))
        if value["qq"] != normalize_qq_user_id(user_id) or value["server"] not in SERVERS:
            raise ExportError("LOCAL_CREDENTIAL_INVALID")
        validate_credentials(value["user"])
        return value

    def _import_locked(self, user_id: str) -> None:
        """管理员手动放入后，下一次 stat 按 QQ 自动导入；不接收聊天附件。"""
        qq = normalize_qq_user_id(user_id)
        incoming = self.import_dir / (qq + ".fgobind")
        if not incoming.exists():
            return
        if incoming.is_symlink() or incoming.stat().st_size > 65536:
            raise ExportError("BINDING_FILE_SIZE_INVALID")
        value = open_binding(incoming.read_bytes(), self.profile_dir / "import-private.dpapi")
        try:
            if value["qq"] != qq:
                raise ExportError("BINDING_QQ_MISMATCH")
            atomic_write(self.path(qq), protect(json_bytes(value)))
            # 已导入文件不再被扫描，避免重启或解绑后自动重新绑定。
            os.replace(incoming, self.import_dir / (qq + ".imported"))
        finally:
            value["user"].clear()

    def save(self, user_id: str, server: str, user: dict, cancelled: Event) -> None:
        if server not in SERVERS:
            raise ExportError("SERVER_SELECTION_REQUIRED")
        validate_credentials(user)
        with self._lock:
            # 取消与删除共享提交边界，解绑后迟到的网络请求不能重新写入凭据。
            if cancelled.is_set():
                raise ExportError("CANCELLED")
            value = dict(qq=normalize_qq_user_id(user_id), server=server, user=user)
            atomic_write(self.path(user_id), protect(json_bytes(value)))
            self._capture_pending.pop(normalize_qq_user_id(user_id), None)
            self._bond_pending.pop(normalize_qq_user_id(user_id), None)

    def delete(self, user_id: str) -> None:
        with self._lock:
            path = self.path(user_id)
            # 解绑只撤销授权，不读取或改写账号的抓包摘要、曲线及归属信息。
            path.unlink(missing_ok=True)
            qq = normalize_qq_user_id(user_id)
            self._capture_pending.pop(qq, None)
            self._bond_pending.pop(qq, None)
            for suffix in (".fgobind", ".imported"):
                (self.import_dir / (qq + suffix)).unlink(missing_ok=True)

    def _profile(self, server: str) -> GameProfile:
        name = "game-protocol-uo.json" if server == "uo" else "game-protocol.json"
        profile = load_profile(self.profile_dir / name)
        if profile is None:
            raise ExportError("LOCAL_PROFILE_REQUIRED")
        return profile

    def client(self, server: str) -> GameClient:
        return GameClient(self._profile(server), server)

    def exchange_saplings(self, qq: str, quantity: Optional[int], cancelled: Event) -> str:
        saved = self.load(qq)
        if saved is None:
            return "兑换失败，尚未绑定 FGO 账号。请使用 /fgo bind 查看区服选项。"
        user, server = saved["user"], saved["server"]
        try:
            key = self._capture_key(server, user)
            with self._lock:
                if key in self._capture_active:
                    return "此 FGO 账号正在执行其他操作，请稍后再试。"
                self._capture_active.add(key)
            try:
                return SaplingClient(self._profile(server), server).exchange(user, quantity, cancelled.is_set)
            finally:
                with self._lock:
                    self._capture_active.remove(key)
        finally:
            user.clear()

    def fetch(
        self, client: GameClient, user: dict, cancelled: Event,
    ) -> str:
        key = self._capture_key(client.server, user)
        with self._lock:
            if key in self._capture_active:
                raise ExportError("ACCOUNT_OPERATION_ALREADY_RUNNING")
            self._capture_active.add(key)
        try:
            return self._fetch(client, user, cancelled)
        finally:
            with self._lock:
                self._capture_active.remove(key)

    def _fetch(
        self, client: GameClient, user: dict, cancelled: Event,
    ) -> str:
        # 协议层会清空传入凭据；仅保留本次请求内存中的原始响应。
        key = self._capture_key(client.server, user)
        root = client.toplogin(dict(user), lambda phase: None, cancelled.is_set)
        captured_day = self._capture_day()
        rate = None
        try:
            metadata = json.loads(self.metadata.read_bytes())
            report, rate = build_real_statistics(root, metadata)
        finally:
            # 取到 toplogin 即记录日期。报告无法计算时保留上一份可用结果。
            with self._lock:
                history = self._capture_history()
                entry = history.setdefault(key, {})
                entry["day"] = captured_day
                points = []
                # 解绑会先取消当前任务；迟到的响应不追加观测，也不恢复登录凭据。
                if rate is not None and not cancelled.is_set():
                    # SDK uid 与 FGO 游戏 ID 不同，必须从本次实际角色表确认归属。
                    game_key = game_history_key(root)
                    game_entry = history.setdefault(game_key, {"day": captured_day})
                    points = history_points(game_entry)
                    # 旧绑定摘要仅在未曾指向其他 FGO ID 时归入当前角色。
                    # 换绑不同账号后，绝不能将旧角色的曲线带入新角色。
                    if entry.get("fgo_history_key") in (None, game_key):
                        points = merge_points(points, history_points(entry))
                        entry.pop("snapshots", None)
                        entry.pop("summary", None)
                    entry["fgo_history_key"] = game_key
                    snapshot = {
                        "snapshot_id": uuid4().hex,
                        "day": captured_day,
                        "captured_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                        **rate._asdict(),
                    }
                    points.append(snapshot)
                    game_entry["day"] = captured_day
                    game_entry["snapshots"] = points
                    game_entry["summary"] = snapshot
                atomic_write(self.directory.parent / "capture_history.json", json_bytes(history))
        chart = render_rate_chart(points) if rate is not None and not cancelled.is_set() else None
        return CaptureReport(report, chart)


def qr_image(value: str, user_nickname: str) -> str:
    import qrcode

    nickname = " ".join(user_nickname.split())
    if not nickname:
        raise ValueError("发送者 QQ 昵称不能为空")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(value)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    canvas = Image.new("RGB", (code.width + 128, code.height + 224), (235, 242, 250))
    draw = ImageDraw.Draw(canvas)
    font_path = _find_font(str(Path(__file__).resolve().parents[1] / "res"))
    background_font = ImageFont.truetype(font_path, 22)
    # 昵称仅绘制在背景，中央白底卡片会完全覆盖背景文字，不覆盖二维码或静区。
    tile_width = max(180, min(int(draw.textlength(nickname[:24], font=background_font)) + 48, canvas.width))
    for row, y in enumerate(range(0, canvas.height, 64)):
        for x in range(-tile_width if row % 2 else 0, canvas.width, tile_width):
            draw.text((x, y), nickname[:24], font=background_font, fill=(211, 223, 238))
    title_font = ImageFont.truetype(font_path, 30)
    title = nickname
    while title and draw.textlength(title, font=title_font) > canvas.width - 80:
        title = title[:-1]
    if title != nickname:
        title = title[:-1] + "…"
    draw.text((canvas.width // 2, 48), title, font=title_font, fill=(37, 59, 88), anchor="mm")
    draw.rounded_rectangle((48, 112, canvas.width - 48, 112 + code.height + 32), radius=18, fill="white")
    # 整数像素原样粘贴，不缩放、不插值；二维码自身保留四模块纯白静区。
    canvas.paste(code, (64, 128))
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
