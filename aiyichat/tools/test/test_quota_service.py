"""额度校验与扣减服务单元测试。

覆盖 QuotaService.check_and_deduct 的核心分支（Repository 全部 mock）：
- 未绑定 Bot → 拒绝
- 文字/语音按估算 token 数扣减，图片按条扣减（与引擎侧 Token 计费语义一致）
- 显式 amount 覆盖默认计算
- 额度不足 → 拒绝并提示充值
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.services.quota_service import QuotaService
from app.utils.token_estimate import estimate_tokens


def _make_service(
    has_binding: bool = True, deduct_ok: bool = True
) -> tuple[QuotaService, MagicMock, MagicMock]:
    """构造注入 mock Repository 的 QuotaService。"""
    binding_repo = MagicMock()
    binding_repo.find_by_user_and_bot = AsyncMock(
        return_value=object() if has_binding else None
    )
    quota_repo = MagicMock()
    quota_repo.deduct = AsyncMock(return_value=(deduct_ok, ""))
    svc = QuotaService(quota_repo=quota_repo, binding_repo=binding_repo)
    return svc, quota_repo, binding_repo


class TestCheckAndDeduct:
    """额度校验与扣减测试集。"""

    def test_no_binding_rejected(self) -> None:
        """用户未绑定 Bot → 拒绝且不扣额度。"""
        svc, quota_repo, _ = _make_service(has_binding=False)
        ok, msg = asyncio.run(
            svc.check_and_deduct("u1", "bot1", "text", "你好")
        )
        assert ok is False
        assert "权限" in msg
        quota_repo.deduct.assert_not_awaited()

    def test_text_deducts_by_estimated_tokens(self) -> None:
        """文字消息按估算 token 数扣减（而非字符数）。"""
        svc, quota_repo, _ = _make_service()
        text = "今天天气不错"
        ok, msg = asyncio.run(
            svc.check_and_deduct("u1", "bot1", "text", text)
        )
        assert ok is True
        assert msg == ""
        expected = estimate_tokens(text)  # 6 个 CJK 字 → 9 token，与字数 6 不同
        assert expected != len(text)
        quota_repo.deduct.assert_awaited_once_with("u1", "text", expected)

    def test_image_deducts_one(self) -> None:
        """图片消息按条计 1。"""
        svc, quota_repo, _ = _make_service()
        ok, _ = asyncio.run(
            svc.check_and_deduct("u1", "bot1", "image", "画一张日落")
        )
        assert ok is True
        quota_repo.deduct.assert_awaited_once_with("u1", "image", 1)

    def test_explicit_amount_overrides(self) -> None:
        """显式指定 amount 时覆盖默认规则。"""
        svc, quota_repo, _ = _make_service()
        asyncio.run(
            svc.check_and_deduct("u1", "bot1", "voice", "很长的语音转写文本", amount=3)
        )
        quota_repo.deduct.assert_awaited_once_with("u1", "voice", 3)

    def test_quota_exhausted_rejected(self) -> None:
        """额度不足 → 拒绝并提示充值。"""
        svc, _, _ = _make_service(deduct_ok=False)
        ok, msg = asyncio.run(
            svc.check_and_deduct("u1", "bot1", "text", "你好")
        )
        assert ok is False
        assert "额度已用完" in msg


class TestEstimateTokens:
    """预检估算器启发式行为（仅用于余额预检，非计费依据）。"""

    def test_cjk_weighted_higher(self) -> None:
        """CJK 字符按 1.5 倍计，高于纯 ASCII。"""
        assert estimate_tokens("你好世界") == 6  # 4 字 ×1.5 向上取整
        assert estimate_tokens("hello world") == 4  # 11 字符 ×0.35 向上取整
        assert estimate_tokens("你好world") == 5  # 2×1.5+5×0.35=4.75→5

    def test_empty_and_min(self) -> None:
        """空文本返回 1，非空最小为 1。"""
        assert estimate_tokens("") == 1
        assert estimate_tokens("a") == 1
