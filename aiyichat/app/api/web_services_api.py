"""C 端网页账号自助对话服务管理 API。

服务 = 渠道 + 角色 + 身份。网页凭据只确定 account_id，客户端必须显式选择
本账号身份与角色；指定渠道时只接受本账号可管理渠道。角色必须可见且启用，
数量受自助配额约束。零服务是合法状态：用户可以停用或删除最后一个服务，
随后按引导重新创建。
"""

import logging
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth_api import AuthError, WebAccountPrincipal, _error, require_web_account
from app.config import settings
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.bot_config import BotConfigInDB
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceInDB, ServiceUpdate
from app.services import web_channel_service
from app.services.auth_service import ensure_account_web_bot
from app.services.service_cleanup import purge_service_memory
from app.services.web_channel_service import ChannelError

logger = logging.getLogger(__name__)

web_services_router = APIRouter()
_service_repo = ServiceRepository()
_profile_repo = ProfileRepository()
_bot_config_repo = BotConfigRepository()

_MAX_NAME_LEN = 60


class WebServiceCreatePayload(BaseModel):
    """新建服务：身份与角色必选，可指定已有渠道。"""

    profile_id: str
    identity_id: str = ""  # 鉴权后由装配核心判必填
    name: str = ""
    bot_id: str = ""


class WebServiceUpdatePayload(BaseModel):
    """编辑服务的请求体：字段缺省表示不修改。"""

    profile_id: Optional[str] = None
    name: Optional[str] = None
    is_active: Optional[bool] = None


def _bad(code: str, detail: str, status_code: int = 400) -> JSONResponse:
    """统一的业务错误响应。"""
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


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
    principal: WebAccountPrincipal, profile_ref: str
) -> tuple[Optional[PromptProfileInDB], Optional[JSONResponse]]:
    """取回该账号可用（可见且启用）的角色，否则给出可读错误。"""
    profile = await _resolve_profile(profile_ref)
    if profile is None:
        return None, _bad("profile_not_found", "所选角色不存在", 404)
    if profile.visibility == "private" and profile.account_id != principal.account_id:
        # 与"不存在"同响应：他人的私有角色不可枚举
        logger.warning(
            "越权尝试：登录账号 %s 试图绑定非本人角色 profile=%s",
            principal.username,
            profile_ref,
        )
        return None, _bad("profile_not_found", "所选角色不存在", 404)
    if not profile.is_active:
        return None, _bad("profile_disabled", "所选角色已被停用，换一个或先启用它", 409)
    return profile, None


def _service_item(
    svc: ServiceInDB,
    profile_name: str,
    current_id: str,
    profile_ref: str = "",
    bot_platform: str = "",
    bot_name: str = "",
    bot_origin: str = "",
    bot_manageable: bool = False,
    bot_state: str = "",
    identity_name: str = "",
) -> dict:
    """服务列表项：附带角色名称、渠道与"是否当前服务"标记。

    `profile_ref` 回填解析后的角色 ID：历史数据里 profile_id 存的是 slug，
    直接回给前端会让角色下拉框匹配不上、保存时误改角色。
    """
    return {
        "id": svc.id,
        "name": svc.name,
        "profile_id": profile_ref or svc.profile_id,
        "profile_name": profile_name,
        "identity_id": svc.identity_id,
        "identity_name": identity_name,
        "bot_id": svc.bot_id,
        "bot_platform": bot_platform,
        "bot_name": bot_name,
        "bot_origin": bot_origin,
        "bot_manageable": bot_manageable,
        "bot_state": bot_state,
        "is_active": svc.is_active,
        "current": svc.id == current_id,
        "created_at": svc.created_at.isoformat(),
    }


def _bot_fields(
    bot: BotConfigInDB | None, account_id: str
) -> dict:
    """把渠道对象转成服务条目需要的渠道字段。"""
    if bot is None:
        return {
            "bot_platform": "",
            "bot_name": "",
            "bot_origin": "",
            "bot_manageable": False,
            "bot_state": "",
        }
    return {
        "bot_platform": bot.platform,
        "bot_name": bot.name,
        "bot_origin": bot.origin,
        "bot_manageable": web_channel_service.is_managed(bot, account_id),
        "bot_state": web_channel_service.compute_state(bot),
    }


async def _items_for(principal: WebAccountPrincipal) -> list[dict]:
    """把账号服务渲染为包含角色、渠道和实际身份的条目。"""
    services = await _service_repo.list_by_account(principal.account_id)
    bots: dict[str, BotConfigInDB | None] = {}
    identity_names: dict[str, str] = {}
    identity_repo = IdentityRepository()
    out: list[dict] = []
    for svc in services:
        if svc.bot_id not in bots:
            bots[svc.bot_id] = await _bot_config_repo.find_by_id(svc.bot_id)
        if svc.identity_id not in identity_names:
            identity = await identity_repo.find_by_id(svc.identity_id)
            identity_names[svc.identity_id] = (
                (identity.name or identity.nickname) if identity else "（身份已失效）"
            )
        bot = bots[svc.bot_id]
        profile = await _resolve_profile(svc.profile_id)
        out.append(
            _service_item(
                svc,
                profile.name if profile else "（角色已失效）",
                "",
                profile.id if profile else "",
                identity_name=identity_names[svc.identity_id],
                **_bot_fields(bot, principal.account_id),
            )
        )
    return out


