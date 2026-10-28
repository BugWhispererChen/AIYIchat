"""C 端身份自助管理接口测试。

覆盖：
- 列表字段满足管理页展示（自我描述、未成年标记、服务引用数）；
- 本人身份的新建、编辑、删除；
- 受控字段（归属 / 平台绑定 / 禁用状态）不可通过请求体改写；
- 跨账号按不存在处理；
- 仍被服务（含已停用）引用时删除返回 409；
- 无服务引用时级联清理对话与主动消息记录。
"""

import asyncio
from typing import Any

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.models.profile import PromptProfileCreate
from app.services import auth_service
from app.utils.rate_limit import account_limiter, login_limiter, register_limiter

PUBLIC_SLUG = "lin-yu"
PASSWORD = "passw0rd9"
CLIENT_IP = "203.0.113.41"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_free_quota_template_id",
    "web_rate_limit_register", "web_rate_limit_login",
    "web_max_profiles", "web_max_services", "trust_proxy_headers",
)


@pytest.fixture(autouse=True)
def web_flags():
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
    mongo._db = mongomock_motor.AsyncMongoMockClient()[db_name]
    await ProfileRepository().create(
        PromptProfileCreate(slug=PUBLIC_SLUG, name="林语", visibility="public")
    )
    from app.main import app

    return app


def _client(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-Forwarded-For": CLIENT_IP},
    )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: AsyncClient, username: str) -> str:
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
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _run(coro: Any) -> None:
    asyncio.run(coro)


