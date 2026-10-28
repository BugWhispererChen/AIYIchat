"""C 端网页凭据层单元测试（mongomock 内存库）。

覆盖计划 P1：
- app/utils/password_hashing：哈希生成/验算/格式判定，以及 app/admin/auth 的 re-export 不断链
- WebCredentialRepository：用户名唯一占位、状态流转、登录失败锁定、拒绝冷却查询
- WebSessionRepository：令牌校验、滑动过期、并发上限踢出、按身份清理
- 索引：web_credentials.username 唯一、web_sessions.token 唯一 + expires_at TTL
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import mongo
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
from app.models.profile import PromptProfileCreate
from app.models.quota_template import QuotaTemplateCreate
from app.models.web_credential import WebConsent, WebCredentialCreate
from app.models.web_session import WebSessionCreate, WebSessionInDB
from app.services import auth_service
from app.services.auth_service import AuthError
from app.utils.password_hashing import hash_password, is_hashed, verify_password
from tools.test.db_test_utils import run_with_db


def _now() -> datetime:
    """当前 UTC 时间（aware），统一测试内时间基准。"""
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """补齐 tzinfo：MongoDB 读回的 datetime 一律是 naive UTC，比较前统一转 aware。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    """转成 MongoDB 实际存储的 naive UTC 形态。"""
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _credential(username: str = "alice", password: str = "secret123") -> WebCredentialCreate:
    """构造测试用身份创建对象。"""
    return WebCredentialCreate(
        account_id="account-1",
        username=username,
        password_hash=hash_password(password),
        created_ip="127.0.0.1",
        consent=WebConsent(age_confirmed=True, age_confirmed_at=_now()),
    )


async def _only_identity(credential):
    identities = await IdentityRepository().list_by_account(credential.account_id)
    assert len(identities) == 1
    return identities[0]


class TestPasswordHashing:
    """密码哈希工具测试集。"""

    def test_roundtrip(self) -> None:
        """哈希可验算，错误密码与非法格式均拒绝。"""
        hashed = hash_password("passw0rd")
        assert is_hashed(hashed)
        assert hashed.startswith("pbkdf2$600000$")
        assert verify_password("passw0rd", hashed) is True
        assert verify_password("Passw0rd", hashed) is False
        assert verify_password("passw0rd", "not-a-hash") is False
        assert verify_password("passw0rd", "pbkdf2$bad$bad$bad") is False

    def test_same_password_different_salt(self) -> None:
        """同一密码两次哈希结果不同（随机盐），且均可验算。"""
        a, b = hash_password("passw0rd"), hash_password("passw0rd")
        assert a != b
        assert verify_password("passw0rd", a) and verify_password("passw0rd", b)

    def test_admin_auth_reexport_intact(self) -> None:
        """app/admin/auth 的既有引用（hash_password/_verify_hash/_is_hashed）不断链。"""
        from app.admin import auth

        hashed = auth.hash_password("legacy-pw")
        assert auth._is_hashed(hashed)
        assert auth._verify_hash("legacy-pw", hashed) is True
        assert auth._verify_hash("wrong", hashed) is False


class TestUsernameNormalization:
    """用户名规范化测试集。"""

    def test_normalize(self) -> None:
        """去空白并转小写。"""
        from app.models.web_credential import normalize_username

        assert normalize_username("  Alice01 ") == "alice01"
        assert normalize_username("BOB") == "bob"


