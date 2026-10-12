"""定妆照后台链路冒烟测试（mongomock + ASGI 内存客户端）。

覆盖真实路由与模板渲染：
- 角色详情页渲染定妆照区块与外貌特征输入
- AI 模型配置页渲染「参考图编辑」功能块
- 上传定妆照 → 媒体落库 → /api/media 可访问 → 清除
- 一键生成候选（mock 文生图）→ 确认设为定妆照
- 编辑保存后 identity.appearance 不丢失（回归防护）
"""

import asyncio
import io
from unittest.mock import AsyncMock, patch

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.db import mongo
from app.models.profile import ProfileIdentity, PromptProfileCreate

SLUG = "test-ref-portrait"


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


async def _setup_app():
    """内存数据库 + 免登录的 ASGI 应用与种子角色。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_admin_ref"]

    from app.db.repositories.profile_repo import ProfileRepository

    await ProfileRepository().create(PromptProfileCreate(
        slug=SLUG, name="测试角色",
        identity=ProfileIdentity(appearance="a young woman"),
    ))

    from app.admin.auth import require_admin
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: "admin"
    return app


async def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


async def _get_profile():
    from app.db.repositories.profile_repo import ProfileRepository

    return await ProfileRepository().find_by_slug(SLUG)


class TestAdminReferenceImage:
    """定妆照管理后台端到端冒烟。"""

    def test_pages_render_and_upload_clear_flow(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    # 详情页渲染定妆照区块与外貌特征输入
                    resp = await client.get(f"/admin/profiles/{SLUG}")
                    assert resp.status_code == 200
                    assert "定妆照" in resp.text
                    assert "identity_appearance" in resp.text

                    # AI 模型配置页渲染参考图编辑功能块
                    resp = await client.get("/admin/multimedia")
                    assert resp.status_code == 200
                    assert "参考图编辑" in resp.text
                    assert "imagegen_edit_config_id" in resp.text

                    # 上传定妆照
                    resp = await client.post(
                        f"/admin/profiles/{SLUG}/reference-image/upload",
                        files={"file": ("ref.png", _png_bytes(), "image/png")},
                    )
                    assert resp.status_code == 200
                    assert "定妆照已更新" in resp.text
                    profile = await _get_profile()
                    assert profile.reference_image_url.startswith("/api/media/")

                    # 媒体接口可读取
                    resp = await client.get(profile.reference_image_url)
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("image/")

                    # 清除定妆照
                    resp = await client.post(
                        f"/admin/profiles/{SLUG}/reference-image/clear"
                    )
                    assert resp.status_code == 200
                    profile = await _get_profile()
                    assert profile.reference_image_url == ""
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_generate_candidate_and_confirm(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    from app.services.imagegen_service import ImageGenService

                    with patch.object(
                        ImageGenService, "generate",
                        AsyncMock(return_value=_png_bytes()),
                    ) as mock_gen:
                        resp = await client.post(
                            f"/admin/profiles/{SLUG}/reference-image/generate"
                        )
                    assert resp.status_code == 200
                    assert "候选定妆照已生成" in resp.text
                    # 生成 prompt 使用外貌描述 + 正面肖像模板
                    prompt = mock_gen.await_args[0][0]
                    assert "a young woman" in prompt
                    assert "portrait" in prompt

                    # 从页面中取出候选 media_url 并确认
                    marker = 'name="media_url" value="'
                    start = resp.text.index(marker) + len(marker)
                    media_url = resp.text[start:resp.text.index('"', start)]
                    assert media_url.startswith("/api/media/")

                    resp = await client.post(
                        f"/admin/profiles/{SLUG}/reference-image/confirm",
                        data={"media_url": media_url},
                    )
                    assert resp.status_code == 200
                    profile = await _get_profile()
                    assert profile.reference_image_url == media_url
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_edit_preserves_appearance(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    resp = await client.post(
                        f"/admin/profiles/{SLUG}/edit",
                        data={
                            "name": "测试角色",
                            "identity_appearance": "a young woman",
                        },
                    )
                    assert resp.status_code == 200
                    profile = await _get_profile()
                    assert profile.identity.appearance == "a young woman"
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())
