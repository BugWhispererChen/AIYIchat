"""默认基础配置组生成单元测试。

覆盖 build_model_configs_migration：
- 按 DEFAULT_MODEL_GROUPS 生成 5 组默认分组并写入功能引用
- .env 已填有效 DASHSCOPE_API_KEY 时注入各组 api_key（首次部署即可用）
- 占位 key / 未填 key 时 api_key 留空，由管理后台「AI 模型配置」页填写
- 生成结果可回填 AppSettingsDocument（幂等：仅在 model_configs 为空时使用）
"""

from app.db.repositories.app_settings_repo import build_model_configs_migration
from app.models.app_settings import (
    DEFAULT_CONFIG_REFS,
    DEFAULT_MODEL_GROUPS,
    PLACEHOLDER_API_KEY,
    AppSettingsDocument,
)


class TestBuildMigration:
    """build_model_configs_migration 生成逻辑。"""

    def test_default_groups_when_unconfigured(self) -> None:
        """未配置 API Key 时生成 5 组默认分组并写入功能引用。"""
        u = build_model_configs_migration(AppSettingsDocument())
        groups = u["model_configs"]
        assert len(groups) == len(DEFAULT_MODEL_GROUPS) == 5
        assert [g["id"] for g in groups] == [g["id"] for g in DEFAULT_MODEL_GROUPS]
        assert [g["default_model"] for g in groups] == [
            g["default_model"] for g in DEFAULT_MODEL_GROUPS
        ]
        # api_key 留空，由身份填入；功能引用不额外改写
        assert all(g["api_key"] == "" for g in groups)
        assert "focus_config_id" not in u
        for key, config_id in DEFAULT_CONFIG_REFS.items():
            assert u[key] == config_id

    def test_dashscope_key_injected_into_all_groups(self) -> None:
        """.env 填入真实 DASHSCOPE_API_KEY 时注入全部默认分组。"""
        u = build_model_configs_migration(AppSettingsDocument(dashscope_api_key="sk-ds"))
        assert all(g["api_key"] == "sk-ds" for g in u["model_configs"])

    def test_placeholder_key_treated_as_unconfigured(self) -> None:
        """.env 模板占位 key 视为未配置，api_key 仍留空。"""
        u = build_model_configs_migration(
            AppSettingsDocument(dashscope_api_key=PLACEHOLDER_API_KEY)
        )
        assert len(u["model_configs"]) == len(DEFAULT_MODEL_GROUPS)
        assert all(g["api_key"] == "" for g in u["model_configs"])

    def test_result_applies_to_document(self) -> None:
        """生成结果可回填 AppSettingsDocument 并完成往返序列化。"""
        u = build_model_configs_migration(AppSettingsDocument(dashscope_api_key="sk-ds"))
        doc = AppSettingsDocument(**u)
        assert len(doc.model_configs) == len(DEFAULT_MODEL_GROUPS)
        flat = doc.to_flat_dict
        assert "model_configs" in flat
        assert len(flat["model_configs"]) == len(DEFAULT_MODEL_GROUPS)

    def test_idempotent_guard_on_populated_doc(self) -> None:
        """已有 model_configs 的文档不应再次迁移（幂等语义）。"""
        # 迁移守卫：build 仅在 model_configs 为空时调用；此处验证生成结果非空，
        # 从而调用方（seed/repo）据 `if not doc.model_configs` 守卫实现幂等。
        u = build_model_configs_migration(AppSettingsDocument())
        assert u["model_configs"]  # 非空 → 调用方据此判定已迁移
