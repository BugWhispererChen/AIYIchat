"""LLM 分级降级链：主模型失败时自动切换到备用方案。

降级策略：
1. 主模型超时/失败 → 切换备用模型（如配置）
2. 备用模型失败 → 返回角色人设模板回复
3. 所有 LLM 失败 → 返回通用兜底文案

设计原则：
- 用户无感知或感知最小化
- 保持角色人设一致性
- 记录降级事件供后续分析
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class FallbackResult:
    """降级链执行结果。"""

    text: str
    used_fallback: bool  # 是否使用了降级方案
    fallback_level: int  # 降级级别：0=主模型，1=备用模型，2=模板回复，3=通用兜底
    original_error: Optional[Exception] = None  # 原始异常（如有）


class FallbackChain:
    """LLM 调用分级降级链。

    按优先级依次尝试：
    1. 主模型（正常调用）
    2. 备用模型（配置 llm_fallback_model 时启用）
    3. 角色模板回复（基于 profile.greeting）
    4. 通用兜底文案
    """

    # 通用兜底文案（所有降级都失败时使用）
    GENERIC_FALLBACK = "网络有点开小差了，你可以再发一次吗？"

    def __init__(self):
        self._fallback_enabled = getattr(settings, "llm_fallback_enabled", True)
        self._fallback_model = getattr(settings, "llm_fallback_model", "")

    async def complete_with_fallback(
        self,
        primary_provider: Any,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
        profile: Any = None,
        **kwargs: Any,
    ) -> FallbackResult:
        """带降级链的 LLM 调用。

        Args:
            primary_provider: 主 LLM Provider
            messages: 消息列表
            temperature: 生成温度
            max_tokens: 最大 token 数
            timeout: 超时时间
            profile: 角色配置（用于生成模板回复）
            **kwargs: 其他参数

        Returns:
            FallbackResult 包含回复文本和降级信息
        """
        original_error: Optional[Exception] = None

        # Level 0: 尝试主模型
        try:
            text = await primary_provider.complete(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                **kwargs,
            )
            return FallbackResult(
                text=text,
                used_fallback=False,
                fallback_level=0,
            )
        except Exception as e:
            original_error = e
            logger.warning(
                "Primary LLM failed: %s, attempting fallback chain", e
            )

        # Level 1: 尝试备用模型
        if self._fallback_enabled and self._fallback_model:
            try:
                text = await self._try_fallback_model(
                    messages, temperature, max_tokens, timeout, **kwargs
                )
                if text:
                    logger.info("Fallback model succeeded")
                    return FallbackResult(
                        text=text,
                        used_fallback=True,
                        fallback_level=1,
                        original_error=original_error,
                    )
            except Exception as e:
                logger.warning("Fallback model failed: %s", e)

        # Level 2: 角色模板回复
        if profile is not None:
            try:
                text = self._generate_profile_fallback(profile, messages)
                if text:
                    logger.info("Using profile template fallback")
                    return FallbackResult(
                        text=text,
                        used_fallback=True,
                        fallback_level=2,
                        original_error=original_error,
                    )
            except Exception as e:
                logger.warning("Profile template fallback failed: %s", e)

        # Level 3: 通用兜底
        logger.error(
            "All fallback levels exhausted, using generic fallback. "
            "Original error: %s",
            original_error,
        )
        return FallbackResult(
            text=self.GENERIC_FALLBACK,
            used_fallback=True,
            fallback_level=3,
            original_error=original_error,
        )

    async def _try_fallback_model(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        timeout: float | None,
        **kwargs: Any,
    ) -> str:
        """尝试使用备用模型。

        通过 provider_factory 创建备用模型 provider 并调用。
        """
        from app.llm.provider_factory import get_llm_provider

        # 使用备用模型配置创建临时 provider
        # 注意：这里假设备用模型已配置在 model_configs 中
        fallback_provider = get_llm_provider("fallback")
        return await fallback_provider.complete(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            **kwargs,
        )

    def _generate_profile_fallback(
        self,
        profile: Any,
        messages: list[dict[str, str]],
    ) -> str:
        """基于角色人设生成模板回复。

        使用角色的 greeting 或 personality 特征生成符合人设的兜底回复。
        """
        # 获取角色问候语作为基础
        greeting = getattr(profile, "greeting", "") or ""
        name = getattr(profile, "name", "AI") or "AI"

        # 根据用户最后一条消息内容生成简单回应
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break

        # 生成符合人设的简短回复
        if greeting:
            # 使用 greeting 的语气和风格
            return f"{greeting.split('。')[0]}。我现在有点恍惚，能再说一次吗？"
        else:
            return f"我是{name}，刚才有点走神了。你刚才说什么？"

    def get_fallback_stats(self) -> dict[str, Any]:
        """获取降级链统计信息。"""
        return {
            "fallback_enabled": self._fallback_enabled,
            "fallback_model": self._fallback_model or "(not configured)",
        }


# 全局降级链实例
_fallback_chain: Optional[FallbackChain] = None


def get_fallback_chain() -> FallbackChain:
    """获取全局降级链实例。"""
    global _fallback_chain
    if _fallback_chain is None:
        _fallback_chain = FallbackChain()
    return _fallback_chain