class TestWebCredentialRepository:
    """网页凭据仓库测试集。"""

    def test_indexes_created(self) -> None:
        """web_credentials 与 web_sessions 索引随 connect_db 建齐。

        注意：MongoDB 返回的 datetime 一律为 naive UTC，mongomock 亦按 TTL 索引
        立即回收 `expires_at` 已过期的文档，故本用例只校验索引规格本身。
        """

        async def body():
            db = mongo.get_database()

            def keys(info: dict) -> set[tuple[str, ...]]:
                """把 index_information 的 key 规格归纳为字段名元组集合。"""
                return {tuple(field for field, _ in spec["key"]) for spec in info.values()}

            ident_info = await db.web_credentials.index_information()
            sess_info = await db.web_sessions.index_information()
            assert ("username",) in keys(ident_info)
            assert ("account_id",) in keys(ident_info)
            assert ("status", "created_at") in keys(ident_info)
            assert ident_info["username_1"]["unique"] is True
            assert ("token",) in keys(sess_info)
            assert sess_info["token_1"]["unique"] is True
            assert sess_info["expires_at_1"]["expireAfterSeconds"] == 0

        run_with_db(body)

    def test_insert_and_find(self) -> None:
        """写入后可按用户名/ID/user_id 查询，默认状态为 pending。"""

        async def body():
            repo = WebCredentialRepository()
            created = await repo.insert(_credential("alice"))
            assert created is not None
            assert created.status == "pending"
            assert created.is_active is False
            assert (await repo.find_by_username("alice")).id == created.id
            assert (await repo.find_by_id(created.id)).username == "alice"
            assert await repo.find_by_username("nobody") is None

        run_with_db(body)

    def test_duplicate_username_rejected(self) -> None:
        """同名并发注册：第二个得到 None（唯一索引原子占位）。

        大小写变体由服务层在入库前统一规范化后再占位，仓库层只按字面值判重。
        """

        async def body():
            repo = WebCredentialRepository()
            assert await repo.insert(_credential("alice")) is not None
            assert await repo.insert(_credential("alice")) is None
            assert await repo.count_by_status("pending") == 1

        run_with_db(body)

    def test_set_account_id_and_status(self) -> None:
        """回填账号归属与审核状态流转。"""

        async def body():
            repo = WebCredentialRepository()
            ident = await repo.insert(_credential("bob"))
            await repo.register_login_failure(ident.id, max_fail=99, lockout_minutes=5)
            assert await repo.set_account_id(ident.id, "account-2") is True
            owned = await repo.find_by_account_id("account-2")
            assert len(owned) == 1 and owned[0].id == ident.id
            assert await repo.set_status(
                ident.id, "active", reviewed_by="admin", review_note="资料齐全", review=True
            ) is True
            refreshed = await repo.find_by_id(ident.id)
            assert refreshed.status == "active" and refreshed.is_active is True
            assert refreshed.reviewed_by == "admin" and refreshed.reviewed_at is not None
            assert refreshed.failed_count == 0 and refreshed.locked_until is None

        run_with_db(body)

    def test_login_failure_locks(self) -> None:
        """连续失败达阈值触发锁定，锁定生效后 reset 解除。"""

        async def body():
            repo = WebCredentialRepository()
            ident = await repo.insert(_credential("carol"))
            ident_id = ident.id
            for i in range(4):
                assert await repo.register_login_failure(ident_id, 5, 15) is None
                assert (await repo.find_by_id(ident_id)).failed_count == i + 1
            locked_until = await repo.register_login_failure(ident_id, 5, 15)
            assert locked_until is not None
            stored = await repo.find_by_id(ident_id)
            assert stored.is_locked is True
            assert stored.failed_count == 0
            assert await repo.reset_login_failures(ident_id) is True
            assert (await repo.find_by_id(ident_id)).is_locked is False

        run_with_db(body)

    def test_lock_expiry(self) -> None:
        """locked_until 过期后不再视为锁定。"""

        async def body():
            repo = WebCredentialRepository()
            ident = await repo.insert(_credential("dave"))
            await repo._collection().update_one(
                {"id": ident.id},
                {"$set": {"locked_until": _now() - timedelta(minutes=1)}},
            )
            assert (await repo.find_by_id(ident.id)).is_locked is False

        run_with_db(body)

    def test_find_recent_rejected(self) -> None:
        """拒绝冷却期内可按用户名命中，超期或不符状态则放行。"""

        async def body():
            repo = WebCredentialRepository()
            ident = await repo.insert(_credential("erin"))
            assert await repo.find_recent_by_username("erin", 24, "rejected") is None
            await repo.set_status(ident.id, "rejected", reviewed_by="admin", review=True)
            assert (
                await repo.find_recent_by_username("erin", 24, "rejected")
            ) is not None
            # 改写审核时间到 25 小时前 → 冷却期结束
            await repo._collection().update_one(
                {"id": ident.id}, {"$set": {"reviewed_at": _now() - timedelta(hours=25)}}
            )
            assert await repo.find_recent_by_username("erin", 24, "rejected") is None

        run_with_db(body)

    def test_set_password_and_delete(self) -> None:
        """重置密码写入新哈希；删除身份后不可查。"""

        async def body():
            repo = WebCredentialRepository()
            ident = await repo.insert(_credential("frank"))
            new_hash = hash_password("newpass1")
            assert await repo.set_password(ident.id, new_hash) is True
            assert verify_password("newpass1", (await repo.find_by_id(ident.id)).password_hash)
            assert await repo.delete_by_id(ident.id) is True
            assert await repo.find_by_id(ident.id) is None

        run_with_db(body)

    def test_list_by_status(self) -> None:
        """待审核列表按状态分页返回。"""

        async def body():
            repo = WebCredentialRepository()
            for name in ("u1", "u2", "u3"):
                await repo.insert(_credential(name))
            one = await repo.insert(_credential("u4"))
            await repo.set_status(one.id, "active", review=True)
            pending = await repo.list_by_status("pending")
            assert {i.username for i in pending} == {"u1", "u2", "u3"}
            assert await repo.count_by_status("active") == 1
            assert len(await repo.list_all()) == 4

        run_with_db(body)


