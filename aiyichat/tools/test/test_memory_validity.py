"""记忆双时间轴（MemoryFact valid_at/invalid_at/expired_at/source_message_at）单元测试。

覆盖方案 P1 的记忆有效性保障：
- is_fact_invalidated：仅当 invalid_at/expired_at 非空且已过期时判定失效
- validity_downweight：带 valid_at 的已过去事件按事件自身日期降权
- 软失效：UPDATE 裁决后旧事实仍在库中，仅被标记失效
- 硬过滤：已失效事实不出现在注入候选中
- 自我强化循环修正：已失效事实不再刷新 last_referenced_at / strength
- 存量兼容：无新字段的老文档 from_mongo 补 None、不被误过滤
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.conversation.focus_tracker import FocusTracker
from app.models.service_memory import (
    MemoryFact,
    ServiceMemoryInDB,
    is_fact_invalidated,
    validity_downweight,
)

_NOW = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)


def _fact(content: str, **kwargs) -> MemoryFact:
    """构造测试事实，默认 created_at/last_referenced_at 为 NOW。"""
    kwargs.setdefault("created_at", _NOW)
    kwargs.setdefault("last_referenced_at", _NOW)
    return MemoryFact(content=content, **kwargs)


class TestIsFactInvalidated:
    """失效判定：仅作用于 invalid_at/expired_at 非空的事实。"""

    def test_none_fields_not_invalidated(self) -> None:
        """存量事实（两字段全 None）不被误判为失效。"""
        fact = _fact("用户喜欢喝咖啡")
        assert is_fact_invalidated(fact, _NOW) is False

    def test_invalid_at_in_past(self) -> None:
        fact = _fact("用户8月26日有面试", invalid_at=_NOW - timedelta(days=1))
        assert is_fact_invalidated(fact, _NOW) is True

    def test_invalid_at_in_future(self) -> None:
        """invalid_at 尚未到期时不算失效。"""
        fact = _fact("用户9月10日体检", invalid_at=_NOW + timedelta(days=8))
        assert is_fact_invalidated(fact, _NOW) is False

    def test_expired_at_in_past(self) -> None:
        fact = _fact("旧事实", expired_at=_NOW - timedelta(hours=1))
        assert is_fact_invalidated(fact, _NOW) is True

    def test_naive_invalid_at_treated_as_utc(self) -> None:
        """存量 naive 时间按 UTC 归一，不报错。"""
        naive_past = (_NOW - timedelta(days=1)).replace(tzinfo=None)
        fact = _fact("naive 失效", invalid_at=naive_past)
        assert is_fact_invalidated(fact, _NOW) is True

    def test_default_now(self) -> None:
        """不传 now 时用当前真实时间。"""
        fact = _fact("永远有效", invalid_at=datetime.now(timezone.utc) + timedelta(days=1))
        assert is_fact_invalidated(fact) is False


class TestValidityDownweight:
    """现实成立轴（valid_at）的过期降权。"""

    def test_no_valid_at_no_downweight(self) -> None:
        """长期事实无 valid_at → 恒为 1.0。"""
        fact = _fact("用户喜欢喝咖啡")
        assert validity_downweight(fact, _NOW) == 1.0

    def test_future_valid_at_no_downweight(self) -> None:
        """未来才成立的事件不降权。"""
        fact = _fact("用户9月10日体检", valid_at=_NOW + timedelta(days=8))
        assert validity_downweight(fact, _NOW) == 1.0

    def test_past_valid_at_downweights(self) -> None:
        """已过去的事件按事件自身日期老化（半衰期 14 天）。"""
        fact = _fact("用户8月26日有面试", valid_at=_NOW - timedelta(days=7))
        w = validity_downweight(fact, _NOW)
        assert 0.0 < w < 1.0

    def test_older_event_lower_weight(self) -> None:
        """越久远的已过去事件权重越低。"""
        recent = _fact("近期事件", valid_at=_NOW - timedelta(days=7))
        old = _fact("远期事件", valid_at=_NOW - timedelta(days=28))
        assert validity_downweight(old, _NOW) < validity_downweight(recent, _NOW)


class TestFromMongoCompat:
    """存量数据兼容：无新字段的老文档补 None，不报错、不被误过滤。"""

    def test_legacy_fact_without_temporal_fields(self) -> None:
        doc = {
            "_id": "mem1",
            "service_id": "svc1",
            "facts": [
                {
                    "content": "用户喜欢喝咖啡",
                    "category": "preference",
                    "created_at": _NOW,
                    "last_referenced_at": _NOW,
                    # 无 valid_at/invalid_at/expired_at/source_message_at/strength/tier
                }
            ],
            "pending_events": [
                {"content": "面试", "due_at": _NOW, "created_at": _NOW}
            ],
            "updated_at": _NOW,
        }
        mem = ServiceMemoryInDB.from_mongo(doc)
        fact = mem.facts[0]
        assert fact.valid_at is None
        assert fact.invalid_at is None
        assert fact.expired_at is None
        assert fact.source_message_at is None
        assert fact.strength == 1.0
        assert fact.tier == "semantic"
        # 存量事实不被硬过滤误杀
        assert is_fact_invalidated(fact, _NOW) is False
        # 存量事件补 followed_up=False
        assert mem.pending_events[0].followed_up is False


class TestSoftReplace:
    """软失效：UPDATE 裁决后旧事实仍在库中，仅被标记失效。"""

    def test_soft_replace_marks_old_fact(self) -> None:
        """soft_replace_fact 写入 invalid_at/expired_at，不删除旧事实。"""
        from app.db.repositories.service_memory_repo import ServiceMemoryRepository

        repo = ServiceMemoryRepository()
        repo._collection = MagicMock()
        collection = repo._collection.return_value
        collection.update_one = AsyncMock()
        collection.find_one = AsyncMock(return_value={
            "service_id": "svc1",
            "facts": [{"content": "旧事实", "strength": 1.0}],
        })
        # add_facts 内部会再 find_one + update_one
        new_fact = _fact("新事实", valid_at=_NOW)
        asyncio.run(repo.soft_replace_fact(
            "svc1", "旧事实", new_fact,
            invalid_at=_NOW, expired_at=_NOW,
        ))
        # 第一次 update_one 是软失效标记（$set invalid_at/expired_at）
        first_call = collection.update_one.call_args_list[0]
        set_fields = first_call.args[1]["$set"]
        assert "facts.$.invalid_at" in set_fields
        assert "facts.$.expired_at" in set_fields
        # 没有 $pull（不删除）
        assert "$pull" not in first_call.args[1]

    def test_apply_update_uses_soft_when_enabled(self) -> None:
        """memory_fact_validity_enabled=True 时走软失效。"""
        tracker = FocusTracker()
        tracker.memory_repo = MagicMock()
        tracker.memory_repo.soft_replace_fact = AsyncMock()
        tracker.memory_repo.replace_fact = AsyncMock()
        old = _fact("旧事实")
        new = _fact("新事实", valid_at=_NOW)
        with patch("app.conversation.focus_tracker.settings") as mock_settings:
            mock_settings.memory_fact_validity_enabled = True
            asyncio.run(tracker._apply_update("svc1", old, new))
        tracker.memory_repo.soft_replace_fact.assert_awaited_once()
        tracker.memory_repo.replace_fact.assert_not_awaited()

    def test_apply_update_falls_back_to_hard_replace(self) -> None:
        """memory_fact_validity_enabled=False 时退回物理替换（优雅降级）。"""
        tracker = FocusTracker()
        tracker.memory_repo = MagicMock()
        tracker.memory_repo.soft_replace_fact = AsyncMock()
        tracker.memory_repo.replace_fact = AsyncMock()
        old = _fact("旧事实")
        new = _fact("新事实")
        with patch("app.conversation.focus_tracker.settings") as mock_settings:
            mock_settings.memory_fact_validity_enabled = False
            asyncio.run(tracker._apply_update("svc1", old, new))
        tracker.memory_repo.replace_fact.assert_awaited_once()
        tracker.memory_repo.soft_replace_fact.assert_not_awaited()


class TestHardFilter:
    """注入前硬过滤：已失效事实不出现在候选中。"""

    def test_invalidated_facts_excluded_from_injection(self) -> None:
        """engine._retrieve_memories 的硬过滤把已失效事实排除出 memory_facts。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine.__new__(ConversationEngine)
        engine.memory_repo = MagicMock()
        engine.topic_manager = MagicMock()
        engine.topic_manager.generate_topic_context = MagicMock(return_value="")

        valid_fact = _fact("用户喜欢喝咖啡", category="preference")
        expired_fact = _fact(
            "用户8月26日有面试", category="life_event",
            invalid_at=_NOW - timedelta(days=1),
        )
        service_memory = MagicMock()
        service_memory.current_topic = ""
        service_memory.mood = None
        service_memory.facts = [valid_fact, expired_fact]
        service_memory.pending_events = []
        service_memory.entities = []
        service_memory.topic_history = []
        engine.memory_repo.find_by_service = AsyncMock(return_value=service_memory)

        with patch("app.conversation.engine.settings") as mock_settings, \
                patch("app.conversation.engine.get_embedding_service") as mock_emb, \
                patch("app.conversation.engine.datetime") as mock_dt:
            mock_settings.character_mood_enabled = False
            mock_settings.memory_expired_filter_enabled = True
            mock_settings.memory_retrieve_topk = 8
            mock_settings.memory_semantic_weight = 0.6
            mock_settings.memory_entity_scan_turns = 3
            mock_dt.now.return_value = _NOW
            mock_emb.return_value.enabled = False
            result = asyncio.run(
                engine._retrieve_memories("svc1", "你好", backfill=False)
            )
        memory_facts = result[2]
        assert "用户喜欢喝咖啡" in memory_facts
        assert "用户8月26日有面试" not in memory_facts

    def test_filter_disabled_keeps_all(self) -> None:
        """memory_expired_filter_enabled=False 时失效事实仍被注入（优雅降级）。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine.__new__(ConversationEngine)
        engine.memory_repo = MagicMock()
        engine.topic_manager = MagicMock()
        engine.topic_manager.generate_topic_context = MagicMock(return_value="")

        expired_fact = _fact(
            "用户8月26日有面试", category="life_event",
            invalid_at=_NOW - timedelta(days=1),
        )
        service_memory = MagicMock()
        service_memory.current_topic = ""
        service_memory.mood = None
        service_memory.facts = [expired_fact]
        service_memory.pending_events = []
        service_memory.entities = []
        service_memory.topic_history = []
        engine.memory_repo.find_by_service = AsyncMock(return_value=service_memory)

        with patch("app.conversation.engine.settings") as mock_settings, \
                patch("app.conversation.engine.get_embedding_service") as mock_emb, \
                patch("app.conversation.engine.datetime") as mock_dt:
            mock_settings.character_mood_enabled = False
            mock_settings.memory_expired_filter_enabled = False
            mock_settings.memory_retrieve_topk = 8
            mock_settings.memory_semantic_weight = 0.6
            mock_settings.memory_entity_scan_turns = 3
            mock_dt.now.return_value = _NOW
            mock_emb.return_value.enabled = False
            result = asyncio.run(
                engine._retrieve_memories("svc1", "你好", backfill=False)
            )
        assert "用户8月26日有面试" in result[2]


class TestSelfReinforcementFix:
    """自我强化循环修正：已失效事实不再刷新 last_referenced_at / strength。"""

    def test_invalidated_facts_not_touched(self) -> None:
        """_touch_and_backfill_memory 把已失效事实从刷新列表中排除。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine.__new__(ConversationEngine)
        engine.memory_repo = MagicMock()
        engine.memory_repo.update_facts_reference = AsyncMock()

        valid = _fact("有效事实")
        invalid = _fact("已失效事实", invalid_at=_NOW - timedelta(days=1))
        emb = MagicMock()
        emb.enabled = False

        asyncio.run(engine._touch_and_backfill_memory(
            "svc1", [valid, invalid], ["有效事实", "已失效事实"], emb,
        ))
        # 只刷新有效事实，已失效事实被排除
        engine.memory_repo.update_facts_reference.assert_awaited_once()
        refreshed = engine.memory_repo.update_facts_reference.call_args.args[1]
        assert refreshed == ["有效事实"]

    def test_all_invalidated_skips_update(self) -> None:
        """全部注入事实都已失效时，不发起任何刷新调用。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine.__new__(ConversationEngine)
        engine.memory_repo = MagicMock()
        engine.memory_repo.update_facts_reference = AsyncMock()

        invalid = _fact("已失效事实", expired_at=_NOW - timedelta(hours=1))
        emb = MagicMock()
        emb.enabled = False

        asyncio.run(engine._touch_and_backfill_memory(
            "svc1", [invalid], ["已失效事实"], emb,
        ))
        engine.memory_repo.update_facts_reference.assert_not_awaited()


class TestParseFactItem:
    """focus 抽取的 new_facts 解析：兼容字符串与对象两种形态。"""

    NOW = datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc)
    TZ8 = timezone(timedelta(hours=8))

    def test_string_form_returns_none_valid_at(self) -> None:
        """旧提示词/模型输出偏差（纯字符串）仍照常入库，valid_at 为 None。"""
        content, valid_at = FocusTracker._parse_fact_item(
            "用户喜欢喝咖啡", self.TZ8, self.NOW
        )
        assert content == "用户喜欢喝咖啡"
        assert valid_at is None

    def test_object_form_with_valid_at(self) -> None:
        """对象形态解析出 valid_at（该本地日 00:00 对应的 UTC）。"""
        content, valid_at = FocusTracker._parse_fact_item(
            {"content": "用户8月26日有面试", "valid_at": "2026-08-26"},
            self.TZ8, self.NOW,
        )
        assert content == "用户8月26日有面试"
        assert valid_at is not None
        # 北京 2026-08-26 00:00 = UTC 2026-08-25 16:00
        assert valid_at == datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)

    def test_object_form_null_valid_at(self) -> None:
        """长期事实 valid_at=null → None。"""
        content, valid_at = FocusTracker._parse_fact_item(
            {"content": "用户对花生过敏", "valid_at": None},
            self.TZ8, self.NOW,
        )
        assert content == "用户对花生过敏"
        assert valid_at is None

    def test_invalid_valid_at_returns_none(self) -> None:
        """valid_at 格式非法时返回 None，事实照常入库（仅缺时间轴）。"""
        content, valid_at = FocusTracker._parse_fact_item(
            {"content": "用户有面试", "valid_at": "下周三"},
            self.TZ8, self.NOW,
        )
        assert content == "用户有面试"
        assert valid_at is None

    def test_empty_content_rejected(self) -> None:
        content, valid_at = FocusTracker._parse_fact_item(
            {"content": "  ", "valid_at": "2026-08-26"}, self.TZ8, self.NOW
        )
        assert content == ""
        assert valid_at is None

    def test_non_dict_non_str_rejected(self) -> None:
        for bad in (123, True, ["x"], None):
            content, valid_at = FocusTracker._parse_fact_item(bad, self.TZ8, self.NOW)
            assert content == ""
            assert valid_at is None
