#!/usr/bin/env python3
"""验证功能实验室开关链路：FEATURE_TOGGLES 收录 + 保存热更新 + 图服务生效。"""

import asyncio


async def main() -> None:
    from app.admin.routes.features import FEATURE_TOGGLES
    from app.config import settings
    from app.db.mongo import connect_db, close_db
    from app.db.repositories.app_settings_repo import AppSettingsRepository
    from app.services.entity_graph_service import (
        get_entity_graph_service,
        reset_entity_graph_service,
    )

    await connect_db()

    # 1. 功能实验室清单收录验证
    keys = [f.key for f in FEATURE_TOGGLES]
    assert "memory_graph_enabled" in keys, "开关未收录到功能实验室"
    toggle = next(f for f in FEATURE_TOGGLES if f.key == "memory_graph_enabled")
    print(f"[OK] 功能实验室已收录开关: {toggle.label}（cost={toggle.cost}）")

    original = settings.memory_graph_enabled
    repo = AppSettingsRepository()
    try:
        # 2. 模拟后台保存开启 → reload → 图服务动态生效
        await repo.upsert({"memory_graph_enabled": True})
        changed = await settings.reload()
        assert settings.memory_graph_enabled is True, "热重载后开关未生效"
        print(f"[OK] 保存 True 后 settings.reload 生效（changed={sorted(changed)[:5]}...）")

        reset_entity_graph_service()
        svc = get_entity_graph_service()
        assert svc.enabled is True, "开启后图服务应启用"
        print("[OK] 开启后图服务单例重置并启用（kuzu 路径）")

        # 3. 模拟关闭 → 降级
        await repo.upsert({"memory_graph_enabled": False})
        await settings.reload()
        reset_entity_graph_service()
        svc = get_entity_graph_service()
        assert svc.enabled is False, "关闭后图服务应降级"
        result = await svc.query_related("test-service", "不存在")
        assert result == []
        print("[OK] 关闭后图服务降级为 MongoDB 模式，查询正常")
    finally:
        # 恢复原始开关状态
        await repo.upsert({"memory_graph_enabled": original})
        await settings.reload()
        reset_entity_graph_service()
        print(f"[OK] 开关已恢复为原始状态: {original}")

    await close_db()
    print("\n=== 开关链路验证全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
