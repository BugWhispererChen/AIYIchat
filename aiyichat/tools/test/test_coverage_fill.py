"""补齐覆盖率的收尾批测试。

覆盖：
- admin/auth：会话创建/校验/销毁/require_admin 各分支
- prompt_assembler：imagegen 注入块、记忆段、历史过滤、示例选取
- imagegen_service：dashscope/gemini 编辑、定妆照读取、纯文生图
- emotion_detector：补充分支
"""

import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor

from app.config import settings
from app.db import mongo


def _run(coro):
    return asyncio.run(coro)


class TestAdminAuth:
    """后台鉴权。"""

    def test_session_lifecycle(self) -> None:
        async def body() -> None:
            from app.admin import auth

            old_pw = settings.admin_password
            settings.admin_password = "pw"
            try:
                resp = SimpleNamespace(cookies={}, set_cookie=lambda **kw: None,
                                       delete_cookie=lambda **kw: None)
                token = auth.create_session(resp)  # type: ignore[arg-type]
                assert token in auth._sessions

                # 校验通过
                assert await auth.verify_credentials("admin", "pw") is True
                assert await auth.verify_credentials("admin", "wrong") is False

                # 销毁
                auth.destroy_session(resp, token)  # type: ignore[arg-type]
                assert token not in auth._sessions
            finally:
                settings.admin_password = old_pw

        _run(body())

    def test_verify_no_password(self) -> None:
        async def body() -> None:
            from app.admin import auth

            old = settings.admin_password
            settings.admin_password = ""
            try:
                assert await auth.verify_credentials("admin", "") is False
            finally:
                settings.admin_password = old

        _run(body())

    def test_require_admin(self) -> None:
        async def body():
            from fastapi import HTTPException

            from app.admin import auth

            old = settings.admin_password
            settings.admin_password = "pw"
            try:
                req = SimpleNamespace()
                # 无 session → 401
                try:
                    await auth.require_admin(req, admin_session="")
                    raise AssertionError("应 401")
                except HTTPException as e:
                    assert e.status_code == 401

                # 有效 session
                auth._sessions["tok-1"] = time.time() + 3600
                assert await auth.require_admin(req, admin_session="tok-1") == "tok-1"

                # 过期 session → 401
                auth._sessions["tok-2"] = time.time() - 10
                try:
                    await auth.require_admin(req, admin_session="tok-2")
                    raise AssertionError("应 401")
                except HTTPException:
                    pass

                # 未配置密码 → 503
                settings.admin_password = ""
                try:
                    await auth.require_admin(req, admin_session="tok-1")
                    raise AssertionError("应 503")
                except HTTPException as e:
                    assert e.status_code == 503
            finally:
                settings.admin_password = old

        _run(body())

    def test_cleanup_expired(self) -> None:
        from app.admin import auth

        auth._sessions["expired"] = time.time() - 100
        auth._cleanup_expired()
        assert "expired" not in auth._sessions

    def test_hash_password_roundtrip(self) -> None:
        """哈希生成/验算：格式自检 + 正误密码 + 坏格式兜底。"""
        from app.admin import auth

        hashed = auth.hash_password("my-secret")
        assert hashed.startswith("pbkdf2$")
        parts = hashed.split("$")
        assert len(parts) == 4 and parts[1] == "600000"

        assert auth._verify_hash("my-secret", hashed) is True
        assert auth._verify_hash("wrong", hashed) is False
        # 坏格式不抛异常
        assert auth._verify_hash("x", "not-a-hash") is False
        assert auth._verify_hash("x", "pbkdf2$bad$bad$bad") is False

    def test_verify_hashed_password(self) -> None:
        """存储为哈希时：verify_credentials 走验算分支且不触发迁移。"""
        async def body() -> None:
            from app.admin import auth

            old = settings.admin_password
            settings.admin_password = auth.hash_password("hashed-pw")
            try:
                assert await auth.verify_credentials("admin", "hashed-pw") is True
                assert await auth.verify_credentials("admin", "nope") is False
                # 登录名不匹配直接拒绝
                assert await auth.verify_credentials("root", "hashed-pw") is False
                # 哈希存储不会被迁移改写
                assert settings.admin_password.startswith("pbkdf2$")
            finally:
                settings.admin_password = old

        _run(body())

    def test_plaintext_auto_migration(self) -> None:
        """明文登录成功后自动迁移为哈希（写 DB + 就地更新 settings）。"""
        async def body() -> None:
            from app.admin import auth
            from app.db.repositories.app_settings_repo import AppSettingsRepository

            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_pw_migrate"]
            old = settings.admin_password
            settings.admin_password = "plain-pw"
            try:
                assert await auth.verify_credentials("admin", "plain-pw") is True
                # 已就地更新为哈希
                assert settings.admin_password.startswith("pbkdf2$")
                # DB 中也是哈希
                doc = await AppSettingsRepository().get_document()
                assert doc is not None and doc.admin_password.startswith("pbkdf2$")
                # 迁移后仍可用原密码登录（验算分支）
                assert await auth.verify_credentials("admin", "plain-pw") is True
            finally:
                settings.admin_password = old
                mongo._db = None

        _run(body())


