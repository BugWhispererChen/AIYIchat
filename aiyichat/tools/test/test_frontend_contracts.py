"""前端静态资源契约测试（不起浏览器）。

守住重设计后的两条底线：
1. 界面不再引用任何外部 CDN —— 内网与离线部署下样式必须完整；
2. 模板里 `asset('...')` 指向的每个本地资源都真实存在并能 200 取回，
   避免 vendor 漏入库或 Docker 打包漏目录导致的白屏。
"""

import asyncio
import re
from pathlib import Path

import mongomock_motor
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.utils.assets import STATIC_DIR, asset

APP_DIR = Path(__file__).resolve().parents[2] / "app"
TEMPLATE_ROOTS = (APP_DIR / "admin" / "templates", APP_DIR / "web" / "templates")
ASSET_CALL = re.compile(r"""asset\(\s*['"]([^'"]+)['"]\s*\)""")
CDN_HOSTS = ("jsdelivr", "unpkg", "cdnjs", "cdn.jsdelivr", "unpkg.com")

_WEB_KEYS = ("web_auth_enabled", "web_register_enabled", "web_captcha_enabled")


@pytest.fixture(autouse=True)
def web_flags():
    """开启网页页面，便于断言 C 端渲染出的资源引用。"""
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_captcha_enabled = False
    yield
    for key, value in saved.items():
        setattr(settings, key, value)


def _templates() -> list[Path]:
    """列出全部模板文件。"""
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        files.extend(sorted(root.rglob("*.html")))
    assert files, "未找到任何模板文件，测试路径可能已变更"
    return files


def _asset_refs() -> set[str]:
    """收集模板里所有 asset('...') 引用的相对路径。"""
    refs: set[str] = set()
    for file in _templates():
        refs.update(ASSET_CALL.findall(file.read_text(encoding="utf-8")))
    return refs


class TestNoExternalCdn:
    """界面样式与脚本一律走本地 /static，不留外部依赖。"""

    def test_templates_have_no_cdn_hosts(self) -> None:
        offenders: list[str] = []
        for file in _templates():
            text = file.read_text(encoding="utf-8")
            for host in CDN_HOSTS:
                if host in text:
                    offenders.append(f"{file.name}:{host}")
        assert not offenders, f"模板仍引用外部 CDN：{offenders}"

    def test_disabled_notice_page_stays_self_contained(self) -> None:
        """功能关闭时的 403 提示页必须零外链、零 JS：不得依赖 CSS 才可读。"""
        from app.web.router import _DISABLED_HTML

        assert "http://" not in _DISABLED_HTML
        assert "https://" not in _DISABLED_HTML
        assert "<script" not in _DISABLED_HTML
        assert "/static/" not in _DISABLED_HTML


class TestStaticAssetsServe:
    """模板引用的每个本地资源都必须真实可取。"""

    def test_every_asset_reference_resolves_on_disk(self) -> None:
        refs = _asset_refs()
        assert refs, "模板中未出现 asset() 引用，接入可能失效"
        missing = [ref for ref in sorted(refs) if not (STATIC_DIR / ref).is_file()]
        assert not missing, f"以下静态资源缺失：{missing}"

    def test_assets_return_200_over_http(self) -> None:
        async def flow() -> None:
            mongo._db = mongomock_motor.AsyncMongoMockClient()["t_frontend_assets"]
            from app.main import app

            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    for ref in sorted(_asset_refs()):
                        response = await client.get(asset(ref).split("?")[0])
                        assert response.status_code == 200, ref
                        assert len(response.content) > 0, ref
            finally:
                mongo._db = None

        asyncio.run(flow())


class TestRenderedPagesUseLocalAssets:
    """渲染结果里出现本地资源链接，且共享设计令牌标记仍在。"""

    def test_admin_and_web_pages_reference_static_assets(self) -> None:
        async def flow() -> None:
            mongo._db = mongomock_motor.AsyncMongoMockClient()["t_frontend_render"]
            from app.main import app

            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    for path in ("/admin/login", "/web/login"):
                        page = await client.get(path)
                        assert page.status_code == 200, path
                        assert "/static/vendor/pico.min.css" in page.text, path
                        assert "/static/css/tokens.css" in page.text, path
                        assert "Design Tokens" in page.text, path
                        for host in CDN_HOSTS:
                            assert host not in page.text, f"{path} 仍含 {host}"
            finally:
                mongo._db = None

        asyncio.run(flow())


class TestAssetVersioning:
    """版本戳让静态文件更新后浏览器自动弃用旧缓存。"""

    def test_asset_url_carries_version(self) -> None:
        url = asset("css/tokens.css")
        assert url.startswith("/static/css/tokens.css")
        assert "?v=" in url, "静态目录存在文件时应带版本戳"

    def test_asset_keeps_query_escaped_path(self) -> None:
        assert asset("/css/base.css") == asset("css/base.css")
