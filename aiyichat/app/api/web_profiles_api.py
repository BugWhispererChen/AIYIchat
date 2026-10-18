"""C 端网页用户自助角色管理 API。

安全边界：
- 全部端点先经 `require_web_user` 解析会话身份，客户端不能指定归属账号；
- 用户只能创建/读写 `visibility="private"` 且 `account_id=本人账号` 的角色，
  公共角色只读，他人的私有角色不可见（列表按可见性过滤）；
- 数量受 `settings.web_max_profiles` 约束，创建文本先过内容安全检查；
- slug 由服务端生成，避免占用公共命名空间与保留名。
"""

import logging
import re
from typing import Literal, Optional
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth_api import AuthError, WebUser, _error, require_web_user
from app.config import settings
from app.conversation.safety_checker import SafetyAction, get_safety_checker
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.profile import (
    ProfileDialogueStrategy,
    ProfileIdentity,
    ProfileLanguageStyle,
    ProfilePersonality,
    PromptProfileCreate,
    PromptProfileInDB,
    PromptProfileUpdate,
)

logger = logging.getLogger(__name__)

web_profiles_router = APIRouter()
_profile_repo = ProfileRepository()
_service_repo = ServiceRepository()

# 自助角色的文本上限：够写一份人设，又不至于让单文档无限膨胀
_MAX_NAME_LEN = 30
_MAX_TEXT_LEN = 600
_MAX_META_LEN = 4000
_MAX_LIST_ITEMS = 40
# 公共角色详情里对读者无意义、且可能泄露运营信息的字段
_HIDDEN_PUBLIC_FIELDS = (
    "account_id",
    "voice_id",
    "voice_clone_target_model",
    "reference_image_url",
    "sticker_group_ids",
    "disable_reason",
)


class WebProfilePayload(BaseModel):
    """网页用户自建/编辑角色的请求体。

    不含 slug、visibility、account_id、声音与定妆照等字段：这些一律由服务端
    判定或由管理员在后台维护，接受客户端入参会打开越权与伪造面。
    """

    name: str
    category: Literal["original", "custom"] = "original"
    identity: ProfileIdentity = Field(default_factory=ProfileIdentity)
    language_style: ProfileLanguageStyle = Field(default_factory=ProfileLanguageStyle)
    personality: ProfilePersonality = Field(default_factory=ProfilePersonality)
    dialogue_strategy: ProfileDialogueStrategy = Field(default_factory=ProfileDialogueStrategy)
    meta_instructions: str = ""
    verbosity: Literal["concise", "standard", "verbose"] = "standard"


def _bad(code: str, detail: str, status_code: int = 400) -> JSONResponse:
    """统一的业务错误响应。"""
    return JSONResponse(status_code=status_code, content={"code": code, "detail": detail})


def _profile_item(profile: PromptProfileInDB, account_id: str) -> dict:
    """把角色档案压成前端列表需要的字段（并标出是否本人自建）。"""
    return {
        "id": profile.id,
        "slug": profile.slug,
        "name": profile.name,
        "category": profile.category,
        "is_active": profile.is_active,
        "visibility": profile.visibility,
        "owned": profile.visibility == "private" and profile.account_id == account_id,
        "updated_at": profile.updated_at.isoformat(),
    }


def _feature_closed() -> Optional[JSONResponse]:
    """自建角色能力关闭时返回拒绝响应（配额设为 0 即关闭）。"""
    if settings.web_max_profiles > 0:
        return None
    return _bad("web_profile_disabled", "自建角色功能未开放", 403)


def _text_violation(payload: WebProfilePayload) -> str:
    """对整份角色文本做内容安全检查，命中则返回给用户的原因。"""
    verdict = get_safety_checker().check_input(payload.model_dump_json())
    if verdict.action == SafetyAction.PASS:
        return ""
    if verdict.action == SafetyAction.CRISIS:
        return "角色描述中包含不适合创建的内容，请调整后重试"
    return f"角色描述包含违禁词「{verdict.matched}」，请调整后重试"