async def _require_owned_service(
    principal: WebAccountPrincipal, service_id: str
) -> tuple[Optional[ServiceInDB], Optional[JSONResponse]]:
    """取回本账号名下的服务；不存在或属于他人时返回错误响应。

    编辑/删除按账号归属放开：账号下的渠道服务（QQ/微信）也可在网页管理。
    """
    svc = await _service_repo.find_by_id(service_id)
    if svc is None or svc.account_id != principal.account_id:
        if svc is not None:
            logger.warning(
                "越权尝试：登录账号 %s 试图操作非本账号服务 service=%s",
                principal.username,
                service_id,
            )
        return None, _bad("not_found", "服务不存在", 404)
    return svc, None


@web_services_router.get("")
async def list_web_services(request: Request) -> JSONResponse:
    """列出本人所有对话服务（聊天页的角色切换器数据源）。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)

    items = await _items_for(principal)
    quota = await web_channel_service.web_quota(principal.account_id)
    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "can_create": quota["limit"] > 0,
            "quota": {"limit": quota["limit"], "used": quota["services"]},
            "bot_quota": {"limit": quota["limit"], "used": quota["bots"]},
        },
    )


@web_services_router.post("", status_code=201)
async def create_web_service(
    request: Request, payload: WebServiceCreatePayload
) -> JSONResponse:
    """在指定或账号默认网页渠道上创建服务。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)
    if settings.web_max_services <= 0:
        return _bad("web_service_disabled", "自建服务功能未开放", 403)

    try:
        bot_id = payload.bot_id.strip() or await ensure_account_web_bot(
            principal.account_id, name="网页助手"
        )
        svc, profile, bot = await web_channel_service.create_service_for_bot(
            account_id=principal.account_id,
            bot_id=bot_id,
            identity_id=payload.identity_id.strip(),
            profile_id=payload.profile_id.strip(),
            name=payload.name.strip(),
        )
    except AuthError as exc:
        return _error(exc)
    except ChannelError as exc:
        return _bad(exc.code, exc.message, exc.status_code)

    identity = await IdentityRepository().find_by_id(svc.identity_id)
    return JSONResponse(
        status_code=201,
        content=_service_item(
            svc,
            profile.name,
            "",
            identity_name=(identity.name or identity.nickname) if identity else "",
            **_bot_fields(bot, principal.account_id),
        ),
    )


@web_services_router.put("/{service_id}")
async def update_web_service(
    request: Request, service_id: str, payload: WebServiceUpdatePayload
) -> JSONResponse:
    """编辑本人服务：改角色 / 名称 / 时区 / 启停。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)

    svc, err = await _require_owned_service(principal, service_id)
    if err is not None or svc is None:
        return err or _bad("not_found", "服务不存在", 404)

    update = ServiceUpdate()
    profile_name = ""

    if payload.profile_id is not None and payload.profile_id.strip():
        profile, err = await _visible_profile_or_error(
            principal, payload.profile_id.strip()
        )
        if err is not None or profile is None:
            return err or _bad("profile_not_found", "所选角色不存在", 404)
        update.profile_id = profile.id
        profile_name = profile.name
    else:
        current = await _resolve_profile(svc.profile_id)
        profile_name = current.name if current else "（角色已失效）"

    if payload.name is not None:
        clean_name = payload.name.strip()
        if len(clean_name) > _MAX_NAME_LEN:
            return _bad("invalid_name", f"服务名称最多 {_MAX_NAME_LEN} 个字符")
        update.name = clean_name
    if payload.is_active is not None and payload.is_active != svc.is_active:
        update.is_active = payload.is_active

    saved = await _service_repo.update(svc.id, update)
    if saved is None:
        return _bad("not_found", "服务不存在", 404)
    bot = await _bot_config_repo.find_by_id(saved.bot_id)
    identity = await IdentityRepository().find_by_id(saved.identity_id)
    return JSONResponse(
        status_code=200,
        content=_service_item(
            saved,
            profile_name,
            "",
            identity_name=(identity.name or identity.nickname) if identity else "",
            **_bot_fields(bot, principal.account_id),
        ),
    )


@web_services_router.delete("/{service_id}")
async def delete_web_service(request: Request, service_id: str) -> JSONResponse:
    """删除本人服务；允许删到零服务，随后按引导重新创建。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)

    svc, err = await _require_owned_service(principal, service_id)
    if err is not None or svc is None:
        return err or _bad("not_found", "服务不存在", 404)

    # 先清记忆/快照再删服务行：反过来会让服务行消失后这些数据再也无法定位
    try:
        await purge_service_memory([svc])
    except Exception:  # noqa: BLE001 - 清理失败时保留服务，便于用户重试
        logger.exception("删除服务前清理记忆失败: service=%s", svc.id)
        return _bad("cleanup_failed", "清理服务记忆失败，请稍后重试", 500)

    if not await _service_repo.delete(svc.id):
        return _bad("not_found", "服务不存在", 404)
    logger.info("网页账号 %s 删除服务 %s", principal.username, svc.name)
    return JSONResponse(status_code=200, content={"id": svc.id, "deleted": True})