class TestWebSessionRepository:
    """网页会话仓库测试集。"""

    def _session(self, token: str, credential_id: str, hours: int = 24) -> WebSessionCreate:
        """构造会话创建对象。"""
        return WebSessionCreate(
            token=token,
            credential_id=credential_id,
            expires_at=_now() + timedelta(hours=hours),
            ip="127.0.0.1",
            user_agent="pytest",
        )

    def test_create_and_find_by_token(self) -> None:
        """签发后可按 token 查询，空 token 直接返回 None。"""

        async def body():
            repo = WebSessionRepository()
            created = await repo.create(self._session("t1", "id1"))
            assert created.expired is False
            assert created.remaining_seconds is not None
            assert (await repo.find_by_token("t1")).id == created.id
            assert await repo.find_by_token("") is None
            assert await repo.find_by_token("nope") is None

        run_with_db(body)

    def test_model_expiry_and_validity(self) -> None:
        """过期判定与会话-状态绑定：aware 与 naive（Mongo 读回形态）两种时间都要正确。"""
        expired = WebSessionInDB(
            **self._session("a", "id1", hours=-1).model_dump()
        )
        assert expired.expired is True
        assert expired.remaining_seconds is None
        assert expired.is_valid_for("active") is False

        live = WebSessionInDB(**self._session("b", "id1", hours=2).model_dump())
        assert live.expired is False
        assert live.is_valid_for("active") is True
        # 管理员禁用/拒绝身份后，未过期 token 也必须立即失效
        assert live.is_valid_for("disabled") is False
        assert live.is_valid_for("pending") is False

        naive_doc = {
            "_id": "x",
            "token": "c",
            "credential_id": "id1",
            "created_at": _naive_utc(_now() - timedelta(hours=3)),
            "last_activity": _naive_utc(_now() - timedelta(hours=3)),
            "expires_at": _naive_utc(_now() - timedelta(seconds=1)),
        }
        assert WebSessionInDB.from_mongo(naive_doc).expired is True

    def test_expired_session_not_reachable(self) -> None:
        """已过期会话不可再命中（真实库由应用层判过期，mongomock 由 TTL 索引回收）。"""

        async def body():
            repo = WebSessionRepository()
            await repo.create(self._session("live", "id1", hours=24))
            # 直接把 expires_at 改到过去 → 模拟会话到期
            await repo._collection().update_one(
                {"token": "live"}, {"$set": {"expires_at": _naive_utc(_now() - timedelta(minutes=1))}}
            )
            assert await repo.find_by_token("live") is None

        run_with_db(body)

    def test_touch_sliding_expiry(self) -> None:
        """滑动过期：touch 后 expires_at 顺延。"""

        async def body():
            repo = WebSessionRepository()
            created = await repo.create(self._session("t2", "id1", hours=1))
            before = _utc(created.expires_at)
            assert await repo.touch("t2", 24) is True
            after = _utc((await repo.find_by_token("t2")).expires_at)
            assert after > before
            assert (after - _now()).total_seconds() > 23 * 3600
            assert await repo.touch("missing-token", 24) is False

        run_with_db(body)

    def test_enforce_max_concurrent_kicks_oldest(self) -> None:
        """并发上限：超出时按创建时间踢掉最旧会话。"""

        async def body():
            repo = WebSessionRepository()
            # 同一身份签发 5 个会话（created_at 相同则插入顺序保持）
            for i in range(5):
                await repo.create(self._session(f"t{i}", "id1"))
            removed = await repo.enforce_max_concurrent("id1", 3)
            assert removed == 2
            tokens = {s.token for s in await repo.list_by_credential("id1")}
            assert tokens == {"t2", "t3", "t4"}
            assert await repo.enforce_max_concurrent("id1", 3) == 0

        run_with_db(body)

    def test_delete_by_token_and_credential(self) -> None:
        """注销单个会话与清空身份全部会话。"""

        async def body():
            repo = WebSessionRepository()
            await repo.create(self._session("t1", "id1"))
            await repo.create(self._session("t2", "id1"))
            await repo.create(self._session("t3", "id2"))
            assert await repo.delete_by_token("t1") is True
            assert await repo.delete_by_token("t1") is False
            assert await repo.delete_by_credential("id1") == 1
            assert await repo.list_by_credential("id1") == []
            assert len(await repo.list_by_credential("id2")) == 1

        run_with_db(body)

    def test_delete_by_id_and_others(self) -> None:
        """按会话 ID 注销，以及"保留当前设备"的下线其他会话。

        存量会话可能缺 id 字段：from_mongo 以 _id 兜底、删除端同时认 id 与 _id，
        否则老会话在设备上列表里看得见却下不掉。
        """

        async def body():
            repo = WebSessionRepository()
            keep = await repo.create(self._session("keep", "id1"))
            assert await repo.create(self._session("other", "id1"))
            assert await repo.create(self._session("peer", "id2"))

            # 跨凭据的下线请求一律无效
            assert await repo.delete_by_id("id1", "nope") is False
            assert await repo.delete_by_credential("id2") == 1

            assert await repo.delete_by_id("id1", keep.id) is True
            assert await repo.find_by_token("keep") is None

            # 无 id 字段的存量会话：按 _id 仍可定位
            legacy_doc = {"token": "legacy", "credential_id": "id1",
                          "expires_at": _naive_utc(_now() + timedelta(hours=2)),
                          "created_at": _naive_utc(_now())}
            inserted = await repo._collection().insert_one(legacy_doc)
            assert await repo.delete_by_id("id1", str(inserted.inserted_id)) is True

            assert await repo.delete_others("id1", "") == 1
            fresh = await repo.create(self._session("me", "id1"))
            await repo.create(self._session("elsewhere", "id1"))
            assert await repo.delete_others("id1", fresh.id) == 1
            assert await repo.find_by_token("me") is not None

        run_with_db(body)

    def test_duplicate_token_rejected(self) -> None:
        """token 唯一索引：重复写入抛错（签发端用随机 token，理论不可达，仅作防护）。"""

        async def body():
            from pymongo.errors import DuplicateKeyError

            repo = WebSessionRepository()
            await repo.create(self._session("dup", "id1"))
            with pytest.raises(DuplicateKeyError):
                await repo.create(self._session("dup", "id2"))

        run_with_db(body)


