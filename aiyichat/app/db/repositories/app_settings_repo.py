"""应用运行时设置的数据库访问层。"""

import logging
import os
from typing import Any

from dotenv import dotenv_values

from app.db.mongo import get_database
from app.models.app_settings import (
    DEFAULT_CONFIG_REFS,
    DEFAULT_MODEL_GROUPS,
    PLACEHOLDER_API_KEY,
    AppSettingsDocument,
    ModelConfig,
)

logger = logging.getLogger(__name__)


class AppSettingsRepository:
    _DOC_ID = "default"

    def _collection(self):
        return get_database().app_settings

    async def get_document(self) -> AppSettingsDocument | None:
        """获取单文档配置。"""
        doc = await self._collection().find_one({"_id": self._DOC_ID})
        return AppSettingsDocument.from_mongo(doc)

    async def upsert(self, values: dict[str, Any]) -> None:
        """更新或插入单文档配置。"""
        from datetime import datetime, timezone

        update_data = dict(values)
        update_data["updated_at"] = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"_id": self._DOC_ID},
            {"$set": update_data},
            upsert=True,
        )

    async def seed_from_env(self, env_path: str = ".env") -> bool:
        """若集合为空，从 .env 解析并插入。返回是否执行了 seed。"""
        existing = await self._collection().find_one({"_id": self._DOC_ID})
        if existing is not None:
            return False

        # dotenv_values 返回 OrderedDict，统一转为 dict 便于后续处理
        raw = dict(dotenv_values(env_path)) if os.path.exists(env_path) else {}
        if not raw:
            logger.warning("No .env file found at %s, using defaults", env_path)

        # 使用 AppSettingsDocument 的默认值作为基础，再用 .env 覆盖
        doc = AppSettingsDocument()
        typed = _coerce_env_to_doc(raw)
        for key, val in typed.items():
            if hasattr(doc, key):
                setattr(doc, key, val)

        # 生成默认基础配置组与功能引用（首次 seed）
        if not doc.model_configs:
            migration = build_model_configs_migration(doc)
            doc.model_configs = [
                ModelConfig(**g) for g in migration.pop("model_configs", [])
            ]
            for key, val in migration.items():
                setattr(doc, key, val)

        await self._collection().insert_one(doc.model_dump(by_alias=True))
        logger.info("Seeded app_settings from .env")
        return True

    async def migrate_model_configs_if_needed(self) -> bool:
        """为已存在的配置文档生成基础配置组与功能引用（幂等）。

        仅当文档存在且 model_configs 为空时执行；返回是否执行了迁移。
        """
        doc = await self.get_document()
        if doc is None or doc.model_configs:
            return False

        updates = build_model_configs_migration(doc)
        await self.upsert(updates)
        logger.info(
            "app_settings model_configs 已迁移：%d 组基础配置",
            len(updates.get("model_configs", [])),
        )
        return True


def build_model_configs_migration(doc: AppSettingsDocument) -> dict[str, Any]:
    """生成默认基础配置组与功能引用。

    幂等：仅在 model_configs 为空时调用。返回需写入（upsert/setattr）的字段字典。
    按 DEFAULT_MODEL_GROUPS 生成 5 组默认分组；.env 已填 DASHSCOPE_API_KEY 时直接
    注入各组 api_key（首次部署即可用），否则留空由管理后台「AI 模型配置」页填写。
    """
    api_key = (
        doc.dashscope_api_key
        if doc.dashscope_api_key and doc.dashscope_api_key != PLACEHOLDER_API_KEY
        else ""
    )
    return {
        "model_configs": [
            ModelConfig(**{**group, "api_key": api_key}).model_dump()
            for group in DEFAULT_MODEL_GROUPS
        ],
        **DEFAULT_CONFIG_REFS,
    }


def _coerce_env_to_doc(raw: dict[str, str | None]) -> dict[str, Any]:
    """将 .env 字符串值转换为 AppSettingsDocument 的字段类型。"""
    result: dict[str, Any] = {}
    bool_fields = {
        "use_mock_db", "asr_enabled",
        "vision_enabled", "tts_enabled", "tts_expression_enabled", "imagegen_enabled",
        "memory_graph_enabled",
        # 语音动态触发窗口开关
        "voice_dynamic_enabled",
        # 对话时间感知（.env 中为 "true"/"on" 字符串，不显式转换会以 str 入库）
        "message_time_enabled", "time_anchor_enabled", "time_idle_hint_enabled",
        "time_day_separator_enabled", "message_time_tag_year_enabled",
        "memory_inject_time_suffix", "time_jionlp_enabled",
        # 记忆双时间轴（软失效与注入前硬过滤）
        "memory_fact_validity_enabled", "memory_expired_filter_enabled",
    }
    int_fields = {
        "context_window_size",
        "summary_trigger_count", "admin_session_hours",
        "chat_max_tokens", "chat_timeout_seconds",
        "proactive_max_tokens", "proactive_timeout_seconds",
        "focus_max_tokens", "focus_timeout_seconds",
        "emotion_max_tokens", "emotion_timeout_seconds",
        "summary_max_tokens", "summary_timeout_seconds",
        "chat_import_max_tokens", "chat_import_timeout_seconds",
        "proactive_max_retries", "proactive_monthly_limit",
        "proactive_quiet_hour_start", "proactive_quiet_hour_end",
        "chat_import_chunk_size",
        # 对话时间感知（单一时区源 + 标签阈值）
        "default_timezone_offset", "message_time_gap_minutes",
        "time_session_gap_hours", "time_reunion_gap_hours",
        # 语音动态触发窗口时长（分钟）
        "voice_window_min_minutes", "voice_window_max_minutes",
        # 聊天记录导入时区偏移
        "chat_import_timezone_offset",
    }
    float_fields = {
        "proactive_llm_temperature",
        "chat_temperature", "proactive_temperature", "focus_temperature",
        "emotion_temperature", "summary_temperature", "chat_import_temperature",
    }

    for key, val in raw.items():
        if val is None:
            continue
        k = key.lower()
        if k in bool_fields:
            result[k] = val.lower() in ("true", "1", "yes", "on")
        elif k in int_fields:
            try:
                result[k] = int(val)
            except ValueError:
                pass
        elif k in float_fields:
            try:
                result[k] = float(val)
            except ValueError:
                pass
        else:
            result[k] = val
    return result
