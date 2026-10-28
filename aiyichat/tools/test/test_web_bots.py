"""C 端聊天渠道（Bot）自助管理接口测试。

覆盖：
- 鉴权、跨账号与管理后台渠道隔离；
- Web / QQ / 微信渠道独立创建、凭据脱敏；
- 渠道与服务配额拆分、零服务删除、级联删除与渠道身份解绑；
- QQ / 微信扫码流程的共享会话与任务生命周期（网络与 Manager 均用替身）。
"""

import asyncio
import base64
from typing import Any
from unittest.mock import patch

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.qq_bind import QQBindSession
from app.config import settings
from app.db import mongo
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.models.bot_config import BotConfigCreate
from app.models.profile import PromptProfileCreate
from app.models.service_memory import MemoryFact
from app.services import auth_service
from app.services.bot_qr_service import qq_sessions
from app.utils.rate_limit import account_limiter, login_limiter, register_limiter

PUBLIC_SLUG = "lin-yu"
PASSWORD = "passw0rd9"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled",
    "web_free_quota_template_id", "web_max_profiles", "web_max_services",
    "web_rate_limit_register", "web_rate_limit_login",
    "trust_proxy_headers",
)


@pytest.fixture(autouse=True)
def web_flags():
    """开启网页鉴权并留出足够的自助额度；用例结束恢复全局配置。"""
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = False
    settings.web_free_quota_template_id = ""
    settings.web_max_profiles = 10
    settings.web_max_services = 5
    settings.web_rate_limit_register = 100
    settings.web_rate_limit_login = 100
    settings.trust_proxy_headers = True
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
    """内存库 + 一个公共角色；账号级 Web 渠道由注册自动创建。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()[db_name]
    await ProfileRepository().create(
        PromptProfileCreate(slug=PUBLIC_SLUG, name="林语", visibility="public")
    )
    from app.main import app

    return app


def _client(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-For": "203.0.113.9"},
    )


async def _signup(client: AsyncClient, username: str) -> str:
    """注册 → 审核通过 → 登录，返回 Bearer 令牌。"""
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            "terms_accepted": True,
            "privacy_accepted": True,
            "age_confirmed": True,
        },
    )
    assert resp.status_code == 201, resp.text
    persona = await WebCredentialRepository().find_by_username(username)
    assert persona is not None
    assert await auth_service.approve(persona.id, "admin") is True
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _public_profile_id() -> str:
    profile = await ProfileRepository().find_by_slug(PUBLIC_SLUG)
    assert profile is not None
    return profile.id


async def _account_id(client: AsyncClient, token: str) -> str:
    me = await client.get("/api/auth/me", headers=_h(token))
    assert me.status_code == 200
    return str(me.json()["account_id"])


async def _identity_id(client: AsyncClient, token: str) -> str:
    resp = await client.get("/api/web/identities", headers=_h(token))
    assert resp.status_code == 200, resp.text
    items = [item for item in resp.json()["items"] if not item["platform"]]
    assert len(items) == 1
    return str(items[0]["id"])


def _bot_payload(platform: str, name: str, **fields: Any) -> dict[str, Any]:
    """渠道创建只提交渠道自身字段；服务由 POST /api/web/services 单独装配。"""
    return {
        "platform": platform,
        "name": name,
        **fields,
    }


def _run(coro: Any) -> None:
    asyncio.run(coro)


class TestWebBotsAccess:
    """鉴权与归属边界。"""

    def test_requires_login(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_auth")
            try:
                async with _client(app) as client:
                    assert (await client.get("/api/web/bots")).status_code == 401
                    assert (await client.post(
                        "/api/web/bots",
                        json={"platform": "web", "name": "x"},
                    )).status_code == 401
            finally:
                mongo._db = None

        _run(flow())

    def test_cross_account_is_hidden(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_cross")
            try:
                async with _client(app) as client:
                    alice = await _signup(client, "alice")
                    bob = await _signup(client, "bob")
                    bot = (await client.get("/api/web/bots", headers=_h(alice))).json()["items"][0]
                    assert (await client.put(
                        f"/api/web/bots/{bot['id']}", json={"name": "劫持"}, headers=_h(bob)
                    )).status_code == 404
                    assert (await client.delete(
                        f"/api/web/bots/{bot['id']}", headers=_h(bob)
                    )).status_code == 404
                    assert (await client.post(
                        f"/api/web/bots/{bot['id']}/qq/qrcode", headers=_h(bob)
                    )).status_code == 404
            finally:
                mongo._db = None

        _run(flow())

    def test_admin_qq_hidden_but_admin_web_manageable(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_admin")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    account_id = await _account_id(client, token)
                    admin_qq = await BotConfigRepository().create(BotConfigCreate(
                        account_id=account_id, name="后台QQ", platform="qq",
                        app_id="a", app_secret="s",
                    ))
                    admin_web = await BotConfigRepository().create(BotConfigCreate(
                        account_id=account_id, name="后台网页", platform="web",
                    ))
                    items = (await client.get("/api/web/bots", headers=_h(token))).json()["items"]
                    ids = {item["id"] for item in items}
                    assert admin_qq.id not in ids
                    assert admin_web.id in ids

                    assert (await client.put(
                        f"/api/web/bots/{admin_qq.id}", json={"name": "x"}, headers=_h(token)
                    )).status_code == 404
                    assert (await client.put(
                        f"/api/web/bots/{admin_web.id}", json={"name": "后台网页改名"}, headers=_h(token)
                    )).status_code == 200
            finally:
                mongo._db = None

        _run(flow())


class TestWebBotCreate:
    """渠道独立创建（不装配服务）与凭据脱敏。"""

    def test_create_web_bot_without_service_then_add_services(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_web")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    pid = await _public_profile_id()
                    created = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("web", "工作助手"),
                        headers=_h(token),
                    )
                    assert created.status_code == 201, created.text
                    body = created.json()
                    bot = body["bot"]
                    assert bot["platform"] == "web" and bot["origin"] == "web"
                    assert bot["service_count"] == 0
                    assert "service" not in body

                    identity_id = await _identity_id(client, token)
                    for name in ("第一个服务", "第二个服务", "第三个服务"):
                        resp = await client.post(
                            "/api/web/services",
                            json={
                                "profile_id": pid,
                                "identity_id": identity_id,
                                "name": name,
                                "bot_id": bot["id"],
                            },
                            headers=_h(token),
                        )
                        assert resp.status_code == 201, resp.text
                    items = (await client.get("/api/web/bots", headers=_h(token))).json()["items"]
                    saved = next(item for item in items if item["id"] == bot["id"])
                    assert saved["service_count"] == 3
            finally:
                mongo._db = None

        _run(flow())

    def test_qq_secret_redaction_and_one_service(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_qq")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    pid = await _public_profile_id()
                    created = await client.post(
                        "/api/web/bots",
                        json=_bot_payload(
                            "qq", "QQ助手",
                            app_id="app-1", app_secret="secret-1",
                        ),
                        headers=_h(token),
                    )
                    assert created.status_code == 201, created.text
                    assert "secret-1" not in created.text
                    bot = created.json()["bot"]
                    assert bot["has_secret"] is True
                    assert bot["app_id"] == "app-1"
                    assert bot["state"] == "ready"
                    assert bot["service_count"] == 0

                    first = await client.post(
                        "/api/web/services",
                        json={
                            "profile_id": pid,
                            "identity_id": await _identity_id(client, token),
                            "bot_id": bot["id"],
                        },
                        headers=_h(token),
                    )
                    assert first.status_code == 201, first.text

                    duplicate = await client.post(
                        "/api/web/services",
                        json={
                            "profile_id": pid,
                            "identity_id": await _identity_id(client, token),
                            "bot_id": bot["id"],
                        },
                        headers=_h(token),
                    )
                    assert duplicate.status_code == 409
                    assert duplicate.json()["code"] == "bot_has_service"

                    updated = await client.put(
                        f"/api/web/bots/{bot['id']}",
                        json={"name": "QQ助手改名", "app_secret": "secret-2"},
                        headers=_h(token),
                    )
                    assert updated.status_code == 200, updated.text
                    assert "secret-2" not in updated.text
                    saved = await BotConfigRepository().find_by_id(bot["id"])
                    assert saved is not None and saved.app_secret == "secret-2"
            finally:
                mongo._db = None

        _run(flow())

    def test_qq_without_credentials_is_pending(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_qq_pending")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    created = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("qq", "待绑定"),
                        headers=_h(token),
                    )
                    assert created.status_code == 201, created.text
                    bot = created.json()["bot"]
                    assert bot["state"] == "pending_bind"
                    assert bot["has_credentials"] is False
                    assert bot["service_count"] == 0
            finally:
                mongo._db = None

        _run(flow())

    def test_delete_bot_resets_channel_identity(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_delete")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    pid = await _public_profile_id()
                    created = await client.post(
                        "/api/web/bots",
                        json=_bot_payload(
                            "qq", "QQ助手",
                            app_id="app-1", app_secret="secret-1",
                        ),
                        headers=_h(token),
                    )
                    assert created.status_code == 201, created.text
                    bot = created.json()["bot"]
                    service = await client.post(
                        "/api/web/services",
                        json={
                            "profile_id": pid,
                            "identity_id": await _identity_id(client, token),
                            "bot_id": bot["id"],
                        },
                        headers=_h(token),
                    )
                    assert service.status_code == 201, service.text
                    service_id = service.json()["id"]
                    saved_service = await ServiceRepository().find_by_id(service_id)
                    assert saved_service is not None
                    identity_repo = IdentityRepository()
                    await identity_repo.bind_platform_id(
                        saved_service.identity_id, "openid-qq-1", "QQ用户"
                    )
                    await identity_repo.bind_bot_openid(
                        saved_service.identity_id, bot["id"], "openid-qq-1"
                    )
                    await ServiceMemoryRepository().add_facts(
                        saved_service.context_key, [MemoryFact(content="渠道记忆")]
                    )

                    deleted = await client.delete(
                        f"/api/web/bots/{bot['id']}", headers=_h(token)
                    )
                    assert deleted.status_code == 200, deleted.text
                    assert deleted.json()["deleted_services"] == 1
                    assert await ServiceRepository().find_by_id(service_id) is None
                    assert await ServiceMemoryRepository().find_by_service(
                        saved_service.context_key
                    ) is None
                    identity = await identity_repo.find_by_id(saved_service.identity_id)
                    assert identity is not None
                    assert identity.platform is None
                    assert identity.platform_id == ""
                    assert bot["id"] not in identity.bot_openids
            finally:
                mongo._db = None

        _run(flow())


class TestWebBotQuotaAndZeroService:
    """渠道与服务配额拆分、零服务删除。"""

    def test_service_full_does_not_block_bot_creation(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_quota")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    pid = await _public_profile_id()
                    identity_id = await _identity_id(client, token)
                    settings.web_max_services = 3

                    web_bot = (await client.get(
                        "/api/web/bots", headers=_h(token)
                    )).json()["items"][0]
                    # 把服务名额用满（上限 3），渠道仍只有 1 个
                    for name in ("甲", "乙", "丙"):
                        resp = await client.post(
                            "/api/web/services",
                            json={
                                "profile_id": pid,
                                "identity_id": identity_id,
                                "name": name,
                                "bot_id": web_bot["id"],
                            },
                            headers=_h(token),
                        )
                        assert resp.status_code == 201, resp.text

                    data = (await client.get("/api/web/bots", headers=_h(token))).json()
                    assert data["quota"] == {"limit": 3, "bots": 1, "services": 3}
                    assert data["can_create_service"] is False
                    assert data["can_create_bot"] is True

                    # 服务满额不误阻渠道创建
                    extra = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("web", "渠道二"),
                        headers=_h(token),
                    )
                    assert extra.status_code == 201, extra.text

                    # 渠道满额仍正常拦截
                    settings.web_max_services = 2
                    over = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("web", "渠道三"),
                        headers=_h(token),
                    )
                    assert over.status_code == 409
                    assert over.json()["code"] == "bot_quota_exceeded"

                    settings.web_max_services = 0
                    disabled = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("web", "渠道四"),
                        headers=_h(token),
                    )
                    assert disabled.status_code == 403
                    data = (await client.get("/api/web/bots", headers=_h(token))).json()
                    assert data["can_create_bot"] is False
                    assert data["can_create_service"] is False
            finally:
                mongo._db = None

        _run(flow())

    def test_zero_service_after_deleting_last_bot(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_last")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    pid = await _public_profile_id()
                    bot = (await client.get("/api/web/bots", headers=_h(token))).json()["items"][0]
                    created = await client.post(
                        "/api/web/services",
                        json={
                            "profile_id": pid,
                            "identity_id": await _identity_id(client, token),
                            "bot_id": bot["id"],
                        },
                        headers=_h(token),
                    )
                    assert created.status_code == 201, created.text
                    service_id = created.json()["id"]

                    # 先删服务：允许回到零服务
                    assert (await client.delete(
                        f"/api/web/services/{service_id}", headers=_h(token)
                    )).status_code == 200
                    assert (await client.get(
                        "/api/web/services", headers=_h(token)
                    )).json()["items"] == []

                    # 再删最后一个渠道：同样允许
                    deleted = await client.delete(
                        f"/api/web/bots/{bot['id']}", headers=_h(token)
                    )
                    assert deleted.status_code == 200, deleted.text
                    assert (await client.get(
                        "/api/web/bots", headers=_h(token)
                    )).json()["items"] == []
            finally:
                mongo._db = None

        _run(flow())


class TestWebBotQrFlows:
    """QQ / 微信扫码流程（平台与 Manager 均用替身）。"""

    def test_qq_qr_flow_and_ownership(self) -> None:
        async def flow() -> None:
            app = await _setup_app("web_bots_qq_qr")
            try:
                async with _client(app) as client:
                    alice = await _signup(client, "alice")
                    bob = await _signup(client, "bob")
                    created = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("qq", "待绑定"),
                        headers=_h(alice),
                    )
                    assert created.status_code == 201, created.text
                    bot = created.json()["bot"]

                    # 平台不匹配的扫码端点必须拒绝
                    assert (await client.post(
                        f"/api/web/bots/{bot['id']}/wechat/qrcode", headers=_h(alice)
                    )).status_code == 400

                    async def fake_create():
                        return QQBindSession(
                            task_id="task-1",
                            bind_key=base64.b64encode(b"k" * 32).decode(),
                            qr_url="https://q.qq.com/connect?task_id=task-1",
                        )

                    async def fake_poll(session):
                        assert session.task_id == "task-1"
                        return {
                            "status": "success",
                            "app_id": "app-1",
                            "app_secret": "secret-1",
                        }

                    with patch("app.services.bot_qr_service.create_bind_task", fake_create), \
                            patch("app.services.bot_qr_service.poll_bind_result", fake_poll):
                        qr = await client.post(
                            f"/api/web/bots/{bot['id']}/qq/qrcode", headers=_h(alice)
                        )
                        assert qr.status_code == 200, qr.text
                        assert qr.json()["task_id"] == "task-1"

                        assert (await client.get(
                            f"/api/web/bots/{bot['id']}/qq/poll-qr", headers=_h(bob)
                        )).status_code == 404

                        polled = await client.get(
                            f"/api/web/bots/{bot['id']}/qq/poll-qr", headers=_h(alice)
                        )
                        assert polled.status_code == 200, polled.text
                        assert polled.json()["status"] == "success"
                        assert "secret-1" not in polled.text

                    saved = await BotConfigRepository().find_by_id(bot["id"])
                    assert saved is not None and saved.app_secret == "secret-1"
                    assert bot["id"] not in qq_sessions.sessions
            finally:
                mongo._db = None

        _run(flow())

    def test_qr_poll_has_own_rate_budget(self) -> None:
        """轮询与取码分属不同限流作用域：30 次轮询不能被取码的 10 次阈值误伤。"""

        async def flow() -> None:
            app = await _setup_app("web_bots_qr_poll_budget")
            try:
                async with _client(app) as client:
                    alice = await _signup(client, "carol")
                    created = await client.post(
                        "/api/web/bots",
                        json=_bot_payload("qq", "待绑定"),
                        headers=_h(alice),
                    )
                    assert created.status_code == 201, created.text
                    bot = created.json()["bot"]

                    async def fake_create():
                        return QQBindSession(
                            task_id="task-2",
                            bind_key=base64.b64encode(b"k" * 32).decode(),
                            qr_url="https://q.qq.com/connect?task_id=task-2",
                        )

                    async def fake_poll(_session):
                        return {"status": "waiting"}

                    with patch("app.services.bot_qr_service.create_bind_task", fake_create), \
                            patch("app.services.bot_qr_service.poll_bind_result", fake_poll):
                        qr = await client.post(
                            f"/api/web/bots/{bot['id']}/qq/qrcode", headers=_h(alice)
                        )
                        assert qr.status_code == 200, qr.text
                        for _ in range(30):
                            polled = await client.get(
                                f"/api/web/bots/{bot['id']}/qq/poll-qr",
                                headers=_h(alice),
                            )
                            assert polled.status_code == 200, polled.text
                            assert polled.json()["status"] == "waiting"
            finally:
                mongo._db = None

        _run(flow())

    def test_wechat_login_and_logout(self) -> None:
        class FakeClient:
            def __init__(self, bot_id: str) -> None:
                self.bot_id = bot_id
                self.token = ""

            async def get_login_qrcode(self) -> dict:
                return {
                    "qrcode_url": "https://example.test/qr.png",
                    "qrcode": "qr-1",
                }

            async def poll_qrcode_status(
                self, qrcode: str, verify_code: str = ""
            ) -> dict:
                assert qrcode == "qr-1"
                return {
                    "status": "confirmed",
                    "bot_token": "wechat-token-1",
                }

            def set_bot_token(self, token: str) -> None:
                self.token = token

            async def complete_login(self, token: str) -> None:
                self.token = token

        class FakeManager:
            def __init__(self) -> None:
                self.clients: dict[str, FakeClient] = {}
                self._pending_clients: dict[str, FakeClient] = {}
                self.started: list[str] = []
                self.cancelled: list[str] = []

            async def create_bot(self, name, account_id="", origin="admin"):
                bot = await BotConfigRepository().create(BotConfigCreate(
                    account_id=account_id, name=name, platform="wechat",
                    origin=origin, status="pending_qr",
                ))
                client = FakeClient(bot.id)
                self.clients[bot.id] = client
                return client

            def get_client(self, config_id: str) -> FakeClient | None:
                return self.clients.get(config_id)

            def is_bot_running(self, config_id: str) -> bool:
                return False

            def start_bot_task(self, config_id: str, client: FakeClient) -> None:
                self.started.append(config_id)

            async def cancel_bot_task(self, config_id: str) -> None:
                self.cancelled.append(config_id)

            async def remove_bot(self, config_id: str) -> None:
                await BotConfigRepository().delete(config_id)
                self.clients.pop(config_id, None)

            async def _on_token_update(self, *args, **kwargs) -> None:
                return None

        async def flow() -> None:
            app = await _setup_app("web_bots_wechat")
            fake = FakeManager()
            try:
                async with _client(app) as client:
                    alice = await _signup(client, "alice")
                    with patch(
                        "app.adapters.wechat_clawbot.get_wechat_manager", return_value=fake
                    ):
                        created = await client.post(
                            "/api/web/bots",
                            json=_bot_payload("wechat", "微信助手"),
                            headers=_h(alice),
                        )
                        assert created.status_code == 201, created.text
                        bot = created.json()["bot"]
                        assert bot["state"] == "pending_qr"

                        qr = await client.post(
                            f"/api/web/bots/{bot['id']}/wechat/qrcode", headers=_h(alice)
                        )
                        assert qr.status_code == 200, qr.text
                        assert qr.json()["qrcode"] == "qr-1"

                        polled = await client.get(
                            f"/api/web/bots/{bot['id']}/wechat/poll-qr?qrcode=qr-1",
                            headers=_h(alice),
                        )
                        assert polled.status_code == 200, polled.text
                        assert polled.json()["status"] == "success"
                        assert bot["id"] in fake.started
                        saved = await BotConfigRepository().find_by_id(bot["id"])
                        assert saved is not None
                        assert saved.status == "online"
                        assert saved.bot_token == "wechat-token-1"

                        logged_out = await client.post(
                            f"/api/web/bots/{bot['id']}/wechat/logout", headers=_h(alice)
                        )
                        assert logged_out.status_code == 200, logged_out.text
                        assert bot["id"] in fake.cancelled
                        saved = await BotConfigRepository().find_by_id(bot["id"])
                        assert saved is not None
                        assert saved.status == "offline"
                        assert saved.bot_token == ""

                        deleted = await client.delete(
                            f"/api/web/bots/{bot['id']}", headers=_h(alice)
                        )
                        assert deleted.status_code == 200, deleted.text
                        assert await BotConfigRepository().find_by_id(bot["id"]) is None
            finally:
                mongo._db = None

        _run(flow())


class TestChannelGuards:
    """渠道启动守卫：空凭据与停用渠道不进入重连循环。"""

    def test_qq_runnable_requires_credentials_and_not_blocked(self) -> None:
        from app.adapters.qq_websocket import is_runnable_qq
        from app.bot_config import BotConfig

        base = {
            "id": "bot-guard",
            "name": "守卫测试",
            "platform": "qq",
            "app_id": "app-1",
            "app_secret": "secret-1",
            "sandbox_mode": True,
        }
        assert is_runnable_qq(BotConfig(**base)) is True
        assert is_runnable_qq(BotConfig(**{**base, "app_secret": ""})) is False
        assert is_runnable_qq(BotConfig(**{**base, "is_blocked": True})) is False
        assert is_runnable_qq(BotConfig(**{**base, "platform": "web"})) is False
