"""C 端网页身份自助管理（角色 / 服务）的接口与越权回归测试。

覆盖要点：
- 未登录拒绝；登录身份只能操作自己账号名下的私有角色与服务；
- 归属字段由服务端写入，客户端伪造 account_id/visibility/bot_id 无效；
- 配额上限、能力关闭、内容安全三条治理闸门；
- 服务删除/停用后的"至少保留一个可聊天服务"保护；
- 聊天按显式 service_id 选择伙伴，越权指定他人服务被拒。
"""

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator, Callable

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.identity import IdentityCreate
from app.models.profile import PromptProfileCreate
from app.models.service import ServiceCreate
from app.models.service_memory import MemoryFact
from app.services import auth_service
from app.utils.rate_limit import login_limiter, register_limiter

WEB_BOT_ID = "web-bot-selfsvc"
PUBLIC_SLUG = "lin-yu"
PASSWORD = "passw0rd9"

# 公共角色详情必须隐去的内部字段：写成字面量而非导入被测模块的私有常量，
# 这样清单被误删时测试会失败，而不是跟着一起放宽断言。
HIDDEN_PUBLIC_FIELDS = (
    "account_id",
    "voice_id",
    "voice_clone_target_model",
    "reference_image_url",
    "sticker_group_ids",
    "disable_reason",
)

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
    "web_free_quota_template_id", "web_session_hours", "web_session_max_concurrent",
    "web_login_max_fail", "web_login_lockout_minutes",
    "web_rate_limit_register", "web_rate_limit_login",
    "web_max_profiles", "web_max_services",
    "trust_proxy_headers",
)


@pytest.fixture(autouse=True)
def web_flags():
    """放开网页鉴权与注册、关验证码与限流，并给自助能力留出小额度。"""
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = False
    settings.web_rate_limit_register = 100
    settings.web_rate_limit_login = 100
    settings.trust_proxy_headers = True
    settings.web_bot_id = WEB_BOT_ID
    settings.web_default_profile_slug = PUBLIC_SLUG
    settings.web_free_quota_template_id = ""
    settings.web_max_profiles = 2
    settings.web_max_services = 3
    register_limiter.reset()
    login_limiter.reset()
    yield
    for key, value in saved.items():
        setattr(settings, key, value)
    register_limiter.reset()
    login_limiter.reset()


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


def _client(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-Forwarded-For": "203.0.113.9"},
    )


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
    persona = await WebCredentialRepository().find_by_username(username)
    assert persona is not None
    assert await auth_service.approve(persona.id, "admin") is True
    resp = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _profile_body(name: str = "阿禾") -> dict[str, Any]:
    return {"name": name, "category": "original", "persona": {"background": "山间书院的学徒"}}


def _run(coro: Any) -> None:
    asyncio.run(coro)