def _invalid_reason(payload: WebProfilePayload) -> str:
    """校验各文本字段长度上限，返回首个超限项的说明。"""
    if not payload.name.strip():
        return "角色名称不能为空"
    if len(payload.name) > _MAX_NAME_LEN:
        return f"角色名称最多 {_MAX_NAME_LEN} 个字符"
    if len(payload.meta_instructions) > _MAX_META_LEN:
        return f"补充指令最多 {_MAX_META_LEN} 个字符"
    long_fields = [
        payload.identity.background,
        payload.identity.appearance,
        payload.identity.era,
        payload.identity.age_appearance,
        payload.language_style.tone,
        payload.personality.care_style,
        payload.dialogue_strategy.greeting,
        payload.dialogue_strategy.safety_guardrails,
    ]
    if any(len(v) > _MAX_TEXT_LEN for v in long_fields):
        return f"单项描述最多 {_MAX_TEXT_LEN} 个字符"
    lists = [
        payload.identity.core_values,
        payload.language_style.vocabulary,
        payload.language_style.forbidden_words,
        payload.language_style.example_utterances,
        payload.dialogue_strategy.boundaries,
    ]
    if any(len(v) > _MAX_LIST_ITEMS for v in lists):
        return f"每类条目最多 {_MAX_LIST_ITEMS} 条"
    return ""


def _build_create(payload: WebProfilePayload, account_id: str, slug: str) -> PromptProfileCreate:
    """请求体 + 服务端归属字段 → 角色创建模型。"""
    return PromptProfileCreate(
        slug=slug,
        name=payload.name.strip(),
        category=payload.category,
        is_active=True,
        visibility="private",
        account_id=account_id,
        identity=payload.identity,
        language_style=payload.language_style,
        personality=payload.personality,
        dialogue_strategy=payload.dialogue_strategy,
        meta_instructions=payload.meta_instructions,
        verbosity=payload.verbosity,
        is_draft=False,
    )


def _gen_slug(username: str) -> str:
    """生成私有角色的 slug：用户名可读前缀 + 随机后缀，与公共命名空间隔离。"""
    base = re.sub(r"[^a-z0-9]", "", username.lower())[:12] or "user"
    return f"w-{base}-{uuid4().hex[:8]}"


async def _find_unique_slug(username: str) -> str:
    """取一个尚未被占用的 slug（重试次数有上限，冲突概率极低）。"""
    for _ in range(5):
        slug = _gen_slug(username)
        if await _profile_repo.find_by_slug(slug) is None:
            return slug
    return f"w-{uuid4().hex}"


async def _resolve_profile(profile_id: str) -> Optional[PromptProfileInDB]:
    """按 ObjectId 或 slug 解析角色（服务里的 profile_id 历史上有两种形态）。"""
    if ObjectId.is_valid(profile_id):
        found = await _profile_repo.find_by_id(profile_id)
        if found is not None:
            return found
    return await _profile_repo.find_by_slug(profile_id)


async def _require_owned_profile(
    web_user: WebUser, profile_id: str
) -> tuple[Optional[PromptProfileInDB], Optional[JSONResponse]]:
    """取回归本人的私有角色；不存在或不属于本人时返回对应错误响应。"""
    profile = await _resolve_profile(profile_id)
    if profile is None:
        return None, _bad("not_found", "角色不存在", 404)
    if profile.visibility != "private" or profile.account_id != web_user.account_id:
        logger.warning(
            "越权尝试：登录身份 %s 试图操作非本人角色 profile=%s",
            web_user.username,
            profile_id,
        )
        # 他人的私有角色与"不存在"同响应，避免枚举探测
        return None, _bad("not_found", "角色不存在", 404)
    return profile, None


@web_profiles_router.get("")
async def list_web_profiles(request: Request) -> JSONResponse:
    """列出可用角色：公共角色（只读）+ 本人自建私有角色（含已停用的）。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    visible = await _profile_repo.list_active(account_id=web_user.account_id)
    mine = await _profile_repo.list_by_account(web_user.account_id)
    seen = {p.id for p in visible}
    items = [_profile_item(p, web_user.account_id) for p in visible]
    # 补上本人已停用的私有角色（不在"活跃可见"集合里），便于在管理页重新启用
    items += [_profile_item(p, web_user.account_id) for p in mine if p.id not in seen]
    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "can_create": settings.web_max_profiles > 0,
            "quota": {"limit": settings.web_max_profiles, "used": len(mine)},
        },
    )


@web_profiles_router.get("/{profile_id}")
async def get_web_profile(request: Request, profile_id: str) -> JSONResponse:
    """角色详情：公共角色返回脱敏后的可读字段，私有角色仅本人可见完整描述。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    profile = await _resolve_profile(profile_id)
    if profile is None:
        return _bad("not_found", "角色不存在", 404)
    owned = profile.visibility == "private" and profile.account_id == web_user.account_id
    if profile.visibility == "private" and not owned:
        return _bad("not_found", "角色不存在", 404)

    data = profile.model_dump(by_alias=False)
    if not owned:
        # 公共角色只读：隐去运营与内部字段，保留前端展示所需的人设描述
        for key in _HIDDEN_PUBLIC_FIELDS:
            data.pop(key, None)
    return JSONResponse(status_code=200, content=data)


