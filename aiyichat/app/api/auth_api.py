"""C 端网页鉴权 API：验证码、注册、登录、注销、当前身份。

安全边界：
- 所有写操作先按 IP 滑动窗口限流，再校验一次性图形验证码；
- 业务规则全部收敛在 `app/services/auth_service.py`，本层只做 HTTP ↔ 服务层转换；
- `require_current_identity` 是网页会话的唯一出口，`platform_id`/`bot_id` 一律由令牌反查，
  绝不信任客户端入参。
"""

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.models.service import ServiceInDB
from app.models.identity import IdentityInDB
from app.models.web_credential import WebCredentialInDB
from app.models.web_session import WebSessionInDB
from app.services import auth_service
from app.services.auth_service import AuthError
from app.services.captcha_service import captcha_service
from app.utils.rate_limit import RateLimited, check_rate_limit

auth_router = APIRouter()
logger = logging.getLogger(__name__)

# 反向代理（nginx）必须以 $remote_addr 覆写 X-Forwarded-For，否则客户端可自造头绕过限流
_XFF_HEADER = "x-forwarded-for"


@dataclass
class WebPrincipal:
    """由会话令牌解析出的当前网页账号会话主体（后续接口只认这个对象）。"""

    credential: WebCredentialInDB
    identity_id: str
    account_id: str
    username: str
    nickname: str
    bot_id: str
    service_id: str


@dataclass
class WebAccountPrincipal:
    """账号中心上下文：只依赖凭据 / 身份 / 会话，不要求账号已有活跃服务。

    与 `WebPrincipal` 分开是有意为之：对话与角色/服务接口必须有可用服务才能工作，
    但账号中心承担的正是"服务被停用后用户仍能自救"的场景（改密、清理数据、注销），
    若复用带服务校验的依赖，这类用户会被 403 彻底挡在门外。
    """

    credential: WebCredentialInDB
    identity: IdentityInDB
    session: WebSessionInDB
    token: str