class TestWebProfileSelfService:
    """自助角色管理。"""

    def test_requires_login(self) -> None:
        async def flow() -> None:
            app = await _setup_app("selfsvc_auth")
            try:
                async with _client(app) as c:
                    assert (await c.get("/api/web/profiles")).status_code == 401
                    assert (await c.post("/api/web/profiles", json=_profile_body())).status_code == 401
            finally:
                mongo._db = None

        _run(flow())

    def test_create_forces_private_ownership(self) -> None:
        """客户端塞入的归属字段必须被服务端覆盖。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_create")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "alice")
                    body = dict(_profile_body(), visibility="public", account_id="系统账号ID")
                    resp = await c.post("/api/web/profiles", json=body, headers=_h(token))
                    assert resp.status_code == 201, resp.text
                    pid = resp.json()["id"]
                    saved = await ProfileRepository().find_by_id(pid)
                    assert saved is not None
                    assert saved.visibility == "private"
                    assert saved.account_id not in ("", "系统账号ID")
                    assert saved.slug.startswith("w-alice")
                    assert saved.is_draft is False
            finally:
                mongo._db = None

        _run(flow())

    def test_list_scopes_visibility(self) -> None:
        """列表只含公共角色与本人私有角色，不含他人私有角色。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_list")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    b = await _signup(c, "bob")
                    mine = await c.post("/api/web/profiles", json=_profile_body("阿禾"), headers=_h(a))
                    assert mine.status_code == 201
                    names_b = {
                        i["name"] for i in (await c.get("/api/web/profiles", headers=_h(b))).json()["items"]
                    }
                    assert "阿禾" not in names_b
                    assert "林语" in names_b          # 公共角色可用
                    items_a = (await c.get("/api/web/profiles", headers=_h(a))).json()["items"]
                    assert {i["name"] for i in items_a} == {"林语", "阿禾"}
                    assert [i for i in items_a if i["name"] == "阿禾"][0]["owned"] is True
            finally:
                mongo._db = None

        _run(flow())

    def test_cross_account_write_is_hidden(self) -> None:
        """他人私有角色按 404 处理：不泄露存在性，也拦下写操作。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_cross")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    b = await _signup(c, "bob")
                    pid = (await c.post("/api/web/profiles", json=_profile_body(), headers=_h(a))).json()["id"]
                    assert (await c.get(f"/api/web/profiles/{pid}", headers=_h(b))).status_code == 404
                    assert (await c.put(f"/api/web/profiles/{pid}", json=_profile_body("改名"), headers=_h(b))).status_code == 404
                    assert (await c.post(f"/api/web/profiles/{pid}/toggle", headers=_h(b))).status_code == 404
                    assert (await c.delete(f"/api/web/profiles/{pid}", headers=_h(b))).status_code == 404
                    # 公共角色同样不接受私有化改写
                    pub = await ProfileRepository().find_by_slug(PUBLIC_SLUG)
                    assert pub is not None
                    assert (await c.put(f"/api/web/profiles/{pub.id}", json=_profile_body(), headers=_h(b))).status_code == 404
            finally:
                mongo._db = None

        _run(flow())

    def test_owner_can_read_private_profile_detail(self) -> None:
        """本人私有角色详情可读：响应必须能被 JSON 解析（时间字段须已序列化）。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_detail")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    pid = (await c.post("/api/web/profiles", json=_profile_body("阿禾"), headers=_h(a))).json()["id"]
                    resp = await c.get(f"/api/web/profiles/{pid}", headers=_h(a))
                    assert resp.status_code == 200, resp.text
                    data = resp.json()
                    assert data["id"] == pid
                    assert data["name"] == "阿禾"
                    assert data["visibility"] == "private"
                    assert data["persona"]["background"] == "山间书院的学徒"
                    # created_at / updated_at 是 datetime，必须以 ISO 字符串出网
                    for key in ("created_at", "updated_at"):
                        assert isinstance(data[key], str), (key, data[key])
                        datetime.fromisoformat(data[key])
            finally:
                mongo._db = None

        _run(flow())

    def test_public_profile_detail_masks_internal_fields(self) -> None:
        """公共角色详情可读且脱敏：只读字段可见，运营与归属字段不外泄。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_detail_pub")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    resp = await c.get(f"/api/web/profiles/{PUBLIC_SLUG}", headers=_h(a))
                    assert resp.status_code == 200, resp.text
                    data = resp.json()
                    assert data["name"] == "林语"
                    assert data["visibility"] == "public"
                    # 编辑表单回填所需的分组必须齐全
                    for key in ("persona", "language_style", "personality", "dialogue_strategy"):
                        assert key in data
                    for key in HIDDEN_PUBLIC_FIELDS:
                        assert key not in data, key
            finally:
                mongo._db = None

        _run(flow())

    def test_quota_and_disable_switch(self) -> None:
        """达到上限 409；配额设为 0 时能力整体关闭 403。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_quota")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    assert (await c.post("/api/web/profiles", json=_profile_body("甲"), headers=_h(a))).status_code == 201
                    assert (await c.post("/api/web/profiles", json=_profile_body("乙"), headers=_h(a))).status_code == 201
                    over = await c.post("/api/web/profiles", json=_profile_body("丙"), headers=_h(a))
                    assert over.status_code == 409
                    # Bob 有独立额度，不受 Alice 占满影响
                    b = await _signup(c, "bob")
                    assert (await c.post("/api/web/profiles", json=_profile_body("乙二"), headers=_h(b))).status_code == 201

                    settings.web_max_profiles = 0
                    blocked = await c.post("/api/web/profiles", json=_profile_body("丁"), headers=_h(b))
                    assert blocked.status_code == 403
            finally:
                mongo._db = None

        _run(flow())

    def test_safety_block_and_length(self) -> None:
        """命中内容安全词或超长字段直接拒绝，不落库。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_safety")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    before = await ProfileRepository().count()
                    bad = await c.post(
                        "/api/web/profiles",
                        json=_profile_body("引流到赌博网站"),
                        headers=_h(a),
                    )
                    assert bad.status_code == 400
                    assert bad.json()["code"] == "safety_blocked"
                    long_name = await c.post(
                        "/api/web/profiles", json=_profile_body("名" * 40), headers=_h(a)
                    )
                    assert long_name.status_code == 400
                    assert await ProfileRepository().count() == before
            finally:
                mongo._db = None

        _run(flow())

    def test_delete_blocked_while_in_use(self) -> None:
        """被自建服务引用的角色不能删除；服务改绑后方可删除。"""

        async def flow() -> None:
            app = await _setup_app("selfsvc_del")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    pid = (await c.post("/api/web/profiles", json=_profile_body(), headers=_h(a))).json()["id"]
                    svc = await c.post(
                        "/api/web/services", json={"profile_id": pid}, headers=_h(a)
                    )
                    assert svc.status_code == 201, svc.text
                    assert (await c.delete(f"/api/web/profiles/{pid}", headers=_h(a))).status_code == 409
                    # 改绑到公共角色后解除引用
                    pub = await ProfileRepository().find_by_slug(PUBLIC_SLUG)
                    assert pub is not None
                    upd = await c.put(
                        f"/api/web/services/{svc.json()['id']}",
                        json={"profile_id": pub.id},
                        headers=_h(a),
                    )
                    assert upd.status_code == 200, upd.text
                    assert (await c.delete(f"/api/web/profiles/{pid}", headers=_h(a))).status_code == 200
                    assert await ProfileRepository().find_by_id(pid) is None
            finally:
                mongo._db = None

        _run(flow())


class TestWebServiceSelfService:
    """自助服务（伙伴）管理。"""

    def test_create_binds_system_bot_and_forces_owner(self) -> None:
        async def flow() -> None:
            app = await _setup_app("svc_create")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    me = await c.get("/api/auth/me", headers=_h(a))
                    assert me.status_code == 200
                    pid = (await c.post("/api/web/profiles", json=_profile_body(), headers=_h(a))).json()["id"]
                    resp = await c.post(
                        "/api/web/services",
                        json={"profile_id": pid, "name": "我的阿禾", "account_id": "x"},
                        headers=_h(a),
                    )
                    assert resp.status_code == 201, resp.text
                    saved = await ServiceRepository().find_by_id(resp.json()["id"])
                    assert saved is not None
                    assert saved.bot_id == WEB_BOT_ID
                    assert saved.profile_id == pid
                    assert saved.account_id == (await ProfileRepository().find_by_id(pid)).account_id
                    assert saved.identity_id == me.json()["identity_id"]

                    # 伪造 bot_id 不再被静默忽略：不属于本账号的渠道一律 404
                    forged = await c.post(
                        "/api/web/services",
                        json={"profile_id": pid, "bot_id": "x"},
                        headers=_h(a),
                    )
                    assert forged.status_code == 404, forged.text
            finally:
                mongo._db = None

        _run(flow())

    def test_cannot_bind_other_identities_private_profile(self) -> None:
        async def flow() -> None:
            app = await _setup_app("svc_priv")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    b = await _signup(c, "bob")
                    pid = (await c.post("/api/web/profiles", json=_profile_body("阿禾"), headers=_h(a))).json()["id"]
                    resp = await c.post("/api/web/services", json={"profile_id": pid}, headers=_h(b))
                    assert resp.status_code == 404
            finally:
                mongo._db = None

        _run(flow())

    def test_inactive_profile_rejected(self) -> None:
        async def flow() -> None:
            app = await _setup_app("svc_inactive")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    pid = (await c.post("/api/web/profiles", json=_profile_body(), headers=_h(a))).json()["id"]
                    assert (await c.post(f"/api/web/profiles/{pid}/toggle", headers=_h(a))).status_code == 200
                    resp = await c.post("/api/web/services", json={"profile_id": pid}, headers=_h(a))
                    assert resp.status_code == 409
            finally:
                mongo._db = None

        _run(flow())

    def test_service_quota_and_cross_account(self) -> None:
        async def flow() -> None:
            app = await _setup_app("svc_quota")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    b = await _signup(c, "bob")
                    pid = (await c.post("/api/web/profiles", json=_profile_body(), headers=_h(a))).json()["id"]
                    # 注册已自带 1 个服务，再建 2 个到上限 3
                    ids = []
                    for name in ("甲", "乙"):
                        r = await c.post("/api/web/services", json={"profile_id": pid, "name": name}, headers=_h(a))
                        assert r.status_code == 201, r.text
                        ids.append(r.json()["id"])
                    over = await c.post("/api/web/services", json={"profile_id": pid}, headers=_h(a))
                    assert over.status_code == 409

                    # Bob 无法读写 Alice 的服务
                    assert (await c.put(f"/api/web/services/{ids[0]}", json={"name": "劫持"}, headers=_h(b))).status_code == 404
                    assert (await c.delete(f"/api/web/services/{ids[0]}", headers=_h(b))).status_code == 404
            finally:
                mongo._db = None

        _run(flow())

    def test_last_active_service_protected(self) -> None:
        """最后一个启用中的服务不能停用、最后一个服务不能删除：否则账号失去聊天能力。"""

        async def flow() -> None:
            app = await _setup_app("svc_last")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    items = (await c.get("/api/web/services", headers=_h(a))).json()["items"]
                    only = items[0]["id"]
                    assert (await c.put(f"/api/web/services/{only}", json={"is_active": False}, headers=_h(a))).status_code == 409
                    assert (await c.delete(f"/api/web/services/{only}", headers=_h(a))).status_code == 409
            finally:
                mongo._db = None

        _run(flow())

    def test_login_identity_keeps_last_active_service(self) -> None:
        """账号内另有渠道服务时，也不能让当前登录身份失去最后一个活跃服务。"""

        async def flow() -> None:
            app = await _setup_app("svc_login_last")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    me = (await c.get("/api/auth/me", headers=_h(a))).json()
                    identity = await IdentityRepository().find_by_id(me["identity_id"])
                    assert identity is not None
                    own = await ServiceRepository().list_by_identity(identity.id)
                    assert own
                    target = own[0]
                    qq_identity = await IdentityRepository().create(
                        IdentityCreate(
                            account_id=identity.account_id,
                            platform="qq",
                            platform_id="openid-qq",
                        )
                    )
                    await ServiceRepository().create(
                        ServiceCreate(
                            account_id=identity.account_id,
                            identity_id=qq_identity.id,
                            bot_id="qq-bot",
                            profile_id=target.profile_id,
                        )
                    )
                    # 账号还有活跃 QQ 服务，但登录身份只剩这一个网页服务 → 两种操作都必须拒绝
                    resp = await c.put(
                        f"/api/web/services/{target.id}",
                        json={"is_active": False},
                        headers=_h(a),
                    )
                    assert resp.status_code == 409, resp.text
                    assert resp.json()["code"] == "last_active_service"
                    resp = await c.delete(
                        f"/api/web/services/{target.id}", headers=_h(a)
                    )
                    assert resp.status_code == 409, resp.text
            finally:
                mongo._db = None

        _run(flow())

    def test_delete_service_purges_memory_and_snapshots(self) -> None:
        """删除服务清记忆/快照，但不删除对话历史。"""

        async def flow() -> None:
            app = await _setup_app("svc_cleanup")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    me = (await c.get("/api/auth/me", headers=_h(a))).json()
                    identity = await IdentityRepository().find_by_id(me["identity_id"])
                    assert identity is not None
                    services = await ServiceRepository().list_by_identity(identity.id)
                    target = services[0]
                    # 再建一个活跃服务，保证删除 target 不触发最后服务保护
                    await ServiceRepository().create(
                        ServiceCreate(
                            account_id=identity.account_id,
                            identity_id=identity.id,
                            bot_id=target.bot_id,
                            profile_id=target.profile_id,
                        )
                    )
                    await ServiceMemoryRepository().add_facts(
                        target.id, [MemoryFact(content="对方喜欢喝咖啡")]
                    )
                    await mongo.get_database().injection_snapshots.insert_one({
                        "service_id": target.id,
                        "conversation_id": "conv-1",
                        "injected_facts": [],
                    })

                    resp = await c.delete(
                        f"/api/web/services/{target.id}", headers=_h(a)
                    )
                    assert resp.status_code == 200, resp.text
                    assert await ServiceMemoryRepository().find_by_service(
                        target.id
                    ) is None
                    assert await mongo.get_database().injection_snapshots.count_documents(
                        {"service_id": target.id}
                    ) == 0
            finally:
                mongo._db = None

        _run(flow())

    def test_delete_shared_group_keeps_peer_memory_until_last(self) -> None:
        """共享组还有其他成员时保留记忆，全部删除后才清理。"""

        async def flow() -> None:
            app = await _setup_app("svc_cleanup_group")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    me = (await c.get("/api/auth/me", headers=_h(a))).json()
                    identity = await IdentityRepository().find_by_id(me["identity_id"])
                    assert identity is not None
                    services = await ServiceRepository().list_by_identity(identity.id)
                    keeper = services[0]
                    group = "grp-cleanup"
                    members = []
                    for name in ("甲", "乙"):
                        members.append(await ServiceRepository().create(
                            ServiceCreate(
                                name=name,
                                account_id=identity.account_id,
                                identity_id=identity.id,
                                bot_id=keeper.bot_id,
                                profile_id=keeper.profile_id,
                                share_group_id=group,
                            )
                        ))
                    await ServiceMemoryRepository().add_facts(
                        group, [MemoryFact(content="共享记忆")]
                    )

                    assert (await c.delete(
                        f"/api/web/services/{members[0].id}", headers=_h(a)
                    )).status_code == 200
                    assert await ServiceMemoryRepository().find_by_service(group) is not None
                    assert (await c.delete(
                        f"/api/web/services/{members[1].id}", headers=_h(a)
                    )).status_code == 200
                    assert await ServiceMemoryRepository().find_by_service(group) is None
            finally:
                mongo._db = None

        _run(flow())


class _RecordingEngine:
    """替身引擎：记录聊天请求实际选中的服务与身份。"""

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
        identity_id: str = "",
    ) -> AsyncIterator[str]:
        _RecordingEngine.calls.append(
            {
                "platform_id": platform_id,
                "service_id": service_id,
                "identity_id": identity_id,
                "bot_id": self.bot_id,
            }
        )
        yield "ok"


def _with_engine(run: Callable[[], Any]) -> None:
    import app.conversation.engine as engine_module

    original = engine_module.ConversationEngine
    _RecordingEngine.calls = []
    engine_module.ConversationEngine = _RecordingEngine  # type: ignore[misc]
    try:
        asyncio.run(run())
    finally:
        engine_module.ConversationEngine = original


class TestChatHonoursServiceSelection:
    """聊天按显式 service_id 选择伙伴。"""

    def test_own_service_passes_through_and_others_rejected(self) -> None:
        async def flow() -> None:
            app = await _setup_app("svc_chat")
            try:
                async with _client(app) as c:
                    a = await _signup(c, "alice")
                    b = await _signup(c, "bob")
                    pid = (await c.post("/api/web/profiles", json=_profile_body(), headers=_h(a))).json()["id"]
                    mine = await c.post("/api/web/services", json={"profile_id": pid}, headers=_h(a))
                    assert mine.status_code == 201
                    sid = mine.json()["id"]

                    resp = await c.post(
                        "/api/conversations/chat/stream",
                        json={"text": "在吗", "service_id": sid},
                        headers=_h(a),
                    )
                    assert resp.status_code == 200
                    assert _RecordingEngine.calls[-1]["service_id"] == sid

                    theirs = await c.get("/api/web/services", headers=_h(b))
                    other_id = theirs.json()["items"][0]["id"]
                    calls_before = len(_RecordingEngine.calls)
                    resp = await c.post(
                        "/api/conversations/chat/stream",
                        json={"text": "在吗", "service_id": other_id},
                        headers=_h(a),
                    )
                    # 他人账号的服务：按"不存在"处理（404），且不落到引擎
                    assert resp.status_code == 404
                    assert len(_RecordingEngine.calls) == calls_before
            finally:
                mongo._db = None

        _with_engine(flow)


class TestCrossChannelAccess:
    """同账号渠道服务：可见、可读史、可续聊、可编辑；跨账号一律拒绝。"""

    async def _seed_channel_service(
        self, client: AsyncClient, token: str
    ) -> tuple[str, str, str]:
        """在当前账号下建 QQ Bot + QQ 身份 + 服务，返回 (service_id, bot_id, identity_id)。"""
        me = (await client.get("/api/auth/me", headers=_h(token))).json()
        web_identity = await IdentityRepository().find_by_id(me["identity_id"])
        assert web_identity is not None
        qq_bot = await BotConfigRepository().create(
            BotConfigCreate(account_id=web_identity.account_id, name="QQ助手", platform="qq")
        )
        qq_identity = await IdentityRepository().create(
            IdentityCreate(
                account_id=web_identity.account_id,
                name="qq用户",
                platform_id="openid-cross-1",
                platform="qq",
            )
        )
        svc = await ServiceRepository().create(
            ServiceCreate(
                name="qq渠道服务",
                account_id=web_identity.account_id,
                identity_id=qq_identity.id,
                bot_id=qq_bot.id,
                profile_id=PUBLIC_SLUG,
            )
        )
        return svc.id, qq_bot.id, qq_identity.id

    def test_chat_uses_service_bot_and_identity(self) -> None:
        async def flow() -> None:
            app = await _setup_app("cross_chat")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "alice")
                    sid, bot_id, identity_id = await self._seed_channel_service(c, token)
                    items = (await c.get("/api/web/services", headers=_h(token))).json()["items"]
                    row = next(i for i in items if i["id"] == sid)
                    assert row["bot_platform"] == "qq"
                    assert row["bot_name"] == "QQ助手"

                    resp = await c.post(
                        "/api/conversations/chat/stream",
                        json={"text": "从网页续聊", "service_id": sid},
                        headers=_h(token),
                    )
                    assert resp.status_code == 200
                    call = _RecordingEngine.calls[-1]
                    assert call["service_id"] == sid
                    assert call["identity_id"] == identity_id
                    assert call["bot_id"] == bot_id
            finally:
                mongo._db = None

        _with_engine(flow)

    def test_history_readable_in_account_only(self) -> None:
        async def flow() -> None:
            app = await _setup_app("cross_history")
            try:
                async with _client(app) as c:
                    alice = await _signup(c, "alice")
                    bob = await _signup(c, "bob")
                    sid, _, identity_id = await self._seed_channel_service(c, alice)
                    conv = await ConversationRepository().create(identity_id, PUBLIC_SLUG, sid)
                    await MessageRepository().create(
                        conversation_id=conv.id, role="user", content="QQ侧的旧消息"
                    )

                    listing = await c.get(
                        f"/api/conversations?service_id={sid}", headers=_h(alice)
                    )
                    assert listing.status_code == 200
                    assert [x["id"] for x in listing.json()] == [conv.id]

                    msgs = await c.get(
                        f"/api/conversations/{conv.id}/messages", headers=_h(alice)
                    )
                    assert msgs.status_code == 200
                    assert msgs.json()[0]["content"] == "QQ侧的旧消息"

                    # 跨账号：服务不可见给 404，会话读取给 403
                    assert (
                        await c.get(f"/api/conversations?service_id={sid}", headers=_h(bob))
                    ).status_code == 404
                    assert (
                        await c.get(
                            f"/api/conversations/{conv.id}/messages", headers=_h(bob)
                        )
                    ).status_code == 403
            finally:
                mongo._db = None

        _run(flow())

    def test_channel_service_editable_in_account(self) -> None:
        async def flow() -> None:
            app = await _setup_app("cross_edit")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "alice")
                    sid, _, _ = await self._seed_channel_service(c, token)

                    resp = await c.put(
                        f"/api/web/services/{sid}",
                        json={"name": "改名后的伙伴"},
                        headers=_h(token),
                    )
                    assert resp.status_code == 200, resp.text
                    saved = await ServiceRepository().find_by_id(sid)
                    assert saved is not None and saved.name == "改名后的伙伴"

                    resp = await c.put(
                        f"/api/web/services/{sid}", json={"is_active": False}, headers=_h(token)
                    )
                    assert resp.status_code == 200

                    resp = await c.delete(f"/api/web/services/{sid}", headers=_h(token))
                    assert resp.status_code == 200
                    assert await ServiceRepository().find_by_id(sid) is None
            finally:
                mongo._db = None

        _run(flow())

    def test_quota_counts_own_services_only(self) -> None:
        async def flow() -> None:
            app = await _setup_app("cross_quota")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "alice")
                    await self._seed_channel_service(c, token)
                    # 注册自带 1 个，再加 2 个到本人上限 3；渠道服务不占名额
                    for name in ("甲", "乙"):
                        resp = await c.post(
                            "/api/web/services",
                            json={"profile_id": PUBLIC_SLUG, "name": name},
                            headers=_h(token),
                        )
                        assert resp.status_code == 201, resp.text
                    over = await c.post(
                        "/api/web/services",
                        json={"profile_id": PUBLIC_SLUG},
                        headers=_h(token),
                    )
                    assert over.status_code == 409
                    quota = (await c.get("/api/web/services", headers=_h(token))).json()["quota"]
                    assert quota == {"limit": 3, "used": 3}
            finally:
                mongo._db = None

        _run(flow())

    def test_new_partner_prefers_account_web_bot(self) -> None:
        """账号已有自己的 web Bot 时，新建伙伴挂到它上面，而不是全局系统 Bot。"""

        async def flow() -> None:
            app = await _setup_app("cross_ownbot")
            try:
                async with _client(app) as c:
                    token = await _signup(c, "alice")
                    me = (await c.get("/api/auth/me", headers=_h(token))).json()
                    identity = await IdentityRepository().find_by_id(me["identity_id"])
                    assert identity is not None
                    own_bot = await BotConfigRepository().create(
                        BotConfigCreate(
                            account_id=identity.account_id, name="账号网页Bot", platform="web"
                        )
                    )

                    resp = await c.post(
                        "/api/web/services",
                        json={"profile_id": PUBLIC_SLUG, "name": "新伙伴"},
                        headers=_h(token),
                    )
                    assert resp.status_code == 201, resp.text
                    saved = await ServiceRepository().find_by_id(resp.json()["id"])
                    assert saved is not None and saved.bot_id == own_bot.id

                    # 默认服务随之切到账号自有 Bot
                    me_after = (await c.get("/api/auth/me", headers=_h(token))).json()
                    assert me_after["bot_id"] == own_bot.id
            finally:
                mongo._db = None

        _run(flow())
