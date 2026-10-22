"""
QQ Bot WebSocket 客户端（多 Bot 版）。

使用 QQ 官方 Bot API v2 的 WebSocket 方式接收事件。
支持从 bots.yaml 加载多个 Bot 配置，每个 Bot 锁定一个角色档案，
通过 asyncio.gather 并发运行多个 WebSocket 客户端。
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import websockets

from app.bot_config import BotConfig, async_load_bots_config
from app.utils.tasks import spawn
from app.config import settings
from app.conversation.engine import ConversationEngine
from app.conversation.human_rhythm import pacing_delay_seconds
from app.conversation.image_task import ImageTask
from app.conversation.message_splitter import limit_messages
from app.conversation.sticker_task import (
    IMAGE_TRANSCRIBE_PREFIX,
    STICKER_TRANSCRIBE_PREFIX,
    StickerTask,
)
from app.services.voice_window import voice_window

if TYPE_CHECKING:
    from app.services.asr_service import ASRService
    from app.services.imagegen_service import ImageGenService
    from app.services.tts_service import TTSService
    from app.profiles.service import ProfileService

logger = logging.getLogger(__name__)

# QQ Bot API URLs
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_SANDBOX = "https://sandbox.api.sgroup.qq.com/gateway"
GATEWAY_URL_PROD = "https://api.sgroup.qq.com/gateway"

# WebSocket op codes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Intents
INTENT_GROUP_AND_C2C = 1 << 25

# 匹配 QQ 内置表情标签的正则（真实格式：<faceType=1,faceId="269",ext="base64json">）
_QQ_FACE_RE = re.compile(r'<faceType=\d+,faceId="\d+",ext="([^"]*)">')


def _decode_qq_face(match: re.Match[str]) -> str:
    """解码 QQ 表情 ext 字段（base64 JSON），提取表情描述文字。"""
    import base64
    try:
        ext_json = json.loads(base64.b64decode(match.group(1)))
        desc = ext_json.get("text", "")
        return "[表情:%s]" % desc if desc else "[表情]"
    except Exception:
        return "[表情]"


class QQWebSocketClient:
    """QQ Bot WebSocket 客户端（单 Bot 实例）"""

    def __init__(self, bot_config: BotConfig):
        self.bot_config = bot_config
        self.engine = ConversationEngine(
            bot_id=bot_config.id,
        )
        self._label = f"[{bot_config.name or bot_config.id}]"
        self._access_token: str = ""
        self._token_expires_at: float = 0
        self._sequence: int | None = None
        self._heartbeat_interval: float = 45.0
        self._ws = None
        self._running = False
        self._msg_seq: int = int(time.time())
        # 消息去重：记录最近处理过的 msg_id，防止平台重复推送
        self._processed_msg_ids: collections.OrderedDict[str, float] = (
            collections.OrderedDict()
        )
        self._dedup_max_size = 500
        # 保护 _processed_msg_ids 的并发访问，防止竞态条件
        self._dedup_lock = asyncio.Lock()
        # 限流退避缓存：平台 openid → 退避到期时间（UTC）。键是 QQ 侧标识，非 identity_id
        self._rate_limited_openids: dict[str, datetime] = {}
        # 最近一次发送失败的详情（QQ 错误码/消息），供主动消息记录留痕
        self._last_send_error: str = ""
        # 多媒体服务（延迟导入，按需实例化）
        self._asr: ASRService | None = None
        self._vision = None
        self._tts: TTSService | None = None
        self._imagegen: ImageGenService | None = None

    async def get_access_token(self) -> str:
        """获取或刷新 access_token"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        async with httpx.AsyncClient() as client:
            resp = await client.post(TOKEN_URL, json={
                "appId": self.bot_config.app_id,
                "clientSecret": self.bot_config.app_secret,
            })
            data = resp.json()
            logger.info(
                "%s Token response: %s",
                self._label,
                json.dumps(data, ensure_ascii=False),
            )

            if "access_token" not in data:
                raise RuntimeError(
                    f"{self._label} 获取 access_token 失败: "
                    f"{data}"
                )

            self._access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 7200))
            self._token_expires_at = now + expires_in
            logger.info(
                "%s access_token 有效期 %d 秒",
                self._label,
                expires_in,
            )
            return self._access_token

    async def get_gateway_url(self) -> str:
        """获取 WebSocket 网关地址"""
        token = await self.get_access_token()
        gateway_url = (
            GATEWAY_URL_SANDBOX
            if self.bot_config.sandbox_mode
            else GATEWAY_URL_PROD
        )

        async with httpx.AsyncClient() as client:
            resp = await client.get(gateway_url, headers={
                "Authorization": f"QQBot {token}",
            })
            data = resp.json()

            ws_url = data.get("url", "")
            if not ws_url:
                raise RuntimeError(
                    f"{self._label} 获取网关地址失败: "
                    f"{data}"
                )

            return ws_url

    async def _send_identify(self):
        """发送鉴权信息"""
        token = await self.get_access_token()
        identify = {
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": INTENT_GROUP_AND_C2C,
                "shard": [0, 1],
            },
        }
        await self._ws.send(json.dumps(identify))
        logger.info("%s 已发送 Identify 鉴权", self._label)

    async def _send_heartbeat(self):
        """发送心跳"""
        heartbeat = {
            "op": OP_HEARTBEAT,
            "d": self._sequence,
        }
        await self._ws.send(json.dumps(heartbeat))

    async def _heartbeat_loop(self):
        """心跳循环"""
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if self._ws and self._running:
                try:
                    await self._send_heartbeat()
                except Exception as e:
                    logger.error(
                        "%s 心跳失败: %s", self._label, e
                    )
                    break

    async def _handle_dispatch(self, data: dict):
        """处理服务端推送的事件"""
        event_type = data.get("t", "")
        event_data = data.get("d", {})

        if event_type in (
            "C2C_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
        ):
            await self._handle_message_event(
                event_type, event_data
            )
        elif event_type == "READY":
            bot_name = event_data.get("user", {}).get(
                "username", "unknown"
            )
            logger.info(
                "%s Bot 已就绪: %s", self._label, bot_name
            )
            print(
                f"{self._label} Bot 已上线: {bot_name}"
            )
        elif event_type == "RESUMED":
            logger.info("%s 连接已恢复", self._label)

    async def _handle_message_event(
        self, event_type: str, data: dict
    ):
        """处理消息事件（支持文本/语音/图片输入）"""
        msg_id = data.get("id", "")

        # 消息去重：用锁保护检查+记录，防止竞态条件
        if msg_id:
            async with self._dedup_lock:
                if msg_id in self._processed_msg_ids:
                    logger.debug(
                        "%s 跳过重复消息: %s", self._label, msg_id
                    )
                    return
                self._processed_msg_ids[msg_id] = time.time()
                # 超过上限时淘汰最早的记录
                while len(self._processed_msg_ids) > self._dedup_max_size:
                    self._processed_msg_ids.popitem(last=False)

        author = data.get("author", {})
        content = data.get("content", "").strip()
        attachments = data.get("attachments", [])

        # user_id 取的是 QQ 官方下发的 openid（按 Bot appid 生成，同一人在不同 Bot 下不同），
        # 属平台协议字段，与本项目 identities 的 identity_id 无关，禁止改名。
        if event_type == "C2C_MESSAGE_CREATE":
            user_id = author.get("user_openid", "")
            nickname = author.get("username", "QQ身份")
            is_group = False
            group_id = ""
        else:
            user_id = author.get("member_openid", "")
            nickname = author.get("username", "QQ身份")
            is_group = True
            group_id = data.get("group_openid", "")
            content = re.sub(r"<@!\d+>", "", content).strip()

        # 解析 QQ 内置表情标签 <faceType=X,faceId="Y",ext="Z"> → [表情:描述]
        content = _QQ_FACE_RE.sub(_decode_qq_face, content)

        # 解析附件：语音/图片
        voice_wav_url = ""
        voice_asr_text = ""
        image_url = ""
        for att in attachments:
            ct = att.get("content_type", "")
            url = att.get("url", "")
            if ct == "voice":
                # QQ 语音附件提供 WAV 直链和平台 ASR 结果
                voice_wav_url = att.get("voice_wav_url", "")
                voice_asr_text = att.get("asr_refer_text", "")
                if not voice_wav_url:
                    voice_wav_url = url  # 兜底用 url 字段
            elif ct.startswith("image/") and url:
                image_url = url

        # 下载图片到 MongoDB 持久存储
        local_media_url = None
        image_data = b""
        if image_url:
            try:
                from app.services.media_storage import save_image_from_url
                local_media_url = await save_image_from_url(image_url)
                print(f"{self._label} [图片已保存] {local_media_url}")
                # 下载原图用于尺寸预判
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(image_url)
                    if resp.status_code == 200:
                        image_data = resp.content
            except Exception as e:
                logger.error("%s 图片保存失败: %s", self._label, e)

        # 语音输入 → 文字
        if voice_wav_url or voice_asr_text:
            transcribed = await self._handle_voice_input(
                voice_wav_url, voice_asr_text
            )
            if transcribed:
                content = transcribed
                print(f"{self._label} [语音识别] {nickname}: {content}")

        # 图片输入 → Vision 描述（识别为表情包时转写为表情包格式，触发以图回图）
        if image_url and settings.vision_enabled:
            img_desc = await self._handle_image_input(image_url, image_data)
            if img_desc:
                is_likely_sticker = self._is_likely_sticker(image_data) if image_data else False
                if img_desc.startswith("[表情包]") or is_likely_sticker:
                    # 强制转写为表情包格式（尺寸预判或 Vision 识别命中）
                    clean_desc = img_desc.replace("[表情包]", "").replace("含义：", "").replace("含义:", "").strip()
                    prefix = f"{STICKER_TRANSCRIBE_PREFIX}，含义：{clean_desc}]"
                else:
                    prefix = f"{IMAGE_TRANSCRIBE_PREFIX}，内容：{img_desc}]"
                content = f"{prefix} {content}" if content else prefix
                print(f"{self._label} [图片识别] {img_desc[:60]}")

        # 图片兜底：vision 未启用/失败时，仍保留图片提示让 Bot 能回复
        if not content and image_url:
            content = f"{IMAGE_TRANSCRIBE_PREFIX}]"

        if not content:
            return

        print(f"{self._label} [收到] {nickname}: {content}")

        # 在分支前初始化 result，确保所有路径下变量已定义
        result = None

        if content.startswith("/") or content.startswith("\uff0f"):
            response_text = self._handle_command(content)
            image_tasks: list[ImageTask] = []
            conv_id = ""
        else:
            try:
                result = await self.engine.handle_message(
                    platform="qq",
                    platform_id=user_id,
                    text=content,
                    nickname=nickname,
                    media_url=local_media_url,
                    content_type="voice" if (voice_wav_url or voice_asr_text) else ("image" if image_url else "text"),
                    is_group=is_group,
                    group_id=group_id,
                )
                response_text = result.text
                image_tasks = result.image_tasks
                conv_id = result.conversation_id
            except Exception as e:
                logger.error(
                    "%s 对话处理失败: %s", self._label, e
                )
                response_text = "网络有点开小差了，你可以再发一次吗？"
                image_tasks = []
                conv_id = ""

        print(
            f"{self._label} [回复] {response_text[:80]}..."
        )

        # 语音触发：身份发语音直接触发；或动态窗口内（身份发过语音后的随机
        # 固定窗口期）文字消息也以语音回复，窗口按会话独立且不续期
        user_sent_voice = bool(voice_wav_url or voice_asr_text)
        in_voice_window = (
            settings.voice_dynamic_enabled
            and bool(conv_id)
            and voice_window.is_active(conv_id)
        )
        is_voice_reply = (
            settings.tts_enabled
            and bool(response_text)
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

        if not is_voice_reply:
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
                texts = [response_text] if response_text.strip() else []
            if not texts:
                # 整轮被清洗成空（模型只出了标签/表情）：不发文本，图片与表情包照常。
                # 空 content 会被 QQ 以 400/40034030 拒绝，且失败仅落日志、对方看到的是已读不回
                logger.info(
                    "%s 本轮无文本内容可发，跳过文本发送: user=%s",
                    self._label, user_id,
                )
            for i, text in enumerate(texts):
                if i > 0:
                    # 逐条间隔统一走节奏层：默认配置下与旧的
                    # min(len*per_char, max_delay) 逐字等价，开启 human 档才叠启动/昼夜
                    await asyncio.sleep(pacing_delay_seconds(text, settings))
                await self._send_reply(
                    user_id, text, msg_id, is_group, group_id
                )

        # 异步发送图片（fire-and-forget）
        if image_tasks and settings.imagegen_enabled:
            for task in image_tasks:
                spawn(
                    self._fire_and_forget_imagegen(
                        user_id, task, msg_id, is_group, group_id,
                        conv_id,
                    )
                )

        # 异步发送表情包（fire-and-forget，延迟于文本后模拟真人节奏）
        sticker_tasks = getattr(result, "sticker_tasks", None) if result else None
        if sticker_tasks:
            for stask in sticker_tasks:
                spawn(
                    self._fire_and_forget_sticker(
                        user_id, stask, msg_id, is_group, group_id, conv_id,
                    )
                )

        # 异步发送语音
        if is_voice_reply and result is not None:
            spawn(
                self._fire_and_forget_tts(
                    user_id, response_text, msg_id, is_group, group_id,
                    result.voice_id, result.voice_instruction,
                    result.voice_pitch_rate, result.voice_emotion_tag,
                    result.account_id,
                )
            )

    def _handle_command(self, content: str) -> str:
        """处理身份指令"""
        cmd = content.replace("\uff0f", "/").strip()
        command = cmd.split(maxsplit=1)[0].lower()

        if command in ("/help", "/帮助"):
            return (
                "爱易聊 使用帮助\n"
                "━━━━━━━━━━\n"
                "/help - 显示此帮助信息\n"
                "━━━━━━━━━━\n"
                "直接发送文字即可开始对话"
            )

        return (
            f"未知指令：{command}\n"
            "发送 /help 查看帮助"
        )

    async def _send_reply(
        self,
        user_id: str,
        content: str,
        msg_id: str,
        is_group: bool,
        group_id: str,
        is_wakeup: bool = False,
    ):
        """
        通过 REST API 发送回复消息。

        Args:
            is_wakeup: 是否使用互动召回能力（与 msg_id 互斥）。
                       设置为 True 时不传 msg_id，用于主动触达身份。
        """
        if not content.strip():
            # 最后一道闸：空 content 平台直接 400（40034030），失败只落日志等于已读不回
            logger.warning("%s 拒绝发送空内容: user=%s", self._label, user_id)
            return False
        try:
            token = await self.get_access_token()
            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            }

            base = (
                "https://sandbox.api.sgroup.qq.com"
                if self.bot_config.sandbox_mode
                else "https://api.sgroup.qq.com"
            )

            if is_group and group_id:
                url = f"{base}/v2/groups/{group_id}/messages"
            else:
                # `/v2/users/` 是 QQ 官方路径字面量，非本项目命名，禁止改名
                url = f"{base}/v2/users/{user_id}/messages"

            self._msg_seq += 1
            payload = {
                "content": content,
                "msg_type": 0,
                "msg_seq": self._msg_seq,
            }
            # 互动召回与 msg_id 互斥：is_wakeup 优先
            if is_wakeup:
                payload["is_wakeup"] = True
            elif msg_id:
                payload["msg_id"] = msg_id

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url, json=payload, headers=headers
                )
                if resp.status_code in (200, 201, 204):
                    self._last_send_error = ""
                    logger.info(
                        "%s 消息发送成功 -> %s (wakeup=%s)",
                        self._label,
                        user_id,
                        is_wakeup,
                    )
                    return True
                else:
                    logger.error(
                        "%s 消息发送失败: %d %s",
                        self._label,
                        resp.status_code,
                        resp.text,
                    )
                    # 保留平台真实错误码/消息，供主动消息失败记录诊断
                    self._last_send_error = self._describe_send_error(resp)
                    # 检测限流错误码，记录退避
                    self._check_rate_limit_error(resp, user_id)
                    return False
        except Exception as e:
            logger.error(
                "%s 发送消息异常: %s", self._label, e
            )
            self._last_send_error = f"发送异常: {e}"
            return False

    @staticmethod
    def _describe_send_error(resp: "httpx.Response") -> str:
        """从 QQ 失败响应中提取可读的错误详情（code + message）。"""
        try:
            data = resp.json()
            code = data.get("code", "")
            message = str(data.get("message", "")).strip()
            if code or message:
                return f"QQ错误 {code}: {message}".strip()
        except Exception:
            pass  # 响应体非 JSON 时回退到 HTTP 状态码
        return f"HTTP {resp.status_code}"

    @property
    def last_send_error(self) -> str:
        """最近一次发送失败的平台错误详情（发送成功后清空）。"""
        return self._last_send_error

    def _check_rate_limit_error(self, resp: "httpx.Response", user_id: str) -> None:
        """检测 QQ 限流错误，记录退避时间（1小时内不再向该身份发送主动消息）。"""
        try:
            error_data = resp.json()
            error_code = error_data.get("code", 0)
            error_message = error_data.get("message", "")
            if error_code == 40034122 or "召回消息已达区间上限" in error_message:
                expire_at = datetime.now(timezone.utc) + timedelta(hours=1)
                self._rate_limited_openids[user_id] = expire_at
                logger.warning(
                    "QQ 限流，身份 %s 退避至 %s", user_id, expire_at.isoformat()
                )
        except Exception:
            pass  # 响应体非 JSON 时忽略

    def _is_rate_limited(self, user_id: str) -> bool:
        """检查身份是否在限流退避期内。"""
        expire_time = self._rate_limited_openids.get(user_id)
        if expire_time is None:
            return False
        if datetime.now(timezone.utc) > expire_time:
            # 退避期已过，移除记录
            del self._rate_limited_openids[user_id]
            return False
        return True

    def can_send_proactive(self, user_id: str) -> bool:
        """主动消息发送前的轻量预检：处于限流退避期则不可发送。

        供调度器在调用 LLM 生成内容之前调用，避免为注定失败的发送浪费额度。
        """
        return not self._is_rate_limited(user_id)

    async def send_proactive(
        self,
        user_id: str,
        content: str,
        is_group: bool = False,
        group_id: str = "",
    ) -> bool:
        """
        主动发送消息给身份（使用互动召回 is_wakeup 能力）。

        用于定时关怀、主动触达等场景。不需要前置的身份消息。
        限制：身份对话后30天内，按周期下发（当天/1-3天/3-7天/7-30天各1条）。

        Args:
            user_id: 目标身份的 openid
            content: 消息文本内容
            is_group: 是否群聊（互动召回仅支持C2C，群聊使用普通主动消息）
            group_id: 群ID（仅群聊时需要）

        Returns:
            发送是否成功
        """
        # 限流退避检查：处于退避期内的身份跳过发送
        if self._is_rate_limited(user_id):
            logger.warning("身份 %s 处于限流退避期，跳过发送", user_id)
            self._last_send_error = (
                f"QQ 召回限流退避中，身份 {user_id} 本轮不可发送"
            )
            return False

        logger.info(
            "%s 主动消息 -> %s (group=%s)",
            self._label, user_id, is_group,
        )
        return await self._send_reply(
            user_id=user_id,
            content=content,
            msg_id="",
            is_group=is_group,
            group_id=group_id,
            is_wakeup=True,
        )

    # ── 多媒体输入处理 ──────────────────────────────

    async def _handle_voice_input(
        self, voice_wav_url: str, asr_refer_text: str
    ) -> str:
        """
        处理语音输入，返回识别文字。

        优先级：
        1. QQ 平台自带的 asr_refer_text（免费、无延迟）
        2. 下载 WAV 后调 dashscope ASR（需 ASR_ENABLED）
        3. 兜底提示文本
        """
        # 优先用 QQ 平台自带 ASR 结果
        if asr_refer_text and asr_refer_text.strip():
            logger.info(
                "%s QQ平台ASR: %s", self._label, asr_refer_text[:60]
            )
            return asr_refer_text.strip()

        # 降级：下载 WAV 调 dashscope ASR
        if voice_wav_url and settings.asr_enabled:
            try:
                from app.services.audio_utils import download_qq_media
                from app.services.asr_service import ASRService

                if self._asr is None:
                    self._asr = ASRService()
                wav_data = await download_qq_media(voice_wav_url)
                text = await self._asr.transcribe(wav_data, fmt="wav")
                if text:
                    return text
            except Exception as e:
                logger.error("%s dashscope ASR失败: %s", self._label, e)

        # 兜底：让 Bot 知道身份发了语音
        return "[身份发送了一条语音消息]"

    async def _handle_image_input(
        self, image_url: str, image_data: bytes = b""
    ) -> str:
        """调用 Vision 描述图片内容，支持表情包尺寸预判。"""
        try:
            from app.services.vision_service import get_vision_service

            is_likely_sticker = False
            if image_data:
                is_likely_sticker = self._is_likely_sticker(image_data)

            svc = get_vision_service()
            if is_likely_sticker:
                return await svc.describe_image_with_hint(
                    image_url, is_likely_sticker=True
                )
            return await svc.describe_image(image_url)
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

    # ── 多媒体输出（fire-and-forget）──────────────────

    async def _fire_and_forget_tts(
        self,
        user_id: str,
        text: str,
        msg_id: str,
        is_group: bool,
        group_id: str,
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
                    await self._send_reply(
                        user_id, text, msg_id, is_group, group_id
                    )
                    return

            # 2. TTS 合成 + 格式转换 + 发送
            from app.services.audio_utils import wav_to_silk
            from app.services.tts_service import TTSService

            if self._tts is None:
                self._tts = TTSService()

            audio_bytes = await self._tts.synthesize(
                text,
                voice_id=voice_id,
                instruction=voice_instruction,
                pitch_rate=voice_pitch_rate,
                emotion_tag=voice_emotion_tag,
            )

            # TTS 返回 mp3，需转为 silk 给 QQ
            silk_data = await wav_to_silk(audio_bytes)

            await self._send_media_reply(
                user_id, silk_data, file_type=3,
                msg_id=msg_id, is_group=is_group, group_id=group_id,
            )
            logger.info("%s TTS 语音已发送", self._label)

        except Exception as e:
            logger.error("%s TTS 发送失败，降级为文字: %s", self._label, e)
            try:
                await self._send_reply(
                    user_id, text, msg_id, is_group, group_id
                )
            except Exception as e2:
                logger.error("%s 文字兜底也失败: %s", self._label, e2)

    async def _fire_and_forget_imagegen(
        self,
        user_id: str,
        task: ImageTask,
        msg_id: str,
        is_group: bool,
        group_id: str,
        conv_id: str = "",
    ):
        """异步生成图片并发送（不阻塞文本回复），同时保存到 MongoDB。"""
        try:
            from app.services.imagegen_service import ImageGenService

            if self._imagegen is None:
                self._imagegen = ImageGenService()

            img_bytes = await self._imagegen.generate_for_task(task)

            # 保存原图到 MongoDB 并记录消息（供管理后台查看高清版）
            if conv_id:
                try:
                    from app.services.media_storage import save_image_from_bytes
                    from app.db.repositories.message_repo import MessageRepository

                    media_url = await save_image_from_bytes(img_bytes)
                    await MessageRepository().create(
                        conversation_id=conv_id,
                        role="assistant",
                        content="(AI生成了一张图片)",
                        content_type="image",
                        media_url=media_url,
                    )
                except Exception as e:
                    logger.error("%s 图片保存到DB失败: %s", self._label, e)

            # 压缩后发送到 QQ（QQ 上传接口有大小限制）
            from app.services.media_storage import compress_for_qq
            send_data = await compress_for_qq(img_bytes)
            logger.info(
                "%s 图片压缩: %d -> %d bytes",
                self._label, len(img_bytes), len(send_data),
            )

            await self._send_media_reply(
                user_id, send_data, file_type=1,
                msg_id=msg_id, is_group=is_group, group_id=group_id,
            )
            logger.info(
                "%s 图片已发送, prompt=%s", self._label, task.prompt[:40]
            )
        except Exception as e:
            logger.error("%s 图片生成/发送失败: %s", self._label, e)

    async def _fire_and_forget_sticker(
        self,
        user_id: str,
        task: StickerTask,
        msg_id: str,
        is_group: bool,
        group_id: str,
        conv_id: str = "",
    ) -> None:
        """异步从媒体库读取表情包字节并发送（短延迟模拟真人节奏）。

        GIF 未超 QQ 上传限制时跳过压缩，避免丢失动画。
        """
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))

            from app.db.repositories.media_repo import MediaRepository
            from app.services.media_storage import QQ_UPLOAD_MAX, compress_for_qq

            media_id = task.media_url.rsplit("/", 1)[-1]
            media_doc = await MediaRepository().find_by_id(media_id)
            if not media_doc or not media_doc.get("data"):
                logger.warning(
                    "%s 表情包媒体不存在: %s", self._label, task.media_url
                )
                return
            data: bytes = media_doc["data"]

            # GIF 保护：不超限直发保留动画，超限才走压缩降级为静图
            if (
                media_doc.get("content_type") == "image/gif"
                and len(data) <= QQ_UPLOAD_MAX
            ):
                send_data = data
            else:
                send_data = await compress_for_qq(
                    data, media_doc.get("content_type", "image/jpeg")
                )

            await self._send_media_reply(
                user_id, send_data, file_type=1,
                msg_id=msg_id, is_group=is_group, group_id=group_id,
            )
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
                    logger.error("%s 表情包消息保存失败: %s", self._label, e)
        except Exception as e:
            logger.error("%s 表情包发送失败: %s", self._label, e)

    async def _send_media_reply(
        self,
        user_id: str,
        file_data: bytes,
        file_type: int,
        msg_id: str,
        is_group: bool,
        group_id: str,
    ):
        """
        上传富媒体文件到 QQ 并发送。

        Args:
            file_data: 文件二进制数据
            file_type: 1=图片, 3=语音
        """
        token = await self.get_access_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }

        base = (
            "https://sandbox.api.sgroup.qq.com"
            if self.bot_config.sandbox_mode
            else "https://api.sgroup.qq.com"
        )

        # Step 1: 上传文件
        if is_group and group_id:
            upload_url = f"{base}/v2/groups/{group_id}/files"
        else:
            upload_url = f"{base}/v2/users/{user_id}/files"

        file_b64 = base64.b64encode(file_data).decode()
        upload_payload = {
            "file_type": file_type,
            "file_data": file_b64,
            "srv_send_msg": False,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                upload_url, json=upload_payload, headers=headers
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    "%s 文件上传失败: %d %s",
                    self._label, resp.status_code, resp.text,
                )
                return
            upload_data = resp.json()
            file_info = upload_data.get("file_info", "")
            if not file_info:
                logger.error(
                    "%s 上传返回无 file_info: %s",
                    self._label, upload_data,
                )
                return

        # Step 2: 发送富媒体消息
        if is_group and group_id:
            msg_url = f"{base}/v2/groups/{group_id}/messages"
        else:
            msg_url = f"{base}/v2/users/{user_id}/messages"

        self._msg_seq += 1
        msg_payload = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": self._msg_seq,
        }
        if msg_id:
            msg_payload["msg_id"] = msg_id

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                msg_url, json=msg_payload, headers=headers
            )
            if resp.status_code not in (200, 201, 204):
                logger.error(
                    "%s 富媒体消息发送失败: %d %s",
                    self._label, resp.status_code, resp.text,
                )

    async def run(self):
        """启动 WebSocket 客户端主循环（含指数退避重连）"""
        self._running = True
        self._reconnect_delay = 5  # 初始重连间隔（秒）
        MAX_RECONNECT_DELAY = 120  # 最大重连间隔

        while self._running:
            try:
                ws_url = await self.get_gateway_url()
                logger.info(
                    "%s 连接 WebSocket: %s",
                    self._label,
                    ws_url,
                )

                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    heartbeat_task = None
                    # 连接成功，重置退避间隔
                    self._reconnect_delay = 5

                    async for raw_msg in ws:
                        try:
                            data = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        op = data.get("op")
                        seq = data.get("s")
                        if seq is not None:
                            self._sequence = seq

                        if op == OP_HELLO:
                            interval = data.get("d", {}).get(
                                "heartbeat_interval", 45000
                            )
                            self._heartbeat_interval = (
                                interval / 1000
                            )
                            await self._send_identify()
                            heartbeat_task = asyncio.create_task(
                                self._heartbeat_loop()
                            )

                        elif op == OP_DISPATCH:
                            spawn(
                                self._handle_dispatch(data)
                            )

                        elif op == OP_HEARTBEAT_ACK:
                            pass

                        elif op == OP_RECONNECT:
                            logger.warning(
                                "%s 服务端要求重连",
                                self._label,
                            )
                            break

                        elif op == OP_INVALID_SESSION:
                            logger.warning(
                                "%s 无效 session",
                                self._label,
                            )
                            break

                    if heartbeat_task:
                        heartbeat_task.cancel()

            except websockets.ConnectionClosed as e:
                logger.warning(
                    "%s 连接关闭: %s, %d秒后重连...",
                    self._label,
                    e,
                    self._reconnect_delay,
                )
            except Exception as e:
                logger.error(
                    "%s 异常: %s, %d秒后重连...",
                    self._label,
                    e,
                    self._reconnect_delay,
                )

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                # 指数退避：每次失败间隔翻倍，上限 MAX_RECONNECT_DELAY
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    MAX_RECONNECT_DELAY,
                )

    def stop(self):
        """停止客户端"""
        self._running = False

    async def close(self):
        """安全关闭客户端"""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass


