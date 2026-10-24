"""P2 加固项的契约用例：安全响应头、文档暴露范围、后台模板的上游文案渲染。"""

import asyncio
import re
from pathlib import Path

import mongomock_motor

from app.config import settings
from app.db import mongo

_APP_DIR = Path(__file__).resolve().parents[2] / "app"

EXPECTED_HEADERS = {
    "x-frame-options": "SAMEORIGIN",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def _client():
    """免登录访问后台所需的内存应用（复用 admin 测试的依赖覆盖套路）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def test_security_headers_on_every_response() -> None:
    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_sec_headers"]
        old_env = settings.app_env
        settings.app_env = "production"
        try:
            async with _client() as client:
                for path in ("/admin/login", "/web/login", "/api/health/live"):
                    resp = await client.get(path)
                    for header, value in EXPECTED_HEADERS.items():
                        assert resp.headers.get(header) == value, (path, header)
                    assert "frame-ancestors 'self'" in resp.headers.get(
                        "content-security-policy", ""
                    ), path
                    assert resp.headers.get("strict-transport-security") == "max-age=31536000"
        finally:
            settings.app_env = old_env
            mongo._db = None

    asyncio.run(case())


def test_api_docs_not_public_in_production() -> None:
    """production 下 `/docs` 与 `/openapi.json` 不注册：路由清单本身就是攻击面目录。"""
    from app.bootstrap import create_app

    old_env = settings.app_env
    try:
        settings.app_env = "production"
        app = create_app()
        assert app.docs_url is None
        assert app.openapi_url is None
        settings.app_env = "development"
        dev_app = create_app()
        assert dev_app.docs_url == "/docs"
    finally:
        settings.app_env = old_env


def test_admin_templates_do_not_inject_upstream_text_into_html() -> None:
    """后台模板不许把上游接口的 message 拼进 innerHTML（第三方接口可控的 DOM 注入面）。"""
    offender = re.compile(r"innerHTML\s*=\s*'[^']*'\+\s*\(?(\w+)\.message")
    hits = [
        f"{html.relative_to(_APP_DIR)}: {offender.search(text).group(0)}"
        for html in sorted(_APP_DIR.rglob("*.html"))
        for text in [html.read_text(encoding="utf-8")]
        if offender.search(text)
    ]
    assert not hits, "模板把上游文案拼进了 innerHTML：\n" + "\n".join(hits)

def test_legal_pages_render_with_version() -> None:
    """《身份协议》《隐私政策》必须真能打开，且版本号与凭据里记录的同意版本同源。"""
    from app.models.web_credential import PRIVACY_VERSION, TERMS_VERSION

    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_legal_pages"]
        try:
            async with _client() as client:
                for path, title, version in (
                    ("/web/terms", "身份协议", TERMS_VERSION),
                    ("/web/privacy", "隐私政策", PRIVACY_VERSION),
                ):
                    resp = await client.get(path)
                    assert resp.status_code == 200, path
                    assert title in resp.text, path
                    assert f"版本 {version}" in resp.text, path
                # 隐私政策必须回答"数据存在哪、会发给谁"，否则等于没披露
                privacy = (await client.get("/web/privacy")).text
                for key in ("PBKDF2", "百炼", "备份", "Cookie", "删除"):
                    assert key in privacy, key
        finally:
            mongo._db = None

    asyncio.run(case())

