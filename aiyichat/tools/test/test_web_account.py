"""C 端账号中心（资料 / 密码 / 设备 / 额度 / 数据与隐私 / 注销）接口回归测试。

覆盖要点：
- 账号端点不要求活跃服务：服务被删空的账号仍能改密、清数据、注销；
- 归属字段由令牌反查，客户端伪造无效；业务错误一律 400 而非 401（401 会把人踢回登录页）；
- 改密保留当前会话、踢掉其他设备；设备列表不泄露令牌；
- 额度过期判定（Mongo 读回 naive 时间）与订单字段裁剪；
- 导出脱敏：密码哈希、令牌、注册 IP、记忆向量一律不出场；
- 清理与注销只动本人数据，共享组含他人身份时拒绝；
- 注销后凭据、会话、订单、主动消息、安全事件、媒体与账号本体全部归零，用户名可复用。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.api import web_account_api
from app.config import settings
from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.injection_snapshot_repo import InjectionSnapshotRepository
from app.db.repositories.media_repo import MediaRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.order_repo import OrderRepository
from app.db.repositories.proactive_repo import ProactiveRecordRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.safety_event_repo import SafetyEventRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.db.repositories.web_session_repo import WebSessionRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.identity import IdentityCreate
from app.models.injection_snapshot import InjectionSnapshotInDB
from app.models.order import OrderCreate
from app.models.proactive_record import ProactiveRecord
from app.models.profile import PromptProfileCreate
from app.models.quota import AccountQuotaUpdate, QuotaEntry
from app.models.safety_event import SafetyEvent
from app.models.service import ServiceCreate, ServiceUpdate
from app.models.service_memory import MemoryFact
from app.services import auth_service
from app.utils.rate_limit import account_limiter, login_limiter, register_limiter

WEB_BOT_ID = "web-bot-account"
PUBLIC_SLUG = "lin-yu"
PASSWORD = "passw0rd9"
NEW_PASSWORD = "brand0wd7"
CLIENT_IP = "203.0.113.31"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
    "web_free_quota_template_id", "web_session_hours", "web_session_max_concurrent",
    "web_login_max_fail", "web_login_lockout_minutes",
    "web_rate_limit_register", "web_rate_limit_login", "web_max_profiles", "web_max_services",
)


@pytest.fixture(autouse=True)
def web_flags():
    """开启网页鉴权与注册、关验证码与限流，用例后复原全局配置与内存计数器。"""
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = False
    settings.web_rate_limit_register = 100
    settings.web_rate_limit_login = 100
    settings.web_bot_id = WEB_BOT_ID
    settings.web_default_profile_slug = PUBLIC_SLUG
    settings.web_free_quota_template_id = ""
    register_limiter.reset()
    login_limiter.reset()
    account_limiter.reset()
    yield
    for key, value in saved.items():
        setattr(settings, key, value)
    register_limiter.reset()
    login_limiter.reset()
    account_limiter.reset()


async def _setup_app(db_name: str) -> Any:
    """内存库 + 系统账号 / web Bot / 一个公共角色。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()[db_name]
    account = await AccountRepository().create(AccountCreate(name="系统账号"))
    await BotConfigRepository().create(
        BotConfigCreate(id=WEB_BOT_ID, account_id=account.id, name="网页助手", platform="web")
    )
    await ProfileRepository().create(
        PromptProfileCreate(slug=PUBLIC_SLUG, name="林语", visibility="public")
    )
    from app.main import app

    return app


async def _teardown() -> None:
    mongo._db = None


def _client(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-Forwarded-For": CLIENT_IP},
    )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime) -> datetime:
    """转成 MongoDB 实际存储的 naive UTC 形态（存量数据即为此形态）。"""
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _signup(client: AsyncClient, username: str) -> str:
    """注册 → 审核通过 → 登录，返回 Bearer 令牌。"""
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username, "password": PASSWORD,
            "terms_accepted": True, "privacy_accepted": True, "age_confirmed": True,
        },
    )
    assert resp.status_code == 201, resp.text
    credential = await WebCredentialRepository().find_by_username(username)
    assert credential is not None
    assert await auth_service.approve(credential.id, "admin") is True
    return await _login(client, username)


