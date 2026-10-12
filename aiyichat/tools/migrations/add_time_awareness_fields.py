#!/usr/bin/env python3
"""数据迁移脚本：为存量数据补齐对话时间感知 P1 新增字段（幂等）。

补齐内容：
1. users.timezone = ""（IANA 名，空串回落全局默认偏移）
2. services.timezone = ""（IANA 名，空串回落用户级/全局默认偏移）
3. service_memories.facts[*] 的四个双时间轴字段补 None
   （valid_at / invalid_at / expired_at / source_message_at）
4. app_settings.default_timezone_offset：若缺失，则用 proactive_timezone_offset
   的值回填（保持运营已配置的时区不丢失），两者皆缺时补默认 8

幂等策略：仅对"缺失"的字段补写，不覆盖已有值；可重复执行。
执行方式（在服务器 Docker 容器内）：
  docker exec aiyichat-app-1 python /app/tools/migrations/add_time_awareness_fields.py
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGODB_DATABASE", "aiyichat")

# MemoryFact 双时间轴四个新字段
_FACT_TEMPORAL_FIELDS = ("valid_at", "invalid_at", "expired_at", "source_message_at")


def _migrate_users(db) -> int:
    """为存量 users 补 timezone=""（仅缺失时）。返回更新条数。"""
    result = db["users"].update_many(
        {"timezone": {"$exists": False}},
        {"$set": {"timezone": ""}},
    )
    return result.modified_count


def _migrate_services(db) -> int:
    """为存量 services 补 timezone=""（仅缺失时）。返回更新条数。"""
    result = db["services"].update_many(
        {"timezone": {"$exists": False}},
        {"$set": {"timezone": ""}},
    )
    return result.modified_count


def _migrate_memory_facts(db) -> int:
    """为存量 service_memories.facts 补四个双时间轴字段为 None。返回更新文档数。"""
    updated = 0
    for doc in db["service_memories"].find({"facts": {"$exists": True, "$ne": []}}):
        facts = doc.get("facts", [])
        changed = False
        for fact in facts:
            for field in _FACT_TEMPORAL_FIELDS:
                if field not in fact:
                    fact[field] = None
                    changed = True
        if changed:
            db["service_memories"].update_one(
                {"_id": doc["_id"]},
                {"$set": {"facts": facts, "updated_at": datetime.now(timezone.utc)}},
            )
            updated += 1
    return updated


def _migrate_app_settings(db) -> bool:
    """把 proactive_timezone_offset 同步进 default_timezone_offset（仅缺失时）。"""
    coll = db["app_settings"]
    raw = coll.find_one({"_id": "default"})
    if raw is None:
        print("[跳过] 未找到 app_settings 文档，应用首次启动会自动 seed")
        return False
    if "default_timezone_offset" in raw:
        print("[跳过] default_timezone_offset 已存在")
        return False
    # 优先沿用运营已配置的主动关怀时区，避免迁移后时区跳变
    offset = raw.get("proactive_timezone_offset", 8)
    try:
        offset = max(-12, min(14, int(offset)))
    except (TypeError, ValueError):
        offset = 8
    coll.update_one(
        {"_id": "default"},
        {"$set": {
            "default_timezone_offset": offset,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    print(f"[完成] default_timezone_offset 已回填为 {offset}（源自 proactive_timezone_offset）")
    return True


def main() -> None:
    """执行迁移。"""
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    users_updated = _migrate_users(db)
    services_updated = _migrate_services(db)
    memory_docs_updated = _migrate_memory_facts(db)
    settings_updated = _migrate_app_settings(db)

    print("[汇总]")
    print(f"  users 补 timezone: {users_updated} 条")
    print(f"  services 补 timezone: {services_updated} 条")
    print(f"  service_memories 补事实时间轴: {memory_docs_updated} 个文档")
    print(f"  app_settings 回填 default_timezone_offset: {'是' if settings_updated else '否'}")

    client.close()


if __name__ == "__main__":
    main()
