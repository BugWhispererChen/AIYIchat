"""C 端聊天渠道（Bot）自助管理 API。

- GET    /api/web/bots                         可管理渠道列表 + 配额
- POST   /api/web/bots                         只创建渠道，服务另行显式装配
- PUT    /api/web/bots/{bot_id}                改名 / QQ 凭据 / 沙箱
- DELETE /api/web/bots/{bot_id}                级联删服务并删渠道
- POST   /api/web/bots/{bot_id}/qq/qrcode      QQ 绑定二维码
- GET    /api/web/bots/{bot_id}/qq/poll-qr     QQ 绑定轮询
- POST   /api/web/bots/{bot_id}/wechat/qrcode  微信登录二维码
- GET    /api/web/bots/{bot_id}/wechat/poll-qr 微信登录轮询
- POST   /api/web/bots/{bot_id}/wechat/logout  微信退出登录

安全边界：
- 全部走 `require_web_account`（账号没有活跃服务时也能自救）；
- 归属与来源由服务端判定，管理员 QQ / 微信渠道统一按不存在处理；
- 响应绝不返回 app_secret / bot_token / context_token；
- 业务错误统一 400/403/404/409/429，绝不返回 401（前端一见 401 就踢登录）。
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth_api import (
    AuthError,
    WebAccountPrincipal,
    _error,
    client_ip,
    require_web_account,
)
from app.db.repositories.service_repo import ServiceRepository
from app.models.bot_config import BotConfigInDB
from app.models.service import ServiceInDB
from app.services import bot_qr_service, web_channel_service
from app.services.web_channel_service import ChannelError
from app.utils.rate_limit import RateLimited, check_rate_limit

logger = logging.getLogger(__name__)

web_bots_router = APIRouter()
_service_repo = ServiceRepository()

# 10 分钟滑动窗口：写操作 20 次、取码 10 次、扫码轮询 120 次
# 轮询与取码必须分开计数：前端每 2~3 秒轮询一次，共用 10 次阈值会让正常扫码
# 在二维码有效期内必然被 429 打断（见 P1-1）
_RATE_WRITE = 20
_RATE_QR = 10
_RATE_QR_POLL = 120


class WebBotCreatePayload(BaseModel):
    """新建渠道：只填渠道自身字段，服务由用户稍后显式装配。"""

    platform: Literal["qq", "wechat", "web"]
    name: str
    sandbox_mode: bool = True
    app_id: str = ""
    app_secret: str = ""


class WebBotUpdatePayload(BaseModel):
    """编辑渠道：字段缺省表示不修改。"""

    name: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    sandbox_mode: Optional[bool] = None


def _bad(code: str, detail: str, status_code: int = 400) -> JSONResponse:
    """统一的业务错误响应。"""
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


def _channel_error(exc: ChannelError) -> JSONResponse:
    """把服务层异常转成统一的业务错误响应。"""
    return _bad(exc.code, exc.message, exc.status_code)


def _limit(
    scope: str, principal: WebAccountPrincipal, request: Request, limit: int
) -> Optional[JSONResponse]:
    """按「凭据 + IP」限流；超限返回 429 响应。"""
    try:
        check_rate_limit(
            scope, f"{principal.credential.id}:{client_ip(request)}", limit
        )
    except RateLimited as exc:
        return _bad("rate_limited", exc.message, 429)
    return None


async def _principal(request: Request) -> tuple[Optional[WebAccountPrincipal], Optional[JSONResponse]]:
    """解析账号中心上下文并转换鉴权异常。"""
    try:
        return await require_web_account(request), None
    except AuthError as exc:
        return None, _error(exc)


def _bot_item(bot: BotConfigInDB, services: list[ServiceInDB]) -> dict:
    """渠道展示项：凭据只暴露"是否已配置"，绝不回传密文。"""
    return {
        "id": bot.id,
        "name": bot.name,
        "platform": bot.platform,
        "origin": bot.origin,
        "manageable": True,
        "state": web_channel_service.compute_state(bot),
        "is_blocked": bot.is_blocked,
        "block_reason": bot.block_reason,
        "sandbox_mode": bot.sandbox_mode,
        "app_id": bot.app_id if bot.platform == "qq" else "",
        "has_secret": bool(bot.app_secret) if bot.platform == "qq" else False,
        "has_credentials": web_channel_service.has_credentials(bot),
        "service_count": len(services),
        "service_names": [svc.name for svc in services],
        "token_expire_at": (
            bot.token_expire_at.isoformat() if bot.token_expire_at else ""
        ),
        "created_at": bot.created_at.isoformat(),
    }


async def _services_of_bot(bot_id: str) -> list[ServiceInDB]:
    """取某个渠道下的服务（渠道删除前/展示时使用）。"""
    return await _service_repo.list_by_bot(bot_id)


@web_bots_router.get("")
async def list_web_bots(request: Request) -> JSONResponse:
    """列出本人账号下可管理的渠道与统一配额。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)

    account_id = principal.account_id
    bots = await web_channel_service.list_managed_bots(account_id)
    services = await _service_repo.list_by_account(account_id)
    by_bot: dict[str, list[ServiceInDB]] = {}
    for svc in services:
        by_bot.setdefault(svc.bot_id, []).append(svc)

    quota = await web_channel_service.web_quota(account_id)
    limit = quota["limit"]
    # 渠道与服务各自独立计数：服务满额不再误阻渠道创建
    can_bot = limit > 0 and quota["bots"] < limit
    can_service = limit > 0 and quota["services"] < limit
    return JSONResponse(
        status_code=200,
        content={
            "items": [_bot_item(bot, by_bot.get(bot.id, [])) for bot in bots],
            "quota": quota,
            "can_create_bot": can_bot,
            "can_create_service": can_service,
        },
    )


