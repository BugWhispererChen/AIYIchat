"""model_registry 与 provider_factory 单元测试。

覆盖：
- resolve_model_config 按 task 解析引用的基础配置组（模型取组的 default_model）
- 未引用/组不存在/无密钥/无模型 时抛 RuntimeError（严格无回退）
- provider_factory 按 task 缓存与 reset
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.llm.model_registry as mr
import app.llm.provider_factory as pf


def _settings(model_configs=None, **overrides) -> SimpleNamespace:
    """构造带默认字段的 settings 替身。"""
    base = {
        "model_configs": model_configs if model_configs is not None else [
            {
                "id": "text-default", "name": "文本",
                "base_url": "http://text", "api_key": "tkey",
                "default_model": "text-model",
            },
            {
                "id": "dashscope-default", "name": "百炼",
                "base_url": "http://ds", "api_key": "dkey",
                "default_model": "ds-model",
            },
        ],
        "chat_config_id": "text-default", "chat_timeout_seconds": 15,
        "summary_config_id": "text-default", "summary_timeout_seconds": 30,
        "vision_config_id": "dashscope-default",
        "embedding_config_id": "dashscope-default",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestResolveModelConfig:
    """resolve_model_config 解析与无回退语义。"""

    def test_resolve_uses_group(self) -> None:
        """文本功能应取所引用组的连接与默认模型。"""
        with patch.object(mr, "settings", _settings()):
            cfg = mr.resolve_model_config("chat")
            assert cfg.base_url == "http://text"
            assert cfg.api_key == "tkey"
            assert cfg.model == "text-model"
            assert cfg.timeout == 15

    def test_media_uses_group_model(self) -> None:
        """媒体功能取所引用组的默认模型与密钥，超时用媒体默认值。"""
        with patch.object(mr, "settings", _settings()):
            cfg = mr.resolve_model_config("vision")
            assert cfg.model == "ds-model"
            assert cfg.api_key == "dkey"
            assert cfg.timeout == 30

    def test_missing_config_raises(self) -> None:
        """config_id 指向不存在的组应抛错（无回退）。"""
        with patch.object(mr, "settings", _settings(chat_config_id="nope")):
            with pytest.raises(RuntimeError):
                mr.resolve_model_config("chat")

    def test_empty_api_key_raises(self) -> None:
        """组未配置密钥应抛错。"""
        cfgs = [{
            "id": "text-default", "name": "t",
            "base_url": "u", "api_key": "", "default_model": "m",
        }]
        with patch.object(mr, "settings", _settings(model_configs=cfgs)):
            with pytest.raises(RuntimeError):
                mr.resolve_model_config("chat")

    def test_no_model_raises(self) -> None:
        """组无默认模型时抛错（严格无回退）。"""
        cfgs = [{
            "id": "dashscope-default", "name": "d",
            "base_url": "u", "api_key": "k", "default_model": "",
        }]
        with patch.object(mr, "settings", _settings(model_configs=cfgs)):
            with pytest.raises(RuntimeError):
                mr.resolve_model_config("vision")


class TestProviderFactory:
    """provider_factory 按 task 缓存与重置。"""

    def teardown_method(self) -> None:
        pf.reset_provider()

    def test_provider_cached_per_task(self) -> None:
        """同一 task 多次获取返回同一实例。"""
        with patch.object(mr, "settings", _settings()):
            pf.reset_provider()
            assert pf.get_llm_provider("chat") is pf.get_llm_provider("chat")

    def test_different_tasks_different_providers(self) -> None:
        """不同 task 使用各自模型，返回不同实例。"""
        cfg = _settings(summary_config_id="dashscope-default")
        with patch.object(mr, "settings", cfg):
            pf.reset_provider()
            chat = pf.get_llm_provider("chat")
            summary = pf.get_llm_provider("summary")
            assert chat is not summary
            assert chat.model_name == "text-model"
            assert summary.model_name == "ds-model"

    def test_reset_clears_cache(self) -> None:
        """reset_provider 后重新获取应为新实例。"""
        with patch.object(mr, "settings", _settings()):
            pf.reset_provider()
            first = pf.get_llm_provider("chat")
            pf.reset_provider()
            assert first is not pf.get_llm_provider("chat")


class TestThinkingFlag:
    """enable_thinking 只在认识该参数的百炼系端点下发。

    混合推理模型（Qwen3 / DeepSeek）默认先吐 reasoning chunk：实测长上下文下
    token 预算被思考吃满（正文 0 字）且首包显著变慢；而 DeepSeek 官方 API 等
    非百炼后端不认识这个参数，传了会直接报错。
    """

    @staticmethod
    def _extra_body(base_url: str, model: str) -> object:
        from app.llm.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=base_url, api_key="k", model=model
        )._extra_body

    def test_dashscope_qwen3_turns_thinking_off(self) -> None:
        assert self._extra_body(
            "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-flash"
        ) == {"enable_thinking": False}

    def test_maas_deepseek_turns_thinking_off(self) -> None:
        """现网口径：MaaS 工作空间端点 + deepseek 同样默认思考（2026-10 实测）。"""
        assert self._extra_body(
            "https://ws-abc.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "deepseek-v4.1-flash",
        ) == {"enable_thinking": False}

    def test_official_deepseek_api_not_touched(self) -> None:
        assert self._extra_body("https://api.deepseek.com/v1", "deepseek-chat") is None

    def test_unknown_model_family_not_touched(self) -> None:
        assert self._extra_body(
            "https://dashscope.aliyuncs.com/compatible-mode/v1", "gpt-4o"
        ) is None