# ── P2/P3：注册编排与登录会话 ──────────────────────────────────────────

# 客户端 IP 一律使用文档测试网段（RFC 5737），严禁写入真实服务器地址
CLIENT_IP = "203.0.113.7"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled",
    "web_free_quota_template_id", "web_session_hours", "web_session_max_concurrent",
    "web_login_max_fail", "web_login_lockout_minutes",
)


@pytest.fixture(autouse=True)
def web_cfg():
    """模块级自动夹具：打开网页开关并设置可预期阈值，用例结束后复原。

    仓库层用例不读这些开关，故对全文件 autouse 不产生影响；
    单个用例需要验证开关关闭行为时在用例内改写即可。
    """
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_session_hours = 24
    settings.web_session_max_concurrent = 3
    settings.web_login_max_fail = 3
    settings.web_login_lockout_minutes = 15
    settings.web_free_quota_template_id = ""
    yield settings
    for key, value in saved.items():
        setattr(settings, key, value)


async def _seed_web_env(bot_id: str = "web-bot", slug: str = "lin-yu") -> str:
    """建系统账号 + platform=web 载体 Bot + 公共预置角色。

    注册已不再读取这些配置，保留种子是为了兼容聊天/额度相关用例。
    """
    account = await AccountRepository().create(AccountCreate(name="系统账号", remark="网页服务载体"))
    await BotConfigRepository().create(
        BotConfigCreate(id=bot_id, account_id=account.id, name="网页助手", platform="web")
    )
    await ProfileRepository().create(PromptProfileCreate(slug=slug, name="林语", visibility="public"))
    return bot_id


async def _register(username: str = "xiaochen", password: str = "passw0rd9", **overrides):
    """按默认合规勾选注册，单项测试可覆盖个别参数。"""
    kwargs = {"terms_accepted": True, "privacy_accepted": True, "age_confirmed": True}
    kwargs.update(overrides)
    return await auth_service.register(username=username, password=password, ip=CLIENT_IP, **kwargs)


async def _login(username: str = "xiaochen", password: str = "passw0rd9"):
    return await auth_service.login(username=username, password=password, ip=CLIENT_IP, user_agent="pytest")


