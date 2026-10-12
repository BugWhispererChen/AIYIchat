#!/usr/bin/env python3
"""共享组功能验证脚本（在测试服务器容器内执行）。

验证点：
1. context_key 解析（设置共享组时返回组 ID）
2. 共享组内两个服务复用同一活跃会话
3. 共享组内两个服务读取同一记忆文档
4. list_by_share_group 返回全部成员
5. 非共享服务仍走独立会话逻辑

执行方式：
  docker compose cp tools/test/verify_share_group.py app:/tmp/
  docker compose exec -T app python /tmp/verify_share_group.py

验证使用临时数据（verify-acc 前缀），结束后自动清理。
"""

import asyncio

from app.db.mongo import close_db, connect_db, get_database
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.service import ServiceCreate

GROUP = "verify-share-group-0001"


async def main() -> None:
    """执行共享组功能验证并清理临时数据。"""
    await connect_db()
    svc_repo = ServiceRepository()
    conv_repo = ConversationRepository()
    mem_repo = ServiceMemoryRepository()

    # 1. 创建两个同账号同角色、不同用户/Bot 的临时服务
    s1 = await svc_repo.create(ServiceCreate(
        name="验证服务A", account_id="verify-acc", user_id="verify-user-1",
        bot_id="verify-bot-1", profile_id="verify-profile", share_group_id=GROUP,
    ))
    s2 = await svc_repo.create(ServiceCreate(
        name="验证服务B", account_id="verify-acc", user_id="verify-user-2",
        bot_id="verify-bot-2", profile_id="verify-profile", share_group_id=GROUP,
    ))

    # 2. context_key 解析
    assert s1.context_key == GROUP and s2.context_key == GROUP
    print("[PASS] context_key 解析为共享组ID")

    # 3. 两个服务 find_or_create_by_key 复用同一会话
    c1 = await conv_repo.find_or_create_by_key(
        s1.user_id, s1.profile_id, s1.context_key
    )
    c2 = await conv_repo.find_or_create_by_key(
        s2.user_id, s2.profile_id, s2.context_key
    )
    assert c1.id == c2.id, f"会话未复用: {c1.id} != {c2.id}"
    print(f"[PASS] 共享组内复用同一会话: {c1.id}")

    # 4. 记忆按组 key 读写
    await mem_repo.upsert_topic(GROUP, "验证话题")
    m1 = await mem_repo.find_by_service(s1.context_key)
    m2 = await mem_repo.find_by_service(s2.context_key)
    assert m1 and m2 and m1.id == m2.id and m1.current_topic == "验证话题"
    print("[PASS] 共享组内读取同一记忆文档")

    # 5. list_by_share_group 返回全部成员
    members = await svc_repo.list_by_share_group(GROUP)
    assert {m.id for m in members} == {s1.id, s2.id}
    print("[PASS] list_by_share_group 返回全部成员")

    # 6. 非共享服务走原有逻辑（独立会话）
    s3 = await svc_repo.create(ServiceCreate(
        name="验证服务C", account_id="verify-acc", user_id="verify-user-3",
        bot_id="verify-bot-3", profile_id="verify-profile",
    ))
    assert s3.context_key == s3.id
    c3 = await conv_repo.find_or_create(s3.user_id, s3.profile_id, s3.id)
    assert c3.id != c1.id
    print("[PASS] 非共享服务仍为独立会话")

    # 清理临时数据
    db = get_database()
    await db.services.delete_many({"account_id": "verify-acc"})
    await db.conversations.delete_many({"service_id": {"$in": [GROUP, s3.id]}})
    await db.messages.delete_many({"conversation_id": {"$in": [c1.id, c3.id]}})
    await db.service_memories.delete_many({"service_id": GROUP})
    print("[CLEAN] 临时数据已清理")
    print("\n全部验证通过")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
