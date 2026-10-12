#!/usr/bin/env python3
"""
数据迁移脚本：消除 bot_config.bot_id 字段，统一使用 UUID id。

迁移内容：
1. bot_configs 集合：将 bot_id 值写入 name（如 name 为空）；$unset 移除 bot_id 字段
2. services 集合：将 bot_id 字段的值从旧字符串更新为对应 Bot 的 UUID id
3. user_bot_bindings 集合：同上

执行方式（在服务器 Docker 容器内）：
  docker exec aiyichat-app-1 python /app/tools/migrations/remove_bot_id_field.py
"""

import os

from pymongo import MongoClient

# 从环境变量获取 MongoDB 连接信息，兜底使用默认值
MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGODB_DATABASE", "aiyichat")


def main() -> None:
    """执行迁移。"""
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    bot_configs = db["bot_configs"]
    services = db["services"]
    user_bot_bindings = db["user_bot_bindings"]

    # ── 步骤 1：构建 bot_id -> UUID id 的映射表 ──────────────
    mapping: dict[str, str] = {}  # old bot_id -> UUID id
    configs = list(bot_configs.find({"bot_id": {"$exists": True}}))

    if not configs:
        print("[跳过] bot_configs 集合中无 bot_id 字段，可能已迁移过")
        client.close()
        return

    print(f"[步骤1] 找到 {len(configs)} 个含 bot_id 的 Bot 配置")

    for doc in configs:
        old_bot_id = doc.get("bot_id", "")
        uuid_id = doc.get("id", "")
        name = doc.get("name", "")

        if not uuid_id:
            print(f"  [警告] Bot _id={doc['_id']} 没有 id 字段，跳过")
            continue

        if old_bot_id:
            mapping[old_bot_id] = uuid_id
            print(f"  映射: {old_bot_id} -> {uuid_id}")

    # ── 步骤 2：更新 bot_configs 集合 ─────────────────────────
    print("\n[步骤2] 更新 bot_configs 集合")
    for doc in configs:
        old_bot_id = doc.get("bot_id", "")
        name = doc.get("name", "")

        update_ops: dict = {}

        # 如果 name 为空，用 bot_id 填充
        if not name and old_bot_id:
            update_ops["$set"] = {"name": old_bot_id}
            print(f"  设置 name: {old_bot_id}")

        # 移除 bot_id 字段
        update_ops["$unset"] = {"bot_id": ""}

        bot_configs.update_one({"_id": doc["_id"]}, update_ops)

    updated_count = len(configs)
    print(f"  完成: 更新了 {updated_count} 个文档")

    # ── 步骤 3：更新 services 集合 ────────────────────────────
    print("\n[步骤3] 更新 services 集合")
    svc_updated = 0
    for svc_doc in services.find({"bot_id": {"$exists": True}}):
        old_val = svc_doc.get("bot_id", "")
        if old_val in mapping:
            new_val = mapping[old_val]
            services.update_one(
                {"_id": svc_doc["_id"]},
                {"$set": {"bot_id": new_val}},
            )
            print(f"  service _id={svc_doc['_id']}: {old_val} -> {new_val}")
            svc_updated += 1
        else:
            # 值可能已经是 UUID 格式，跳过
            print(f"  service _id={svc_doc['_id']}: bot_id={old_val}（不在映射表中，可能已是 UUID）")

    print(f"  完成: 更新了 {svc_updated} 个文档")

    # ── 步骤 4：更新 user_bot_bindings 集合 ───────────────────
    print("\n[步骤4] 更新 user_bot_bindings 集合")
    binding_updated = 0
    for bind_doc in user_bot_bindings.find({"bot_id": {"$exists": True}}):
        old_val = bind_doc.get("bot_id", "")
        if old_val in mapping:
            new_val = mapping[old_val]
            user_bot_bindings.update_one(
                {"_id": bind_doc["_id"]},
                {"$set": {"bot_id": new_val}},
            )
            print(f"  binding _id={bind_doc['_id']}: {old_val} -> {new_val}")
            binding_updated += 1
        else:
            print(f"  binding _id={bind_doc['_id']}: bot_id={old_val}（不在映射表中，可能已是 UUID）")

    print(f"  完成: 更新了 {binding_updated} 个文档")

    # ── 步骤 5：清理旧索引（可选）──────────────────────────────
    print("\n[步骤5] 检查并清理旧索引")
    try:
        # bot_configs 上以 bot_id 为前缀的索引
        for idx_name, idx_info in bot_configs.index_information().items():
            keys = [k for k, _ in idx_info.get("key", [])]
            if "bot_id" in keys and idx_name != "_id_":
                bot_configs.drop_index(idx_name)
                print(f"  删除 bot_configs 索引: {idx_name}")
    except Exception as e:
        print(f"  [警告] 清理索引失败: {e}")

    print("\n[完成] 迁移成功!")
    print(f"  bot_configs: {updated_count} 个文档已更新（移除 bot_id 字段）")
    print(f"  services: {svc_updated} 个文档已更新（bot_id 值改为 UUID）")
    print(f"  user_bot_bindings: {binding_updated} 个文档已更新（bot_id 值改为 UUID）")

    client.close()


if __name__ == "__main__":
    main()
