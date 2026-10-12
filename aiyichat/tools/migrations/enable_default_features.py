#!/usr/bin/env python3
"""数据迁移脚本：为存量 app_settings 启用零成本能力开关。

将两个纯规则、零额外成本的能力开关默认启用（若字段缺失或为旧的 False 默认）：
- support_strategy_enabled：情感支持策略（探索→安慰→行动）
- consistency_check_enabled：人设一致性自检（防穿帮 + 合规）

幂等策略：仅当字段"缺失"时补写为 True，避免覆盖运营已显式关闭的选择。
若运营曾主动关闭这两项，请勿运行本脚本，或运行后在「功能实验室」重新关闭。

执行方式（在服务器 Docker 容器内）：
  docker exec aiyichat-app-1 python /app/tools/migrations/enable_default_features.py
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGODB_DATABASE", "aiyichat")

# 需补写为 True 的零成本能力开关
_DEFAULT_ON_FEATURES = (
    "support_strategy_enabled",
    "consistency_check_enabled",
)


def main() -> None:
    """执行迁移。"""
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    coll = db["app_settings"]

    raw = coll.find_one({"_id": "default"})
    if raw is None:
        print("[跳过] 未找到 app_settings 文档，应用首次启动会自动 seed（默认已启用）")
        client.close()
        return

    # 仅对"缺失"的字段补写 True，尊重运营已显式设置的值
    updates: dict[str, object] = {}
    for key in _DEFAULT_ON_FEATURES:
        if key not in raw:
            updates[key] = True

    if not updates:
        print("[跳过] 目标能力开关字段均已存在，无需迁移")
        client.close()
        return

    updates["updated_at"] = datetime.now(timezone.utc)
    coll.update_one({"_id": "default"}, {"$set": updates})

    enabled = [k for k in updates if k != "updated_at"]
    print(f"[完成] 已为 {len(enabled)} 个零成本能力开关补写默认启用：")
    for k in enabled:
        print(f"  - {k} = True")

    client.close()


if __name__ == "__main__":
    main()
