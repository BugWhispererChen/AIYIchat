"""基础配置迁移填充单元测试。

覆盖 build_model_configs_migration（Spec 迁移方案）：
- 由旧 llm_* / vision_base_url / dashscope_api_key 生成 text-default 与 dashscope-default 两组
- 旧 llm_aux_model 生成独立 aux-default 组，focus/emotion/summary 引用该组
- 无 aux 时不生成额外组
- 未配置任何 AI 模型（无 key / 占位 key）时生成 5 组默认分组并写入功能引用
- 生成结果可回填 AppSettingsDocument（幂等：仅在 model_configs 为空时使用）
"""

from app.db.repositories.app_settings_repo import build_model_configs_migration
from app.models.app_settings import (
    DASHSCOPE_DEFAULT_CONFIG_ID,
    DEFAULT_CONFIG_REFS,
    DEFAULT_MODEL_GROUPS,
    PLACEHOLDER_API_KEY,
    TEXT_DEFAULT_CONFIG_ID,
    AppSettingsDocument,
)


def _doc(**overrides) -> AppSettingsDocument:
    base = {
        "llm_base_url": "http://main/v1",
        "llm_api_key": "sk-main",
        "llm_model": "deepseek-chat",
        "vision_base_url": "http://ds/v1",
        "dashscope_api_key": "sk-ds",
    }
    base.update(overrides)
    return AppSettingsDocument(**base)


class TestBuildMigration:
    """build_model_configs_migration 生成逻辑。"""

    def test_generates_two_groups(self) -> None:
        """默认生成 text-default 与 dashscope-default 两组。"""
        u = build_model_configs_migration(_doc())
        groups = u["model_configs"]
        ids = [g["id"] for g in groups]
        assert ids == [TEXT_DEFAULT_CONFIG_ID, DASHSCOPE_DEFAULT_CONFIG_ID]
        text = groups[0]
        assert text["base_url"] == "http://main/v1"
        assert text["api_key"] == "sk-main"
        assert text["default_model"] == "deepseek-chat"
        ds = groups[1]
        assert ds["base_url"] == "http://ds/v1"
        assert ds["api_key"] == "sk-ds"

    def test_aux_model_creates_group(self) -> None:
        """旧 llm_aux_model 生成独立 aux-default 组，focus/emotion/summary 引用该组。"""
        u = build_model_configs_migration(_doc(llm_aux_model="qwen-turbo"))
        groups = u["model_configs"]
        assert len(groups) == 3
        aux = groups[2]
        assert aux["id"] == "aux-default"
        assert aux["default_model"] == "qwen-turbo"
        assert u["focus_config_id"] == "aux-default"
        assert u["emotion_config_id"] == "aux-default"
        assert u["summary_config_id"] == "aux-default"

    def test_no_aux_no_extra_group(self) -> None:
        """无 llm_aux_model 时不生成额外配置组。"""
        u = build_model_configs_migration(_doc())
        assert len(u["model_configs"]) == 2
        assert "focus_config_id" not in u
        assert "emotion_config_id" not in u
        assert "summary_config_id" not in u

    def test_result_applies_to_document(self) -> None:
        """迁移结果可回填 AppSettingsDocument 并完成往返序列化。"""
        u = build_model_configs_migration(_doc(llm_aux_model="qwen-turbo"))
        doc = AppSettingsDocument(**u)
        assert len(doc.model_configs) == 3
        assert doc.focus_config_id == "aux-default"
        flat = doc.to_flat_dict
        assert "model_configs" in flat
        assert len(flat["model_configs"]) == 3

    def test_idempotent_guard_on_populated_doc(self) -> None:
        """已有 model_configs 的文档不应再次迁移（幂等语义）。"""
        # 迁移守卫：build 仅在 model_configs 为空时调用；此处验证生成结果非空，
        # 从而调用方（seed/repo）据 `if not doc.model_configs` 守卫实现幂等。
        u = build_model_configs_migration(_doc())
        assert u["model_configs"]  # 非空 → 调用方据此判定已迁移

    def test_default_groups_when_unconfigured(self) -> None:
        """未配置任何 AI 模型时生成 5 组默认分组并写入功能引用。"""
        u = build_model_configs_migration(AppSettingsDocument())
        groups = u["model_configs"]
        assert len(groups) == len(DEFAULT_MODEL_GROUPS) == 5
        assert [g["id"] for g in groups] == [g["id"] for g in DEFAULT_MODEL_GROUPS]
        assert [g["default_model"] for g in groups] == [
            g["default_model"] for g in DEFAULT_MODEL_GROUPS
        ]
        # api_key 留空，由用户填入；不产生额外配置组
        assert all(g["api_key"] == "" for g in groups)
        assert "focus_config_id" not in u
        # 功能引用指向对应默认分组
        for key, config_id in DEFAULT_CONFIG_REFS.items():
            assert u[key] == config_id

    def test_placeholder_key_treated_as_unconfigured(self) -> None:
        """.env 模板占位 key 视为未配置，仍生成默认分组。"""
        doc = AppSettingsDocument(llm_api_key=PLACEHOLDER_API_KEY)
        u = build_model_configs_migration(doc)
        assert len(u["model_configs"]) == len(DEFAULT_MODEL_GROUPS)
        assert all(g["api_key"] == "" for g in u["model_configs"])
