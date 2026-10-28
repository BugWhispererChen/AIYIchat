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
import hashlib
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
import websockets

from app.bot_config import BotConfig, async_load_bots_config
from app.utils.tasks import spawn
from app.config import settings
from app.conversation.engine import ConversationEngine, StreamEvent
from app.conversation.human_rhythm import pacing_delay_seconds
from app.conversation.image_task import ImageTask
from app.conversation.message_splitter import limit_messages
from app.conversation.sticker_task import (
    IMAGE_TRANSCRIBE_PREFIX,
    STICKER_TRANSCRIBE_PREFIX,
    StickerTask,
)
from app.services.voice_window import voice_window
from app.utils.redaction import mask_secret

if TYPE_CHECKING:
    from app.services.asr_service import ASRService
    from app.services.imagegen_service import ImageGenService
    from app.services.tts_service import TTSService
    from app.profiles.service import ProfileService

logger = logging.getLogger(__name__)

# QQ Bot API v2 官方基址：正式环境统一走 api.bot.qq.com，沙箱保持独立
API_BASE_URL_PROD = "https://api.bot.qq.com"
API_BASE_URL_SANDBOX = "https://sandbox.api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_SANDBOX = f"{API_BASE_URL_SANDBOX}/gateway"
GATEWAY_URL_PROD = f"{API_BASE_URL_PROD}/gateway"

# WebSocket op codes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Intents：主动授权互动事件在独立的 INTERACTION 分组中
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26

# 稳定限流信号：HTTP 429 或 QQ 稳定业务码；不得匹配可变的错误文案。
# 50002 为流式消息限流；40034122 为互动召回周期上限（旧 code 过渡兼容）。
RATE_LIMIT_ERROR_CODES = frozenset({"40034122", "50002"})

# 富媒体阈值与上限（官方 SDK：超过 5MB 走分片，视频/文件硬上限 100MB）
MEDIA_CHUNK_THRESHOLD = 5 * 1024 * 1024
MEDIA_MAX_SIZE = 100 * 1024 * 1024
MD5_10M_SIZE = 10_002_432

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


