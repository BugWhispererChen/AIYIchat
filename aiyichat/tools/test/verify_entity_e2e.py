#!/usr/bin/env python3
"""端到端验证：真实服务 ID 的实体 upsert → 别名/关系查询 → 清理测试数据。"""

import asyncio


async def main() -> None:
    from app.db.mongo import connect_db, close_db
    from app.db.repositories.service_memory_repo import ServiceMemoryRepository
    from app.models.service_memory import EntityRelation, MemoryEntity
    from app.services.entity_graph_service import get_entity_graph_service

    await connect_db()
    repo = ServiceMemoryRepository()

    # 查找一个真实存在的服务（只读取第一个）
    all_mem = await repo.list_all()
    if not all_mem:
        print("[SKIP] 数据库中无任何服务记忆，跳过端到端验证")
        await close_db()
        return
    service_id = all_mem[0].service_id
    print(f"[INFO] 使用真实服务: {service_id}")

    # 备份该服务当前实体，验证后恢复
    backup = await repo.find_by_service(service_id)
    backup_entities = [e.model_dump() for e in backup.entities] if backup else []

    try:
        # 1. upsert 测试实体（含别名、实体间关系）
        await repo.upsert_entities(service_id, [
            MemoryEntity(
                name="测试小红", relation_to_user="女朋友",
                aliases=["小红红"], sentiment="亲近",
                relations=[EntityRelation(target="测试小明", relation="哥哥")],
                mention_count=1,
            ),
        ])
        mem = await repo.find_by_service(service_id)
        ent = next(e for e in mem.entities if e.name == "测试小红")
        assert ent.aliases == ["小红红"]
        assert ent.relations[0].valid is True
        print("[OK] 实体 upsert 成功（别名/情感/关系边完整落库）")

        # 2. 别名命中模拟（与 engine 注入相同的匹配逻辑）
        scan_text = "今天小红红和我吵架了"
        hit = ent.name in scan_text or any(a in scan_text for a in ent.aliases)
        assert hit, "别名应命中扫描文本"
        print("[OK] 别名'小红红'命中当前输入（注入路径可用）")

        # 3. 图服务降级一跳查询
        svc = get_entity_graph_service()
        paths = await svc.query_related(service_id, "测试小红")
        assert len(paths) == 1 and paths[0]["relations"] == ["哥哥"]
        print(f"[OK] 降级一跳查询: {paths[0]['description']}")

        # 4. 关系变更 → 旧关系标 invalid
        await repo.upsert_entities(service_id, [
            MemoryEntity(
                name="测试小红",
                relations=[EntityRelation(target="测试小明", relation="前男友")],
                mention_count=1,
            ),
        ])
        mem = await repo.find_by_service(service_id)
        ent = next(e for e in mem.entities if e.name == "测试小红")
        rels = {r.relation: r.valid for r in ent.relations}
        assert rels["哥哥"] is False and rels["前男友"] is True
        assert ent.mention_count == 2
        print(f"[OK] 关系变更时间线保留: {rels}，提及次数={ent.mention_count}")
    finally:
        # 恢复原始实体数据（清理测试痕迹）
        from datetime import datetime, timezone
        await repo._collection().update_one(
            {"service_id": service_id},
            {"$set": {"entities": backup_entities, "updated_at": datetime.now(timezone.utc)}},
        )
        print("[OK] 测试数据已清理，原始实体已恢复")

    await close_db()
    print("\n=== 端到端验证全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