class TestPromptAssemblerImageGen:
    """Prompt 组装的图片能力注入与历史过滤。"""

    def _profile(self, ref_url="", appearance=""):
        from app.models.profile import (
            ProfileDialogueStrategy,
            ProfilePersona,
            ProfileLanguageStyle,
            ProfilePersonality,
            PromptProfileInDB,
        )

        return PromptProfileInDB(
            _id="000000000000000000000002",
            slug="img", name="小画",
            source="测试",
            reference_image_url=ref_url,
            persona=ProfilePersona(
                background="生活在现代都市", era="现代",
                appearance=appearance, core_values=["真诚"],
            ),
            language_style=ProfileLanguageStyle(
                tone="活泼", emoji_habits=["😊"],
                example_utterances=["今天天气真好", "我们聊聊音乐吧", "最近在忙什么"],
            ),
            personality=ProfilePersonality(traits={"warmth": 0.8}, care_style="体贴"),
            dialogue_strategy=ProfileDialogueStrategy(greeting="你好"),
            meta_instructions="额外指令",
        )

    def test_assemble_with_imagegen_ref(self) -> None:
        from app.llm.prompt_assembler import PromptAssembler

        old_img = settings.imagegen_enabled
        old_edit = settings.imagegen_edit_enabled
        settings.imagegen_enabled = True
        settings.imagegen_edit_enabled = True
        try:
            asm = PromptAssembler()
            msgs = asm.assemble(
                profile=self._profile(ref_url="/api/media/1"),
                history=[],
                identity_input="给我拍张照",
                emotion_hint="开心",
                current_topic="摄影",
                memory_facts=["喜欢旅行", "养猫"],
                length_instruction="简短回复",
                strategy_instruction="多倾听",
                topic_context="之前聊过音乐",
            )
            joined = "\n".join(m["content"] for m in msgs)
            assert "图片回复能力" in joined
            assert "标准形象保持一致" in joined  # has_ref 分支
            assert "当前话题" in joined and "喜欢旅行" in joined
        finally:
            settings.imagegen_enabled = old_img
            settings.imagegen_edit_enabled = old_edit

    def test_assemble_imagegen_appearance_fallback(self) -> None:
        from app.llm.prompt_assembler import PromptAssembler

        old_img = settings.imagegen_enabled
        old_edit = settings.imagegen_edit_enabled
        settings.imagegen_enabled = True
        settings.imagegen_edit_enabled = False
        try:
            asm = PromptAssembler()
            msgs = asm.assemble(
                profile=self._profile(appearance="a young woman with long hair"),
                history=[],
                identity_input="自拍一张",
            )
            joined = "\n".join(m["content"] for m in msgs)
            assert "外貌特征" in joined  # appearance fallback 分支
        finally:
            settings.imagegen_enabled = old_img
            settings.imagegen_edit_enabled = old_edit

    def test_filter_history_and_keywords(self) -> None:
        from app.llm.prompt_assembler import PromptAssembler
        from app.models.message import MessageInDB

        asm = PromptAssembler()
        # 关键词提取（按标点/空格分词）
        kws = asm._extract_keywords("旅行，摄影，音乐")
        assert "旅行" in kws and "摄影" in kws
        # 示例选取
        examples = ["聊聊旅行吧", "今天吃了火锅", "摄影是我的爱好"]
        selected = asm._select_examples("我想聊旅行", examples, k=2)
        assert len(selected) == 2
        # 历史过滤（>5 条）
        history = [
            MessageInDB(_id=str(i), conversation_id="c", role="user",
                        content=f"消息{i}关于旅行？" if i % 2 else f"闲聊{i}")
            for i in range(12)
        ]
        filtered = asm._filter_history(history, "旅行计划")
        assert len(filtered) <= 15 and len(filtered) >= 5


