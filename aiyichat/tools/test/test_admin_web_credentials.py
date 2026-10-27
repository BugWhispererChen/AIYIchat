"""Admin 网页账号治理路由测试（mongomock + ASGI 内存客户端）。

覆盖 `/admin/web-credentials` 列表、审核（通过/拒绝）、禁用/启用、重置密码、额度调整与能力配置保存。
"""

import asyncio
from typing import Any

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.admin.auth import require_admin
from app.config import settings
from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.db.repositories.web_session_repo import WebSessionRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.profile import PromptProfileCreate
from app.models.quota import AccountQuotaUpdate, QuotaEntry
from app.services import auth_service
from app.utils.password_hashing import hash_password, verify_password

WEB_BOT_ID = "web-bot-admin"
PROFILE_SLUG = "lin-yu"
PASSWORD = "passw0rd9"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
    "web_free_quota_template_id", "web_session_hours", "web_session_max_concurrent",
    "web_login_max_fail", "web_login_lockout_minutes",
    "web_rate_limit_register", "web_rate_limit_login",
)


@pytest.fixture(autouse=True)
def web_flags():
    """用例期间打开网页开关并在结束后完整复原运行时配置。"""
    snapshot = {name: getattr(settings, name) for name in settings._fields()}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = False
    settings.web_bot_id = WEB_BOT_ID
    settings.web_default_profile_slug = PROFILE_SLUG
    settings.web_free_quota_template_id = ""
    settings.admin_username = "admin"
    # require_admin 在未配置密码时返回 503，会掩盖"未登录"分支；固定为非空以稳定断言 401/303
    settings.admin_password = hash_password("admin-test-password")
    yield
    for name, value in snapshot.items():
        setattr(settings, name, value)


