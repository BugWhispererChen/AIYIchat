"""Admin 账号管理路由。"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import (
    _account_repo,
    _bot_config_repo,
    _conv_repo,
    _msg_repo,
    _profile_repo,
    _render,
    _service_repo,
    _identity_repo,
)
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.models.account import AccountCreate, AccountUpdate
from app.models.bot_config import BotConfigCreate, BotConfigInDB, BotConfigUpdate
from app.models.quota import AccountQuotaUpdate, QuotaEntry
from app.models.service import ServiceCreate, ServiceUpdate
from app.models.identity import IdentityCreate, IdentityUpdate
from app.conversation.time_context import COMMON_TIMEZONES, resolve_tz
from app.utils.redaction import mask_secret

accounts_router = APIRouter(tags=["admin"])
_quota_repo = QuotaRepository()
_memory_repo = ServiceMemoryRepository()
_quota_template_repo = QuotaTemplateRepository()


class _BotView:
    """Bot 配置的展示视图：凭据只以脱敏形态暴露，其余字段委托原对象。

    直接把 `BotConfigInDB` 交给模板会把明文 `app_secret` 写进后台 HTML，
    一张页面截图就等于交出凭据。
    """

    def __init__(self, bot: BotConfigInDB) -> None:
        self._bot = bot
        self.app_secret = ""
        self.app_secret_masked = mask_secret(bot.app_secret)
        self.has_secret = bool(bot.app_secret)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._bot, name)


def _bot_views(bots: list[BotConfigInDB]) -> list[_BotView]:
    """把 Bot 列表转成脱敏展示视图。"""
    return [_BotView(bot) for bot in bots]


async def _account_detail_with_error(
    request: Request, account_id: str, error: str
) -> Response:
    """渲染账号详情页并附带错误信息。"""
    account = await _account_repo.find_by_id(account_id)
    if not account:
        return RedirectResponse(
            url="/admin/accounts", status_code=status.HTTP_303_SEE_OTHER
        )
    quota = await _quota_repo.ensure_quota(account_id)
    if quota is None or not hasattr(quota, "text_quota"):
        unlimited = QuotaEntry(limit=10**9, used=0)
        quota = type("obj", (object,), {
            "text_quota": unlimited,
            "voice_quota": unlimited,
            "image_quota": unlimited,
        })()
    services = await _service_repo.list_by_account(account_id)
    identities = await _identity_repo.list_by_account(account_id, limit=1000)
    bots = await _bot_config_repo.list_by_account(account_id)
    profiles = await _profile_repo.list_active(account_id=account_id)
    service_infos = []
    for svc in services:
        identity = await _identity_repo.find_by_id(svc.identity_id)
        bot = await _bot_config_repo.find_by_id(svc.bot_id)
        profile = await _profile_repo.find_by_slug(svc.profile_id)
        if profile is None:
            profile = await _profile_repo.find_by_id(svc.profile_id)
        service_infos.append({
            "service": svc,
            "identity": identity,
            "bot": bot,
            "profile": profile,
        })
    return _render(request, "account_detail.html", {
        "account": account,
        "quota": quota,
        "service_infos": service_infos,
        "identities": identities,
        "bots": _bot_views(bots),
        "profiles": profiles,
        "common_timezones": COMMON_TIMEZONES,
        "active_page": "accounts",
        "error": error,
    })


@accounts_router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(
    request: Request,
    q: str = "",
    _s: str = Depends(require_admin),
) -> Response:
    """账号列表页，支持按名称或备注搜索。"""
    accounts = await _account_repo.search(q, limit=100) if q.strip() else await _account_repo.list_all(limit=100)
    account_infos = []
    for acc in accounts:
        quota = await _quota_repo.find_by_account(acc.id)
        services = await _service_repo.list_by_account(acc.id)
        account_infos.append({
            "account": acc,
            "quota": quota,
            "service_count": len(services),
        })
    return _render(request, "accounts.html", {
        "account_infos": account_infos,
        "active_page": "accounts",
    })


@accounts_router.post("/accounts/create", response_class=HTMLResponse)
async def account_create(
    request: Request, _s: str = Depends(require_admin)
) -> Response:
    """创建账号。"""
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return _render(request, "accounts.html", {
            "error": "账号名称不能为空",
            "active_page": "accounts",
        })

    account = AccountCreate(name=name, remark=str(form.get("remark", "")))
    created = await _account_repo.create(account)
    await _quota_repo.ensure_quota(created.id)
    return RedirectResponse(
        url="/admin/accounts", status_code=status.HTTP_303_SEE_OTHER
    )


@accounts_router.get("/accounts/{account_id}", response_class=HTMLResponse)
async def account_detail(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """账号详情页。"""
    account = await _account_repo.find_by_id(account_id)
    if not account:
        return RedirectResponse(
            url="/admin/accounts", status_code=status.HTTP_303_SEE_OTHER
        )

    quota = await _quota_repo.ensure_quota(account_id)
    # 防御性处理：确保额度对象结构完整，避免旧数据导致模板渲染失败
    if quota is None or not hasattr(quota, "text_quota"):
        from app.models.quota import QuotaEntry
        unlimited = QuotaEntry(limit=10**9, used=0)
        quota = type("obj", (object,), {
            "text_quota": unlimited,
            "voice_quota": unlimited,
            "image_quota": unlimited,
        })()
    services = await _service_repo.list_by_account(account_id)
    identities = await _identity_repo.list_by_account(account_id, limit=1000)
    bots = await _bot_config_repo.list_by_account(account_id)
    profiles = await _profile_repo.list_active(account_id=account_id)

    # 组装服务展示信息
    service_infos = []
    for svc in services:
        identity = await _identity_repo.find_by_id(svc.identity_id)
        # svc.bot_id 现在存储的是 Bot 的 UUID id
        bot = await _bot_config_repo.find_by_id(svc.bot_id)
        # profile_id 可能是 slug 或 ObjectId，优先按 slug 查找，失败则按 id 查找
        profile = await _profile_repo.find_by_slug(svc.profile_id)
        if profile is None:
            profile = await _profile_repo.find_by_id(svc.profile_id)
        service_infos.append({
            "service": svc,
            "identity": identity,
            "bot": bot,
            "profile": profile,
        })

    quota_templates = await _quota_template_repo.list_all()

    return _render(request, "account_detail.html", {
        "account": account,
        "quota": quota,
        "service_infos": service_infos,
        "identities": identities,
        "bots": _bot_views(bots),
        "profiles": profiles,
        "quota_templates": quota_templates,
        # 身份时区下拉（常用 IANA 列表 + 手动输入）
        "common_timezones": COMMON_TIMEZONES,
        "active_page": "accounts",
    })


@accounts_router.post("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def account_edit(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """编辑账号基本信息。"""
    form = await request.form()
    update = AccountUpdate(
        name=str(form.get("name", "")).strip() or None,
        remark=str(form.get("remark", "")).strip() or None,
    )
    await _account_repo.update(account_id, update)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/delete", response_class=HTMLResponse
)
async def account_delete(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """删除账号及其所有关联数据（级联删除）。"""
    await _account_repo.delete_cascade(account_id)
    return RedirectResponse(
        url="/admin/accounts", status_code=status.HTTP_303_SEE_OTHER
    )


@accounts_router.post(
    "/accounts/{account_id}/quota", response_class=HTMLResponse
)
async def account_quota_update(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """更新账号额度。"""
    form = await request.form()

    def _entry(field: str) -> QuotaEntry | None:
        limit = int(str(form.get(f"{field}_limit", 0)) or 0)
        used = int(str(form.get(f"{field}_used", 0)) or 0)
        return QuotaEntry(limit=limit, used=used)

    update = AccountQuotaUpdate(
        text_quota=_entry("text"),
        voice_quota=_entry("voice"),
        image_quota=_entry("image"),
    )
    await _quota_repo.create_or_update(account_id, update)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── 账号内身份管理 ─────────────────────────────────────────


@accounts_router.post(
    "/accounts/{account_id}/identities/create", response_class=HTMLResponse
)
async def account_identity_create(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """在账号下创建身份。platform_id 由系统自动生成。"""
    form = await request.form()
    platform = str(form.get("platform", "qq")).strip()
    if platform not in ("qq", "wechat", "web"):
        platform = "qq"

    await _identity_repo.create(
        IdentityCreate(
            account_id=account_id,
            name=str(form.get("name", "")).strip(),
            platform=platform,  # type: ignore[arg-type]
            nickname=str(form.get("nickname", "")).strip(),
            self_description=str(form.get("self_description", "")).strip(),
            is_minor=form.get("is_minor") == "on",
        )
    )
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/identities/{identity_id}/delete",
    response_class=HTMLResponse,
)
async def account_identity_delete(
    request: Request,
    account_id: str,
    identity_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """删除账号下的身份（需先删除关联服务）。"""
    svc_count = await _service_repo.count_by_identity(identity_id)
    if svc_count > 0:
        return await _account_detail_with_error(
            request, account_id,
            f"该身份下有 {svc_count} 个对话服务，请先删除服务",
        )
    await _identity_repo.delete(identity_id)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/identities/{identity_id}/timezone",
    response_class=HTMLResponse,
)
async def account_identity_set_timezone(
    request: Request,
    account_id: str,
    identity_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """设置身份时区（IANA 名，空串回落全局默认偏移）。

    校验失败时返回错误提示而不落库，避免无效时区名导致运行时静默回落
    且运营无从感知。校验通过 resolve_tz 试解析，不依赖 tzdata 是否已装。
    """
    form = await request.form()
    # 手动输入优先于下拉选择（运营可能输入下拉列表之外的合法 IANA 名）
    tz_name = str(form.get("timezone_custom", "")).strip() or str(
        form.get("timezone", "")
    ).strip()
    if tz_name:
        # 试解析：无效名会静默回落，这里主动检测并提示运营
        resolved, resolved_name = resolve_tz(identity_timezone=tz_name)
        if resolved_name != tz_name:
            return await _account_detail_with_error(
                request, account_id,
                f"时区名「{tz_name}」无效或无法识别，请使用 IANA 标准名（如 Asia/Shanghai）",
            )
    await _identity_repo.update(identity_id, IdentityUpdate(timezone=tz_name))
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/identities/{identity_id}/toggle-block",
    response_class=HTMLResponse,
)
async def account_identity_toggle_block(
    request: Request,
    account_id: str,
    identity_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """禁用/启用账号下的身份（禁用前需先删除关联服务）。"""
    identity = await _identity_repo.find_by_id(identity_id)
    if not identity:
        return RedirectResponse(
            url=f"/admin/accounts/{account_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    form = await request.form()
    if not identity.is_blocked:
        # 禁用：检查引用
        svc_count = await _service_repo.count_by_identity(identity_id)
        if svc_count > 0:
            return await _account_detail_with_error(
                request, account_id,
                f"该身份下有 {svc_count} 个对话服务，请先删除服务后再禁用",
            )
        reason = str(form.get("block_reason", "")).strip()
        await _identity_repo.set_blocked(identity_id, True, reason)
    else:
        # 启用
        await _identity_repo.set_blocked(identity_id, False)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── 账号内 Bot 管理 ────────────────────────────────────────


@accounts_router.post(
    "/accounts/{account_id}/bots/create", response_class=HTMLResponse
)
async def account_bot_create(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """在账号下创建 Bot（id 自动生成 UUID，name 必填）。
    微信平台走 WeChatBotManager 以注册 pending_clients。
    """
    form = await request.form()
    name = str(form.get("name", "")).strip()
    platform = str(form.get("platform", "qq")).strip()
    if platform not in ("qq", "wechat", "web"):
        platform = "qq"
    if not name:
        return RedirectResponse(
            url=f"/admin/accounts/{account_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if platform == "wechat":
        # 微信Bot创建需走Manager，自动注册pending_clients供QR码操作使用
        from app.adapters.wechat_clawbot import get_wechat_manager
        await get_wechat_manager().create_bot(name=name, account_id=account_id)
    else:
        config = BotConfigCreate(
            account_id=account_id,
            name=name,
            platform=platform,  # type: ignore[arg-type]
            app_id=str(form.get("app_id", "")).strip(),
            app_secret=str(form.get("app_secret", "")).strip(),
            sandbox_mode=form.get("sandbox_mode") == "on",
        )
        await _bot_config_repo.create(config)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/bots/{config_id}/edit",
    response_class=HTMLResponse,
)
async def account_bot_edit(
    request: Request,
    account_id: str,
    config_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """在账号详情页编辑 Bot 配置，完成后返回账号详情页。"""
    form = await request.form()
    raw_secret = str(form.get("app_secret", "")).strip()
    update = BotConfigUpdate(
        name=str(form.get("name", "")).strip() or None,
        app_id=str(form.get("app_id", "")).strip() or None,
        # 留空或仍是脱敏串（含 *）则不更新：脱敏值一旦写回库，BotManager 巡检会
        # 重启 Bot 并拿它去铸 token，平台侧随即全部认证失败。
        app_secret=None if (not raw_secret or "*" in raw_secret) else raw_secret,
        sandbox_mode=form.get("sandbox_mode") == "on",
    )
    await _bot_config_repo.update(config_id, update)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/bots/{config_id}/delete",
    response_class=HTMLResponse,
)
async def account_bot_delete(
    request: Request,
    account_id: str,
    config_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """删除账号下的 Bot（需先删除关联服务）。
    微信平台走 WeChatBotManager 以清理后台轮询任务。
    """
    svc_count = await _service_repo.count_by_bot(config_id)
    if svc_count > 0:
        return await _account_detail_with_error(
            request, account_id,
            f"该 Bot 下有 {svc_count} 个对话服务，请先删除服务",
        )
    bot = await _bot_config_repo.find_by_id(config_id)
    if bot and bot.platform == "wechat":
        # 微信Bot删除需走Manager，先停止后台任务再删除DB记录
        from app.adapters.wechat_clawbot import get_wechat_manager
        await get_wechat_manager().remove_bot(config_id)
    else:
        await _bot_config_repo.delete(config_id)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/bots/{config_id}/stop",
    response_class=HTMLResponse,
)
async def account_bot_stop(
    request: Request,
    account_id: str,
    config_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """停止微信 Bot 的消息轮询任务，将状态设为离线。"""
    from app.adapters.wechat_clawbot import get_wechat_manager

    manager = get_wechat_manager()
    await manager.cancel_bot_task(config_id)
    await _bot_config_repo.update_status(config_id, "offline")
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/bots/{config_id}/toggle-block",
    response_class=HTMLResponse,
)
async def account_bot_toggle_block(
    request: Request,
    account_id: str,
    config_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """禁用/启用账号下的 Bot（禁用前需先删除关联服务）。"""
    bot = await _bot_config_repo.find_by_id(config_id)
    if not bot:
        return RedirectResponse(
            url=f"/admin/accounts/{account_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    form = await request.form()
    if not bot.is_blocked:
        # 禁用：检查引用
        svc_count = await _service_repo.count_by_bot(config_id)
        if svc_count > 0:
            return await _account_detail_with_error(
                request, account_id,
                f"该 Bot 下有 {svc_count} 个对话服务，请先删除服务后再禁用",
            )
        reason = str(form.get("block_reason", "")).strip()
        await _bot_config_repo.set_blocked(config_id, True, reason)
    else:
        # 启用
        await _bot_config_repo.set_blocked(config_id, False)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── 账号内 Service 管理 ────────────────────────────────────


@accounts_router.post(
    "/accounts/{account_id}/services", response_class=HTMLResponse
)
async def account_service_create(
    request: Request,
    account_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """为账号添加对话服务，名称自动生成：{身份}-{角色}-{Bot名称}。"""
    form = await request.form()
    identity_id = str(form.get("identity_id", "")).strip()
    bot_id = str(form.get("bot_id", "")).strip()
    profile_id = str(form.get("profile_id", "")).strip()

    if not all([identity_id, bot_id, profile_id]):
        return RedirectResponse(
            url=f"/admin/accounts/{account_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # 1:1 约束：一个 Bot 只允许创建一个服务。
    # 网页 Bot 是载体通道，允许一个 Bot 承载多个伙伴（与 C 端自助建服务一致）。
    bot = await _bot_config_repo.find_by_id(bot_id)
    if bot is None or bot.platform != "web":
        existing_bot_svc = await _service_repo.count_by_bot(bot_id)
        if existing_bot_svc > 0:
            return await _account_detail_with_error(
                request, account_id, "该 Bot 已有服务，一个 Bot 只允许创建一个服务",
            )

    # 检查引用的实体是否被禁用
    identity = await _identity_repo.find_by_id(identity_id)
    # 检查身份是否已绑定真实平台账号（非UUID表示已有真实身份绑定，新身份将无法使用该Bot）
    if identity:
        try:
            uuid.UUID(identity.platform_id)
        except (ValueError, AttributeError):
            return await _account_detail_with_error(
                request, account_id,
                "该身份已绑定真实平台账号，无法创建新服务。请选择未绑定的身份或新建身份。",
            )
    if identity and identity.is_blocked:
        return await _account_detail_with_error(
            request, account_id, "该身份已被禁用，无法创建服务",
        )
    if bot and bot.is_blocked:
        return await _account_detail_with_error(
            request, account_id, "该 Bot 已被禁用，无法创建服务",
        )
    profile = await _profile_repo.find_by_slug(profile_id)
    if profile is None:
        profile = await _profile_repo.find_by_id(profile_id)
    if profile and not profile.is_active:
        return await _account_detail_with_error(
            request, account_id, "该角色已被禁用，无法创建服务",
        )

    # 自动生成服务名称：{身份}-{角色}-{Bot名称}
    name = "-".join(
        p for p in [
            identity.name or identity.platform_id if identity else "",
            profile.name if profile else "",
            bot.name if bot else "",
        ] if p
    )

    service = ServiceCreate(
        name=name,
        account_id=account_id,
        identity_id=identity_id,
        bot_id=bot_id,
        profile_id=profile_id,
    )
    await _service_repo.create(service)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/services/{service_id}/edit",
    response_class=HTMLResponse,
)
async def account_service_edit(
    request: Request,
    account_id: str,
    service_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """编辑对话服务。"""
    form = await request.form()
    is_active = form.get("is_active") == "on"
    # 禁用时记录原因，启用时清空
    disable_reason = str(form.get("disable_reason", "")).strip() if not is_active else ""
    profile_id = str(form.get("profile_id", "")).strip() or None
    share_group_id = str(form.get("share_group_id", "")).strip()

    # 共享组约束校验：仅允许同账号、同角色的服务共享，避免人设错乱
    if share_group_id:
        current = await _service_repo.find_by_id(service_id)
        target_profile = profile_id or (current.profile_id if current else "")
        members = await _service_repo.list_by_share_group(share_group_id)
        for m in members:
            if m.id == service_id:
                continue
            if m.account_id != account_id or m.profile_id != target_profile:
                return await _account_detail_with_error(
                    request, account_id,
                    "共享组校验失败：组内服务必须属于同一账号且使用同一角色",
                )

    update = ServiceUpdate(
        name=str(form.get("name", "")).strip() or None,
        profile_id=profile_id,
        is_active=is_active,
        disable_reason=disable_reason,
        share_group_id=share_group_id,
    )
    await _service_repo.update(service_id, update)
    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@accounts_router.post(
    "/accounts/{account_id}/services/{service_id}/delete",
    response_class=HTMLResponse,
)
async def account_service_delete(
    request: Request,
    account_id: str,
    service_id: str,
    _s: str = Depends(require_admin),
) -> Response:
    """删除对话服务（级联删除关联的对话、消息和长期记忆，并释放身份绑定）。

    共享组保护：组 key 下的对话/记忆仅在删除组内最后一个成员时才一并清理。
    """
    # 先查询服务，获取共享组与关联的 identity_id，用于后续保护判断与释放绑定
    service = await _service_repo.find_by_id(service_id)

    # 级联删除服务自身 key 下的数据（加入共享组前的历史仍挂在服务自身 ID 下）
    conv_ids = await _conv_repo.list_ids_by_service(service_id)
    if conv_ids:
        await _msg_repo.delete_by_conversation_ids(conv_ids)
    await _conv_repo.delete_by_service(service_id)
    await _memory_repo.delete_by_service(service_id)

    # 共享组数据：仅当该服务是组内最后一个成员时才一并删除
    if service and service.share_group_id:
        members = await _service_repo.list_by_share_group(service.share_group_id)
        others = [m for m in members if m.id != service_id]
        if not others:
            group_key = service.share_group_id
            group_conv_ids = await _conv_repo.list_ids_by_service(group_key)
            if group_conv_ids:
                await _msg_repo.delete_by_conversation_ids(group_conv_ids)
            await _conv_repo.delete_by_service(group_key)
            await _memory_repo.delete_by_service(group_key)

    await _service_repo.delete(service_id)

    # 释放身份绑定：若该身份无其他服务，重置 platform_id 为 UUID（恢复可绑定状态）
    if service:
        remaining = await _service_repo.count_by_identity(service.identity_id)
        if remaining == 0:
            await _identity_repo.update(
                service.identity_id,
                IdentityUpdate(platform_id=str(uuid.uuid4())),
            )

    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
