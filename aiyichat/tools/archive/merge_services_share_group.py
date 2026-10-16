#!/usr/bin/env python3
"""
数据迁移脚本：将若干已有服务并入一个共享组，合并其对话与长期记忆。

迁移内容：
1. 校验各服务属于同一账号且使用同一角色（共享组约束）
2. 选取最近活跃的对话为目标会话，将其余活跃会话的消息重挂到目标会话
   （消息按 created_at 读取，天然按时间线合并），目标会话归属 key 改为组 ID
3. 摘要重置为空（消息合并后由增量摘要链路重新生成）
4. 合并各服务的长期记忆（事实按内容去重、话题取最新、待跟进事件合并）到组 ID
5. 为各服务写入 share_group_id

执行方式（在服务器 Docker 容器内）：
  docker exec aiyichat-app-1 python /app/tools/migrations/merge_services_share_group.py \
    --services <服务ID1> <服务ID2> [--group-id <组ID>] [--dry-run]
"""

import argparse
import os
import uuid

from pymongo import MongoClient

# 从环境变量获取 MongoDB 连接信息，兜底使用默认值
MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGODB_DATABASE", "aiyichat")


def merge_memories(memory_docs: list[dict], group_id: str) -> dict:
    """合并多份服务记忆文档为一份共享组记忆。

    事实按 content 去重（保留 last_referenced_at 较新的一条，向量随之保留）；
    当前话题取 updated_at 最新文档的值；话题历史顺序合并去重（上限10）；
    待跟进事件直接合并。
    """
    docs = sorted(memory_docs, key=lambda d: d.get("updated_at") or d["_id"].generation_time.replace(tzinfo=None))
    facts_by_content: dict[str, dict] = {}
    topic_history: list[str] = []
    pending_events: list[dict] = []
    for doc in docs:
        for fact in doc.get("facts", []):
            content = fact.get("content", "")
            if not content:
                continue
            existing = facts_by_content.get(content)
            if existing is None or (
                fact.get("last_referenced_at")
                and existing.get("last_referenced_at")
                and fact["last_referenced_at"] > existing["last_referenced_at"]
            ):
                facts_by_content[content] = fact
        for topic in doc.get("topic_history", []):
            if topic and topic not in topic_history:
                topic_history.append(topic)
        pending_events.extend(doc.get("pending_events", []))

    latest = docs[-1]
    return {
        "service_id": group_id,
        "facts": list(facts_by_content.values()),
        "current_topic": latest.get("current_topic", ""),
        "topic_history": topic_history[-10:],
        "pending_events": pending_events,
        "updated_at": latest.get("updated_at"),
    }


def main() -> None:
    """执行迁移。"""
    parser = argparse.ArgumentParser(description="将若干服务并入共享组并合并对话与记忆")
    parser.add_argument("--services", nargs="+", required=True, help="要共享的服务ID列表（至少2个）")
    parser.add_argument("--group-id", default="", help="共享组ID，缺省自动生成 UUID")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入数据")
    args = parser.parse_args()

    if len(args.services) < 2:
        print("[错误] --services 至少需要 2 个服务ID")
        return

    client: MongoClient = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    services_col = db["services"]
    convs_col = db["conversations"]
    msgs_col = db["messages"]
    memories_col = db["service_memories"]

    # ── 步骤 1：加载并校验服务 ──────────────────────────────
    services = list(services_col.find({"id": {"$in": args.services}}))
    found_ids = {s["id"] for s in services}
    missing = set(args.services) - found_ids
    if missing:
        print(f"[错误] 以下服务不存在: {missing}")
        client.close()
        return

    accounts = {s.get("account_id", "") for s in services}
    profiles = {s.get("profile_id", "") for s in services}
    if len(accounts) > 1 or len(profiles) > 1:
        print(f"[错误] 共享组约束校验失败：账号={accounts}，角色={profiles}，必须同账号同角色")
        client.close()
        return

    group_id = args.group_id or str(uuid.uuid4())
    print(f"[步骤1] 校验通过：{len(services)} 个服务，共享组ID = {group_id}")

    # ── 步骤 2：合并活跃对话 ────────────────────────────────
    active_convs = list(convs_col.find({
        "service_id": {"$in": args.services + [group_id]},
        "status": "active",
    }))
    if active_convs:
        # 选最近活跃的会话为目标，其余会话的消息重挂到目标
        active_convs.sort(key=lambda c: c.get("last_message_at") or c["_id"].generation_time.replace(tzinfo=None))
        target = active_convs[-1]
        others = active_convs[:-1]
        other_ids = [str(c["_id"]) for c in others]
        print(f"[步骤2] 目标会话 _id={target['_id']}，待合并会话 {len(others)} 个")

        if not args.dry_run:
            if other_ids:
                moved = msgs_col.update_many(
                    {"conversation_id": {"$in": other_ids}},
                    {"$set": {"conversation_id": str(target["_id"])}},
                ).modified_count
                print(f"  重挂消息 {moved} 条")
                convs_col.delete_many({"_id": {"$in": [c["_id"] for c in others]}})
                print(f"  删除已合并会话 {len(others)} 个")
            # 目标会话改挂组 key，重算消息数，摘要重置待重新生成
            msg_count = msgs_col.count_documents({"conversation_id": str(target["_id"])})
            convs_col.update_one(
                {"_id": target["_id"]},
                {"$set": {
                    "service_id": group_id,
                    "message_count": msg_count,
                    "summary": None,
                    "summary_message_count": 0,
                }},
            )
            print(f"  目标会话已挂到组 key，message_count={msg_count}，摘要已重置")
    else:
        print("[步骤2] 无活跃会话，跳过合并（非活跃会话保留原状作为历史归档）")

    # ── 步骤 3：合并长期记忆 ────────────────────────────────
    memory_docs = list(memories_col.find({
        "service_id": {"$in": args.services + [group_id]},
    }))
    if memory_docs:
        merged = merge_memories(memory_docs, group_id)
        print(
            f"[步骤3] 合并 {len(memory_docs)} 份记忆 → "
            f"事实 {len(merged['facts'])} 条，话题历史 {len(merged['topic_history'])} 个，"
            f"待跟进事件 {len(merged['pending_events'])} 个"
        )
        if not args.dry_run:
            memories_col.delete_many({"_id": {"$in": [d["_id"] for d in memory_docs]}})
            memories_col.insert_one(merged)
            print("  已写入共享组记忆文档")
    else:
        print("[步骤3] 无长期记忆，跳过")

    # ── 步骤 4：写入 share_group_id ─────────────────────────
    if not args.dry_run:
        updated = services_col.update_many(
            {"id": {"$in": args.services}},
            {"$set": {"share_group_id": group_id}},
        ).modified_count
        print(f"[步骤4] 已为 {updated} 个服务写入 share_group_id")
    else:
        print(f"[步骤4] (dry-run) 将为 {len(services)} 个服务写入 share_group_id={group_id}")

    print("\n[完成] " + ("dry-run 预览结束，未写入任何数据" if args.dry_run else "迁移成功!"))
    client.close()


if __name__ == "__main__":
    main()
