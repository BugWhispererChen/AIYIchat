"""C 端聊天渠道（Bot）自助管理的业务层。

渠道是接入平台（QQ / 微信 / 网页）的实例，服务 = 渠道 + 角色 + 身份。
本模块集中处理可管理边界、配额与渠道 CRUD；渠道创建与首个服务装配已解耦，
服务一律由用户通过 `POST /api/web/services` 显式创建。

安全边界：
- 归属与来源由服务端校验，客户端只能提交本账号身份、角色、名称与渠道凭据；
- 管理员后台创建的 QQ / 微信渠道不进入 C 端管理面（统一 404）；
- 凭据只写不读，响应由 API 层脱敏；
- 零服务是合法状态：删除渠道不要求账号保留其它启用中的服务。
"""

import logging
from typing import Literal, Optional

from app.config import settings
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.bot_config import BotConfigCreate, BotConfigInDB, BotConfigUpdate
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceInDB
from app.services.service_cleanup import purge_service_memory
from app.services.service_provisioning import (
    ServiceProvisionError,
    create_service,
    has_external_service,
)

logger = logging.getLogger(__name__)

_MAX_BOT_NAME_LEN = 40


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


def _clean_name(value: str, *, field: str, max_len: int) -> str:
    """去除首尾空白并校验长度；空值由调用方决定是否允许。"""
    cleaned = value.strip()
    if len(cleaned) > max_len:
        raise ChannelError("invalid_name", f"{field}最多 {max_len} 个字符", 400)
    return cleaned


async def web_quota(account_id: str) -> dict:
    """按账号可管理渠道统计自助渠道与服务配额。"""
    bots = await BotConfigRepository().list_by_account(account_id)
    managed = [b for b in bots if is_managed(b, account_id)]
    managed_ids = {b.id for b in managed}
    services = await ServiceRepository().list_by_account(account_id)
    counted = {svc.id for svc in services if svc.bot_id in managed_ids}
    return {
        "limit": settings.web_max_services,
        "bots": len(managed),
        "services": len(counted),
    }


async def _assert_bot_quota(account_id: str) -> dict:
    """创建渠道前校验自助能力与渠道配额；不受服务满额影响。"""
    if settings.web_max_services <= 0:
        raise ChannelError("web_service_disabled", "自助服务功能未开放", 403)
    quota = await web_quota(account_id)
    limit = quota["limit"]
    if quota["bots"] >= limit:
        raise ChannelError(
            "bot_quota_exceeded",
            f"最多可添加 {limit} 个渠道，请先删除不用的",
            409,
        )
    return quota


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


async def create_managed_bot(
    *,
    account_id: str,
    platform: Literal["qq", "wechat", "web"],
    name: str,
    sandbox_mode: bool = True,
    app_id: str = "",
    app_secret: str = "",
) -> BotConfigInDB:
    """只创建 C 端渠道；服务由用户在服务页显式装配。"""
    clean_name = _clean_name(name, field="渠道名称", max_len=_MAX_BOT_NAME_LEN)
    if not clean_name:
        raise ChannelError("invalid_name", "请填写渠道名称", 400)
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if platform == "qq" and bool(app_id) != bool(app_secret):
        raise ChannelError(
            "invalid_credentials", "AppID 与 AppSecret 需同时填写", 400
        )
    await _assert_bot_quota(account_id)

    bot = await _create_bot_record(
        account_id=account_id,
        platform=platform,
        name=clean_name,
        app_id=app_id,
        app_secret=app_secret,
        sandbox_mode=sandbox_mode,
    )

    quota = await web_quota(account_id)
    if quota["bots"] > quota["limit"]:
        await _delete_bot_record(bot)
        raise ChannelError(
            "quota_exceeded", "渠道数量已达上限，请刷新后重试", 409
        )
    logger.info(
        "C 端账号 %s 创建渠道 %s（%s）", account_id, bot.name, platform
    )
    return bot


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


async def delete_managed_bot(account_id: str, bot_id: str) -> int:
    """删除渠道并级联删除服务；零服务是合法状态，不做最后服务保护。"""
    bot = await get_managed_bot(account_id, bot_id)
    repo = ServiceRepository()
    services = await repo.list_by_bot(bot.id)

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
        if not await has_external_service(identity_id):
            await identity_repo.reset_platform_binding(identity_id, bot.id)
    try:
        from app.services.bot_qr_service import drop_qq_session

        drop_qq_session(bot.id)
    except Exception:  # noqa: BLE001 - 扫码会话清理失败不应阻断删除
        logger.warning("清理渠道 %s 的扫码会话失败", bot.id, exc_info=True)
    logger.info(
        "C 端账号 %s 删除渠道 %s，级联删除 %d 个服务",
        account_id, bot.name, len(services),
    )
    return len(services)


async def create_service_for_bot(
    *,
    account_id: str,
    bot_id: str,
    identity_id: str,
    profile_id: str,
    name: str = "",
) -> tuple[ServiceInDB, PromptProfileInDB, BotConfigInDB]:
    """在可管理渠道上创建服务，并在创建前后复核自助配额。"""
    if settings.web_max_services <= 0:
        raise ChannelError("web_service_disabled", "自助服务功能未开放", 403)
    bot = await get_managed_bot(account_id, bot_id)
    quota = await web_quota(account_id)
    if quota["services"] >= quota["limit"]:
        raise ChannelError(
            "service_quota_exceeded",
            f"最多可创建 {quota['limit']} 个服务，请先删除不用的",
            409,
        )

    service: Optional[ServiceInDB] = None
    try:
        try:
            result = await create_service(
                account_id=account_id,
                bot_id=bot.id,
                identity_id=identity_id,
                profile_ref=profile_id,
                name=name,
            )
        except ServiceProvisionError as exc:
            raise ChannelError(exc.code, exc.message, exc.status_code) from exc
        service = result.service
        quota_after = await web_quota(account_id)
        if quota_after["services"] > quota_after["limit"]:
            raise ChannelError(
                "quota_exceeded", "服务数量已达上限，请刷新后重试", 409
            )
    except Exception:
        if service is not None:
            await ServiceRepository().delete(service.id)
        raise

    logger.info(
        "C 端账号 %s 在渠道 %s 上创建服务 %s",
        account_id, bot.name, service.name,
    )
    return service, result.profile, bot


async def list_managed_bots(account_id: str) -> list[BotConfigInDB]:
    """列出账号下 C 端可管理的渠道。"""
    bots = await BotConfigRepository().list_by_account(account_id)
    return [b for b in bots if is_managed(b, account_id)]