class TestLegacySettingsDocSafety:
    """存量 app_settings 文档缺字段时的默认值安全（已知部署坑的回归锁）。

    存量库的 app_settings 文档不会重新 seed，缺失字段由 `from_mongo` 用
    AppSettingsDocument 的模型默认值补齐，因此"默认关闭"必须在
    `_RuntimeSettings` 与 `AppSettingsDocument` 两处同时写 False，
    否则老库一启动就会变成开启。
    """

    def test_missing_fields_do_not_enable_web_auth(self) -> None:
        """老库文档（无 web_* 字段）经 reload 后开关仍为关闭，且两处默认值一致。"""
        from app.config import settings
        from app.models.app_settings import AppSettingsDocument

        legacy_doc = {
            "_id": "default",
            "admin_username": "admin",
            "context_window_size": 20,
        }
        doc = AppSettingsDocument.from_mongo(legacy_doc)
        flat = doc.to_flat_dict
        assert "web_auth_enabled" in flat and flat["web_auth_enabled"] is False
        assert flat["web_register_enabled"] is False
        assert flat["web_register_require_review"] is True

        snapshot = {name: getattr(settings, name) for name in settings._fields()}
        try:
            # 先把运行时值置成"已开启"，模拟误配状态：老库文档 reload 后必须被模型默认值翻回关闭
            settings.web_auth_enabled = True
            settings.web_register_enabled = True
            # 模拟启动/热重载时的真实路径：_apply_dict 只遍历 _fields()
            settings._apply_dict(flat)
            assert settings.web_auth_enabled is False
            assert settings.web_register_enabled is False
            assert settings.web_register_require_review is True
            # 四处一致性：_fields() 必须收录全部 web_* 键，否则值会卡死在类默认值
            for name in AppSettingsDocument.model_fields:
                if name.startswith("web_"):
                    assert name in settings._fields(), f"{name} 未登记到 _fields()"
                    assert name in flat, f"{name} 未出现在 AppSettingsDocument"
        finally:
            for name, value in snapshot.items():
                setattr(settings, name, value)


class TestExperienceQuotaCoversFirstTurn:
    """内置体验额度必须支撑首条消息（额度单位为 Token，预检含输出上限）。"""

    def test_new_identity_passes_engine_precheck(self) -> None:
        """用引擎的真实预检公式校验：新注册账号首条消息不得被判"额度不足"。

        回归意义：文字额度按 Token 计且预检要求 `剩余 >= 输入估算 + chat_max_tokens`，
        若把体验额度写成"200 条"这类小数值，新账号一发言即被拒且只表现为一句提示，
        很容易在联调中被忽略。
        """
        from app.conversation.engine import ConversationEngine
        from app.utils.token_estimate import estimate_tokens

        async def body():
            await _seed_web_env()
            ident = await _register("firstturn")
            identity = await _only_identity(ident)
            engine = ConversationEngine(bot_id="web-bot")
            required = estimate_tokens("你好，请陪我聊聊天，说说你今天过得怎么样") + settings.chat_max_tokens
            ok, reason = await engine._quota_precheck(identity.account_id, required)
            assert ok is True, f"新身份体验额度不足以发起首条消息: {reason}"
            quota = await QuotaRepository().find_by_account(identity.account_id)
            assert quota.text_quota.limit >= 10 * required, "体验额度应至少支撑约 10 轮对话"

        run_with_db(body)


class TestValidationRules:
    """用户名与密码策略测试集（不依赖数据库）。"""

    @pytest.mark.parametrize("raw", ["ab", "a" * 33, "has space", "admin", "root", "_lead"])
    def test_bad_usernames(self, raw: str) -> None:
        """非法用户名：长度越界、含空格、保留名、下划线开头。"""
        with pytest.raises(AuthError) as err:
            auth_service.validate_username(raw)
        assert err.value.code == "invalid_username"
        assert err.value.status_code == 400

    @pytest.mark.parametrize("raw", ["Alice01 ", " BOB_2 ", "user_123"])
    def test_good_usernames(self, raw: str) -> None:
        """合法用户名返回规范化小写值。"""
        assert auth_service.validate_username(raw) == raw.strip().lower()

    @pytest.mark.parametrize(
        "pwd",
        [
            "short1",           # 不足 8 位
            "alllettersabc",    # 无数字
            "12345678",         # 无字母（且在黑名单）
            "password123",      # 黑名单
            "iloveyou",         # 黑名单
            "xiaochen123",      # 与用户名相同
        ],
    )
    def test_weak_passwords(self, pwd: str) -> None:
        """弱密码一律拒绝。"""
        with pytest.raises(AuthError) as err:
            auth_service.validate_password("xiaochen", pwd)
        assert err.value.code == "weak_password"

    def test_strong_password_passes(self) -> None:
        """满足长度与字母+数字要求的密码通过。"""
        auth_service.validate_password("xiaochen", "Zk9-trellis42")


