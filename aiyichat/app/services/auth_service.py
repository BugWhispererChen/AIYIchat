"""C 端网页身份的注册、登录与治理编排。

设计要点（对应执行计划第二节与第五节 P2/P3）：
- 注册必须原子建齐 Account→Quota→User→Service 四件套：流式链路没有自动绑定分支，
  引擎要求 User 与 Service 均已存在，且 `_quota_precheck` 在额度记录缺失时直接拒发。
- 用户名占位依赖 `web_credentials.username` 唯一索引，四件套建齐后才回填 `identity_id`，
  中途失败必须回滚已写文档（含删除身份，避免用户名被永久占死）。
- 凭据与业务身份分离：密码哈希只进 `web_credentials`，不进会被 prompt 消费的 user 对象。
"""

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bson.errors import InvalidId

from app.config import settings
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.db.repositories.web_session_repo import WebSessionRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.quota import AccountQuotaUpdate, QuotaEntry
from app.models.service import ServiceCreate
from app.models.identity import IdentityCreate
from app.models.web_credential import (
    PRIVACY_VERSION,
    TERMS_VERSION,
    WebConsent,
    WebCredentialCreate,
    WebCredentialInDB,
    normalize_username,
)
from app.models.web_session import WebSessionCreate, WebSessionInDB
from app.utils.password_hashing import hash_password, verify_password

logger = logging.getLogger(__name__)

# 审核拒绝后的重注冷却时长（小时）：防止被拒者立刻换 IP 反复注册刷额度
REJECT_COOLDOWN_HOURS = 24

# 内置体验额度（`web_free_quota_template_id` 为空或模板不存在时回落）。
# 单位与 `quota_templates` / 管理后台「套餐计费」一致：文字额度按 **Token** 计（LLM 实际 usage 结算），
# 语音/图片按 **次/张** 计。预检公式为 `剩余额度 >= 输入估算 token + chat_max_tokens`，
# 故文字额度必须显著高于单轮上限（默认 chat_max_tokens=2048），否则新账号首条消息即被拒。
FREE_TEXT_LIMIT = 200_000  # ≈ 100 轮体验对话
FREE_VOICE_LIMIT = 30
FREE_IMAGE_LIMIT = 20
FREE_QUOTA_VALID_DAYS = 30

# 用户名规则：规范化后 3-32 位，小写字母/数字/下划线，且必须以字母或数字开头
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
USERNAME_FIRST_MUST_ALNUM = re.compile(r"^[a-z0-9]")
# 保留名：避免与管理员账号、系统角色混淆或被用于仿冒
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "root", "system", "operator", "support",
    "service", "staff", "web", "bot", "test", "guest", "aiyichat",
})

# 弱密码黑名单（常见 top100 归一化集合）：命中即拒绝，防止撞库
WEAK_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "p@ssw0rd", "qwerty123",
    "12345678", "123456789", "1234567890", "1234567", "11111111", "00000000",
    "abc123456", "abcd1234", "abcdef123", "a12345678", "12345678a", "1a2b3c4d",
    "iloveyou", "iloveyou123", "letmein123", "welcome123", "welcome1", "admin123",
    "administrator1", "root1234", "pass1234", "test1234", "user1234", "guest123",
    "monkey123", "dragon123", "sunshine123", "princess1", "football123", "baseball12",
    "superman123", "batman123", "trustno1", "whatever1", "starwars1", "gateway123",
    "harley123", "ranger123", "buster123", "soccer123", "hockey123", "killer123",
    "jordan123", "banana123", "samsung123", "nikkie1234", "gateway321", "monkey2233",
    "00000000a", "11111111a", "12121212", "123123123", "123321123", "12341234",
    "12344321", "1234qwer", "qwer1234", "asdf1234", "asdfghjkl1", "zxcvbnm123",
    "qwertyuiop1", "poiuytrewq1", "1q2w3e4r", "1qaz2wsx", "qazwsx123", "zaq12wsx",
    "pass12345", "test12345", "abcd12345", "abc12345", "a1b2c3d4", "pw123456",
    "secret123", "secret1234", "master123", "shadow123", "michael123", "jennifer1",
    "computer123", "internet123", "samsung12", "iphone1234", "android123", "google123",
    "facebook123", "instagram1", "hotmail123", "66666666", "88888888", "999999999",
    "123456a", "654321a", "123abc456", "password0", "passw0rd1", "qwertyui1",
})


