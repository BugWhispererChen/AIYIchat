"""C 端账号下的对话身份自助管理接口。

身份是账号下独立可管理的资源：承载对话归属、对方资料、历史与记忆。
列表/编辑/删除均按 `require_web_account` 取账号并校验归属，跨账号一律按
「不存在」处理（记录越权告警但不泄露资源信息），业务错误不使用会触发前端
踢下线的 401。
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth_api import AuthError, WebAccountPrincipal, _error, require_web_account
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.identity import IdentityCreate, IdentityInDB, IdentityUpdate
from app.services.service_provisioning import IdentityOption, list_identity_options

logger = logging.getLogger(__name__)

web_identity_router = APIRouter()


class WebIdentityCreatePayload(BaseModel):
    """创建无平台绑定的普通身份。"""

    name: str = Field(min_length=1, max_length=60)
    nickname: str = Field(default="", max_length=30)
    self_description: str = Field(default="", max_length=600)
    is_minor: bool = False


class WebIdentityUpdatePayload(BaseModel):
    """编辑身份资料：仅开放 name / nickname / self_description / is_minor。

    `account_id`、`platform`、`platform_id`、禁用状态等受控字段不接受请求体写入。
    """

    name: str | None = Field(default=None, max_length=60)
    nickname: str | None = Field(default=None, max_length=30)
    self_description: str | None = Field(default=None, max_length=600)
    is_minor: bool | None = None


def _identity_item(option: IdentityOption) -> dict:
    identity = option.identity
    return {
        "id": identity.id,
        "name": identity.name or identity.nickname or "未命名身份",
        "nickname": identity.nickname,
        "self_description": identity.self_description,
        "is_minor": identity.is_minor,
        "platform": identity.platform or "",
        "bound": option.bound,
        "external_bot_id": option.external_bot_id,
        "external_platform": option.external_platform,
        "is_blocked": identity.is_blocked,
        "service_count": option.service_count,
    }


def _bad(code: str, detail: str, status_code: int = 400) -> JSONResponse:
    """统一的业务错误响应。"""
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


async def _require_owned_identity(
    principal: WebAccountPrincipal, identity_id: str
) -> tuple[IdentityInDB | None, JSONResponse | None]:
    """取回本账号身份；不存在或属于他人时返回 404（越权写告警日志）。"""
    identity = await IdentityRepository().find_by_id(identity_id)
    if identity is None or identity.account_id != principal.account_id:
        if identity is not None:
            logger.warning(
                "越权尝试：登录账号 %s 试图操作非本账号身份 identity=%s",
                principal.username,
                identity_id,
            )
        return None, _bad("not_found", "身份不存在", 404)
    return identity, None


@web_identity_router.get("")
async def list_web_identity_options(request: Request) -> JSONResponse:
    """列出本账号身份及其外部渠道占用、服务引用数。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)
    options = await list_identity_options(principal.account_id)
    return JSONResponse(
        status_code=200,
        content={"items": [_identity_item(option) for option in options]},
    )


@web_identity_router.post("", status_code=201)
async def create_web_identity(
    request: Request, payload: WebIdentityCreatePayload
) -> JSONResponse:
    """显式创建普通身份，不关联网页登录或网页渠道。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)
    name = payload.name.strip()
    if not name:
        return _bad("invalid_name", "身份名称不能为空")
    identity = await IdentityRepository().create(
        IdentityCreate(
            account_id=principal.account_id,
            name=name,
            nickname=payload.nickname.strip() or name,
            self_description=payload.self_description.strip(),
            is_minor=payload.is_minor,
        )
    )
    return JSONResponse(
        status_code=201,
        content=_identity_item(IdentityOption(identity=identity)),
    )


@web_identity_router.put("/{identity_id}")
async def update_web_identity(
    request: Request, identity_id: str, payload: WebIdentityUpdatePayload
) -> JSONResponse:
    """编辑本人身份资料；受控字段（归属、平台绑定、禁用状态）不可改写。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)

    identity, err = await _require_owned_identity(principal, identity_id)
    if err is not None or identity is None:
        return err or _bad("not_found", "身份不存在", 404)

    update = IdentityUpdate()
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            return _bad("invalid_name", "身份名称不能为空")
        update.name = name
    if payload.nickname is not None:
        update.nickname = payload.nickname.strip()
    if payload.self_description is not None:
        update.self_description = payload.self_description.strip()
    if payload.is_minor is not None:
        update.is_minor = payload.is_minor

    saved = await IdentityRepository().update(identity.id, update)
    if saved is None:
        return _bad("not_found", "身份不存在", 404)
    options = await list_identity_options(principal.account_id)
    option = next(
        (item for item in options if item.identity.id == saved.id),
        IdentityOption(identity=saved),
    )
    return JSONResponse(status_code=200, content=_identity_item(option))


@web_identity_router.delete("/{identity_id}")
async def delete_web_identity(request: Request, identity_id: str) -> JSONResponse:
    """删除本人身份；仍被任一服务（含已停用）引用时拒绝，级联清理关联数据。"""
    try:
        principal = await require_web_account(request)
    except AuthError as exc:
        return _error(exc)

    identity, err = await _require_owned_identity(principal, identity_id)
    if err is not None or identity is None:
        return err or _bad("not_found", "身份不存在", 404)

    service_count = await ServiceRepository().count_by_identity(identity.id)
    if service_count > 0:
        return _bad(
            "identity_in_use",
            f"该身份仍被 {service_count} 个服务使用，请先删除或改绑这些服务",
            409,
        )

    # 与管理端同一语义：连带对话、主动消息等数据一起清理，避免留下孤儿
    stats = await IdentityRepository().delete_cascade(identity.id)
    logger.info("网页账号 %s 删除身份 %s：%s", principal.username, identity.id, stats)
    return JSONResponse(status_code=200, content={"id": identity.id, "deleted": True})
