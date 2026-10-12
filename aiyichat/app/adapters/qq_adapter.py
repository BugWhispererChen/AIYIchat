import hashlib
import hmac
import logging
import time
from typing import Optional

import httpx

from app.adapters.base import ChannelAdapter, NormalizedMessage
from app.config import settings
from app.db.repositories.bot_config_repo import BotConfigRepository

logger = logging.getLogger(__name__)

# QQ Bot API 基础URL
QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"


class QQAdapter(ChannelAdapter):
    """
    QQ官方Bot API适配器。

    支持：
    - C2C_MESSAGE_CREATE: 私聊消息
    - GROUP_AT_MESSAGE_CREATE: 群@消息
    - 签名验证
    - 消息回复（被动回复 + 主动发送）
    """

    def __init__(self):
        self._app_id = settings.qq_app_id
        self._app_secret = settings.qq_app_secret
        self._bot_token = settings.qq_bot_token
        base = QQ_SANDBOX_API_BASE if settings.qq_sandbox_mode else QQ_API_BASE
        self._api_base = base
        self._http = httpx.AsyncClient(
            base_url=base,
            headers={
                "Authorization": f"QQBot {self._bot_token}",
                "X-Union-Appid": self._app_id,
            },
            timeout=10.0,
        )
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._bot_config_repo = BotConfigRepository()

    async def get_access_token(self) -> str:
        """获取或刷新 QQ Bot access_token（带过期校验，提前 60 秒刷新）。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={
                    "appId": self._app_id,
                    "clientSecret": self._app_secret,
                },
            )
            data = resp.json()
            self._access_token = data.get("access_token", "")
            # QQ access_token 默认有效期 7200 秒，记录过期时间以便到期自动刷新
            expires_in = int(data.get("expires_in", 7200))
            self._token_expires_at = now + expires_in
            return self._access_token

    def verify_signature(self, body: bytes, signature: str, timestamp: str) -> bool:
        """验证QQ webhook请求签名。"""
        try:
            seed = self._app_secret
            msg = f"{timestamp}{body.decode('utf-8')}"
            expected = hmac.new(
                seed.encode("utf-8"),
                msg.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception as e:
            logger.error("Signature verification failed: %s", e)
            return False

    async def _resolve_bot_id(self) -> str:
        """根据 app_id 查找对应的 BotConfig，返回 Bot UUID id。"""
        cfg = await self._bot_config_repo.find_by_app_id("qq", self._app_id)
        if cfg:
            return cfg.id
        # 兜底：若数据库中无记录，使用 app_id 作为 bot_id
        return self._app_id

    async def parse_incoming(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """解析QQ webhook事件为标准化消息。"""
        event_type = raw_event.get("t", "")
        data = raw_event.get("d", {})
        bot_id = await self._resolve_bot_id()

        if event_type == "C2C_MESSAGE_CREATE":
            # 私聊消息
            author = data.get("author", {})
            content = data.get("content", "").strip()
            return NormalizedMessage(
                platform="qq",
                user_id=author.get("user_openid", ""),
                nickname=author.get("username", ""),
                content=content,
                message_id=data.get("id", ""),
                is_group=False,
                bot_id=bot_id,
            )

        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            # 群@消息
            author = data.get("author", {})
            content = data.get("content", "").strip()
            # 移除@bot的部分
            content = self._strip_at_mention(content)
            return NormalizedMessage(
                platform="qq",
                user_id=author.get("member_openid", ""),
                nickname=author.get("username", ""),
                content=content,
                message_id=data.get("id", ""),
                is_group=True,
                group_id=data.get("group_openid", ""),
                bot_id=bot_id,
            )

        logger.debug("Ignored event type: %s", event_type)
        return None

    async def send_message(self, user_id: str, content: str, **kwargs) -> bool:
        """
        发送消息到QQ用户。

        支持两种模式：
        1. 被动回复（在webhook响应中直接返回）
        2. 主动发送（通过REST API，用于异步回复）
        """
        msg_id = kwargs.get("msg_id", "")
        is_group = kwargs.get("is_group", False)
        group_id = kwargs.get("group_id", "")

        try:
            token = await self.get_access_token()
            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            }

            if is_group and group_id:
                url = f"{self._api_base}/v2/groups/{group_id}/messages"
            else:
                url = f"{self._api_base}/v2/users/{user_id}/messages"

            payload = {
                "content": content,
                "msg_type": 0,  # 文本消息
            }
            if msg_id:
                payload["msg_id"] = msg_id

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    logger.info("Message sent to %s", user_id)
                    return True
                else:
                    logger.error(
                        "Failed to send message: %d %s",
                        resp.status_code,
                        resp.text,
                    )
                    return False

        except Exception as e:
            logger.error("Send message error: %s", e)
            return False

    def _strip_at_mention(self, content: str) -> str:
        """移除消息中的@bot部分。"""
        import re

        return re.sub(r"<@!\d+>", "", content).strip()


# 全局单例
qq_adapter = QQAdapter()