class TestWebIdentitySelfService:
    def test_list_exposes_management_fields(self) -> None:
        async def flow() -> None:
            app = await _setup_app("identity_list")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    resp = await client.get("/api/web/identities", headers=_h(token))
                    assert resp.status_code == 200, resp.text
                    items = resp.json()["items"]
                    assert len(items) == 1
                    item = items[0]
                    assert item["name"] == "alice"
                    assert item["self_description"] == ""
                    assert item["is_minor"] is False
                    assert item["bound"] is False
                    assert item["service_count"] == 0
            finally:
                mongo._db = None

        _run(flow())

    def test_create_edit_and_delete(self) -> None:
        async def flow() -> None:
            app = await _setup_app("identity_crud")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    created = await client.post(
                        "/api/web/identities",
                        json={
                            "name": "另一个我",
                            "nickname": "小另",
                            "self_description": "喜欢夜跑",
                            "is_minor": True,
                        },
                        headers=_h(token),
                    )
                    assert created.status_code == 201, created.text
                    identity = created.json()
                    assert identity["name"] == "另一个我"
                    assert identity["self_description"] == "喜欢夜跑"
                    assert identity["is_minor"] is True
                    assert identity["service_count"] == 0

                    updated = await client.put(
                        f"/api/web/identities/{identity['id']}",
                        json={
                            "name": "改名后",
                            "nickname": "阿改",
                            "self_description": "改成白天跑步",
                            "is_minor": False,
                        },
                        headers=_h(token),
                    )
                    assert updated.status_code == 200, updated.text
                    saved = updated.json()
                    assert saved["name"] == "改名后"
                    assert saved["nickname"] == "阿改"
                    assert saved["self_description"] == "改成白天跑步"
                    assert saved["is_minor"] is False

                    deleted = await client.delete(
                        f"/api/web/identities/{identity['id']}", headers=_h(token)
                    )
                    assert deleted.status_code == 200, deleted.text
                    names = {
                        item["id"]
                        for item in (await client.get(
                            "/api/web/identities", headers=_h(token)
                        )).json()["items"]
                    }
                    assert identity["id"] not in names
            finally:
                mongo._db = None

        _run(flow())

    def test_controlled_fields_cannot_be_overwritten(self) -> None:
        async def flow() -> None:
            app = await _setup_app("identity_guarded")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    identity = (await client.get(
                        "/api/web/identities", headers=_h(token)
                    )).json()["items"][0]
                    resp = await client.put(
                        f"/api/web/identities/{identity['id']}",
                        json={
                            "name": "合法改名",
                            "account_id": "attacker-account",
                            "platform": "qq",
                            "platform_id": "stolen-openid",
                            "is_blocked": True,
                        },
                        headers=_h(token),
                    )
                    assert resp.status_code == 200, resp.text
                    me = await client.get("/api/auth/me", headers=_h(token))
                    saved = await IdentityRepository().find_by_id(identity["id"])
                    assert saved is not None
                    assert saved.name == "合法改名"
                    assert saved.account_id == me.json()["account_id"]
                    assert saved.account_id != "attacker-account"
                    assert saved.platform is None
                    assert saved.platform_id == ""
                    assert saved.is_blocked is False
            finally:
                mongo._db = None

        _run(flow())

    def test_cross_account_is_hidden(self) -> None:
        async def flow() -> None:
            app = await _setup_app("identity_cross")
            try:
                async with _client(app) as client:
                    alice = await _signup(client, "alice")
                    bob = await _signup(client, "bob")
                    identity = (await client.get(
                        "/api/web/identities", headers=_h(alice)
                    )).json()["items"][0]
                    assert (await client.put(
                        f"/api/web/identities/{identity['id']}",
                        json={"name": "劫持"},
                        headers=_h(bob),
                    )).status_code == 404
                    assert (await client.delete(
                        f"/api/web/identities/{identity['id']}", headers=_h(bob)
                    )).status_code == 404
                    saved = await IdentityRepository().find_by_id(identity["id"])
                    assert saved is not None and saved.name == "alice"
            finally:
                mongo._db = None

        _run(flow())

    def test_delete_blocked_while_referenced_by_service(self) -> None:
        async def flow() -> None:
            app = await _setup_app("identity_in_use")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    identity = (await client.get(
                        "/api/web/identities", headers=_h(token)
                    )).json()["items"][0]
                    service = await client.post(
                        "/api/web/services",
                        json={"profile_id": PUBLIC_SLUG, "identity_id": identity["id"]},
                        headers=_h(token),
                    )
                    assert service.status_code == 201, service.text
                    service_id = service.json()["id"]

                    blocked = await client.delete(
                        f"/api/web/identities/{identity['id']}", headers=_h(token)
                    )
                    assert blocked.status_code == 409, blocked.text
                    assert blocked.json()["code"] == "identity_in_use"
                    assert "1" in blocked.json()["detail"]

                    # 停用不算解除引用：仍必须拒绝
                    assert (await client.put(
                        f"/api/web/services/{service_id}",
                        json={"is_active": False},
                        headers=_h(token),
                    )).status_code == 200
                    assert (await client.delete(
                        f"/api/web/identities/{identity['id']}", headers=_h(token)
                    )).status_code == 409

                    assert (await client.delete(
                        f"/api/web/services/{service_id}", headers=_h(token)
                    )).status_code == 200
                    assert (await client.delete(
                        f"/api/web/identities/{identity['id']}", headers=_h(token)
                    )).status_code == 200

                    items = (await client.get(
                        "/api/web/identities", headers=_h(token)
                    )).json()["items"]
                    assert items == []
                    # 删除身份不等于注销账号
                    assert (await client.get("/api/auth/me", headers=_h(token))).status_code == 200
            finally:
                mongo._db = None

        _run(flow())

    def test_delete_cascades_conversations_and_proactive_records(self) -> None:
        async def flow() -> None:
            app = await _setup_app("identity_cascade")
            try:
                async with _client(app) as client:
                    token = await _signup(client, "alice")
                    created = await client.post(
                        "/api/web/identities",
                        json={"name": "待删除身份"},
                        headers=_h(token),
                    )
                    identity_id = created.json()["id"]
                    await ConversationRepository().create(
                        identity_id, PUBLIC_SLUG, "svc-cascade"
                    )
                    db = mongo.get_database()
                    await db.proactive_records.insert_one(
                        {"id": "pro-identity", "identity_id": identity_id, "stage": 1}
                    )

                    resp = await client.delete(
                        f"/api/web/identities/{identity_id}", headers=_h(token)
                    )
                    assert resp.status_code == 200, resp.text
                    assert await IdentityRepository().find_by_id(identity_id) is None
                    assert await db.conversations.count_documents(
                        {"identity_id": identity_id}
                    ) == 0
                    assert await db.proactive_records.count_documents(
                        {"identity_id": identity_id}
                    ) == 0
            finally:
                mongo._db = None

        _run(flow())
