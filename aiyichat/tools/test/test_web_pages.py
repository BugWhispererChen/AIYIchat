"""C 端网页路由渲染测试（mongomock + ASGI 内存客户端，不启浏览器）。

只断言渲染出的 HTML：
- 关闭 `web_auth_enabled` 时各张页面一律 403，且提示页不含任何账号数据、根路径维持原 404；
- 开启时各张页面 200，并含各自约定的关键结构与共享设计令牌（证明 admin partial 复用生效）。
"""

import asyncio
from typing import Any

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.profile import PromptProfileCreate
from app.services import auth_service

WEB_BOT_ID = "web-bot-pages"
PROFILE_SLUG = "lin-yu-page"
PASSWORD = "passw0rd9"
LEAK_USERNAME = "leakcheck"

PAGE_PATHS = ("/web/register", "/web/login", "/web/chat", "/web/account", "/web/services", "/web/profiles")

_WEB_KEYS = (
    "web_auth_enabled", "web_register_enabled", "web_register_require_review",
    "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
    "web_free_quota_template_id",
)


@pytest.fixture(autouse=True)
def web_flags():
    """默认开启网页注册与验证码（页面渲染的真实形态），用例后复原全局配置。"""
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = True
    settings.web_bot_id = WEB_BOT_ID
    settings.web_default_profile_slug = PROFILE_SLUG
    settings.web_free_quota_template_id = ""
    yield
    for key, value in saved.items():
        setattr(settings, key, value)


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
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestWebPagesDisabled:
    """总开关关闭：网页功能整体不可见，且老实例行为零变化。"""

    def test_pages_forbidden_and_data_free(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_web_pages_off")
            try:
                # 先用开启态建一个真实账号（走服务层，绕开验证码），再关旗：
                # 提示页绝不允许带出任何账号信息
                await auth_service.register(
                    username=LEAK_USERNAME,
                    password=PASSWORD,
                    terms_accepted=True,
                    privacy_accepted=True,
                    age_confirmed=True,
                    ip="203.0.113.9",
                )
                settings.web_auth_enabled = False
                async with _client(app) as client:
                    for path in PAGE_PATHS:
                        page = await client.get(path)
                        assert page.status_code == 403, path
                        assert "网页身份功能未开放" in page.text
                        assert LEAK_USERNAME not in page.text
                        assert PASSWORD not in page.text
                    # 根路径不重定向：保持 FastAPI 原有 404，存量部署升级无感知
                    assert (await client.get("/")).status_code == 404
            finally:
                mongo._db = None

        asyncio.run(flow())


class TestWebPagesRender:
    """总开关开启：三张页面的关键结构与共享样式令牌齐备。"""

    def test_register_page_landmarks(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_web_pages_register")
            try:
                async with _client(app) as client:
                    resp = await client.get("/web/register")
                    assert resp.status_code == 200
                    html = resp.text
                    # 三个必勾选项与字段名同 API 契约一致
                    assert 'name="terms_accepted"' in html
                    assert 'name="privacy_accepted"' in html
                    assert 'name="age_confirmed"' in html
                    assert "我已阅读并同意《身份协议》" in html
                    assert "我已阅读并同意《隐私政策》" in html
                    assert "我已满 14 周岁" in html
                    # 未成年人保护与部署者责任声明
                    assert "未满 14 周岁不得注册" in html
                    assert "备案" in html
                    assert "/web/login" in html
                    assert "Design Tokens" in html  # admin 的设计令牌 partial 复用生效
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_login_page_landmarks(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_web_pages_login")
            try:
                async with _client(app) as client:
                    resp = await client.get("/web/login")
                    assert resp.status_code == 200
                    html = resp.text
                    assert 'id="captcha-img"' in html
                    assert 'id="captcha-answer"' in html
                    assert "/web/register" in html
                    assert "aiyichat_web_token" in html  # 与聊天页约定的令牌键
                    assert "Design Tokens" in html
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_chat_page_landmarks(self) -> None:
        async def flow() -> None:
            app = await _setup_app("t_web_pages_chat")
            try:
                async with _client(app) as client:
                    resp = await client.get("/web/chat")
                    assert resp.status_code == 200
                    html = resp.text
                    assert 'id="transcript"' in html
                    assert "aiyichat_web_token" in html
                    assert "/api/conversations/chat/stream" in html
                    assert "退出登录" in html
                    assert "Design Tokens" in html
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_services_page_landmarks_and_profiles_redirect(self) -> None:
        """聊天服务页三区齐备；旧角色页 307 落到角色分区。"""

        async def flow() -> None:
            app = await _setup_app("t_web_pages_services")
            try:
                async with _client(app) as client:
                    resp = await client.get("/web/services")
                    assert resp.status_code == 200
                    html = resp.text
                    for marker in (
                        "svc-tabs", "channel-list", "service-list", "role-list",
                        "bot-editor", "service-editor", "web-services.js",
                        "web-roles.js", "聊天服务",
                    ):
                        assert marker in html, marker
                    assert "Design Tokens" in html

                    redirect = await client.get("/web/profiles", follow_redirects=False)
                    assert redirect.status_code == 307
                    assert redirect.headers["location"] == "/web/services#roles"
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_account_page_landmarks(self) -> None:
        """账号中心：五个分区齐备，顶栏入口与聊天页一致。"""

        async def flow() -> None:
            app = await _setup_app("t_web_pages_account")
            try:
                async with _client(app) as client:
                    resp = await client.get("/web/account")
                    assert resp.status_code == 200
                    html = resp.text
                    for anchor in ("account-profile", "account-security", "account-quota",
                                   "account-data", "account-danger"):
                        assert f'id="{anchor}"' in html, anchor
                    assert "/api/web/account" not in html  # 数据一律由前端带令牌去取
                    assert 'id="account-delete"' in html
                    assert 'href="/web/account"' in html  # 顶栏入口
                    assert "Design Tokens" in html

                    chat = await client.get("/web/chat")
                    assert 'href="/web/account"' in chat.text

                    login = await client.get("/web/login")
                    assert "联系管理员重置" in login.text
            finally:
                mongo._db = None

        asyncio.run(flow())

    def test_root_redirects_to_login(self) -> None:
        """开启后根路径引导到登录页（303：浏览器改用最干净的 GET 跳转）。"""

        async def flow() -> None:
            app = await _setup_app("t_web_pages_root")
            try:
                async with _client(app) as client:
                    resp = await client.get("/")
                    assert resp.status_code == 303
                    assert resp.headers["location"] == "/web/login"
            finally:
                mongo._db = None

        asyncio.run(flow())