async def _login(client: AsyncClient, username: str, password: str = PASSWORD) -> str:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


async def _identity_of(username: str) -> Any:
    """取该网页身份关联的 identity（测试里直接操作底层数据用）。"""
    credential = await WebCredentialRepository().find_by_username(username)
    assert credential is not None and credential.identity_id
    identity = await IdentityRepository().find_by_id(credential.identity_id)
    assert identity is not None
    return identity


async def _seed_full_data(username: str) -> dict[str, Any]:
    """给账号灌一份可清理/可导出/可注销的数据，返回关键 ID。"""
    credential = await WebCredentialRepository().find_by_username(username)
    assert credential is not None
    identity = await _identity_of(username)
    services = await ServiceRepository().list_by_identity(identity.id)
    assert services
    service = services[0]

    conv = await ConversationRepository().create(identity.id, service.profile_id, service.context_key)
    media_id = await MediaRepository().create(b"png-bytes", "image/png", "pic.png")
    await MessageRepository().create(
        conv.id, "user", "帮我画只猫", media_url=f"/api/media/{media_id}"
    )
    await MessageRepository().create(conv.id, "assistant", "好的，猫来了")
    # 引擎落消息时会同步会话计数，概览直接读该字段
    await ConversationRepository().update_after_message(conv.id, 2)
    await ServiceMemoryRepository().add_facts(
        service.context_key, [MemoryFact(content="身份在准备考研", embedding=[0.11, 0.22])]
    )
    await InjectionSnapshotRepository().save(
        InjectionSnapshotInDB(
            id="snap-1", service_id=service.context_key, conversation_id=conv.id,
            reply_text="好的，猫来了", injected_facts=[],
        )
    )
    await OrderRepository().create(
        OrderCreate(account_id=identity.account_id, template_name="体验包",
                    text_limit=1000, operator="admin", account_name="敏感账号名")
    )
    await ProactiveRecordRepository().create(
        ProactiveRecord(id="pro-1", identity_id=identity.id, platform_openid="openid",
                        bot_id=WEB_BOT_ID, stage=1, content="今天还好吗", status="sent")
    )
    await SafetyEventRepository().create(
        SafetyEvent(event_type="forbidden", platform="web", platform_id=username,
                    identity_id=identity.id, account_id=identity.account_id,
                    bot_id=WEB_BOT_ID, excerpt="敏感输入摘要")
    )
    return {
        "credential_id": credential.id,
        "identity": identity,
        "service": service,
        "conversation": conv,
        "media_id": media_id,
    }


def _run(coro: Any) -> None:
    asyncio.run(coro)


class _RecordingEngine:
    """替身引擎：记录收到的昵称，验证账号资料的贯通效果。"""

    calls: list[dict[str, Any]] = []

    def __init__(self, bot_id: str = "") -> None:
        self.bot_id = bot_id

    async def handle_message_stream(
        self,
        platform: str,
        platform_id: str,
        text: str,
        nickname: str = "",
        content_type: str = "text",
        service_id: str = "",
    ) -> AsyncIterator[str]:
        _RecordingEngine.calls.append(
            {"platform": platform, "platform_id": platform_id,
             "nickname": nickname, "service_id": service_id}
        )
        yield "ok"


def _with_fake_engine(run: Callable[[], Any]) -> None:
    import app.conversation.engine as engine_module

    original = engine_module.ConversationEngine
    _RecordingEngine.calls = []
    engine_module.ConversationEngine = _RecordingEngine  # type: ignore[misc]
    try:
        asyncio.run(run())
    finally:
        engine_module.ConversationEngine = original  # type: ignore[misc]


