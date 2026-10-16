# 归档脚本

本目录存放**已失效或一次性**的工具脚本，仅作历史留档：

- 不参与 `pytest` 收集（文件名不匹配 `test_*.py`），`tools/test/run_tests.py` 不会执行它们；
- 不随 Linux 部署包交付（`deploy_package/package_deploy.py` 只显式拷贝 `tools/seed_companion_profiles.py`）；
- 未被 `Dockerfile`、`docker-compose.yml` 或任何生产代码引用，**不要在新代码中引用本目录**。

如需复跑，须自行按脚本内的环境变量要求配置 `.env`。

| 文件 | 原位置 | 归档原因 |
|---|---|---|
| `verify_entity_e2e.py`、`verify_entity_network.py`、`verify_graph_toggle.py` | `tools/test/` | 实体关系图谱功能上线时的一次性容器内人工验证 |
| `verify_message_time.py`、`verify_length_guide.sh`、`verify_verbosity_ui.py` | `tools/test/` | 时间戳/长度软引导/verbosity 已上线，对应能力已有 pytest 回归覆盖 |
| `verify_password_change.py` | `tools/test/` | 改密功能一次性验证；原硬编码后台口令已改为读环境变量 |
| `verify_share_group.py` | `tools/test/` | 服务共享组一次性验证 |
| `manual_imagegen_edit_check.py` | `tools/test/` | 图像编辑模型人工对比选型，非自动化用例 |
| `deploy.sh`、`deploy.ps1` | `tools/deploy_package/` | 部署包实际从 `aiyichat/` 根目录取 `deploy.sh`，Windows 走 EXE 安装包，这两个副本从未被引用 |
| `add_model_configs.py` | `tools/migrations/` | 模型配置迁移已由 `app/config.py` 的 `load_settings_from_db` 自动完成 |
| `add_time_awareness_fields.py` | `tools/migrations/` | 回填目标 `default_timezone_offset` 已是单一时区源并在 config 内置默认值；脚本原读取的 `proactive_timezone_offset` 已随兼容垫片删除，现跑会失效 |
| `enable_default_features.py` | `tools/migrations/` | `support_strategy_enabled`/`consistency_check_enabled` 已在 `app/config.py` 默认启用并随首次 seed 落库 |
| `backfill_last_active.py`、`merge_services_share_group.py`、`remove_bot_id_field.py` | `tools/migrations/` | 历史一次性数据修复/字段下线，线上已执行完毕 |
| `seed_sticker_assets.py` | `tools/` | 开发期执行一次，产物 `data/stickers/` 与 `manifest.json` 已入库 |

生产依赖 `tools/seed_companion_profiles.py`（被 `app/bootstrap.py` 运行时导入、被 `Dockerfile` COPY，
**不可移动或删除**）仍保留原位。
