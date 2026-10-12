"""model_registry 与 provider_factory 单元测试。

覆盖：
- resolve_model_config 按 task 解析引用的基础配置组
- 功能级 model 覆盖优先于组 default_model
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
                "default_model": "",
            },
        ],
        "chat_config_id": "text-default", "chat_model": "", "chat_timeout_seconds": 15,
        "summary_config_id": "text-default", "summary_model": "", "summary_timeout_seconds": 30,
        "vision_config_id": "dashscope-default", "vision_model": "qwen-vl-max",
        "embedding_config_id": "dashscope-default", "embedding_model": "text-embedding-v3",
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

    def test_feature_model_override(self) -> None:
        """功能级 model 覆盖应优先于组 default_model。"""
        with patch.object(mr, "settings", _settings(chat_model="gpt-4o")):
            assert mr.resolve_model_config("chat").model == "gpt-4o"

    def test_media_uses_own_model(self) -> None:
        """媒体功能使用自身 *_model 与所引用组的密钥。"""
        with patch.object(mr, "settings", _settings()):
            cfg = mr.resolve_model_config("vision")
            assert cfg.model == "qwen-vl-max"
            assert cfg.api_key == "dkey"

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
        """既无功能级 model 又无组默认模型应抛错。"""
        cfgs = [{
            "id": "dashscope-default", "name": "d",
            "base_url": "u", "api_key": "k", "default_model": "",
        }]
        with patch.object(mr, "settings", _settings(model_configs=cfgs, vision_model="")):
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
        cfg = _settings(summary_config_id="dashscope-default", summary_model="ds-model")
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