class TestAccountAccess:
    """账号中心的可访问性与归属。"""

    def test_requires_login(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_login")
            try:
                async with _client(app) as c:
                    for path in ("/profile", "/sessions", "/quota", "/data", "/memories"):
                        assert (await c.get("/api/web/account" + path)).status_code == 401, path
                    resp = await c.put("/api/web/account/profile", json={"nickname": "阿梨"})
                    assert resp.status_code == 401
            finally:
                await _teardown()

        _run(flow())

    def test_works_without_active_service(self) -> None:
        """服务被删空时账号中心仍可用——这正是用户自救的入口。"""
        async def flow() -> None:
            app = await _setup_app("acct_nosvc")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "nosvc")
                    identity = await _identity_of("nosvc")
                    assert await ServiceRepository().delete_by_account(identity.account_id) > 0

                    assert (await c.get("/api/auth/me", headers=_h(token))).status_code == 403
                    profile = await c.get("/api/web/account/profile", headers=_h(token))
                    assert profile.status_code == 200, profile.text
                    assert profile.json()["username"] == "nosvc"
                    assert (await c.get("/api/web/account/sessions", headers=_h(token))).status_code == 200
            finally:
                await _teardown()

        _run(flow())


class TestProfileModule:
    """资料更新。"""

    def test_update_persists_whitelisted_fields(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_profile")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "alice")
                    resp = await c.put(
                        "/api/web/account/profile",
                        headers=_h(token),
                        json={
                            "nickname": "阿梨", "self_description": "在读研究生",
                            "timezone": "Asia/Shanghai",
                            # 归属字段必须由服务端判定，客户端塞值一律无效
                            "username": "evil", "account_id": "other", "platform": "qq",
                        },
                    )
                    assert resp.status_code == 200, resp.text
                    body = resp.json()
                    assert body["nickname"] == "阿梨"
                    assert body["self_description"] == "在读研究生"
                    assert body["timezone"] == "Asia/Shanghai"
                    assert body["username"] == "alice"

                    identity = await _identity_of("alice")
                    assert identity.nickname == "阿梨"
                    assert identity.platform == "web"
            finally:
                await _teardown()

        _run(flow())

    def test_invalid_inputs_rejected(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_badinput")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "bob")
                    empty = await c.put(
                        "/api/web/account/profile", headers=_h(token),
                        json={"nickname": "   "},
                    )
                    assert empty.status_code == 400
                    assert empty.json()["code"] == "invalid_nickname"

                    bad_tz = await c.put(
                        "/api/web/account/profile", headers=_h(token),
                        json={"nickname": "阿波", "timezone": "Mars/Unknown"},
                    )
                    assert bad_tz.status_code == 400
                    assert bad_tz.json()["code"] == "invalid_timezone"

                    long_desc = await c.put(
                        "/api/web/account/profile", headers=_h(token),
                        json={"nickname": "阿波", "self_description": "字" * 601},
                    )
                    assert long_desc.status_code == 400
                    assert long_desc.json()["code"] == "invalid_description"
            finally:
                await _teardown()

        _run(flow())

    def test_nickname_flows_into_chat(self) -> None:
        """改完昵称，聊天链路必须用新称呼，且不再接受客户端伪造 nickname。"""
        async def flow() -> None:
            app = await _setup_app("acct_nick_chat")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "carol")
                    await c.put(
                        "/api/web/account/profile", headers=_h(token),
                        json={"nickname": "小卡"},
                    )
                    _RecordingEngine.calls = []
                    await c.post(
                        "/api/conversations/chat/stream",
                        headers=_h(token),
                        json={"text": "在吗", "nickname": "冒充者"},
                    )
                    assert _RecordingEngine.calls
                    assert _RecordingEngine.calls[0]["nickname"] == "小卡"
            finally:
                await _teardown()

        _with_fake_engine(flow)


class TestPasswordModule:
    """自助改密。"""

    def test_rejections_stay_400(self) -> None:
        """错旧密码 / 弱密码 / 与旧密码相同都必须是 400：401 会让前端把用户踢下线。"""
        async def flow() -> None:
            app = await _setup_app("acct_pw_reject")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "dave")
                    wrong = await c.post(
                        "/api/web/account/password", headers=_h(token),
                        json={"current_password": "nope1234", "new_password": NEW_PASSWORD},
                    )
                    assert wrong.status_code == 400
                    assert wrong.json()["code"] == "invalid_current_password"

                    weak = await c.post(
                        "/api/web/account/password", headers=_h(token),
                        json={"current_password": PASSWORD, "new_password": "abc"},
                    )
                    assert weak.status_code == 400
                    assert weak.json()["code"] == "weak_password"

                    same = await c.post(
                        "/api/web/account/password", headers=_h(token),
                        json={"current_password": PASSWORD, "new_password": PASSWORD},
                    )
                    assert same.status_code == 400
                    assert same.json()["code"] == "same_password"

                    # 失败若干次后当前会话依旧可用
                    assert (await c.get("/api/web/account/profile", headers=_h(token))).status_code == 200
            finally:
                await _teardown()

        _run(flow())

    def test_success_keeps_current_session_and_kicks_others(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_pw_ok")
            try:
                async with _client(app) as c:
                    mine = await _signup(c, "erin")
                    other = await _login(c, "erin")  # 第二台设备
                    assert (await c.get("/api/web/account/profile", headers=_h(other))).status_code == 200

                    resp = await c.post(
                        "/api/web/account/password", headers=_h(mine),
                        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                    )
                    assert resp.status_code == 200, resp.text
                    assert resp.json()["revoked_sessions"] == 1
                    assert (await c.get("/api/web/account/profile", headers=_h(mine))).status_code == 200
                    assert (await c.get("/api/web/account/profile", headers=_h(other))).status_code == 401

                    # 新密码可登录，旧密码失效
                    assert (await c.post("/api/auth/login",
                                         json={"username": "erin", "password": PASSWORD})).status_code == 401
                    assert (await _login(c, "erin", NEW_PASSWORD))
            finally:
                await _teardown()

        _run(flow())


class TestSessionModule:
    """登录设备管理。"""

    def test_list_masks_ip_and_marks_current(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_sessions")
            try:
                async with _client(app) as c:
                    mine = await _signup(c, "frank")
                    other = await _login(c, "frank")
                    resp = await c.get("/api/web/account/sessions", headers=_h(mine))
                    assert resp.status_code == 200, resp.text
                    body = resp.json()
                    assert len(body["items"]) == 2
                    assert body["max_concurrent"] == settings.web_session_max_concurrent

                    raw = resp.text
                    assert mine not in raw and other not in raw  # 令牌绝不外泄
                    assert CLIENT_IP not in raw  # IP 已脱敏
                    current = [i for i in body["items"] if i["current"]]
                    assert len(current) == 1
            finally:
                await _teardown()

        _run(flow())

    def test_revoke_rules(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_revoke")
            try:
                async with _client(app) as c:
                    mine = await _signup(c, "grace")
                    other = await _login(c, "grace")
                    items = (await c.get("/api/web/account/sessions", headers=_h(mine))).json()["items"]
                    target = [i for i in items if not i["current"]][0]["id"]

                    cur = [i for i in items if i["current"]][0]["id"]
                    self_kill = await c.delete(
                        f"/api/web/account/sessions/{cur}", headers=_h(mine)
                    )
                    assert self_kill.status_code == 400
                    assert self_kill.json()["code"] == "cannot_revoke_current"

                    gone = await c.delete("/api/web/account/sessions/nonexistent", headers=_h(mine))
                    assert gone.status_code == 404

                    ok = await c.delete(f"/api/web/account/sessions/{target}", headers=_h(mine))
                    assert ok.status_code == 200, ok.text
                    assert (await c.get("/api/web/account/profile", headers=_h(other))).status_code == 401

                    # 再来一台，然后一键下线其他
                    third = await _login(c, "grace")
                    revoke = await c.post("/api/web/account/sessions/revoke-others",
                                          headers=_h(mine), json={})
                    assert revoke.status_code == 200, revoke.text
                    assert revoke.json()["revoked"] == 1
                    assert (await c.get("/api/web/account/profile", headers=_h(third))).status_code == 401
                    assert (await c.get("/api/web/account/profile", headers=_h(mine))).status_code == 200
            finally:
                await _teardown()

        _run(flow())


class TestQuotaModule:
    """额度中心。"""

    def test_expiry_and_order_projection(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_quota")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "heidi")
                    identity = await _identity_of("heidi")
                    await QuotaRepository().create_or_update(
                        identity.account_id,
                        AccountQuotaUpdate(
                            text_quota=QuotaEntry(limit=1000, used=900,
                                                  expires_at=_naive(_now() + timedelta(days=5))),
                            voice_quota=QuotaEntry(limit=10, used=3,
                                                   expires_at=_naive(_now() - timedelta(days=1))),
                            image_quota=QuotaEntry(limit=0, used=0, expires_at=None),
                        ),
                    )
                    await OrderRepository().create(
                        OrderCreate(account_id=identity.account_id, template_name="月卡",
                                    text_limit=2000, validity_days=30, amount=9.9,
                                    operator="admin", account_name="web:heidi", remark="备注")
                    )

                    resp = await c.get("/api/web/account/quota", headers=_h(token))
                    assert resp.status_code == 200, resp.text
                    quota = resp.json()["quota"]
                    assert quota["text"]["remaining"] == 100
                    assert quota["text"]["expired"] is False
                    assert quota["text"]["expires_at"]
                    assert quota["voice"]["expired"] is True
                    assert quota["voice"]["remaining"] == 0  # 过期后剩余不可用
                    assert quota["image"]["expires_at"] == ""
                    assert quota["image"]["expired"] is False

                    orders = resp.json()["orders"]
                    assert orders["total"] == 1
                    item = orders["items"][0]
                    assert item["template_name"] == "月卡"
                    for leaked in ("operator", "account_id", "account_name", "template_id"):
                        assert leaked not in item
            finally:
                await _teardown()

        _run(flow())


class TestDataModule:
    """数据概览、记忆、导出与清理。"""

    def test_overview_counts(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_data")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "ivan")
                    await _seed_full_data("ivan")
                    resp = await c.get("/api/web/account/data", headers=_h(token))
                    assert resp.status_code == 200, resp.text
                    body = resp.json()
                    assert body["counts"]["conversations"] == 1
                    assert body["counts"]["messages"] == 2
                    assert body["counts"]["facts"] == 1
                    assert body["services"][0]["memory_facts"] == 1

                    mem = await c.get("/api/web/account/memories", headers=_h(token))
                    assert mem.status_code == 200
                    assert mem.json()["items"][0]["facts"][0]["content"] == "身份在准备考研"
                    assert "embedding" not in mem.text
            finally:
                await _teardown()

        _run(flow())

    def test_export_excludes_secrets(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_export")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "judy")
                    seeded = await _seed_full_data("judy")
                    resp = await c.get("/api/web/account/export", headers=_h(token))
                    assert resp.status_code == 200, resp.text
                    assert 'attachment; filename="aiyichat-account-judy.json"' in \
                        resp.headers["content-disposition"]
                    assert resp.headers["cache-control"] == "no-store"

                    body = resp.json()
                    assert body["account"]["username"] == "judy"
                    assert body["conversations"][0]["messages"][0]["content"] == "帮我画只猫"
                    assert body["memories"][0]["facts"][0]["content"] == "身份在准备考研"
                    assert body["truncated"] is False

                    raw = resp.text
                    for forbidden in ("password_hash", "embedding", "created_ip",
                                      "pbkdf2$", "bot_openids", '"token"', '"operator"'):
                        assert forbidden not in raw, forbidden
                    assert token not in raw
                    assert seeded["identity"].account_id not in raw
            finally:
                await _teardown()

        _run(flow())

    def test_clear_conversations_only_touches_own_data(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_clearconv")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "kate")
                    seeded = await _seed_full_data("kate")

                    # 另建一个无关账号的对话，验证不被连带删除
                    other = await AccountRepository().create(AccountCreate(name="other-acct"))
                    other_identity = await IdentityRepository().create(
                        IdentityCreate(account_id=other.id, name="other", platform="qq",
                                       platform_id="openid-other")
                    )
                    other_svc = await ServiceRepository().create(
                        ServiceCreate(name="other-svc", account_id=other.id,
                                      identity_id=other_identity.id, bot_id=WEB_BOT_ID,
                                      profile_id=PUBLIC_SLUG)
                    )
                    other_conv = await ConversationRepository().create(
                        other_identity.id, PUBLIC_SLUG, other_svc.id
                    )
                    await MessageRepository().create(other_conv.id, "user", "别人的对话")

                    unconfirmed = await c.post("/api/web/account/data/conversations/clear",
                                               headers=_h(token), json={"confirm": False})
                    assert unconfirmed.status_code == 400

                    resp = await c.post("/api/web/account/data/conversations/clear",
                                         headers=_h(token), json={"confirm": True})
                    assert resp.status_code == 200, resp.text
                    stats = resp.json()
                    assert stats["conversations"] == 1
                    assert stats["messages"] == 2
                    assert stats["media"] == 1
                    assert stats["snapshots"] == 1

                    assert await ConversationRepository().list_by_identity(seeded["identity"].id) == []
                    assert await MediaRepository().find_by_id(seeded["media_id"]) is None
                    assert await InjectionSnapshotRepository().list_by_service(
                        seeded["service"].context_key) == []
                    # 记忆保留，别人的数据不受影响
                    memory = await ServiceMemoryRepository().find_by_service(
                        seeded["service"].context_key)
                    assert memory is not None and len(memory.facts) == 1
                    assert await MessageRepository().get_all_for_conversation(other_conv.id)

                    after = await c.get("/api/web/account/data", headers=_h(token))
                    assert after.json()["counts"]["messages"] == 0
            finally:
                await _teardown()

        _run(flow())

    def test_clear_memory_requires_ownership(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_clearmem")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "leo")
                    seeded = await _seed_full_data("leo")
                    svc_id = seeded["service"].id

                    resp = await c.post(f"/api/web/account/data/memories/{svc_id}/clear",
                                         headers=_h(token), json={"confirm": True})
                    assert resp.status_code == 200, resp.text
                    assert resp.json()["facts"] == 1
                    assert await ServiceMemoryRepository().find_by_service(
                        seeded["service"].context_key) is None

                    stranger = await c.post("/api/web/account/data/memories/nope/clear",
                                             headers=_h(token), json={"confirm": True})
                    assert stranger.status_code == 404
            finally:
                await _teardown()

        _run(flow())

    def test_clear_memory_refuses_shared_group(self) -> None:
        """共享组里有他人服务时，记忆属于共同上下文，不允许单方清空。"""
        async def flow() -> None:
            app = await _setup_app("acct_sharedmem")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "mia")
                    identity = await _identity_of("mia")
                    group = "share-group-1"
                    await ServiceRepository().create(
                        ServiceCreate(name="他人伙伴", account_id="other-acct",
                                      identity_id="other-identity", bot_id=WEB_BOT_ID,
                                      profile_id=PUBLIC_SLUG, share_group_id=group)
                    )
                    mine = await ServiceRepository().list_by_identity(identity.id)
                    target = mine[0]
                    await ServiceRepository().update(
                        target.id, ServiceUpdate(share_group_id=group)
                    )
                    await ServiceMemoryRepository().add_facts(
                        group, [MemoryFact(content="共同记忆")]
                    )

                    resp = await c.post(f"/api/web/account/data/memories/{target.id}/clear",
                                         headers=_h(token), json={"confirm": True})
                    assert resp.status_code == 409, resp.text
                    assert resp.json()["code"] == "shared_context"
            finally:
                await _teardown()

        _run(flow())


class TestDeleteAccount:
    """注销：强确认、跨渠道保护、彻底清除。"""

    def test_requires_password_and_username(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_del_guard")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "nina")
                    bad_pw = await c.post("/api/web/account/delete", headers=_h(token),
                                          json={"current_password": "nope1234",
                                                "username_confirm": "nina"})
                    assert bad_pw.status_code == 400
                    assert bad_pw.json()["code"] == "invalid_current_password"

                    bad_name = await c.post("/api/web/account/delete", headers=_h(token),
                                            json={"current_password": PASSWORD,
                                                  "username_confirm": "someone"})
                    assert bad_name.status_code == 400
                    assert bad_name.json()["code"] == "username_mismatch"

                    # 两次失败都不能把账号怎么样
                    assert (await c.get("/api/web/account/profile", headers=_h(token))).status_code == 200
            finally:
                await _teardown()

        _run(flow())

    def test_blocks_when_other_identity_exists(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_del_multi")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "omar")
                    identity = await _identity_of("omar")
                    await IdentityRepository().create(
                        IdentityCreate(account_id=identity.account_id, name="qq-peer",
                                       platform="qq", platform_id="openid-qq")
                    )
                    resp = await c.post("/api/web/account/delete", headers=_h(token),
                                        json={"current_password": PASSWORD,
                                              "username_confirm": "omar"})
                    assert resp.status_code == 409, resp.text
                    assert resp.json()["code"] == "linked_identity_exists"
                    # 被拒后账号完好
                    assert (await c.get("/api/web/account/profile", headers=_h(token))).status_code == 200
            finally:
                await _teardown()

        _run(flow())

    def test_purges_everything_and_frees_username(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_del_ok")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "petra")
                    seeded = await _seed_full_data("petra")
                    account_id = seeded["identity"].account_id

                    resp = await c.post("/api/web/account/delete", headers=_h(token),
                                        json={"current_password": PASSWORD,
                                              "username_confirm": "PETRA"})
                    assert resp.status_code == 200, resp.text

                    db = mongo.get_database()
                    assert (await c.get("/api/web/account/profile", headers=_h(token))).status_code == 401
                    assert await AccountRepository().find_by_id(account_id) is None
                    assert await IdentityRepository().find_by_id(seeded["identity"].id) is None
                    assert await ServiceRepository().list_by_account(account_id) == []
                    assert await ConversationRepository().list_by_identity(seeded["identity"].id) == []
                    assert await QuotaRepository().find_by_account(account_id) is None
                    assert await WebCredentialRepository().find_by_username("petra") is None
                    assert await WebSessionRepository().list_by_credential(
                        seeded["credential_id"]) == []
                    assert await OrderRepository().list_recent(account_id=account_id) == []
                    assert await db.proactive_records.count_documents(
                        {"identity_id": seeded["identity"].id}) == 0
                    assert await db.safety_events.count_documents(
                        {"account_id": account_id}) == 0
                    assert await db.injection_snapshots.count_documents(
                        {"service_id": seeded["service"].context_key}) == 0
                    assert await MediaRepository().find_by_id(seeded["media_id"]) is None

                    # 用户名已释放，可重新注册
                    again = await c.post(
                        "/api/auth/register",
                        json={"username": "petra", "password": PASSWORD,
                              "terms_accepted": True, "privacy_accepted": True,
                              "age_confirmed": True},
                    )
                    assert again.status_code == 201, again.text
            finally:
                await _teardown()

        _run(flow())


class TestRateLimits:
    """写操作限流。"""

    def test_password_and_delete_are_throttled(self) -> None:
        async def flow() -> None:
            app = await _setup_app("acct_rate")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "quinn")
                    saved = (web_account_api._RATE_PASSWORD, web_account_api._RATE_DELETE)
                    web_account_api._RATE_PASSWORD = 1
                    web_account_api._RATE_DELETE = 1
                    try:
                        first = await c.post(
                            "/api/web/account/password", headers=_h(token),
                            json={"current_password": "nope1234", "new_password": NEW_PASSWORD},
                        )
                        assert first.status_code == 400
                        second = await c.post(
                            "/api/web/account/password", headers=_h(token),
                            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                        )
                        assert second.status_code == 429
                        assert "retry-after" in second.headers

                        await c.post("/api/web/account/delete", headers=_h(token),
                                     json={"current_password": "nope1234",
                                           "username_confirm": "quinn"})
                        blocked = await c.post("/api/web/account/delete", headers=_h(token),
                                               json={"current_password": PASSWORD,
                                                     "username_confirm": "quinn"})
                        assert blocked.status_code == 429
                        # 被限流时账号仍然完好
                        assert (await c.get("/api/web/account/profile",
                                            headers=_h(token))).status_code == 200
                    finally:
                        web_account_api._RATE_PASSWORD, web_account_api._RATE_DELETE = saved
            finally:
                await _teardown()

        _run(flow())
