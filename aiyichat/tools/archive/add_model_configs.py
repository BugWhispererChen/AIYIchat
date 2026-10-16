#!/usr/bin/env python3
"""数据迁移脚本：为 app_settings 生成可复用的"基础配置"组与各功能引用。

将扁平的 LLM / 多媒体配置迁移为 model_configs（连接凭证组）+ 各功能
{task}_config_id 引用。幂等：若 app_settings.model_configs 已非空则跳过。

说明：应用启动时会自动补齐默认分组（app.config.load_settings_from_db →
AppSettingsRepository.migrate_model_configs_if_needed），本脚本仅作手动/显式
触发用途；它直接复用上述应用函数，因此行为随当前数据模型一致——按
DEFAULT_MODEL_GROUPS 生成 5 组默认分组，旧的 llm_* / llm_aux_* 直连字段与
aux-default 分组已随开源版清理移除。

执行方式（在服务器 Docker 容器内）：
  docker exec aiyichat-app-1 python /app/tools/archive/add_model_configs.py
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient

from app.db.repositories.app_settings_repo import build_model_configs_migration
from app.models.app_settings import AppSettingsDocument

# 从环境变量获取 MongoDB 连接信息，兜底使用默认值
MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGODB_DATABASE", "aiyichat")


def main() -> None:
    """执行迁移。"""
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    coll = db["app_settings"]

    raw = coll.find_one({"_id": "default"})
    if raw is None:
        print("[跳过] 未找到 app_settings 文档，应用首次启动会自动 seed 生成")
        client.close()
        return

    if raw.get("model_configs"):
        print("[跳过] model_configs 已存在，无需迁移")
        client.close()
        return

    doc = AppSettingsDocument.from_mongo(raw)
    if doc is None:
        print("[错误] app_settings 文档解析失败")
        client.close()
        return

    updates = build_model_configs_migration(doc)
    updates["updated_at"] = datetime.now(timezone.utc)
    coll.update_one({"_id": "default"}, {"$set": updates})

    groups = updates.get("model_configs", [])
    print(f"[完成] 已生成 {len(groups)} 组基础配置：")
    for g in groups:
        key_state = "已配置" if g.get("api_key") else "空"
        print(
            f"  - {g['id']}: {g['name']} "
            f"(base_url={g['base_url']!r}, model={g['default_model']!r}, key={key_state})"
        )

    overrides = {k: v for k, v in updates.items() if k.endswith("_config_id")}
    if overrides:
        print(f"[完成] 功能引用覆盖: {overrides}")

    client.close()


if __name__ == "__main__":
    main()
