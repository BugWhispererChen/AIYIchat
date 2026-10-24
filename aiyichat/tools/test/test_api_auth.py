"""C 端鉴权 API 与越权回归测试（mongomock + ASGI 内存客户端）。

覆盖计划第六节的必测项：
- 取验证码 → 注册（pending）→ 审核通过 → 登录 → 会话访问
- 验证码一次性消费、IP 限流 429、重复用户名 409、未成年拦截
- `chat/stream` 无 token 必须 401、带 token 但 platform_id/bot_id 不匹配必须 403
- 开关关闭时旧 API 行为完全不变
"""

import asyncio
from typing import Any, AsyncIterator, Callable

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.api import conversations_api
from app.api.auth_api import client_ip
from app.config import settings
from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.profile import PromptProfileCreate
from app.services import auth_service
from app.services.captcha_service import captcha_service
from app.utils.rate_limit import login_limiter, register_limiter

WEB_BOT_ID = "web-bot-api"
PROFILE_SLUG = "lin-yu"
PASSWORD = "passw0rd9"
CLIENT_IP = "203.0.113.7"

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
    "web_free_quota_template_id", "web_session_hours", "web_session_max_concurrent",
    "web_login_max_fail", "web_login_lockout_minutes",
    "web_rate_limit_register", "web_rate_limit_login",
    "trust_proxy_headers",
)


@pytest.fixture(autouse=True)
def web_flags():
    """默认打开网页鉴权与注册、关闭验证码与限流阈值放宽，用例后复原。

    验证码与限流各自有专项用例，在其用例内单独打开/收紧。
    """
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = False
    settings.web_rate_limit_register = 100
    settings.web_rate_limit_login = 100
    settings.web_session_hours = 24
    settings.web_session_max_concurrent = 3
    settings.web_login_max_fail = 5
    settings.web_bot_id = WEB_BOT_ID
    settings.web_default_profile_slug = PROFILE_SLUG
    settings.web_free_quota_template_id = ""
    # 测试客户端用 X-Forwarded-For 模拟不同客户端 IP，等价于"代理已覆写"场景
    settings.trust_proxy_headers = True
    register_limiter.reset()
    login_limiter.reset()
    captcha_service.reset()
    yield
    for key, value in saved.items():
        setattr(settings, key, value)
    register_limiter.reset()
    login_limiter.reset()
    captcha_service.reset()