class AuthError(Exception):
    """鉴权业务异常：携带 HTTP 状态码与对身份可读的中文提示。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class LoginResult:
    """登录成功产物：身份与会话。"""

    credential: WebCredentialInDB
    token: str


@dataclass
class SessionResult:
    """令牌解析产物：凭据、关联身份与承载它的会话记录。

    账号中心需要会话 ID 来区分"当前设备"与"其他设备"，故单独返回 session；
    只需要凭据与身份的旧调用方继续用 `resolve_token`。
    """

    credential: WebCredentialInDB
    identity_id: str
    session: WebSessionInDB


@dataclass
class _Draft:
    """注册过程已写入的文档 ID，供失败回滚。"""

    credential_id: str = ""
    account_id: str = ""
    identity_id: str = ""
    service_id: str = ""
    extras: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    """当前 UTC aware 时间，统一模块内时间基准。"""
    return datetime.now(timezone.utc)


def ensure_web_enabled() -> None:
    """网页鉴权总开关校验：关闭时所有 C 端接口一律拒绝，保证老实例无感知。"""
    if not settings.web_auth_enabled:
        raise AuthError("web_auth_disabled", "网页登录功能未开放", 403)


def validate_username(raw: str) -> str:
    """校验并返回规范化用户名。"""
    username = normalize_username(raw)
    if not username:
        raise AuthError("invalid_username", "用户名不能为空")
    if not USERNAME_PATTERN.match(username) or not USERNAME_FIRST_MUST_ALNUM.match(username):
        raise AuthError("invalid_username", "用户名需为 3-32 位字母、数字或下划线，且以字母或数字开头")
    if username in RESERVED_USERNAMES:
        raise AuthError("invalid_username", "该用户名为保留名，请更换")
    return username


def validate_password(username: str, password: str) -> None:
    """密码策略：长度 8-64、至少 1 字母 + 1 数字、不在弱密码黑名单、不含用户名。"""
    if len(password) < 8 or len(password) > 64:
        raise AuthError("weak_password", "密码长度需为 8-64 位")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise AuthError("weak_password", "密码需同时包含字母和数字")
    if password.lower() in WEAK_PASSWORDS:
        raise AuthError("weak_password", "该密码过于常见，请更换")
    if username and username in password.lower():
        raise AuthError("weak_password", "密码不能包含用户名")


def build_consent(terms_accepted: bool, privacy_accepted: bool, age_confirmed: bool) -> WebConsent:
    """组装合规留痕；未满 14 周岁或未同意条款/隐私直接拒绝注册。"""
    if not age_confirmed:
        raise AuthError("age_not_confirmed", "年满 14 周岁方可注册，暂不支持未成年人注册", 400)
    if not terms_accepted or not privacy_accepted:
        raise AuthError("consent_required", "请先阅读并同意身份协议与隐私政策", 400)
    now = _utcnow()
    return WebConsent(
        terms_version=TERMS_VERSION,
        terms_accepted_at=now,
        privacy_version=PRIVACY_VERSION,
        privacy_accepted_at=now,
        age_confirmed=True,
        age_confirmed_at=now,
    )


async def resolve_default_profile_id() -> str:
    """解析新服务默认角色：优先 `web_default_profile_slug`，回落任一在用公共角色。

    引擎 `_resolve_profile` 同时接受角色 ObjectId 与 slug，故这里直接返回 slug；
    预置角色全部为 public，跨账号可用，无需为每个身份各建 Bot。

    回落清单必须过滤 `is_active` 与草稿：`list_public()` 按名称排序且不看启用状态，
    曾据此选中一个已停用的角色，导致新账号首条消息即回"尚未配置角色"。
    """
    repo = ProfileRepository()
    slug = settings.web_default_profile_slug.strip()
    if slug:
        profile = await repo.find_by_slug(slug)
        if profile is not None and profile.is_active and not profile.is_draft:
            return slug
    candidates = [p for p in await repo.list_active() if not p.is_draft]
    if not candidates:
        raise AuthError("profile_not_configured", "未配置可用的默认角色，请联系管理员", 503)
    return candidates[0].slug


async def ensure_account_web_bot(account_id: str, name: str = "") -> str:
    """取该账号的网页载体 Bot，没有则按需创建。

    优先复用账号名下未停用的 `platform="web"` Bot（管理员可在账号详情页建多个）；
    其次回落到系统载体 Bot（存量部署）；两者都没有时自动创建一个账号级 Bot，
    使注册与新建服务不再依赖全局配置。
    """
    repo = BotConfigRepository()
    own = [b for b in await repo.list_by_account(account_id) if b.platform == "web"]
    usable = [b for b in own if not b.is_blocked]
    if usable:
        return usable[0].id
    if own:
        # 全部被管理员停用：不自动复活，避免绕过停用
        raise AuthError("web_bot_blocked", "网页服务已停用，请联系管理员", 503)

    system_bot_id = settings.web_bot_id.strip()
    if system_bot_id:
        bot = await repo.find_by_id(system_bot_id)
        if bot is None:
            raise AuthError(
                "web_bot_not_found", "网页服务配置异常（系统 Bot 不存在），请联系管理员", 503
            )
        if bot.is_blocked:
            raise AuthError("web_bot_blocked", "网页服务已停用，请联系管理员", 503)
        if bot.platform != "web":
            raise AuthError("web_bot_platform", "系统 Bot 平台不是 web，请联系管理员", 503)
        return bot.id

    created = await repo.create(
        BotConfigCreate(
            account_id=account_id,
            name=name or "网页助手",
            platform="web",
            origin="web",
        )
    )
    logger.info("为账号 %s 自动创建网页载体 Bot %s", account_id, created.id)
    return created.id


async def build_free_quota(account_id: str) -> tuple[AccountQuotaUpdate, str]:
    """按 `web_free_quota_template_id` 生成注册赠送额度，模板缺失则回落内置体验额度。"""
    template_id = settings.web_free_quota_template_id.strip()
    if template_id:
        try:
            template = await QuotaTemplateRepository().find_by_id(template_id)
        except InvalidId:
            template = None
        if template is not None:
            expires_at = (
                _utcnow() + timedelta(days=template.validity_days)
                if template.validity_days
                else None
            )
            return (
                AccountQuotaUpdate(
                    text_quota=QuotaEntry(limit=template.text_limit, used=0, expires_at=expires_at),
                    voice_quota=QuotaEntry(limit=template.voice_limit, used=0, expires_at=expires_at),
                    image_quota=QuotaEntry(limit=template.image_limit, used=0, expires_at=expires_at),
                ),
                f"template:{template.name}",
            )
    expires_at = _utcnow() + timedelta(days=FREE_QUOTA_VALID_DAYS)
    return (
        AccountQuotaUpdate(
            text_quota=QuotaEntry(limit=FREE_TEXT_LIMIT, used=0, expires_at=expires_at),
            voice_quota=QuotaEntry(limit=FREE_VOICE_LIMIT, used=0, expires_at=expires_at),
            image_quota=QuotaEntry(limit=FREE_IMAGE_LIMIT, used=0, expires_at=expires_at),
        ),
        "builtin",
    )


async def rollback(draft: _Draft) -> None:
    """回滚注册途中已写入的文档。

    账号级联删除覆盖 User/Service/Quota；身份单独删除，否则用户名会被永久占死、
    身份再也无法注册。回滚自身失败只告警，不掩盖原始异常。
    """
    try:
        if draft.account_id:
            await AccountRepository().delete_cascade(draft.account_id)
        if draft.credential_id:
            await WebCredentialRepository().delete_by_id(draft.credential_id)
    except Exception:  # noqa: BLE001 - 回滚失败不能覆盖原始异常
        logger.exception(
            "注册回滚失败，遗留脏数据需人工清理: account_id=%s identity_id=%s",
            draft.account_id,
            draft.credential_id,
        )


async def _purge_rejected(credential: WebCredentialInDB) -> None:
    """清理被拒身份的遗留数据，释放用户名以便冷却期后重新注册。

    唯一索引按字面值占位，被拒记录不删除就永远注册不了；被拒身份从未激活，
    其业务四件套与会话可安全级联清除。
    """
    await WebSessionRepository().delete_by_credential(credential.id)
    if credential.identity_id:
        identity = await IdentityRepository().find_by_id(credential.identity_id)
        if identity is not None:
            await AccountRepository().delete_cascade(identity.account_id)
    await WebCredentialRepository().delete_by_id(credential.id)


async def register(
    *,
    username: str,
    password: str,
    terms_accepted: bool,
    privacy_accepted: bool,
    age_confirmed: bool,
    ip: str = "",
) -> WebCredentialInDB:
    """自助注册：校验 → 占用户名 → 建四件套 → 落 pending（或免审直达 active）。

    Returns:
        已落库的网页身份（status 由 `web_register_require_review` 决定）
    Raises:
        AuthError: 校验不通过、用户名冲突、处于拒绝冷却期、或依赖配置缺失
    """
    ensure_web_enabled()
    if not settings.web_register_enabled:
        raise AuthError("web_register_disabled", "暂未开放注册", 403)

    name = validate_username(username)
    validate_password(name, password)
    consent = build_consent(terms_accepted, privacy_accepted, age_confirmed)

    credentials = WebCredentialRepository()
    existing = await credentials.find_by_username(name)
    if existing is not None:
        # 被拒用户名在冷却期后允许重新注册；其余状态（pending/active/disabled）一律占用
        if existing.status != "rejected":
            raise AuthError("username_taken", "该用户名已被占用", 409)
        if (
            await credentials.find_recent_by_username(name, REJECT_COOLDOWN_HOURS, "rejected")
            is not None
        ):
            raise AuthError("register_cooldown", "上次审核未通过，请稍后再试", 409)
        await _purge_rejected(existing)

    # 先占用户名（唯一索引原子判重），再建业务四件套
    credential = await credentials.insert(
        WebCredentialCreate(
            username=name,
            password_hash=hash_password(password),
            status="pending",
            created_ip=ip,
            consent=consent,
        )
    )
    if credential is None:
        raise AuthError("username_taken", "该用户名已被占用", 409)

    draft = _Draft(credential_id=credential.id)
    try:
        profile_slug = await resolve_default_profile_id()

        account = await AccountRepository().create(
            AccountCreate(name=f"web:{name}", remark="C 端网页身份自助注册")
        )
        draft.account_id = account.id
        bot_id = await ensure_account_web_bot(account.id, name="网页助手")

        quota_update, quota_source = await build_free_quota(account.id)
        await QuotaRepository().create_or_update(account.id, quota_update)

        identity = await IdentityRepository().create(
            IdentityCreate(
                account_id=account.id,
                name=name,
                platform_id=name,  # 非 UUID：_is_uuid 判定会自动拒绝把 QQ openid 绑到该身份
                platform="web",
                nickname=name,
            )
        )
        draft.identity_id = identity.id

        service = await ServiceRepository().create(
            ServiceCreate(
                name="默认服务",
                account_id=account.id,
                identity_id=identity.id,
                bot_id=bot_id,
                profile_id=profile_slug,
            )
        )
        draft.service_id = service.id

        await credentials.set_identity_id(credential.id, identity.id)
        if not settings.web_register_require_review:
            await credentials.set_status(credential.id, "active", reviewed_by="system", review_note=f"免审注册（额度来源：{quota_source}）", review=True)
    except AuthError:
        await rollback(draft)
        raise
    except Exception as e:  # noqa: BLE001 - 任意一步失败都要回滚并转成可读错误
        await rollback(draft)
        raise AuthError("register_failed", f"注册失败，请稍后重试：{e}", 500) from e

    saved = await credentials.find_by_id(credential.id)
    if saved is None:
        raise AuthError("register_failed", "注册状态写入异常，请稍后重试", 500)
    return saved


async def login(*, username: str, password: str, ip: str = "", user_agent: str = "") -> LoginResult:
    """登录：校验凭据与状态 → 签发会话 → 并发上限控制 → 滑动过期起点。

    失败计数与锁定按身份维度进行；无论用户名是否存在都返回同一提示，避免枚举账号。
    """
    ensure_web_enabled()
    name = normalize_username(username)
    credentials = WebCredentialRepository()
    credential = await credentials.find_by_username(name)
    if credential is None:
        raise AuthError("login_failed", "用户名或密码错误", 401)

    if credential.is_locked:
        raise AuthError("login_locked", "登录失败次数过多，请稍后再试", 429)
    if not verify_password(password, credential.password_hash):
        locked_until = await credentials.register_login_failure(
            credential.id, settings.web_login_max_fail, settings.web_login_lockout_minutes
        )
        if locked_until is not None:
            raise AuthError("login_locked", "登录失败次数过多，账号已临时锁定", 429)
        raise AuthError("login_failed", "用户名或密码错误", 401)

    if credential.status == "pending":
        raise AuthError("login_pending", "注册正在等待审核，通过后即可登录", 403)
    if credential.status == "rejected":
        raise AuthError("login_rejected", "注册审核未通过，如有疑问请联系管理员", 403)
    if credential.status == "disabled":
        raise AuthError("login_disabled", "账号已被停用", 403)

    now = _utcnow()
    token = secrets.token_urlsafe(32)
    sessions = WebSessionRepository()
    await sessions.create(
        WebSessionCreate(
            token=token,
            credential_id=credential.id,
            expires_at=now + timedelta(hours=settings.web_session_hours),
            ip=ip,
            user_agent=user_agent[:300],
        )
    )
    # 新会话已入池，超出并发上限时按创建时间挤掉最旧设备
    await sessions.enforce_max_concurrent(credential.id, settings.web_session_max_concurrent)
    await credentials.reset_login_failures(credential.id)
    saved = await credentials.find_by_id(credential.id)
    return LoginResult(credential=saved or credential, token=token)


async def resolve_session(token: str) -> SessionResult:
    """由会话令牌解析出凭据、关联身份与会话记录。

    令牌缺失/过期/被 TTL 回收、身份非 active、身份已被删除或 identity_id 未回填，
    一律抛 AuthError(401)，由依赖层转成 401/403。
    """
    ensure_web_enabled()
    session = await WebSessionRepository().find_by_token(token)
    if session is None or session.expired:
        raise AuthError("unauthorized", "登录状态已失效，请重新登录", 401)
    credential = await WebCredentialRepository().find_by_id(session.credential_id)
    if credential is None:
        raise AuthError("unauthorized", "登录状态已失效，请重新登录", 401)
    if credential.status != "active":
        raise AuthError("forbidden", "账号不可用，请联系管理员", 403)
    if not credential.identity_id:
        raise AuthError("unauthorized", "账号数据不完整，请重新登录", 401)
    # 滑动过期：每次成功访问顺延
    await WebSessionRepository().touch(session.token, settings.web_session_hours)
    return SessionResult(credential=credential, identity_id=credential.identity_id, session=session)


async def resolve_token(token: str) -> tuple[WebCredentialInDB, str]:
    """由会话令牌解析出身份与其关联身份 ID（供 `require_current_identity` 使用）。"""
    result = await resolve_session(token)
    return result.credential, result.identity_id


async def logout(token: str) -> bool:
    """注销：删除当前会话令牌。"""
    return await WebSessionRepository().delete_by_token(token)


async def change_password(
    credential_id: str,
    current_password: str,
    new_password: str,
    keep_session_id: str = "",
) -> int:
    """用户自助改密：校验旧密码 → 写入新哈希 → 保留当前会话、踢掉其他设备。

    与管理员 `reset_password` 的区别：本函数面向已登录用户，必须验旧密码，
    且只下线"其他"会话，避免用户改完密码就被自己的当前设备踢出。

    Returns:
        被下线的其他会话数量
    Raises:
        AuthError: 凭据不存在、旧密码错误、新旧相同、新密码不合策略、写库失败
    """
    repo = WebCredentialRepository()
    credential = await repo.find_by_id(credential_id)
    if credential is None:
        raise AuthError("credential_not_found", "账号不存在", 404)
    if not verify_password(current_password, credential.password_hash):
        raise AuthError("invalid_current_password", "当前密码不正确", 400)
    if verify_password(new_password, credential.password_hash):
        raise AuthError("same_password", "新密码不能与当前密码相同", 400)
    validate_password(credential.username, new_password)

    if not await repo.set_password(credential_id, hash_password(new_password)):
        raise AuthError("password_change_failed", "密码修改失败，请稍后重试", 500)

    revoked = await WebSessionRepository().delete_others(credential_id, keep_session_id)
    logger.info(
        "网页身份 %s 自助修改密码，已下线其他会话 %d 个", credential.username, revoked
    )
    return revoked


async def approve(credential_id: str, reviewed_by: str, note: str = "") -> bool:
    """审核通过：置 active 并留痕。"""
    return await WebCredentialRepository().set_status(
        credential_id, "active", reviewed_by=reviewed_by, review_note=note, review=True
    )


async def reject(credential_id: str, reviewed_by: str, note: str = "") -> bool:
    """审核拒绝：置 rejected（触发 24h 重注冷却）并清空既有会话。"""
    ok = await WebCredentialRepository().set_status(
        credential_id, "rejected", reviewed_by=reviewed_by, review_note=note, review=True
    )
    if ok:
        await WebSessionRepository().delete_by_credential(credential_id)
    return ok


async def set_disabled(credential_id: str, disabled: bool, operator: str, note: str = "") -> bool:
    """禁用/启用账号：禁用同时清空全部会话，实现"发消息立即被拒"。"""
    ok = await WebCredentialRepository().set_status(
        credential_id, "disabled" if disabled else "active", reviewed_by=operator, review_note=note
    )
    if ok and disabled:
        await WebSessionRepository().delete_by_credential(credential_id)
    return ok


async def reset_password(credential_id: str, new_password: str, operator: str) -> bool:
    """管理员重置密码：写入新哈希并强制所有旧会话下线。"""
    username = ""
    credential = await WebCredentialRepository().find_by_id(credential_id)
    if credential is not None:
        username = credential.username
    validate_password(username, new_password)
    ok = await WebCredentialRepository().set_password(credential_id, hash_password(new_password))
    if ok:
        await WebSessionRepository().delete_by_credential(credential_id)
    return ok