def client_ip(request: Request) -> str:
    """取客户端 IP。

    只有显式开启 `trust_proxy_headers`（反向代理已用 `$remote_addr` 覆写该头）时
    才读取 `X-Forwarded-For`；默认直连部署下信任它等于让客户端自选限流 key。
    """
    if settings.trust_proxy_headers:
        xff = request.headers.get(_XFF_HEADER, "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def bearer_token(request: Request) -> str:
    """从 `Authorization: Bearer <token>` 取令牌；兼容网页端使用的 X-Auth-Token 头。"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-auth-token", "").strip()


async def require_current_identity(request: Request) -> WebPrincipal:
    """网页会话依赖：令牌有效则返回当前身份，否则 401/403。

    Raises:
        AuthError: 总开关关闭、缺/错令牌、会话过期、身份被禁用或业务服务缺失
    """
    token = bearer_token(request)
    if not token:
        raise AuthError("unauthorized", "请先登录", 401)
    result = await auth_service.resolve_session(token)
    identity = await IdentityRepository().find_by_id(result.identity_id)
    if identity is None or identity.is_blocked:
        raise AuthError("forbidden", "账号不可用，请联系管理员", 403)
    service = await _resolve_service(identity)
    return WebPrincipal(
        credential=result.credential,
        identity_id=identity.id,
        account_id=identity.account_id,
        username=result.credential.username,
        nickname=identity.nickname or identity.name or result.credential.username,
        bot_id=service.bot_id,
        service_id=service.id,
    )


async def require_web_account(request: Request) -> WebAccountPrincipal:
    """账号中心依赖：解析凭据 / 身份 / 当前会话，不校验是否存在活跃服务。

    Raises:
        AuthError: 总开关关闭、缺/错令牌、会话过期、凭据非 active、身份被禁用或已删除
    """
    token = bearer_token(request)
    if not token:
        raise AuthError("unauthorized", "请先登录", 401)
    result = await auth_service.resolve_session(token)
    identity = await IdentityRepository().find_by_id(result.identity_id)
    if identity is None or identity.is_blocked:
        raise AuthError("forbidden", "账号不可用，请联系管理员", 403)
    return WebAccountPrincipal(
        credential=result.credential,
        identity=identity,
        session=result.session,
        token=token,
    )


async def _resolve_service(identity: IdentityInDB) -> ServiceInDB:
    """反查该网页渠道身份的对话服务。

    优先级：账号自己的 web Bot → 系统载体 Bot（存量部署）→ 名下任一活跃服务。
    """
    repo = ServiceRepository()
    active = [s for s in await repo.list_by_identity(identity.id) if s.is_active]

    own_bot_ids = {
        bot.id
        for bot in await BotConfigRepository().list_by_account(identity.account_id)
        if bot.platform == "web" and not bot.is_blocked
    }
    for svc in active:
        if svc.bot_id in own_bot_ids:
            return svc

    system_bot_id = settings.web_bot_id.strip()
    if system_bot_id:
        for svc in active:
            if svc.bot_id == system_bot_id:
                return svc

    if active:
        return active[0]
    raise AuthError("no_service", "账号尚未绑定对话服务，请联系管理员", 403)


async def require_account_service(
    principal: WebPrincipal, service_id: str
) -> ServiceInDB:
    """按账号归属取回服务，供 C 端跨身份读聊使用。

    归属口径是 account_id：同账号的网页渠道身份可以使用任一服务（含 QQ/微信渠道）。
    跨身份访问记审计日志，便于追溯网页侧读取渠道身份数据的操作。
    """
    svc = await ServiceRepository().find_by_id(service_id)
    if svc is None or svc.account_id != principal.account_id:
        if svc is not None:
            logger.warning(
                "越权尝试：登录账号 %s 试图使用非本账号服务 service=%s（归属账号 %s）",
                principal.username,
                service_id,
                svc.account_id,
            )
        raise AuthError("not_found", "服务不存在", 404)
    if svc.identity_id != principal.identity_id:
        logger.warning(
            "跨身份访问：登录账号 %s 使用同账号服务 %s（归属身份 %s）",
            principal.username,
            svc.id,
            svc.identity_id,
        )
    return svc


class CaptchaRequest(BaseModel):
    """需要图形验证码的请求公共字段。"""

    captcha_id: str = ""
    captcha_answer: str = ""


class RegisterRequest(CaptchaRequest):
    """注册请求体。"""

    username: str
    password: str
    terms_accepted: bool = False
    privacy_accepted: bool = False
    age_confirmed: bool = False


class LoginRequest(CaptchaRequest):
    """登录请求体。"""

    username: str
    password: str


def _error(exc: AuthError | RateLimited) -> JSONResponse:
    """把业务异常转成 JSON 响应（保留 code 供前端分支处理）。"""
    status_code = 429 if isinstance(exc, RateLimited) else exc.status_code
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
    return JSONResponse(
        status_code=status_code,
        content={"code": exc.scope if isinstance(exc, RateLimited) else exc.code,
                 "detail": exc.message},
        headers=headers,
    )


def _guard_captcha(req: CaptchaRequest) -> None:
    """验证码校验：开关关闭直接放行，否则一次性消费。"""
    if not settings.web_captcha_enabled:
        return
    if not captcha_service.verify(req.captcha_id, req.captcha_answer):
        raise AuthError("captcha_failed", "验证码错误或已失效，请重新获取", 400)


# 验证码接口本身也是资源：每次调用都要生成图片并在内存里挂一份答案，
# 不限流就是给人免费刷内存的入口（登录/注册有 `web_rate_limit_*`，这一项此前漏了）。
_CAPTCHA_RATE_LIMIT = 30


@auth_router.get("/captcha")
async def get_captcha(request: Request) -> Any:
    """获取图形验证码（返回 base64 内联图片，无需二次请求图片资源）。"""
    try:
        if not settings.web_auth_enabled:
            raise AuthError("web_auth_disabled", "网页登录功能未开放", 403)
        check_rate_limit("captcha", client_ip(request), _CAPTCHA_RATE_LIMIT)
        captcha_id, image = captcha_service.generate()
    except RateLimited as exc:
        return _error(AuthError("rate_limited", exc.message, 429))
    except AuthError as exc:
        return _error(exc)
    return {"captcha_id": captcha_id, "image": image}


@auth_router.post("/register")
async def register(req: RegisterRequest, request: Request) -> JSONResponse:
    """自助注册：限流 → 验证码 → 原子建号，成功后按审核开关返回 pending/active。"""
    try:
        check_rate_limit("register", client_ip(request), settings.web_rate_limit_register)
        _guard_captcha(req)
        credential = await auth_service.register(
            username=req.username,
            password=req.password,
            terms_accepted=req.terms_accepted,
            privacy_accepted=req.privacy_accepted,
            age_confirmed=req.age_confirmed,
            ip=client_ip(request),
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    need_review = credential.status == "pending"
    return JSONResponse(
        status_code=201,
        content={
            "username": credential.username,
            "status": credential.status,
            "detail": "注册成功，等待管理员审核" if need_review else "注册成功，请登录",
        },
    )


@auth_router.post("/login")
async def login(req: LoginRequest, request: Request) -> JSONResponse:
    """登录：限流 → 验证码 → 校验凭据与状态 → 签发 Bearer 令牌。"""
    try:
        check_rate_limit("login", client_ip(request), settings.web_rate_limit_login)
        _guard_captcha(req)
        result = await auth_service.login(
            username=req.username,
            password=req.password,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    return JSONResponse(
        status_code=200,
        content={
            "token": result.token,
            "username": result.credential.username,
            "expires_in_hours": settings.web_session_hours,
        },
    )


@auth_router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """注销当前令牌（幂等：无有效令牌也返回成功）。"""
    try:
        credential, _ = await auth_service.resolve_token(bearer_token(request))
    except AuthError as exc:
        return _error(exc)
    await auth_service.logout(bearer_token(request))
    return JSONResponse(status_code=200, content={"ok": True, "username": credential.username})


@auth_router.get("/me")
async def me(request: Request) -> JSONResponse:
    """当前登录账号信息：网页聊天页据此渲染账号资料与额度余量。"""
    try:
        identity = await require_current_identity(request)
    except AuthError as exc:
        return _error(exc)
    quota = await QuotaRepository().find_by_account(identity.account_id)
    return JSONResponse(
        status_code=200,
        content={
            "username": identity.username,
            "identity_id": identity.identity_id,
            "bot_id": identity.bot_id,
            "service_id": identity.service_id,
            "quota": None
            if quota is None
            else {
                "text": {"limit": quota.text_quota.limit, "used": quota.text_quota.used},
                "voice": {"limit": quota.voice_quota.limit, "used": quota.voice_quota.used},
                "image": {"limit": quota.image_quota.limit, "used": quota.image_quota.used},
            },
        },
    )