class TestRegistration:
    """注册编排测试集。"""

    def test_creates_account_resources_without_service(self) -> None:
        """注册原子创建 Account+Quota+Identity+网页渠道，且不创建任何对话服务。"""

        async def body():
            await _seed_web_env()
            ident = await _register("xiaochen")
            assert ident.status == "pending"
            assert ident.account_id
            assert ident.created_ip == CLIENT_IP
            assert ident.consent.age_confirmed is True
            assert ident.consent.terms_version

            identity = await _only_identity(ident)
            assert identity.platform is None
            assert identity.platform_id == ""
            assert await ServiceRepository().list_by_account(ident.account_id) == []
            bots = await BotConfigRepository().list_by_account(ident.account_id)
            assert len(bots) == 1 and bots[0].platform == "web"
            quota = await QuotaRepository().find_by_account(identity.account_id)
            assert (quota.text_quota.limit, quota.voice_quota.limit, quota.image_quota.limit) == (
                auth_service.FREE_TEXT_LIMIT,
                auth_service.FREE_VOICE_LIMIT,
                auth_service.FREE_IMAGE_LIMIT,
            )
            assert quota.text_quota.expires_at is not None
            assert quota.text_quota.used == 0

        run_with_db(body)

    def test_password_hash_not_in_user(self) -> None:
        """密码哈希只落 web_credentials，绝不进入被 prompt 消费的 user 对象。"""

        async def body():
            await _seed_web_env()
            ident = await _register("hashsep")
            identity = await _only_identity(ident)
            identity_doc = await mongo.get_database().identities.find_one({"id": identity.id})
            assert "password_hash" not in identity_doc
            assert ident.password_hash.startswith("pbkdf2$")

        run_with_db(body)

    def test_duplicate_username(self) -> None:
        """重复用户名（含大小写变体）返回 409。"""

        async def body():
            await _seed_web_env()
            await _register("alice01")
            with pytest.raises(AuthError) as err:
                await _register("alice01")
            assert err.value.code == "username_taken" and err.value.status_code == 409
            with pytest.raises(AuthError) as err2:
                await _register(" Alice01 ")
            assert err2.value.code == "username_taken"
            # 占用者未释放：总数仍为 1
            assert await WebCredentialRepository().count_by_status("pending") == 1

        run_with_db(body)

    def test_minor_and_consent_rejected(self) -> None:
        """未满 14 周岁或未同意条款 → 拒绝且不留下任何身份记录。"""

        async def body():
            await _seed_web_env()
            with pytest.raises(AuthError) as err:
                await _register("kid", age_confirmed=False)
            assert err.value.code == "age_not_confirmed"
            with pytest.raises(AuthError) as err2:
                await _register("kid2", terms_accepted=False)
            assert err2.value.code == "consent_required"
            with pytest.raises(AuthError) as err3:
                await _register("kid3", privacy_accepted=False)
            assert err3.value.code == "consent_required"
            assert await WebCredentialRepository().list_all() == []
            assert await IdentityRepository().count() == 0

        run_with_db(body)

    def test_register_creates_account_web_bot_without_service(self) -> None:
        """注册为该账号创建一个 platform=web 渠道，但不装配任何服务。"""

        async def body():
            await _seed_web_env()
            ident = await _register("autobot")
            identity = await _only_identity(ident)
            bots = await BotConfigRepository().list_by_account(identity.account_id)
            assert len(bots) == 1
            assert bots[0].platform == "web"
            assert bots[0].account_id == identity.account_id
            assert bots[0].is_blocked is False
            assert await ServiceRepository().list_by_account(identity.account_id) == []

        run_with_db(body)

    def test_quota_template_and_fallback(self) -> None:
        """模板存在按模板发放；模板 ID 非法或不存在则回落内置体验额度。"""

        async def body():
            await _seed_web_env()
            tpl = await QuotaTemplateRepository().create(
                QuotaTemplateCreate(name="体验包", text_limit=999, voice_limit=9, image_limit=1, validity_days=7)
            )
            settings.web_free_quota_template_id = tpl.id
            ident = await _register("tpluser")
            identity = await _only_identity(ident)
            quota = await QuotaRepository().find_by_account(identity.account_id)
            assert quota.text_quota.limit == 999
            assert quota.text_quota.expires_at is not None

            # 非法 ObjectId 形态 → 回落内置
            settings.web_free_quota_template_id = "not-an-objectid"
            ident2 = await _register("badtpl")
            identity2 = await _only_identity(ident2)
            assert (await QuotaRepository().find_by_account(identity2.account_id)).text_quota.limit == (
                auth_service.FREE_TEXT_LIMIT
            )

            # 指向不存在但格式合法的模板 → 同样回落内置
            settings.web_free_quota_template_id = "0" * 24
            ident3 = await _register("ghosttpl")
            identity3 = await _only_identity(ident3)
            assert (await QuotaRepository().find_by_account(identity3.account_id)).voice_quota.limit == (
                auth_service.FREE_VOICE_LIMIT
            )

        run_with_db(body)

    def test_no_review_activates(self) -> None:
        """关闭审核开关时注册后直接 active。"""

        async def body():
            await _seed_web_env()
            settings.web_register_require_review = False
            ident = await _register("fastlane")
            assert ident.status == "active"
            assert ident.reviewed_by == "system"
            result = await _login("fastlane")
            assert result.token

        run_with_db(body)

    def test_register_without_any_profile_succeeds(self) -> None:
        """注册不读取角色配置：没有公共角色也能成功，只是不会自动建服务。"""

        async def body():
            await _seed_web_env()
            await mongo.get_database().profiles.delete_many({})
            ident = await _register("noprofile")
            assert ident.account_id
            identity = await _only_identity(ident)
            assert await ProfileRepository().list_active() == []
            assert await ServiceRepository().list_by_account(identity.account_id) == []

        run_with_db(body)

    def test_flags_off(self) -> None:
        """开关关闭时接口一律拒绝，且不写入任何数据。"""

        async def body():
            await _seed_web_env()
            settings.web_auth_enabled = False
            with pytest.raises(AuthError) as err:
                await _register("gated")
            assert err.value.code == "web_auth_disabled" and err.value.status_code == 403
            with pytest.raises(AuthError) as err2:
                await _login("gated")
            assert err2.value.code == "web_auth_disabled"
            settings.web_auth_enabled = True
            settings.web_register_enabled = False
            with pytest.raises(AuthError) as err3:
                await _register("gated")
            assert err3.value.code == "web_register_disabled"
            assert await WebCredentialRepository().list_all() == []

        run_with_db(body)


