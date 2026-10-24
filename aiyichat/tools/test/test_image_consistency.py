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

import pytest

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

    @pytest.fixture(autouse=True)
    def _enable_imagegen(self):
        """图片链路的用例必须在"图片生成已开启"的前置下跑。

        `imagegen_enabled` 代码默认是 False，而关闭时 Engine 会直接丢弃图片提示且不扣额度
        （关了还扣额度等于让用户为永远发不出的图付费）。整库跑测时前面用例会把它改回
        False，所以这里逐用例钉住前置条件，不依赖运行顺序。
        """
        old = settings.imagegen_enabled
        settings.imagegen_enabled = True
        yield
        settings.imagegen_enabled = old

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
            # 任务带扣费账号：生成或发送失败时适配器据此退还额度
            account_id="acc-1",
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


class TestImagegenDisabled:
    """图片生成开关关掉时：丢弃图片提示，且不扣图片额度。

    适配器会丢弃图片任务，Engine 先扣额度就等于用户为一张永远发不出的图付费。
    """

    def test_no_quota_deducted_and_no_task(self) -> None:
        old = settings.imagegen_enabled
        settings.imagegen_enabled = False
        try:
            engine = _build_engine(
                profile=_make_engine_profile(
                    reference_image_url="/api/media/ref1",
                    persona=SimpleNamespace(appearance="a young woman"),
                )
            )
            result, _ = _run(
                engine, "发张自拍看看",
                reply="给你拍一张[IMG:realistic photo, selfie at home]",
            )
        finally:
            settings.imagegen_enabled = old
        assert result.image_tasks == []
        deduct_calls = [c[0] for c in engine.quota_repo.deduct.await_args_list]
        assert ("acc-1", "image", 1) not in deduct_calls


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

class TestImageQuotaRefund:
    """图片额度：Engine 预扣，生成或发送失败要退还。"""

    def test_engine_stamps_account_on_tasks(self) -> None:
        """任务要带着扣费账号，否则适配器失败时无从退还。"""
        engine = _build_engine(profile=_make_engine_profile(
            reference_image_url="/api/media/ref1",
            persona=SimpleNamespace(appearance="a young woman"),
        ))
        old = settings.imagegen_enabled
        settings.imagegen_enabled = True
        try:
            result, _ = _run(
                engine, "拍张照",
                reply="[IMG:realistic photo, sunset]",
            )
        finally:
            settings.imagegen_enabled = old
        assert result.image_tasks
        assert all(task.account_id == "acc-1" for task in result.image_tasks)

    def test_qq_image_failure_refunds_quota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        import app.db.repositories.quota_repo as quota_module
        from app.adapters.qq_websocket import QQWebSocketClient
        from app.conversation.image_task import ImageTask

        calls: list[tuple[str, str, int]] = []

        class _Quota:
            def __init__(self) -> None:
                pass

            async def refund(self, account_id: str, content_type: str, amount: int) -> bool:
                calls.append(("refund", content_type, amount))
                return True

        monkeypatch.setattr(quota_module, "QuotaRepository", _Quota)

        adapter = object.__new__(QQWebSocketClient)
        adapter._label = "qq-test"
        adapter._imagegen = SimpleNamespace(
            generate_for_task=lambda task: _raise()
        )

        async def _raise():
            raise RuntimeError("生成失败")

        async def case() -> None:
            await adapter._fire_and_forget_imagegen(
                "user-1", ImageTask(prompt="sunset", account_id="acc-9"),
                "msg-1", False, "",
            )

        asyncio.run(case())
        assert calls == [("refund", "image", 1)], calls

