"""C 端网页账号账号中心 API：资料、密码、登录设备、额度、数据与隐私、注销。

安全边界：
- 全部端点走 `require_web_account`，归属（account_id / identity_id / credential_id）
  一律由会话令牌反查，客户端入参只允许携带字段值本身；
- 与 `/api/web/profiles`、`/api/web/services` 不同，本模块**不要求存在活跃服务**：
  服务被停用或删空的账号恰恰最需要改密、清理数据与注销；
- 业务错误统一 400/403/404/409，绝不返回 401——网页端 `WebAuth.call` 一见 401
  就把人踢回登录页，"旧密码填错"这类可恢复的输入错误必须是 400；
- 导出与注销属高危出口：导出白名单字段、注销需当前密码 + 用户名双重确认并限流。
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.api.auth_api import (
    WebAccountPrincipal,
    _error,
    client_ip,
    require_web_account,
)
from app.config import settings
from app.db.repositories.order_repo import OrderRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.web_session_repo import WebSessionRepository
from app.services import web_account_service as account
from app.services.auth_service import AuthError, change_password
from app.services.web_account_service import delete_account
from app.utils.password_hashing import verify_password
from app.utils.rate_limit import RateLimited, check_rate_limit

logger = logging.getLogger(__name__)

web_account_router = APIRouter()

# 限流阈值（10 分钟滑动窗口）：改密与注销给得极紧，普通写操作宽松到不影响正常使用
_RATE_WRITE = 20
_RATE_PASSWORD = 5
_RATE_DATA = 10
_RATE_DELETE = 3


class ProfileUpdatePayload(BaseModel):
    """网页登录账号资料更新请求体。"""

    nickname: str = ""


class PasswordChangePayload(BaseModel):
    """改密请求体：必须带当前密码。"""

    current_password: str = ""
    new_password: str = ""


class ConfirmPayload(BaseModel):
    """破坏性操作的显式确认位（前端复选框/二次弹窗都要置真）。"""

    confirm: bool = False


class AccountDeletePayload(BaseModel):
    """注销请求体：当前密码 + 手工输入的用户名。"""

    current_password: str = ""
    username_confirm: str = ""


async def _principal(request: Request) -> WebAccountPrincipal:
    """解析账号中心上下文并把业务异常转成 JSON 响应（沿用 auth_api 的错误形状）。"""
    return await require_web_account(request)


def _limit(scope: str, principal: WebAccountPrincipal, request: Request, limit: int) -> None:
    """按「凭据 + IP」限流：同 IP  NAT 后的多账号不会互相打死，单账号刷不动。"""
    check_rate_limit(scope, f"{principal.credential.id}|{client_ip(request)}", limit)


def _service_unavailable(detail: str) -> JSONResponse:
    """依赖缺失（额度/订单仓储异常）时给身份一句可操作的话。"""
    return JSONResponse(status_code=503, content={"code": "dependency_missing", "detail": detail})


@web_account_router.get("/profile")
async def get_profile(request: Request) -> JSONResponse:
    """当前账号资料：用户名只读，昵称/自我描述/时区可改。"""
    try:
        principal = await _principal(request)
    except AuthError as exc:
        return _error(exc)
    return JSONResponse(
        status_code=200,
        content=account.profile_payload(principal.credential, principal.account),
    )


@web_account_router.put("/profile")
async def update_profile(request: Request, payload: ProfileUpdatePayload) -> JSONResponse:
    """保存资料：字段白名单 + 长度/时区校验，归属字段一律不接受客户端入参。"""
    try:
        principal = await _principal(request)
        _limit("account_write", principal, request, _RATE_WRITE)
        data = await account.update_profile(
            principal.credential,
            principal.account_id,
            nickname=payload.nickname,
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(status_code=200, content=data)


@web_account_router.post("/password")
async def change_my_password(request: Request, payload: PasswordChangePayload) -> JSONResponse:
    """自助改密：验旧密码 → 策略校验 → 保留当前会话、下线其他设备。"""
    try:
        principal = await _principal(request)
        _limit("account_password", principal, request, _RATE_PASSWORD)
        revoked = await change_password(
            principal.credential.id,
            payload.current_password,
            payload.new_password,
            keep_session_id=principal.session.id,
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(status_code=200, content={"ok": True, "revoked_sessions": revoked})


@web_account_router.get("/sessions")
async def list_sessions(request: Request) -> JSONResponse:
    """登录设备列表：IP 脱敏、设备名可读、绝不返回令牌。"""
    try:
        principal = await _principal(request)
    except AuthError as exc:
        return _error(exc)
    sessions = await WebSessionRepository().list_by_credential(principal.credential.id)
    return JSONResponse(
        status_code=200,
        content={
            "items": account.sessions_payload(sessions, principal.session.id),
            "max_concurrent": settings.web_session_max_concurrent,
            "session_hours": settings.web_session_hours,
        },
    )


@web_account_router.delete("/sessions/{session_id}")
async def revoke_session(request: Request, session_id: str) -> JSONResponse:
    """下线指定设备；当前设备必须走「退出登录」，否则会留下一个无法撤销的会话。"""
    try:
        principal = await _principal(request)
        _limit("account_write", principal, request, _RATE_WRITE)
        await account.revoke_session(
            principal.credential.id, session_id, principal.session.id
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(status_code=200, content={"id": session_id, "revoked": True})


@web_account_router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request) -> JSONResponse:
    """下线除当前设备外的全部会话（怀疑其他设备泄露时的一键处置）。"""
    try:
        principal = await _principal(request)
        _limit("account_write", principal, request, _RATE_WRITE)
        revoked = await WebSessionRepository().delete_others(
            principal.credential.id, principal.session.id
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    logger.info("网页账号 %s 主动下线其他会话 %d 个", principal.credential.username, revoked)
    return JSONResponse(status_code=200, content={"revoked": revoked})


@web_account_router.get("/quota")
async def get_quota(request: Request, order_limit: int = 20) -> JSONResponse:
    """额度中心：三类额度余量与有效期 + 管理员发放记录（只读）。"""
    try:
        principal = await _principal(request)
    except AuthError as exc:
        return _error(exc)
    account_id = principal.account_id
    quota = await QuotaRepository().find_by_account(account_id)
    bounded = max(1, min(order_limit, 50))
    orders = await OrderRepository().list_recent(account_id=account_id, limit=bounded)
    total = await OrderRepository().count(account_id=account_id)
    return JSONResponse(
        status_code=200,
        content={
            "quota": account.quota_payload(quota),
            "orders": {"items": account.order_payload(orders), "total": total},
            "units": {"text": "Token", "voice": "次", "image": "张"},
        },
    )


@web_account_router.get("/data")
async def get_data(request: Request) -> JSONResponse:
    """数据概览：对话/消息/记忆总量与按服务的分布。"""
    try:
        principal = await _principal(request)
    except AuthError as exc:
        return _error(exc)
    return JSONResponse(
        status_code=200,
        content=await account.data_overview(principal.account_id),
    )


@web_account_router.get("/memories")
async def get_memories(request: Request) -> JSONResponse:
    """长期记忆明细：按服务分组，排除向量与评分等内部字段。"""
    try:
        principal = await _principal(request)
    except AuthError as exc:
        return _error(exc)
    return JSONResponse(
        status_code=200,
        content={"items": await account.memories_payload(principal.account_id)},
    )


@web_account_router.get("/export")
async def export_data(request: Request) -> Response:
    """导出个人数据包（JSON 附件）。

    字段全部白名单组装：密码哈希、会话令牌、注册 IP、记忆向量、运营字段一律不出现在导出里。
    """
    try:
        principal = await _principal(request)
    except AuthError as exc:
        return _error(exc)
    try:
        _limit("account_data", principal, request, _RATE_DATA)
        payload, truncated = await account.export_payload(
            principal.credential, principal.account
        )
    except RateLimited as exc:
        return _error(exc)
    except Exception:  # noqa: BLE001 - 导出失败不能把半截 JSON 发出去
        logger.exception("网页账号导出失败: username=%s", principal.credential.username)
        return _service_unavailable("导出失败，请稍后重试或联系管理员")
    payload["truncated"] = truncated
    filename = f"aiyichat-account-{principal.credential.username}.json"
    return JSONResponse(
        status_code=200,
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@web_account_router.post("/data/conversations/clear")
async def clear_conversations(request: Request, payload: ConfirmPayload) -> JSONResponse:
    """清空本人全部对话记录（含消息、注入快照与其引用的媒体），长期记忆不受影响。"""
    try:
        principal = await _principal(request)
        _limit("account_data", principal, request, _RATE_DATA)
        if not payload.confirm:
            raise AuthError("confirm_required", "请先确认清空对话记录的影响", 400)
        stats = await account.clear_conversations(
            principal.account_id, principal.username
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(status_code=200, content=stats)


@web_account_router.post("/data/memories/{service_id}/clear")
async def clear_memory(request: Request, service_id: str, payload: ConfirmPayload) -> JSONResponse:
    """清空指定服务的长期记忆；该服务与其他渠道身份共享上下文时拒绝。"""
    try:
        principal = await _principal(request)
        _limit("account_data", principal, request, _RATE_DATA)
        if not payload.confirm:
            raise AuthError("confirm_required", "请先确认清空记忆的影响", 400)
        service = await account.find_owned_service(principal.account_id, service_id)
        stats = await account.clear_memory(
            principal.account_id, principal.username, service
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(status_code=200, content=stats)


@web_account_router.post("/delete")
async def delete_my_account(request: Request, payload: AccountDeletePayload) -> JSONResponse:
    """注销账号：当前密码 + 输入用户名确认后立即硬删除全部数据并释放用户名。

    不可恢复。执行顺序见 `web_account_service.delete_account`。
    """
    try:
        principal = await _principal(request)
        _limit("account_delete", principal, request, _RATE_DELETE)
        if not verify_password(
            payload.current_password, principal.credential.password_hash
        ):
            raise AuthError("invalid_current_password", "当前密码不正确", 400)
        account.normalize_confirm_username(payload.username_confirm, principal.credential.username)
        stats = await delete_account(principal.credential, principal.account_id)
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(
        status_code=200,
        content={"deleted": True, "username": principal.credential.username, "stats": stats},
    )
