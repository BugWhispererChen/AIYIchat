"""自定义角色创建向导冒烟测试（mongomock + ASGI 内存客户端）。

覆盖真实路由与模板渲染：
- LLM 生成角色信息（mock Provider）→ 草稿入库且不出现在角色列表
- 向导各步骤页面渲染与草稿守卫（非草稿 slug → 404）
- 定妆照上传 → 预置音色试听/选择 → 确认可见性转正全流程
- 私有角色未选账号时阻断、取消创建删除草稿
"""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.db import mongo
from app.models.profile import PromptProfileCreate

# LLM 生成的角色档案 JSON（mock 响应）
_LLM_PROFILE_JSON = json.dumps({
    "persona": {
        "background": "24岁的咖啡店店长，温柔细腻。",
        "era": "现代",
        "age_appearance": "24岁左右的都市女性",
        "appearance": "a young woman in her mid-20s with long black hair",
        "core_values": ["真诚", "温暖"],
        "relationships": [],
    },
    "language_style": {
        "vocabulary": ["哎呀", "没事的"],
        "sentence_patterns": ["喜欢用语气词"],
        "tone": "温柔亲切",
        "emoji_habits": ["😊"],
        "forbidden_words": [],
        "example_utterances": ["今天过得怎么样呀？"],
    },
    "personality": {
        "mbti_hint": "ENFJ",
        "traits": {"warmth": 0.9, "humor": 0.6},
        "care_style": "耐心倾听后给出安慰",
    },
    "dialogue_strategy": {
        "greeting": "嗨，你来啦。",
        "on_negative_emotion": "先共情再安慰",
        "on_positive_emotion": "一起开心",
        "on_silence": "轻声问候",
        "boundaries": ["不探听隐私"],
        "safety_guardrails": "保持友善",
    },
    "meta_instructions": "扮演温柔的咖啡店店长",
}, ensure_ascii=False)


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


def _fake_llm_provider() -> SimpleNamespace:
    """返回 mock LLM Provider：complete 固定返回角色档案 JSON。"""
    return SimpleNamespace(complete=AsyncMock(return_value=_LLM_PROFILE_JSON))