async def _seed_env() -> Any:
    """准备内存库、系统账号、web 载体 Bot 与公共角色。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["t_web_credentials"]
    account = await AccountRepository().create(AccountCreate(name="系统账号"))
    await BotConfigRepository().create(
        BotConfigCreate(id=WEB_BOT_ID, account_id=account.id, name="网页助手", platform="web")
    )
    await ProfileRepository().create(
        PromptProfileCreate(slug=PROFILE_SLUG, name="林语", visibility="public")
    )
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: "admin-session"
    return app


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _cleanup(app: Any) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


async def _register_identity(username: str) -> str:
    """注册并返回身份 ID（默认待审核）。"""
    credential = await auth_service.register(
        username=username,
        password=PASSWORD,
        terms_accepted=True,
        privacy_accepted=True,
        age_confirmed=True,
        ip="203.0.113.9",
    )
    return credential.id


class TestWebCredentialsPage:
    """列表页渲染与筛选。"""

    def test_list_renders_pending(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("pendingone")
                async with _client(app) as client:
                    resp = await client.get("/admin/web-credentials")
                    assert resp.status_code == 200
                    assert "待审核（1）" in resp.text
                    assert "pendingone" in resp.text
                    assert "203.0.113.9" in resp.text
                    # 已拒绝/已禁用筛选下不出现该身份
                    resp = await client.get("/admin/web-credentials?status_filter=rejected")
                    assert "pendingone" not in resp.text
                    assert ident_id
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_search_and_all_filter(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                await _register_identity("alpha01")
                await _register_identity("beta02")
                async with _client(app) as client:
                    resp = await client.get("/admin/web-credentials?status_filter=all&q=alp")
                    assert "alpha01" in resp.text and "beta02" not in resp.text
                    resp = await client.get("/admin/web-credentials?status_filter=all")
                    assert "alpha01" in resp.text and "beta02" in resp.text
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_requires_admin(self) -> None:
        """未登录后台时不得进入治理页（401 经全局处理器转 303 跳登录）。"""

        async def flow() -> None:
            app = await _seed_env()
            app.dependency_overrides.clear()
            try:
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/web-credentials",
                        headers={"Accept": "text/html,application/xhtml+xml"},
                        follow_redirects=False,
                    )
                    assert resp.status_code in (303, 401)
                    assert resp.status_code != 200
            finally:
                await _cleanup(app)

        asyncio.run(flow())


class TestReviewActions:
    """审核与治理动作。"""

    def test_approve_via_form(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("tobeapproved")
                async with _client(app) as client:
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"action": "approve", "note": "资料完整", "status_filter": "pending"},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303
                    ident = await WebCredentialRepository().find_by_id(ident_id)
                    assert ident.status == "active"
                    assert ident.reviewed_by == "admin"
                    assert ident.review_note == "资料完整"
                    # 通过后即可登录
                    login = await client.post(
                        "/api/auth/login", json={"username": "tobeapproved", "password": PASSWORD}
                    )
                    assert login.json().get("token")
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_review_requires_action(self) -> None:
        """缺 action 字段必须 422：锁住路由契约，防止反过来给 Form(...) 加默认值掩盖前端丢字段。"""

        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("noaction")
                async with _client(app) as client:
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"note": "缺 action", "status_filter": "pending"},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 422
                    ident = await WebCredentialRepository().find_by_id(ident_id)
                    assert ident.status == "pending"  # 校验失败不得产生任何状态变更
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_reject_sets_cooldown(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("rejectme")
                async with _client(app) as client:
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"action": "reject", "note": "资料不实", "status_filter": "pending"},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303
                    ident = await WebCredentialRepository().find_by_id(ident_id)
                    assert ident.status == "rejected" and ident.reviewed_at is not None
                    # 冷却期内同名注册被拒
                    with pytest.raises(auth_service.AuthError) as err:
                        await auth_service.register(
                            username="rejectme",
                            password="another123",
                            terms_accepted=True,
                            privacy_accepted=True,
                            age_confirmed=True,
                            ip="203.0.113.9",
                        )
                    assert err.value.code == "register_cooldown"
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_disable_kicks_sessions(self) -> None:
        """后台禁用后：会话清空、令牌立即失效、用户名不可重注。"""

        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("bannedviaadmin")
                async with _client(app) as client:
                    await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"action": "approve", "status_filter": "pending"},
                    )
                    login = await client.post(
                        "/api/auth/login", json={"username": "bannedviaadmin", "password": PASSWORD}
                    )
                    token = login.json()["token"]
                    assert (
                        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                    ).status_code == 200

                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"action": "disable", "note": "违规使用", "status_filter": "active"},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303
                    assert (
                        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                    ).status_code == 401
                    assert await WebSessionRepository().list_by_credential(ident_id) == []
                    # 已禁用不属于可重注状态：仍返回 409
                    resp = await client.post(
                        "/api/auth/register",
                        json={
                            "username": "bannedviaadmin", "password": PASSWORD,
                            "terms_accepted": True, "privacy_accepted": True, "age_confirmed": True,
                        },
                    )
                    assert resp.status_code == 409
                    # 重新启用后恢复可登录
                    await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"action": "enable", "status_filter": "disabled"},
                    )
                    assert (await WebCredentialRepository().find_by_id(ident_id)).status == "active"
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_unknown_identity_and_action(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/admin/web-credentials/no-such-id/review",
                        data={"action": "approve", "status_filter": "pending"},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303 and "error=not_found" in resp.headers["location"]
                    ident_id = await _register_identity("badaction")
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/review",
                        data={"action": "explode", "status_filter": "pending"},
                        follow_redirects=False,
                    )
                    assert "error=unknown_action" in resp.headers["location"]
            finally:
                await _cleanup(app)

        asyncio.run(flow())


class TestPasswordAndQuota:
    """重置密码与额度调整。"""

    def test_reset_password(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("pwmgmt")
                async with _client(app) as client:
                    await client.post(
                        f"/admin/web-credentials/{ident_id}/review", data={"action": "approve"}
                    )
                    # 弱密码被策略拦下
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/reset-password",
                        data={"new_password": "password123"},
                        follow_redirects=False,
                    )
                    assert "error=weak_password" in resp.headers["location"]
                    # 旧会话在成功重置后失效
                    old = (
                        await client.post(
                            "/api/auth/login", json={"username": "pwmgmt", "password": PASSWORD}
                        )
                    ).json()["token"]
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/reset-password",
                        data={"new_password": "brandnew99"},
                        follow_redirects=False,
                    )
                    assert "msg=password_reset" in resp.headers["location"]
                    assert (
                        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old}"})
                    ).status_code == 401
                    ident = await WebCredentialRepository().find_by_id(ident_id)
                    assert verify_password("brandnew99", ident.password_hash)
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_adjust_quota(self) -> None:
        """仅改写传入正数的上限，已用量与其余项保持不变。"""

        async def flow() -> None:
            app = await _seed_env()
            try:
                ident_id = await _register_identity("quotamgmt")
                ident = await WebCredentialRepository().find_by_id(ident_id)
                # 预置：文字额度上限 500、已用 7
                await QuotaRepository().create_or_update(
                    ident.account_id,
                    AccountQuotaUpdate(text_quota=QuotaEntry(limit=500, used=7)),
                )
                async with _client(app) as client:
                    resp = await client.post(
                        f"/admin/web-credentials/{ident_id}/quota",
                        data={"text_limit": "1200", "voice_limit": "0", "image_limit": "0"},
                        follow_redirects=False,
                    )
                    assert "msg=quota_updated" in resp.headers["location"]
                    quota = await QuotaRepository().find_by_account(ident.account_id)
                    assert quota.text_quota.limit == 1200
                    # 已用量原样保留，未传正数的语音/图片上限不变
                    assert quota.text_quota.used == 7
                    assert quota.voice_quota.limit == auth_service.FREE_VOICE_LIMIT
            finally:
                await _cleanup(app)

        asyncio.run(flow())

    def test_quota_missing_cases(self) -> None:
        """身份不存在或未回填 user_id 时给出可读错误，不抛异常。"""

        async def flow() -> None:
            app = await _seed_env()
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/admin/web-credentials/no-id/quota",
                        data={"text_limit": "10"},
                        follow_redirects=False,
                    )
                    assert "error=not_found" in resp.headers["location"]
            finally:
                await _cleanup(app)

        asyncio.run(flow())


class TestSettingsSave:
    """能力配置保存并热重载。"""

    def test_saves_and_reloads(self) -> None:
        async def flow() -> None:
            app = await _seed_env()
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/admin/web-credentials/settings",
                        data={
                            "web_auth_enabled": "on",
                            "web_register_enabled": "on",
                            "web_register_require_review": "",
                            "web_captcha_enabled": "",
                            "web_bot_id": WEB_BOT_ID,
                            "web_default_profile_slug": PROFILE_SLUG,
                            "web_free_quota_template_id": "",
                            "web_session_hours": "12",
                            "web_session_max_concurrent": "2",
                            "web_login_max_fail": "4",
                            "web_login_lockout_minutes": "30",
                            "web_rate_limit_register": "3",
                            "web_rate_limit_login": "9",
                        },
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303 and "msg=settings_saved" in resp.headers["location"]
                    # 未勾选的开关以 False 落库并热重载生效
                    assert settings.web_register_require_review is False
                    assert settings.web_captcha_enabled is False
                    assert settings.web_session_hours == 12
                    assert settings.web_rate_limit_login == 9
                    # 免审生效后，新注册可直接登录
                    resp = await client.post(
                        "/api/auth/register",
                        json={
                            "username": "noreview1", "password": PASSWORD,
                            "terms_accepted": True, "privacy_accepted": True, "age_confirmed": True,
                        },
                    )
                    assert resp.json()["status"] == "active"
            finally:
                await _cleanup(app)

        asyncio.run(flow())
