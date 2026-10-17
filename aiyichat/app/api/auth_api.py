"""C 端网页鉴权 API：验证码、注册、登录、注销、当前用户。

安全边界：
- 所有写操作先按 IP 滑动窗口限流，再校验一次性图形验证码；
- 业务规则全部收敛在 `app/services/auth_service.py`，本层只做 HTTP ↔ 服务层转换；
- `require_web_user` 是网页会话的唯一出口，`platform_id`/`bot_id` 一律由令牌反查，
  绝不信任客户端入参。
"""

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.user_repo import UserRepository
from app.models.service import ServiceInDB
from app.models.user import UserInDB
from app.models.web_identity import WebIdentityInDB
from app.services import auth_service
from app.services.auth_service import AuthError
from app.services.captcha_service import captcha_service
from app.utils.rate_limit import RateLimited, check_rate_limit

auth_router = APIRouter()

# 反向代理（nginx）必须以 $remote_addr 覆写 X-Forwarded-For，否则客户端可自造头绕过限流
_XFF_HEADER = "x-forwarded-for"


@dataclass
class WebUser:
    """由会话令牌解析出的当前网页用户（后续接口只认这个对象）。"""

    identity: WebIdentityInDB
    user_id: str
    account_id: str
    username: str
    bot_id: str
    service_id: str


def client_ip(request: Request) -> str:
    """取客户端 IP：优先反向代理写入的 X-Forwarded-For 首值，回落直连地址。"""
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


async def require_web_user(request: Request) -> WebUser:
    """网页会话依赖：令牌有效则返回当前用户，否则 401/403。

    Raises:
        AuthError: 总开关关闭、缺/错令牌、会话过期、身份被禁用或业务服务缺失
    """
    token = bearer_token(request)
    if not token:
        raise AuthError("unauthorized", "请先登录", 401)
    identity, user_id = await auth_service.resolve_token(token)
    user = await UserRepository().find_by_id(user_id)
    if user is None or user.is_blocked:
        raise AuthError("forbidden", "账号不可用，请联系管理员", 403)
    service = await _resolve_service(user)
    return WebUser(
        identity=identity,
        user_id=user.id,
        account_id=user.account_id,
        username=identity.username,
        bot_id=service.bot_id,
        service_id=service.id,
    )


async def _resolve_service(user: UserInDB) -> ServiceInDB:
    """反查该网页用户的对话服务：优先配置的系统 Bot，回落其名下任一活跃服务。"""
    repo = ServiceRepository()
    bot_id = settings.web_bot_id.strip()
    if bot_id:
        service = await repo.find_by_user_and_bot(user.id, bot_id)
        if service is not None:
            return service
    for svc in await repo.list_by_user(user.id):
        if svc.is_active:
            return svc
    raise AuthError("no_service", "账号尚未绑定对话服务，请联系管理员", 403)


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


@auth_router.get("/captcha")
async def get_captcha() -> Any:
    """获取图形验证码（返回 base64 内联图片，无需二次请求图片资源）。"""
    try:
        if not settings.web_auth_enabled:
            raise AuthError("web_auth_disabled", "网页登录功能未开放", 403)
        captcha_id, image = captcha_service.generate()
    except AuthError as exc:
        return _error(exc)
    return {"captcha_id": captcha_id, "image": image}


@auth_router.post("/register")
async def register(req: RegisterRequest, request: Request) -> JSONResponse:
    """自助注册：限流 → 验证码 → 原子建号，成功后按审核开关返回 pending/active。"""
    try:
        check_rate_limit("register", client_ip(request), settings.web_rate_limit_register)
        _guard_captcha(req)
        identity = await auth_service.register(
            username=req.username,
            password=req.password,
            terms_accepted=req.terms_accepted,
            privacy_accepted=req.privacy_accepted,
            age_confirmed=req.age_confirmed,
            ip=client_ip(request),
        )
    except (AuthError, RateLimited) as exc:
        return _error(exc)
    need_review = identity.status == "pending"
    return JSONResponse(
        status_code=201,
        content={
            "username": identity.username,
            "status": identity.status,
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
            "username": result.identity.username,
            "expires_in_hours": settings.web_session_hours,
        },
    )


@auth_router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """注销当前令牌（幂等：无有效令牌也返回成功）。"""
    try:
        identity, _ = await auth_service.resolve_token(bearer_token(request))
    except AuthError as exc:
        return _error(exc)
    await auth_service.logout(bearer_token(request))
    return JSONResponse(status_code=200, content={"ok": True, "username": identity.username})


@auth_router.get("/me")
async def me(request: Request) -> JSONResponse:
    """当前登录用户信息：网页聊天页据此渲染身份与额度余量。"""
    try:
        user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)
    quota = await QuotaRepository().find_by_account(user.account_id)
    return JSONResponse(
        status_code=200,
        content={
            "username": user.username,
            "user_id": user.user_id,
            "bot_id": user.bot_id,
            "service_id": user.service_id,
            "quota": None
            if quota is None
            else {
                "text": {"limit": quota.text_quota.limit, "used": quota.text_quota.used},
                "voice": {"limit": quota.voice_quota.limit, "used": quota.voice_quota.used},
                "image": {"limit": quota.image_quota.limit, "used": quota.image_quota.used},
            },
        },
    )
