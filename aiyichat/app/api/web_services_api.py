"""C 端网页账号自助对话服务管理 API。

服务是"身份 × 渠道 Bot × 角色"的对话装配单元，决定聊天时扮演谁、记忆与额度归属。

安全边界：
- 归属字段（account_id / identity_id）一律由会话令牌与服务端写入，客户端只能
  选择角色、渠道与名称：指定渠道时只接受本账号可管理的渠道，管理员后台创建的
  QQ / 微信渠道不开放给 C 端；Web 服务挂账号自己的网页渠道（无则自动创建，回落
  到系统载体渠道）；
- 角色必须对该账号可见（公共角色或本人私有角色）且处于启用状态；
- 数量受 `settings.web_max_services` 约束，渠道与服务共用同一上限；
- 始终保留至少一个启用中的服务，且当前登录账号不能失去最后一个活跃服务，
  否则账号会失去聊天能力。
"""

import logging
from typing import Optional
from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.auth_api import AuthError, WebPrincipal, _error, require_current_identity
from app.config import settings
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.bot_config import BotConfigInDB
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceCreate, ServiceInDB, ServiceUpdate
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
    """新建服务的请求体：角色必填，可选指定渠道。"""

    profile_id: str
    name: str = ""
    timezone: str = ""
    bot_id: str = ""


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
    """校验 IANA 时区名，非法时返回给身份的原因。"""
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
    principal: WebPrincipal, profile_ref: str
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
        "bot_id": svc.bot_id,
        "bot_platform": bot_platform,
        "bot_name": bot_name,
        "bot_origin": bot_origin,
        "bot_manageable": bot_manageable,
        "bot_state": bot_state,
        "is_active": svc.is_active,
        "timezone": svc.timezone,
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


async def _items_for(principal: WebPrincipal) -> list[dict]:
    """把账号下所有服务渲染为前端条目（角色名与渠道名按需解析，失败给占位）。"""
    services = await _service_repo.list_by_account(principal.account_id)
    bots: dict[str, BotConfigInDB | None] = {}
    out: list[dict] = []
    for svc in services:
        if svc.bot_id not in bots:
            bots[svc.bot_id] = await _bot_config_repo.find_by_id(svc.bot_id)
        bot = bots[svc.bot_id]
        profile = await _resolve_profile(svc.profile_id)
        out.append(
            _service_item(
                svc,
                profile.name if profile else "（角色已失效）",
                principal.service_id,
                profile.id if profile else "",
                **_bot_fields(bot, principal.account_id),
            )
        )
    return out


async def _require_owned_service(
    principal: WebPrincipal, service_id: str
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
    if svc.identity_id != principal.identity_id:
        logger.warning(
            "跨身份编辑：登录账号 %s 操作同账号服务 %s（归属身份 %s）",
            principal.username,
            svc.id,
            svc.identity_id,
        )
    return svc, None


async def _last_active_guard(
    principal: WebPrincipal, svc: ServiceInDB
) -> Optional[JSONResponse]:
    """停用/删除活跃服务前的双重保护。

    - 账号维度：账号至少要保留一个启用中的服务（既有产品规则）；
    - 登录账号维度：当前网页账号也必须保留一个启用中的服务，否则
      `require_current_identity` 会因 no_service 拒绝后续所有核心接口，
      用户会被自己的操作锁死（账号下的其他渠道身份的服务不能兜底）。
    """
    siblings = await _service_repo.list_by_account(principal.account_id)
    account_active_others = [
        s for s in siblings if s.id != svc.id and s.is_active
    ]
    if not account_active_others:
        return _bad(
            "last_active_service",
            "至少需要保留一个启用中的服务才能继续聊天",
            409,
        )
    if svc.identity_id == principal.identity_id:
        own = await _service_repo.list_by_identity(principal.identity_id)
        own_active_others = [s for s in own if s.id != svc.id and s.is_active]
        if not own_active_others:
            return _bad(
                "last_active_service",
                "当前账号至少需要保留一个启用中的服务才能继续聊天",
                409,
            )
    return None


@web_services_router.get("")
async def list_web_services(request: Request) -> JSONResponse:
    """列出本人所有对话服务（聊天页的角色切换器数据源）。"""
    try:
        principal = await require_current_identity(request)
    except AuthError as exc:
        return _error(exc)

    items = await _items_for(principal)
    quota = await web_channel_service.web_quota(
        principal.account_id, principal.identity_id
    )
    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "current_service_id": principal.service_id,
            "can_create": quota["limit"] > 0,
            "quota": {"limit": quota["limit"], "used": quota["services"]},
            "bot_quota": {"limit": quota["limit"], "used": quota["bots"]},
        },
    )


