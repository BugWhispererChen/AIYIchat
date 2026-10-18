"""C 端网页用户自助对话服务管理 API。

服务是"用户 × Bot × 角色"的对话装配单元，决定聊天时扮演谁、记忆与额度归属。

安全边界：
- 归属字段（account_id / user_id / bot_id）一律由会话令牌与服务端配置写入，
  客户端只能选择角色与名称：网页用户统一使用系统 web Bot（`settings.web_bot_id`），
  自选 Bot 会把平台凭据托管问题暴露给 C 端，故不开放；
- 角色必须对该账号可见（公共角色或本人私有角色）且处于启用状态；
- 数量受 `settings.web_max_services` 约束；
- 始终保留至少一个启用中的服务，否则账号会失去聊天能力。
"""

import logging
from typing import Optional
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth_api import AuthError, WebUser, _error, require_web_user
from app.config import settings
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceCreate, ServiceInDB, ServiceUpdate

logger = logging.getLogger(__name__)

web_services_router = APIRouter()
_service_repo = ServiceRepository()
_profile_repo = ProfileRepository()

_MAX_NAME_LEN = 60


class WebServiceCreatePayload(BaseModel):
    """新建服务的请求体：只允许指定角色与可选名称/时区。"""

    profile_id: str
    name: str = ""
    timezone: str = ""


class WebServiceUpdatePayload(BaseModel):
    """编辑服务的请求体：字段缺省表示不修改。"""

    profile_id: Optional[str] = None
    name: Optional[str] = None
    timezone: Optional[str] = None
    is_active: Optional[bool] = None


def _bad(code: str, detail: str, status_code: int = 400) -> JSONResponse:
    """统一的业务错误响应。"""
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


def _timezone_error(tz_name: str) -> str:
    """校验 IANA 时区名，非法时返回给用户的原因。"""
    if not tz_name:
        return ""
    try:
        ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - 名称非法与 tzdata 缺失同样归为不可用
        return f"时区名称无效：{tz_name}"
    return ""


async def _resolve_profile(profile_id: str) -> Optional[PromptProfileInDB]:
    """按 ObjectId 或 slug 解析角色（历史数据里 profile_id 有两种形态）。"""
    if not profile_id:
        return None
    if ObjectId.is_valid(profile_id):
        found = await _profile_repo.find_by_id(profile_id)
        if found is not None:
            return found
    return await _profile_repo.find_by_slug(profile_id)


async def _visible_profile_or_error(
    web_user: WebUser, profile_ref: str
) -> tuple[Optional[PromptProfileInDB], Optional[JSONResponse]]:
    """取回该账号可用（可见且启用）的角色，否则给出可读错误。"""
    profile = await _resolve_profile(profile_ref)
    if profile is None:
        return None, _bad("profile_not_found", "所选角色不存在", 404)
    if profile.visibility == "private" and profile.account_id != web_user.account_id:
        # 与"不存在"同响应：他人的私有角色不可枚举
        logger.warning(
            "越权尝试：登录身份 %s 试图绑定非本人角色 profile=%s",
            web_user.username,
            profile_ref,
        )
        return None, _bad("profile_not_found", "所选角色不存在", 404)
    if not profile.is_active:
        return None, _bad("profile_disabled", "所选角色已被停用，换一个或先启用它", 409)
    return profile, None


def _service_item(
    svc: ServiceInDB, profile_name: str, current_id: str, profile_ref: str = ""
) -> dict:
    """服务列表项：附带角色名称与"是否当前服务"标记。

    `profile_ref` 回填解析后的角色 ID：历史数据里 profile_id 存的是 slug，
    直接回给前端会让角色下拉框匹配不上、保存时误改角色。
    """
    return {
        "id": svc.id,
        "name": svc.name,
        "profile_id": profile_ref or svc.profile_id,
        "profile_name": profile_name,
        "bot_id": svc.bot_id,
        "is_active": svc.is_active,
        "timezone": svc.timezone,
        "current": svc.id == current_id,
        "created_at": svc.created_at.isoformat(),
    }


async def _items_for(web_user: WebUser) -> list[dict]:
    """把账号下所有服务渲染为前端条目（角色名按需解析，失败给占位）。"""
    services = await _service_repo.list_by_account(web_user.account_id)
    out: list[dict] = []
    for svc in services:
        profile = await _resolve_profile(svc.profile_id)
        out.append(
            _service_item(
                svc,
                profile.name if profile else "（角色已失效）",
                web_user.service_id,
                profile.id if profile else "",
            )
        )
    return out


def _web_bot_or_error() -> tuple[str, Optional[JSONResponse]]:
    """取系统 web Bot；未配置时提示联系管理员（服务必须绑定 Bot）。"""
    bot_id = settings.web_bot_id.strip()
    if not bot_id:
        return "", _bad(
            "web_bot_missing", "系统未配置网页对话 Bot，请联系管理员", 503
        )
    return bot_id, None


async def _require_owned_service(
    web_user: WebUser, service_id: str
) -> tuple[Optional[ServiceInDB], Optional[JSONResponse]]:
    """取回归本人的服务；不存在或属于他人时返回错误响应。"""
    svc = await _service_repo.find_by_id(service_id)
    if svc is None or svc.account_id != web_user.account_id or svc.user_id != web_user.user_id:
        if svc is not None:
            logger.warning(
                "越权尝试：登录身份 %s 试图操作他人服务 service=%s",
                web_user.username,
                service_id,
            )
        return None, _bad("not_found", "服务不存在", 404)
    return svc, None


