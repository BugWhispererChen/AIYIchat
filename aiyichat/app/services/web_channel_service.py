"""C 端聊天渠道（Bot）自助管理的业务层。

渠道是接入平台（QQ / 微信 / 网页）的实例，服务是「角色 × 渠道」的聊天单元。
本模块集中处理可管理边界、配额、渠道身份复用、渠道 CRUD 与首个服务装配，
HTTP 层只负责鉴权、参数与错误响应形状。

安全边界：
- 归属与来源一律由服务端写入，客户端只能提交名称、凭据与角色；
- 管理员后台创建的 QQ / 微信渠道不进入 C 端管理面（统一 404）；
- 凭据只写不读，响应由 API 层脱敏；
- 删除渠道前保护「至少一个启用中的服务」。
"""

import logging
from typing import Literal, Optional
from uuid import UUID

from bson import ObjectId

from app.config import settings
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.bot_config import BotConfigCreate, BotConfigInDB, BotConfigUpdate
from app.models.identity import IdentityCreate, IdentityInDB
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceCreate, ServiceInDB
from app.services.service_cleanup import purge_service_memory

logger = logging.getLogger(__name__)

_PLATFORM_LABELS = {"qq": "QQ", "wechat": "微信", "web": "网页"}
_MAX_BOT_NAME_LEN = 40
_MAX_SERVICE_NAME_LEN = 60


