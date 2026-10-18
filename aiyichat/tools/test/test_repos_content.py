"""内容类仓库层单元测试（mongomock 内存库）。

覆盖：
- ConversationRepository / MessageRepository / ProactiveRecordRepository
- SafetyEventRepository / ProfileRepository / AppSettingsRepository
- ServiceMemoryRepository / AnalysisTaskRepository
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from app.chat_import.models import AnalysisTask
from app.db.repositories.analysis_task_repo import AnalysisTaskRepository
from app.db.repositories.app_settings_repo import (
    AppSettingsRepository,
    _coerce_env_to_doc,
    build_model_configs_migration,
)
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.proactive_repo import ProactiveRecordRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.safety_event_repo import SafetyEventRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.models.app_settings import AppSettingsDocument
from app.models.proactive_record import ProactiveRecord
from app.models.profile import PromptProfileCreate, PromptProfileUpdate
from app.models.safety_event import SafetyEvent
from app.models.service_memory import (
    MemoryEntity,
    MemoryFact,
    PendingEvent,
    reinforce_strength,
    score_memory_fact,
)
from tools.test.db_test_utils import run_with_db


class TestConversationRepository:
    """对话仓库测试集。"""

    def test_crud_and_query(self) -> None:
        async def body():
            repo = ConversationRepository()
            conv = await repo.create("u-1", "p-1", "svc-1", title="标题")
            assert await repo.find_by_id(conv.id) is not None
            assert await repo.find_active("u-1", "p-1", "svc-1") is not None

            # find_or_create 复用已有活跃对话
            again = await repo.find_or_create("u-1", "p-1", "svc-1")
            assert again.id == conv.id
            # 共享组 key：先查后建
            by_key = await repo.find_or_create_by_key("u-2", "p-1", "svc-1")
            assert by_key.id == conv.id
            created = await repo.find_or_create_by_key("u-2", "p-1", "grp-1")
            assert created.service_id == "grp-1"

            # 消息计数与摘要
            await repo.update_after_message(conv.id, 2)
            await repo.set_summary(conv.id, "摘要内容", summary_message_count=2)
            updated = await repo.find_by_id(conv.id)
            assert updated is not None
            assert updated.message_count == 2
            assert updated.summary == "摘要内容"

            # 列表/搜索/统计
            assert len(await repo.list_by_identity("u-1")) == 1
            assert len(await repo.list_by_service("svc-1")) == 1
            assert len(await repo.list_recent()) == 2
            assert len(await repo.search("摘要")) == 1
            assert await repo.count() == 2
            assert await repo.list_ids_by_service("svc-1") == [conv.id]
            latest = await repo.find_latest_by_service("svc-1")
            assert latest is not None and latest.id == conv.id
            assert await repo.find_latest_by_service("none") is None

            # 删除
            assert await repo.delete(conv.id) is True
            assert await repo.delete_by_service("grp-1") == 1

        run_with_db(body)


class TestMessageRepository:
    """消息仓库测试集。"""

    @pytest.mark.parametrize("explicit_time", [False, True])
    def test_create_preserves_received_time(self, explicit_time: bool) -> None:
        """发生时间可早于写入时间；省略时两个字段都采用当前时间。"""
        from app.models.message import MessageInDB

        async def body() -> None:
            """使用真实仓库验证返回对象及实际存储的时间字段。"""
            repo = MessageRepository()
            written_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
            received_at = written_at - timedelta(minutes=2)
            expected = received_at if explicit_time else written_at
            with patch("app.db.repositories.message_repo.datetime", wraps=datetime) as clock:
                clock.now.return_value = written_at
                message = await repo.create(
                    "conv-time", "user", "接收时刻应保留",
                    created_at=received_at if explicit_time else None,
                )
            assert isinstance(message, MessageInDB)
            assert message.created_at == expected
            assert message.updated_at == written_at
            stored = (await repo.get_recent("conv-time"))[0]
            # mongomock 与 MongoDB 默认返回无时区的 UTC 时间。
            assert stored.created_at.replace(tzinfo=timezone.utc) == expected
            assert stored.updated_at.replace(tzinfo=timezone.utc) == written_at

        run_with_db(body)

    @pytest.mark.parametrize("query", ["recent", "paginated", "all"])
    def test_stable_time_and_id_order(self, query: str) -> None:
        """同时间按 ObjectId 同向排序，不能依赖插入顺序或仅按 ID 排序。"""
        from app.db.mongo import get_database

        async def body() -> None:
            """打乱插入顺序，覆盖时间优先、同刻兜底及分页截断。"""
            repo = MessageRepository()
            old = datetime(2026, 10, 14, tzinfo=timezone.utc)
            recent = old + timedelta(days=1)
            await get_database().messages.insert_many([
                {
                    "_id": ObjectId(f"{number:024x}"),
                    "conversation_id": "conv-order",
                    "role": "user" if number % 2 else "assistant",
                    "content": str(number),
                    "created_at": timestamp,
                }
                for number, timestamp in [
                    (1, recent), (6, old), (3, recent), (5, old), (2, recent),
                ]
            ])
            await repo.create("another-conv", "user", "不能混入其他会话")
            if query == "recent":
                full = await repo.get_recent("conv-order")
                window = await repo.get_recent("conv-order", limit=2)
                assert [m.content for m in window] == ["2", "3"]
            elif query == "paginated":
                full = await repo.list_paginated("conv-order")
                window = await repo.list_paginated("conv-order", skip=1, limit=3)
                assert [m.content for m in window] == ["6", "1", "2"]
                assert await repo.list_paginated("conv-order", skip=5) == []
            else:
                full = await repo.get_all_for_conversation("conv-order")
            assert [m.content for m in full] == ["5", "6", "1", "2", "3"]

        run_with_db(body)

    def test_crud_and_stats(self) -> None:
        async def body():
            repo = MessageRepository()
            m1 = await repo.create("conv-1", "user", "你好", emotion_tag="joy")
            await repo.create(
                "conv-1", "assistant", "你好呀", latency_ms=120, llm_model="m1"
            )
            await repo.create("conv-2", "user", "别的对话")

            assert len(await repo.get_recent("conv-1")) == 2
            assert len(await repo.list_paginated("conv-1", limit=1)) == 1
            assert await repo.count_by_conversation("conv-1") == 2
            assert len(await repo.get_all_for_conversation("conv-1")) == 2
            assert await repo.count_total() == 3
            assert await repo.count_today() == 3

            # 情绪分布
            dist = await repo.emotion_distribution(days=7)
            assert {"emotion": "joy", "count": 1} in dist

            # 删除
            assert await repo.delete(m1.id) is True
            assert await repo.delete_by_conversation("conv-1") == 1
            assert await repo.delete_by_conversation_ids([]) == 0
            assert await repo.delete_by_conversation_ids(["conv-2"]) == 1

        run_with_db(body)

    def test_daily_aggregations(self) -> None:
        """按天聚合统计应返回补零后的完整日期序列。

        mongomock 尚不支持 $dateToString 的 timezone 选项，
        遇到该限制时跳过断言（管道组装逻辑仍被覆盖）。
        """

        async def body():
            repo = MessageRepository()
            await repo.create("conv-1", "user", "hi")
            await repo.create("conv-1", "assistant", "hey", latency_ms=100)

            try:
                daily = await repo.count_daily(days=3)
            except NotImplementedError:
                return
            assert len(daily) == 3
            assert sum(d["count"] for d in daily) >= 2

            latency = await repo.avg_latency_daily(days=3)
            assert len(latency) == 3
            assert any(d["avg_ms"] > 0 for d in latency)

        run_with_db(body)


class TestProactiveRecordRepository:
    """主动消息记录仓库测试集。"""

    def test_lifecycle_and_stats(self) -> None:
        async def body():
            repo = ProactiveRecordRepository()
            rid1 = await repo.create(ProactiveRecord(
                identity_id="u-1", platform_openid="oid-1", bot_id="b-1", stage=1,
            ))
            rid2 = await repo.create(ProactiveRecord(
                identity_id="u-1", platform_openid="oid-1", bot_id="b-1", stage=2,
            ))
            await repo.mark_sent(rid1, "关怀内容")
            await repo.mark_failed(rid2, "接口失败")

            r1 = await repo.find_by_id(rid1)
            assert r1 is not None and r1.status == "sent"
            assert await repo.find_by_id("no-such") is None

            assert len(await repo.find_latest_by_identity("u-1", "b-1")) == 2
            since = datetime.now(timezone.utc) - timedelta(days=1)
            assert await repo.get_sent_stages("u-1", "b-1", since) == [1]
            # 阶段1已发送；阶段2失败1次未达上限，不在已尝试列表
            attempted = await repo.get_attempted_stages("u-1", "b-1", since)
            assert 1 in attempted and 2 not in attempted
            # 失败达上限后被抑制
            attempted = await repo.get_attempted_stages(
                "u-1", "b-1", since, max_retries=1
            )
            assert 2 in attempted

            assert await repo.count_sent_this_month("u-1", "b-1", "qq") == 1
            latest = await repo.get_latest_sent("u-1", "b-1")
            assert latest is not None and latest.stage == 1
            assert await repo.get_latest_sent("u-x", "b-1") is None

            # 管理面板查询
            assert len(await repo.list_recent()) == 2
            assert len(await repo.list_recent(status="sent")) == 1
            assert await repo.count_all() == 2
            assert await repo.count_all(status="failed") == 1
            summary = await repo.stats_summary(days=7)
            assert summary["total"] == 2
            assert summary["sent"] == 1
            assert summary["failed"] == 1
            assert summary["by_stage"] == {1: 1}

        run_with_db(body)

    def test_cleanup_old_records(self) -> None:
        async def body():
            repo = ProactiveRecordRepository()
            old = datetime.now(timezone.utc) - timedelta(days=40)
            rid_old_sent = await repo.create(ProactiveRecord(
                identity_id="u-1", platform_openid="oid-1", bot_id="b-1",
                stage=1, created_at=old,
            ))
            rid_old_failed = await repo.create(ProactiveRecord(
                identity_id="u-1", platform_openid="oid-1", bot_id="b-1",
                stage=2, created_at=old,
            ))
            await repo.mark_failed(rid_old_failed, "接口失败")
            # 新记录（当前时间）不应被保留期清理命中
            await repo.create(ProactiveRecord(
                identity_id="u-1", platform_openid="oid-1", bot_id="b-1", stage=3,
            ))

            # 预览：30 天口径命中 2 条旧记录，其中失败 1 条
            assert await repo.count_older_than(30) == 2
            assert await repo.count_older_than(30, only_failed=True) == 1

            # 仅清理失败记录：成功记录保留（不影响月配额与阶段判定）
            assert await repo.delete_older_than(30, only_failed=True) == 1
            assert await repo.find_by_id(rid_old_sent) is not None
            assert await repo.count_all() == 2

            # 全量清理剩余旧记录
            assert await repo.delete_older_than(30) == 1
            assert await repo.count_all() == 1

            # days 下限保护：传 0 等同保留 1 天，不会删掉刚创建的记录
            assert await repo.count_older_than(0) == 0
            assert await repo.delete_older_than(0) == 0

            # 单条删除
            remaining = await repo.list_recent()
            assert await repo.delete_by_id(remaining[0].id) is True
            assert await repo.delete_by_id("no-such") is False
            assert await repo.count_all() == 0

        run_with_db(body)


class TestSafetyEventRepository:
    """安全事件仓库测试集。"""

    def test_create_list_count(self) -> None:
        async def body():
            repo = SafetyEventRepository()
            await repo.create(SafetyEvent(event_type="crisis", excerpt="片段"))
            await repo.create(SafetyEvent(event_type="forbidden"))

            assert len(await repo.list_recent()) == 2
            assert len(await repo.list_recent(event_type="crisis")) == 1
            assert await repo.count() == 2
            assert await repo.count(event_type="forbidden") == 1
            by_type = await repo.count_recent_by_type(days=7)
            assert by_type == {"crisis": 1, "forbidden": 1}

        run_with_db(body)


class TestProfileRepository:
    """角色档案仓库测试集。"""

    def test_crud_and_visibility(self) -> None:
        async def body():
            repo = ProfileRepository()
            pub = await repo.create(PromptProfileCreate(
                slug="pub-role", name="公共角色", visibility="public",
            ))
            await repo.create(PromptProfileCreate(
                slug="priv-role", name="私有角色",
                visibility="private", account_id="acc-1",
            ))

            assert await repo.find_by_slug("pub-role") is not None
            assert await repo.find_by_slug("none") is None
            assert await repo.find_by_id(pub.id) is not None
            assert await repo.count() == 2

            # 可见性过滤
            assert len(await repo.list_active()) == 1
            assert len(await repo.list_active(account_id="acc-1")) == 2
            assert len(await repo.list_active(account_id="acc-2")) == 1
            assert len(await repo.list_public()) == 1
            assert len(await repo.list_by_account("acc-1")) == 1
            assert len(await repo.list_all()) == 2
            assert len(await repo.search("公共")) == 1

            # upsert：已有 slug 时更新
            upserted = await repo.upsert_by_slug(PromptProfileCreate(
                slug="pub-role", name="公共角色v2",
            ))
            assert upserted is not None and upserted.name == "公共角色v2"

            # 空更新与实际更新
            same = await repo.update("pub-role", PromptProfileUpdate())
            assert same is not None
            updated = await repo.update(
                "pub-role", PromptProfileUpdate(name="改名")
            )
            assert updated is not None and updated.name == "改名"

            # 删除为软删除（置 is_active=False）
            assert await repo.delete("pub-role") is True
            after = await repo.find_by_slug("pub-role")
            assert after is not None and after.is_active is False

        run_with_db(body)


class TestAppSettingsRepository:
    """应用设置仓库测试集。"""

    def test_get_upsert_and_migrate(self) -> None:
        async def body():
            repo = AppSettingsRepository()
            assert await repo.get_document() is None

            # upsert 后可读取；model_configs 为空触发迁移
            await repo.upsert({"chat_config_id": "text-default"})
            doc = await repo.get_document()
            assert doc is not None and doc.chat_config_id == "text-default"

            migrated = await repo.migrate_model_configs_if_needed()
            assert migrated is True
            # 二次迁移应为幂等 no-op
            assert await repo.migrate_model_configs_if_needed() is False

        run_with_db(body)

    def test_seed_from_env(self) -> None:
        async def body():
            repo = AppSettingsRepository()
            # .env 不存在时用默认值 seed
            assert await repo.seed_from_env("no_such.env") is True
            doc = await repo.get_document()
            assert doc is not None and len(doc.model_configs) >= 1
            # 已存在时不重复 seed
            assert await repo.seed_from_env("no_such.env") is False

        run_with_db(body)

    def test_coerce_env_to_doc(self) -> None:
        """.env 字符串值按字段类型转换，非法值忽略。"""
        result = _coerce_env_to_doc({
            "USE_MOCK_DB": "true",
            "CHAT_MAX_TOKENS": "512",
            "CHAT_TEMPERATURE": "0.7",
            "SUMMARY_TIMEOUT_SECONDS": "45",
            "CONTEXT_WINDOW_SIZE": "bad-int",
            "PROACTIVE_TEMPERATURE": "bad-float",
            "EMPTY": None,
        })
        assert result["use_mock_db"] is True
        assert result["chat_max_tokens"] == 512
        assert result["chat_temperature"] == 0.7
        assert result["summary_timeout_seconds"] == 45
        assert "context_window_size" not in result
        assert "proactive_temperature" not in result
        assert "empty" not in result

    def test_build_model_configs_migration(self) -> None:
        """.env 已填有效百炼 Key 时注入全部默认分组。"""
        doc = AppSettingsDocument(dashscope_api_key="sk-ds")
        updates = build_model_configs_migration(doc)
        assert len(updates["model_configs"]) == 5
        assert all(g["api_key"] == "sk-ds" for g in updates["model_configs"])

    def test_build_model_configs_migration_default(self) -> None:
        """无有效 key 时生成默认分组（api_key 留空，由身份填入）。"""
        doc = AppSettingsDocument()
        updates = build_model_configs_migration(doc)
        assert len(updates["model_configs"]) == 5
        assert all(g["api_key"] == "" for g in updates["model_configs"])
        assert "focus_config_id" not in updates


class TestServiceMemoryRepository:
    """服务记忆仓库测试集。"""

    def test_clear_current_topic_is_atomic_and_preserves_memory(self) -> None:
        """清理只发出一次带预期话题的条件更新，不重写记忆文档或提取节流时间。"""
        from app.db.mongo import get_database

        async def body() -> None:
            """用真实集合检查落库结果，同时核验更新条件与更新字段。"""
            repo = ServiceMemoryRepository()
            collection = get_database().service_memories
            updated_at = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
            await collection.insert_one({
                "service_id": "svc-1",
                "current_topic": "考试",
                "updated_at": updated_at,
                "facts": [MemoryFact(content="喜欢猫").model_dump()],
                "topic_history": ["电影", "旅行"],
                "entities": [MemoryEntity(name="小红", relation_to_identity="朋友").model_dump()],
                "pending_events": [PendingEvent(content="面试", due_at=updated_at).model_dump()],
                "mood": {"label": "平静", "valence": 0.1, "updated_at": updated_at},
                "legacy_marker": {"keep": True},
            })
            await repo.upsert_topic("svc-other", "考试")
            before = await collection.find_one({"service_id": "svc-1"})
            other_before = await collection.find_one({"service_id": "svc-other"})
            assert before is not None
            with (
                patch.object(repo, "_collection", return_value=collection),
                patch.object(
                    collection, "update_one", new_callable=AsyncMock,
                    wraps=collection.update_one,
                ) as update_one,
                patch.object(
                    collection, "find_one", new_callable=AsyncMock,
                    wraps=collection.find_one,
                ) as find_one,
            ):
                await repo.clear_current_topic("svc-1", "考试")
                # 禁止先读后写、无条件更新、upsert 或额外刷新 updated_at。
                find_one.assert_not_awaited()
                update_one.assert_awaited_once_with(
                    {"service_id": "svc-1", "current_topic": "考试"},
                    {"$set": {"current_topic": ""}},
                )
            after = await collection.find_one({"service_id": "svc-1"})
            assert after is not None
            assert after == {**before, "current_topic": ""}
            assert after["updated_at"].replace(tzinfo=timezone.utc) == updated_at
            assert await collection.find_one({"service_id": "svc-other"}) == other_before
            # 以相同旧值重复清理是幂等操作，其他数据和文档数量保持不变。
            await repo.clear_current_topic("svc-1", "考试")
            assert await collection.find_one({"service_id": "svc-1"}) == after
            assert await collection.count_documents({}) == 2

        run_with_db(body)

    def test_clear_current_topic_does_not_overwrite_concurrent_topic(self) -> None:
        """旧轮读取后产生的新话题必须保留，旧轮的清理不能覆盖并发提取结果。"""
        from app.db.mongo import get_database

        async def body() -> None:
            """确定性模拟读取旧焦点、新焦点写入、旧轮清理的交错顺序。"""
            repo = ServiceMemoryRepository()
            collection = get_database().service_memories
            await repo.upsert_topic("svc-1", "考试")
            stale = await repo.find_by_service("svc-1")
            assert stale is not None and stale.current_topic == "考试"
            await repo.update_topic_and_facts("svc-1", "旅行", [MemoryFact(content="准备去海边")])
            await repo.append_topic_history("svc-1", "考试")
            before = await collection.find_one({"service_id": "svc-1"})
            assert before is not None and before["current_topic"] == "旅行"
            await repo.clear_current_topic("svc-1", stale.current_topic)
            assert await collection.find_one({"service_id": "svc-1"}) == before
            assert await collection.count_documents({}) == 1

        run_with_db(body)

    @pytest.mark.parametrize("expected_topic", ["", "考试"], ids=["empty-topic", "topic"])
    def test_clear_current_topic_missing_service_does_not_insert(self, expected_topic: str) -> None:
        """服务不存在时不新增文档，也不清除其他服务的同名话题。"""
        from app.db.mongo import get_database

        async def body() -> None:
            """空库和仅有其他服务两种情况下都不得发生 upsert。"""
            repo = ServiceMemoryRepository()
            collection = get_database().service_memories
            await repo.clear_current_topic("missing", expected_topic)
            assert await collection.count_documents({}) == 0
            await repo.upsert_topic("svc-other", expected_topic)
            before = await collection.find_one({"service_id": "svc-other"})
            await repo.clear_current_topic("missing", expected_topic)
            assert await repo.find_by_service("missing") is None
            assert await collection.count_documents({}) == 1
            assert await collection.find_one({"service_id": "svc-other"}) == before

        run_with_db(body)

    def test_topic_and_facts(self) -> None:
        async def body():
            repo = ServiceMemoryRepository()
            assert await repo.find_by_service("svc-1") is None

            await repo.upsert_topic("svc-1", "旅行")
            mem = await repo.find_by_service("svc-1")
            assert mem is not None and mem.current_topic == "旅行"

            # add_facts：首次创建、去重、上限淘汰
            await repo.add_facts("svc-2", [MemoryFact(content="喜欢猫")])
            await repo.add_facts("svc-2", [
                MemoryFact(content="喜欢猫"),  # 重复应跳过
                MemoryFact(content="住在上海"),
            ])
            await repo.add_facts("svc-2", [])  # 空列表 no-op
            mem2 = await repo.find_by_service("svc-2")
            assert mem2 is not None and len(mem2.facts) == 2

            # update_topic_and_facts：相似事实更新引用时间而非重复存储
            await repo.update_topic_and_facts(
                "svc-2", "宠物", [MemoryFact(content="喜欢猫")]
            )
            mem2 = await repo.find_by_service("svc-2")
            assert mem2 is not None and len(mem2.facts) == 2
            await repo.update_topic_and_facts(
                "svc-2", "宠物", [MemoryFact(content="养了一只狗")]
            )
            mem2 = await repo.find_by_service("svc-2")
            assert mem2 is not None and len(mem2.facts) == 3

            # 替换与删除
            await repo.replace_fact(
                "svc-2", "住在上海", MemoryFact(content="搬到了北京")
            )
            mem2 = await repo.find_by_service("svc-2")
            assert mem2 is not None
            contents = [f.content for f in mem2.facts]
            assert "搬到了北京" in contents and "住在上海" not in contents
            await repo.delete_fact("svc-2", "搬到了北京")

            # 向量回填
            await repo.set_fact_embedding("svc-2", "喜欢猫", [0.1, 0.2])
            await repo.set_fact_embedding("svc-2", "喜欢猫", [])  # 空向量 no-op

            # 话题历史
            await repo.append_topic_history("svc-2", "美食")
            await repo.append_topic_history("svc-2", "  ")  # 空白 no-op
            mem2 = await repo.find_by_service("svc-2")
            assert mem2 is not None and "美食" in mem2.topic_history

            assert len(await repo.list_all()) == 2

            # 删除
            assert await repo.delete_by_service("svc-1") is True
            assert await repo.delete_by_service_ids([]) == 0
            assert await repo.delete_by_service_ids(["svc-2"]) == 1

        run_with_db(body)

    def test_pending_events(self) -> None:
        async def body():
            repo = ServiceMemoryRepository()
            await repo.add_pending_events("svc-1", [])  # 空列表 no-op
            due = datetime.now(timezone.utc)
            await repo.add_pending_events("svc-1", [
                PendingEvent(content="面试", due_at=due),
            ])
            # 重复事件去重
            await repo.add_pending_events("svc-1", [
                PendingEvent(content="面试", due_at=due),
                PendingEvent(content="体检", due_at=due),
            ])
            mem = await repo.find_by_service("svc-1")
            assert mem is not None and len(mem.pending_events) == 2

            await repo.mark_event_followed_up("svc-1", "面试")
            mem = await repo.find_by_service("svc-1")
            assert mem is not None
            followed = {e.content: e.followed_up for e in mem.pending_events}
            assert followed["面试"] is True and followed["体检"] is False

        run_with_db(body)

    def test_is_similar(self) -> None:
        """字符级 Jaccard 相似度判断。"""
        assert ServiceMemoryRepository._is_similar("", "abc") is False
        assert ServiceMemoryRepository._is_similar("abc", "abc") is True
        assert ServiceMemoryRepository._is_similar("喜欢猫咪", "喜欢猫") is True
        assert ServiceMemoryRepository._is_similar("abcd", "wxyz") is False

    def test_strength_reinforce_on_reference(self) -> None:
        """方案B：引用事实时强度 ×1.2（上限 3.0），刷新最后引用时间。"""
        async def body():
            repo = ServiceMemoryRepository()
            await repo.add_facts("svc-s", [MemoryFact(content="喜欢猫")])
            # 连续引用 2 次：1.0 → 1.2 → 1.44
            await repo.update_fact_reference("svc-s", "喜欢猫")
            await repo.update_fact_reference("svc-s", "喜欢猫")
            mem = await repo.find_by_service("svc-s")
            assert mem is not None
            fact = mem.facts[0]
            assert abs(fact.strength - 1.44) < 1e-6
        run_with_db(body)

    def test_core_tier_never_evicted(self) -> None:
        """方案A：超上限时 core 层（安全/身份）永不淘汰，仅淘汰非 core 层。"""
        async def body():
            from app.config import settings
            repo = ServiceMemoryRepository()
            old = settings.memory_max_facts
            settings.memory_max_facts = 3
            try:
                now = datetime.now(timezone.utc)
                # 1 条 core + 3 条 semantic（递增引用时间，事实0最旧），超过上限 3
                await repo.add_facts("svc-t", [
                    MemoryFact(
                        content="对花生过敏", category="safety", tier="core",
                        last_referenced_at=now,
                    ),
                ])
                for i in range(3):
                    await repo.add_facts("svc-t", [
                        MemoryFact(
                            content=f"一般事实{i}", tier="semantic",
                            last_referenced_at=now + timedelta(seconds=i),
                        ),
                    ])
                mem = await repo.find_by_service("svc-t")
                assert mem is not None
                contents = [f.content for f in mem.facts]
                # core 层必须保留
                assert "对花生过敏" in contents
                # 总数不超过上限
                assert len(contents) <= 3
                # 最旧的 semantic（事实0）应被淘汰，较新的保留
                assert "一般事实0" not in contents
                assert "一般事实2" in contents
            finally:
                settings.memory_max_facts = old
        run_with_db(body)

    def test_upsert_entities(self) -> None:
        """方案D：实体关系按名称 upsert，合并属性并刷新提及时间。"""
        async def body():
            repo = ServiceMemoryRepository()
            await repo.upsert_entities("svc-e", [
                MemoryEntity(
                    name="小红", relation_to_identity="女朋友",
                    attributes={"生日": "下周三"},
                ),
            ])
            # 二次 upsert：合并属性、更新关系
            await repo.upsert_entities("svc-e", [
                MemoryEntity(
                    name="小红", relation_to_identity="女友",
                    attributes={"城市": "上海"},
                ),
                MemoryEntity(name="大黄", relation_to_identity="宠物狗"),
            ])
            mem = await repo.find_by_service("svc-e")
            assert mem is not None
            assert len(mem.entities) == 2
            xh = next(e for e in mem.entities if e.name == "小红")
            assert xh.relation_to_identity == "女友"
            assert xh.attributes == {"生日": "下周三", "城市": "上海"}
            # 空列表 no-op
            await repo.upsert_entities("svc-e", [])
        run_with_db(body)

    def test_entity_aliases_merge(self) -> None:
        """人际关系网络：别名精确命中合并，aliases 并集去重。"""
        async def body():
            repo = ServiceMemoryRepository()
            await repo.upsert_entities("svc-alias", [
                MemoryEntity(
                    name="王建国", relation_to_identity="同事",
                    aliases=["老王"], mention_count=1,
                ),
            ])
            # 二次 upsert：同名实体追加别名，合并而非新建，别名并集去重、提及次数累加
            await repo.upsert_entities("svc-alias", [
                MemoryEntity(
                    name="王建国", relation_to_identity="同事",
                    aliases=["王哥"], mention_count=1,
                ),
            ])
            mem = await repo.find_by_service("svc-alias")
            assert mem is not None
            assert len(mem.entities) == 1
            ent = mem.entities[0]
            assert ent.name == "王建国"
            assert set(ent.aliases) == {"老王", "王哥"}
            assert ent.mention_count == 2
        run_with_db(body)

    def test_entity_relation_conflict_invalid(self) -> None:
        """人际关系网络：同 target 关系变更时旧关系标 invalid 保留时间线。"""
        async def body():
            from app.models.service_memory import EntityRelation
            repo = ServiceMemoryRepository()
            await repo.upsert_entities("svc-rel", [
                MemoryEntity(
                    name="小红", relation_to_identity="朋友",
                    relations=[EntityRelation(target="小明", relation="同事")],
                    mention_count=1,
                ),
            ])
            # 关系变更：同事 → 哥哥（同 target 不同关系）
            await repo.upsert_entities("svc-rel", [
                MemoryEntity(
                    name="小红", relation_to_identity="朋友",
                    relations=[EntityRelation(target="小明", relation="哥哥")],
                    mention_count=1,
                ),
            ])
            mem = await repo.find_by_service("svc-rel")
            assert mem is not None
            ent = mem.entities[0]
            assert len(ent.relations) == 2
            # 旧关系（同事）标 invalid，新关系（哥哥）有效（时间线保留）
            old_rel = next(r for r in ent.relations if r.relation == "同事")
            assert old_rel.valid is False
            new_rel = next(r for r in ent.relations if r.relation == "哥哥")
            assert new_rel.valid is True
        run_with_db(body)

    def test_entity_eviction_by_activity(self) -> None:
        """人际关系网络：超上限时按活跃度（mention_count + last_mentioned_at）淘汰。"""
        async def body():
            from app.config import settings
            repo = ServiceMemoryRepository()
            old = settings.memory_max_entities
            settings.memory_max_entities = 3
            try:
                now = datetime.now(timezone.utc)
                # 4 个实体：活跃度递增（mention_count 0,1,2,3），超上限 3 需淘汰 1 个
                for i in range(4):
                    await repo.upsert_entities("svc-evict", [
                        MemoryEntity(
                            name=f"实体{i}", relation_to_identity="朋友",
                            mention_count=i,
                            last_mentioned_at=now + timedelta(seconds=i),
                        ),
                    ])
                mem = await repo.find_by_service("svc-evict")
                assert mem is not None
                assert len(mem.entities) == 3
                names = [e.name for e in mem.entities]
                # 最不活跃的实体0被淘汰，高活跃度实体保留
                assert "实体0" not in names
                assert "实体3" in names
            finally:
                settings.memory_max_entities = old
        run_with_db(body)


class TestMemoryScoring:
    """记忆评分与强化（方案B）纯函数测试。"""

    def test_reinforce_strength_growth_and_cap(self) -> None:
        assert abs(reinforce_strength(1.0) - 1.2) < 1e-6
        assert abs(reinforce_strength(2.9) - 3.0) < 1e-6  # 上限 3.0
        assert reinforce_strength(3.0) == 3.0

    def test_stronger_fact_decays_slower(self) -> None:
        """强度越高，同一时间间隔下得分越高（衰减越慢）。"""
        now = datetime.now(timezone.utc)
        old_ref = now - timedelta(days=14)  # 14 天前（原半衰期）
        weak = MemoryFact(content="x", last_referenced_at=old_ref, strength=1.0)
        strong = MemoryFact(content="x", last_referenced_at=old_ref, strength=3.0)
        assert score_memory_fact(strong, now) > score_memory_fact(weak, now)
        # 强度 3.0 时半衰期 ×3，14 天仅衰减到 0.5^(1/3)
        assert abs(score_memory_fact(strong, now) - 0.5 ** (14 / 42)) < 1e-6


class TestAnalysisTaskRepository:
    """分析任务仓库测试集。"""

    def test_upsert_find_list(self) -> None:
        async def body():
            repo = AnalysisTaskRepository()
            task = AnalysisTask(task_id="t-1", status="analyzing", progress=50)
            await repo.upsert(task)
            # 二次 upsert 更新状态
            task.status = "done"
            task.progress = 100
            await repo.upsert(task)

            found = await repo.find_by_id("t-1")
            assert found is not None and found.status == "done"
            assert await repo.find_by_id("no-such") is None

            await repo.upsert(AnalysisTask(task_id="t-2"))
            recent = await repo.list_recent()
            assert len(recent) == 2

        run_with_db(body)