class TestReviewAndGovernance:
    """审核闭环与治理测试集。"""

    def test_pending_then_approve(self) -> None:
        """pending 不可登录，审核通过后可登录。"""

        async def body():
            await _seed_web_env()
            ident = await _register("reviewme")
            with pytest.raises(AuthError) as err:
                await _login("reviewme")
            assert err.value.code == "login_pending" and err.value.status_code == 403
            assert await auth_service.approve(ident.id, "admin", "资料完整") is True
            result = await _login("reviewme")
            assert result.credential.status == "active"
            assert result.token

        run_with_db(body)

    def test_reject_blocks_login_and_cooldown(self) -> None:
        """审核拒绝：不可登录，冷却期内不可重注，超期后同名可重注册。"""

        async def body():
            await _seed_web_env()
            ident = await _register("rejected1")
            assert await auth_service.reject(ident.id, "admin", "资料不实") is True
            with pytest.raises(AuthError) as err:
                await _login("rejected1")
            assert err.value.code == "login_rejected"
            with pytest.raises(AuthError) as err2:
                await _register("rejected1")
            assert err2.value.code == "register_cooldown"
            # 超冷却期 → 被拒记录清理，同名可重注册
            await WebCredentialRepository()._collection().update_one(
                {"id": ident.id},
                {"$set": {"reviewed_at": _naive_utc(_now() - timedelta(hours=25))}},
            )
            fresh = await _register("rejected1", password="newpass77")
            assert fresh.status == "pending"
            assert fresh.account_id  # 新记录已重建账号、身份与默认服务
            assert await WebCredentialRepository().count_by_status("pending") == 1

        run_with_db(body)

    def test_disable_kicks_sessions(self) -> None:
        """禁用身份：会话立即清空，正在使用的 token 失效。"""

        async def body():
            await _seed_web_env()
            ident = await _register("banned1")
            await auth_service.approve(ident.id, "admin")
            result = await _login("banned1")
            credential = await auth_service.resolve_token(result.token)
            assert credential.id == ident.id and credential.account_id == ident.account_id
            assert await auth_service.set_disabled(ident.id, True, "admin", "违规") is True
            with pytest.raises(AuthError) as err:
                await auth_service.resolve_token(result.token)
            assert err.value.status_code == 401
            assert await WebSessionRepository().list_by_credential(ident.id) == []
            # 重新启用后需重新登录
            await auth_service.set_disabled(ident.id, False, "admin")
            again = await _login("banned1")
            assert (await auth_service.resolve_token(again.token)).status == "active"

        run_with_db(body)

    def test_reset_password_revokes_sessions(self) -> None:
        """管理员重置密码：旧会话全部失效，新密码可登录、旧密码被拒。"""

        async def body():
            await _seed_web_env()
            ident = await _register("pwreset")
            await auth_service.approve(ident.id, "admin")
            old = await _login("pwreset")
            assert await auth_service.reset_password(ident.id, "brandnew99", "admin") is True
            with pytest.raises(AuthError) as err:
                await auth_service.resolve_token(old.token)
            assert err.value.status_code == 401
            with pytest.raises(AuthError) as err2:
                await _login("pwreset", "passw0rd9")
            assert err2.value.code == "login_failed"
            assert (await _login("pwreset", "brandnew99")).token

        run_with_db(body)


