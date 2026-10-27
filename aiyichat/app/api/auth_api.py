"""C 端网页鉴权 API：网页凭据只确定账号，对话身份由服务决定。"""

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.account import AccountInDB
from app.models.service import ServiceInDB
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
class WebAccountPrincipal:
    """网页登录主体：凭据、所属账号与当前会话。"""

    credential: WebCredentialInDB
    account: AccountInDB
    session: WebSessionInDB
    token: str

    @property
    def account_id(self) -> str:
        return self.account.id

    @property
    def username(self) -> str:
        return self.credential.username

    @property
    def nickname(self) -> str:
        return self.account.name or self.credential.username


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


async def require_web_account(request: Request) -> WebAccountPrincipal:
    """解析网页凭据与所属账号，不要求账号已有活跃服务。"""
    token = bearer_token(request)
    if not token:
        raise AuthError("unauthorized", "请先登录", 401)
    result = await auth_service.resolve_session(token)
    account = await AccountRepository().find_by_id(result.account_id)
    if account is None:
        raise AuthError("forbidden", "账号不可用，请联系管理员", 403)
    return WebAccountPrincipal(
        credential=result.credential,
        account=account,
        session=result.session,
        token=token,
    )


async def require_account_service(
    principal: WebAccountPrincipal, service_id: str
) -> ServiceInDB:
    """按账号归属取回服务；对话身份只从该服务读取。"""
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
        credential = await auth_service.resolve_token(bearer_token(request))
    except AuthError as exc:
        return _error(exc)
    await auth_service.logout(bearer_token(request))
    return JSONResponse(status_code=200, content={"ok": True, "username": credential.username})


@auth_router.get("/me")
async def me(request: Request) -> JSONResponse:
    """当前网页登录账号与额度，不包含任何对话身份。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)
    quota = await QuotaRepository().find_by_account(principal.account_id)
    return JSONResponse(
        status_code=200,
        content={
            "username": principal.username,
            "nickname": principal.nickname,
            "account_id": principal.account_id,
            "quota": None
            if quota is None
            else {
                "text": {"limit": quota.text_quota.limit, "used": quota.text_quota.used},
                "voice": {"limit": quota.voice_quota.limit, "used": quota.voice_quota.used},
                "image": {"limit": quota.image_quota.limit, "used": quota.image_quota.used},
            },
        },
    )