@dataclass(frozen=True)
class QQResponseError:
    """QQ HTTP、网络、JSON 或业务错误的统一安全描述。"""

    status_code: int = 0
    code: str = ""
    message: str = ""
    trace_id: str = ""
    category: str = "http"

    def describe(self) -> str:
        """只组合白名单字段，不拼接完整响应体或鉴权信息。"""
        parts: list[str] = []
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.code:
            parts.append(f"QQ错误 {self.code}")
        if not parts:
            parts.append({
                "network": "网络错误",
                "json": "JSON解析错误",
                "business": "QQ业务错误",
            }.get(self.category, "QQ请求错误"))
        if self.message:
            parts.append(self.message)
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        return ": ".join(parts)


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
        # 主动消息平台授权：(c2c/group, openid) → 明确允许/拒绝；缺省一律拒绝
        self._proactive_authorizations: dict[tuple[str, str], bool] = {}
        # 多媒体服务（延迟导入，按需实例化）
        self._asr: ASRService | None = None
        self._vision = None
        self._tts: TTSService | None = None
        self._imagegen: ImageGenService | None = None
        # 最近发送的平台消息，供两分钟撤回与互动反馈回查。
        # 撤回必须按会话目标隔离，避免一个聊天里的 /recall 撤掉别人的消息。
        self._last_sent_message_id: str = ""
        self._recent_outbound: collections.OrderedDict[str, float] = (
            collections.OrderedDict()
        )
        self._last_sent_by_target: dict[str, tuple[str, float]] = {}
        # 全量群上下文单独去重，不能占用 @ 消息的回复去重键
        self._processed_group_context_ids: collections.OrderedDict[str, float] = (
            collections.OrderedDict()
        )

    @staticmethod
    def _parse_response(
        resp: httpx.Response,
    ) -> tuple[dict[str, object], QQResponseError | None]:
        """统一解析 HTTP 状态、JSON 及新旧 QQ 业务错误字段。"""
        status_code = resp.status_code
        if status_code == 204:
            return {}, None
        try:
            raw_data = resp.json()
        except (TypeError, ValueError):
            error = QQResponseError(
                status_code=status_code,
                category="json" if 200 <= status_code < 300 else "http",
                message="响应不是有效 JSON" if 200 <= status_code < 300 else "",
            )
            return {}, error
        if not isinstance(raw_data, dict):
            return {}, QQResponseError(
                status_code=status_code,
                category="json",
                message="响应 JSON 不是对象",
            )

        data: dict[str, object] = raw_data
        raw_code = data.get(
            "err_code", data.get("code", data.get("errcode", ""))
        )
        code = "" if raw_code in (None, "", 0, "0") else str(raw_code)
        raw_message = data.get("message") or data.get("errmsg") or ""
        message = raw_message.strip() if isinstance(raw_message, str) else ""
        raw_trace_id = data.get("trace_id", "")
        trace_id = str(raw_trace_id).strip() if raw_trace_id else ""
        if not 200 <= status_code < 300 or code:
            return data, QQResponseError(
                status_code=status_code,
                code=code,
                message=message,
                trace_id=trace_id,
                category="business" if code else "http",
            )
        return data, None

    def _safe_error_detail(self, error: QQResponseError) -> str:
        """脱敏并限制错误长度，避免令牌或完整鉴权响应进入日志。"""
        detail = error.describe()
        for secret in (self._access_token, self.bot_config.app_secret):
            if secret:
                detail = detail.replace(secret, "***")
        detail = re.sub(
            r"(?i)\b(QQBot|Bearer)\s+[^\s,;]+", r"\1 ***", detail
        )
        detail = re.sub(
            r"(?i)(access_token|refresh_token|clientSecret|appSecret)"
            r"(\"?\s*[:=]\s*\"?)[^\",;\s}]+",
            r"\1\2***",
            detail,
        )
        return detail[:300]

    @property
    def _api_base_url(self) -> str:
        """按环境返回唯一 REST 基址，避免各发送路径硬编码漂移。"""
        return (
            API_BASE_URL_SANDBOX
            if self.bot_config.sandbox_mode
            else API_BASE_URL_PROD
        )

    async def get_access_token(self) -> str:
        """获取或刷新 access_token。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(TOKEN_URL, json={
                    "appId": self.bot_config.app_id,
                    "clientSecret": self.bot_config.app_secret,
                })
        except httpx.HTTPError as exc:
            network_error = QQResponseError(category="network", message=str(exc))
            raise RuntimeError(
                f"{self._label} 获取 access_token 失败: "
                f"{self._safe_error_detail(network_error)}"
            ) from exc

        data, error = self._parse_response(resp)
        # 该响应含 access_token 与 refresh_token，原文一律不许落日志：只记字段名
        logger.info("%s Token 响应字段: %s", self._label, list(data.keys()))
        if error is not None:
            raise RuntimeError(
                f"{self._label} 获取 access_token 失败: "
                f"{self._safe_error_detail(error)}"
            )
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError(f"{self._label} 获取 access_token 失败: 响应缺少令牌")

        self._access_token = access_token
        raw_expires_in = data.get("expires_in", 7200)
        try:
            expires_in = (
                int(raw_expires_in)
                if isinstance(raw_expires_in, (str, int, float))
                else 7200
            )
        except ValueError:
            expires_in = 7200
        self._token_expires_at = now + expires_in
        logger.info(
            "%s access_token 有效期 %d 秒",
            self._label,
            expires_in,
        )
        return self._access_token

    async def get_gateway_url(self) -> str:
        """获取 WebSocket 网关地址。"""
        token = await self.get_access_token()
        gateway_url = (
            GATEWAY_URL_SANDBOX
            if self.bot_config.sandbox_mode
            else GATEWAY_URL_PROD
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(gateway_url, headers={
                    "Authorization": f"QQBot {token}",
                })
        except httpx.HTTPError as exc:
            network_error = QQResponseError(category="network", message=str(exc))
            raise RuntimeError(
                f"{self._label} 获取网关地址失败: "
                f"{self._safe_error_detail(network_error)}"
            ) from exc

        data, error = self._parse_response(resp)
        if error is not None:
            raise RuntimeError(
                f"{self._label} 获取网关地址失败: "
                f"{self._safe_error_detail(error)}"
            )
        ws_url = data.get("url", "")
        if not isinstance(ws_url, str) or not ws_url:
            raise RuntimeError(f"{self._label} 获取网关地址失败: 响应缺少 url")
        return ws_url

    async def _send_identify(self):
        """发送鉴权信息"""
        token = await self.get_access_token()
        identify = {
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": INTENT_GROUP_AND_C2C | INTENT_INTERACTION,
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
        elif event_type in (
            "C2C_MSG_RECEIVE",
            "C2C_MSG_REJECT",
            "GROUP_MSG_RECEIVE",
            "GROUP_MSG_REJECT",
        ):
            await self._handle_proactive_switch_event(event_type, event_data)
        elif event_type == "GROUP_MESSAGE_CREATE":
            if settings.qq_full_group_context_enabled:
                await self._handle_group_context_event(event_data)
        elif event_type == "INTERACTION_CREATE":
            await self._handle_interaction_event(event_data)
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

    async def _store_proactive_authorization(
        self,
        target_type: str,
        target_id: str,
        allowed: bool,
        event_type: str,
    ) -> None:
        """缓存并持久化平台授权；数据库异常不改变平台刚刚下发的事实。"""
        if not target_id:
            logger.warning("%s 忽略缺少 openid 的授权事件: %s", self._label, event_type)
            return
        self._proactive_authorizations[(target_type, target_id)] = allowed
        try:
            from app.db.repositories.qq_proactive_auth_repo import (
                QQProactiveAuthRepository,
            )

            await QQProactiveAuthRepository().upsert(
                bot_id=self.bot_config.id,
                target_type=target_type,
                target_id=target_id,
                allowed=allowed,
                event_type=event_type,
            )
        except Exception as exc:
            logger.error(
                "%s 主动消息授权持久化失败: type=%s allowed=%s error=%s",
                self._label, target_type, allowed, exc,
            )
        logger.info(
            "%s 主动消息授权更新: type=%s allowed=%s",
            self._label, target_type, allowed,
        )

    async def _restore_proactive_authorizations(self) -> None:
        """启动时恢复该 Bot 的 C2C/群授权，未知目标仍保持拒绝。"""
        from app.db.repositories.qq_proactive_auth_repo import (
            QQProactiveAuthRepository,
        )

        docs = await QQProactiveAuthRepository().list_by_bot(self.bot_config.id)
        restored: dict[tuple[str, str], bool] = {}
        for doc in docs:
            target_type = doc.get("target_type")
            target_id = doc.get("target_id")
            allowed = doc.get("allowed")
            if (
                target_type in ("c2c", "group")
                and isinstance(target_id, str)
                and target_id
                and isinstance(allowed, bool)
            ):
                restored[(target_type, target_id)] = allowed
        self._proactive_authorizations = restored
        logger.info("%s 已恢复 %d 条主动消息授权", self._label, len(restored))

    async def _handle_proactive_switch_event(
        self, event_type: str, data: dict
    ) -> None:
        """处理资料页主动消息开关的官方 RECEIVE/REJECT 事件。"""
        is_group = event_type.startswith("GROUP_")
        target_type = "group" if is_group else "c2c"
        target_id = data.get("group_openid" if is_group else "openid", "")
        await self._store_proactive_authorization(
            target_type=target_type,
            target_id=target_id if isinstance(target_id, str) else "",
            allowed=event_type.endswith("_RECEIVE"),
            event_type=event_type,
        )

    async def _handle_interaction_event(self, data: dict) -> None:
        """按互动类型分发主动授权、按钮指令与 LIKE/UNLIKE 反馈。"""
        interaction_type = data.get("type", 0)
        nested = data.get("data", {})
        if not isinstance(nested, dict):
            nested = {}
        resolved = nested.get("resolved", {})
        if not isinstance(resolved, dict):
            resolved = {}

        if interaction_type in (18, "18", 19, "19"):
            await self._handle_interaction_authorization(data, resolved)
            return

        interaction_id = str(data.get("id", ""))
        if interaction_type in (11, "11", 12, "12"):
            # 按钮/菜单类互动必须回执，否则客户端一直转圈。
            if interaction_id:
                await self._ack_interaction(interaction_id)
            button_data = str(resolved.get("button_data", "")).strip()
            if button_data.startswith("/"):
                await self._run_interaction_command(data, button_data)
            return

        feedback_opt = str(
            resolved.get("feedback_opt", nested.get("feedback_opt", ""))
        ).upper()
        if feedback_opt in ("LIKE", "UNLIKE"):
            await self._record_interaction_feedback(data, resolved, feedback_opt)

    async def _handle_interaction_authorization(
        self, data: dict, resolved: dict
    ) -> None:
        """处理 INTERACTION_CREATE 的 type=18/19 主动消息授权事件。"""
        interaction_type = data.get("type", 0)
        authorize_data = resolved.get("authorize_data", {})
        if not isinstance(authorize_data, dict):
            authorize_data = {}
        scope = authorize_data.get("scope", "")
        if interaction_type in (18, "18") and scope in ("", "c2c_push"):
            target_type = "c2c"
            target_id = data.get("user_openid", "")
        elif interaction_type in (19, "19") and scope in ("", "group_push"):
            target_type = "group"
            target_id = data.get("group_openid", "")
        else:
            return
        await self._store_proactive_authorization(
            target_type=target_type,
            target_id=target_id if isinstance(target_id, str) else "",
            allowed=True,
            event_type=f"INTERACTION_CREATE:{interaction_type}",
        )

    async def _ack_interaction(self, interaction_id: str) -> None:
        """按钮类互动回执；失败只告警，不影响消息主链路。"""
        try:
            token = await self.get_access_token()
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.put(
                    f"{self._api_base_url}/interactions/{interaction_id}",
                    json={"code": 0},
                    headers={"Authorization": f"QQBot {token}"},
                )
            _data, error = self._parse_response(resp)
            if error is not None:
                logger.warning(
                    "%s 互动回执失败: %s",
                    self._label, self._safe_error_detail(error),
                )
        except Exception as exc:
            logger.warning("%s 互动回执异常: %s", self._label, exc)

    async def _run_interaction_command(self, data: dict, command: str) -> None:
        """执行键盘按钮命令；普通文本聊天仍需回调消息事件。"""
        user_id = str(data.get("user_openid", ""))
        group_id = str(data.get("group_openid", ""))
        is_group = bool(group_id)
        if not user_id and is_group:
            user_id = str(data.get("group_member_openid", ""))
        if not user_id:
            return
        response = self._handle_command(command)
        await self._send_reply(
            user_id,
            response,
            "",
            is_group,
            group_id,
            conversation_id="",
            attach_keyboard=command.lower() in ("/help", "/帮助", "/menu", "/菜单"),
        )

    async def _record_interaction_feedback(
        self, data: dict, resolved: dict, feedback_opt: str
    ) -> None:
        """把平台 LIKE/UNLIKE 幂等写入现有离线克隆评审队列。"""
        event_id = str(data.get("id", ""))
        platform_message_id = str(
            resolved.get("message_id", data.get("message_id", ""))
        )
        if not event_id or not platform_message_id:
            logger.warning("%s 反馈事件缺少 event_id 或 message_id", self._label)
            return
        try:
            from app.db.mongo import get_database

            binding = await get_database().qq_message_bindings.find_one({
                "bot_id": self.bot_config.id,
                "platform_message_id": platform_message_id,
            })
            if not binding:
                logger.warning(
                    "%s 反馈消息未找到会话映射: %s",
                    self._label, platform_message_id,
                )
                return
            conversation_id = str(binding.get("conversation_id", ""))
            if not conversation_id:
                return
            from app.db.repositories.clone_repo import CloneFeedbackRepository
            from app.db.repositories.conversation_repo import ConversationRepository
            from app.db.repositories.message_repo import MessageRepository
            from app.db.repositories.profile_repo import ProfileRepository
            from app.db.repositories.service_repo import ServiceRepository
            from app.models.clone import CloneFeedbackInDB

            conversation = await ConversationRepository().find_by_id(conversation_id)
            if conversation is None:
                return
            service = await ServiceRepository().find_by_id(conversation.service_id)
            if service is None:
                return
            profile = await ProfileRepository().find_by_id(service.profile_id)
            recent = await MessageRepository().get_recent(conversation_id, 10)
            assistant = next(
                (m for m in reversed(recent) if m.role == "assistant"), None
            )
            if assistant is None or profile is None:
                return
            feedback = CloneFeedbackInDB(
                account_id=service.account_id,
                service_id=service.id,
                conversation_id=conversation_id,
                message_id=assistant.id,
                profile_id=profile.id,
                clone_version=getattr(profile, "clone_version", 0),
                rating="like" if feedback_opt == "LIKE" else "dislike",
                source="qq",
                platform_event_id=f"{self.bot_config.id}:{event_id}",
                platform_message_id=platform_message_id,
            )
            await CloneFeedbackRepository().upsert_platform_event(feedback)
            logger.info(
                "%s 原生反馈已入库: %s message=%s",
                self._label, feedback_opt, platform_message_id,
            )
        except Exception as exc:
            logger.error("%s 原生反馈写入失败: %s", self._label, exc)

    async def _handle_group_context_event(self, data: dict) -> None:
        """全量群消息仅补充上下文，默认不回复、不触发 LLM。"""
        msg_id = str(data.get("id", ""))
        content = str(data.get("content", ""))
        if not content.strip() or "<@!" in content:
            # 带 @ 的消息由 GROUP_AT_MESSAGE_CREATE 处理，避免同一句入历史两次
            return
        if msg_id:
            if msg_id in self._processed_group_context_ids:
                return
            self._processed_group_context_ids[msg_id] = time.time()
            while len(self._processed_group_context_ids) > self._dedup_max_size:
                self._processed_group_context_ids.popitem(last=False)
        author = data.get("author", {})
        if author.get("bot"):
            return
        user_id = str(
            author.get("member_openid", author.get("user_openid", ""))
        )
        content = _QQ_FACE_RE.sub(_decode_qq_face, content).strip()
        if not user_id or not content:
            return
        try:
            await self.engine.record_context_message(
                platform="qq",
                platform_id=user_id,
                text=content,
                nickname=str(author.get("username", "QQ身份")),
            )
        except Exception as exc:
            logger.warning("%s 群上下文写入失败: %s", self._label, exc)

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
                # 掩码输出：/api/media/<id> 的 id 是能力令牌，拿到完整 URL 就能读原图
                print(
                    f"{self._label} [图片已保存] "
                    f"/api/media/{mask_secret(local_media_url.rsplit('/', 1)[-1])}"
                )
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
        is_command = content.startswith("/") or content.startswith("\uff0f")
        command = content.replace("\uff0f", "/").split(maxsplit=1)[0].lower()

        if is_command:
            if command in ("/recall", "/撤回"):
                recalled = await self.recall_last_message(
                    user_id, is_group=is_group, group_id=group_id
                )
                response_text = (
                    "已撤回最近一条消息。"
                    if recalled
                    else "没有可撤回的消息，或已超过两分钟。"
                )
            else:
                response_text = self._handle_command(content)
            image_tasks: list[ImageTask] = []
            conv_id = ""
        else:
            user_sent_voice = bool(voice_wav_url or voice_asr_text)
            if not is_group:
                spawn(self._send_input_notify(user_id, msg_id))
            use_stream = (
                settings.qq_stream_enabled
                and not is_group
                and not user_sent_voice
            )
            if use_stream and settings.tts_enabled:
                # 仅当该会话确实处于语音动态窗口时才放弃流式，避免整类
                # “开启 TTS 的部署”永久用不上 C2C 流式。
                conversation_hint = await self._latest_conversation_id(user_id)
                if (
                    conversation_hint
                    and voice_window.is_active(conversation_hint)
                ):
                    use_stream = False
            if use_stream:
                await self._handle_stream_turn(
                    user_id=user_id,
                    content=content,
                    nickname=nickname,
                    msg_id=msg_id,
                    local_media_url=local_media_url or "",
                    content_type=(
                        "voice" if user_sent_voice
                        else ("image" if image_url else "text")
                    ),
                )
                return
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

        # 语音触发：对方发语音直接触发；或动态窗口内（对方发过语音后的随机
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
            # 仅对方发语音触发时开启窗口（固定窗口，窗口内不续期）
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
                    user_id,
                    text,
                    msg_id,
                    is_group,
                    group_id,
                    use_markdown=settings.qq_markdown_enabled,
                    attach_keyboard=(
                        settings.qq_keyboard_enabled
                        and is_command
                        and command in ("/help", "/帮助", "/menu", "/菜单")
                        and i == len(texts) - 1
                    ),
                    conversation_id=conv_id,
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

        if command in ("/help", "/帮助", "/menu", "/菜单"):
            return (
                "爱易聊 使用帮助\n"
                "━━━━━━━━━━\n"
                "/help - 显示帮助与快捷指令\n"
                "/status - 查看当前对话状态\n"
                "/recall - 撤回两分钟内最近一条消息\n"
                "━━━━━━━━━━\n"
                "直接发送文字即可开始对话"
            )

        if command == "/status":
            return "当前对话服务正常，直接发送消息即可继续。"

        if command in ("/recall", "/撤回"):
            return "正在尝试撤回最近一条消息。"

        return (
            f"未知指令：{command}\n"
            "发送 /help 查看帮助"
        )

    async def _send_input_notify(self, user_id: str, msg_id: str) -> None:
        """发送 C2C 原生输入状态；失败不阻塞本轮回复。"""
        try:
            token = await self.get_access_token()
            self._msg_seq += 1
            payload: dict[str, Any] = {
                "msg_type": 6,
                "input_notify": {"input_type": 1, "input_second": 60},
                "msg_seq": self._msg_seq,
            }
            if msg_id:
                payload["msg_id"] = msg_id
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self._api_base_url}/v2/users/{user_id}/messages",
                    json=payload,
                    headers={"Authorization": f"QQBot {token}"},
                )
            _data, error = self._parse_response(resp)
            if error is not None:
                logger.debug(
                    "%s 输入状态发送失败: %s",
                    self._label, self._safe_error_detail(error),
                )
        except Exception as exc:
            logger.debug("%s 输入状态异常: %s", self._label, exc)

    @staticmethod
    def _target_key(user_id: str, is_group: bool, group_id: str) -> str:
        """撤回记录按会话目标隔离，群聊与私聊互不影响。"""
        return f"group:{group_id}" if is_group and group_id else f"user:{user_id}"

    def _remember_outbound(
        self,
        message_id: str,
        user_id: str,
        is_group: bool,
        group_id: str,
    ) -> None:
        """记录最近一条已发送消息及其会话目标。"""
        if not message_id:
            return
        now = time.time()
        self._last_sent_message_id = message_id
        self._recent_outbound[message_id] = now
        self._last_sent_by_target[
            self._target_key(user_id, is_group, group_id)
        ] = (message_id, now)
        while len(self._recent_outbound) > 100:
            self._recent_outbound.popitem(last=False)

    async def recall_last_message(
        self,
        user_id: str,
        is_group: bool = False,
        group_id: str = "",
        max_age_seconds: int = 120,
    ) -> bool:
        """撤回当前会话最近一条消息；超过两分钟边界直接拒绝。"""
        target_key = self._target_key(user_id, is_group, group_id)
        message_id, sent_at = self._last_sent_by_target.get(
            target_key, ("", 0.0)
        )
        if not message_id or not sent_at:
            return False
        if time.time() - sent_at > max_age_seconds:
            self._last_sent_by_target.pop(target_key, None)
            self._recent_outbound.pop(message_id, None)
            return False
        target_id = group_id if is_group and group_id else user_id
        scope = "groups" if is_group and group_id else "users"
        try:
            token = await self.get_access_token()
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.delete(
                    f"{self._api_base_url}/v2/{scope}/{target_id}/messages/{message_id}",
                    headers={"Authorization": f"QQBot {token}"},
                )
            _data, error = self._parse_response(resp)
            if error is not None:
                logger.warning(
                    "%s 撤回失败: %s",
                    self._label, self._safe_error_detail(error),
                )
                return False
            self._recent_outbound.pop(message_id, None)
            self._last_sent_by_target.pop(target_key, None)
            if self._last_sent_message_id == message_id:
                self._last_sent_message_id = ""
            logger.info("%s 消息已撤回: %s", self._label, message_id)
            return True
        except Exception as exc:
            logger.warning("%s 撤回异常: %s", self._label, exc)
            return False

    async def _send_stream_frame(
        self,
        user_id: str,
        msg_id: str,
        event_id: str,
        msg_seq: int,
        index: int,
        content: str,
        input_state: int,
        stream_msg_id: str = "",
    ) -> tuple[int, str] | None:
        """发送一次流式替换帧；限流按官方建议退避重试。"""
        token = await self.get_access_token()
        current_index = index
        for attempt in range(3):
            payload: dict[str, Any] = {
                "input_mode": "replace",
                "input_state": input_state,
                "content_type": "markdown",
                "content_raw": content,
                "event_id": event_id,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "index": current_index,
            }
            if stream_msg_id:
                payload["stream_msg_id"] = stream_msg_id
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    resp = await client.post(
                        f"{self._api_base_url}/v2/users/{user_id}/stream_messages",
                        json=payload,
                        headers={"Authorization": f"QQBot {token}"},
                    )
            except Exception as exc:
                logger.warning("%s 流式帧网络异常: %s", self._label, exc)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    current_index += 1
                    continue
                return None
            data, error = self._parse_response(resp)
            if error is not None:
                if (
                    resp.status_code == 429
                    or error.code in RATE_LIMIT_ERROR_CODES
                ) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    current_index += 1
                    continue
                logger.warning(
                    "%s 流式帧失败: %s",
                    self._label, self._safe_error_detail(error),
                )
                return None
            new_stream_id = str(data.get("id", "")) or stream_msg_id
            return current_index + 1, new_stream_id
        return None

    async def _send_stream_media(
        self, user_id: str, event: StreamEvent
    ) -> None:
        """流式收尾产生的图片/表情包沿用普通富媒体发送链路。"""
        if event.media_kind not in ("image", "sticker") or not event.media_url:
            logger.debug("%s 流式媒体暂不发送: %s", self._label, event.media_kind)
            return
        try:
            from app.db.repositories.media_repo import MediaRepository

            media_id = event.media_url.rsplit("/", 1)[-1]
            doc = await MediaRepository().find_by_id(media_id)
            if not doc or not doc.get("data"):
                return
            await self._send_media_reply(
                user_id,
                doc["data"],
                file_type=1,
                msg_id="",
                is_group=False,
                group_id="",
            )
        except Exception as exc:
            logger.warning("%s 流式媒体发送失败: %s", self._label, exc)

    async def _handle_stream_turn(
        self,
        user_id: str,
        content: str,
        nickname: str,
        msg_id: str,
        local_media_url: str,
        content_type: str,
    ) -> None:
        """消费引擎流并把全量文本替换帧推给 QQ；失败后仅降级发送，不重跑引擎。"""
        current = ""
        stream_msg_id = ""
        msg_seq = self._msg_seq = (self._msg_seq + 1) % 65536
        index = 0
        last_flush = 0.0
        stream_available = True
        try:
            async for event in self.engine.handle_message_stream(
                platform="qq",
                platform_id=user_id,
                text=content,
                nickname=nickname,
                content_type=content_type,
                media_url=local_media_url,
            ):
                if isinstance(event, StreamEvent):
                    if event.kind == "reset":
                        current = ""
                        continue
                    if event.kind == "media":
                        await self._send_stream_media(user_id, event)
                        continue
                    delta = event.text
                else:
                    delta = str(event)
                current += delta
                now = time.monotonic()
                if stream_available and current.strip() and now - last_flush >= 0.5:
                    frame = await self._send_stream_frame(
                        user_id, msg_id, msg_id, msg_seq, index,
                        current, 1, stream_msg_id,
                    )
                    if frame is None:
                        stream_available = False
                    else:
                        index, stream_msg_id = frame
                        last_flush = now
        except asyncio.CancelledError:
            # 停止 Bot 时尽量把平台气泡收尾，避免客户端一直显示“生成中”。
            if stream_available and current.strip():
                await asyncio.shield(self._send_stream_frame(
                    user_id, msg_id, msg_id, msg_seq, index,
                    current, 10, stream_msg_id,
                ))
            raise
        except Exception as exc:
            logger.error("%s 流式引擎异常: %s", self._label, exc)

        if not current.strip():
            if not stream_available:
                await self._send_reply(
                    user_id, "网络有点开小差了，你可以再发一次吗？",
                    msg_id, False, "",
                )
            return
        if stream_available:
            frame = await self._send_stream_frame(
                user_id, msg_id, msg_id, msg_seq, index,
                current, 10, stream_msg_id,
            )
            if frame is not None:
                self._remember_outbound(frame[1], user_id, False, "")
                if frame[1]:
                    conversation_id = await self._latest_conversation_id(user_id)
                    await self._record_outbound_binding(
                        frame[1], conversation_id, user_id, False, ""
                    )
                return
        await self._send_reply(
            user_id,
            current,
            msg_id,
            False,
            "",
            use_markdown=settings.qq_markdown_enabled,
        )

    @staticmethod
    def _help_keyboard() -> dict[str, Any]:
        """构造帮助/指令键盘；平台未授权时由调用方降级为普通文本。"""
        return {
            "content": {
                "rows": [{
                    "buttons": [
                        {
                            "id": "help",
                            "render_data": {
                                "label": "使用帮助",
                                "visited_label": "已打开帮助",
                                "style": 1,
                            },
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": "/help",
                            },
                        },
                        {
                            "id": "status",
                            "render_data": {
                                "label": "当前状态",
                                "visited_label": "状态已发送",
                                "style": 0,
                            },
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": "/status",
                            },
                        },
                    ]
                }]
            }
        }

    async def _latest_conversation_id(self, user_id: str) -> str:
        """流式接口不返回会话 ID，按身份与服务反查最近会话供反馈映射。"""
        try:
            from app.db.repositories.conversation_repo import (
                ConversationRepository,
            )
            from app.db.repositories.identity_repo import IdentityRepository
            from app.db.repositories.service_repo import ServiceRepository

            identity = await IdentityRepository().find_by_platform(
                "qq", user_id
            )
            if identity is None:
                return ""
            service = await ServiceRepository().find_by_identity_and_bot(
                identity.id, self.bot_config.id
            )
            if service is None:
                return ""
            conversation = await ConversationRepository().find_latest_by_service(
                service.id
            )
            return conversation.id if conversation is not None else ""
        except Exception as exc:
            logger.debug("%s 流式会话映射查询失败: %s", self._label, exc)
            return ""

    async def _record_outbound_binding(
        self,
        platform_message_id: str,
        conversation_id: str,
        user_id: str,
        is_group: bool,
        group_id: str,
    ) -> None:
        """保存平台消息到内部会话的映射，供 LIKE/UNLIKE 精确回查。"""
        if not platform_message_id or not conversation_id:
            return
        try:
            from app.db.mongo import get_database

            now = datetime.now(timezone.utc)
            await get_database().qq_message_bindings.update_one(
                {
                    "bot_id": self.bot_config.id,
                    "platform_message_id": platform_message_id,
                },
                {
                    "$set": {
                        "conversation_id": conversation_id,
                        "user_openid": user_id,
                        "group_openid": group_id if is_group else "",
                        "is_group": is_group,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
        except Exception as exc:
            logger.warning("%s 消息映射保存失败: %s", self._label, exc)

    async def _send_reply(
        self,
        user_id: str,
        content: str,
        msg_id: str,
        is_group: bool,
        group_id: str,
        is_wakeup: bool = False,
        use_markdown: bool = False,
        attach_keyboard: bool = False,
        conversation_id: str = "",
    ) -> bool:
        """通过 REST API 发送回复；Markdown/键盘失败自动降级普通文本。"""
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
            if is_group and group_id:
                url = f"{self._api_base_url}/v2/groups/{group_id}/messages"
            else:
                # `/v2/users/` 是 QQ 官方路径字面量，非本项目命名，禁止改名
                url = f"{self._api_base_url}/v2/users/{user_id}/messages"

            self._msg_seq += 1
            payload: dict[str, Any] = {"msg_seq": self._msg_seq}
            if use_markdown:
                payload.update({"markdown": {"content": content}, "msg_type": 2})
            else:
                payload.update({"content": content, "msg_type": 0})
            if attach_keyboard and settings.qq_keyboard_enabled:
                payload["keyboard"] = self._help_keyboard()
            # 互动召回与 msg_id 互斥：is_wakeup 优先
            if is_wakeup:
                payload["is_wakeup"] = True
            elif msg_id:
                payload["msg_id"] = msg_id

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            data, error = self._parse_response(resp)
            if error is None:
                self._last_send_error = ""
                response_id = str(data.get("id", ""))
                if response_id:
                    self._remember_outbound(
                        response_id, user_id, is_group, group_id
                    )
                    await self._record_outbound_binding(
                        response_id,
                        conversation_id,
                        user_id,
                        is_group,
                        group_id,
                    )
                logger.info(
                    "%s 消息发送成功 -> %s (wakeup=%s, markdown=%s, keyboard=%s)",
                    self._label, user_id, is_wakeup, use_markdown,
                    attach_keyboard and settings.qq_keyboard_enabled,
                )
                return True

            self._last_send_error = self._safe_error_detail(error)
            logger.error("%s 消息发送失败: %s", self._label, self._last_send_error)
            rate_limit_id = group_id if is_group and group_id else user_id
            self._check_rate_limit_error(resp, rate_limit_id, error)
            if use_markdown or (attach_keyboard and settings.qq_keyboard_enabled):
                logger.warning("%s Markdown/键盘不可用，降级普通文本", self._label)
                return await self._send_reply(
                    user_id=user_id,
                    content=content,
                    msg_id=msg_id,
                    is_group=is_group,
                    group_id=group_id,
                    is_wakeup=is_wakeup,
                    use_markdown=False,
                    attach_keyboard=False,
                    conversation_id=conversation_id,
                )
            return False
        except Exception as exc:
            error = QQResponseError(category="network", message=str(exc))
            self._last_send_error = self._safe_error_detail(error)
            logger.error("%s 发送消息异常: %s", self._label, self._last_send_error)
            if use_markdown or attach_keyboard:
                try:
                    return await self._send_reply(
                        user_id=user_id,
                        content=content,
                        msg_id=msg_id,
                        is_group=is_group,
                        group_id=group_id,
                        is_wakeup=is_wakeup,
                        conversation_id=conversation_id,
                    )
                except Exception:
                    pass
            return False

    def _describe_send_error(self, resp: httpx.Response) -> str:
        """从统一解析结果生成脱敏错误详情，保留旧诊断入口。"""
        _data, error = self._parse_response(resp)
        return self._safe_error_detail(error) if error is not None else ""

    @property
    def last_send_error(self) -> str:
        """最近一次发送失败的平台错误详情（发送成功后清空）。"""
        return self._last_send_error

    def _check_rate_limit_error(
        self,
        resp: httpx.Response,
        user_id: str,
        error: QQResponseError | None = None,
    ) -> None:
        """仅按 HTTP 429 或稳定错误码退避，兼容旧 code 字段。"""
        if error is None:
            _data, error = self._parse_response(resp)
        if error is None:
            error = QQResponseError(status_code=resp.status_code)
        if resp.status_code != 429 and error.code not in RATE_LIMIT_ERROR_CODES:
            return
        expire_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self._rate_limited_openids[user_id] = expire_at
        logger.warning(
            "QQ 限流，目标 %s 退避至 %s", user_id, expire_at.isoformat()
        )

    def _is_rate_limited(self, user_id: str) -> bool:
        """检查目标是否在限流退避期内。"""
        expire_time = self._rate_limited_openids.get(user_id)
        if expire_time is None:
            return False
        if datetime.now(timezone.utc) > expire_time:
            # 退避期已过，移除记录
            del self._rate_limited_openids[user_id]
            return False
        return True

    def _has_proactive_authorization(
        self, user_id: str, is_group: bool, group_id: str
    ) -> bool:
        """平台授权是硬门槛：仅明确允许返回 True，未知和拒绝均拦截。"""
        if is_group:
            if not group_id:
                return False
            key = ("group", group_id)
        else:
            if not user_id:
                return False
            key = ("c2c", user_id)
        return self._proactive_authorizations.get(key) is True

    def _can_send_proactive_target(
        self, user_id: str, is_group: bool, group_id: str = ""
    ) -> bool:
        """组合授权与限流预检，不产生网络请求或 LLM 消耗。"""
        if not self._has_proactive_authorization(user_id, is_group, group_id):
            return False
        rate_limit_id = group_id if is_group else user_id
        return not self._is_rate_limited(rate_limit_id)

    def can_send_proactive(self, user_id: str) -> bool:
        """LLM 前轻量预检；群目标接受 ``group_openid::member_openid``。"""
        if "::" in user_id:
            group_id, member_openid = user_id.split("::", 1)
            return self._can_send_proactive_target(
                member_openid, is_group=True, group_id=group_id
            )
        return self._can_send_proactive_target(user_id, is_group=False)

    async def send_proactive(
        self,
        user_id: str,
        content: str,
        is_group: bool = False,
        group_id: str = "",
    ) -> bool:
        """主动发送消息；平台未明确授权或处于限流期时直接失败。"""
        if not self._has_proactive_authorization(user_id, is_group, group_id):
            target_id = group_id if is_group else user_id
            logger.warning("%s 主动消息未获平台授权，跳过发送", self._label)
            self._last_send_error = f"QQ 主动消息未授权或已拒绝: {target_id}"
            return False
        if not self._can_send_proactive_target(user_id, is_group, group_id):
            rate_limit_id = group_id if is_group else user_id
            logger.warning("%s 主动消息目标处于限流退避期，跳过发送", self._label)
            self._last_send_error = (
                f"QQ 召回限流退避中，目标 {rate_limit_id} 本轮不可发送"
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
            # 互动召回（is_wakeup）仅支持 C2C；群聊必须走普通主动消息
            is_wakeup=not is_group,
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
        # 语音额度按"条"计：后台展示的是"剩余 N 次"，按 len(text) 扣会让一条长语音
        # 把几十次额度一次吃光，口径与展示完全对不上
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
                    await self._send_reply(
                        user_id, text, msg_id, is_group, group_id
                    )
                    return
                quota_charged = True

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
            if quota_charged and account_id:
                # 合成/上传/发送任一步炸了都退回额度：对方最终收到的是文字，
                # 让用户为一条没发出去的语音付费说不过去
                from app.db.repositories.quota_repo import QuotaRepository

                await QuotaRepository().refund(account_id, "voice", 1)
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
                conversation_id=conv_id,
            )
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
                    "%s 表情包媒体不存在: id=%s", self._label, mask_secret(media_id)
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
                conversation_id=conv_id,
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

    def _media_scope_path(self, user_id: str, is_group: bool, group_id: str) -> str:
        """返回 C2C/群目标下的统一媒体路径前缀。"""
        if is_group and group_id:
            return f"{self._api_base_url}/v2/groups/{group_id}"
        return f"{self._api_base_url}/v2/users/{user_id}"

    async def _upload_media_small(
        self,
        user_id: str,
        file_data: bytes,
        file_type: int,
        is_group: bool,
        group_id: str,
        file_name: str = "",
    ) -> str | None:
        """一次 Base64 上传（官方 ≤20MB，实践中 5MB 以上走分片）。"""
        token = await self.get_access_token()
        payload: dict[str, Any] = {
            "file_type": file_type,
            "file_data": base64.b64encode(file_data).decode(),
            "srv_send_msg": False,
        }
        if file_type == 4 and file_name:
            payload["file_name"] = file_name
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._media_scope_path(user_id, is_group, group_id)}/files",
                json=payload,
                headers={"Authorization": f"QQBot {token}"},
            )
        data, error = self._parse_response(resp)
        if error is not None:
            self._last_send_error = self._safe_error_detail(error)
            return None
        file_info = data.get("file_info", "")
        if isinstance(file_info, str) and file_info:
            return file_info
        self._last_send_error = "QQ 上传响应缺少 file_info"
        return None

    async def _upload_media_chunked(
        self,
        user_id: str,
        file_data: bytes,
        file_type: int,
        is_group: bool,
        group_id: str,
        file_name: str = "",
    ) -> str | None:
        """按官方 SDK 执行 prepare → 分片 PUT/finish → complete。"""
        if len(file_data) > MEDIA_MAX_SIZE:
            self._last_send_error = "QQ 媒体超过 100MB 上限"
            return None
        path_prefix = self._media_scope_path(user_id, is_group, group_id)
        token = await self.get_access_token()
        md5 = hashlib.md5(file_data).hexdigest()
        sha1 = hashlib.sha1(file_data).hexdigest()
        md5_10m = (
            hashlib.md5(file_data[:MD5_10M_SIZE]).hexdigest()
            if len(file_data) > MD5_10M_SIZE else md5
        )
        prepare_body: dict[str, Any] = {
            "file_type": file_type,
            "file_name": file_name or "media.bin",
            "file_size": len(file_data),
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{path_prefix}/upload_prepare",
                json=prepare_body,
                headers={"Authorization": f"QQBot {token}"},
            )
        prepare, error = self._parse_response(resp)
        if error is not None:
            self._last_send_error = self._safe_error_detail(error)
            return None
        upload_id = str(prepare.get("upload_id", ""))
        raw_block_size = prepare.get("block_size", 0)
        block_size = (
            int(raw_block_size)
            if isinstance(raw_block_size, (str, int, float))
            else 0
        )
        raw_parts = prepare.get("parts", [])
        if not upload_id or block_size <= 0 or not isinstance(raw_parts, list):
            self._last_send_error = "QQ 分片上传准备响应不完整"
            return None
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict):
                return None
            part_index = int(raw_part.get("index", 0) or 0)
            presigned_url = str(raw_part.get("presigned_url", ""))
            if part_index <= 0 or not presigned_url:
                return None
            offset = (part_index - 1) * block_size
            part_data = file_data[offset:offset + block_size]
            part_md5 = hashlib.md5(part_data).hexdigest()
            put_ok = False
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=300.0) as client:
                        put_resp = await client.put(
                            presigned_url,
                            content=part_data,
                            headers={"Content-Type": "application/octet-stream"},
                        )
                    if put_resp.status_code in (200, 201, 204):
                        put_ok = True
                        break
                    if 400 <= put_resp.status_code < 500:
                        self._last_send_error = (
                            f"QQ 分片上传 HTTP {put_resp.status_code}"
                        )
                        return None
                except Exception as exc:
                    self._last_send_error = f"QQ 分片上传网络异常: {exc}"
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            if not put_ok:
                return None

            finish_ok = False
            for attempt in range(3):
                async with httpx.AsyncClient(timeout=30.0) as client:
                    finish_resp = await client.post(
                        f"{path_prefix}/upload_part_finish",
                        json={
                            "upload_id": upload_id,
                            "part_index": part_index,
                            "block_size": len(part_data),
                            "md5": part_md5,
                        },
                        headers={"Authorization": f"QQBot {token}"},
                    )
                _finish, finish_error = self._parse_response(finish_resp)
                if finish_error is None:
                    finish_ok = True
                    break
                self._last_send_error = self._safe_error_detail(finish_error)
                if finish_resp.status_code != 429 and (
                    finish_error.code not in RATE_LIMIT_ERROR_CODES
                ):
                    return None
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            if not finish_ok:
                return None
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=60.0) as client:
                complete_resp = await client.post(
                    f"{path_prefix}/files",
                    json={"upload_id": upload_id},
                    headers={"Authorization": f"QQBot {token}"},
                )
            complete, complete_error = self._parse_response(complete_resp)
            if complete_error is None:
                file_info = complete.get("file_info", "")
                if isinstance(file_info, str) and file_info:
                    return file_info
                self._last_send_error = "QQ 分片上传完成响应缺少 file_info"
                return None
            self._last_send_error = self._safe_error_detail(complete_error)
            if complete_resp.status_code != 429 and (
                complete_error.code not in RATE_LIMIT_ERROR_CODES
            ):
                return None
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        return None

    async def _send_media_reply(
        self,
        user_id: str,
        file_data: bytes,
        file_type: int,
        msg_id: str,
        is_group: bool,
        group_id: str,
        file_name: str = "",
        conversation_id: str = "",
    ) -> bool:
        """上传富媒体并发送；小文件 Base64，大文件分片，失败统一返回 False。"""
        if not file_data:
            self._last_send_error = "空媒体文件"
            return False
        try:
            if len(file_data) > MEDIA_CHUNK_THRESHOLD:
                file_info = await self._upload_media_chunked(
                    user_id, file_data, file_type, is_group, group_id, file_name
                )
            else:
                file_info = await self._upload_media_small(
                    user_id, file_data, file_type, is_group, group_id, file_name
                )
            if not file_info:
                logger.error(
                    "%s 文件上传失败: %s", self._label, self._last_send_error
                )
                return False

            token = await self.get_access_token()
            msg_url = (
                f"{self._api_base_url}/v2/groups/{group_id}/messages"
                if is_group and group_id
                else f"{self._api_base_url}/v2/users/{user_id}/messages"
            )
            self._msg_seq += 1
            msg_payload: dict[str, Any] = {
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_seq": self._msg_seq,
            }
            if msg_id:
                msg_payload["msg_id"] = msg_id
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    msg_url,
                    json=msg_payload,
                    headers={"Authorization": f"QQBot {token}"},
                )
            data, error = self._parse_response(resp)
            if error is not None:
                self._last_send_error = self._safe_error_detail(error)
                logger.error(
                    "%s 富媒体消息发送失败: %s",
                    self._label, self._last_send_error,
                )
                return False
            response_id = str(data.get("id", ""))
            if response_id:
                self._remember_outbound(
                    response_id, user_id, is_group, group_id
                )
                await self._record_outbound_binding(
                    response_id, conversation_id, user_id, is_group, group_id
                )
            self._last_send_error = ""
            return True
        except Exception as exc:
            error = QQResponseError(category="network", message=str(exc))
            self._last_send_error = self._safe_error_detail(error)
            logger.error(
                "%s 富媒体消息发送异常: %s", self._label, self._last_send_error
            )
            return False

    async def send_file(
        self,
        user_id: str,
        file_data: bytes,
        file_name: str,
        msg_id: str = "",
        is_group: bool = False,
        group_id: str = "",
    ) -> bool:
        """发送普通文件（type=4），支持分片上传。"""
        return await self._send_media_reply(
            user_id, file_data, 4, msg_id, is_group, group_id,
            file_name=file_name,
        )

    async def send_video(
        self,
        user_id: str,
        video_data: bytes,
        msg_id: str = "",
        is_group: bool = False,
        group_id: str = "",
    ) -> bool:
        """发送视频（type=2），支持分片上传。"""
        return await self._send_media_reply(
            user_id, video_data, 2, msg_id, is_group, group_id,
            file_name="video.mp4",
        )

    async def run(self):
        """启动 WebSocket 客户端主循环（含授权恢复与指数退避重连）。"""
        self._running = True
        self._reconnect_delay = 5  # 初始重连间隔（秒）
        MAX_RECONNECT_DELAY = 120  # 最大重连间隔
        try:
            await self._restore_proactive_authorizations()
        except Exception as exc:
            # 恢复失败时缓存保持为空，主动消息 fail-closed，不影响被动回复上线
            logger.error("%s 主动消息授权恢复失败: %s", self._label, exc)

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


def is_runnable_qq(cfg: BotConfig) -> bool:
    """QQ 渠道是否具备启动条件：平台匹配、凭据齐全且未被管理员停用。

    C 端自助创建但尚未扫码绑定的渠道也会被活跃服务引用；没有这道守卫，
    BotManager 会拿着空凭据反复铸 token，日志与平台调用都会被噪声淹没。
    """
    return bool(
        cfg.platform == "qq"
        and cfg.app_id
        and cfg.app_secret
        and not cfg.is_blocked
    )


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

            # 只保留被活跃服务引用、凭据齐全且未停用的 Bot 配置
            new_configs = [
                c for c in new_configs
                if c.id in active_bot_ids and is_runnable_qq(c)
            ]

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
        """对比新旧配置，执行增删改（只处理可运行的QQ平台Bot）"""
        # 只处理具备启动条件的QQ渠道
        qq_configs = [c for c in new_configs if is_runnable_qq(c)]
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
