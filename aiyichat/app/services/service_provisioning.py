"""对话服务装配：管理端与 C 端共用实体校验和落库口径。"""

from dataclasses import dataclass
from uuid import UUID

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.bot_config import BotConfigInDB
from app.models.identity import IdentityInDB
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceCreate, ServiceInDB

_MAX_SERVICE_NAME_LEN = 60


class ServiceProvisionError(Exception):
    """服务装配业务错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class IdentityOption:
    """身份及其现有外部渠道占用情况。"""

    identity: IdentityInDB
    external_bot_id: str = ""
    external_platform: str = ""
    bound: bool = False
    service_count: int = 0


@dataclass(frozen=True)
class ServiceProvisionResult:
    service: ServiceInDB
    profile: PromptProfileInDB
    bot: BotConfigInDB
    identity: IdentityInDB


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def resolve_visible_profile(
    account_id: str, profile_ref: str
) -> PromptProfileInDB:
    """按 id 或 slug 解析账号可见且启用的角色。"""
    ref = profile_ref.strip()
    repo = ProfileRepository()
    profile = await repo.find_by_id(ref) if ObjectId.is_valid(ref) else None
    if profile is None:
        profile = await repo.find_by_slug(ref)
    if profile is None:
        raise ServiceProvisionError("profile_not_found", "所选角色不存在", 404)
    if profile.visibility == "private" and profile.account_id != account_id:
        raise ServiceProvisionError("profile_not_found", "所选角色不存在", 404)
    if not profile.is_active:
        raise ServiceProvisionError("profile_disabled", "所选角色已被停用，换一个或先启用它", 409)
    return profile


async def list_identity_options(account_id: str) -> list[IdentityOption]:
    """列出账号身份及其外部渠道占用情况与服务引用数。"""
    identities = await IdentityRepository().list_by_account(account_id, limit=1000)
    services = await ServiceRepository().list_by_account(account_id)
    bots = {
        bot.id: bot
        for bot in await BotConfigRepository().list_by_account(account_id)
    }
    external_by_identity: dict[str, BotConfigInDB] = {}
    service_counts: dict[str, int] = {}
    for svc in services:
        service_counts[svc.identity_id] = service_counts.get(svc.identity_id, 0) + 1
        bot = bots.get(svc.bot_id)
        if bot is not None and bot.platform != "web" and svc.identity_id not in external_by_identity:
            external_by_identity[svc.identity_id] = bot
    options: list[IdentityOption] = []
    for identity in identities:
        external = external_by_identity.get(identity.id)
        options.append(
            IdentityOption(
                identity=identity,
                external_bot_id=external.id if external else "",
                external_platform=(
                    external.platform if external else (identity.platform or "")
                ),
                bound=bool(identity.platform_id) and not _is_uuid(identity.platform_id),
                service_count=service_counts.get(identity.id, 0),
            )
        )
    return options


async def has_external_service(identity_id: str) -> bool:
    """身份是否仍有 QQ/微信服务；网页服务不占用外部绑定。"""
    for svc in await ServiceRepository().list_by_identity(identity_id):
        bot = await BotConfigRepository().find_by_id(svc.bot_id)
        if bot is not None and bot.platform != "web":
            return True
    return False


def identity_bot_error(option: IdentityOption, bot: BotConfigInDB) -> str:
    """返回身份与渠道组合的不可用原因；网页渠道不读取平台绑定。"""
    identity = option.identity
    if identity.is_blocked:
        return "该身份已被禁用，无法创建服务"
    if bot.platform == "web":
        return ""
    if option.bound:
        return "该身份已绑定真实平台账号，请选择未绑定身份"
    if identity.platform not in (None, bot.platform):
        return "身份的外部平台绑定与渠道平台不一致"
    if option.external_platform and option.external_platform != bot.platform:
        return "该身份已用于其他平台渠道"
    if option.external_bot_id and option.external_bot_id != bot.id:
        return "该身份已用于其他 QQ/微信渠道"
    return ""


async def create_service(
    *,
    account_id: str,
    bot_id: str,
    identity_id: str,
    profile_ref: str,
    name: str = "",
) -> ServiceProvisionResult:
    """校验渠道、身份、角色并创建服务。"""
    if not all([account_id.strip(), bot_id.strip(), identity_id.strip(), profile_ref.strip()]):
        raise ServiceProvisionError("invalid_reference", "渠道、身份与角色均为必选项")

    bot = await BotConfigRepository().find_by_id(bot_id.strip())
    if bot is None or bot.account_id != account_id:
        raise ServiceProvisionError("bot_not_found", "所选渠道不存在", 404)
    if bot.is_blocked:
        raise ServiceProvisionError("bot_blocked", "该渠道已被停用", 403)

    options = await list_identity_options(account_id)
    option = next((item for item in options if item.identity.id == identity_id.strip()), None)
    if option is None:
        raise ServiceProvisionError("identity_not_found", "所选身份不存在", 404)
    identity_error = identity_bot_error(option, bot)
    if identity_error:
        raise ServiceProvisionError("identity_unavailable", identity_error, 409)

    profile = await resolve_visible_profile(account_id, profile_ref)
    if bot.platform != "web" and await ServiceRepository().count_by_bot(bot.id) > 0:
        raise ServiceProvisionError(
            "bot_has_service", "该渠道已有服务，一个渠道只允许创建一个服务", 409
        )

    clean_name = name.strip()
    if len(clean_name) > _MAX_SERVICE_NAME_LEN:
        raise ServiceProvisionError(
            "invalid_name", f"服务名称最多 {_MAX_SERVICE_NAME_LEN} 个字符"
        )
    identity = option.identity
    if not clean_name:
        identity_name = identity.name or identity.nickname or "未命名身份"
        clean_name = f"{identity_name} · {profile.name} · {bot.name}"[:_MAX_SERVICE_NAME_LEN]

    try:
        service = await ServiceRepository().create(
            ServiceCreate(
                name=clean_name,
                account_id=account_id,
                identity_id=identity.id,
                bot_id=bot.id,
                profile_id=profile.id,
                single_service_bot_id=bot.id if bot.platform != "web" else None,
            )
        )
    except DuplicateKeyError as exc:
        raise ServiceProvisionError(
            "bot_has_service", "该渠道已有服务，一个渠道只允许创建一个服务", 409
        ) from exc
    return ServiceProvisionResult(
        service=service,
        profile=profile,
        bot=bot,
        identity=identity,
    )