@web_services_router.get("")
async def list_web_services(request: Request) -> JSONResponse:
    """列出本人所有对话服务（聊天页的角色切换器数据源）。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    items = await _items_for(web_user)
    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "current_service_id": web_user.service_id,
            "can_create": settings.web_max_services > 0,
            "quota": {"limit": settings.web_max_services, "used": len(items)},
        },
    )


@web_services_router.post("", status_code=201)
async def create_web_service(
    request: Request, payload: WebServiceCreatePayload
) -> JSONResponse:
    """新建服务：绑定系统 web Bot + 该账号可见的角色，归属字段由服务端写入。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    if settings.web_max_services <= 0:
        return _bad("web_service_disabled", "自建服务功能未开放", 403)

    bot_id, err = _web_bot_or_error()
    if err is not None:
        return err

    profile, err = await _visible_profile_or_error(web_user, payload.profile_id)
    if err is not None or profile is None:
        return err or _bad("profile_not_found", "所选角色不存在", 404)

    existing = await _service_repo.list_by_account(web_user.account_id)
    if len(existing) >= settings.web_max_services:
        return _bad(
            "service_quota_exceeded",
            f"最多可创建 {settings.web_max_services} 个服务，请先删除不用的",
            409,
        )

    name = payload.name.strip()[:_MAX_NAME_LEN] or f"web:{web_user.username}-{profile.name}"
    tz_err = _timezone_error(payload.timezone.strip())
    if tz_err:
        return _bad("invalid_timezone", tz_err)

    # 同用户同 Bot 允许多个服务：聊天接口显式携带 service_id 选择服务，
    # 不依赖 (用户, Bot) 的唯一性
    svc = await _service_repo.create(
        ServiceCreate(
            name=name,
            account_id=web_user.account_id,
            user_id=web_user.user_id,
            bot_id=bot_id,
            profile_id=profile.id,
            timezone=payload.timezone.strip(),
        )
    )
    logger.info(
        "网页用户 %s 创建服务 %s（角色=%s，当前 %d/%d）",
        web_user.username,
        svc.name,
        profile.name,
        len(existing) + 1,
        settings.web_max_services,
    )
    return JSONResponse(
        status_code=201, content=_service_item(svc, profile.name, web_user.service_id)
    )


@web_services_router.put("/{service_id}")
async def update_web_service(
    request: Request, service_id: str, payload: WebServiceUpdatePayload
) -> JSONResponse:
    """编辑本人服务：改角色 / 名称 / 时区 / 启停。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    svc, err = await _require_owned_service(web_user, service_id)
    if err is not None or svc is None:
        return err or _bad("not_found", "服务不存在", 404)

    update = ServiceUpdate()
    profile_name = ""

    if payload.profile_id is not None and payload.profile_id.strip():
        profile, err = await _visible_profile_or_error(
            web_user, payload.profile_id.strip()
        )
        if err is not None or profile is None:
            return err or _bad("profile_not_found", "所选角色不存在", 404)
        update.profile_id = profile.id
        profile_name = profile.name
    else:
        current = await _resolve_profile(svc.profile_id)
        profile_name = current.name if current else "（角色已失效）"

    if payload.name is not None:
        update.name = payload.name.strip()[:_MAX_NAME_LEN]
    if payload.timezone is not None:
        tz = payload.timezone.strip()
        tz_err = _timezone_error(tz)
        if tz_err:
            return _bad("invalid_timezone", tz_err)
        update.timezone = tz
    if payload.is_active is not None and payload.is_active != svc.is_active:
        # 停用前确认不是最后一个启用中的服务：否则账号直接失去聊天能力
        if not payload.is_active:
            siblings = await _service_repo.list_by_account(web_user.account_id)
            active_others = [s for s in siblings if s.id != svc.id and s.is_active]
            if not active_others:
                return _bad(
                    "last_active_service",
                    "至少需要保留一个启用的服务才能继续聊天",
                    409,
                )
        update.is_active = payload.is_active

    saved = await _service_repo.update(svc.id, update)
    if saved is None:
        return _bad("not_found", "服务不存在", 404)
    return JSONResponse(
        status_code=200, content=_service_item(saved, profile_name, web_user.service_id)
    )


@web_services_router.delete("/{service_id}")
async def delete_web_service(request: Request, service_id: str) -> JSONResponse:
    """删除本人服务；删除后必须仍有至少一个服务，避免账号失去聊天入口。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    svc, err = await _require_owned_service(web_user, service_id)
    if err is not None or svc is None:
        return err or _bad("not_found", "服务不存在", 404)

    siblings = await _service_repo.list_by_account(web_user.account_id)
    if len(siblings) <= 1:
        return _bad(
            "last_service",
            "这是你唯一的服务，删除后将无法聊天；如需换角色请直接修改它的角色",
            409,
        )

    if not await _service_repo.delete(svc.id):
        return _bad("not_found", "服务不存在", 404)
    logger.info("网页用户 %s 删除服务 %s", web_user.username, svc.name)
    return JSONResponse(status_code=200, content={"id": svc.id, "deleted": True})