async def _setup_app():
    """内存数据库 + 免登录的 ASGI 应用。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_profile_create"]

    from app.admin.auth import require_admin
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: "admin"
    return app


async def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_draft_via_wizard(client: AsyncClient) -> str:
    """走 Step 1 生成草稿并返回 slug（mock LLM）。"""
    with patch(
        "app.profiles.creator.get_llm_provider",
        return_value=_fake_llm_provider(),
    ):
        resp = await client.post(
            "/admin/profiles/create/generate",
            data={"name": "林小满", "description": "温柔的咖啡店店长"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/admin/profiles/create/" in location and location.endswith("/image")
    # slug 位于 /admin/profiles/create/{slug}/image
    return location.split("/")[-2]


class TestProfileCreateWizard:
    """自定义角色创建向导端到端冒烟。"""

    def test_full_wizard_flow(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                from app.db.repositories.profile_repo import ProfileRepository

                repo = ProfileRepository()
                async with _client(app) as client:
                    # Step 1 页面渲染
                    resp = await client.get("/admin/profiles/create")
                    assert resp.status_code == 200
                    assert "角色名称" in resp.text

                    # Step 1 提交 → LLM 生成草稿
                    slug = await _create_draft_via_wizard(client)
                    draft = await repo.find_by_slug(slug)
                    assert draft is not None
                    assert draft.is_draft is True and draft.is_active is False
                    assert draft.name == "林小满"
                    assert draft.source == "自定义创建"
                    assert draft.persona.appearance  # 英文外貌描述

                    # 草稿不出现在角色列表与搜索
                    assert all(p.slug != slug for p in await repo.list_all())
                    assert all(
                        p.slug != slug for p in await repo.search("林小满")
                    )
                    # 角色管理页不显示草稿
                    resp = await client.get("/admin/profiles")
                    assert slug not in resp.text

                    # Step 2：上传定妆照 → 跳转声音步骤
                    resp = await client.post(
                        f"/admin/profiles/create/{slug}/image/upload",
                        files={"file": ("p.png", _png_bytes(), "image/png")},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303
                    assert resp.headers["location"].endswith("/voice")
                    draft = await repo.find_by_slug(slug)
                    assert draft is not None and draft.reference_image_url

                    # Step 3：页面渲染预置音色清单
                    resp = await client.get(f"/admin/profiles/create/{slug}/voice")
                    assert resp.status_code == 200
                    assert "龙安灵心" in resp.text

                    # 选择非法预置音色 → 阻断
                    resp = await client.post(
                        f"/admin/profiles/create/{slug}/voice/select",
                        data={"voice_id": "not-a-voice"},
                    )
                    assert resp.status_code == 200 and "无效" in resp.text

                    # 预置音色试听（mock TTS 合成）
                    with patch(
                        "app.services.tts_service.TTSService.synthesize",
                        new=AsyncMock(return_value=b"fake-audio"),
                    ):
                        resp = await client.post(
                            f"/admin/profiles/create/{slug}/voice/preset-preview",
                            data={"voice_id": "longanlingxin_v3.1"},
                        )
                    assert resp.status_code == 200 and "试听音频已生成" in resp.text

                    # 选定预置音色
                    resp = await client.post(
                        f"/admin/profiles/create/{slug}/voice/select",
                        data={"voice_id": "longanlingxin_v3.1"},
                    )
                    assert resp.status_code == 200 and "已选定" in resp.text
                    draft = await repo.find_by_slug(slug)
                    assert draft is not None and draft.voice_id == "longanlingxin_v3.1"

                    # Step 4：确认页渲染
                    resp = await client.get(f"/admin/profiles/create/{slug}/confirm")
                    assert resp.status_code == 200
                    assert "可见性" in resp.text

                    # 确认转正（公共）
                    resp = await client.post(
                        f"/admin/profiles/create/{slug}/confirm",
                        data={"visibility": "public"},
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303
                    assert resp.headers["location"] == f"/admin/profiles/{slug}"

                    final = await repo.find_by_slug(slug)
                    assert final is not None
                    assert final.is_draft is False and final.is_active is True
                    assert final.visibility == "public"
                    # 转正后出现在角色列表
                    assert any(p.slug == slug for p in await repo.list_all())
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_private_requires_account(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    slug = await _create_draft_via_wizard(client)
                    # 私有但未选账号 → 阻断并提示
                    resp = await client.post(
                        f"/admin/profiles/create/{slug}/confirm",
                        data={"visibility": "private"},
                    )
                    assert resp.status_code == 200
                    assert "必须选择所属账号" in resp.text

                    from app.db.repositories.profile_repo import ProfileRepository

                    draft = await ProfileRepository().find_by_slug(slug)
                    assert draft is not None and draft.is_draft is True
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_cancel_deletes_draft(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                from app.db.repositories.profile_repo import ProfileRepository

                async with _client(app) as client:
                    slug = await _create_draft_via_wizard(client)
                    resp = await client.post(
                        f"/admin/profiles/create/{slug}/cancel",
                        follow_redirects=False,
                    )
                    assert resp.status_code == 303
                    assert resp.headers["location"] == "/admin/profiles"
                    draft = await ProfileRepository().find_by_slug(slug)
                    # 软删除：草稿保留但已禁用，且不在列表
                    assert draft is not None and draft.is_active is False
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_wizard_rejects_non_draft_slug(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                from app.db.repositories.profile_repo import ProfileRepository

                # 已转正角色不允许进入向导
                await ProfileRepository().create(
                    PromptProfileCreate(slug="regular", name="普通角色")
                )
                async with _client(app) as client:
                    for path in ("image", "voice", "confirm"):
                        resp = await client.get(
                            f"/admin/profiles/create/regular/{path}"
                        )
                        assert resp.status_code == 404
                    resp = await client.post("/admin/profiles/create/regular/cancel")
                    assert resp.status_code == 404
                    # 不存在的 slug 同样 404
                    resp = await client.get("/admin/profiles/create/none/image")
                    assert resp.status_code == 404
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_generate_requires_name_and_description(self) -> None:
        async def _flow() -> None:
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    resp = await client.post(
                        "/admin/profiles/create/generate",
                        data={"name": "", "description": ""},
                    )
                    assert resp.status_code == 200
                    assert "必填" in resp.text
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())
