"""C 端账号下的对话身份选项与创建接口。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth_api import AuthError, _error, require_web_account
from app.db.repositories.identity_repo import IdentityRepository
from app.models.identity import IdentityCreate
from app.services.service_provisioning import IdentityOption, list_identity_options

web_identity_router = APIRouter()


class WebIdentityCreatePayload(BaseModel):
    """创建无平台绑定的普通身份。"""

    name: str = Field(min_length=1, max_length=60)
    nickname: str = Field(default="", max_length=30)
    self_description: str = Field(default="", max_length=600)
    is_minor: bool = False


def _identity_item(option: IdentityOption) -> dict:
    identity = option.identity
    return {
        "id": identity.id,
        "name": identity.name or identity.nickname or "未命名身份",
        "nickname": identity.nickname,
        "platform": identity.platform or "",
        "bound": option.bound,
        "external_bot_id": option.external_bot_id,
        "external_platform": option.external_platform,
        "is_blocked": identity.is_blocked,
    }


@web_identity_router.get("")
async def list_web_identity_options(request: Request) -> JSONResponse:
    """列出本账号身份及其外部渠道占用状态。"""
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
        return JSONResponse(
            status_code=400,
            content={"code": "invalid_name", "detail": "身份名称不能为空"},
        )
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