@web_profiles_router.post("", status_code=201)
async def create_web_profile(request: Request, payload: WebProfilePayload) -> JSONResponse:
    """创建私有角色：长度与内容校验 → 配额校验 → 服务端写入归属字段。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    closed = _feature_closed()
    if closed is not None:
        return closed

    reason = _invalid_reason(payload)
    if reason:
        return _bad("invalid_profile", reason)
    violation = _text_violation(payload)
    if violation:
        return _bad("safety_blocked", violation)

    mine = await _profile_repo.list_by_account(web_user.account_id)
    if len(mine) >= settings.web_max_profiles:
        return _bad(
            "profile_quota_exceeded",
            f"最多可创建 {settings.web_max_profiles} 个自建角色，请先删除不用的",
            409,
        )

    slug = await _find_unique_slug(web_user.username)
    profile = await _profile_repo.create(_build_create(payload, web_user.account_id, slug))
    logger.info(
        "网页用户 %s 创建私有角色 %s（slug=%s，当前 %d/%d）",
        web_user.username,
        profile.name,
        profile.slug,
        len(mine) + 1,
        settings.web_max_profiles,
    )
    return JSONResponse(status_code=201, content=_profile_item(profile, web_user.account_id))


@web_profiles_router.put("/{profile_id}")
async def update_web_profile(
    request: Request, profile_id: str, payload: WebProfilePayload
) -> JSONResponse:
    """整体替换本人私有角色的可编辑字段。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    profile, err = await _require_owned_profile(web_user, profile_id)
    if err is not None or profile is None:
        return err or _bad("not_found", "角色不存在", 404)

    reason = _invalid_reason(payload)
    if reason:
        return _bad("invalid_profile", reason)
    violation = _text_violation(payload)
    if violation:
        return _bad("safety_blocked", violation)

    saved = await _profile_repo.update(
        profile.slug,
        PromptProfileUpdate(
            name=payload.name.strip(),
            category=payload.category,
            identity=payload.identity,
            language_style=payload.language_style,
            personality=payload.personality,
            dialogue_strategy=payload.dialogue_strategy,
            meta_instructions=payload.meta_instructions,
            verbosity=payload.verbosity,
        ),
    )
    if saved is None:
        return _bad("update_failed", "角色保存异常，请重试", 500)
    return JSONResponse(status_code=200, content=_profile_item(saved, web_user.account_id))


@web_profiles_router.post("/{profile_id}/toggle")
async def toggle_web_profile(request: Request, profile_id: str) -> JSONResponse:
    """启停本人私有角色。

    停用一个正被自己服务使用的角色是允许的（该服务会暂时提示未绑定角色），
    但用户可随时启用回来；因此这里不做服务引用拦截，拦截放在删除路径。
    """
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    profile, err = await _require_owned_profile(web_user, profile_id)
    if err is not None or profile is None:
        return err or _bad("not_found", "角色不存在", 404)

    turning_off = profile.is_active
    saved = await _profile_repo.update(
        profile.slug,
        PromptProfileUpdate(
            is_active=not turning_off,
            disable_reason="用户自行停用" if turning_off else "",
        ),
    )
    if saved is None:
        return _bad("not_found", "角色不存在", 404)
    return JSONResponse(status_code=200, content=_profile_item(saved, web_user.account_id))


@web_profiles_router.delete("/{profile_id}")
async def delete_web_profile(request: Request, profile_id: str) -> JSONResponse:
    """物理删除本人私有角色（释放配额）；仍被服务引用时要求先处理服务。"""
    try:
        web_user = await require_web_user(request)
    except AuthError as exc:
        return _error(exc)

    profile, err = await _require_owned_profile(web_user, profile_id)
    if err is not None or profile is None:
        return err or _bad("not_found", "角色不存在", 404)

    # 本账号下的服务引用检查：删除会让这些服务失去角色，必须先改绑或删除服务
    services = await _service_repo.list_by_account(web_user.account_id)
    if any(s.profile_id in (profile.id, profile.slug) for s in services):
        return _bad(
            "profile_in_use",
            "该角色仍被你的服务使用，请先删除或更换对应服务中的角色",
            409,
        )

    if not await _profile_repo.hard_delete(profile.slug):
        return _bad("not_found", "角色不存在", 404)
    logger.info(
        "网页用户 %s 删除私有角色 %s（slug=%s）", web_user.username, profile.name, profile.slug
    )
    return JSONResponse(status_code=200, content={"id": profile.id, "deleted": True})
