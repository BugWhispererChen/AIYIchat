"""Admin 其余路由与公共 API 测试（mongomock + ASGI 内存客户端）。

覆盖：
- admin/router.py：登录/登出、仪表盘、AI模型配置页、热重载、系统监控与辅助函数
- admin/routes/conversations.py：对话列表/详情/删除
- admin/routes/billing.py：套餐模板管理与开通台账
- admin/routes/profiles.py：角色列表/详情/启停/编辑/定妆照
- app/api：health / profiles / conversations / media / proactive
"""

import asyncio
import json
import os
import re
from unittest.mock import AsyncMock, patch

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.models.profile import PromptProfileCreate
from app.models.service import ServiceCreate
from app.models.identity import IdentityCreate
from app.utils.rate_limit import admin_login_limiter


async def _setup_app(override_auth: bool = True):
    """内存数据库 + （可选）免登录的 ASGI 应用。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_admin_misc"]

    from app.admin.auth import require_admin
    from app.main import app

    if override_auth:
        app.dependency_overrides[require_admin] = lambda: "admin"
    return app


@pytest.fixture(autouse=True)
def _reset_admin_login_guards():
    """后台登录限流/锁定是进程内状态，用例之间必须相互隔离。"""
    from app.admin.auth import reset_admin_guards

    admin_login_limiter.reset()
    reset_admin_guards()
    yield
    admin_login_limiter.reset()
    reset_admin_guards()


async def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _run(coro) -> None:
    asyncio.run(coro)


class TestAuthFlow:
    """登录/登出流程与后台鉴权失败分支。"""

    def test_login_logout(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "test-pw"
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/login")
                    assert resp.status_code == 200

                    # 密码错误
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "wrong"},
                    )
                    assert resp.status_code == 200 and "用户名或密码错误" in resp.text

                    # 登录成功 → 303 + 会话 Cookie
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "test-pw"},
                    )
                    assert resp.status_code == 303
                    token = resp.cookies.get("admin_session")
                    assert token

                    resp = await client.get(
                        "/admin/logout", cookies={"admin_session": token}
                    )
                    assert resp.status_code == 303

                    # 未认证：浏览器导航重定向
                    resp = await client.get(
                        "/admin/accounts", headers={"accept": "text/html"}
                    )
                    assert resp.status_code == 303
                    # fetch 请求返回 JSON 401
                    resp = await client.get("/admin/accounts")
                    assert resp.status_code == 401
                    # HTMX 请求返回提示片段
                    resp = await client.get(
                        "/admin/accounts", headers={"HX-Request": "true"}
                    )
                    assert resp.status_code == 200 and "会话已过期" in resp.text
            finally:
                settings.admin_password = old_pw
                await _teardown_app(app)

        _run(flow())


class TestAdminLoginGuards:
    """后台登录防爆破：用户名锁定与 IP 限流。"""

    def test_username_lockout_and_reset(self) -> None:
        async def flow() -> None:
            from app.admin.auth import reset_admin_guards

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            old_max_fail = settings.admin_login_max_fail
            old_rate = settings.admin_login_rate_limit
            settings.admin_password = "test-pw"
            settings.admin_login_max_fail = 3
            settings.admin_login_rate_limit = 100
            admin_login_limiter.reset()
            reset_admin_guards()
            try:
                async with _client(app) as client:
                    for _ in range(2):
                        resp = await client.post(
                            "/admin/login",
                            data={"username": "admin", "password": "wrong"},
                        )
                        assert resp.status_code == 200
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "wrong"},
                    )
                    assert resp.status_code == 429 and "次数过多" in resp.text
                    # 锁定期内即使密码正确也拒绝
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "test-pw"},
                    )
                    assert resp.status_code == 429
                    reset_admin_guards()
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "test-pw"},
                    )
                    assert resp.status_code == 303
            finally:
                settings.admin_password = old_pw
                settings.admin_login_max_fail = old_max_fail
                settings.admin_login_rate_limit = old_rate
                admin_login_limiter.reset()
                reset_admin_guards()
                await _teardown_app(app)

        _run(flow())

    def test_ip_rate_limit(self) -> None:
        async def flow() -> None:
            from app.admin.auth import reset_admin_guards

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            old_max_fail = settings.admin_login_max_fail
            old_rate = settings.admin_login_rate_limit
            settings.admin_password = "test-pw"
            settings.admin_login_max_fail = 100
            settings.admin_login_rate_limit = 2
            admin_login_limiter.reset()
            reset_admin_guards()
            try:
                async with _client(app) as client:
                    for _ in range(2):
                        resp = await client.post(
                            "/admin/login",
                            data={"username": "admin", "password": "wrong"},
                        )
                        assert resp.status_code == 200
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "test-pw"},
                    )
                    assert resp.status_code == 429
            finally:
                settings.admin_password = old_pw
                settings.admin_login_max_fail = old_max_fail
                settings.admin_login_rate_limit = old_rate
                admin_login_limiter.reset()
                reset_admin_guards()
                await _teardown_app(app)

        _run(flow())


class TestProactiveApiAuth:
    """内部运维接口必须要求后台会话。"""

    def test_requires_admin_session(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "test-pw"
            try:
                async with _client(app) as client:
                    resp = await client.get(
                        "/api/proactive/records",
                        params={"identity_id": "x", "bot_id": "y"},
                    )
                    assert resp.status_code == 401

                    login = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "test-pw"},
                    )
                    assert login.status_code == 303
                    token = login.cookies.get("admin_session")
                    assert token
                    resp = await client.get(
                        "/api/proactive/records",
                        params={"identity_id": "x", "bot_id": "y"},
                        cookies={"admin_session": token},
                    )
                    assert resp.status_code == 200
                    assert resp.json() == []
            finally:
                settings.admin_password = old_pw
                await _teardown_app(app)

        _run(flow())


class TestPasswordChange:
    """修改密码路由。"""

    def test_change_password_flow(self) -> None:
        async def flow() -> None:
            from app.admin import auth

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "old-password-1"
            try:
                async with _client(app) as client:
                    # 登录拿到会话
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "old-password-1"},
                    )
                    assert resp.status_code == 303
                    token = resp.cookies.get("admin_session")
                    assert token
                    cookies = {"admin_session": token}

                    # GET 页面
                    resp = await client.get("/admin/password", cookies=cookies)
                    assert resp.status_code == 200 and "修改密码" in resp.text

                    # 当前密码错误
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "bad",
                        "new_password": "new-password-1",
                        "confirm_password": "new-password-1",
                    })
                    assert resp.status_code == 200 and "当前密码不正确" in resp.text

                    # 新密码太短
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "short",
                        "confirm_password": "short",
                    })
                    assert resp.status_code == 200 and "至少 8 位" in resp.text

                    # 两次不一致
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "new-password-1",
                        "confirm_password": "new-password-2",
                    })
                    assert resp.status_code == 200 and "不一致" in resp.text

                    # 与旧密码相同
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "old-password-1",
                        "confirm_password": "old-password-1",
                    })
                    assert resp.status_code == 200 and "不能与当前密码相同" in resp.text

                    # 成功：303 跳登录页 + 会话失效 + settings 变为哈希
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "new-password-1",
                        "confirm_password": "new-password-1",
                    })
                    assert resp.status_code == 303
                    assert "msg=password_changed" in resp.headers.get("location", "")
                    assert token not in auth._sessions
                    assert settings.admin_password.startswith("pbkdf2$")

                    # 旧会话已失效
                    resp = await client.get("/admin/password", cookies=cookies)
                    assert resp.status_code in (303, 401)

                    # 旧密码拒绝、新密码可登录
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "old-password-1"},
                    )
                    assert resp.status_code == 200 and "用户名或密码错误" in resp.text
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "new-password-1"},
                    )
                    assert resp.status_code == 303

                    # 登录页带成功提示
                    resp = await client.get(
                        "/admin/login", params={"msg": "password_changed"}
                    )
                    assert resp.status_code == 200 and "密码已修改" in resp.text
            finally:
                settings.admin_password = old_pw
                auth._sessions.clear()
                await _teardown_app(app)

        _run(flow())


class TestInsecureDefaultPasswordBlocked:
    """默认口令 fail-closed：模板值随源码公开，放行等于把后台白送。"""

    def test_login_is_blocked_on_template_password(self) -> None:
        async def flow() -> None:
            from app.admin import auth

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            try:
                for template_value in ("", "admin123", "your-admin-password"):
                    settings.admin_password = template_value
                    assert await auth.verify_credentials("admin", template_value) is False
                    async with _client(app) as client:
                        # 登录页给出配置提示，而不是一个"看起来能试"的表单
                        resp = await client.get("/admin/login")
                        assert resp.status_code == 200
                        assert "后台登录已停用" in resp.text
                        # 提交直接 503（不做凭据比对，也不进限流/锁定计数）
                        resp = await client.post(
                            "/admin/login",
                            data={"username": "admin", "password": "anything"},
                        )
                        assert resp.status_code == 503, template_value
                        # 受保护页面同样不可用
                        resp = await client.get("/admin/accounts")
                        assert resp.status_code == 503
            finally:
                settings.admin_password = old_pw
                auth._sessions.clear()
                await _teardown_app(app)

        _run(flow())

    def test_change_password_rejects_default_and_clears_all_sessions(self) -> None:
        async def flow() -> None:
            from app.admin import auth

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "old-password-2"
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "old-password-2"},
                    )
                    assert resp.status_code == 303
                    token = resp.cookies.get("admin_session")
                    cookies = {"admin_session": token}

                    # 另一个浏览器上的"旧会话"
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "old-password-2"},
                    )
                    other_token = resp.cookies.get("admin_session")
                    assert other_token and other_token != token
                    # 明文口令登录时已透明迁移为哈希，后续断言以迁移后的值为基准
                    stored_before = settings.admin_password
                    assert stored_before.startswith("pbkdf2$")

                    # 想把口令改回模板默认值：拒绝
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-2",
                        "new_password": "admin123",
                        "confirm_password": "admin123",
                    })
                    assert resp.status_code == 200 and "默认口令" in resp.text
                    assert settings.admin_password == stored_before

                    # 成功改密：全部后台会话一起作废，不只是当前这条
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-2",
                        "new_password": "new-password-2",
                        "confirm_password": "new-password-2",
                    })
                    assert resp.status_code == 303
                    assert auth._sessions == {}
                    for tk in (token, other_token):
                        r = await client.get("/admin/accounts", cookies={"admin_session": tk})
                        assert r.status_code in (303, 401)
            finally:
                settings.admin_password = old_pw
                auth._sessions.clear()
                await _teardown_app(app)

        _run(flow())

    def test_env_password_wins_over_stale_db_default(self) -> None:
        """库里残留模板默认值时，用 .env 的强口令覆盖，否则 fail-closed 会变成永久锁门。"""
        from app.config import _reconcile_admin_password
        from app.db.repositories.app_settings_repo import AppSettingsRepository

        async def flow() -> None:
            app = await _setup_app()
            repo = AppSettingsRepository()
            old_pw = settings.admin_password
            try:
                await repo.upsert({"admin_password": "admin123"})
                # .env 也是默认值：保持原样（此时登录被挡住，提示去配置）
                with patch(
                    "app.config._bootstrap_settings_to_dict",
                    return_value={"admin_password": "admin123"},
                ):
                    await _reconcile_admin_password(repo)
                doc = await repo.get_document()
                assert doc is not None and doc.admin_password == "admin123"

                # .env 给了强口令：覆盖为哈希并写回库
                with patch(
                    "app.config._bootstrap_settings_to_dict",
                    return_value={"admin_password": "a-strong-new-password"},
                ):
                    await _reconcile_admin_password(repo)
                doc = await repo.get_document()
                assert doc is not None and doc.admin_password.startswith("pbkdf2$")

                # 库里已是正常口令：绝不覆盖
                await repo.upsert({"admin_password": "already-changed-password"})
                with patch(
                    "app.config._bootstrap_settings_to_dict",
                    return_value={"admin_password": "another-password"},
                ):
                    await _reconcile_admin_password(repo)
                doc = await repo.get_document()
                assert doc is not None and doc.admin_password == "already-changed-password"
            finally:
                settings.admin_password = old_pw
                await _teardown_app(app)

        _run(flow())

    def test_generated_password_is_usable(self) -> None:
        from app.admin.auth import hash_password
        from app.utils.password_hashing import (
            generate_admin_password,
            is_insecure_admin_password,
            verify_password,
        )

        assert is_insecure_admin_password("")
        assert is_insecure_admin_password("  admin123  ")
        assert is_insecure_admin_password("your-admin-password")
        assert not is_insecure_admin_password("some-unknown-pw")

        password = generate_admin_password()
        assert len(password) >= 12
        assert not is_insecure_admin_password(password)
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(not c.isalnum() for c in password)
        # 生成的口令能直接被后台的验密逻辑接受
        assert verify_password(password, hash_password(password))
        assert generate_admin_password() != password


class TestDashboardAndSettings:
    """仪表盘、AI模型配置与热重载。"""
    def test_dashboard_multimedia_reload_metrics(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            old_max_tokens = settings.chat_max_tokens
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/")
                    assert resp.status_code == 200

                    resp = await client.get("/admin/multimedia")
                    assert resp.status_code == 200

                    # 保存配置（基础配置组 JSON 合并 + 脱敏密钥保留 + 非法数值跳过）
                    resp = await client.post("/admin/multimedia", data={
                        "model_configs_json": (
                            '[{"id":"text-default","name":"文本",'
                            '"base_url":"http://x","api_key":"sk-abcdef123456",'
                            '"default_model":"m1"},'
                            '{"id":"","name":"无ID被跳过"}]'
                        ),
                        "chat_config_id": "text-default",
                        "chat_model": "m1",
                        "chat_temperature": "0.5",
                        "chat_max_tokens": "100",
                        "chat_timeout_seconds": "bad",
                        "asr_enabled": "on",
                    })
                    assert resp.status_code == 200 and "已保存并生效" in resp.text

                    resp = await client.post("/admin/api/reload-settings")
                    assert resp.status_code == 200 and resp.json()["status"] == "ok"

                    resp = await client.get("/admin/api/metrics")
                    assert resp.status_code == 200
            finally:
                settings.chat_max_tokens = old_max_tokens
                await _teardown_app(app)

        _run(flow())

    def test_dashboard_emotion_distribution_is_chinese(self) -> None:
        """情绪图例必须全部中文，不能把检测器的英文标签透传到前端。"""

        async def flow() -> None:
            app = await _setup_app()
            try:
                repo = MessageRepository()
                for tag in ("confused", "tired", "happy", "shy"):
                    await repo.create(
                        "conv-emotion", "user", "测试消息", emotion_tag=tag
                    )

                async with _client(app) as client:
                    # mongomock 不支持带 timezone 的 $dateToString，按日曲线打桩；
                    # 情绪聚合仍走真实仓储逻辑。
                    with (
                        patch.object(
                            MessageRepository, "count_daily", AsyncMock(return_value=[])
                        ),
                        patch.object(
                            MessageRepository,
                            "avg_latency_daily",
                            AsyncMock(return_value=[]),
                        ),
                    ):
                        resp = await client.get("/admin/")
                assert resp.status_code == 200
                match = re.search(
                    r"window\.DASHBOARD_CHARTS = (\{.*?\});", resp.text, re.S
                )
                assert match, "仪表盘未注入图表数据"
                charts = json.loads(match.group(1))
                assert set(charts["emotion"]["labels"]) == {
                    "困惑", "疲惫", "开心", "害羞",
                }
                assert "confused" not in resp.text
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_helper_functions(self) -> None:
        """路由模块内的纯函数。"""
        from datetime import datetime, timezone

        from app.admin.router import (
            _group_field,
            _mask_api_key,
            _masked_model_configs,
            _to_beijing,
        )

        assert _mask_api_key("") == ""
        assert _mask_api_key("short") == "*****"
        assert _mask_api_key("sk-abcdef123456") == "sk-a****3456"
        assert _group_field({"id": "a"}, "id") == "a"
        assert _group_field({"id": None}, "id") == ""
        masked = _masked_model_configs([{"id": "g1", "api_key": "sk-abcdef123456"}])
        assert masked[0]["has_key"] is True and "****" in masked[0]["api_key_masked"]
        assert _to_beijing(None) is None
        bj = _to_beijing(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
        assert bj is not None and bj.hour == 8


class TestFeatureGapThresholds:
    """通过真实后台 POST、仓库及热重载验证两档间隔参数。"""

    @pytest.mark.parametrize(("form", "expected"), [
        ({"time_session_gap_hours": "8", "time_reunion_gap_hours": "72"}, (8, 72)),
        ({"time_session_gap_hours": "1", "time_reunion_gap_hours": "1"}, (1, 1)),
        ({"time_session_gap_hours": "72", "time_reunion_gap_hours": "720"}, (72, 720)),
        ({"time_session_gap_hours": "0", "time_reunion_gap_hours": "0"}, (1, 1)),
        ({"time_session_gap_hours": "-5", "time_reunion_gap_hours": "-1"}, (1, 1)),
        ({"time_session_gap_hours": "999", "time_reunion_gap_hours": "9999"}, (72, 720)),
        ({"time_session_gap_hours": "24", "time_reunion_gap_hours": "8"}, (24, 24)),
        ({"time_session_gap_hours": "100", "time_reunion_gap_hours": "24"}, (72, 72)),
        ({"time_session_gap_hours": "bad", "time_reunion_gap_hours": "bad"}, (12, 96)),
        ({"time_session_gap_hours": "", "time_reunion_gap_hours": ""}, (12, 96)),
        ({"time_session_gap_hours": "3.5", "time_reunion_gap_hours": "NaN"}, (12, 96)),
        ({}, (12, 96)),
        ({"time_session_gap_hours": "24"}, (24, 96)),
        ({"time_reunion_gap_hours": "3"}, (12, 12)),
    ])
    def test_post_clamps_persists_and_reloads(
        self, form: dict[str, str], expected: tuple[int, int],
    ) -> None:
        """边界限幅、非法回退及强档不小于本次软档，同时验证页面回显。"""
        from app.db.repositories.app_settings_repo import AppSettingsRepository

        async def flow() -> None:
            """完整调用 ASGI 路由，隔离热重载对全局配置及环境变量的影响。"""
            app = await _setup_app()
            try:
                with patch.dict(vars(settings)), patch.dict(os.environ):
                    settings.time_session_gap_hours = 12
                    settings.time_reunion_gap_hours = 96
                    async with _client(app) as client:
                        before = await client.get("/admin/features")
                        assert before.status_code == 200
                        for field, value in (
                            ("time_session_gap_hours", 12), ("time_reunion_gap_hours", 96),
                        ):
                            assert re.search(
                                rf'<input\b[^>]*name="{field}"[^>]*value="{value}"',
                                before.text,
                            )
                        response = await client.post("/admin/features", data=form)
                        assert response.status_code == 200
                        assert "已保存并生效" in response.text
                        doc = await AppSettingsRepository().get_document()
                        assert doc is not None
                        assert (doc.time_session_gap_hours, doc.time_reunion_gap_hours) == expected
                        assert (
                            settings.time_session_gap_hours, settings.time_reunion_gap_hours,
                        ) == expected
                        after = await client.get("/admin/features")
                        assert after.status_code == 200
                        for field, value in zip(
                            ("time_session_gap_hours", "time_reunion_gap_hours"), expected,
                        ):
                            for html in (response.text, after.text):
                                assert re.search(
                                    rf'<input\b[^>]*name="{field}"[^>]*value="{value}"', html,
                                )
            finally:
                await _teardown_app(app)

        _run(flow())


class TestAdminConversations:
    """对话管理路由。"""

    def test_list_detail_delete(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                conv_repo = ConversationRepository()
                msg_repo = MessageRepository()
                identity = await IdentityRepository().create(
                    IdentityCreate(account_id="acc-1", nickname="小明")
                )
                conv = await conv_repo.create(identity.id, "prof-x", "svc-1", title="谈心")
                msg = await msg_repo.create(conv.id, "user", "你好")

                async with _client(app) as client:
                    resp = await client.get("/admin/conversations")
                    assert resp.status_code == 200
                    # 身份列渲染昵称（回归：模板误用改名前的 info.user 会退化成 ID 前缀）
                    assert "小明" in resp.text
                    resp = await client.get(
                        "/admin/conversations", params={"q": "谈心"}
                    )
                    assert resp.status_code == 200

                    # 详情（整页 + HTMX 片段），不存在 → 404
                    resp = await client.get(f"/admin/conversations/{conv.id}")
                    assert resp.status_code == 200
                    resp = await client.get(
                        f"/admin/conversations/{conv.id}",
                        headers={"HX-Request": "true"},
                    )
                    assert resp.status_code == 200
                    resp = await client.get("/admin/conversations/" + "0" * 24)
                    assert resp.status_code == 404

                    # 删除单条消息
                    resp = await client.post(
                        f"/admin/conversations/{conv.id}/messages/{msg.id}/delete"
                    )
                    assert resp.status_code == 303
                    assert await msg_repo.count_by_conversation(conv.id) == 0

                    # 删除整个对话；再次删除 → 404
                    resp = await client.post(f"/admin/conversations/{conv.id}/delete")
                    assert resp.status_code == 303
                    resp = await client.post(f"/admin/conversations/{conv.id}/delete")
                    assert resp.status_code == 404
            finally:
                await _teardown_app(app)

        _run(flow())


class TestBilling:
    """套餐计费路由。"""

    def test_templates_and_apply(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                from app.db.repositories.account_repo import AccountRepository
                from app.db.repositories.order_repo import OrderRepository
                from app.db.repositories.quota_repo import QuotaRepository
                from app.models.account import AccountCreate

                acc = await AccountRepository().create(AccountCreate(name="账A"))
                tpl_repo = QuotaTemplateRepository()

                async with _client(app) as client:
                    resp = await client.get("/admin/billing")
                    assert resp.status_code == 200

                    # 空名称不创建
                    resp = await client.post(
                        "/admin/billing/templates/create", data={"name": " "}
                    )
                    assert resp.status_code == 303
                    assert len(await tpl_repo.list_all()) == 0

                    # 创建模板（含有效期）
                    resp = await client.post(
                        "/admin/billing/templates/create",
                        data={
                            "name": "月卡", "text_limit": "100",
                            "voice_limit": "10", "image_limit": "5",
                            "validity_days": "30",
                        },
                    )
                    assert resp.status_code == 303
                    tpl = (await tpl_repo.list_all())[0]

                    # 编辑模板
                    resp = await client.post(
                        f"/admin/billing/templates/{tpl.id}/update",
                        data={"name": "月卡v2", "text_limit": "200"},
                    )
                    assert resp.status_code == 303

                    # 无效模板 ID 开通 → 直接重定向，不记台账
                    resp = await client.post(
                        f"/admin/accounts/{acc.id}/apply-template",
                        data={"template_id": ""},
                    )
                    assert resp.status_code == 303
                    assert await OrderRepository().count() == 0

                    # 正常开通：写额度 + 记台账
                    resp = await client.post(
                        f"/admin/accounts/{acc.id}/apply-template",
                        data={"template_id": tpl.id, "amount": "9.9"},
                    )
                    assert resp.status_code == 303
                    quota = await QuotaRepository().find_by_account(acc.id)
                    assert quota is not None and quota.text_quota.limit == 200
                    assert quota.text_quota.expires_at is not None
                    assert await OrderRepository().count() == 1

                    resp = await client.get("/admin/billing")
                    assert resp.status_code == 200

                    resp = await client.post(
                        f"/admin/billing/templates/{tpl.id}/delete"
                    )
                    assert resp.status_code == 303
            finally:
                await _teardown_app(app)

        _run(flow())


class TestAdminProfiles:
    """角色管理路由。"""

    def test_list_detail_toggle_edit(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                repo = ProfileRepository()
                profile = await repo.create(
                    PromptProfileCreate(slug="hero", name="主角")
                )
                await repo.create(
                    PromptProfileCreate(
                        slug="w-alice-1", name="阿禾自建", visibility="private", account_id="acc-1"
                    )
                )
                async with _client(app) as client:
                    resp = await client.get("/admin/profiles")
                    assert resp.status_code == 200 and "主角" in resp.text
                    resp = await client.get("/admin/profiles", params={"q": "主角"})
                    assert resp.status_code == 200

                    # 可见范围筛选：C 端自建角色的事后审核巡查入口
                    priv = await client.get("/admin/profiles", params={"vis": "private"})
                    assert priv.status_code == 200
                    assert "阿禾自建" in priv.text and "主角" not in priv.text
                    pub = await client.get("/admin/profiles", params={"vis": "public"})
                    assert pub.status_code == 200
                    assert "主角" in pub.text and "阿禾自建" not in pub.text

                    resp = await client.get("/admin/profiles/hero")
                    assert resp.status_code == 200
                    resp = await client.get("/admin/profiles/none")
                    assert resp.status_code == 404

                    # 被服务引用时禁用 → 400
                    svc = await ServiceRepository().create(ServiceCreate(
                        account_id="acc-1", identity_id="u-1", bot_id="b-1",
                        profile_id=profile.id,
                    ))
                    resp = await client.post(
                        "/admin/profiles/hero/toggle",
                        data={"disable_reason": "下线"},
                    )
                    assert resp.status_code == 400
                    await ServiceRepository().delete(svc.id)

                    # 禁用成功 → 再启用
                    resp = await client.post(
                        "/admin/profiles/hero/toggle",
                        data={"disable_reason": "下线"},
                    )
                    assert resp.status_code == 200
                    disabled = await repo.find_by_slug("hero")
                    assert disabled is not None and disabled.is_active is False
                    resp = await client.post("/admin/profiles/hero/toggle")
                    assert resp.status_code == 200

                    # 编辑表单保存（trait 非法值回退 0.5）
                    resp = await client.post(
                        "/admin/profiles/hero/edit",
                        data={
                            "name": "主角v2", "is_active": "on",
                            "persona_core_values": "勇敢\n善良",
                            "rel_name": "配角", "rel_relation": "朋友",
                            "rel_attitude": "信任",
                            "trait_key": "开朗", "trait_value": "bad",
                        },
                    )
                    assert resp.status_code == 200
                    edited = await repo.find_by_slug("hero")
                    assert edited is not None and edited.name == "主角v2"
                    assert edited.personality.traits["开朗"] == 0.5
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_reference_image_ops(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                repo = ProfileRepository()
                await repo.create(PromptProfileCreate(slug="hero", name="主角"))
                async with _client(app) as client:
                    # 上传非图片 → 错误提示
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/upload",
                        files={"file": ("a.txt", b"hello", "text/plain")},
                    )
                    assert resp.status_code == 200 and "请上传图片文件" in resp.text

                    # 上传空文件 → 错误提示
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/upload",
                        files={"file": ("a.png", b"", "image/png")},
                    )
                    assert resp.status_code == 200 and "上传文件为空" in resp.text

                    # 上传有效图片 → 定妆照更新
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/upload",
                        files={"file": ("a.png", b"\x89PNG-data", "image/png")},
                    )
                    assert resp.status_code == 200 and "定妆照已更新" in resp.text
                    prof = await repo.find_by_slug("hero")
                    assert prof is not None
                    assert prof.reference_image_url.startswith("/api/media/")

                    # 确认候选图：非法地址被拒 / 合法地址生效
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/confirm",
                        data={"media_url": "http://evil"},
                    )
                    assert resp.status_code == 200 and "无效的候选图地址" in resp.text
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/confirm",
                        data={"media_url": "/api/media/xyz"},
                    )
                    assert resp.status_code == 200 and "定妆照已更新" in resp.text

                    # 未填写外貌时生成 → 提示先填写
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/generate"
                    )
                    assert resp.status_code == 200 and "外貌特征" in resp.text

                    # 清除定妆照
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/clear"
                    )
                    assert resp.status_code == 200 and "定妆照已清除" in resp.text
            finally:
                await _teardown_app(app)

        _run(flow())


class TestPublicApi:
    """公共 API：health / profiles / conversations / media。"""

    def test_health_detail_requires_admin_session(self) -> None:
        """匿名只给总状态；延迟、磁盘余量、任务槽位与异常原文留给后台会话。"""
        import time as time_mod

        from app.admin import auth

        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "health-pw"
            try:
                async with _client(app) as client:
                    resp = await client.get("/api/health")
                    assert resp.status_code == 200
                    body = resp.json()
                    assert set(body["checks"]) == {
                        "mongodb", "llm", "embedding", "disk", "background_tasks"
                    }
                    for name, item in body["checks"].items():
                        assert set(item) == {"status"}, name

                    token = "health-admin-session"
                    auth._sessions[token] = time_mod.time() + 60
                    resp = await client.get(
                        "/api/health", cookies={"admin_session": token}
                    )
                    assert resp.status_code == 200
                    checks = resp.json()["checks"]
                    assert set(checks["mongodb"]) != {"status"}
                    assert set(checks["disk"]) != {"status"}
                    assert set(checks["background_tasks"]) != {"status"}
            finally:
                auth._sessions.clear()
                settings.admin_password = old_pw
                await _teardown_app(app)

        _run(flow())

    def test_health_and_profiles_api(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            try:
                await ProfileRepository().create(
                    PromptProfileCreate(slug="hero", name="主角")
                )
                async with _client(app) as client:
                    resp = await client.get("/api/health")
                    assert resp.status_code == 200

                    # 角色全量接口已收归管理员：未登录不放行
                    # （require_admin 在未配置密码时返回 503，故两种拒绝码都算通过）
                    resp = await client.get("/api/profiles")
                    assert resp.status_code in (401, 503)
                    resp = await client.post(
                        "/api/profiles", json={"slug": "x", "name": "x"}
                    )
                    assert resp.status_code in (401, 503)

                app.dependency_overrides.clear()
                from app.admin.auth import require_admin

                app.dependency_overrides[require_admin] = lambda: "admin"
                async with _client(app) as client:
                    # 列表 / 详情 / 创建 / 更新 / 删除
                    resp = await client.get("/api/profiles")
                    assert resp.status_code == 200 and len(resp.json()) == 1
                    resp = await client.get("/api/profiles", params={"active_only": "false"})
                    assert resp.status_code == 200
                    resp = await client.get("/api/profiles/hero")
                    assert resp.status_code == 200 and resp.json()["slug"] == "hero"
                    resp = await client.get("/api/profiles/none")
                    assert resp.status_code == 404

                    resp = await client.post(
                        "/api/profiles",
                        json={"slug": "hero", "name": "重复"},
                    )
                    assert resp.status_code == 409
                    resp = await client.post(
                        "/api/profiles",
                        json={"slug": "villain", "name": "反派"},
                    )
                    assert resp.status_code == 201

                    resp = await client.put(
                        "/api/profiles/villain", json={"name": "反派v2"}
                    )
                    assert resp.status_code == 200 and resp.json()["updated"] is True
                    resp = await client.put(
                        "/api/profiles/none", json={"name": "x"}
                    )
                    assert resp.status_code == 404

                    resp = await client.delete("/api/profiles/villain")
                    assert resp.status_code == 200 and resp.json()["deleted"] is True
                    resp = await client.delete("/api/profiles/none")
                    assert resp.status_code == 404
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_media_api(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            try:
                async with _client(app) as client:
                    # 媒体：不存在 → 404；新 public_id → 返回二进制
                    resp = await client.get("/api/media/" + "0" * 24)
                    assert resp.status_code == 404
                    from app.db.repositories.media_repo import MediaRepository
                    mid = await MediaRepository().create(b"img-bytes", "image/png")
                    resp = await client.get(f"/api/media/{mid}")
                    assert resp.status_code == 200
                    assert resp.content == b"img-bytes"
            finally:
                await _teardown_app(app)

        _run(flow())


class TestMediaAccess:
    """媒体访问：新 public_id 是能力令牌，旧 ObjectId 仅管理员可读。"""

    def test_public_capability_and_legacy_admin_only(self) -> None:
        async def flow() -> None:
            from app.db.repositories.media_repo import MediaRepository

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "media-pw"
            try:
                async with _client(app) as client:
                    pid = await MediaRepository().create(b"new-bytes", "image/png")
                    assert len(pid) == 32
                    resp = await client.get(f"/api/media/{pid}")
                    assert resp.status_code == 200
                    assert resp.content == b"new-bytes"
                    cache = resp.headers["cache-control"]
                    assert "private" in cache and "public" not in cache
                    assert resp.headers["x-content-type-options"] == "nosniff"

                    # 旧 ObjectId 无能力令牌：匿名 404，管理员会话可读
                    legacy = await mongo.get_database().media.insert_one({
                        "data": b"old-bytes", "content_type": "image/png",
                    })
                    oid = str(legacy.inserted_id)
                    assert (
                        await client.get(f"/api/media/{oid}")
                    ).status_code == 404

                    login = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "media-pw"},
                    )
                    assert login.status_code == 303
                    token = login.cookies.get("admin_session")
                    assert token
                    resp = await client.get(
                        f"/api/media/{oid}", cookies={"admin_session": token}
                    )
                    assert resp.status_code == 200
                    assert resp.content == b"old-bytes"

                    # 非法 ID 不再 500
                    assert (
                        await client.get("/api/media/not-an-id")
                    ).status_code == 404
            finally:
                settings.admin_password = old_pw
                await _teardown_app(app)

        _run(flow())


class TestProactiveApi:
    """主动消息 API。"""

    def test_trigger_and_records(self) -> None:
        async def flow() -> None:
            # 鉴权由 TestProactiveApiAuth 覆盖；本用例只关心业务分支
            app = await _setup_app(override_auth=True)
            try:
                from unittest.mock import AsyncMock

                from app.db.repositories.proactive_repo import (
                    ProactiveRecordRepository,
                )
                from app.models.proactive_record import ProactiveRecord
                from app.proactive.scheduler import get_proactive_scheduler

                identity_repo = IdentityRepository()
                identity = await identity_repo.create(IdentityCreate(
                    account_id="acc-1", platform="qq", platform_id="oid-1",
                ))
                await ServiceRepository().create(ServiceCreate(
                    account_id="acc-1", identity_id=identity.id, bot_id="b-1",
                    profile_id="p-1",
                ))

                scheduler = get_proactive_scheduler()
                scheduler._send_message = AsyncMock(return_value=True)

                async with _client(app) as client:
                    # 身份不存在 → 404
                    resp = await client.post("/api/proactive/trigger", json={
                        "identity_id": "no-user", "bot_id": "b-1",
                    })
                    assert resp.status_code == 404

                    # 自定义内容 + 发送成功
                    resp = await client.post("/api/proactive/trigger", json={
                        "identity_id": identity.id, "bot_id": "b-1",
                        "content": "想你了", "stage": 1,
                    })
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["success"] is True and body["record_id"]

                    # 发送失败分支
                    scheduler._send_message = AsyncMock(return_value=False)
                    resp = await client.post("/api/proactive/trigger", json={
                        "identity_id": identity.id, "bot_id": "b-1", "content": "在吗",
                    })
                    assert resp.status_code == 200 and resp.json()["success"] is False

                    # 发送抛异常：返回可读失败并落 failed，不能留 pending
                    scheduler._send_message = AsyncMock(side_effect=RuntimeError("boom"))
                    resp = await client.post("/api/proactive/trigger", json={
                        "identity_id": identity.id, "bot_id": "b-1", "content": "在吗",
                    })
                    assert resp.status_code == 200 and resp.json()["success"] is False
                    latest = await ProactiveRecordRepository().find_latest_by_identity(
                        identity.id, "b-1", limit=1
                    )
                    assert latest and latest[0].status == "failed"
                    assert "boom" in latest[0].error_msg

                    # 记录查询
                    await ProactiveRecordRepository().create(ProactiveRecord(
                        identity_id=identity.id, platform_openid="oid-1", bot_id="b-1",
                        stage=1,
                    ))
                    resp = await client.get("/api/proactive/records", params={
                        "identity_id": identity.id, "bot_id": "b-1",
                    })
                    assert resp.status_code == 200 and len(resp.json()) >= 1
            finally:
                await _teardown_app(app)

        _run(flow())