async def _setup_app(db_name: str) -> Any:
    """挂内存库、seed 系统 Bot 与公共角色，返回 ASGI 应用。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()[db_name]
    account = await AccountRepository().create(AccountCreate(name="系统账号"))
    await BotConfigRepository().create(
        BotConfigCreate(id=WEB_BOT_ID, account_id=account.id, name="网页助手", platform="web")
    )
    await ProfileRepository().create(
        PromptProfileCreate(slug=PROFILE_SLUG, name="林语", visibility="public")
    )
    from app.main import app

    return app


def _client(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-For": CLIENT_IP},
    )


async def _register(client: AsyncClient, username: str, password: str = PASSWORD) -> Any:
    return await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": password,
            "terms_accepted": True,
            "privacy_accepted": True,
            "age_confirmed": True,
        },
    )


async def _login(client: AsyncClient, username: str, password: str = PASSWORD) -> str:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


async def _approve(username: str) -> str:
    """走服务层审核通过，返回身份 ID。"""
    credential = await WebCredentialRepository().find_by_username(username)
    assert credential is not None
    assert await auth_service.approve(credential.id, "admin") is True
    return credential.id


class _FakeEngine:
    """替身引擎：记录实际收到的身份参数，供越权断言。"""

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
        _FakeEngine.calls.append(
            {
                "bot_id": self.bot_id,
                "platform": platform,
                "platform_id": platform_id,
                "text": text,
                "nickname": nickname,
                "content_type": content_type,
                "service_id": service_id,
                "identity_id": identity_id,
            }
        )
        yield f"echo|{platform}|{platform_id}|{self.bot_id}"


def _with_fake_engine(run: Callable[[], Any]) -> None:
    """在替身引擎下执行异步流程，结束后还原真实引擎引用。"""
    import app.conversation.engine as engine_module

    original = engine_module.ConversationEngine
    _FakeEngine.calls = []
    engine_module.ConversationEngine = _FakeEngine  # type: ignore[misc]
    try:
        asyncio.run(run())
    finally:
        engine_module.ConversationEngine = original  # type: ignore[misc]


class TestCaptchaEndpoint:
    """图形验证码接口与一次性消费。"""

    def test_issue(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_captcha_issue")
            try:
                async with _client(app) as client:
                    resp = await client.get("/api/auth/captcha")
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["captcha_id"]
                    assert body["image"].startswith("data:image/png;base64,")
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_gate_off(self) -> None:
        """总开关关闭时不签发验证码。"""
        settings.web_auth_enabled = False

        async def flow() -> None:
            app = await _setup_app("t_captcha_gate")
            try:
                async with _client(app) as client:
                    assert (await client.get("/api/auth/captcha")).status_code == 403
                    assert (
                        await client.post("/api/auth/register", json={"username": "abc123", "password": PASSWORD})
                    ).status_code == 403
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_required_and_single_use(self) -> None:
        """开启验证码：缺失/错误答案拒绝，正确答案放行，同一验证码复用立即失效。"""
        settings.web_captcha_enabled = True

        async def flow() -> None:
            app = await _setup_app("t_captcha_use")
            try:
                async with _client(app) as client:
                    payload = {
                        "username": "capuser",
                        "password": PASSWORD,
                        "terms_accepted": True,
                        "privacy_accepted": True,
                        "age_confirmed": True,
                    }
                    # 无验证码
                    resp = await client.post("/api/auth/register", json=payload)
                    assert resp.status_code == 400 and resp.json()["code"] == "captcha_failed"

                    cid = (await client.get("/api/auth/captcha")).json()["captcha_id"]
                    captcha_service._answers[cid]  # 确认答案已入表
                    resp = await client.post("/api/auth/register", json={**payload, "captcha_id": cid, "captcha_answer": "99999"})
                    assert resp.status_code == 400 and resp.json()["code"] == "captcha_failed"

                    # 答错已被消费 → 必须重新取码
                    cid2 = (await client.get("/api/auth/captcha")).json()["captcha_id"]
                    answer2 = str(captcha_service._answers[cid2])
                    resp = await client.post(
                        "/api/auth/register", json={**payload, "captcha_id": cid2, "captcha_answer": answer2}
                    )
                    assert resp.status_code == 201, resp.text
                    resp = await client.post(
                        "/api/auth/register",
                        json={**payload, "captcha_id": cid2, "captcha_answer": answer2, "username": "capuser2"},
                    )
                    assert resp.status_code == 400 and resp.json()["code"] == "captcha_failed"
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_expired_captcha(self, monkeypatch) -> None:
        """超过有效期的验证码判失败。"""
        import time as time_module

        real_monotonic = time_module.monotonic
        state = {"offset": 0.0}

        def fake_monotonic() -> float:
            return real_monotonic() + state["offset"]

        monkeypatch.setattr("app.services.captcha_service.time.monotonic", fake_monotonic)
        cid, _ = captcha_service.generate()
        answer = str(captcha_service._answers[cid])
        state["offset"] = 3600.0  # 前进 1 小时 → TTL 到期
        assert captcha_service.verify(cid, answer) is False
        assert captcha_service.verify("unknown-id", "1") is False
        assert captcha_service.verify(cid, answer) is False


class TestAuthApiFlow:
    """注册 / 登录 / me / logout 主链路。"""

    def test_full_flow(self) -> None:
        """注册→pending 不可登录→审核通过→登录→me→登出→me 失效。"""

        async def flow() -> None:
            app = await _setup_app("t_auth_flow")
            try:
                async with _client(app) as client:
                    resp = await _register(client, "streamer")
                    assert resp.status_code == 201, resp.text
                    assert resp.json()["status"] == "pending"

                    resp = await client.post("/api/auth/login", json={"username": "streamer", "password": PASSWORD})
                    assert resp.status_code == 403 and resp.json()["code"] == "login_pending"

                    await _approve("streamer")
                    token = await _login(client, "streamer")
                    assert token

                    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["username"] == "streamer"
                    assert body["bot_id"] == WEB_BOT_ID
                    assert body["quota"]["text"]["limit"] == auth_service.FREE_TEXT_LIMIT

                    assert (await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})).status_code == 200
                    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_no_review_activates(self) -> None:
        """免审开关打开时注册后直接可登录。"""
        settings.web_register_require_review = False

        async def flow() -> None:
            app = await _setup_app("t_auth_fast")
            try:
                async with _client(app) as client:
                    resp = await _register(client, "fastuser")
                    assert resp.json()["status"] == "active"
                    token = await _login(client, "fastuser")
                    assert len(token) > 20
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_rejections(self) -> None:
        """重复用户名 409、未成年 400、弱密码 400、非法用户名 400。"""

        async def flow() -> None:
            app = await _setup_app("t_auth_reject")
            try:
                async with _client(app) as client:
                    assert (await _register(client, "dup1")).status_code == 201
                    resp = await _register(client, "dup1")
                    assert resp.status_code == 409 and resp.json()["code"] == "username_taken"

                    resp = await client.post(
                        "/api/auth/register",
                        json={"username": "kid1", "password": PASSWORD, "terms_accepted": True,
                              "privacy_accepted": True, "age_confirmed": False},
                    )
                    assert resp.status_code == 400 and resp.json()["code"] == "age_not_confirmed"

                    resp = await _register(client, "weak1", password="password123")
                    assert resp.status_code == 400 and resp.json()["code"] == "weak_password"

                    resp = await _register(client, "admin")
                    assert resp.status_code == 400 and resp.json()["code"] == "invalid_username"
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_me_requires_valid_token(self) -> None:
        """无令牌 401、伪造令牌 401、禁用后令牌立即失效。"""

        async def flow() -> None:
            app = await _setup_app("t_auth_me")
            try:
                async with _client(app) as client:
                    await _register(client, "tokenme")
                    ident_id = await _approve("tokenme")
                    token = await _login(client, "tokenme")
                    assert (await client.get("/api/auth/me")).status_code == 401
                    assert (
                        await client.get("/api/auth/me", headers={"Authorization": "Bearer forged"})
                    ).status_code == 401
                    assert (
                        await client.get("/api/auth/me", headers={"X-Auth-Token": token})
                    ).status_code == 200
                    await auth_service.set_disabled(ident_id, True, "admin", "违规")
                    assert (
                        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                    ).status_code == 401
            finally:
                mongo._db = None

        asyncio.run(flow())


class TestRateLimitApi:
    """IP 限流：注册与登录各自独立计数并返回 429。"""

    def test_client_ip_ignores_forged_header_by_default(self) -> None:
        """默认直连部署不信任 X-Forwarded-For；仅显式开启时才取代理写入值。"""
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")],
            "client": ("198.51.100.2", 12345),
        })
        old = settings.trust_proxy_headers
        try:
            settings.trust_proxy_headers = False
            assert client_ip(request) == "198.51.100.2"
            settings.trust_proxy_headers = True
            assert client_ip(request) == "203.0.113.5"
        finally:
            settings.trust_proxy_headers = old

    def test_register_429(self) -> None:
        settings.web_rate_limit_register = 2

        async def flow() -> None:
            app = await _setup_app("t_rl_register")
            try:
                async with _client(app) as client:
                    assert (await _register(client, "rl1")).status_code == 201
                    assert (await _register(client, "rl2")).status_code == 201
                    resp = await _register(client, "rl3")
                    assert resp.status_code == 429
                    assert resp.json()["code"] == "register"
                    assert int(resp.headers["retry-after"]) >= 1
                    # 超限请求不得落库
                    assert await WebCredentialRepository().find_by_username("rl3") is None
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_login_429_independent(self) -> None:
        """登录限流独立于注册：注册额度用尽不影响登录计数。"""
        settings.web_rate_limit_register = 1
        settings.web_rate_limit_login = 2

        async def flow() -> None:
            app = await _setup_app("t_rl_login")
            try:
                async with _client(app) as client:
                    assert (await _register(client, "lfa")).status_code == 201
                    assert (await _register(client, "lfb")).status_code == 429
                    await _approve("lfa")
                    assert (await client.post("/api/auth/login", json={"username": "lfa", "password": "wrongpass1"})).status_code == 401
                    assert (await client.post("/api/auth/login", json={"username": "lfa", "password": "wrongpass2"})).status_code == 401
                    resp = await client.post("/api/auth/login", json={"username": "lfa", "password": PASSWORD})
                    assert resp.status_code == 429 and resp.json()["code"] == "login"
            finally:
                mongo._db = None

        asyncio.run(flow())


class TestChatStreamAuthorization:
    """流式对话越权回归：身份只能来自令牌。"""

    def test_no_token_401(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_stream_noauth")
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"bot_id": WEB_BOT_ID, "platform": "web", "platform_id": "victim", "text": "hi"},
                    )
                    assert resp.status_code == 401
            finally:
                mongo._db = None

        _with_fake_engine(flow)

    def test_impersonation_403(self) -> None:
        """带 A 的令牌但传他人 platform_id / 未授权 bot_id → 403。"""

        async def flow() -> None:
            app = await _setup_app("t_stream_imp")
            try:
                async with _client(app) as client:
                    await _register(client, "usera")
                    await _approve("usera")
                    token = await _login(client, "usera")
                    headers = {"Authorization": f"Bearer {token}"}

                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"bot_id": WEB_BOT_ID, "platform_id": "userb", "text": "hi"},
                        headers=headers,
                    )
                    assert resp.status_code == 403
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"bot_id": "someone-else-bot", "platform_id": "usera", "text": "hi"},
                        headers=headers,
                    )
                    assert resp.status_code == 403
                    assert _FakeEngine.calls == []  # 冒充请求不得进入引擎
            finally:
                mongo._db = None

        _with_fake_engine(flow)

    def test_banned_identity_cannot_chat(self) -> None:
        """被封禁（禁用）的身份：会话即时失效，发消息被拒。"""

        async def flow() -> None:
            app = await _setup_app("t_stream_ban")
            try:
                async with _client(app) as client:
                    await _register(client, "banned")
                    ident_id = await _approve("banned")
                    token = await _login(client, "banned")
                    headers = {"Authorization": f"Bearer {token}"}
                    assert (
                        await client.post("/api/conversations/chat/stream", json={"text": "hi"}, headers=headers)
                    ).status_code == 200
                    await auth_service.set_disabled(ident_id, True, "admin", "违规")
                    _FakeEngine.calls = []
                    resp = await client.post(
                        "/api/conversations/chat/stream", json={"text": "hi"}, headers=headers
                    )
                    assert resp.status_code == 401
                    assert _FakeEngine.calls == []  # 封禁后不得进入引擎、不得扣额度
            finally:
                mongo._db = None

        _with_fake_engine(flow)

    def test_credential_derived_from_token(self) -> None:
        """网页账号不传 platform_id：引擎收到的是令牌本人的身份。"""

        async def flow() -> None:
            app = await _setup_app("t_stream_ok")
            try:
                async with _client(app) as client:
                    await _register(client, "alice")
                    await _register(client, "bob")
                    await _approve("alice")
                    await _approve("bob")
                    token_a = await _login(client, "alice")
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"text": "hello"},
                        headers={"Authorization": f"Bearer {token_a}"},
                    )
                    assert resp.status_code == 200
                    assert "data: echo|web|alice|" in resp.text
                    call = _FakeEngine.calls[0]
                    assert call["platform"] == "web"
                    assert call["platform_id"] == "alice"
                    assert call["bot_id"] == WEB_BOT_ID
                    # 两个注册身份的会话与身份互不可见：各自独立 user/service
                    identity_a = await IdentityRepository().find_by_platform("web", "alice")
                    identity_b = await IdentityRepository().find_by_platform("web", "bob")
                    assert identity_a.id != identity_b.id and identity_a.account_id != identity_b.account_id
            finally:
                mongo._db = None

        _with_fake_engine(flow)

    def test_history_endpoints_scoped(self) -> None:
        """对话列表与消息历史：只能读自己的，读他人 403。"""

        class _FakeConvRepo:
            """替身对话仓库：按归属返回固定对话，避免构造完整对话文档。"""

            def __init__(self, owner_id: str) -> None:
                self.owner_id = owner_id

            async def find_by_id(self, conversation_id: str) -> Any:
                if conversation_id == "missing":
                    return None
                return type(
                    "Conv",
                    (),
                    {"id": conversation_id, "identity_id": self.owner_id, "service_id": ""},
                )()

        async def flow() -> None:
            app = await _setup_app("t_stream_hist")
            try:
                async with _client(app) as client:
                    await _register(client, "owner")
                    await _register(client, "intruder")
                    await _approve("owner")
                    await _approve("intruder")
                    owner = await IdentityRepository().find_by_platform("web", "owner")
                    other = await IdentityRepository().find_by_platform("web", "intruder")
                    token = await _login(client, "owner")
                    headers = {"Authorization": f"Bearer {token}"}

                    # 他人 identity_id → 403
                    resp = await client.get(f"/api/conversations?identity_id={other.id}", headers=headers)
                    assert resp.status_code == 403
                    # 自己的 identity_id → 200（无数据时为空列表）
                    resp = await client.get(f"/api/conversations?identity_id={owner.id}", headers=headers)
                    assert resp.status_code == 200 and resp.json() == []
                    # 他人 service_id → 404（按"服务不存在"处理，不泄露存在性）
                    svc_b = (await _services_of(other.id))[0].id
                    resp = await client.get(f"/api/conversations?service_id={svc_b}", headers=headers)
                    assert resp.status_code == 404

                    original = conversations_api._conv_repo
                    conversations_api._conv_repo = _FakeConvRepo(other.id)  # type: ignore[assignment]
                    try:
                        resp = await client.get("/api/conversations/conv-x/messages", headers=headers)
                        assert resp.status_code == 403
                        conversations_api._conv_repo = _FakeConvRepo(owner.id)  # type: ignore[assignment]
                        resp = await client.get("/api/conversations/conv-x/messages", headers=headers)
                        assert resp.status_code == 200
                        assert (await client.get("/api/conversations/missing/messages", headers=headers)).status_code == 404
                    finally:
                        conversations_api._conv_repo = original
            finally:
                mongo._db = None

        asyncio.run(flow())


async def _services_of(identity_id: str) -> list[Any]:
    """取该用户名下的对话服务列表。"""
    from app.db.repositories.service_repo import ServiceRepository

    return await ServiceRepository().list_by_identity(identity_id)


class TestConversationApiAlwaysRequiresAuth:
    """HTTP 对话接口不再有匿名旧分支：开关关闭或无令牌一律拒绝。"""

    def test_switch_off_closes_http_api(self) -> None:
        settings.web_auth_enabled = False

        async def flow() -> None:
            app = await _setup_app("t_legacy_stream")
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"bot_id": WEB_BOT_ID, "platform": "qq",
                              "platform_id": "openid-1", "text": "hi"},
                    )
                    assert resp.status_code == 401
                    assert (await client.get("/api/conversations")).status_code == 401
                    assert (
                        await client.get(
                            f"/api/conversations/{'0' * 24}/messages"
                        )
                    ).status_code == 401
                    assert _FakeEngine.calls == []
            finally:
                mongo._db = None

        _with_fake_engine(flow)

    def test_no_token_cannot_read_conversation(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_conv_no_token")
            try:
                async with _client(app) as client:
                    # 先有合法会话也不能被匿名读取
                    from app.db.repositories.conversation_repo import (
                        ConversationRepository,
                    )

                    conv = await ConversationRepository().create(
                        "identity-x", PROFILE_SLUG, "service-x"
                    )
                    resp = await client.get(
                        f"/api/conversations/{conv.id}/messages"
                    )
                    assert resp.status_code == 401
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_auth_endpoints_refused(self) -> None:
        """开关关闭时注册/登录一律 403，不写入任何网页凭据。"""
        settings.web_auth_enabled = False

        async def flow() -> None:
            app = await _setup_app("t_legacy_auth")
            try:
                async with _client(app) as client:
                    assert (await _register(client, "ghost")).status_code == 403
                    assert (
                        await client.post("/api/auth/login", json={"username": "ghost", "password": PASSWORD})
                    ).status_code == 403
                    assert await WebCredentialRepository().list_all() == []
            finally:
                mongo._db = None

        asyncio.run(flow())


class TestChatStreamPayloadCleaning:
    """SSE 网关层：内部标记不许出现在 data: 帧里，异常原文不许回显给客户端。

    引擎侧的清洗有单测覆盖，这里补的是"过完网关到浏览器的那段"：
    `stream_frames` 的分帧、错误帧的措辞，都是引擎单测看不到的部分。
    """

    def test_sse_frames_hide_internal_markup(self) -> None:
        import app.conversation.engine as engine_module
        from app.conversation.response_processor import (
            DisplayStreamCleaner,
            ResponseProcessor,
        )

        class _CleaningEngine:
            """用真实清洗器逐块下发，标签故意横跨增量（最坏情况）。"""

            def __init__(self, bot_id: str = "") -> None:
                self.bot_id = bot_id

            async def handle_message_stream(self, **kwargs: Any) -> AsyncIterator[str]:
                cleaner = DisplayStreamCleaner(ResponseProcessor())
                for delta in ("（微笑）给你", "拍一张[IMG:sunset", " over the sea]，看这"):
                    for piece in cleaner.push(delta):
                        yield piece
                for piece in cleaner.flush():
                    yield piece

        async def flow() -> None:
            app = await _setup_app("t_stream_clean")
            original = engine_module.ConversationEngine
            engine_module.ConversationEngine = _CleaningEngine  # type: ignore[misc]
            try:
                async with _client(app) as client:
                    await _register(client, "cleaner")
                    await _approve("cleaner")
                    token = await _login(client, "cleaner")
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"text": "拍一张"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert resp.status_code == 200, resp.text
                    body = resp.text
                    # 帧是分片下发的，拼接所有 data: 载荷再比对文案完整性
                    joined = "".join(
                        line.removeprefix("data: ")
                        for line in body.splitlines()
                        if line.startswith("data:") and line != "data: [DONE]"
                    )
                    assert "[IMG:" not in body, body
                    assert "sunset" not in body, body
                    assert "微笑" not in body, body
                    assert joined == "😊给你拍一张，看这", joined
            finally:
                engine_module.ConversationEngine = original  # type: ignore[misc]
                mongo._db = None

        asyncio.run(flow())

    def test_sse_frames_carry_gate_events_in_order(self) -> None:
        """开闸门后引擎 yield StreamEvent：控制帧必须原样、按序到达浏览器。

        分帧只发生在网关，引擎单测看不到；`test_stream_frames.py` 验的是 `stream_frames`
        的纯函数输出与前端 JS 契约，两者都碰不到 HTTP 响应里真实的帧序列。这里补的正是
        从路由出口到 `data:` 帧这一段——空气泡该没有帧、换行该被转义成字面 ``\\n``。
        """
        import app.conversation.engine as engine_module
        from app.conversation.stream_gate import StreamEvent

        class _GatedEngine:
            def __init__(self, bot_id: str = "") -> None:
                self.bot_id = bot_id

            async def handle_message_stream(self, **kwargs: Any) -> AsyncIterator[Any]:
                yield StreamEvent(kind="reset")
                yield StreamEvent(kind="msg", text="第一句")
                yield StreamEvent(kind="msg", text="")
                yield StreamEvent(kind="msg", text="第二句\n带换行")

        async def flow() -> None:
            app = await _setup_app("t_stream_gate_events")
            original = engine_module.ConversationEngine
            engine_module.ConversationEngine = _GatedEngine  # type: ignore[misc]
            try:
                async with _client(app) as client:
                    await _register(client, "gated")
                    await _approve("gated")
                    token = await _login(client, "gated")
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"text": "再说一次"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert resp.status_code == 200, resp.text
                    payloads = [
                        line.removeprefix("data: ")
                        for line in resp.text.splitlines()
                        if line.startswith("data:")
                    ]
                    assert payloads == [
                        "[RESET]", "[MSG]", "第一句", "[MSG]", "第二句\\n带换行", "[DONE]",
                    ], payloads
            finally:
                engine_module.ConversationEngine = original  # type: ignore[misc]
                mongo._db = None

        asyncio.run(flow())

    def test_error_frame_keeps_internals_out(self) -> None:
        """生成中断时只给稳定话术：异常原文可能带连接串、主机与集合名。"""
        import app.conversation.engine as engine_module

        secret = "mongodb://root:Pa55w0rd@mongo-internal:27017/aiyichat"

        class _BoomEngine:
            def __init__(self, bot_id: str = "") -> None:
                self.bot_id = bot_id

            async def handle_message_stream(self, **kwargs: Any) -> AsyncIterator[str]:
                yield "前半句"
                raise RuntimeError(f"连接失败: {secret}")

        async def flow() -> None:
            app = await _setup_app("t_stream_error")
            original = engine_module.ConversationEngine
            engine_module.ConversationEngine = _BoomEngine  # type: ignore[misc]
            try:
                async with _client(app) as client:
                    await _register(client, "boom")
                    await _approve("boom")
                    token = await _login(client, "boom")
                    resp = await client.post(
                        "/api/conversations/chat/stream",
                        json={"text": "hi"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    body = resp.text
                    assert "前半句" in body, body
                    assert "[ERROR]" in body, body
                    assert secret not in body, "异常原文带着内部连接串回显给了客户端"
                    assert "mongo-internal" not in body and "RuntimeError" not in body, body
            finally:
                engine_module.ConversationEngine = original  # type: ignore[misc]
                mongo._db = None

        asyncio.run(flow())