class TestLoginProtection:
    """登录防暴力破解与会话并发测试集。"""

    def test_unknown_user_and_wrong_password(self) -> None:
        """未知用户名与错误密码返回同一提示，避免账号枚举。"""

        async def body():
            await _seed_web_env()
            ident = await _register("enumtest")
            await auth_service.approve(ident.id, "admin")
            with pytest.raises(AuthError) as missing:
                await _login("nobody-here")
            with pytest.raises(AuthError) as wrong:
                await _login("enumtest", "wrongpass123")
            assert missing.value.message == wrong.value.message == "用户名或密码错误"

        run_with_db(body)

    def test_lockout_after_max_failures(self) -> None:
        """连续失败达阈值即锁定（第 N 次失败当场锁定），锁定期内正确密码也被拒。"""

        async def body():
            await _seed_web_env()
            ident = await _register("lockme")
            await auth_service.approve(ident.id, "admin")
            codes: list[str] = []
            for _ in range(3):
                with pytest.raises(AuthError) as err:
                    await _login("lockme", "badpassword1")
                codes.append(err.value.code)
            assert codes[:2] == ["login_failed", "login_failed"]
            assert codes[2] == "login_locked"  # 达到 web_login_max_fail 当场锁定
            assert (await WebCredentialRepository().find_by_id(ident.id)).is_locked is True
            with pytest.raises(AuthError) as locked:
                await _login("lockme", "passw0rd9")
            assert locked.value.code == "login_locked" and locked.value.status_code == 429

        run_with_db(body)

    def test_max_concurrent_sessions(self) -> None:
        """并发上限 3：第 4 次登录挤掉最旧会话，其 token 立即失效。"""

        async def body():
            await _seed_web_env()
            ident = await _register("multi")
            await auth_service.approve(ident.id, "admin")
            tokens = [(await _login("multi")).token for _ in range(4)]
            kept = {s.token for s in await WebSessionRepository().list_by_credential(ident.id)}
            assert len(kept) == 3
            assert tokens[0] not in kept
            with pytest.raises(AuthError) as err:
                await auth_service.resolve_token(tokens[0])
            assert err.value.status_code == 401
            assert (await auth_service.resolve_token(tokens[3])).id == ident.id

        run_with_db(body)

    def test_logout(self) -> None:
        """注销当前会话后 token 不可用，二次注销返回 False。"""

        async def body():
            await _seed_web_env()
            ident = await _register("logout1")
            await auth_service.approve(ident.id, "admin")
            result = await _login("logout1")
            assert await auth_service.logout(result.token) is True
            assert await auth_service.logout(result.token) is False
            with pytest.raises(AuthError):
                await auth_service.resolve_token(result.token)

        run_with_db(body)

    def test_resolve_requires_web_enabled(self) -> None:
        """总开关关闭后，已签发 token 一律失效（回滚式停服）。"""

        async def body():
            await _seed_web_env()
            ident = await _register("killswitch")
            await auth_service.approve(ident.id, "admin")
            result = await _login("killswitch")
            await auth_service.resolve_token(result.token)
            settings.web_auth_enabled = False
            with pytest.raises(AuthError) as err:
                await auth_service.resolve_token(result.token)
            assert err.value.code == "web_auth_disabled"

        run_with_db(body)