class BotManager:
    """Bot 生命周期管理器，支持热加载 bots.yaml 配置。"""

    def __init__(self, config_path: str = "bots.yaml", poll_interval: int = 30):
        self._config_path = config_path
        self._poll_interval = poll_interval
        # registry: config_id -> (config, client, task)
        self._registry: dict[str, tuple[BotConfig, QQWebSocketClient, asyncio.Task]] = {}
        self._watch_task: asyncio.Task | None = None
        self._profile_svc: ProfileService | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self, initial_configs: list[BotConfig]):
        """启动初始 Bot 并开始配置监控循环"""
        from app.profiles.service import ProfileService
        self._profile_svc = ProfileService()

        for cfg in initial_configs:
            await self._start_bot(cfg)

        self._watch_task = asyncio.create_task(
            self._config_watch_loop()
        )
        logger.info(
            "BotManager started: %d bot(s), polling every %ds",
            len(self._registry),
            self._poll_interval,
        )

    async def _start_bot(self, config: BotConfig) -> bool:
        """启动单个 Bot（Bot 不再绑定固定角色，无需验证 profile）。"""
        client = QQWebSocketClient(config)
        task = asyncio.create_task(self._supervised_run(client))
        self._registry[config.id] = (config, client, task)
        logger.info(
            "[%s] Bot 已启动",
            config.name or config.id,
        )
        return True

    async def _stop_bot(self, config_id: str):
        """安全停止单个 Bot"""
        entry = self._registry.pop(config_id, None)
        if entry is None:
            return
        config, client, task = entry
        await client.close()
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        logger.info("[%s] Bot 已停止", config.name or config_id)

    async def _supervised_run(self, client: QQWebSocketClient):
        """包装 Bot 运行，捕获未预期异常防止级联崩溃"""
        try:
            await client.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "[%s] Bot 意外终止: %s",
                client.bot_config.name or client.bot_config.id,
                e,
            )

    async def _config_watch_loop(self):
        """定期轮询 MongoDB，检测 Bot 配置变更并应用（仅启动被活跃服务引用的 Bot）"""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(self._poll_interval)
            try:
                new_configs = await async_load_bots_config()
            except Exception as e:
                logger.warning(
                    "Bot 配置加载失败，跳过本轮: %s", e
                )
                continue

            # 查询被活跃服务引用的 Bot ID，过滤出需要运行的 Bot
            try:
                from app.db.repositories.service_repo import ServiceRepository
                active_bot_ids = await ServiceRepository().list_active_bot_ids()
            except Exception as e:
                logger.warning("活跃服务查询失败，跳过本轮: %s", e)
                continue

            # 只保留被活跃服务引用的 Bot 配置
            new_configs = [c for c in new_configs if c.id in active_bot_ids]

            # 同时刷新 settings
            try:
                from app.config import settings
                changed = await settings.reload()
                if changed:
                    # Provider 由 model_configs 分组与各功能的 config_id 引用决定
                    if changed & {"model_configs"} or any(
                        f.endswith(("_config_id", "_timeout_seconds"))
                        for f in changed
                    ):
                        from app.llm.provider_factory import reset_provider
                        reset_provider()
                        logger.info("LLM provider 已重建")
            except Exception as e:
                logger.warning("Settings reload 失败: %s", e)

            await self._apply_diff(new_configs)

    async def _apply_diff(self, new_configs: list[BotConfig]):
        """对比新旧配置，执行增删改（只处理QQ平台Bot）"""
        # 只处理QQ平台的Bot
        qq_configs = [c for c in new_configs if c.platform == "qq"]
        new_map = {c.id: c for c in qq_configs}
        current_ids = set(self._registry.keys())
        new_ids = set(new_map.keys())

        # 删除的 Bot
        for config_id in current_ids - new_ids:
            logger.info("[%s] 配置已移除，正在停止...", config_id)
            await self._stop_bot(config_id)

        # 新增的 Bot
        for config_id in new_ids - current_ids:
            logger.info("[%s] 检测到新配置，正在启动...", config_id)
            await self._start_bot(new_map[config_id])

        # 配置变更的 Bot
        for config_id in current_ids & new_ids:
            old_config = self._registry[config_id][0]
            new_config = new_map[config_id]
            if old_config != new_config:
                logger.info(
                    "[%s] 配置已变更，正在重启...", config_id
                )
                await self._stop_bot(config_id)
                await self._start_bot(new_config)

    async def wait(self):
        """阻塞直到收到 shutdown 信号"""
        await self._shutdown_event.wait()

    def get_client(self, bot_id: str) -> QQWebSocketClient | None:
        """根据 bot_id 获取对应的 WebSocket 客户端实例（供主动消息调度器使用）。"""
        entry = self._registry.get(bot_id)
        if entry is None:
            return None
        return entry[1]

    def list_clients(self) -> list[QQWebSocketClient]:
        """获取所有活跃的客户端实例列表。"""
        return [entry[1] for entry in self._registry.values()]

    async def shutdown(self):
        """停止所有 Bot 和监控循环"""
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
        logger.info("BotManager shutdown complete")