class ChannelError(Exception):
    """渠道业务错误：由 API 层转换为 {code, detail}。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def is_managed(bot: BotConfigInDB, account_id: str) -> bool:
    """C 端是否可管理该渠道。

    管理员创建的 Web Bot 仍作为账号载体开放管理；管理员创建的 QQ / 微信渠道
    不在 C 端展示，避免用户改写或删除管理员配置的凭据。
    """
    if bot.account_id != account_id:
        return False
    return bot.platform == "web" or bot.origin == "web"


def compute_state(bot: BotConfigInDB) -> str:
    """渠道在 C 端页面上的状态。"""
    if bot.is_blocked:
        return "blocked"
    if bot.platform == "wechat":
        if bot.status in ("online", "pending_qr", "offline"):
            return bot.status
        return "offline"
    if bot.platform == "qq":
        return "ready" if (bot.app_id and bot.app_secret) else "pending_bind"
    return "ready"


def has_credentials(bot: BotConfigInDB) -> bool:
    """渠道是否已具备可启动凭据。"""
    if bot.platform == "qq":
        return bool(bot.app_id and bot.app_secret)
    if bot.platform == "wechat":
        return bool(bot.bot_token)
    return False


def _is_uuid(value: str) -> bool:
    """判断 platform_id 是否仍是占位 UUID（尚未绑定真实 openid）。"""
    try:
        UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _clean_name(value: str, *, field: str, max_len: int) -> str:
    """去除首尾空白并校验长度；空值由调用方决定是否允许。"""
    cleaned = value.strip()
    if len(cleaned) > max_len:
        raise ChannelError("invalid_name", f"{field}最多 {max_len} 个字符", 400)
    return cleaned


def validate_timezone(tz_name: str) -> str:
    """校验 IANA 时区名，合法返回空串，否则返回可读原因。"""
    if not tz_name:
        return ""
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - 名称非法与 tzdata 缺失同样归为不可用
        return f"时区名称无效：{tz_name}"
    return ""


async def resolve_visible_profile(
    account_id: str, profile_ref: str
) -> PromptProfileInDB:
    """取回账号可见且启用中的角色，失败抛 ChannelError。"""
    if not profile_ref:
        raise ChannelError("profile_not_found", "所选角色不存在", 404)
    repo = ProfileRepository()
    found: Optional[PromptProfileInDB] = None
    if ObjectId.is_valid(profile_ref):
        found = await repo.find_by_id(profile_ref)
    if found is None:
        found = await repo.find_by_slug(profile_ref)
    if found is None:
        raise ChannelError("profile_not_found", "所选角色不存在", 404)
    if found.visibility == "private" and found.account_id != account_id:
        # 与不存在同响应：他人的私有角色不可枚举
        raise ChannelError("profile_not_found", "所选角色不存在", 404)
    if not found.is_active:
        raise ChannelError("profile_disabled", "所选角色已被停用，换一个或先启用它", 409)
    return found


async def web_quota(account_id: str, login_identity_id: str) -> dict:
    """统一的渠道/服务配额口径。

    渠道数：账号下可管理的渠道（含账号级 Web 渠道）。
    服务数：挂在可管理渠道上的服务 + 登录账号名下的服务，按 service id 去重。
    管理员专用渠道身份上的服务不计入，保留「管理员渠道不占网页名额」的语义。
    """
    bots = await BotConfigRepository().list_by_account(account_id)
    managed = [b for b in bots if is_managed(b, account_id)]
    managed_ids = {b.id for b in managed}
    services = await ServiceRepository().list_by_account(account_id)
    counted = {
        svc.id
        for svc in services
        if svc.bot_id in managed_ids or svc.identity_id == login_identity_id
    }
    return {
        "limit": settings.web_max_services,
        "bots": len(managed),
        "services": len(counted),
    }


async def _assert_create_quota(account_id: str, login_identity_id: str) -> dict:
    """创建前校验自助能力与配额。"""
    if settings.web_max_services <= 0:
        raise ChannelError("web_service_disabled", "自助服务功能未开放", 403)
    quota = await web_quota(account_id, login_identity_id)
    limit = quota["limit"]
    if quota["bots"] >= limit:
        raise ChannelError(
            "bot_quota_exceeded",
            f"最多可添加 {limit} 个渠道，请先删除不用的",
            409,
        )
    if quota["services"] >= limit:
        raise ChannelError(
            "service_quota_exceeded",
            f"最多可创建 {limit} 个服务，请先删除不用的",
            409,
        )
    return quota


async def resolve_channel_identity(
    account_id: str, platform: Literal["qq", "wechat"], username: str
) -> tuple[IdentityInDB, bool]:
    """取账号下该平台的渠道身份，没有则新建。

    复用同一平台身份是为了让同一账号的多个同平台渠道都能服务同一个 openid：
    引擎按 (identity_id, bot_id) 找服务，若每个渠道各建占位身份，同一发送者
    只会命中其中一个渠道。返回 (身份, 是否本次新建)。
    """
    identities = await IdentityRepository().list_by_account(account_id, limit=200)
    candidates = [i for i in identities if i.platform == platform]
    bound = [i for i in candidates if not _is_uuid(i.platform_id)]
    if bound:
        return bound[0], False
    if candidates:
        return candidates[0], False
    label = _PLATFORM_LABELS.get(platform, platform)
    created = await IdentityRepository().create(
        IdentityCreate(
            account_id=account_id,
            name=f"{username}（{label}）",
            platform=platform,
            nickname=username,
        )
    )
    return created, True


async def _delete_bot_record(bot: BotConfigInDB) -> None:
    """删除渠道本身：微信需先停任务，其它直接删 DB。"""
    if bot.platform == "wechat":
        from app.adapters.wechat_clawbot import get_wechat_manager

        await get_wechat_manager().remove_bot(bot.id)
    else:
        await BotConfigRepository().delete(bot.id)


async def _create_bot_record(
    *,
    account_id: str,
    platform: Literal["qq", "wechat", "web"],
    name: str,
    app_id: str,
    app_secret: str,
    sandbox_mode: bool,
) -> BotConfigInDB:
    """创建渠道记录；微信走 Manager 以注册待登录客户端。"""
    repo = BotConfigRepository()
    if platform == "wechat":
        from app.adapters.wechat_clawbot import get_wechat_manager

        client = await get_wechat_manager().create_bot(
            name=name, account_id=account_id, origin="web"
        )
        bot = await repo.find_by_id(client.bot_id)
        if bot is None:
            await get_wechat_manager().remove_bot(client.bot_id)
            raise ChannelError("bot_create_failed", "微信渠道创建失败，请稍后重试", 500)
        return bot
    return await repo.create(
        BotConfigCreate(
            account_id=account_id,
            name=name,
            platform=platform,
            origin="web",
            app_id=app_id,
            app_secret=app_secret,
            sandbox_mode=sandbox_mode,
        )
    )


def _default_service_name(profile_name: str, bot_name: str) -> str:
    """服务默认名：角色 · 渠道，便于聊天页区分同名角色。"""
    return f"{profile_name} · {bot_name}"[:_MAX_SERVICE_NAME_LEN]


async def create_managed_bot(
    *,
    account_id: str,
    login_identity_id: str,
    username: str,
    platform: Literal["qq", "wechat", "web"],
    name: str,
    profile_id: str,
    service_name: str = "",
    timezone: str = "",
    sandbox_mode: bool = True,
    app_id: str = "",
    app_secret: str = "",
) -> tuple[BotConfigInDB, ServiceInDB, PromptProfileInDB]:
    """创建 C 端渠道，并同事务装配首个服务。"""
    clean_name = _clean_name(name, field="渠道名称", max_len=_MAX_BOT_NAME_LEN)
    if not clean_name:
        raise ChannelError("invalid_name", "请填写渠道名称", 400)

    profile = await resolve_visible_profile(account_id, profile_id)
    tz_err = validate_timezone(timezone.strip())
    if tz_err:
        raise ChannelError("invalid_timezone", tz_err, 400)

    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if platform == "qq" and bool(app_id) != bool(app_secret):
        raise ChannelError(
            "invalid_credentials", "AppID 与 AppSecret 需同时填写", 400
        )

    await _assert_create_quota(account_id, login_identity_id)

    bot: Optional[BotConfigInDB] = None
    service: Optional[ServiceInDB] = None
    identity_created = False
    identity_id = login_identity_id
    try:
        bot = await _create_bot_record(
            account_id=account_id,
            platform=platform,
            name=clean_name,
            app_id=app_id,
            app_secret=app_secret,
            sandbox_mode=sandbox_mode,
        )

        if platform == "web":
            identity_id = login_identity_id
        else:
            identity, identity_created = await resolve_channel_identity(
                account_id, platform, username
            )
            identity_id = identity.id

        service = await ServiceRepository().create(
            ServiceCreate(
                name=(
                    _clean_name(
                        service_name,
                        field="服务名称",
                        max_len=_MAX_SERVICE_NAME_LEN,
                    )
                    or _default_service_name(profile.name, bot.name)
                ),
                account_id=account_id,
                identity_id=identity_id,
                bot_id=bot.id,
                profile_id=profile.id,
                timezone=timezone.strip(),
            )
        )

        # 并发兜底：插入后复核，超额则整体回滚
        quota = await web_quota(account_id, login_identity_id)
        if quota["bots"] > quota["limit"] or quota["services"] > quota["limit"]:
            raise ChannelError(
                "quota_exceeded", "渠道或服务数量已达上限，请刷新后重试", 409
            )

        logger.info(
            "C 端账号 %s 创建渠道 %s（%s）并装配服务 %s",
            account_id,
            bot.name,
            platform,
            service.name,
        )
        return bot, service, profile
    except Exception:
        if service is not None:
            await ServiceRepository().delete(service.id)
        if bot is not None:
            await _delete_bot_record(bot)
        if identity_created:
            await IdentityRepository().delete(identity_id)
        raise


async def update_managed_bot(
    *,
    account_id: str,
    bot_id: str,
    name: Optional[str] = None,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    sandbox_mode: Optional[bool] = None,
) -> BotConfigInDB:
    """更新 C 端渠道：名称；QQ 额外允许凭据与沙箱。"""
    bot = await get_managed_bot(account_id, bot_id)
    update = BotConfigUpdate()
    if name is not None:
        clean_name = _clean_name(name, field="渠道名称", max_len=_MAX_BOT_NAME_LEN)
        if not clean_name:
            raise ChannelError("invalid_name", "渠道名称不能为空", 400)
        update.name = clean_name
    if bot.platform == "qq":
        if app_id is not None and app_id.strip():
            update.app_id = app_id.strip()
        # 留空或仍是脱敏串（含 *）不更新：脱敏值写回库会让 BotManager 拿它认证
        if app_secret is not None and app_secret.strip() and "*" not in app_secret:
            update.app_secret = app_secret.strip()
        if sandbox_mode is not None:
            update.sandbox_mode = sandbox_mode
    saved = await BotConfigRepository().update(bot.id, update)
    if saved is None:
        raise ChannelError("not_found", "渠道不存在", 404)
    return saved


async def get_managed_bot(account_id: str, bot_id: str) -> BotConfigInDB:
    """取回账号下可管理且未被禁用的渠道。"""
    bot = await BotConfigRepository().find_by_id(bot_id)
    if bot is None or not is_managed(bot, account_id):
        raise ChannelError("not_found", "渠道不存在", 404)
    if bot.is_blocked:
        raise ChannelError("bot_blocked", "该渠道已被管理员停用，请联系管理员", 403)
    return bot


async def delete_managed_bot(
    account_id: str, bot_id: str, login_identity_id: str = ""
) -> int:
    """删除 C 端渠道：级联删服务，保护最后一个活跃服务。

    `login_identity_id` 是当前网页登录账号：账号下的渠道服务可能挂在别的身份上，
    仅按账号统计会让登录账号失去最后一个活跃服务后被 no_service 锁死。
    """
    bot = await get_managed_bot(account_id, bot_id)
    repo = ServiceRepository()
    services = await repo.list_by_bot(bot.id)
    siblings = await repo.list_by_account(account_id)
    other_active = [
        svc for svc in siblings if svc.bot_id != bot.id and svc.is_active
    ]
    if any(svc.is_active for svc in services) and not other_active:
        raise ChannelError(
            "last_active_service",
            "至少需要保留一个启用中的服务才能继续聊天",
            409,
        )
    if login_identity_id:
        deleting_login_active = any(
            svc.identity_id == login_identity_id and svc.is_active
            for svc in services
        )
        own = await repo.list_by_identity(login_identity_id)
        own_active_after = [
            svc for svc in own if svc.bot_id != bot.id and svc.is_active
        ]
        if deleting_login_active and not own_active_after:
            raise ChannelError(
                "last_active_service",
                "当前账号至少需要保留一个启用中的服务才能继续聊天",
                409,
            )

    # 网页渠道使用登录账号，不能重置；只有 QQ / 微信渠道的专用身份才清理绑定
    identity_ids = (
        {svc.identity_id for svc in services} if bot.platform != "web" else set()
    )
    try:
        await purge_service_memory(services)
    except Exception as exc:  # noqa: BLE001 - 清理失败时保留服务行，便于重试
        logger.exception("删除渠道前清理记忆失败: bot=%s", bot.id)
        raise ChannelError(
            "cleanup_failed", "清理服务记忆失败，请稍后重试", 500
        ) from exc
    for svc in services:
        await repo.delete(svc.id)

    await _delete_bot_record(bot)

    identity_repo = IdentityRepository()
    for identity_id in identity_ids:
        remaining = await repo.count_by_identity(identity_id)
        if remaining == 0:
            await identity_repo.reset_platform_binding(identity_id, bot.id)

    try:
        from app.services.bot_qr_service import drop_qq_session

        drop_qq_session(bot.id)
    except Exception:  # noqa: BLE001 - 扫码会话清理失败不应阻断删除
        logger.warning("清理渠道 %s 的扫码会话失败", bot.id, exc_info=True)

    logger.info(
        "C 端账号 %s 删除渠道 %s，级联删除 %d 个服务",
        account_id,
        bot.name,
        len(services),
    )
    return len(services)


async def create_service_for_bot(
    *,
    account_id: str,
    login_identity_id: str,
    username: str,
    bot_id: str,
    profile_id: str,
    name: str = "",
    timezone: str = "",
) -> tuple[ServiceInDB, PromptProfileInDB, BotConfigInDB]:
    """在指定渠道上创建服务，供 /api/web/services 复用。"""
    if settings.web_max_services <= 0:
        raise ChannelError("web_service_disabled", "自建服务功能未开放", 403)

    bot = await get_managed_bot(account_id, bot_id)
    if bot.platform != "web" and await ServiceRepository().count_by_bot(bot.id) > 0:
        raise ChannelError(
            "bot_has_service", "该渠道已有服务，一个渠道只允许创建一个服务", 409
        )

    profile = await resolve_visible_profile(account_id, profile_id)
    tz_err = validate_timezone(timezone.strip())
    if tz_err:
        raise ChannelError("invalid_timezone", tz_err, 400)

    quota = await web_quota(account_id, login_identity_id)
    if quota["services"] >= quota["limit"]:
        raise ChannelError(
            "service_quota_exceeded",
            f"最多可创建 {quota['limit']} 个服务，请先删除不用的",
            409,
        )

    identity_created = False
    identity_id = login_identity_id
    if bot.platform != "web":
        identity, identity_created = await resolve_channel_identity(
            account_id, bot.platform, username
        )
        identity_id = identity.id

    try:
        service = await ServiceRepository().create(
            ServiceCreate(
                name=(
                    _clean_name(
                        name, field="服务名称", max_len=_MAX_SERVICE_NAME_LEN
                    )
                    or _default_service_name(profile.name, bot.name)
                ),
                account_id=account_id,
                identity_id=identity_id,
                bot_id=bot.id,
                profile_id=profile.id,
                timezone=timezone.strip(),
            )
        )
        quota_after = await web_quota(account_id, login_identity_id)
        if quota_after["services"] > quota_after["limit"]:
            await ServiceRepository().delete(service.id)
            raise ChannelError(
                "quota_exceeded", "服务数量已达上限，请刷新后重试", 409
            )
    except Exception:
        if identity_created:
            await IdentityRepository().delete(identity_id)
        raise

    logger.info(
        "C 端账号 %s 在渠道 %s 上创建服务 %s",
        account_id,
        bot.name,
        service.name,
    )
    return service, profile, bot


async def list_managed_bots(account_id: str) -> list[BotConfigInDB]:
    """列出账号下 C 端可管理的渠道。"""
    bots = await BotConfigRepository().list_by_account(account_id)
    return [b for b in bots if is_managed(b, account_id)]
