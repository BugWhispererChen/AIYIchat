"""
微信 ClawBot 适配器（多Bot版）。

基于官方 iLink Bot 协议（HTTP/JSON），通过长轮询接收消息。
支持文本、语音、图片消息收发，typing 状态，CDN 媒体加解密。
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
import logging
import random
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional
from urllib.parse import quote

import aiohttp

from app.adapters.base import ChannelAdapter, NormalizedMessage
from app.config import settings
from app.conversation.engine import ConversationEngine
from app.conversation.human_rhythm import pacing_delay_seconds
from app.conversation.message_splitter import limit_messages
from app.conversation.image_task import ImageTask
from app.conversation.sticker_task import (
    IMAGE_TRANSCRIBE_PREFIX,
    STICKER_TRANSCRIBE_PREFIX,
    StickerTask,
)
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.models.bot_config import BotConfigInDB
from app.services.voice_window import voice_window
from app.utils.redaction import brief_json, mask_secret
from app.utils.tasks import spawn

if TYPE_CHECKING:
    from app.services.asr_service import ASRService
    from app.services.imagegen_service import ImageGenService
    from app.services.tts_service import TTSService

logger = logging.getLogger(__name__)

# ── iLink Bot API 端点 ────────────────────────────────
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
QRCODE_URL = f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode"
QRCODE_STATUS_URL = f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status"

# 本项目协议实现版本与观测标识，不能冒用官方插件版本。
ILINK_CHANNEL_VERSION = "1.0.0"
ILINK_BOT_AGENT = "AIYIchat/1.0.0"
ILINK_APP_ID = "bot"
_version_parts = [int(part) for part in ILINK_CHANNEL_VERSION.split(".")]
ILINK_APP_CLIENT_VERSION = str(
    ((_version_parts[0] & 0xFF) << 16)
    | ((_version_parts[1] & 0xFF) << 8)
    | (_version_parts[2] & 0xFF)
)

# CDN 基础 URL
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


def _build_base_info() -> dict[str, str]:
    """构造所有鉴权 Bot POST 共用的协议元数据。"""
    return {
        "channel_version": ILINK_CHANNEL_VERSION,
        "bot_agent": ILINK_BOT_AGENT,
    }


class SessionExpiredError(Exception):
    """iLink session 过期（ret: -14），需重新扫码登录。"""
    pass


class WeChatClient(ChannelAdapter):
    """单个微信Bot客户端（基于官方 iLink 协议）。"""

    def __init__(
        self,
        bot_id: str,
        bot_token: str = "",
        on_token_update: Optional[Callable] = None,
        account_id: str = "",
    ) -> None:
        self.bot_id = bot_id
        self.account_id = account_id
        self._label = "[WX:%s]" % bot_id[:8]

        # 认证
        self._bot_token: str = bot_token
        self._ilink_user_id: Optional[str] = None
        self._token_expire_at: Optional[datetime] = None

        # 消息轮询
        self._get_updates_buf: str = ""
        self._running: bool = False
        self._reconnect_delay: int = 5
        self._poll_task: Optional[asyncio.Task[dict[str, Any]]] = None

        # per-user context_token 缓存（官方协议: 每条入站消息独立 context_token）
        self._context_tokens: dict[str, str] = {}

        # sendtyping
        self._typing_ticket: str = ""

        # 消息去重（message_id 持久化；异常缺失时使用稳定内容指纹）
        self._processed_msg_ids: collections.OrderedDict[str, float] = (
            collections.OrderedDict()
        )
        self._dedup_max_size: int = 500
        self._dedup_lock: asyncio.Lock = asyncio.Lock()

        # 对话引擎
        self.engine = ConversationEngine(bot_id=bot_id)

        # HTTP session
        self._session: Optional[aiohttp.ClientSession] = None

        # 最近一次发送失败的平台原因（供主动消息记录/诊断使用）
        self._last_send_error: str = ""

        # 回调
        self.on_token_update = on_token_update

        # 多媒体服务（延迟初始化）
        self._vision = None
        self._asr: ASRService | None = None
        self._tts: TTSService | None = None
        self._imagegen: ImageGenService | None = None

        # 登录流程临时数据；重定向只影响后续扫码状态请求
        self._qrcode: str = ""
        self._qr_base_url: str = ILINK_BASE_URL
        self._qr_verify_code: str = ""
        # 扫码确认返回的 Bot API 主机；持久化后跨重启保留
        self._api_base_url: str = ILINK_BASE_URL
        # 引用缓存过期清理按小时节流，避免每条入站消息都扫集合
        self._last_quote_purge: float = 0.0

    # ── ChannelAdapter 抽象方法实现 ─────────────────────

    async def parse_incoming(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """将 iLink getupdates 单条消息解析为标准化消息。"""
        msg_id = raw_event.get("message_id", 0)
        from_user_id = raw_event.get("from_user_id", "")
        nickname = raw_event.get("from_nickname", "")
        item_list = raw_event.get("item_list", [])

        # 提取文本
        text = ""
        for item in item_list:
            if item.get("type") == 1:
                text = item.get("text_item", {}).get("text", "")
                break

        if not text:
            return None

        return NormalizedMessage(
            platform="wechat",
            user_id=from_user_id,
            nickname=nickname,
            content=text,
            message_id=str(msg_id),
            bot_id=self.bot_id,
        )

    async def send_message(self, user_id: str, content: str, **kwargs) -> bool:
        """发送文本消息到微信身份，返回平台是否确认成功。"""
        return await self.send_text(user_id, content)

    # ── 请求头 ────────────────────────────────────────

    @staticmethod
    def _get_app_headers() -> dict[str, str]:
        """生成扫码 GET 与 Bot 请求共用的 iLink App 请求头。"""
        return {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
        }

    def _get_headers(self, include_auth: bool = True) -> dict[str, str]:
        """生成 JSON POST 请求头，取码时不携带已有 Bot 鉴权。"""
        random_uin = secrets.randbits(32)
        uin_b64 = base64.b64encode(str(random_uin).encode("ascii")).decode()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin_b64,
            **self._get_app_headers(),
        }
        if include_auth and self._bot_token:
            headers["Authorization"] = "Bearer %s" % self._bot_token
        return headers

    # ── 登录流程 ──────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """确保 HTTP session 已创建。"""
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _bot_url(self, path: str) -> str:
        """拼接扫码确认后返回的 Bot API 主机，重启后仍沿用。"""
        return f"{self._api_base_url}{path}"

    async def get_login_qrcode(self) -> dict:
        """获取登录二维码；已有 token 仅用于识别重复绑定。"""
        session = await self._ensure_session()
        self._qr_base_url = ILINK_BASE_URL
        self._qr_verify_code = ""

        async with session.post(
            QRCODE_URL,
            params={"bot_type": "3"},
            headers=self._get_headers(include_auth=False),
            json={
                "local_token_list": [self._bot_token] if self._bot_token else []
            },
        ) as resp:
            raw_data = await resp.read()
            if resp.status != 200:
                raise RuntimeError("获取二维码失败: HTTP %s" % resp.status)
            try:
                data = json.loads(raw_data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.error("%s 二维码响应解析失败: %s", self._label, e)
                raise RuntimeError("获取二维码失败: 无法解析响应") from e

            logger.info(
                "%s QRCode response: %s",
                self._label,
                brief_json(data, ("ret", "errcode", "msg", "errmsg")),
            )

            ret_code = data.get("ret", data.get("errcode", 0))
            if ret_code != 0:
                error_msg = data.get("msg", data.get("errmsg", "Unknown error"))
                raise RuntimeError(
                    "获取二维码失败: %s (code: %s)" % (error_msg, ret_code)
                )

            if "qrcode" not in data:
                raise RuntimeError("获取二维码失败: 响应中无 qrcode 字段")

            self._qrcode = data["qrcode"]
            qrcode_url = data.get("qrcode_img_content", "")
            if not qrcode_url:
                raise RuntimeError("获取二维码失败: 响应中无qrcode_img_content")

            # 生成二维码图片
            try:
                import io

                import qrcode as qrcode_lib

                qr = qrcode_lib.QRCode(version=1, box_size=10, border=2)
                qr.add_data(qrcode_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                img_b64 = base64.b64encode(buffer.getvalue()).decode()
                qrcode_data_uri = "data:image/png;base64,%s" % img_b64
            except ImportError:
                logger.warning("%s qrcode 库未安装，跳过图片生成", self._label)
                qrcode_data_uri = ""

            return {"qrcode": self._qrcode, "qrcode_url": qrcode_data_uri}

    async def poll_qrcode_status(
        self, qrcode: str, verify_code: str = ""
    ) -> dict[str, str]:
        """长轮询扫码状态，保留平台状态机并处理 IDC 重定向。"""
        session = await self._ensure_session()
        if verify_code:
            self._qr_verify_code = verify_code.strip()
        params = {"qrcode": qrcode}
        if self._qr_verify_code:
            params["verify_code"] = self._qr_verify_code
        try:
            async with session.get(
                f"{self._qr_base_url}/ilink/bot/get_qrcode_status",
                params=params,
                headers=self._get_app_headers(),
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError("扫码状态请求失败: HTTP %s" % resp.status)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    raise RuntimeError("扫码状态响应无法解析") from e

                # 成功响应可能带凭据，只记录状态与错误码，不记录完整响应。
                logger.info(
                    "%s 扫码状态: status=%s, ret=%s",
                    self._label, data.get("status", ""), data.get("ret", 0),
                )
                ret_code = data.get("ret", data.get("errcode", 0))
                if ret_code != 0:
                    if ret_code == -14:
                        return {"status": "wait"}
                    raise RuntimeError("扫码状态请求失败: ret=%s" % ret_code)

                status = str(data.get("status") or "wait")
                if status == "scaned_but_redirect":
                    redirect_host = str(data.get("redirect_host", "")).strip()
                    if redirect_host:
                        self._qr_base_url = (
                            redirect_host.rstrip("/")
                            if "://" in redirect_host
                            else "https://%s" % redirect_host.rstrip("/")
                        )
                elif status != "wait":
                    # wait/redirect 期间继续携带验证码；其余状态结束本次提交。
                    self._qr_verify_code = ""

                if status == "confirmed":
                    bot_token = str(data.get("bot_token", ""))
                    if not bot_token:
                        raise RuntimeError("扫码确认响应缺少 bot_token")
                    self._ilink_user_id = data.get("ilink_user_id")
                    self._bot_token = bot_token
                    baseurl = str(data.get("baseurl", "")).strip()
                    if baseurl:
                        self._api_base_url = baseurl.rstrip("/")
                        try:
                            await self._persist_api_base_url()
                        except Exception as exc:
                            # 持久化失败不否定平台已确认的登录，重启后回退默认主机
                            logger.warning(
                                "%s Bot API 主机持久化失败: %s",
                                self._label, exc,
                            )
                    logger.info("%s 扫码确认成功", self._label)
                return {"status": status, **{
                    key: str(data[key])
                    for key in ("bot_token", "ilink_user_id", "baseurl")
                    if data.get(key)
                }}
        except asyncio.TimeoutError:
            logger.debug("%s 扫码长轮询超时，将重试", self._label)
            return {"status": "wait"}

    def set_bot_token(self, bot_token: str) -> None:
        """设置 bot_token（供 admin 路由调用）。"""
        self._bot_token = bot_token

    async def complete_login(self, bot_token: str) -> dict:
        """登录完成：使用 bot_token，调用 getconfig 获取 typing_ticket。

        注意: getconfig 仅返回 typing_ticket，context_token 从入站消息获取。
        """
        session = await self._ensure_session()
        self._bot_token = bot_token

        # 调用 getconfig 获取 typing_ticket
        try:
            async with session.post(
                self._bot_url("/ilink/bot/getconfig"),
                headers=self._get_headers(),
                json={"base_info": _build_base_info()},
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("%s getconfig 响应解析失败", self._label)
                    data = {}

                logger.info("%s getconfig keys: %s", self._label, list(data.keys()))
                self._typing_ticket = data.get("typing_ticket", "")
                if self._typing_ticket:
                    logger.info("%s typing_ticket 已获取", self._label)
        except Exception as e:
            logger.warning("%s getconfig 调用失败（非阻塞）: %s", self._label, e)

        # 计算过期时间（bot_token 有效期 24 小时）
        self._token_expire_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # 回调保存 token
        if self.on_token_update:
            await self.on_token_update(self.bot_id, "", self._token_expire_at)

        logger.info("%s 登录完成", self._label)
        return {"bot_token": bot_token, "typing_ticket": self._typing_ticket}

    # ── 消息轮询主循环 ────────────────────────────────

    async def start(self) -> None:
        """启动消息轮询（含指数退避重连与持久状态恢复）。"""
        if not self._bot_token:
            logger.warning("%s 无 bot_token，无法启动", self._label)
            return

        self._running = True
        self._reconnect_delay = 5
        max_reconnect_delay = 120
        await self._ensure_session()

        # 先恢复游标和去重记录，再开始收消息，避免重启后重复进入引擎。
        await self.load_sync_state()
        await self._notify_lifecycle("start")

        # 获取 typing_ticket（非阻塞）
        await self._refresh_typing_ticket()

        # 从 MongoDB 恢复已缓存的 context_token（支持主动消息）
        await self.load_context_tokens()

        logger.info("%s 微信Bot启动", self._label)

        while self._running:
            try:
                await self._polling_loop()
                self._reconnect_delay = 5  # 成功轮询后重置

            except SessionExpiredError:
                logger.warning("%s Session 过期，需要重新扫码", self._label)
                self._running = False
                await self._notify_lifecycle("stop")
                await self._mark_offline()
                raise

            except asyncio.CancelledError:
                self._running = False
                raise

            except Exception as e:
                logger.error(
                    "%s 轮询错误: %s", self._label, e, exc_info=True
                )

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, max_reconnect_delay
                )

    async def stop(self) -> None:
        """停止消息轮询，显式取消请求并通知平台离线。"""
        self._running = False
        poll_task = self._poll_task
        if poll_task is not None and not poll_task.done():
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        self._poll_task = None
        await self._notify_lifecycle("stop")
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("%s 微信Bot停止", self._label)

    async def _notify_lifecycle(self, action: Literal["start", "stop"]) -> None:
        """发送生命周期通知；HTTP、解析或业务失败均只告警。"""
        if not self._bot_token:
            return
        url = self._bot_url(f"/ilink/bot/msg/notify{action}")
        try:
            session = await self._ensure_session()
            async with session.post(
                url,
                headers=self._get_headers(),
                json={"base_info": _build_base_info()},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(
                        "%s notify%s HTTP %s", self._label, action, resp.status
                    )
                    return
                data = json.loads(text) if text.strip() else {}
                ret = data.get("ret", data.get("errcode", 0))
                if ret != 0:
                    logger.warning("%s notify%s ret=%s", self._label, action, ret)
        except Exception as e:
            logger.warning("%s notify%s 失败: %s", self._label, action, e)

    async def _mark_offline(self) -> None:
        """标记 Bot 为离线状态。"""
        try:
            repo = BotConfigRepository()
            await repo.update_status(self.bot_id, "offline")
            logger.info("%s 已标记为离线", self._label)
        except Exception as e:
            logger.error("%s 标记离线失败: %s", self._label, e)

    async def _polling_loop(self) -> None:
        """执行一次长轮询；全部消息处理并持久去重后才推进游标。"""
        session = await self._ensure_session()
        payload: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": _build_base_info(),
        }
        self._poll_task = asyncio.create_task(
            self._fetch_updates(session, payload)
        )
        try:
            data = await self._poll_task
        except asyncio.TimeoutError:
            logger.debug("%s getupdates 长轮询超时，继续下一轮", self._label)
            return
        except SessionExpiredError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "%s getupdates 请求异常: %s", self._label, e, exc_info=True
            )
            return
        finally:
            self._poll_task = None

        try:
            messages = data.get("msgs", [])
            if messages:
                logger.info(
                    "%s getupdates 返回 %d 条消息",
                    self._label, len(messages),
                )
            for msg in messages:
                await self._process_message_once(msg)

            # 空游标不覆盖旧值；任何处理/落库异常都不会执行到这里。
            new_cursor = str(data.get("get_updates_buf", ""))
            if new_cursor:
                await self._persist_get_updates_buf(new_cursor)
            if not messages:
                await asyncio.sleep(1)
        except Exception as e:
            logger.error("%s 处理消息或推进游标失败: %s", self._label, e)

    async def _fetch_updates(
        self, session: aiohttp.ClientSession, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """发送协议约定的 getupdates 请求并校验 HTTP 与业务返回码。"""
        async with session.post(
            self._bot_url("/ilink/bot/getupdates"),
            headers=self._get_headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=50),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError("getupdates HTTP %s" % resp.status)
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError("getupdates 响应无法解析") from e
            ret = data.get("ret", data.get("errcode", 0))
            if ret != 0:
                if ret == -14:
                    raise SessionExpiredError("session timeout (ret: -14)")
                raise RuntimeError("getupdates ret=%s" % ret)
            return data

    # ── 持久游标与消息去重 ─────────────────────────────

    async def load_sync_state(self) -> None:
        """按 Bot 恢复游标和最近消息回执；读取失败时禁止直接轮询。"""
        from app.db.mongo import get_database

        doc = await get_database().wechat_sync_state.find_one(
            {"bot_id": self.bot_id}
        )
        if not doc:
            return
        self._get_updates_buf = str(doc.get("get_updates_buf", ""))
        self._api_base_url = str(doc.get("api_base_url") or ILINK_BASE_URL)
        self._processed_msg_ids.clear()
        for msg_key in doc.get("processed_msg_ids", [])[-self._dedup_max_size:]:
            self._processed_msg_ids[str(msg_key)] = time.time()

    async def _persist_api_base_url(self) -> None:
        """持久化扫码返回的 Bot API 主机，供重启后继续使用。"""
        from app.db.mongo import get_database

        await get_database().wechat_sync_state.update_one(
            {"bot_id": self.bot_id},
            {
                "$set": {
                    "api_base_url": self._api_base_url,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {"processed_msg_ids": []},
            },
            upsert=True,
        )

    async def _persist_get_updates_buf(self, new_cursor: str) -> None:
        """持久化非空新游标成功后才更新内存状态。"""
        from app.db.mongo import get_database

        await get_database().wechat_sync_state.update_one(
            {"bot_id": self.bot_id},
            {
                "$set": {
                    "get_updates_buf": new_cursor,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {"processed_msg_ids": []},
            },
            upsert=True,
        )
        self._get_updates_buf = new_cursor

    async def reset_sync_state(self) -> None:
        """重新登录新会话时清除旧游标与回执。"""
        from app.db.mongo import get_database

        await get_database().wechat_sync_state.delete_one({"bot_id": self.bot_id})
        self._get_updates_buf = ""
        self._api_base_url = ILINK_BASE_URL
        self._processed_msg_ids.clear()

    @staticmethod
    def _message_dedup_key(msg: dict[str, Any]) -> str:
        """优先使用平台 message_id，缺失时生成跨重启稳定的内容指纹。"""
        msg_id = msg.get("message_id")
        if msg_id not in (None, "", 0):
            return str(msg_id)
        canonical = json.dumps(msg, ensure_ascii=False, sort_keys=True, default=str)
        return "sha256:%s" % hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _is_msg_duplicate(self, msg_key: str) -> bool:
        """只查询回执，不在处理完成前写入去重状态。"""
        async with self._dedup_lock:
            return msg_key in self._processed_msg_ids

    async def _mark_msg_processed(self, msg_key: str) -> None:
        """持久化消息占位回执，再更新有界内存缓存。"""
        from app.db.mongo import get_database

        async with self._dedup_lock:
            await get_database().wechat_sync_state.update_one(
                {"bot_id": self.bot_id},
                {
                    "$push": {
                        "processed_msg_ids": {
                            "$each": [msg_key],
                            "$slice": -self._dedup_max_size,
                        }
                    },
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
                upsert=True,
            )
            self._processed_msg_ids[msg_key] = time.time()
            self._processed_msg_ids.move_to_end(msg_key)
            while len(self._processed_msg_ids) > self._dedup_max_size:
                self._processed_msg_ids.popitem(last=False)

    async def _process_message_once(self, msg: dict[str, Any]) -> None:
        """先持久占位再处理，崩溃重拉时宁可少投一次也不重复进入引擎。"""
        msg_key = self._message_dedup_key(msg)
        if await self._is_msg_duplicate(msg_key):
            logger.debug("%s 跳过已处理消息: %s", self._label, msg_key)
            return
        await self._mark_msg_processed(msg_key)
        try:
            await self._handle_message(msg)
        except Exception:
            # 明确失败可安全回滚；进程崩溃来不及回滚时仍由占位记录阻止重复。
            await self._unmark_msg_processed(msg_key)
            raise

    async def _unmark_msg_processed(self, msg_key: str) -> None:
        """处理失败时撤销持久占位，让下一轮能重试该消息。"""
        from app.db.mongo import get_database

        async with self._dedup_lock:
            await get_database().wechat_sync_state.update_one(
                {"bot_id": self.bot_id},
                {
                    "$pull": {"processed_msg_ids": msg_key},
                    "$set": {"updated_at": datetime.now(timezone.utc)},
                },
            )
            self._processed_msg_ids.pop(msg_key, None)

    # ── 引用缓存 ──────────────────────────────────────

    @staticmethod
    def _quote_session_key(msg: dict[str, Any]) -> str:
        """引用隔离键：优先 session_id，其次取会话对端用户。"""
        return str(
            msg.get("session_id")
            or msg.get("from_user_id")
            or msg.get("to_user_id")
            or ""
        )

    @staticmethod
    def _quote_svr_ids(msg: dict[str, Any]) -> list[str]:
        """收集服务端可能用于引用的稳定消息 ID。"""
        candidates: list[str] = [
            str(value)
            for value in (
                msg.get("svr_id"),
                msg.get("message_id"),
                msg.get("client_id"),
            )
            if value not in (None, "", 0, "0")
        ]
        for item in msg.get("item_list", []) or []:
            if not isinstance(item, dict):
                continue
            for key in ("svr_id", "msg_id"):
                value = item.get(key)
                if value not in (None, "", 0, "0"):
                    candidates.append(str(value))
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _item_type(item: dict[str, Any]) -> int | None:
        """兼容 JSON 数字与字符串形式的 item.type。"""
        value = item.get("type")
        if isinstance(value, bool):
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _item_text(cls, item: dict[str, Any]) -> str:
        """从单个 MessageItem 取可读文本。"""
        item_type = cls._item_type(item)
        if item_type == 1:
            return str(item.get("text_item", {}).get("text", ""))
        if item_type == 3:
            return str(item.get("voice_item", {}).get("text", ""))
        if item_type == 4:
            return str(item.get("file_item", {}).get("file_name", ""))
        return ""

    @classmethod
    def _item_media_kind(cls, item: dict[str, Any]) -> str:
        """返回媒体类型名，供引用提示与缓存检索。"""
        item_type = cls._item_type(item)
        if item_type is None:
            return ""
        return {
            2: "image",
            3: "voice",
            4: "file",
            5: "video",
        }.get(item_type, "")

    async def _cache_quote_message(
        self,
        msg: dict[str, Any],
        *,
        text: str,
        media_kind: str = "",
        media_url: str = "",
        role: str = "user",
    ) -> None:
        """按 Bot/会话/svr_id 持久化引用语料，失败不影响消息主流程。"""
        if not settings.wechat_quote_cache_enabled:
            return
        session_key = self._quote_session_key(msg)
        ids = self._quote_svr_ids(msg)
        if not session_key or not ids:
            return
        try:
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            repo = WeChatQuoteCacheRepository()
            now = time.monotonic()
            if now - self._last_quote_purge >= 3600:
                self._last_quote_purge = now
                spawn(repo.purge_expired())
            for svr_id in ids:
                await repo.upsert(
                    bot_id=self.bot_id,
                    account_id=self.account_id,
                    session_key=session_key,
                    svr_id=svr_id,
                    message_id=str(msg.get("message_id", "")),
                    role=role,
                    text=text,
                    media_kind=media_kind,
                    media_url=media_url,
                )
        except Exception as exc:
            logger.warning("%s 引用缓存写入失败: %s", self._label, exc)

    async def _cache_message_without_reply(self, msg: dict[str, Any]) -> None:
        """缓存 Bot 回显消息，供其被引用时还原。"""
        texts = [
            self._item_text(item)
            for item in msg.get("item_list", []) or []
            if isinstance(item, dict)
        ]
        media_items = [
            item for item in msg.get("item_list", []) or []
            if isinstance(item, dict) and self._item_media_kind(item)
        ]
        await self._cache_quote_message(
            msg,
            text="\n".join(part for part in texts if part),
            media_kind=(
                self._item_media_kind(media_items[-1]) if media_items else ""
            ),
            role="assistant",
        )

    @staticmethod
    def _nth_index(
        text: str, value: str, occurrence: int, start: int = 0
    ) -> int:
        """按官方语义查找第 occurrence 次出现的子串。"""
        if not text or not value or occurrence < 0:
            return -1
        position = start
        for current in range(occurrence + 1):
            position = text.find(value, position)
            if position < 0:
                return -1
            if current < occurrence:
                position += len(value)
        return position

    @classmethod
    def _partial_quote_candidate(
        cls,
        full_text: str,
        partial: dict[str, Any],
        end_search_mode: str,
    ) -> str:
        """按全局/相对索引两种官方口径截取局部引用。"""
        start_text = str(partial.get("start", ""))
        end_text = str(partial.get("end", ""))
        if not start_text or not end_text:
            return ""
        try:
            start_index = int(partial.get("startindex", 0))
            end_index = int(partial.get("endindex", 0))
        except (TypeError, ValueError):
            return ""
        start = cls._nth_index(full_text, start_text, start_index)
        if start < 0:
            return ""
        end_from = start + len(start_text) if end_search_mode == "relative" else 0
        end = cls._nth_index(full_text, end_text, end_index, end_from)
        if end < start:
            return ""
        return full_text[start:end + len(end_text)]

    @classmethod
    def _resolve_partial_quote(
        cls, full_text: str, partial: dict[str, Any]
    ) -> str:
        """还原局部引用并用 quotemd5 消歧，校验失败不伪造内容。"""
        candidates: list[str] = []
        for mode in ("global", "relative"):
            value = cls._partial_quote_candidate(full_text, partial, mode)
            if value and value not in candidates:
                candidates.append(value)
        if not candidates:
            return ""
        expected = str(partial.get("quotemd5", "")).strip().lower()
        if not expected:
            return candidates[0]
        for value in candidates:
            if hashlib.md5(value.encode("utf-8")).hexdigest() == expected:
                return value
        return ""

    async def _resolve_quote_context(
        self, item: dict[str, Any], msg: dict[str, Any]
    ) -> str:
        """还原整条/局部引用与媒体提示，缓存未命中时给出明确占位。"""
        ref = item.get("ref_msg")
        if not isinstance(ref, dict):
            return ""
        ref_items: list[dict[str, Any]] = []
        message_item = ref.get("message_item")
        if isinstance(message_item, dict):
            ref_items.append(message_item)
        direct_text = str(ref.get("title", "")).strip()
        if message_item:
            direct_text = self._item_text(message_item).strip() or direct_text
        ref_media = self._item_media_kind(message_item) if message_item else ""
        if not ref_media:
            for item_type, key in (
                ("image", "image_item"),
                ("voice", "voice_item"),
                ("file", "file_item"),
                ("video", "video_item"),
            ):
                if isinstance(ref.get(key), dict):
                    ref_media = item_type
                    break
        svr_id = ""
        for key in ("svr_id", "message_id", "msg_id"):
            value = ref.get(key)
            if value not in (None, "", 0, "0"):
                svr_id = str(value)
                break
        if not svr_id and isinstance(message_item, dict):
            # 官方客户端在无 svr_id 时回退到被引用消息项的 msg_id。
            nested_msg_id = message_item.get("msg_id")
            if nested_msg_id not in (None, "", 0, "0"):
                svr_id = str(nested_msg_id)
        raw_partial = ref.get("partial_text")
        partial: dict[str, Any] = (
            raw_partial if isinstance(raw_partial, dict) else {}
        )
        has_partial = bool(partial)
        partial_invalid = False
        cached_text = ""
        cached_media = ""
        selected_text = ""
        if svr_id and settings.wechat_quote_cache_enabled:
            try:
                from app.db.repositories.wechat_quote_repo import (
                    WeChatQuoteCacheRepository,
                )

                cached = await WeChatQuoteCacheRepository().find(
                    bot_id=self.bot_id,
                    session_key=self._quote_session_key(msg),
                    svr_id=svr_id,
                )
                if cached:
                    cached_text = str(cached.get("text", ""))
                    cached_media = str(cached.get("media_kind", ""))
                    if has_partial and cached_text:
                        selected_text = self._resolve_partial_quote(
                            cached_text, partial
                        )
                        partial_invalid = not selected_text
            except Exception as exc:
                logger.warning("%s 引用缓存读取失败: %s", self._label, exc)
        if has_partial:
            # 局部引用不得退化为整条正文，避免把用户未选中的内容注入上下文。
            content = selected_text or direct_text
        else:
            content = direct_text or cached_text
        media_kind = ref_media or cached_media
        parts: list[str] = []
        if content:
            parts.append(f"【引用消息】{content[:2000]}")
        elif media_kind:
            parts.append(f"【引用{media_kind}】")
        elif has_partial and partial_invalid and cached_text:
            parts.append("【引用消息：局部引用校验失败】")
        elif svr_id:
            parts.append("【引用消息：本地缓存未命中】")
        if media_kind and content:
            parts.append(f"（引用中包含{media_kind}媒体）")
        return "\n".join(parts)

    # ── 消息处理 ──────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """处理单条消息（支持文本、图片、语音、文件、视频和引用）。"""
        # 1. 缓存 per-user context_token 并持久化到 MongoDB
        msg_id = msg.get("message_id", 0)
        from_user_id = msg.get("from_user_id", "")
        msg_ctx_token = msg.get("context_token", "")
        if msg_ctx_token and from_user_id:
            self._context_tokens[from_user_id] = msg_ctx_token
            spawn(
                self._persist_context_token(from_user_id, msg_ctx_token)
            )

        # Bot 回显只进引用缓存，绝不能再次进入对话引擎形成自问自答。
        if msg.get("message_type") == 2:
            await self._cache_message_without_reply(msg)
            return

        item_shapes = [
            {
                "type": item.get("type"),
                "keys": sorted(item.keys()),
                "ref_type": type(item.get("ref_msg")).__name__,
            }
            for item in msg.get("item_list", []) or []
            if isinstance(item, dict)
        ]
        ref_shapes: list[dict[str, Any]] = []
        for item in msg.get("item_list", []) or []:
            ref = item.get("ref_msg") if isinstance(item, dict) else None
            if not isinstance(ref, dict):
                continue
            message_item = ref.get("message_item")
            ref_shapes.append({
                "keys": sorted(ref.keys()),
                "svr_id": bool(ref.get("svr_id")),
                "title_len": len(str(ref.get("title", ""))),
                "has_partial": isinstance(ref.get("partial_text"), dict),
                "message_item": {
                    "type": message_item.get("type"),
                    "keys": sorted(message_item.keys()),
                } if isinstance(message_item, dict) else None,
            })
        has_ref = bool(ref_shapes)
        logger.info(
            "%s 收到消息: msg_id=%s, from=%s, has_ctx=%s, has_ref=%s, items=%s, refs=%s",
            self._label, msg_id, from_user_id, bool(msg_ctx_token),
            has_ref, item_shapes, ref_shapes,
        )

        # 3. 发送打字状态
        spawn(self._send_typing(from_user_id, status=1))

        # 4. 解析 item_list（引用先还原，媒体再下载与分析）
        item_list = msg.get("item_list", [])
        text = ""
        media_url: Optional[str] = None
        content_type = "text"
        quote_context = ""
        for candidate in item_list:
            if isinstance(candidate, dict) and candidate.get("ref_msg"):
                quote_context = await self._resolve_quote_context(
                    candidate, msg
                )
                if quote_context:
                    break

        for item in item_list:
            item_type = item.get("type")

            if item_type == 1:  # TEXT
                text = item.get("text_item", {}).get("text", "")

            elif item_type == 2:  # IMAGE
                content_type = "image"
                text = f"{IMAGE_TRANSCRIBE_PREFIX}]"
                image_item = item.get("image_item", {})
                decrypted = await self._download_cdn_media(image_item)
                if decrypted:
                    media_url = await self._save_image(decrypted)
                    if settings.vision_enabled:
                        desc = await self._handle_image_input(decrypted)
                        if desc:
                            is_likely_sticker = self._is_likely_sticker(decrypted)
                            # 识别为表情包时转写为表情包格式（触发以图回图）
                            if desc.startswith("[表情包]") or is_likely_sticker:
                                clean_desc = desc.replace("[表情包]", "").replace("含义：", "").replace("含义:", "").strip()
                                text = f"{STICKER_TRANSCRIBE_PREFIX}，含义：{clean_desc}]"
                            else:
                                text = f"{IMAGE_TRANSCRIBE_PREFIX}，内容：{desc}]"

            elif item_type == 3:  # VOICE
                content_type = "voice"
                voice_item = item.get("voice_item", {})
                # 优先使用官方平台 ASR 结果
                platform_asr = voice_item.get("text", "")
                if platform_asr and platform_asr.strip():
                    logger.info(
                        "%s 平台ASR: %s", self._label, platform_asr[:60]
                    )
                    text = platform_asr.strip()
                elif settings.asr_enabled:
                    # 降级: 下载语音 -> silk_to_wav -> ASR
                    decrypted = await self._download_cdn_media(voice_item)
                    if decrypted:
                        text = await self._handle_voice_input(decrypted)
                if not text:
                    text = "[身份发送了一条语音消息]"

            elif item_type == 4:  # FILE
                content_type = "file"
                file_item = item.get("file_item", {})
                file_name = str(file_item.get("file_name", "文件"))
                decrypted = await self._download_cdn_media(file_item)
                if decrypted:
                    try:
                        from app.services.file_text import extract_text_from_file
                        from app.services.media_storage import (
                            save_file_from_bytes,
                        )

                        media_url = await save_file_from_bytes(
                            decrypted,
                            filename=file_name,
                            content_type="application/octet-stream",
                        )
                        extracted = await asyncio.to_thread(
                            extract_text_from_file, decrypted, file_name
                        )
                        if extracted:
                            text = (
                                f"[身份发送了文件：{file_name}]\n"
                                f"{extracted}"
                            )
                        else:
                            text = (
                                f"[身份发送了文件：{file_name}，"
                                "文件已保存，暂不支持解析正文]"
                            )
                    except Exception as exc:
                        logger.warning(
                            "%s 入站文件处理失败: %s", self._label, exc
                        )
                if not text:
                    text = f"[身份发送了文件：{file_name}，下载失败]"

            elif item_type == 5:  # VIDEO
                content_type = "video"
                video_item = item.get("video_item", {})
                decrypted = await self._download_cdn_media(video_item)
                if decrypted:
                    try:
                        from app.services.media_storage import (
                            save_file_from_bytes,
                        )

                        media_url = await save_file_from_bytes(
                            decrypted,
                            filename="video.mp4",
                            content_type="video/mp4",
                        )
                        text = "[身份发送了一个视频，视频已保存]"
                    except Exception as exc:
                        logger.warning(
                            "%s 入站视频保存失败: %s", self._label, exc
                        )
                if not text:
                    text = "[身份发送了一个视频，下载失败]"

        if not text:
            logger.debug("%s 消息无可处理文本", self._label)
            # 取消打字状态
            spawn(self._send_typing(from_user_id, status=2))
            return

        # 引用只在进入引擎前拼接；缓存保存原始正文，避免重复嵌套引用。
        cache_text = text
        if quote_context:
            text = f"{quote_context}\n{text}"
        await self._cache_quote_message(
            msg,
            text=cache_text,
            media_kind=content_type if content_type in ("image", "voice", "file", "video") else "",
            media_url=media_url or "",
        )

        # 5. 调用对话引擎（result 预置 None，确保异常路径下变量已定义）
        result = None
        try:
            result = await self.engine.handle_message(
                platform="wechat",
                platform_id=from_user_id,
                text=text,
                nickname=msg.get("from_nickname", ""),
                media_url=media_url,
                content_type=content_type,
            )
            reply_text = result.text
            img_tasks = result.image_tasks
            conv_id = result.conversation_id
        except Exception as e:
            logger.error(
                "%s 对话引擎异常: %s", self._label, e, exc_info=True
            )
            reply_text = "网络有点开小差了，你可以再发一次吗？"
            img_tasks = []
            conv_id = ""

        # 6. 回复分发（result 非 None 才允许语音回复，引擎异常时降级为文字）
        # 语音触发：对方发语音直接触发；或动态窗口内（对方发过语音后的随机
        # 固定窗口期）文字消息也以语音回复，窗口按会话独立且不续期
        user_sent_voice = (content_type == "voice")
        in_voice_window = (
            settings.voice_dynamic_enabled
            and bool(conv_id)
            and voice_window.is_active(conv_id)
        )
        is_voice_reply = (
            settings.tts_enabled
            and reply_text
            and result is not None
            and (user_sent_voice or in_voice_window)
        )
        if is_voice_reply and user_sent_voice:
            # 仅对方发语音触发时开启窗口（固定窗口，窗口内不续期）
            voice_window.touch(
                conv_id,
                settings.voice_window_min_minutes,
                settings.voice_window_max_minutes,
            )

        if is_voice_reply and result is not None:
            # 语音回复（内部含额度检查 + 文字兜底）；多消息回复时逐条合成，
            # 情感标签逐条生效，语音节奏与文字拆分一致，更趋近真人
            voice_result = result
            voice_texts = voice_result.texts or [reply_text]
            for seg_text in voice_texts:
                spawn(
                    self._fire_and_forget_tts(
                        from_user_id,
                        seg_text,
                        voice_result.voice_id,
                        voice_result.voice_instruction,
                        voice_result.voice_pitch_rate,
                        voice_result.voice_emotion_tag,
                        voice_result.account_id,
                    )
                )
        elif reply_text:
            # 文字回复（含 TTS 未启用时的语音降级）
            # 拟人化多消息回复：优先使用引擎拆分后的消息列表，逐条发送
            if (
                settings.multi_message_enabled
                and result is not None
                and result.texts
            ):
                texts = limit_messages(
                    [t for t in result.texts if t.strip()],
                    settings.multi_message_max_count,
                )
            else:
                texts = [reply_text]
            for i, text in enumerate(texts):
                if i > 0:
                    # 逐条间隔统一走节奏层：默认配置下与旧的
                    # min(len*per_char, max_delay) 逐字等价，开启 human 档才叠启动/昼夜
                    await asyncio.sleep(pacing_delay_seconds(text, settings))
                await self.send_text(from_user_id, text)

        if img_tasks and settings.imagegen_enabled:
            for task in img_tasks:
                spawn(
                    self._fire_and_forget_imagegen(from_user_id, task, conv_id)
                )

        # 异步发送表情包（fire-and-forget，延迟于文本后模拟真人节奏）
        sticker_tasks = getattr(result, "sticker_tasks", None) if result else None
        if sticker_tasks:
            for stask in sticker_tasks:
                spawn(
                    self._fire_and_forget_sticker(from_user_id, stask, conv_id)
                )

        # 7. 取消打字状态
        spawn(self._send_typing(from_user_id, status=2))

    # ── CDN 媒体下载（官方协议） ──────────────────────

    async def _download_cdn_media(self, media_item: dict) -> Optional[bytes]:
        """从 CDN 下载并解密媒体文件（使用官方 CDNMedia 协议）。"""
        # 提取 CDNMedia 结构
        cdn_media = media_item.get("media", {})
        encrypt_query_param = cdn_media.get("encrypt_query_param", "")
        aes_key_b64 = (
            cdn_media.get("aes_key", "") or media_item.get("aeskey", "")
        )

        full_url = cdn_media.get("full_url", "").strip()
        if (not full_url and not encrypt_query_param) or not aes_key_b64:
            logger.warning("%s CDN 媒体缺少必要字段，跳过下载", self._label)
            return None

        # 完整下载地址优先；旧参数必须作为查询值完整编码。
        download_url = full_url or "%s/download?encrypted_query_param=%s" % (
            CDN_BASE_URL, quote(encrypt_query_param, safe="")
        )

        # 解析 per-item AES 密钥
        aes_key = self._parse_aes_key(aes_key_b64)

        try:
            session = await self._ensure_session()
            async with session.get(download_url) as resp:
                if resp.status != 200:
                    logger.error(
                        "%s CDN 下载失败: status=%s", self._label, resp.status
                    )
                    return None
                encrypted_data = await resp.read()

            decrypted = self._aes_decrypt(encrypted_data, aes_key)
            logger.info(
                "%s CDN 下载解密完成: %d bytes", self._label, len(decrypted)
            )
            return decrypted
        except Exception as e:
            logger.error("%s CDN 下载/解密失败: %s", self._label, e)
            return None

    # ── CDN 媒体上传（官方协议） ──────────────────────

    # iLink getuploadurl 的 media_type 整数映射（对齐 wxchat-sdk 官方定义）
    _MEDIA_TYPE_MAP: dict[str, int] = {"image": 1, "video": 2, "file": 3, "voice": 4}

    async def _upload_cdn_media(
        self, data: bytes, to_user_id: str, media_type: str
    ) -> Optional[dict]:
        """按官方协议上传媒体到 CDN，返回 CDNMedia 引用。"""
        # 1. 生成随机 AES key
        aes_key = secrets.token_bytes(16)
        aes_key_hex = aes_key.hex()

        # 2. 加密
        encrypted = self._aes_encrypt(data, aes_key)

        # 3. 计算哈希和文件标识
        raw_md5 = hashlib.md5(data).hexdigest()
        filekey = "ec_%s" % uuid.uuid4().hex[:16]

        # 4. 获取上传参数（对齐 wxchat-sdk / cc-connect 开源实现）
        media_type_int = self._MEDIA_TYPE_MAP.get(media_type, 1)
        upload_info = await self._get_upload_url(
            filekey=filekey,
            media_type=media_type_int,
            to_user_id=to_user_id,
            rawsize=len(data),
            rawfilemd5=raw_md5,
            filesize=len(encrypted),
            no_need_thumb=True,
            aeskey=aes_key_hex,
        )
        if not upload_info:
            return None

        # 5. 完整上传地址优先，旧 upload_param 回退并正确编码查询值。
        upload_full_url = upload_info.get("upload_full_url", "").strip()
        upload_param = upload_info.get("upload_param", "")
        if upload_full_url:
            cdn_upload_url = upload_full_url
        elif upload_param:
            cdn_upload_url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
                CDN_BASE_URL,
                quote(upload_param, safe=""),
                quote(filekey, safe=""),
            )
        else:
            logger.error("%s getuploadurl 未返回可用上传地址", self._label)
            return None

        # 6. 上传加密数据到 CDN
        try:
            session = await self._ensure_session()
            async with session.post(
                cdn_upload_url,
                data=encrypted,
                headers={"Content-Type": "application/octet-stream"},
            ) as resp:
                if resp.status != 200:
                    logger.error(
                        "%s CDN 上传失败: status=%s", self._label, resp.status
                    )
                    return None
                encrypt_query_param = resp.headers.get(
                    "x-encrypted-param", ""
                )
        except Exception as e:
            logger.error("%s CDN 上传异常: %s", self._label, e)
            return None

        if not encrypt_query_param:
            logger.error(
                "%s CDN 上传响应中无 x-encrypted-param", self._label
            )
            return None

        # 7. 构造 CDNMedia 引用（aes_key 格式: base64(hex_string)）
        return {
            "cdn_media": {
                "encrypt_query_param": encrypt_query_param,
                "aes_key": base64.b64encode(aes_key_hex.encode()).decode(),
                "encrypt_type": 1,
            },
            "raw_size": len(data),
            "cipher_size": len(encrypted),
        }

    async def _get_upload_url(self, **kwargs) -> Optional[dict]:
        """调用 getuploadurl 获取 CDN 上传参数。"""
        try:
            session = await self._ensure_session()
            # 请求体一律只记字段名（与本方法内响应日志同口径）：静态护栏禁止把
            # json.dumps 的结果打进日志，见 tools/test/test_no_credential_logging.py
            logger.debug(
                "%s getuploadurl 请求字段: %s",
                self._label, list(kwargs.keys()),
            )
            payload = {**kwargs, "base_info": _build_base_info()}
            async with session.post(
                self._bot_url("/ilink/bot/getuploadurl"),
                headers=self._get_headers(),
                json=payload,
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("%s getuploadurl HTTP %s", self._label, resp.status)
                    return None
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(
                        "%s getuploadurl 响应解析失败: %s",
                        self._label, text[:200],
                    )
                    return None
                ret = data.get("ret", 0)
                if ret != 0:
                    # 失败响应里带 upload_id / 临时上传地址这类一次性凭据，只记字段名
                    logger.error(
                        "%s getuploadurl 失败: ret=%s, msg=%s, 响应字段=%s",
                        self._label, ret, data.get("msg", ""),
                        list(data.keys()),
                    )
                    return None
                logger.debug(
                    "%s getuploadurl 成功: %s",
                    self._label,
                    list(data.keys()),
                )
                return data
        except Exception as e:
            logger.error("%s getuploadurl 异常: %s", self._label, e)
            return None

    # ── 消息发送 ──────────────────────────────────────

    async def _persist_context_token(
        self, user_id: str, token: str
    ) -> None:
        """将 context_token 持久化到 MongoDB（用于Bot重启后恢复主动消息能力）。"""
        try:
            from app.db.mongo import get_database

            await get_database().wechat_context_tokens.update_one(
                {"user_id": user_id, "bot_id": self.bot_id},
                {
                    "$set": {
                        "token": token,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except Exception as e:
            logger.debug(
                "%s context_token 持久化失败: %s", self._label, e
            )

    async def load_context_tokens(self) -> None:
        """从 MongoDB 加载所有缓存的 context_token（启动时调用）。"""
        try:
            from app.db.mongo import get_database

            cursor = get_database().wechat_context_tokens.find(
                {"bot_id": self.bot_id}
            )
            count = 0
            async for doc in cursor:
                uid = doc.get("user_id", "")
                tkn = doc.get("token", "")
                if uid and tkn:
                    self._context_tokens[uid] = tkn
                    count += 1
            if count:
                logger.info(
                    "%s 从 DB 恢复 %d 个 context_token",
                    self._label, count,
                )
        except Exception as e:
            logger.warning(
                "%s 加载 context_token 失败: %s", self._label, e
            )

    async def send_proactive(self, user_id: str, content: str) -> bool:
        """
        主动发送消息给身份（基于缓存的 context_token）。

        用于定时关怀、主动触达等场景。
        前提：身份至少给Bot发送过一条消息（获取初始 context_token）。

        Args:
            user_id: 微信身份的 ilink user_id
            content: 消息文本内容

        Returns:
            发送是否成功
        """
        ctx = self._get_context_token(user_id)
        if not ctx:
            logger.warning(
                "%s 主动消息失败: 身份 %s 无 context_token",
                self._label, user_id,
            )
            return False
        logger.info("%s 主动消息 -> %s", self._label, user_id)
        return await self.send_text(user_id, content)

    @property
    def last_send_error(self) -> str:
        """最近一次发送失败的平台错误详情（发送成功后清空）。"""
        return self._last_send_error

    def _get_context_token(self, user_id: str) -> str:
        """获取指定身份的 context_token。"""
        ctx = self._context_tokens.get(user_id, "")
        if not ctx:
            logger.warning(
                "%s 身份 %s 无缓存的 context_token", self._label, user_id
            )
        return ctx

    def can_send_proactive(self, user_id: str) -> bool:
        """主动消息发送前的轻量预检：无缓存 context_token 则不可发送。

        微信主动消息依赖内存缓存的 context_token（服务重启即丢失），
        供调度器在调用 LLM 生成内容之前调用，避免为注定失败的发送浪费额度。
        """
        return bool(self._context_tokens.get(user_id, ""))

    async def _cache_outbound_quote(
        self, payload: dict[str, Any], response: dict[str, Any]
    ) -> None:
        """发送成功后按服务端 message_id 缓存出站正文/媒体类型。"""
        server_message_id = str(
            response.get("message_id", response.get("msg_id", ""))
        )
        if not server_message_id:
            return
        msg = payload.get("msg", {})
        if not isinstance(msg, dict):
            return
        to_user_id = str(msg.get("to_user_id", ""))
        if not to_user_id:
            return
        text_parts: list[str] = []
        media_kind = ""
        for item in msg.get("item_list", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == 1:
                text_parts.append(
                    str(item.get("text_item", {}).get("text", ""))
                )
            elif item_type == 2:
                media_kind = "image"
            elif item_type == 3:
                media_kind = "voice"
            elif item_type == 4:
                media_kind = "file"
                text_parts.append(
                    str(item.get("file_item", {}).get("file_name", ""))
                )
            elif item_type == 5:
                media_kind = "video"
        body = "\n".join(part for part in text_parts if part).strip()
        if not body and not media_kind:
            return
        await self._cache_quote_message(
            {"from_user_id": to_user_id, "message_id": server_message_id},
            text=body,
            media_kind=media_kind,
            role="assistant",
        )

    async def _send_message_payload(
        self, payload: dict[str, Any], message_kind: str
    ) -> bool:
        """统一发送文本/图片/文件，并同时校验 HTTP 与平台 ret。"""
        try:
            session = await self._ensure_session()
            async with session.post(
                self._bot_url("/ilink/bot/sendmessage"),
                headers=self._get_headers(),
                json=payload,
            ) as resp:
                resp_text = await resp.text()
                if resp.status != 200:
                    self._last_send_error = "HTTP %s" % resp.status
                    logger.error(
                        "%s 发送%s失败: %s",
                        self._label, message_kind, self._last_send_error,
                    )
                    return False
                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    self._last_send_error = "响应解析失败"
                    logger.error("%s 发送%s响应解析失败", self._label, message_kind)
                    return False
                ret = data.get("ret", data.get("errcode", 0))
                if ret != 0:
                    self._last_send_error = str(
                        data.get("msg") or data.get("errmsg") or f"ret={ret}"
                    )
                    logger.error(
                        "%s 发送%s失败: %s",
                        self._label, message_kind, self._last_send_error,
                    )
                    return False
                self._last_send_error = ""
                await self._cache_outbound_quote(payload, data)
                return True
        except Exception as e:
            self._last_send_error = str(e)
            logger.error("%s 发送%s异常: %s", self._label, message_kind, e)
            return False

    async def send_text(self, to_user_id: str, text: str) -> bool:
        """发送文本消息，返回平台是否确认成功。

        平台返回 `ret != 0`、响应不可解析或网络异常都必须返回 False：
        主动消息调度器据此写 failed，避免"记录成功但用户没收到"。
        """
        if not text.strip():
            # 空文本发出去只会拿到平台失败响应，且占用一次 context_token
            logger.info("%s 跳过空文本发送: to=%s", self._label, to_user_id)
            self._last_send_error = "空文本"
            return False
        payload: dict = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": "ec-%s" % uuid.uuid4().hex[:12],
                "message_type": 2,  # BOT 消息
                "message_state": 2,  # FINISHED
                "context_token": self._get_context_token(to_user_id),
                "item_list": [
                    {"type": 1, "text_item": {"text": text}}
                ],
            },
            "base_info": _build_base_info(),
        }

        logger.info(
            "%s 发送文本: to=%s, len=%d, has_ctx=%s",
            self._label, to_user_id, len(text),
            bool(self._get_context_token(to_user_id)),
        )

        sent = await self._send_message_payload(payload, "文本")
        if sent:
            logger.info("%s 文本已发送给 %s", self._label, to_user_id)
        return sent

    async def send_image(self, to_user_id: str, image_data: bytes) -> bool:
        """发送图片（官方 CDN 上传）。"""
        upload_result = await self._upload_cdn_media(
            image_data, to_user_id, "image"
        )
        if not upload_result:
            self._last_send_error = "图片 CDN 上传失败"
            return False

        cdn_media = upload_result["cdn_media"]
        cipher_size = upload_result["cipher_size"]

        payload: dict = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": "ec-%s" % uuid.uuid4().hex[:12],
                "message_type": 2,
                "message_state": 2,
                "context_token": self._get_context_token(to_user_id),
                "item_list": [
                    {
                        "type": 2,
                        "image_item": {
                            "media": cdn_media,
                            "mid_size": cipher_size,
                        },
                    }
                ],
            },
            "base_info": _build_base_info(),
        }

        sent = await self._send_message_payload(payload, "图片")
        if sent:
            logger.info("%s 图片已发送给 %s", self._label, to_user_id)
        return sent

    async def send_file(
        self, to_user_id: str, file_data: bytes, file_name: str
    ) -> bool:
        """发送文件（官方 CDN 上传，用于 MP3 语音等场景）。"""
        upload_result = await self._upload_cdn_media(
            file_data, to_user_id, "file"
        )
        if not upload_result:
            self._last_send_error = "文件 CDN 上传失败"
            return False

        cdn_media = upload_result["cdn_media"]
        raw_size = upload_result["raw_size"]

        payload: dict = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": "ec-%s" % uuid.uuid4().hex[:12],
                "message_type": 2,
                "message_state": 2,
                "context_token": self._get_context_token(to_user_id),
                "item_list": [
                    {
                        "type": 4,
                        "file_item": {
                            "media": cdn_media,
                            "file_name": file_name,
                            "len": str(raw_size),
                        },
                    }
                ],
            },
            "base_info": _build_base_info(),
        }

        sent = await self._send_message_payload(payload, "文件")
        if sent:
            logger.info("%s 文件已发送给 %s (%s)", self._label, to_user_id, file_name)
        return sent

    async def send_video(self, to_user_id: str, video_data: bytes) -> bool:
        """发送视频（type=5），发送前按官方 CDN 流程加密上传。"""
        upload_result = await self._upload_cdn_media(
            video_data, to_user_id, "video"
        )
        if not upload_result:
            self._last_send_error = "视频 CDN 上传失败"
            return False
        cdn_media = upload_result["cdn_media"]
        cipher_size = upload_result["cipher_size"]
        payload: dict = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": "ec-%s" % uuid.uuid4().hex[:12],
                "message_type": 2,
                "message_state": 2,
                "context_token": self._get_context_token(to_user_id),
                "item_list": [{
                    "type": 5,
                    "video_item": {
                        "media": cdn_media,
                        "video_size": cipher_size,
                    },
                }],
            },
            "base_info": _build_base_info(),
        }
        sent = await self._send_message_payload(payload, "视频")
        if sent:
            logger.info("%s 视频已发送给 %s", self._label, to_user_id)
        return sent

    # ── sendtyping 打字状态 ───────────────────────────

    async def _refresh_typing_ticket(self) -> None:
        """从 getconfig 获取 typing_ticket（非阻塞）。"""
        if not self._bot_token:
            return
        try:
            session = await self._ensure_session()
            async with session.post(
                self._bot_url("/ilink/bot/getconfig"),
                headers=self._get_headers(),
                json={"base_info": _build_base_info()},
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return
                self._typing_ticket = data.get("typing_ticket", "")
                if self._typing_ticket:
                    logger.info("%s typing_ticket 已获取", self._label)
        except Exception as e:
            logger.debug("%s typing_ticket 获取失败: %s", self._label, e)

    async def _send_typing(self, to_user_id: str, status: int = 1) -> None:
        """发送打字状态。status: 1=开始, 2=取消。"""
        if not self._typing_ticket:
            return
        payload: dict = {
            "typing_ticket": self._typing_ticket,
            "ilink_user_id": to_user_id,
            "status": status,
            "base_info": _build_base_info(),
        }
        try:
            session = await self._ensure_session()
            async with session.post(
                self._bot_url("/ilink/bot/sendtyping"),
                headers=self._get_headers(),
                json=payload,
            ):
                pass  # fire-and-forget
        except Exception as e:
            logger.debug("%s sendtyping 失败: %s", self._label, e)

    # ── AES 加解密 ────────────────────────────────────

    @staticmethod
    def _parse_aes_key(aes_key_b64: str) -> bytes:
        """解析 AES 密钥（兼容 16 字节直接用 / 32 字节 hex ASCII 两种格式）。"""
        raw = base64.b64decode(aes_key_b64)
        if len(raw) == 16:
            return raw
        if len(raw) == 32:
            try:
                return bytes.fromhex(raw.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                pass
        # 兜底: 截取前 16 字节
        return raw[:16]

    @staticmethod
    def _aes_encrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB 加密 + PKCS7 填充。"""
        from Crypto.Cipher import AES

        pad_len = 16 - (len(data) % 16)
        padded = data + bytes([pad_len] * pad_len)
        return AES.new(key, AES.MODE_ECB).encrypt(padded)

    @staticmethod
    def _aes_decrypt(data: bytes, key: bytes) -> bytes:
        """AES-128-ECB 解密 + 去除 PKCS7 填充。"""
        from Crypto.Cipher import AES

        decrypted = AES.new(key, AES.MODE_ECB).decrypt(data)
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 16:
            return decrypted
        return decrypted[:-pad_len]

    # ── 多媒体输入处理 ────────────────────────────────

    async def _save_image(self, image_data: bytes) -> Optional[str]:
        """保存图片到媒体库，返回 media_url。"""
        try:
            from app.services.media_storage import save_image_from_bytes

            return await save_image_from_bytes(image_data)
        except Exception as e:
            logger.error("%s 图片保存失败: %s", self._label, e)
            return None

    async def _handle_image_input(self, image_data: bytes) -> str:
        """调用 Vision 描述图片内容，支持表情包尺寸预判。"""
        try:
            from app.services.vision_service import get_vision_service

            is_likely_sticker = self._is_likely_sticker(image_data)
            svc = get_vision_service()

            # 自动检测图片格式，构造正确的 base64 data URI
            mime_type = self._detect_image_mime(image_data)
            image_b64 = (
                "data:%s;base64,%s"
                % (mime_type, base64.b64encode(image_data).decode())
            )
            if is_likely_sticker:
                return await svc.describe_image_with_hint(
                    image_b64, is_likely_sticker=True
                )
            return await svc.describe_image(image_b64)
        except Exception as e:
            logger.error("%s 图片识别失败: %s", self._label, e)
            return ""

    @staticmethod
    def _is_likely_sticker(data: bytes) -> bool:
        """根据图片尺寸预判是否为表情包（宽或高 <= 阈值）。"""
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(data))
            max_dim = max(img.width, img.height)
            return max_dim <= settings.sticker_max_dimension
        except Exception:
            return False

    @staticmethod
    def _detect_image_mime(data: bytes) -> str:
        """根据文件头（magic bytes）检测图片 MIME 类型。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:4] == b"GIF8":
            return "image/gif"
        # 兜底为 JPEG（最常见格式）
        return "image/jpeg"

    async def _handle_voice_input(self, silk_data: bytes) -> str:
        """下载的 silk 数据 -> wav -> ASR。"""
        try:
            from app.services.asr_service import ASRService
            from app.services.audio_utils import silk_to_wav

            if self._asr is None:
                self._asr = ASRService()
            wav_data = await silk_to_wav(silk_data)
            text = await self._asr.transcribe(wav_data, fmt="wav")
            if text:
                logger.info("%s ASR 结果: %s", self._label, text[:60])
            return text or ""
        except Exception as e:
            logger.error("%s ASR 识别失败: %s", self._label, e)
            return ""

    # ── 多媒体输出（fire-and-forget）──────────────────

    async def _fire_and_forget_imagegen(
        self, to_user_id: str, task: ImageTask, conv_id: str
    ) -> None:
        """异步生成图片并发送。"""
        try:
            from app.services.imagegen_service import ImageGenService

            if self._imagegen is None:
                self._imagegen = ImageGenService()

            img_bytes = await self._imagegen.generate_for_task(task)

            # 保存到 MongoDB
            if conv_id:
                try:
                    from app.db.repositories.message_repo import MessageRepository
                    from app.services.media_storage import save_image_from_bytes

                    media_url = await save_image_from_bytes(img_bytes)
                    await MessageRepository().create(
                        conversation_id=conv_id,
                        role="assistant",
                        content="(AI生成了一张图片)",
                        content_type="image",
                        media_url=media_url,
                    )
                except Exception as e:
                    logger.error(
                        "%s 图片保存到DB失败: %s", self._label, e
                    )

            if not await self.send_image(to_user_id, img_bytes):
                raise RuntimeError(self._last_send_error or "图片发送失败")
            logger.info(
                "%s 图片已发送, prompt=%s", self._label, task.prompt[:40]
            )
        except Exception as e:
            logger.error("%s 图片生成/发送失败: %s", self._label, e)
            if task.account_id:
                # 额度在 Engine 侧已按张预扣；图没生成或没发出去就该退
                from app.db.repositories.quota_repo import QuotaRepository

                await QuotaRepository().refund(task.account_id, "image", 1)

    async def _fire_and_forget_sticker(
        self, to_user_id: str, task: StickerTask, conv_id: str
    ) -> None:
        """异步从媒体库读取表情包字节并发送（短延迟模拟真人节奏）。"""
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))

            from app.db.repositories.media_repo import MediaRepository

            media_id = task.media_url.rsplit("/", 1)[-1]
            media_doc = await MediaRepository().find_by_id(media_id)
            if not media_doc or not media_doc.get("data"):
                logger.warning(
                    "%s 表情包媒体不存在: id=%s",
                    self._label, mask_secret(media_id),
                )
                return

            if not await self.send_image(to_user_id, media_doc["data"]):
                raise RuntimeError(self._last_send_error or "表情包发送失败")
            logger.info(
                "%s 表情包已发送, desc=%s", self._label, task.description[:20]
            )

            # 记录消息（供管理后台查看）
            if conv_id:
                try:
                    from app.db.repositories.message_repo import (
                        MessageRepository,
                    )

                    await MessageRepository().create(
                        conversation_id=conv_id,
                        role="assistant",
                        content="(发送了一个表情包)",
                        content_type="image",
                        media_url=task.media_url,
                    )
                except Exception as e:
                    logger.error(
                        "%s 表情包消息保存失败: %s", self._label, e
                    )
        except Exception as e:
            logger.error("%s 表情包发送失败: %s", self._label, e)

    async def _fire_and_forget_tts(
        self,
        to_user_id: str,
        text: str,
        voice_id: str = "longxiaochun",
        voice_instruction: str = "",
        voice_pitch_rate: float = 1.0,
        voice_emotion_tag: str = "",
        account_id: str = "",
    ) -> None:
        """异步生成语音并发送，失败时降级为文字回复。"""
        # 语音额度按"条"计（与后台"剩余 N 次"的展示口径一致），不按文本长度
        quota_charged = False
        try:
            # 1. 前置扣减语音额度，不足则降级文字
            if account_id:
                from app.db.repositories.quota_repo import QuotaRepository

                ok, reason = await QuotaRepository().deduct(
                    account_id, "voice", 1
                )
                if not ok:
                    logger.info(
                        "%s 语音额度不足(%s)，降级为文字回复",
                        self._label, reason,
                    )
                    await self.send_text(to_user_id, text)
                    return
                quota_charged = True

            # 2. TTS 合成 + 以 MP3 文件形式发送
            from app.services.tts_service import TTSService

            if self._tts is None:
                self._tts = TTSService()

            mp3_bytes = await self._tts.synthesize(
                text,
                voice_id=voice_id,
                instruction=voice_instruction,
                pitch_rate=voice_pitch_rate,
                emotion_tag=voice_emotion_tag,
            )

            if not await self.send_file(to_user_id, mp3_bytes, "voice_reply.mp3"):
                raise RuntimeError(self._last_send_error or "语音文件发送失败")
            logger.info("%s TTS 语音文件已发送", self._label)

        except Exception as e:
            logger.error("%s TTS 发送失败，降级为文字: %s", self._label, e)
            if quota_charged and account_id:
                # 合成或发送没成功就不该收这条语音的额度（对方收到的是文字兜底）
                from app.db.repositories.quota_repo import QuotaRepository

                await QuotaRepository().refund(account_id, "voice", 1)
            try:
                await self.send_text(to_user_id, text)
            except Exception as e2:
                logger.error("%s 文字兜底也失败: %s", self._label, e2)


# ── BotManager ────────────────────────────────────────


class WeChatBotManager:
    """微信Bot生命周期管理器（对齐 QQ BotManager 设计模式）。"""

    def __init__(self, poll_interval: int = 30) -> None:
        self._poll_interval = poll_interval
        # registry: config_id -> (config, client, task)
        self._registry: dict[
            str, tuple[BotConfigInDB, WeChatClient, asyncio.Task]
        ] = {}
        self._pending_clients: dict[str, WeChatClient] = {}
        self._watch_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._repo = BotConfigRepository()

    # ── 启动与加载 ────────────────────────────────────

    async def load_bots(self) -> None:
        """从数据库加载所有在线的微信Bot并启动。"""
        configs = await self._repo.list_by_platform("wechat")
        for cfg in configs:
            if cfg.status == "online" and cfg.bot_token and not cfg.is_blocked:
                try:
                    await self._start_bot(cfg)
                except Exception as e:
                    logger.error(
                        "[WeChatBotManager] 启动Bot %s 失败: %s",
                        cfg.name or cfg.id, e,
                    )

        # 启动配置监控循环
        self._watch_task = asyncio.create_task(self._config_watch_loop())
        logger.info(
            "[WeChatBotManager] 已启动: %d bot(s), 轮询间隔 %ds",
            len(self._registry), self._poll_interval,
        )

    async def _start_bot(self, cfg: BotConfigInDB) -> None:
        """启动单个Bot。"""
        client = WeChatClient(
            bot_id=cfg.id,
            bot_token=cfg.bot_token,
            on_token_update=self._on_token_update,
            account_id=cfg.account_id,
        )
        task = asyncio.create_task(self._supervised_run(cfg.id, client))
        self._registry[cfg.id] = (cfg, client, task)
        logger.info(
            "[WeChatBotManager] Bot 已启动: %s", cfg.name or cfg.id
        )

    async def _stop_bot(self, config_id: str) -> None:
        """安全停止单个Bot。"""
        entry = self._registry.pop(config_id, None)
        if entry is None:
            return
        cfg, client, task = entry
        await client.stop()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        logger.info(
            "[WeChatBotManager] Bot 已停止: %s", cfg.name or config_id
        )

    async def _supervised_run(
        self, config_id: str, client: WeChatClient
    ) -> None:
        """包装Bot运行，捕获未预期异常防止级联崩溃。"""
        try:
            await client.start()
        except asyncio.CancelledError:
            pass
        except SessionExpiredError:
            logger.warning(
                "[WeChatBotManager] Bot %s session 已过期", config_id
            )
        except Exception as e:
            logger.error(
                "[WeChatBotManager] Bot %s 意外终止: %s", config_id, e
            )

    # ── 配置热加载 ────────────────────────────────────

    async def _config_watch_loop(self) -> None:
        """定期轮询 MongoDB，检测配置变更并应用。"""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(self._poll_interval)
            try:
                new_configs = await self._repo.list_by_platform("wechat")
            except Exception as e:
                logger.warning(
                    "[WeChatBotManager] 配置加载失败: %s", e
                )
                continue

            # 同时刷新 settings
            try:
                changed = await settings.reload()
                if changed:
                    # Provider 由 model_configs 分组与各功能的 config_id 引用决定
                    if changed & {"model_configs"} or any(
                        f.endswith(("_config_id", "_timeout_seconds"))
                        for f in changed
                    ):
                        from app.llm.provider_factory import reset_provider

                        reset_provider()
                        logger.info(
                            "[WeChatBotManager] LLM provider 已重建"
                        )
            except Exception as e:
                logger.warning(
                    "[WeChatBotManager] Settings reload 失败: %s", e
                )

            await self._apply_diff(new_configs)

    async def _apply_diff(self, new_configs: list[BotConfigInDB]) -> None:
        """对比新旧配置，执行增删改。"""
        # 只处理在线、有 token 且未被停用的配置
        active_map = {
            c.id: c for c in new_configs
            if c.status == "online" and c.bot_token and not c.is_blocked
        }
        current_ids = set(self._registry.keys())
        new_ids = set(active_map.keys())

        # 新增
        for config_id in new_ids - current_ids:
            logger.info(
                "[WeChatBotManager] 检测到新Bot: %s", config_id
            )
            await self._start_bot(active_map[config_id])

        # 删除（离线或被移除）
        for config_id in current_ids - new_ids:
            logger.info(
                "[WeChatBotManager] Bot 已离线: %s", config_id
            )
            await self._stop_bot(config_id)

    # ── Admin 路由接口 ────────────────────────────────

    async def create_bot(
        self,
        name: str,
        account_id: str = "",
        origin: Literal["admin", "web"] = "admin",
    ) -> WeChatClient:
        """创建新Bot（未登录状态）。"""
        from app.models.bot_config import BotConfigCreate

        cfg = BotConfigCreate(
            name=name,
            account_id=account_id,
            platform="wechat",
            origin=origin,
            status="pending_qr",
        )
        created = await self._repo.create(cfg)

        client = WeChatClient(
            bot_id=created.id,
            on_token_update=self._on_token_update,
            account_id=account_id,
        )
        self._pending_clients[created.id] = client
        return client

    def get_client(self, config_id: str) -> Optional[WeChatClient]:
        """获取指定Bot的客户端。"""
        entry = self._registry.get(config_id)
        if entry:
            return entry[1]
        # 检查待登录客户端
        return self._pending_clients.get(config_id)

    def is_bot_running(self, config_id: str) -> bool:
        """检查Bot是否正在运行。"""
        entry = self._registry.get(config_id)
        if entry is None:
            return False
        _, _, task = entry
        return not task.done()

    def start_bot_task(
        self, config_id: str, client: WeChatClient
    ) -> None:
        """启动Bot轮询任务（供 admin 路由调用）。"""
        # 构造临时 config
        cfg = BotConfigInDB(
            id=config_id,
            name=config_id[:8],
            platform="wechat",
            status="online",
            bot_token=client._bot_token,
        )
        task = asyncio.create_task(
            self._supervised_run(config_id, client)
        )
        self._registry[config_id] = (cfg, client, task)
        # 从 pending 移除
        self._pending_clients.pop(config_id, None)
        logger.info(
            "[WeChatBotManager] Bot %s 任务已启动", config_id
        )

    async def cancel_bot_task(self, config_id: str) -> None:
        """取消Bot轮询任务（供 admin 路由调用）。"""
        entry = self._registry.pop(config_id, None)
        if entry is None:
            return
        _, client, task = entry
        await client.stop()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        logger.info(
            "[WeChatBotManager] Bot %s 任务已取消", config_id
        )

    async def remove_bot(self, config_id: str) -> None:
        """删除 Bot，并清理只属于该渠道的游标、引用缓存与登录状态。"""
        await self.cancel_bot_task(config_id)
        # 移除 pending
        self._pending_clients.pop(config_id, None)
        from app.db.mongo import get_database
        from app.db.repositories.wechat_quote_repo import (
            WeChatQuoteCacheRepository,
        )

        await WeChatQuoteCacheRepository().delete_by_bot(config_id)
        await get_database().wechat_sync_state.delete_one({"bot_id": config_id})
        await self._repo.delete(config_id)
        logger.info("[WeChatBotManager] Bot %s 已删除", config_id)

    def list_bots(self) -> list[dict]:
        """列出所有Bot状态。"""
        return [
            {
                "config_id": config_id,
                "running": not task.done(),
            }
            for config_id, (_, _, task) in self._registry.items()
        ]

    async def _on_token_update(
        self, bot_id: str, token: str, expire_at: datetime
    ) -> None:
        """Token 更新时持久化到数据库。"""
        if token:
            await self._repo.update_wechat_token(bot_id, token, expire_at)
        logger.info(
            "[WeChatBotManager] Bot %s token 已更新, 过期: %s",
            bot_id, expire_at,
        )

    async def shutdown(self) -> None:
        """停止所有Bot和监控循环。"""
        self._shutdown_event.set()
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

        config_ids = list(self._registry.keys())
        for config_id in config_ids:
            await self._stop_bot(config_id)

        logger.info("[WeChatBotManager] 所有微信Bot已关闭")


# ── 全局管理器 ────────────────────────────────────────

_wechat_manager: Optional[WeChatBotManager] = None


def get_wechat_manager() -> WeChatBotManager:
    """获取全局微信Bot管理器。"""
    global _wechat_manager
    if _wechat_manager is None:
        _wechat_manager = WeChatBotManager()
    return _wechat_manager