class TestImageGenEditBranches:
    """imagegen 编辑与生成分支。"""

    def _cfg(self, model, base=""):
        from app.llm.model_registry import ResolvedModelConfig

        return ResolvedModelConfig(base_url=base, api_key="k", model=model, timeout=30)

    def test_load_reference_from_media(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_im_ref"]
            try:
                from app.db.repositories.media_repo import MediaRepository
                from app.services.imagegen_service import ImageGenService

                mid = await MediaRepository().create(b"refbytes", "image/png")
                out = await ImageGenService._load_reference_bytes(f"/api/media/{mid}")
                assert out == b"refbytes"
                # 不存在
                try:
                    await ImageGenService._load_reference_bytes("/api/media/" + "0" * 24)
                    raise AssertionError("应抛 RuntimeError")
                except RuntimeError:
                    pass
            finally:
                mongo._db = None

        _run(body())

    def test_generate_plain(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            svc = ImageGenService()
            svc._download = AsyncMock(return_value=b"img-bytes")
            with patch(
                "app.services.imagegen_service.resolve_model_config",
                return_value=self._cfg("wanx-v1"),
            ), patch(
                "app.services.imagegen_service.asyncio.to_thread",
                new=AsyncMock(return_value="http://img-url"),
            ):
                out = await svc.generate("a cat")
                assert out == b"img-bytes"

        _run(body())

    def test_generate_with_reference_gemini(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            svc = ImageGenService()
            svc._load_reference_bytes = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")

            b64img = base64.b64encode(b"gen-image").decode()
            fake_resp = SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"candidates": [{
                    "content": {"parts": [{"inlineData": {"data": b64img}}]}
                }]},
            )

            class FakeClient:
                def __init__(self, *a, **k):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return None

                async def post(self, *a, **k):
                    return fake_resp

            with patch(
                "app.services.imagegen_service.resolve_model_config",
                return_value=self._cfg("gemini-2.0-flash", "https://generativelanguage.googleapis.com/v1beta"),
            ), patch(
                "app.services.imagegen_service.httpx.AsyncClient", FakeClient
            ):
                out = await svc.generate_with_reference("new scene", "/api/media/1")
                assert out == b"gen-image"

        _run(body())

    def test_generate_with_reference_dashscope(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            svc = ImageGenService()
            svc._load_reference_bytes = AsyncMock(return_value=b"\xff\xd8jpeg")
            svc._download = AsyncMock(return_value=b"edited-image")

            with patch(
                "app.services.imagegen_service.resolve_model_config",
                return_value=self._cfg("qwen-image-edit"),
            ), patch(
                "app.services.imagegen_service.asyncio.to_thread",
                new=AsyncMock(return_value="http://edited-url"),
            ):
                out = await svc.generate_with_reference("new scene", "/api/media/1")
                assert out == b"edited-image"

        _run(body())


class TestEmotionDetectorMore:
    """情绪检测补充分支。"""

    def test_intensity_hints_all_emotions(self) -> None:
        from app.conversation.emotion_detector import _get_intensity_hint

        for emo in ("sad", "angry", "anxious", "lonely"):
            assert _get_intensity_hint(emo, 0.9)
            assert _get_intensity_hint(emo, 0.6)
            assert _get_intensity_hint(emo, 0.2)
        assert _get_intensity_hint("happy", 0.5) == "开心愉悦"
        assert _get_intensity_hint("unknown", 0.5) == ""

    def test_detect_various(self) -> None:
        from app.conversation.emotion_detector import (
            detect_emotion,
            detect_emotion_with_history,
        )

        assert detect_emotion("好焦虑睡不着") == "anxious"
        assert detect_emotion("感觉好孤独一个人") == "lonely"
        assert detect_emotion("气死我了太过分") == "angry"
        # 无有效历史
        r = detect_emotion_with_history("好难过", [None, None])
        assert r is not None
