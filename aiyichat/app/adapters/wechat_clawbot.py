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
from typing import TYPE_CHECKING, Callable, Literal, Optional

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
from app.utils.redaction import brief_json
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
GETCONFIG_URL = f"{ILINK_BASE_URL}/ilink/bot/getconfig"
GETUPDATES_URL = f"{ILINK_BASE_URL}/ilink/bot/getupdates"
SENDMESSAGE_URL = f"{ILINK_BASE_URL}/ilink/bot/sendmessage"
SENDTYPING_URL = f"{ILINK_BASE_URL}/ilink/bot/sendtyping"
GETUPLOADURL_URL = f"{ILINK_BASE_URL}/ilink/bot/getuploadurl"

# CDN 基础 URL
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


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
    ) -> None:
        self.bot_id = bot_id
        self._label = "[WX:%s]" % bot_id[:8]

        # 认证
        self._bot_token: str = bot_token
        self._ilink_user_id: Optional[str] = None
        self._token_expire_at: Optional[datetime] = None

        # 消息轮询
        self._get_updates_buf: str = ""
        self._running: bool = False
        self._reconnect_delay: int = 5

        # per-user context_token 缓存（官方协议: 每条入站消息独立 context_token）
        self._context_tokens: dict[str, str] = {}

        # sendtyping
        self._typing_ticket: str = ""

        # 消息去重（基于官方 message_id）
        self._processed_msg_ids: collections.OrderedDict[int, float] = (
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

        # 登录流程临时数据
        self._qrcode: str = ""

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

    def _get_headers(self) -> dict[str, str]:
        """生成 iLink API 请求头。"""
        random_uin = secrets.randbits(32)
        uin_b64 = base64.b64encode(random_uin.to_bytes(4, "big")).decode()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin_b64,
        }
        if self._bot_token:
            headers["Authorization"] = "Bearer %s" % self._bot_token
        return headers

    # ── 登录流程 ──────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """确保 HTTP session 已创建。"""
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_login_qrcode(self) -> dict:
        """获取登录二维码。"""
        session = await self._ensure_session()
        headers = {"iLink-App-ClientVersion": "1"}

        async with session.get(
            QRCODE_URL, params={"bot_type": "3"}, headers=headers
        ) as resp:
            raw_data = await resp.read()
            try:
                data = json.loads(raw_data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.error("%s 二维码响应解析失败: %s", self._label, e)
                raise RuntimeError("获取二维码失败: 无法解析响应")

            logger.info("%s QRCode response: %s", self._label, data)

            ret_code = data.get("ret", data.get("errcode", 0))
            if ret_code != 0:
                error_msg = data.get("msg", data.get("errmsg", "Unknown error"))
                raise RuntimeError(
                    "获取二维码失败: %s (code: %s)" % (error_msg, ret_code)
                )

            if "qrcode" not in data:
                raise RuntimeError("获取二维码失败: 响应中无qrcode字段: %s" % data)

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

    async def poll_qrcode_status(self, qrcode: str) -> Optional[str]:
        """长轮询扫码状态，返回 bot_token。"""
        session = await self._ensure_session()
        try:
            async with session.get(
                QRCODE_STATUS_URL,
                params={"qrcode": qrcode},
                headers={"iLink-App-ClientVersion": "1"},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(
                        "%s 扫码状态响应解析失败: %s", self._label, text[:100]
                    )
                    return None

                logger.info("%s 扫码状态: %s", self._label, data)

                ret_code = data.get("ret", data.get("errcode", 0))
                if ret_code != 0:
                    error_msg = data.get("msg", data.get("errmsg", ""))
                    if "timeout" in error_msg.lower() or ret_code == -14:
                        return None
                    logger.warning(
                        "%s 扫码异常: %s (code: %s)",
                        self._label, error_msg, ret_code,
                    )
                    return None

                if data.get("bot_token"):
                    bot_token = data["bot_token"]
                    self._ilink_user_id = data.get("ilink_user_id")
                    self._bot_token = bot_token
                    logger.info(
                        "%s 扫码成功, ilink_user_id=%s",
                        self._label, self._ilink_user_id,
                    )
                    return bot_token

                return None
        except asyncio.TimeoutError:
            logger.debug("%s 扫码长轮询超时，将重试", self._label)
            return None

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
                GETCONFIG_URL,
                headers=self._get_headers(),
                json={"base_info": {"channel_version": "1.0.0"}},
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
        """启动消息轮询（含指数退避重连）。"""
        if not self._bot_token:
            logger.warning("%s 无 bot_token，无法启动", self._label)
            return

        self._running = True
        self._reconnect_delay = 5
        max_reconnect_delay = 120
        await self._ensure_session()

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
        """停止消息轮询。"""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("%s 微信Bot停止", self._label)

    async def _mark_offline(self) -> None:
        """标记 Bot 为离线状态。"""
        try:
            repo = BotConfigRepository()
            await repo.update_status(self.bot_id, "offline")
            logger.info("%s 已标记为离线", self._label)
        except Exception as e:
            logger.error("%s 标记离线失败: %s", self._label, e)

    async def _polling_loop(self) -> None:
        """长轮询消息主循环（35s hold）。"""
        session = await self._ensure_session()
        payload: dict = {
            "cursor": self._get_updates_buf or "",
            "hold": 35000,
        }

        try:
            async with session.post(
                GETUPDATES_URL,
                headers=self._get_headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=50),
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(
                        "%s getupdates 响应解析失败: %s",
                        self._label, text[:100],
                    )
                    await asyncio.sleep(2)
                    return

                # 检查 session 过期
                ret = data.get("ret")
                if ret is not None and ret != 0:
                    if ret == -14:
                        raise SessionExpiredError("session timeout (ret: -14)")
                    error_msg = data.get("msg", "")
                    logger.error(
                        "%s getupdates 错误: ret=%s, msg=%s",
                        self._label, ret, error_msg,
                    )
                    await asyncio.sleep(2)
                    return

                # 更新游标
                if "get_updates_buf" in data:
                    self._get_updates_buf = data["get_updates_buf"]

                # 处理消息
                messages = data.get("msgs", [])
                if messages:
                    logger.info(
                        "%s getupdates 返回 %d 条消息",
                        self._label, len(messages),
                    )
                for msg in messages:
                    try:
                        await self._handle_message(msg)
                    except Exception as e:
                        logger.error(
                            "%s 处理消息错误: %s",
                            self._label, e, exc_info=True,
                        )

                # 无消息时短暂休眠避免空转
                if not messages:
                    await asyncio.sleep(1)

        except asyncio.TimeoutError:
            logger.debug("%s getupdates 长轮询超时，继续下一轮", self._label)
        except SessionExpiredError:
            raise
        except Exception as e:
            logger.error(
                "%s getupdates 请求异常: %s", self._label, e, exc_info=True
            )

    # ── 消息去重 ──────────────────────────────────────

    async def _is_msg_duplicate(self, msg_id: int) -> bool:
        """基于 message_id 的消息去重。"""
        async with self._dedup_lock:
            if msg_id in self._processed_msg_ids:
                logger.debug("%s 跳过重复消息: %s", self._label, msg_id)
                return True
            self._processed_msg_ids[msg_id] = time.time()
            while len(self._processed_msg_ids) > self._dedup_max_size:
                self._processed_msg_ids.popitem(last=False)
            return False

    # ── 消息处理 ──────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """处理单条消息（支持文本、图片、语音）。"""
        # 1. 消息去重（基于官方 message_id）
        msg_id = msg.get("message_id", 0)
        if msg_id and await self._is_msg_duplicate(msg_id):
            return

        # 2. 缓存 per-user context_token 并持久化到 MongoDB
        from_user_id = msg.get("from_user_id", "")
        msg_ctx_token = msg.get("context_token", "")
        if msg_ctx_token and from_user_id:
            self._context_tokens[from_user_id] = msg_ctx_token
            spawn(
                self._persist_context_token(from_user_id, msg_ctx_token)
            )

        logger.info(
            "%s 收到消息: msg_id=%s, from=%s, has_ctx=%s",
            self._label, msg_id, from_user_id, bool(msg_ctx_token),
        )

        # 3. 发送打字状态
        spawn(self._send_typing(from_user_id, status=1))

        # 4. 解析 item_list
        item_list = msg.get("item_list", [])
        text = ""
        media_url: Optional[str] = None
        content_type = "text"

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

            elif item_type == 5:  # VIDEO
                content_type = "video"
                text = "[身份发送了一个视频]"

        if not text:
            logger.debug("%s 消息无可处理文本", self._label)
            # 取消打字状态
            spawn(self._send_typing(from_user_id, status=2))
            return

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
        # 语音触发：身份发语音直接触发；或动态窗口内（身份发过语音后的随机
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
            # 仅身份发语音触发时开启窗口（固定窗口，窗口内不续期）
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

        if not encrypt_query_param or not aes_key_b64:
            logger.warning("%s CDN 媒体缺少必要字段，跳过下载", self._label)
            return None

        # 构造下载 URL
        download_url = "%s/download?encrypted_query_param=%s" % (
            CDN_BASE_URL, encrypt_query_param
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
            base_info={"channel_version": "1.0.0"},
        )
        if not upload_info:
            return None

        # 5. 从响应中提取 upload_param，构造 CDN 上传 URL
        upload_param = upload_info.get("upload_param", "")
        if not upload_param:
            logger.error("%s getuploadurl 未返回 upload_param", self._label)
            return None

        from urllib.parse import quote

        cdn_upload_url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
            CDN_BASE_URL, quote(upload_param), quote(filekey)
        )

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
            async with session.post(
                GETUPLOADURL_URL,
                headers=self._get_headers(),
                json=kwargs,
            ) as resp:
                text = await resp.text()
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
        session = await self._ensure_session()
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
            "base_info": {"channel_version": "2.0.0"},
        }

        logger.info(
            "%s 发送文本: to=%s, len=%d, has_ctx=%s",
            self._label, to_user_id, len(text),
            bool(self._get_context_token(to_user_id)),
        )

        try:
            async with session.post(
                SENDMESSAGE_URL,
                headers=self._get_headers(),
                json=payload,
            ) as resp:
                resp_text = await resp.text()
                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    logger.error(
                        "%s sendmessage 响应解析失败: %s",
                        self._label, resp_text[:100],
                    )
                    self._last_send_error = "响应解析失败"
                    return False
                ret = data.get("ret", data.get("errcode"))
                if ret is not None and ret != 0:
                    self._last_send_error = str(
                        data.get("msg")
                        or data.get("errmsg")
                        or f"ret={ret}"
                    )
                    logger.error(
                        "%s 发送文本失败: %s",
                        self._label,
                        brief_json(data, ("ret", "errcode", "errmsg")),
                    )
                    return False
                self._last_send_error = ""
                logger.info(
                    "%s 文本已发送给 %s", self._label, to_user_id
                )
                return True
        except Exception as e:
            self._last_send_error = str(e)
            logger.error("%s 发送文本异常: %s", self._label, e)
            return False

    async def send_image(self, to_user_id: str, image_data: bytes) -> None:
        """发送图片（官方 CDN 上传）。"""
        upload_result = await self._upload_cdn_media(
            image_data, to_user_id, "image"
        )
        if not upload_result:
            raise RuntimeError("图片 CDN 上传失败")

        cdn_media = upload_result["cdn_media"]
        cipher_size = upload_result["cipher_size"]

        session = await self._ensure_session()
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
            "base_info": {"channel_version": "2.0.0"},
        }

        try:
            async with session.post(
                SENDMESSAGE_URL,
                headers=self._get_headers(),
                json=payload,
            ) as resp:
                resp_text = await resp.text()
                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    logger.error(
                        "%s 图片 sendmessage 响应解析失败: %s",
                        self._label, resp_text[:100],
                    )
                    return
                ret = data.get("ret")
                if ret is not None and ret != 0:
                    logger.error(
                        "%s 发送图片失败: %s", self._label, data
                    )
                else:
                    logger.info(
                        "%s 图片已发送给 %s", self._label, to_user_id
                    )
        except Exception as e:
            logger.error("%s 发送图片异常: %s", self._label, e)

    async def send_file(
        self, to_user_id: str, file_data: bytes, file_name: str
    ) -> None:
        """发送文件（官方 CDN 上传，用于 MP3 语音等场景）。"""
        upload_result = await self._upload_cdn_media(
            file_data, to_user_id, "file"
        )
        if not upload_result:
            raise RuntimeError("文件 CDN 上传失败")

        cdn_media = upload_result["cdn_media"]
        raw_size = upload_result["raw_size"]

        session = await self._ensure_session()
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
                            "file_size": raw_size,
                        },
                    }
                ],
            },
            "base_info": {"channel_version": "2.0.0"},
        }

        try:
            async with session.post(
                SENDMESSAGE_URL,
                headers=self._get_headers(),
                json=payload,
            ) as resp:
                resp_text = await resp.text()
                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    logger.error(
                        "%s 文件 sendmessage 响应解析失败: %s",
                        self._label, resp_text[:100],
                    )
                    return
                ret = data.get("ret")
                if ret is not None and ret != 0:
                    logger.error(
                        "%s 发送文件失败: %s", self._label, data
                    )
                else:
                    logger.info(
                        "%s 文件已发送给 %s (%s)",
                        self._label, to_user_id, file_name,
                    )
        except Exception as e:
            logger.error("%s 发送文件异常: %s", self._label, e)

    # ── sendtyping 打字状态 ───────────────────────────

    async def _refresh_typing_ticket(self) -> None:
        """从 getconfig 获取 typing_ticket（非阻塞）。"""
        if not self._bot_token:
            return
        try:
            session = await self._ensure_session()
            async with session.post(
                GETCONFIG_URL,
                headers=self._get_headers(),
                json={"base_info": {"channel_version": "1.0.0"}},
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
        }
        try:
            session = await self._ensure_session()
            async with session.post(
                SENDTYPING_URL,
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

            await self.send_image(to_user_id, img_bytes)
            logger.info(
                "%s 图片已发送, prompt=%s", self._label, task.prompt[:40]
            )
        except Exception as e:
            logger.error("%s 图片生成/发送失败: %s", self._label, e)

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
                    "%s 表情包媒体不存在: %s", self._label, task.media_url
                )
                return

            await self.send_image(to_user_id, media_doc["data"])
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
        try:
            # 1. 前置扣减语音额度，不足则降级文字
            if account_id:
                from app.db.repositories.quota_repo import QuotaRepository

                ok, reason = await QuotaRepository().deduct(
                    account_id, "voice", len(text)
                )
                if not ok:
                    logger.info(
                        "%s 语音额度不足(%s)，降级为文字回复",
                        self._label, reason,
                    )
                    await self.send_text(to_user_id, text)
                    return

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

            await self.send_file(to_user_id, mp3_bytes, "voice_reply.mp3")
            logger.info("%s TTS 语音文件已发送", self._label)

        except Exception as e:
            logger.error("%s TTS 发送失败，降级为文字: %s", self._label, e)
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
        """删除Bot。"""
        await self.cancel_bot_task(config_id)
        # 移除 pending
        self._pending_clients.pop(config_id, None)
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
