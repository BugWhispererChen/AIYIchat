#!/usr/bin/env python3
"""在服务器上验证人际关系网络功能的容器内行为（只读验证）。

验证点：
1. kuzu 可导入且版本正确
2. EntityGraphService 实例化成功，未启用时降级模式正确
3. MemoryEntity/EntityRelation 新字段与 from_mongo 兼容逻辑正常
"""

import asyncio
import sys


async def main() -> None:
    # 0. 初始化 MongoDB 连接（降级查询需访问数据库）
    from app.db.mongo import connect_db, close_db
    await connect_db()

    # 1. kuzu 导入验证
    try:
        import kuzu
        print(f"[OK] kuzu 已安装: {kuzu.__version__ if hasattr(kuzu, '__version__') else 'unknown'}")
    except ImportError as e:
        print(f"[FAIL] kuzu 导入失败: {e}")
        sys.exit(1)

    # 2. 图服务降级验证（memory_graph_enabled 默认 False）
    from app.config import settings
    from app.services.entity_graph_service import get_entity_graph_service

    svc = get_entity_graph_service()
    print(f"[INFO] memory_graph_enabled={settings.memory_graph_enabled}")
    print(f"[INFO] graph_service.enabled={svc.enabled}")
    assert svc.enabled is False, "未启用配置时图服务应为降级状态"
    print("[OK] 图服务未启用时正确降级")

    # 降级查询应返回空列表（无异常）
    result = await svc.query_related("test-service", "不存在的人物")
    assert result == [], f"降级查询应返回空列表，实际: {result}"
    print("[OK] 降级模式多跳查询正常返回空结果")

    # 3. 数据模型新字段验证
    from app.models.service_memory import EntityRelation, MemoryEntity

    ent = MemoryEntity(
        name="小红",
        relation_to_user="女朋友",
        entity_type="person",
        aliases=["小红红"],
        sentiment="亲近",
        relations=[EntityRelation(target="小明", relation="哥哥")],
        mention_count=3,
    )
    dumped = ent.model_dump()
    assert dumped["aliases"] == ["小红红"]
    assert dumped["relations"][0]["valid"] is True
    assert dumped["mention_count"] == 3
    print("[OK] MemoryEntity 新字段序列化正常")

    # from_mongo 兼容：存量无新字段的文档
    legacy_doc = {
        "_id": "abc",
        "service_id": "svc-legacy",
        "entities": [{"name": "老王", "relation_to_user": "同事", "attributes": {}}],
    }
    from app.models.service_memory import ServiceMemoryInDB
    mem = ServiceMemoryInDB.from_mongo(legacy_doc)
    old_ent = mem.entities[0]
    assert old_ent.entity_type == "person"
    assert old_ent.aliases == []
    assert old_ent.mention_count == 0
    print("[OK] 存量实体 from_mongo 兼容补默认值正常")

    print("\n=== 全部验证通过 ===")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