@web_services_router.post("", status_code=201)
async def create_web_service(
    request: Request, payload: WebServiceCreatePayload
) -> JSONResponse:
    """新建服务：指定渠道时走渠道装配；缺省仍挂账号 web 渠道。"""
    try:
        principal = await require_current_identity(request)
    except AuthError as exc:
        return _error(exc)

    if settings.web_max_services <= 0:
        return _bad("web_service_disabled", "自建服务功能未开放", 403)

    profile, err = await _visible_profile_or_error(principal, payload.profile_id)
    if err is not None or profile is None:
        return err or _bad("profile_not_found", "所选角色不存在", 404)

    tz = payload.timezone.strip()
    tz_err = _timezone_error(tz)
    if tz_err:
        return _bad("invalid_timezone", tz_err)

    bot_ref = payload.bot_id.strip()
    if bot_ref:
        try:
            svc, saved_profile, bot = (
                await web_channel_service.create_service_for_bot(
                    account_id=principal.account_id,
                    login_identity_id=principal.identity_id,
                    username=principal.username,
                    bot_id=bot_ref,
                    profile_id=profile.id,
                    name=payload.name.strip(),
                    timezone=tz,
                )
            )
        except ChannelError as exc:
            return _bad(exc.code, exc.message, exc.status_code)
        return JSONResponse(
            status_code=201,
            content=_service_item(
                svc,
                saved_profile.name,
                principal.service_id,
                "",
                **_bot_fields(bot, principal.account_id),
            ),
        )

    quota = await web_channel_service.web_quota(
        principal.account_id, principal.identity_id
    )
    if quota["services"] >= quota["limit"]:
        return _bad(
            "service_quota_exceeded",
            f"最多可创建 {quota['limit']} 个服务，请先删除不用的",
            409,
        )

    try:
        bot_id = await ensure_account_web_bot(principal.account_id, name="网页助手")
    except AuthError as exc:
        return _bad(exc.code, exc.message, exc.status_code)

    legacy_bot = await _bot_config_repo.find_by_id(bot_id)
    name = payload.name.strip()[:_MAX_NAME_LEN] or (
        f"{profile.name} · {legacy_bot.name}"[:_MAX_NAME_LEN]
        if legacy_bot
        else profile.name
    )

    # 同身份同 Bot 允许多个服务：聊天接口显式携带 service_id 选择服务，
    # 不依赖 (身份, Bot) 的唯一性
    svc = await _service_repo.create(
        ServiceCreate(
            name=name,
            account_id=principal.account_id,
            identity_id=principal.identity_id,
            bot_id=bot_id,
            profile_id=profile.id,
            timezone=tz,
        )
    )
    logger.info(
        "网页账号 %s 创建服务 %s（角色=%s，当前 %d/%d）",
        principal.username,
        svc.name,
        profile.name,
        quota["services"] + 1,
        quota["limit"],
    )
    return JSONResponse(
        status_code=201,
        content=_service_item(
            svc,
            profile.name,
            principal.service_id,
            "",
            **_bot_fields(legacy_bot, principal.account_id),
        ),
    )


@web_services_router.put("/{service_id}")
async def update_web_service(
    request: Request, service_id: str, payload: WebServiceUpdatePayload
) -> JSONResponse:
    """编辑本人服务：改角色 / 名称 / 时区 / 启停。"""
    try:
        principal = await require_current_identity(request)
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
        update.name = payload.name.strip()[:_MAX_NAME_LEN]
    if payload.timezone is not None:
        tz = payload.timezone.strip()
        tz_err = _timezone_error(tz)
        if tz_err:
            return _bad("invalid_timezone", tz_err)
        update.timezone = tz
    if payload.is_active is not None and payload.is_active != svc.is_active:
        # 停用前确认账号与当前网页账号都还有活跃服务（见 _last_active_guard）
        if not payload.is_active:
            guard = await _last_active_guard(principal, svc)
            if guard is not None:
                return guard
        update.is_active = payload.is_active

    saved = await _service_repo.update(svc.id, update)
    if saved is None:
        return _bad("not_found", "服务不存在", 404)
    bot = await _bot_config_repo.find_by_id(saved.bot_id)
    return JSONResponse(
        status_code=200,
        content=_service_item(
            saved,
            profile_name,
            principal.service_id,
            "",
            **_bot_fields(bot, principal.account_id),
        ),
    )


@web_services_router.delete("/{service_id}")
async def delete_web_service(request: Request, service_id: str) -> JSONResponse:
    """删除本人服务；删除活跃服务后必须仍有至少一个启用中的服务。"""
    try:
        principal = await require_current_identity(request)
    except AuthError as exc:
        return _error(exc)

    svc, err = await _require_owned_service(principal, service_id)
    if err is not None or svc is None:
        return err or _bad("not_found", "服务不存在", 404)

    if svc.is_active:
        guard = await _last_active_guard(principal, svc)
        if guard is not None:
            return guard

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
