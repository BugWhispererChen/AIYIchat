"""模型基础配置解析层。

将各 AI 功能（task）解析到其引用的一组"基础配置"（连接凭证 + 模型），
供 provider_factory 与多媒体服务统一取用。严格无回退：功能未引用有效配置组
或该组缺少 API 密钥时抛出 RuntimeError，使该功能不可用（与既定语义一致）。
"""

from dataclasses import dataclass
from typing import Any

from app.config import settings

# 文本类功能默认超时（秒），当 {task}_timeout_seconds 缺失时使用
_DEFAULT_TEXT_TIMEOUT = 30
# 媒体类功能默认超时（秒），媒体服务通常自带更细的超时控制
_DEFAULT_MEDIA_TIMEOUT = 30

# 所有支持独立配置的功能 task 名称
TEXT_TASKS = frozenset({
    "chat", "proactive", "focus", "emotion", "summary", "chat_import",
})
MEDIA_TASKS = frozenset({
    "asr", "tts", "imagegen", "imagegen_edit", "vision", "embedding",
})


@dataclass
class ResolvedModelConfig:
    """某功能解析后的最终连接参数。"""

    base_url: str
    api_key: str
    model: str
    timeout: int


def _group_get(group: Any, key: str, default: str = "") -> str:
    """从一组基础配置（dict 或 ModelConfig 对象）读取字段。"""
    if isinstance(group, dict):
        val = group.get(key, default)
    else:
        val = getattr(group, key, default)
    return val if val is not None else default


def _find_group(config_id: str) -> Any | None:
    """在 settings.model_configs 中按 id 查找一组基础配置。"""
    if not config_id:
        return None
    for group in settings.model_configs or []:
        if _group_get(group, "id") == config_id:
            return group
    return None


def resolve_model_config(task: str) -> ResolvedModelConfig:
    """解析某功能引用的基础配置组，返回最终连接参数。

    Args:
        task: 功能名称，如 chat/proactive/focus/emotion/summary/chat_import/
              asr/tts/imagegen/vision/embedding

    Returns:
        ResolvedModelConfig(base_url, api_key, model, timeout)

    Raises:
        RuntimeError: 功能未引用有效配置组、组不存在或组未配置 API 密钥、
                      且无可用模型名时抛出（严格无回退）。
    """
    config_id = str(getattr(settings, f"{task}_config_id", "") or "")
    group = _find_group(config_id)
    if group is None:
        raise RuntimeError(
            f"功能 [{task}] 未引用有效的基础配置 (config_id={config_id!r})，请在管理后台配置"
        )

    api_key = _group_get(group, "api_key")
    if not api_key:
        name = _group_get(group, "name") or config_id
        raise RuntimeError(f"功能 [{task}] 引用的基础配置 [{name}] 未配置 API 密钥")

    # model：直接使用配置组的默认模型
    model = _group_get(group, "default_model")
    if not model:
        name = _group_get(group, "name") or config_id
        raise RuntimeError(f"功能 [{task}] 未指定模型，且基础配置 [{name}] 无默认模型")

    # timeout：文本类读 {task}_timeout_seconds，媒体类用默认值
    if task in TEXT_TASKS:
        timeout = int(getattr(settings, f"{task}_timeout_seconds", 0) or _DEFAULT_TEXT_TIMEOUT)
    else:
        timeout = _DEFAULT_MEDIA_TIMEOUT

    return ResolvedModelConfig(
        base_url=_group_get(group, "base_url"),
        api_key=api_key,
        model=model,
        timeout=timeout,
    )