async def main():
    """多 Bot WebSocket 客户端入口"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(
            encoding="utf-8", errors="replace"
        )
        sys.stderr.reconfigure(
            encoding="utf-8", errors="replace"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 初始化数据库
    from app.db.mongo import connect_db
    await connect_db()

    # 从 MongoDB 加载 Bot 配置
    from app.bot_config import async_load_bots_config

    bot_configs = await async_load_bots_config()

    print("=" * 50)
    print("  爱易聊 AIYIchat - QQ Bot 多角色服务")
    print("=" * 50)
    try:
        from app.llm.model_registry import resolve_model_config
        _chat_model = resolve_model_config("chat").model
    except Exception:
        _chat_model = "(未配置)"
    print(f"  对话模型:  {_chat_model}")
    print(f"  Bot数量:   {len(bot_configs)} 个")
    print("  配置来源:  MongoDB")
    print("-" * 50)
    for cfg in bot_configs:
        mode = "沙箱" if cfg.sandbox_mode else "正式"
        print(
            f"  {cfg.name or cfg.id}: "
            f"app={cfg.app_id}, "
            f"模式={mode}"
        )
    print("=" * 50)
    print()

    manager = BotManager()
    await manager.start(bot_configs)

    try:
        await manager.wait()
    except KeyboardInterrupt:
        print("\n[退出] 正在关闭所有 Bot...")
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
