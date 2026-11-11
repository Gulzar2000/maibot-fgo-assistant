"""FGO小助手 - MaiBot 插件

基于 kcn3388/fgogacha 移植，提供 FGO 模拟抽卡功能。
支持十连/百连、卡池切换、日替卡池、在线数据更新。
"""
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Set
from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from pydantic import field_validator
import asyncio
import base64
import logging
import os
import re

from .gacha.engine import (
    format_hundred_pull_result_text,
    format_ten_pull_result_text,
)
from .gacha.pool_fetcher import fetch_pool_list
from .gacha.card_assets import download_result_card, ensure_result_cards, load_thumbnail_index
from .gacha.rate_builder import fetch_scheduled_pool_data
from .gacha.pool_manager import DrawLimitError, PoolManager, normalize_qq_user_id
from .gacha.pool_messages import build_nested_forward_nodes, format_pool_messages
from .gacha.image_gen import generate_ten_pull_image
from .gacha.downloader import build_card_name_map
from .gacha.statistics import format_draw_statistics
from .gacha.accounts import AccountStore, qr_image
from .gacha.rate_chart import CaptureReport
from .gacha.account_protocol.errors import MAINTENANCE_MESSAGE, ExportError
from .gacha.credentials import validate_credentials
from .gacha.events import DEFAULT_ALIAS_RULES, fetch_events, format_event_messages, normalize_event_keyword
from .gacha.material_image import render_material_report
from .gacha.materials import MaterialQueryError, MaterialService
from .gacha.bond import BondDataError

logger = logging.getLogger(__name__)


class GachaConfig(PluginConfigBase):
    __ui_label__ = "抽卡设置"
    __ui_icon__ = "gamepad-2"
    __ui_order__ = 1
    display_mode: str = Field(
        default="image",
        description="抽卡展示样式: text(文字版) / image(图片版)",
        json_schema_extra={"label": "展示样式", "x-widget": "select", "enum_values": ["image", "text"]},
    )
    follow_pool_type: str = Field(
        default="latest",
        description="自动更新跟随: latest(最新卡池) / story(剧情卡池)",
        json_schema_extra={"label": "跟随策略", "x-widget": "select", "enum_values": ["latest", "story"]},
    )
    daily_limit: int = Field(
        default=100,
        ge=1,
        description="每位QQ每日实际抽卡次数上限，十一连计11次，百连计110次",
        json_schema_extra={"label": "每日上限", "min": 1},
    )
    cooldown_seconds: int = Field(
        default=15,
        ge=0,
        description="每位QQ抽卡冷却时间(秒)，0=无冷却",
        json_schema_extra={"label": "冷却时间", "min": 0, "max": 300},
    )
    auto_update_hours: int = Field(
        default=0,
        description="自动更新卡池间隔(小时)，0=禁用",
        json_schema_extra={"label": "自动更新", "min": 0, "max": 168},
    )


class EventAliasRule(PluginConfigBase):
    # SDK 生成 WebUI 列表项时需要独立构造一条默认规则。
    aliases: List[str] = Field(
        default_factory=lambda: list(DEFAULT_ALIAS_RULES[0]["aliases"]),
        min_length=1, description="可用于查询的别名，完整匹配后扩展搜索",
    )
    titles: List[str] = Field(
        default_factory=lambda: list(DEFAULT_ALIAS_RULES[0]["titles"]),
        min_length=1, description="活动名称片段，包含任意一项即可匹配",
    )

    @field_validator("aliases", "titles")
    @classmethod
    def validate_nonempty_terms(cls, terms: List[str]) -> List[str]:
        if any(not normalize_event_keyword(term) for term in terms):
            raise ValueError("活动别名和目标名称片段不能为空白")
        return terms


class EventsConfig(PluginConfigBase):
    __ui_label__ = "活动查询"
    __ui_icon__ = "calendar"
    __ui_order__ = 2
    alias_rules: List[EventAliasRule] = Field(
        default_factory=lambda: [EventAliasRule(**rule) for rule in DEFAULT_ALIAS_RULES],
        description="活动别名规则；配置此列表会替换默认规则，空列表关闭别名扩展",
    )


class RealStatisticsConfig(PluginConfigBase):
    __ui_label__ = "真实出率"
    __ui_icon__ = "chart-bar"
    __ui_order__ = 3
    confirm_repeat_capture: bool = Field(default=True, description="当天重复抓包前要求再次发送命令确认")


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.4", description="配置版本")


class FgoGachaConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    gacha: GachaConfig = Field(default_factory=GachaConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    real_statistics: RealStatisticsConfig = Field(default_factory=RealStatisticsConfig)


class FgoGachaPlugin(MaiBotPlugin):
    config_model = FgoGachaConfig

    def __init__(self):
        super().__init__()
        self._pool_manager: Optional[PoolManager] = None
        self._update_task: Optional[asyncio.Task] = None
        self._download_task: Optional[asyncio.Task] = None
        self._pool_refresh_lock = asyncio.Lock()
        self._account_jobs: Dict[str, asyncio.Task] = {}
        self._account_cancels: Dict[str, Event] = {}
        self._account_unbinding: Set[str] = set()
        self._accounts: Optional[AccountStore] = None
        self._materials: Optional[MaterialService] = None

    async def on_load(self) -> None:
        data_dir = os.path.join(self.ctx.paths.data_dir, "fgogacha")
        os.makedirs(data_dir, exist_ok=True)
        self._pool_manager = PoolManager(data_dir)

        if self.config.gacha.auto_update_hours > 0:
            self._start_auto_update()

    async def on_unload(self) -> None:
        for cancel in self._account_cancels.values():
            cancel.set()
        jobs = list(self._account_jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._account_jobs.clear()
        self._account_cancels.clear()
        if self._download_task:
            self._download_task.cancel()
            self._download_task = None
        if self._update_task:
            self._update_task.cancel()
            self._update_task = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if self._update_task:
            self._update_task.cancel()
        if self.config.gacha.auto_update_hours > 0:
            self._start_auto_update()

    def _start_auto_update(self) -> None:
        async def _loop():
            while True:
                await asyncio.sleep(self.config.gacha.auto_update_hours * 3600)
                try:
                    await self._do_fetch_pools(silent=True)
                except Exception as e:
                    logger.warning(f"自动更新卡池失败: {e}")

        self._update_task = asyncio.ensure_future(_loop())

    @property
    def pm(self) -> PoolManager:
        if self._pool_manager is None:
            data_dir = os.path.join(self.ctx.paths.data_dir, "fgogacha")
            os.makedirs(data_dir, exist_ok=True)
            self._pool_manager = PoolManager(data_dir)
        return self._pool_manager

    @property
    def icon_dir(self) -> str:
        return os.path.join(self.ctx.paths.data_dir, "fgogacha", "icons")

    @property
    def res_dir(self) -> str:
        return os.path.join(os.path.dirname(__file__), "res")

    @property
    def accounts(self) -> AccountStore:
        if self._accounts is None:
            self._accounts = AccountStore(
                Path(self.ctx.paths.data_dir) / "fgogacha" / "bindings",
                Path(__file__).parent / "private",
                Path(self.res_dir) / "gacha_metadata_cn.json",
            )
        return self._accounts

    def _start_account_job(self, user_id: str, stream_id: str, operation) -> bool:
        if user_id in self._account_jobs or user_id in self._account_unbinding:
            return False
        cancel = Event()
        self._account_cancels[user_id] = cancel

        async def run():
            try:
                await operation(cancel)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 不记录异常报文或 traceback，避免第三方异常携带登录凭据。
                code = error.code if isinstance(error, ExportError) else type(error).__name__
                logger.error("FGO 账号操作失败：%s", code)
                if code == "SERVER_MAINTENANCE":
                    await self.ctx.send.text(MAINTENANCE_MESSAGE, stream_id)
                    return
                await self.ctx.send.text(
                    f"FGO 账号操作失败（{code}）。授权失效请重新 /fgo bind；其他错误请联系管理员。",
                    stream_id,
                )
            finally:
                cancel.set()
                self._account_jobs.pop(user_id, None)
                self._account_cancels.pop(user_id, None)

        self._account_jobs[user_id] = asyncio.create_task(run())
        return True

    @Command(
        name="fgo_bind", description="扫码绑定 FGO 账号：1 哔哩安卓、2 iOS、3 享游安卓；群聊发送时 @ 发起者",
        pattern=r"^/fgo\s+(?:bind|绑定)(?:\s+[123])?\s*$",
    )
    async def cmd_bind(self, stream_id: str = "", user_id: str = "", platform: str = "",
                       group_id: str = "", message: Any = "", **kwargs) -> tuple[bool, str, bool]:
        if platform != "qq" or not stream_id:
            return True, "/fgo bind 仅支持 QQ 群聊或私聊。", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, str(error), True
        text = kwargs.get("text", message if isinstance(message, str) else "")
        match = re.fullmatch(r"/fgo\s+(?:bind|绑定)(?:\s+([123]))?\s*", text)
        usage = "请选择要绑定的服务器（也可使用 /fgo 绑定 <区服编号>）：\n/fgo bind 1 — 哔哩安卓\n/fgo bind 2 — iOS\n/fgo bind 3 — 享游安卓"
        if match is None or match.group(1) is None:
            await self.ctx.send.text(usage, stream_id)
            return True, usage, True
        server = {"1": "android", "2": "ios", "3": "uo"}[match.group(1)]
        try:
            user_nickname = message["message_info"]["user_info"]["user_nickname"]
            if not isinstance(user_nickname, str) or not user_nickname.strip():
                raise ValueError("发送者 QQ 昵称为空或格式错误")
        except (KeyError, TypeError, ValueError) as error:
            return True, f"无法获取绑定者昵称：{error}，未开始扫码绑定。", True
        # 核验真实聊天流；账号归属仍只使用命令发送者 QQ，不能使用群号。
        if group_id:
            stream = await self.ctx.chat.get_stream_by_group_id(group_id, "qq")
        else:
            stream = await self.ctx.chat.get_stream_by_user_id(user_id, "qq")
        if not stream or stream["session_id"] != stream_id or stream["is_group_session"] != bool(group_id):
            return True, "无法确认 QQ 聊天会话，未开始绑定。", True

        async def bind(cancel: Event):
            client = await asyncio.to_thread(self.accounts.client, server)
            ticket, value = await asyncio.to_thread(client.generate)
            image = await asyncio.to_thread(qr_image, value, user_nickname)
            prompt = (
                "请本人在 180 秒内扫码并确认登录。区服：" + {"android": "哔哩安卓", "ios": "iOS", "uo": "享游安卓"}[server]
                + "。凭据加密保存在机器人本机，可用 /fgo unbind 删除。"
            )
            # 群聊将真正的 @、提示和二维码放在同一条消息中；私聊不加 @。
            segments = []
            if group_id:
                segments.append({"type": "at", "data": {"target_user_id": user_id}})
            segments.extend([
                {"type": "text", "content": "\n" + prompt if group_id else prompt},
                {"type": "image", "binary_data_base64": image},
            ])
            if not await self.ctx.send.hybrid(segments, stream_id, storage_message=False):
                raise ExportError("QR_SEND_FAILED")
            value = image = None
            deadline = asyncio.get_running_loop().time() + 180
            user = {}
            try:
                while asyncio.get_running_loop().time() < deadline:
                    phase, user = await asyncio.to_thread(client.poll, ticket)
                    if phase == "authorized":
                        validate_credentials(user)
                        # 验证所选区服有可读取角色后才替换绑定，失败保留原绑定。
                        report = await asyncio.to_thread(
                            self.accounts.fetch, client, user, cancel,
                        )
                        await asyncio.to_thread(self.accounts.save, user_id, server, user, cancel)
                        await self._send_capture_report(report, stream_id, "FGO 账号绑定成功。\n")
                        return
                    if phase == "expired":
                        break
                    await asyncio.sleep(2)
                await self.ctx.send.text("二维码已过期，请重新 /fgo bind。", stream_id)
            finally:
                user.clear()

        if not self._start_account_job(user_id, stream_id, bind):
            return True, "已有账号操作正在进行，可用 /fgo unbind 取消。", True
        return True, "", True

    @Command(name="fgo_unbind", description="解除当前 QQ 的 FGO 绑定并删除本地登录凭据",
             pattern=r"^/fgo\s+(?:unbind|解绑)\s*$")
    async def cmd_unbind(self, stream_id: str = "", user_id: str = "", platform: str = "", **kwargs) -> tuple[bool, str, bool]:
        if platform != "qq":
            return True, "仅支持 QQ 账号解除绑定。", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, str(error), True
        if user_id in self._account_unbinding:
            return True, "正在解除绑定，请稍候。", True
        self._account_unbinding.add(user_id)
        try:
            cancel = self._account_cancels.get(user_id)
            if cancel is not None:
                cancel.set()
                job = self._account_jobs[user_id]
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                # 任务可能在首次调度前被取消，此时协程的 finally 尚未运行。
                self._account_jobs.pop(user_id, None)
                self._account_cancels.pop(user_id, None)
            await asyncio.to_thread(self.accounts.delete, user_id)
        finally:
            self._account_unbinding.discard(user_id)
        text = "已解除 FGO 绑定并删除本地登录凭据，真实出率历史与模拟抽卡记录保留。"
        await self.ctx.send.text(text, stream_id)
        return True, text, True

    async def _ack(self, stream_id: str, msg: str) -> None:
        """发送即时确认消息"""
        try:
            await self.ctx.send.text(msg, stream_id)
        except Exception:
            pass

    # === /fgo help — 指令帮助 ===

    @Command(
        name="fgo_help",
        description="查看FGO小助手指令帮助",
        pattern=r"^/fgo(?:\s+(?:help|帮助))?\s*$",
    )
    async def cmd_help(self, stream_id: str = "", **kwargs) -> tuple[bool, str, bool]:
        text = (
            "FGO小助手\n"
            "\n"
            "【模拟抽卡】\n"
            "/fgo 10 — 模拟十一连\n"
            "/fgo 100 — 模拟十次十一连，共110抽\n"
            "/fgo pools — 查看国服当前及未来卡池\n"
            "/fgo switch <卡池编号> [子池编号] — 切换模拟卡池\n"
            "/fgo stat — 查看累计模拟出率与评级\n"
            "\n"
            "模拟抽卡有每日额度和冷却，按QQ独立计算。\n"
            "单五星UP池支持330抽保底，切换卡池会重置保底进度。\n"
            "\n"
            "【真实账号】\n"
            "/fgo bind <区服编号> — 扫码绑定，1=哔哩安卓、2=iOS、3=享游安卓；不填编号查看提示\n"
            "/fgo unbind — 解除绑定并删除保存的登录凭据\n"
            "/fgo stat real — 抓取真实账号的AP、五星出率与评级\n"
            "/fgo 羁绊计算 或 /fgo 挖矿 — 查询羁绊剩余圣晶石与进度变化\n"
            "/fgo tree [兑换数] — 兑换青铜果实，省略数量则兑换最多可换个数\n"
            "\n"
            "每个青铜果实消耗40 AP和1株青铜树苗。\n"
            "真实账号查询及兑换需要先绑定，登录可能使游戏掉线。\n"
            "\n"
            "【活动查询】\n"
            "/fgo events [关键词] — 查询国服活动排期\n"
            "例如 /fgo events 泳装、/fgo events 术冠\n"
            "/fgo material <素材> — 图片汇总 Free 本效率、活动奖励和未来交换券\n"
            "例如 /fgo 素材 羽毛、/fgo material 术金饼干\n"
            "\n"
            "/fgo help — 查看本帮助\n"
            "\n"
            "【中文别名】\n"
            "保留 /fgo 前缀，参数用法与原指令相同：\n"
            "帮助=help，卡池=pools，切换卡池=switch\n"
            "模拟出率=stat，真实出率/抓包=stat real\n"
            "绑定=bind，解绑=unbind，种树=tree，活动=events，素材=material"
        )
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, True

    @Command(
        name="fgo_material",
        description="查询国服普通素材、棋子和技能石，以单图汇总 Free 本 AP 排行、当前及未来活动和未来交换券",
        pattern=r"^/fgo\s+(?:material|素材)(?:\s+[^\r\n]+)?\s*$",
    )
    async def cmd_material(self, stream_id: str = "", message: str = "", **kwargs) -> tuple[bool, str, bool]:
        msg_text = str(kwargs.get("text", message) if isinstance(message, dict) else message)
        match = re.fullmatch(r"/fgo\s+(?:material|素材)\s+([^\r\n]+)", msg_text.strip())
        if match is None:
            return True, "用法：/fgo material 羽毛 或 /fgo 素材 羽毛。支持普通素材、棋子和技能石的名称或别名。", True
        if not stream_id:
            return True, "素材查询图片需要在 QQ 聊天中使用。", True
        try:
            if self._materials is None:
                self._materials = MaterialService(Path(self.ctx.paths.data_dir) / "fgogacha" / "materials")
            report = await self._materials.query(match.group(1).strip())
            image = await asyncio.to_thread(render_material_report, report, self.res_dir)
        except MaterialQueryError as error:
            return True, str(error), True
        except Exception:
            logger.exception("FGO 素材数据查询或图片生成失败")
            return True, "素材查询失败：数据获取、校验或图片生成出错，详情已记录日志，请稍后重试。", True
        try:
            if not await self.ctx.send.image(image, stream_id):
                raise RuntimeError("素材图片发送服务返回失败")
        except Exception:
            logger.exception("FGO 素材图片发送失败")
            return True, "素材图片已生成，但 QQ 发送失败，请稍后重试。", True
        return True, "", True

    # === /fgo events [关键词] — 国服活动排期 ===

    @Command(
        name="fgo_events",
        description="查询国服当前及未来活动的起止日期，可按活动名称关键词或别名筛选",
        pattern=r"^/fgo\s+(?:events|活动)(?:\s+([^\r\n]+))?\s*$",
    )
    async def cmd_events(self, stream_id: str = "", message: str = "", **kwargs) -> tuple[bool, str, bool]:
        msg_text = str(kwargs.get("text", message) if isinstance(message, dict) else message)
        match = re.fullmatch(r"/fgo\s+(?:events|活动)(?:\s+([^\r\n]+))?", msg_text.strip())
        if match is None:
            return False, "用法：/fgo events [关键词] 或 /fgo 活动 [关键词]", True
        keyword = (match.group(1) or "").strip()
        try:
            events = await fetch_events()
            messages = format_event_messages(
                events, keyword, alias_rules=[rule.model_dump() for rule in self.config.events.alias_rules]
            )
        except Exception as error:
            logger.exception("国服活动排期查询失败")
            return True, f"活动排期获取失败：{error}。请稍后重新使用 /fgo events。", True
        if not stream_id:
            return True, "\n\n".join(messages), True
        try:
            if len(messages) == 1:
                sent = await self.ctx.send.text(messages[0], stream_id)
            else:
                login = await self.ctx.api.call("adapter.napcat.system.get_login_info", version="1")
                if not isinstance(login, dict):
                    raise ValueError("NapCat 未返回登录账号信息")
                account_id = str(login["user_id"])
                if not account_id.isascii() or not account_id.isdigit() or int(account_id) <= 0:
                    raise ValueError("NapCat 返回的登录 QQ 号无效")
                nodes = build_nested_forward_nodes(messages, account_id, title="FGO 国服活动日程")
                sent = await self.ctx.send.forward(nodes, stream_id, storage_message=False)
            if not sent:
                raise RuntimeError("活动日程发送失败，详情见发送服务与 NapCat 日志")
        except Exception as error:
            logger.exception("国服活动日程发送失败")
            return True, f"活动排期已获取，但发送失败：{error}。", True
        return True, "", True

    # === /fgo stat — 个人累计出率 ===

    @Command(
        name="fgo_stat",
        description="查看发送者的模拟出率",
        pattern=r"^/fgo\s+(?:stat|模拟出率)\s*$",
    )
    async def cmd_stat(self, stream_id: str = "", user_id: str = "", platform: str = "", **kwargs) -> tuple[bool, str, bool]:
        if platform != "qq":
            return True, "仅支持查询 QQ 账号的 FGO 出率。", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, f"{error}，无法查询统计。", True

        total_draws, ssr_count = await asyncio.to_thread(self.pm.get_user_statistics, user_id)
        text = "模拟出率：" + format_draw_statistics(total_draws, ssr_count).removeprefix("你的模拟抽卡五星出率为")
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, True

    @Command(
        name="fgo_stat_real",
        description="查询发送者绑定 FGO 账号的真实出率",
        pattern=r"^/fgo\s+(?:stat\s+real|真实出率|抓包)\s*$",
    )
    async def cmd_stat_real(self, stream_id: str = "", user_id: str = "", platform: str = "", **kwargs) -> tuple[bool, str, bool]:
        if platform != "qq":
            return True, "仅支持查询 QQ 账号的 FGO 出率。", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, f"{error}，无法查询统计。", True

        async def query(cancel: Event):
            saved = None
            try:
                saved = await asyncio.to_thread(self.accounts.load, user_id)
                if saved is None:
                    real = "真实出率：尚未绑定，请使用 /fgo bind 扫码绑定。"
                else:
                    settings = self.config.real_statistics
                    real = await asyncio.to_thread(
                        self.accounts.query_real, user_id, saved["server"], dict(saved["user"]), cancel,
                        confirm_repeat_capture=settings.confirm_repeat_capture,
                    )
            except Exception as error:
                code = error.code if isinstance(error, ExportError) else type(error).__name__
                logger.error("FGO 真实统计查询失败：%s", code)
                if code == "SERVER_MAINTENANCE":
                    real = MAINTENANCE_MESSAGE
                else:
                    real = f"真实出率：查询失败（{code}），授权失效请重新 /fgo bind。"
            finally:
                if saved is not None:
                    saved["user"].clear()
            await self._send_capture_report(real, stream_id)

        if not self._start_account_job(user_id, stream_id, query):
            return True, "已有账号操作正在进行，请稍后再试。", True
        return True, "", True

    @Command(
        name="fgo_bond",
        description="计算绑定账号的羁绊剩余圣晶石并对比上次查询",
        pattern=r"^/fgo\s+(?:羁绊计算|挖矿)\s*$",
    )
    async def cmd_bond(self, stream_id: str = "", user_id: str = "", platform: str = "", **kwargs) -> tuple[bool, str, bool]:
        if platform != "qq":
            return True, "羁绊计算仅支持 QQ 绑定账号。", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, f"{error}，无法查询羁绊。", True

        async def query(cancel: Event):
            saved = await asyncio.to_thread(self.accounts.load, user_id)
            if saved is None:
                await self.ctx.send.text("尚未绑定 FGO 账号，请使用 /fgo bind 扫码绑定。", stream_id)
                return
            try:
                report = await asyncio.to_thread(
                    self.accounts.query_bond, user_id, saved["server"], dict(saved["user"]), cancel,
                    confirm_repeat_capture=self.config.real_statistics.confirm_repeat_capture,
                )
            except BondDataError as error:
                await self.ctx.send.text("羁绊计算失败：" + str(error), stream_id)
                return
            finally:
                saved["user"].clear()
            if not cancel.is_set():
                await self._send_capture_report(report, stream_id)

        if not self._start_account_job(user_id, stream_id, query):
            return True, "已有账号操作正在进行，请稍后再试。", True
        return True, "", True

    async def _send_capture_report(self, report: str, stream_id: str, prefix: str = "") -> None:
        if isinstance(report, CaptureReport) and report.image_base64:
            sent = await self.ctx.send.hybrid([
                {"type": "text", "content": prefix + report},
                {"type": "image", "binary_data_base64": report.image_base64},
            ], stream_id)
        else:
            sent = await self.ctx.send.text(prefix + report, stream_id)
        if not sent:
            raise ExportError("STATISTICS_SEND_FAILED")

    @Command(
        name="fgo_tree",
        description="兑换青铜果实，省略兑换数则按当前 AP 和库存兑换最大个数",
        pattern=r"^/fgo\s+(?:tree|种树)(?:[ \t]+[^\r\n]*)?\s*$",
    )
    async def cmd_tree(self, stream_id: str = "", user_id: str = "", platform: str = "",
                       message: Any = "", **kwargs) -> tuple[bool, str, bool]:
        if platform != "qq" or not stream_id:
            return True, "仅支持在 QQ 中兑换青铜果实。", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, f"{error}，无法兑换。", True
        text = kwargs.get("text", message if isinstance(message, str) else "")
        match = re.fullmatch(r"/fgo\s+(?:tree|种树)(?:[ \t]+([0-9]{1,9}))?\s*", text)
        quantity = int(match[1]) if match and match[1] is not None else None
        if match is None or quantity == 0:
            usage = "兑换数须为正整数。中文别名：/fgo 种树 [兑换数]。发送 /fgo tree 兑换当前最大个数，或 /fgo tree 1 兑换 1 个。"
            await self.ctx.send.text(usage, stream_id)
            return True, usage, True

        async def exchange(cancel: Event):
            try:
                reply = await asyncio.to_thread(self.accounts.exchange_saplings, user_id, quantity, cancel)
            except ExportError as error:
                if error.code == "SERVER_MAINTENANCE":
                    reply = MAINTENANCE_MESSAGE
                elif error.code == "PURCHASE_RESULT_UNCONFIRMED":
                    reply = "兑换结果未确认，请先在游戏内核对青铜果实数量，再决定是否重试。"
                else:
                    reply = f"兑换失败（{error.code}）。"
            except (KeyError, OSError, TypeError, ValueError) as error:
                logger.error("FGO 兑换预检失败：%s", type(error).__name__)
                reply = "兑换失败，无法读取账号或库存数据。"
            await self.ctx.send.text(reply, stream_id)

        if not self._start_account_job(user_id, stream_id, exchange):
            return True, "已有账号操作正在进行，请稍后再试。", True
        return True, "", True

    # === /fgo init — 数据初始化 ===

    @Command(
        name="fgo_init",
        description="初始化FGO抽卡数据目录",
        pattern=r"^/fgo\s+init\s*$",
    )
    async def cmd_data_init(self, stream_id: str = "", **kwargs) -> tuple[bool, str, bool]:
        os.makedirs(self.pm.data_dir, exist_ok=True)
        os.makedirs(self.icon_dir, exist_ok=True)
        return True, "FGO 抽卡数据目录已初始化。请使用 /fgo pools 自动获取并查看卡池数据。", True

    async def _do_fetch_pools(self, silent: bool = False) -> str:
        """刷新完整国服日程和模拟数据，串行发布缓存，避免并发刷新相互覆盖。"""
        async with self._pool_refresh_lock:
            pool_list = await fetch_pool_list()
            # 每次刷新一次卡片索引，后续子池共享它，及时识别新实装卡片。
            names = await asyncio.to_thread(build_card_name_map)
            # 只限制同时下载的数量，不截断未来日程。
            semaphore = asyncio.Semaphore(4)

            async def fetch(pool: dict) -> List[dict]:
                async with semaphore:
                    try:
                        data = await fetch_scheduled_pool_data(pool)
                        if data is None:
                            raise ValueError("卡池返回空数据")
                        children = data["sub_pools"]
                        pool["type"] = "daily pickup" if len(children) > 1 else "normal"
                        pool["sub_pool"] = [
                            {"id": child["s_id"], "sub_title": child["sub_title"]} for child in children
                        ]
                        pool["available"] = True
                        return [{**child, "p_id": pool["id"], "pool_href": pool["href"]} for child in children]
                    except Exception as error:
                        # 保留日程，但明确标记不可抽取，不能让失败的池对应旧概率数据。
                        logger.exception("构建卡池失败: %s", pool["href"])
                        pool["data_error"] = str(error)
                        pool["available"] = False
                        pool["sub_pool"] = []
                        return []

            groups = await asyncio.gather(*(fetch(pool) for pool in pool_list))
            gacha_data = [child for group in groups for child in group]
            if not gacha_data:
                raise ValueError("所有卡池模拟数据下载失败，请检查日志后重新使用 /fgo pools")
            available = [pool for pool in pool_list if pool["available"]]
            default = dict(available[0])
            if default["type"] == "daily pickup":
                default["s_id"] = default["sub_pool"][0]["id"]
            await asyncio.to_thread(self.pm.save_pool_snapshot, pool_list, gacha_data, default)
            await asyncio.to_thread(self.pm.save_card_names, names)
            return f"日程已更新：{len(pool_list)} 个国服卡池，{len(available)} 个可抽取，共 {len(gacha_data)} 个子池。"

    # === /fgo pools — 自动下载并查询卡池 ===

    @Command(
        name="fgo_query_pools",
        description="自动下载并查询国服当前及未来FGO卡池，合并转发显示",
        pattern=r"^/fgo\s+(?:pools|卡池)\s*$",
    )
    async def cmd_query_pools(self, stream_id: str = "", **kwargs) -> tuple[bool, str, bool]:
        await self._ack(stream_id, "正在更新国服当前及未来卡池，请稍候...")
        try:
            summary = await self._do_fetch_pools()
            pools = await asyncio.to_thread(self.pm.load_pools)
            banner = await asyncio.to_thread(
                self.pm.get_stream_banner, stream_id, self.config.gacha.follow_pool_type == "latest"
            )
            messages = format_pool_messages(pools, banner, summary)
        except Exception as error:
            logger.exception("国服卡池日程更新失败")
            return True, f"卡池列表获取失败：{error}。请稍后重新使用 /fgo pools。", True

        if not stream_id:
            return True, "\n\n".join(messages), True
        try:
            # NapCat 原样使用节点 user_id 作为 uin，使用实际登录账号标识节点作者。
            login = await self.ctx.api.call("adapter.napcat.system.get_login_info", version="1")
            if not isinstance(login, dict):
                raise ValueError("NapCat 未返回登录账号信息")
            account_id = str(login["user_id"])
            if not account_id.isascii() or not account_id.isdigit() or int(account_id) <= 0:
                raise ValueError("NapCat 返回的登录 QQ 号无效")
            # 参考色图插件关闭内容入库；分页转发再嵌套到外层，群里只发送一张卡片。
            nodes = build_nested_forward_nodes(messages, account_id)
            logger.info("发送嵌套卡池日程：页数=%d，文本节点数=%d，文本字节数=%d", len(nodes),
                        len(messages), sum(len(text.encode("utf-8")) for text in messages))
            sent = await self.ctx.send.forward(nodes, stream_id, storage_message=False)
            if not sent:
                raise RuntimeError("嵌套合并转发发送失败，详情见 send_service 与 NapCat 日志")
            return True, "", True
        except Exception as error:
            logger.exception("国服卡池日程已更新，但嵌套合并转发发送失败（文本节点总数=%d）", len(messages))
            return True, f"卡池数据已更新，可正常抽取，但列表发送失败：{error}。", True

    # === /fgo switch <id> [sub_id] — 切换卡池或日替子池 ===

    @Command(
        name="fgo_switch_pool",
        description="切换FGO抽卡卡池，可指定子池编号",
        pattern=r"^/fgo\s+(?:switch|切换卡池)\s+(\d+)(?:\s+(\d+))?\s*$",
    )
    async def cmd_switch_pool(self, stream_id: str = "", message: str = "", user_id: str = "", **kwargs) -> tuple[bool, str, bool]:
        msg_text = str(kwargs.get("text", message) if isinstance(message, dict) else message)
        m = re.fullmatch(r"/fgo\s+(?:switch|切换卡池)\s+(\d+)(?:\s+(\d+))?", msg_text.strip())
        if not m:
            return False, "用法: /fgo switch <卡池编号> [子池编号] 或 /fgo 切换卡池 <卡池编号> [子池编号]", True
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, f"{error}，未切换卡池。", True

        pool_id = int(m.group(1))
        sub_id = int(m.group(2)) if m.group(2) is not None else None
        pools = await asyncio.to_thread(self.pm.load_pools)
        if not pools:
            return True, "暂无卡池数据，请先用 /fgo pools 获取数据。", True

        target = None
        for p in pools:
            if p.get("id") == pool_id:
                target = p
                break
        if target is None:
            return True, f"未找到编号为 {pool_id} 的卡池。使用 /fgo pools 查看可用列表。", True

        if not target.get("available", False):
            return True, "该卡池模拟数据暂不可用，请使用 /fgo pools 刷新后再试。", True
        target = dict(target)
        result = f"已切换到卡池: [{pool_id}] {target['title']}"
        if target["type"] == "daily pickup":
            # 省略子池编号时保持原 switch 行为，选择列表中的第一个子池。
            children = target["sub_pool"]
            if not children:
                return True, "该卡池缺少子池数据，请使用 /fgo pools 刷新后再试。", True
            selected_id = children[0]["id"] if sub_id is None else sub_id
            child = next((item for item in children if item["id"] == selected_id), None)
            if child is None:
                return True, f"子池编号 {selected_id} 不存在，请使用 /fgo pools 查看子池列表。", True
            target["s_id"] = selected_id
            result += f"；子池: [{selected_id}] {child['sub_title']}"
        elif sub_id is not None:
            return True, f"卡池 [{pool_id}] 没有可选子池，请使用 /fgo switch {pool_id}。", True

        await asyncio.to_thread(self.pm.select_user_pool, user_id, stream_id, target)
        await self.ctx.send.text(result, stream_id)
        return True, result, True

    # === /fgo 10 — 十连抽卡 ===

    @Command(
        name="fgo_10draw",
        description="FGO模拟十连抽卡",
        pattern=r"^/fgo\s+10\s*$",
    )
    async def cmd_ten_draw(
        self, stream_id: str = "", user_id: str = "", message: Optional[Dict[str, Any]] = None, **kwargs
    ) -> tuple[bool, str, bool]:
        try:
            if message is None:
                raise ValueError("缺少命令消息")
            user_nickname = message["message_info"]["user_info"]["user_nickname"]
            if not isinstance(user_nickname, str) or not user_nickname.strip():
                raise ValueError("发送者昵称为空或格式错误")
        except (KeyError, TypeError, ValueError) as error:
            logger.exception("无法获取十一连抽卡者昵称")
            return True, f"无法获取抽卡者昵称：{error}，未进行抽卡。", True
        return await self._do_gacha(stream_id, user_id, count=11, user_nickname=user_nickname.strip())

    # === /fgo 100 — 百连抽卡 ===

    @Command(
        name="fgo_100draw",
        description="FGO模拟百连抽卡（十次十一连，共110抽）",
        pattern=r"^/fgo\s+100\s*$",
    )
    async def cmd_hundred_draw(self, stream_id: str = "", user_id: str = "", **kwargs) -> tuple[bool, str, bool]:
        return await self._do_gacha(stream_id, user_id, count=110)

    async def _do_gacha(
        self, stream_id: str, user_id: str = "", count: int = 11, user_nickname: str = ""
    ) -> tuple[bool, str, bool]:
        try:
            user_id = normalize_qq_user_id(user_id)
        except ValueError as error:
            return True, f"{error}，未进行抽卡。", True
        banner = await asyncio.to_thread(self.pm.get_stream_banner, stream_id, self.config.gacha.follow_pool_type == "latest")
        if banner is None:
            return True, "暂未设置卡池，请先用 /fgo pools 获取数据。", True

        gacha_data = await asyncio.to_thread(self.pm.get_gacha_data_for_banner, banner)
        if gacha_data is None:
            return True, "卡池数据不完整，请先用 /fgo pools 刷新数据。", True

        server = banner.get("server", "国服")
        name_map = await asyncio.to_thread(self.pm.load_card_names)
        banner_title = banner.get("title", "")

        try:
            # 额度检查、概率抽取与计数读写在同一个线程临界区完成，避免并发超额。
            result, pity_triggered = await asyncio.to_thread(
                self.pm.draw_for_user, user_id, banner, gacha_data, count,
                daily_limit=self.config.gacha.daily_limit, cooldown_seconds=self.config.gacha.cooldown_seconds,
            )
        except DrawLimitError as error:
            msg = str(error)
            if stream_id:
                await self.ctx.send.text(msg, stream_id)
            return True, msg, True
        except (KeyError, OSError, ValueError, TypeError) as error:
            logger.exception("抽卡或保底状态处理失败")
            return True, f"抽卡失败：{error}。请检查日志；卡池数据异常时使用 /fgo pools 刷新。", True
        await self._ack(stream_id, "抽卡中..." if count == 11 else "百连（110 抽）抽卡中，请稍候...")
        if count == 11:
            pickup_count = sum(card["type"] == "svt" and card["rarity"] == "up5" for card in result)
        else:
            pickup_count = sum(card["count"] for card in result if card["type"] == "upsvt_5")
        # 一次请求最多 110 抽，只会触发一次 330 抽保底；扣除该张后才判断正常出货。
        notices: List[str] = []
        if pickup_count > int(pity_triggered):
            notices.append("恭喜你，你出货了~！")
        if pity_triggered:
            notices.append("很遗憾，你保底了~")

        try:
            if count == 11 and self.config.gacha.display_mode == "image" and stream_id:
                try:
                    await ensure_result_cards(result, self.icon_dir)
                    image_path = await asyncio.to_thread(
                        generate_ten_pull_image, result, self.res_dir, self.icon_dir, server, banner_title, name_map,
                        user_nickname=user_nickname,
                    )
                    try:
                        image_b64 = await asyncio.to_thread(
                            lambda: base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
                        )
                        if not await self.ctx.send.image(image_b64, stream_id):
                            raise RuntimeError("结果图片发送失败，详情见 send_service 与 NapCat 日志")
                    finally:
                        await asyncio.to_thread(Path(image_path).unlink, missing_ok=True)
                    return True, "", True
                except (OSError, ValueError, RuntimeError) as error:
                    logger.exception("十一连结果图片生成或发送失败")
                    return True, f"十一连结果图片生成或发送失败：{error}。", True

            formatter = format_ten_pull_result_text if count == 11 else format_hundred_pull_result_text
            text_result = formatter(result, server, name_map, banner_title)
            if stream_id:
                if count == 110:
                    # @ 与结果正文放在同一条混合消息中，由 QQ 适配器编码为真正的 @。
                    segments = [
                        {"type": "at", "data": {"target_user_id": user_id}},
                        {"type": "text", "content": "\n" + text_result},
                    ]
                    if not await self.ctx.send.hybrid(segments, stream_id):
                        logger.error("百连结果发送失败，详情见 send_service 与 NapCat 日志")
                        return True, "百连结果发送失败，请检查 send_service 与 NapCat 日志。", True
                else:
                    await self.ctx.send.text(text_result, stream_id)
            elif notices:
                text_result += "\n" + "\n".join(notices)
            return True, text_result, True
        finally:
            # 抽取已持久化；即使结果图片发送失败，出货与确定召唤也应告知用户。
            if stream_id:
                for notice in notices:
                    if not await self.ctx.send.text(notice, stream_id):
                        raise RuntimeError("抽卡提示发送失败，详情见 send_service 与 NapCat 日志")

    # === /fgo download — 下载资源 ===

    @Command(
        name="fgo_download",
        description="下载FGO从者/礼装带框召唤卡片",
        pattern=r"^/fgo\s+download\s*$",
    )
    async def cmd_download_res(self, stream_id: str = "", **kwargs) -> tuple[bool, str, bool]:
        if self._download_task is not None and not self._download_task.done():
            return True, "图标下载任务正在运行，请等待完成。", True
        await self._ack(stream_id, "正在获取卡片名称及 Mooncell 召唤图片索引...")
        self._download_task = asyncio.create_task(self._do_download_icons(stream_id))
        return True, "图标下载已在后台开始，完成后会在日志中显示结果。", True

    async def _do_download_icons(self, stream_id: str) -> None:
        await asyncio.to_thread(os.makedirs, self.icon_dir, exist_ok=True)

        logger.info("fgogacha: 开始下载 Mooncell 召唤图片及 Atlas 中文名称...")
        await self._ack(stream_id, "正在获取名称和 Mooncell 带框召唤图片...")

        total_success = 0
        total_fail = 0

        try:
            # Atlas 保留中文名称索引，召唤图改为 Mooncell 图鉴中的带框成品缩略图。
            name_map = await asyncio.to_thread(build_card_name_map)
            thumbnail_index = await load_thumbnail_index(self.icon_dir, refresh=True)
            svt_ids = sorted(thumbnail_index["svt"], key=int)

            logger.info(f"fgogacha: 获取到 {len(svt_ids)} 个从者, {len(name_map.get('svt',{}))+len(name_map.get('ce',{}))} 个卡片名称")

            if name_map:
                await asyncio.to_thread(self.pm.save_card_names, name_map)
                logger.info(f"fgogacha: 保存了 {sum(len(cards) for cards in name_map.values())} 个卡片名称映射")

            # 下载从者图标
            await self._ack(stream_id, f"获取到 {len(svt_ids)} 个从者，开始下载图标...")
            success = 0
            fail = 0
            for i, sid in enumerate(svt_ids):
                ok = await download_result_card("svt", sid, self.icon_dir, thumbnail_index)
                if ok:
                    success += 1
                else:
                    fail += 1
                if (i + 1) % 50 == 0:
                    logger.info(f"fgogacha: 从者图标进度 {i + 1}/{len(svt_ids)} (成功 {success} 失败 {fail})")

            logger.info(f"fgogacha: 从者图标下载完成 成功 {success}/{len(svt_ids)}")
            total_success += success
            total_fail += fail

            # 下载礼装图标
            ce_ids = sorted(thumbnail_index["ce"], key=int)
            await self._ack(stream_id, f"开始下载 {len(ce_ids)} 个礼装图标...")
            success = 0
            fail = 0
            for i, cid in enumerate(ce_ids):
                ok = await download_result_card("ce", cid, self.icon_dir, thumbnail_index)
                if ok:
                    success += 1
                else:
                    fail += 1
                if (i + 1) % 100 == 0:
                    logger.info(f"fgogacha: 礼装图标进度 {i + 1}/{len(ce_ids)} (成功 {success} 失败 {fail})")

            logger.info(f"fgogacha: 礼装图标下载完成 成功 {success}/{len(ce_ids)}")
            total_success += success
            total_fail += fail

        except Exception as e:
            logger.exception("fgogacha: 下载任务中断")
            await self._ack(stream_id, f"图标下载中断：{e}。请检查日志后重新执行 /fgo download。")
            return

        total_count = total_success + total_fail
        logger.info(f"fgogacha: 全部下载完成 成功 {total_success}/{total_count}")
        msg = f"图标下载完成。成功 {total_success}/{total_count}，失败 {total_fail}。"
        try:
            await self.ctx.send.text(msg, stream_id)
        except Exception:
            pass

def create_plugin() -> FgoGachaPlugin:
    return FgoGachaPlugin()
