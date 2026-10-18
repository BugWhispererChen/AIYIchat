"""C 端网页用户自助管理（角色 / 服务）的接口与越权回归测试。

覆盖要点：
- 未登录拒绝；登录用户只能操作自己账号名下的私有角色与服务；
- 归属字段由服务端写入，客户端伪造 account_id/visibility/bot_id 无效；
- 配额上限、能力关闭、内容安全三条治理闸门；
- 服务删除/停用后的"至少保留一个可聊天服务"保护；
- 聊天按显式 service_id 选择伙伴，越权指定他人服务被拒。
"""

import asyncio
from typing import Any, AsyncIterator, Callable

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.profile import PromptProfileCreate
from app.services import auth_service
from app.utils.rate_limit import login_limiter, register_limiter

WEB_BOT_ID = "web-bot-selfsvc"
PUBLIC_SLUG = "lin-yu"
PASSWORD = "passw0rd9"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
    "web_free_quota_template_id", "web_session_hours", "web_session_max_concurrent",
    "web_login_max_fail", "web_login_lockout_minutes",
    "web_rate_limit_register", "web_rate_limit_login",
    "web_max_profiles", "web_max_services",
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
                        json={"profile_id": pid, "name": "我的阿禾", "account_id": "x", "bot_id": "x"},
                        headers=_h(a),
                    )
                    assert resp.status_code == 201, resp.text
                    saved = await ServiceRepository().find_by_id(resp.json()["id"])
                    assert saved is not None
                    assert saved.bot_id == WEB_BOT_ID
                    assert saved.profile_id == pid
                    assert saved.account_id == (await ProfileRepository().find_by_id(pid)).account_id
                    assert saved.identity_id == me.json()["identity_id"]
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


class _RecordingEngine:
    """替身引擎：记录聊天请求实际选中的服务。"""

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
        _RecordingEngine.calls.append({"platform_id": platform_id, "service_id": service_id})
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
                    # 他人服务：拒绝且不落到引擎
                    assert resp.status_code == 403
                    assert len(_RecordingEngine.calls) == calls_before
            finally:
                mongo._db = None

        _with_engine(flow)
