"""P0-P2 改动的后台页面冒烟测试（mongomock + ASGI 内存客户端）。

覆盖真实路由与模板渲染：
- 记忆管理页：空态列表 → 种子记忆后列表/详情 → 删除事实 → 清空记忆
- 内容安全页：词表维护区渲染 → 保存追加词落库并热生效
- 主动关怀页：免打扰时段设置渲染 → 保存后配置更新
- 仪表盘：embedding 状态提示区渲染
"""

import asyncio
from datetime import datetime, timezone

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.db import mongo


async def _setup_app():
    """内存数据库 + 免登录的 ASGI 应用。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_admin_p0p2"]

    from app.admin.auth import require_admin
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: "admin"
    return app


async def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


def _client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


class TestMemoriesPage:
    """记忆管理页冒烟测试。"""

    def test_memories_flow(self) -> None:
        """空态 → 种子记忆 → 列表/详情 → 删除事实 → 清空。"""

        async def _flow() -> None:
            app = await _setup_app()
            try:
                from app.db.repositories.service_memory_repo import (
                    ServiceMemoryRepository,
                )
                from app.models.service_memory import MemoryFact, PendingEvent

                async with _client(app) as client:
                    # 空态列表
                    resp = await client.get("/admin/memories")
                    assert resp.status_code == 200
                    assert "暂无长期记忆" in resp.text

                    # 种子一条记忆（事实 + 待跟进事件）
                    repo = ServiceMemoryRepository()
                    await repo.upsert_topic("svc-1", "考研复习")
                    await repo.add_facts("svc-1", [
                        MemoryFact(content="身份在准备考研", category="event"),
                    ])
                    await repo.add_pending_events("svc-1", [
                        PendingEvent(
                            content="下周一有面试",
                            due_at=datetime.now(timezone.utc),
                        ),
                    ])

                    # 列表展示
                    resp = await client.get("/admin/memories")
                    assert resp.status_code == 200
                    assert "考研复习" in resp.text

                    # 详情展示事实与事件
                    resp = await client.get("/admin/memories/svc-1")
                    assert resp.status_code == 200
                    assert "身份在准备考研" in resp.text
                    assert "下周一有面试" in resp.text

                    # 删除单条事实
                    resp = await client.post(
                        "/admin/memories/svc-1/delete-fact",
                        data={"content": "身份在准备考研"},
                    )
                    assert resp.status_code == 303
                    memory = await repo.find_by_service("svc-1")
                    assert memory is not None
                    assert len(memory.facts) == 0

                    # 清空记忆
                    resp = await client.post("/admin/memories/svc-1/clear")
                    assert resp.status_code == 303
                    assert await repo.find_by_service("svc-1") is None
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())


class TestSafetyWordlists:
    """内容安全词表维护冒烟测试。"""

    def test_render_and_save(self) -> None:
        """页面渲染词表维护区，保存追加词后落库并热生效。"""

        async def _flow() -> None:
            app = await _setup_app()
            try:
                from app.config import settings
                from app.conversation.safety_checker import (
                    get_safety_checker,
                    reset_safety_checker,
                )

                async with _client(app) as client:
                    resp = await client.get("/admin/safety")
                    assert resp.status_code == 200
                    assert "词表维护" in resp.text

                    # 保存追加词（换行分隔）
                    resp = await client.post(
                        "/admin/safety/wordlists",
                        data={
                            "crisis_extra": "特殊危机词A",
                            "forbidden_extra": "特殊违禁词B\n特殊违禁词C",
                        },
                    )
                    assert resp.status_code == 303
                    assert settings.safety_crisis_extra == ["特殊危机词A"]
                    assert settings.safety_forbidden_extra == [
                        "特殊违禁词B", "特殊违禁词C",
                    ]

                    # 追加词热生效：检查器命中新词
                    checker = get_safety_checker()
                    verdict = checker.check_input("我想了解特殊违禁词B的事")
                    assert verdict.action.value == "block"
            finally:
                # 还原全局状态，避免污染其他用例
                settings.safety_crisis_extra = []
                settings.safety_forbidden_extra = []
                reset_safety_checker()
                await _teardown_app(app)

        asyncio.run(_flow())


class TestProactiveQuietHours:
    """主动关怀免打扰设置冒烟测试。"""

    def test_render_and_save(self) -> None:
        """页面渲染免打扰设置区，保存后配置热更新。"""

        async def _flow() -> None:
            app = await _setup_app()
            try:
                from app.config import settings

                async with _client(app) as client:
                    resp = await client.get("/admin/proactive")
                    assert resp.status_code == 200
                    assert "免打扰时段" in resp.text

                    resp = await client.post(
                        "/admin/proactive/quiet-hours",
                        data={
                            "quiet_start": "23", "quiet_end": "7",
                            "tz_offset": "8",
                        },
                    )
                    assert resp.status_code == 303
                    assert settings.proactive_quiet_hour_start == 23
                    assert settings.proactive_quiet_hour_end == 7
            finally:
                from app.config import settings

                settings.proactive_quiet_hour_start = 22
                settings.proactive_quiet_hour_end = 8
                await _teardown_app(app)

        asyncio.run(_flow())


class TestProactiveRecordCleanup:
    """主动关怀记录清理冒烟测试（批量按保留天数 + 单条删除）。"""

    def test_cleanup_and_single_delete(self) -> None:
        """页面渲染清理区与预览条数 → 批量清理 → 单条删除。"""

        async def _flow() -> None:
            app = await _setup_app()
            try:
                from datetime import timedelta

                from app.db.repositories.proactive_repo import (
                    ProactiveRecordRepository,
                )
                from app.models.proactive_record import ProactiveRecord

                repo = ProactiveRecordRepository()
                old = datetime.now(timezone.utc) - timedelta(days=40)
                old_id = await repo.create(ProactiveRecord(
                    identity_id="u-1", platform_openid="oid-1", bot_id="b-1",
                    stage=1, created_at=old,
                ))
                new_id = await repo.create(ProactiveRecord(
                    identity_id="u-1", platform_openid="oid-1", bot_id="b-1",
                    stage=2,
                ))

                async with _client(app) as client:
                    # 清理区渲染 + 预览条数（默认保留 30 天 → 1 条可清理）
                    resp = await client.get("/admin/proactive")
                    assert resp.status_code == 200
                    assert "记录清理" in resp.text
                    assert "当前可清理 1 条" in resp.text

                    # 批量清理：仅删除保留期之外的记录，并回显删除条数
                    resp = await client.post(
                        "/admin/proactive/records/cleanup",
                        data={"retain_days": "30", "status": ""},
                    )
                    assert resp.status_code == 303
                    assert "deleted=1" in resp.headers["location"]
                    assert await repo.find_by_id(old_id) is None
                    assert await repo.find_by_id(new_id) is not None

                    # 删除后页面提示已删除条数
                    resp = await client.get("/admin/proactive?deleted=1")
                    assert "已删除 1 条记录" in resp.text

                    # 保留天数钳制：传 0 不会全量删除
                    resp = await client.post(
                        "/admin/proactive/records/cleanup",
                        data={"retain_days": "0", "status": "failed"},
                    )
                    assert resp.status_code == 303
                    assert "deleted=0" in resp.headers["location"]
                    assert "status=failed" in resp.headers["location"]
                    assert await repo.find_by_id(new_id) is not None

                    # 单条删除
                    resp = await client.post(
                        f"/admin/proactive/records/{new_id}/delete",
                        data={"status": ""},
                    )
                    assert resp.status_code == 303
                    assert "deleted=1" in resp.headers["location"]
                    assert await repo.count_all() == 0

                    # 删除不存在的记录 → deleted=0
                    resp = await client.post(
                        "/admin/proactive/records/no-such/delete", data={}
                    )
                    assert "deleted=0" in resp.headers["location"]
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())


class TestDashboardEmbeddingStatus:
    """仪表盘 embedding 状态提示冒烟测试。"""

    def test_disabled_hint_rendered(self) -> None:
        """embedding 未启用时展示提示条。"""

        async def _flow() -> None:
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/")
                    assert resp.status_code == 200
                    assert "语义检索未启用" in resp.text
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())

    def test_degraded_card_shows_error_code(self) -> None:
        """降级告警条要单独显示错误码。

        `last_error` 是整串文本且按 120 字符截断展示，长报错恰好会把最关键的错误码挤出
        可视区，而"免费额度耗尽"与"限流"这类问题只能靠错误码区分。
        """
        from datetime import datetime, timezone
        from unittest.mock import patch

        from app.services.embedding_service import EmbeddingService

        status = {
            "enabled": True,
            "fail_count": 7,
            "last_error": "Error code: 400 - " + "x" * 400,
            "last_error_code": "400:AllocationQuota.FreeTierOnly",
            "last_error_at": datetime(2026, 10, 22, tzinfo=timezone.utc),
            "degraded": True,
            "breaker_state": "open",
            "recovery_in_seconds": 42,
        }

        async def _flow() -> None:
            app = await _setup_app()
            try:
                with patch.object(EmbeddingService, "status", return_value=status):
                    async with _client(app) as client:
                        resp = await client.get("/admin/")
                        assert resp.status_code == 200
                        assert "400:AllocationQuota.FreeTierOnly" in resp.text
                        assert "42" in resp.text  # 冷却剩余秒数
                        # 降级分支与"未启用"分支互斥，不能同时出现
                        assert "语义检索未启用" not in resp.text
            finally:
                await _teardown_app(app)

        asyncio.run(_flow())