@web_bots_router.post("", status_code=201)
async def create_web_bot(
    request: Request, payload: WebBotCreatePayload
) -> JSONResponse:
    """创建渠道；服务由 `POST /api/web/services` 显式装配。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_write", principal, request, _RATE_WRITE)
    if limited is not None:
        return limited

    account_id = principal.account_id
    try:
        bot = await web_channel_service.create_managed_bot(
            account_id=account_id,
            platform=payload.platform,
            name=payload.name,
            sandbox_mode=payload.sandbox_mode,
            app_id=payload.app_id,
            app_secret=payload.app_secret,
        )
    except ChannelError as exc:
        return _channel_error(exc)

    services = await _services_of_bot(bot.id)
    return JSONResponse(
        status_code=201,
        content={"bot": _bot_item(bot, services)},
    )


@web_bots_router.put("/{bot_id}")
async def update_web_bot(
    request: Request, bot_id: str, payload: WebBotUpdatePayload
) -> JSONResponse:
    """编辑渠道：名称 / QQ 凭据 / 沙箱。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_write", principal, request, _RATE_WRITE)
    if limited is not None:
        return limited

    try:
        bot = await web_channel_service.update_managed_bot(
            account_id=principal.account_id,
            bot_id=bot_id,
            name=payload.name,
            app_id=payload.app_id,
            app_secret=payload.app_secret,
            sandbox_mode=payload.sandbox_mode,
        )
    except ChannelError as exc:
        return _channel_error(exc)

    services = await _services_of_bot(bot.id)
    return JSONResponse(status_code=200, content={"bot": _bot_item(bot, services)})


@web_bots_router.delete("/{bot_id}")
async def delete_web_bot(request: Request, bot_id: str) -> JSONResponse:
    """删除渠道：级联删除其下服务，保留对话历史、清理服务记忆与注入快照。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_write", principal, request, _RATE_WRITE)
    if limited is not None:
        return limited

    try:
        deleted = await web_channel_service.delete_managed_bot(
            principal.account_id, bot_id
        )
    except ChannelError as exc:
        return _channel_error(exc)
    return JSONResponse(
        status_code=200,
        content={"id": bot_id, "deleted": True, "deleted_services": deleted},
    )


@web_bots_router.post("/{bot_id}/qq/qrcode")
async def create_qq_qrcode(request: Request, bot_id: str) -> JSONResponse:
    """获取 QQ 扫码绑定二维码。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_qr", principal, request, _RATE_QR)
    if limited is not None:
        return limited

    try:
        bot = await web_channel_service.get_managed_bot(
            principal.account_id, bot_id
        )
    except ChannelError as exc:
        return _channel_error(exc)
    if bot.platform != "qq":
        return _bad("platform_mismatch", "仅 QQ 渠道支持扫码绑定", 400)

    try:
        session, image = await bot_qr_service.qq_sessions.create(bot.id)
    except Exception as exc:  # noqa: BLE001 - 平台错误转成可读响应
        logger.error("[QQBind] 创建绑定任务失败: %s", exc)
        return _bad("qr_create_failed", f"获取二维码失败：{exc}", 502)
    return JSONResponse(
        status_code=200,
        content={
            "status": "waiting",
            "image": image,
            "message": "请使用手机QQ扫码，选择要绑定的机器人",
            "task_id": session.task_id,
        },
    )


