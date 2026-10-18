"""人物一致性图片生成链路单元测试。

覆盖：
- engine._build_image_tasks：定妆照携带与外貌降级描述组装
- engine.handle_message：[IMG:self:] 标签→图片额度扣减→结构化 ImageTask
- imagegen_service 提供商路由（dashscope / openai / gemini）
- generate_for_task 降级链：参考图编辑失败/未启用时回退纯文生图
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
from app.conversation.engine import ConversationEngine
from app.conversation.image_task import ImageTask
from app.services.imagegen_service import (
    ImageGenService,
    _detect_image_mime,
    _detect_provider,
)
from tools.test.test_engine_handle_message import _build_engine, _make_profile as _make_engine_profile, _run


def _make_profile(ref_url: str = "", appearance: str = ""):
    return SimpleNamespace(
        reference_image_url=ref_url,
        persona=SimpleNamespace(appearance=appearance),
    )


class TestBuildImageTasks:
    """_build_image_tasks 组装逻辑。"""

    def test_self_with_reference(self) -> None:
        """本人出镜且有定妆照：携带 ref，降级描述补充外貌。"""
        profile = _make_profile(
            ref_url="/api/media/abc", appearance="a young woman"
        )
        tasks = ConversationEngine._build_image_tasks(
            [("realistic photo, selfie in a cafe", True)], profile
        )
        assert tasks == [ImageTask(
            prompt="realistic photo, selfie in a cafe",
            contains_self=True,
            ref_image_url="/api/media/abc",
            fallback_prompt="a young woman, realistic photo, selfie in a cafe",
        )]

    def test_self_without_reference(self) -> None:
        """本人出镜但无定妆照：ref 为空，仍预置外貌降级描述。"""
        profile = _make_profile(appearance="a young woman")
        tasks = ConversationEngine._build_image_tasks(
            [("realistic photo, selfie", True)], profile
        )
        assert tasks[0].ref_image_url == ""
        assert tasks[0].fallback_prompt.startswith("a young woman")

    def test_not_self_no_reference_attached(self) -> None:
        """非本人出镜：不携带定妆照，降级描述等于原描述。"""
        profile = _make_profile(
            ref_url="/api/media/abc", appearance="a young woman"
        )
        tasks = ConversationEngine._build_image_tasks(
            [("realistic photo, sunset over the sea", False)], profile
        )
        assert tasks[0].ref_image_url == ""
        assert tasks[0].fallback_prompt == "realistic photo, sunset over the sea"

    def test_appearance_not_duplicated(self) -> None:
        """描述已含外貌时不重复拼接。"""
        profile = _make_profile(appearance="a young woman")
        tasks = ConversationEngine._build_image_tasks(
            [("realistic photo, A YOUNG WOMAN taking a selfie", True)], profile
        )
        assert tasks[0].fallback_prompt == (
            "realistic photo, A YOUNG WOMAN taking a selfie"
        )


class TestEngineImageFlow:
    """handle_message 图片链路：标签提取、额度扣减、ImageTask 组装。"""

    def _profile(self, ref_url: str = ""):
        return _make_engine_profile(
            reference_image_url=ref_url,
            persona=SimpleNamespace(appearance="a young woman"),
        )

    def test_self_tag_deducts_quota_and_builds_task(self) -> None:
        """[IMG:self:] 回复：扣减图片额度，产出携带定妆照的 ImageTask。"""
        engine = _build_engine(profile=self._profile("/api/media/ref1"))
        result, _ = _run(
            engine, "发张自拍看看",
            reply="给你拍一张[IMG:self:realistic photo, selfie at home]",
        )

        # 图片额度扣减：第二次 deduct 为 image 方向
        deduct_calls = [c[0] for c in engine.quota_repo.deduct.await_args_list]
        assert ("acc-1", "image", 1) in deduct_calls

        assert result.image_tasks == [ImageTask(
            prompt="realistic photo, selfie at home",
            contains_self=True,
            ref_image_url="/api/media/ref1",
            fallback_prompt="a young woman, realistic photo, selfie at home",
        )]
        assert "[IMG:" not in result.text

    def test_plain_tag_without_reference(self) -> None:
        """普通 [IMG:] 标签：不携带定妆照。"""
        engine = _build_engine(profile=self._profile("/api/media/ref1"))
        result, _ = _run(
            engine, "拍个日落",
            reply="看[IMG:realistic photo, sunset over the sea]",
        )
        task = result.image_tasks[0]
        assert task.contains_self is False
        assert task.ref_image_url == ""

    def test_image_dropped_when_quota_exhausted(self) -> None:
        """图片额度不足：任务被移除，文本回复不受影响。"""
        engine = _build_engine(profile=self._profile())

        # 仅图片方向额度不足，文本方向（身份输入/AI回复）正常
        async def _deduct(account_id, quota_type, amount):
            if quota_type == "image":
                return (False, "图片额度已用尽")
            return (True, "")

        engine.quota_repo.deduct = AsyncMock(side_effect=_deduct)
        result, _ = _run(
            engine, "自拍",
            reply="好的[IMG:self:realistic photo, selfie]",
        )
        assert result.image_tasks == []
        assert result.text == "好的"


class TestProviderRouting:
    """参考图编辑提供商识别。"""

    @staticmethod
    def _cfg(model: str, base_url: str = ""):
        return SimpleNamespace(model=model, base_url=base_url, api_key="k", timeout=30)

    def test_dashscope_by_empty_base(self) -> None:
        assert _detect_provider(self._cfg("qwen-image-edit")) == "dashscope"

    def test_dashscope_by_base_url(self) -> None:
        cfg = self._cfg("wan2.5-i2i-preview", "https://dashscope.aliyuncs.com/api/v1")
        assert _detect_provider(cfg) == "dashscope"

    def test_gemini_by_model(self) -> None:
        cfg = self._cfg("gemini-2.5-flash-image", "https://example.com/v1")
        assert _detect_provider(cfg) == "gemini"

    def test_openai_compatible(self) -> None:
        cfg = self._cfg("gpt-image-1", "https://api.openai.com/v1")
        assert _detect_provider(cfg) == "openai"

    def test_detect_mime(self) -> None:
        assert _detect_image_mime(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
        assert _detect_image_mime(b"\xff\xd8\xff" + b"0" * 8) == "image/jpeg"


class TestGenerateForTaskFallback:
    """generate_for_task 降级链。"""

    def _task(self) -> ImageTask:
        return ImageTask(
            prompt="realistic photo, selfie",
            contains_self=True,
            ref_image_url="/api/media/abc",
            fallback_prompt="a young woman, realistic photo, selfie",
        )

    def test_reference_edit_success(self, monkeypatch) -> None:
        """参考图编辑成功时直接返回结果，不触发降级。"""
        monkeypatch.setattr(settings, "imagegen_edit_enabled", True)
        svc = ImageGenService()
        svc.generate_with_reference = AsyncMock(return_value=b"edited")
        svc.generate = AsyncMock(return_value=b"t2i")

        result = asyncio.run(svc.generate_for_task(self._task()))
        assert result == b"edited"
        svc.generate.assert_not_called()

    def test_fallback_on_edit_failure(self, monkeypatch) -> None:
        """参考图编辑失败时降级为纯文生图（使用 fallback_prompt）。"""
        monkeypatch.setattr(settings, "imagegen_edit_enabled", True)
        svc = ImageGenService()
        svc.generate_with_reference = AsyncMock(
            side_effect=RuntimeError("api error")
        )
        svc.generate = AsyncMock(return_value=b"t2i")

        result = asyncio.run(svc.generate_for_task(self._task()))
        assert result == b"t2i"
        svc.generate.assert_awaited_once_with(
            "a young woman, realistic photo, selfie"
        )

    def test_skip_edit_when_disabled(self, monkeypatch) -> None:
        """参考图编辑未启用时直接走纯文生图。"""
        monkeypatch.setattr(settings, "imagegen_edit_enabled", False)
        svc = ImageGenService()
        svc.generate_with_reference = AsyncMock(return_value=b"edited")
        svc.generate = AsyncMock(return_value=b"t2i")

        result = asyncio.run(svc.generate_for_task(self._task()))
        assert result == b"t2i"
        svc.generate_with_reference.assert_not_called()

    def test_skip_edit_when_not_self(self, monkeypatch) -> None:
        """非本人出镜任务不走参考图编辑。"""
        monkeypatch.setattr(settings, "imagegen_edit_enabled", True)
        svc = ImageGenService()
        svc.generate_with_reference = AsyncMock(return_value=b"edited")
        svc.generate = AsyncMock(return_value=b"t2i")

        task = ImageTask(prompt="realistic photo, sunset", contains_self=False)
        result = asyncio.run(svc.generate_for_task(task))
        assert result == b"t2i"
        svc.generate.assert_awaited_once_with("realistic photo, sunset")
