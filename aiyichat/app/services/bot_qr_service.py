"""QQ / 微信渠道扫码会话共享层。

后台与 C 端复用同一份扫码会话与轮询节流，避免两套实现产生行为差异：
- QQ：绑定会话按 config_id 存在进程内存，含 2.5s / 6s 两档轮询节流；
- 微信：登录二维码、轮询、退出与 client 重建统一收敛在这里。

扫码成功只负责落库（QQ 凭据 / 微信 bot_token），是否额外做后台绑定验证
由调用方决定；C 端创建渠道时已有正式服务，不需要临时服务验证。
"""

import base64
import io
import logging
import time
from typing import Optional

from app.adapters import wechat_clawbot
from app.adapters.qq_bind import QQBindSession, create_bind_task, poll_bind_result
from app.adapters.wechat_clawbot import WeChatClient
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.models.bot_config import BotConfigUpdate

logger = logging.getLogger(__name__)

_bot_repo = BotConfigRepository()

# 被平台限流后的最小轮询间隔（秒）
_RATE_LIMITED_INTERVAL = 6.0
# 正常最小轮询间隔（秒），与前端轮询节奏匹配
_NORMAL_INTERVAL = 2.5


def render_qr_data_uri(qr_url: str) -> str:
    """把二维码内容渲染为 PNG data URI；qrcode 库缺失时返回空串。"""
    try:
        import qrcode as qrcode_lib  # type: ignore[import-untyped]

        qr = qrcode_lib.QRCode(version=1, box_size=10, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return "data:image/png;base64,%s" % base64.b64encode(
            buffer.getvalue()
        ).decode()
    except ImportError:
        logger.warning("qrcode 库未安装，无法生成二维码图片")
        return ""


class QQBindSessionStore:
    """QQ 扫码绑定会话与节流状态（进程内，容器重启即失效）。"""

    def __init__(self) -> None:
        self.sessions: dict[str, QQBindSession] = {}
        self.last_poll_at: dict[str, float] = {}
        self.rate_limited: set[str] = set()

    def drop(self, config_id: str) -> None:
        """清理会话及其节流状态。"""
        self.sessions.pop(config_id, None)
        self.last_poll_at.pop(config_id, None)
        self.rate_limited.discard(config_id)

    async def create(self, config_id: str) -> tuple[QQBindSession, str]:
        """创建绑定任务，返回会话与二维码 data URI。"""
        session = await create_bind_task()
        self.sessions[config_id] = session
        self.last_poll_at.pop(config_id, None)
        self.rate_limited.discard(config_id)
        return session, render_qr_data_uri(session.qr_url)

    async def poll(self, config_id: str) -> dict:
        """轮询绑定状态；成功时写入凭据并清理会话。"""
        session = self.sessions.get(config_id)
        if session is None:
            return {"status": "missing", "message": "绑定会话不存在，请重新获取二维码"}

        now = time.monotonic()
        last_at = self.last_poll_at.get(config_id, 0.0)
        min_interval = (
            _RATE_LIMITED_INTERVAL
            if config_id in self.rate_limited
            else _NORMAL_INTERVAL
        )
        if now - last_at < min_interval:
            return {"status": "waiting", "message": "等待扫码..."}
        self.last_poll_at[config_id] = now

        try:
            result = await poll_bind_result(session)
        except Exception as exc:  # noqa: BLE001 - 平台错误统一转成可读状态
            msg = str(exc)
            logger.error("[QQBind] Poll error: %s", msg, exc_info=True)
            if "频率" in msg or "频繁" in msg:
                self.rate_limited.add(config_id)
                return {"status": "rate_limited", "message": "扫码确认太频繁，请稍候再试"}
            # 这个 message 会经后台与 C 端两条接口返回（web_bots_api 直接转述），
            # 异常原文可能带平台会话标识或内部地址，稳定话术即可，细节留在日志
            return {"status": "error", "message": "绑定失败，请重新获取二维码"}

        status = result.get("status")
        if status == "success":
            await _bot_repo.update(
                config_id,
                BotConfigUpdate(
                    app_id=str(result.get("app_id", "")),
                    app_secret=str(result.get("app_secret", "")),
                ),
            )
            self.drop(config_id)
            logger.info("[QQBind] Bot %s 扫码绑定成功，凭据已保存", config_id)
            return {"status": "success", "message": "绑定成功"}
        if status == "expired":
            self.drop(config_id)
            return {"status": "expired", "message": "二维码已过期，请重新获取"}
        return {"status": status or "waiting", "message": result.get("message", "")}


qq_sessions = QQBindSessionStore()


def drop_qq_session(config_id: str) -> None:
    """供渠道删除时清理扫码会话。"""
    qq_sessions.drop(config_id)


async def ensure_wechat_client(config_id: str) -> Optional[WeChatClient]:
    """取微信渠道的客户端；不存在时按 DB 记录重建并注册为待登录。"""
    manager = wechat_clawbot.get_wechat_manager()
    client = manager.get_client(config_id)
    if client is not None:
        return client
    cfg = await _bot_repo.find_by_id(config_id)
    if cfg is None:
        return None
    client = WeChatClient(
        bot_id=cfg.id,
        bot_token=cfg.bot_token,
        on_token_update=manager._on_token_update,
        account_id=cfg.account_id,
    )
    manager._pending_clients[cfg.id] = client
    return client


async def create_wechat_qr(config_id: str) -> Optional[dict]:
    """获取微信登录二维码并标记 pending_qr；渠道不存在返回 None。"""
    client = await ensure_wechat_client(config_id)
    if client is None:
        return None
    qr_data = await client.get_login_qrcode()
    qrcode_url = qr_data.get("qrcode_url", "")
    await _bot_repo.update_qrcode(config_id, qrcode_url)
    await _bot_repo.update_status(config_id, "pending_qr")
    return {"qrcode_url": qrcode_url, "qrcode": qr_data.get("qrcode", "")}


async def poll_wechat_login(
    config_id: str, qrcode: str, verify_code: str = ""
) -> dict:
    """轮询微信扫码状态；验证码、重定向和异常均保留可恢复状态。"""
    client = await ensure_wechat_client(config_id)
    if client is None:
        return {"status": "error", "message": "渠道不存在"}

    manager = wechat_clawbot.get_wechat_manager()
    try:
        previous_token = getattr(client, "_bot_token", "") or ""
        result = await client.poll_qrcode_status(qrcode, verify_code)
        status = result.get("status", "wait")
        if status in {"confirmed", "binded_redirect"}:
            bot_token = result.get("bot_token", "") or client._bot_token
            if not bot_token:
                return {
                    "status": "binded_redirect",
                    "message": "该 Bot 已绑定，但本渠道无可用凭据，请先退出登录后重新扫码",
                }
            if status == "confirmed":
                await _bot_repo.update_wechat_bot_token(config_id, bot_token)
                if previous_token and previous_token != bot_token:
                    await client.reset_sync_state()
            client.set_bot_token(bot_token)
            await _bot_repo.update_status(config_id, "online")
            # 重复确认或重复绑定只保留一个轮询任务。
            if manager.is_bot_running(config_id):
                await manager.cancel_bot_task(config_id)
            manager.start_bot_task(config_id, client)
            logger.info("[WeChatBot] Bot %s 登录成功并已启动", config_id)
            return {"status": "success", "message": "登录成功"}

        messages = {
            "wait": "等待扫码...",
            "scaned": "已扫码，请在微信中继续确认",
            "expired": "二维码已过期，请重新获取",
            "need_verifycode": "请输入微信中显示的验证码",
            "verify_code_blocked": "验证码错误次数过多，请重新获取二维码",
            "scaned_but_redirect": "已切换登录服务器，请继续在微信中确认",
        }
        if status in messages:
            return {"status": status, "message": messages[status]}
        return {"status": "error", "message": "未知扫码状态，请重新获取二维码"}
    except Exception as exc:  # noqa: BLE001 - 平台错误统一转成可读状态
        # 网络异常可能拼有 qrcode/verify_code 查询串，日志仅保留异常类型。
        logger.error("[WeChatBot] Poll QR error: %s", type(exc).__name__)
        # 异常原文可能包含平台凭据，双端只返回稳定可恢复话术。
        return {"status": "error", "message": "扫码状态获取失败，请稍后重试或重新获取二维码"}


async def logout_wechat(config_id: str) -> bool:
    """退出微信渠道：取消任务、清 token/context、置 offline。"""
    manager = wechat_clawbot.get_wechat_manager()
    await manager.cancel_bot_task(config_id)
    manager._pending_clients.pop(config_id, None)
    return await _bot_repo.clear_wechat_auth(config_id)