@web_bots_router.get("/{bot_id}/qq/poll-qr")
async def poll_qq_qrcode(request: Request, bot_id: str) -> JSONResponse:
    """轮询 QQ 绑定状态；成功后凭据落库，渠道由 Manager 热加载启动。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_poll", principal, request, _RATE_QR_POLL)
    if limited is not None:
        return limited

    try:
        bot = await web_channel_service.get_managed_bot(
            principal.account_id, bot_id
        )
    except ChannelError as exc:
        return _channel_error(exc)
    if bot.platform != "qq":
        return _bad("platform_mismatch", "仅 QQ 渠道支持扫码绑定", 400)

    result = await bot_qr_service.qq_sessions.poll(bot_id)
    if result.get("status") == "missing":
        return _bad(
            "qr_session_missing",
            result.get("message", "绑定会话不存在，请重新获取二维码"),
            404,
        )
    return JSONResponse(status_code=200, content=result)


@web_bots_router.post("/{bot_id}/wechat/qrcode")
async def create_wechat_qrcode(request: Request, bot_id: str) -> JSONResponse:
    """获取微信扫码登录二维码。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_qr", principal, request, _RATE_QR)
    if limited is not None:
        return limited

    try:
        bot = await web_channel_service.get_managed_bot(
            principal.account_id, bot_id
        )
    except ChannelError as exc:
        return _channel_error(exc)
    if bot.platform != "wechat":
        return _bad("platform_mismatch", "仅微信渠道支持扫码登录", 400)

    try:
        qr_data = await bot_qr_service.create_wechat_qr(bot.id)
    except Exception as exc:  # noqa: BLE001 - 平台错误转成可读响应
        logger.error("[WeChatBot] 获取登录二维码失败: %s", exc)
        return _bad("qr_create_failed", f"获取二维码失败：{exc}", 502)
    if qr_data is None:
        return _bad("not_found", "渠道不存在", 404)
    return JSONResponse(
        status_code=200,
        content={
            "status": "waiting",
            "image_url": qr_data.get("qrcode_url", ""),
            "qrcode": qr_data.get("qrcode", ""),
            "message": "请使用微信扫码登录",
        },
    )


@web_bots_router.get("/{bot_id}/wechat/poll-qr")
async def poll_wechat_qrcode(
    request: Request, bot_id: str, qrcode: str, verify_code: str = ""
) -> JSONResponse:
    """轮询微信登录状态，可提交验证码；成功后 token 落库并启动任务。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_poll", principal, request, _RATE_QR_POLL)
    if limited is not None:
        return limited

    try:
        bot = await web_channel_service.get_managed_bot(
            principal.account_id, bot_id
        )
    except ChannelError as exc:
        return _channel_error(exc)
    if bot.platform != "wechat":
        return _bad("platform_mismatch", "仅微信渠道支持扫码登录", 400)
    return JSONResponse(
        status_code=200,
        content=await bot_qr_service.poll_wechat_login(bot_id, qrcode, verify_code),
    )


@web_bots_router.post("/{bot_id}/wechat/logout")
async def logout_wechat(request: Request, bot_id: str) -> JSONResponse:
    """退出微信渠道登录。"""
    principal, err = await _principal(request)
    if err is not None or principal is None:
        return err or _bad("unauthorized", "请先登录", 401)
    limited = _limit("web_bot_write", principal, request, _RATE_WRITE)
    if limited is not None:
        return limited

    try:
        bot = await web_channel_service.get_managed_bot(
            principal.account_id, bot_id
        )
    except ChannelError as exc:
        return _channel_error(exc)
    if bot.platform != "wechat":
        return _bad("platform_mismatch", "仅微信渠道支持退出登录", 400)

    await bot_qr_service.logout_wechat(bot_id)
    return JSONResponse(
        status_code=200, content={"status": "offline", "message": "已退出登录"}
    )
