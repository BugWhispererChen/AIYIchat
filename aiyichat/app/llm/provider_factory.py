"""LLM 提供者工厂。

按功能（task）解析其引用的基础配置组，构造对应的 OpenAI 兼容 Provider，
并按 task 缓存单例。运行时切换配置后调用 reset_provider() 清空缓存即可生效。
"""

from app.llm.base import LLMProvider
from app.llm.model_registry import resolve_model_config
from app.llm.openai_provider import OpenAICompatibleProvider

# 按 task 缓存的 Provider 单例
_providers: dict[str, LLMProvider] = {}


def get_llm_provider(task: str = "chat") -> LLMProvider:
    """获取指定功能的 LLM 提供者单例。

    Args:
        task: 功能名称（chat/proactive/focus/emotion/summary/chat_import 等）。

    Raises:
        RuntimeError: 该功能未引用有效基础配置时（严格无回退）。
    """
    provider = _providers.get(task)
    if provider is None:
        cfg = resolve_model_config(task)
        provider = OpenAICompatibleProvider(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            timeout=cfg.timeout,
        )
        _providers[task] = provider
    return provider


def reset_provider() -> None:
    """清空全部 Provider 缓存（用于运行时切换模型或测试）。"""
    _providers.clear()
