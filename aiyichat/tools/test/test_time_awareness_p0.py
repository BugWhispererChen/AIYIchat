"""对话时间感知 P0：抽取侧时间基点与引擎侧事件注入/审计日志的单元测试。

覆盖方案 P0 的三块改造：
1. focus_tracker：抽取时间基点由「抽取时刻」改为「消息时刻」，事件改为输出绝对日期
2. engine：pending_events 注入对话 Prompt，并区分「已过去/即将发生」
3. engine：time_ctx 审计日志字段完整
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.conversation.engine import ConversationEngine
from app.conversation.focus_tracker import _EVENTS_ADDENDUM, FocusTracker
from app.llm.prompt_assembler import TimeAudit
from app.models.message import MessageInDB
from app.models.service_memory import PendingEvent

# 北京时间偏移（与配置默认值一致）
_TZ8 = timezone(timedelta(hours=8))


def _msg(mid: str, role: str, content: str, at: datetime) -> MessageInDB:
    """构造测试消息。"""
    return MessageInDB(
        _id=mid, conversation_id="conv1", role=role, content=content, created_at=at,
    )


class TestFocusExtractTimeBase:
    """焦点抽取的时间基点必须是消息时刻而非抽取时刻（治 D6）。"""

    @staticmethod
    def _run_extract(messages: list[MessageInDB]) -> tuple[str, str]:
        """在 mock LLM 下跑一次 _extract，返回 (system_prompt, dialog_text)。"""
        tracker = FocusTracker()
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value='{"current_topic": "", "new_facts": []}'
        )
        with patch("app.conversation.focus_tracker.settings") as mock_settings, \
                patch(
                    "app.conversation.focus_tracker.get_llm_provider",
                    return_value=provider,
                ), \
                patch("app.conversation.focus_tracker.datetime") as mock_dt:
            mock_settings.default_timezone_offset = 8
            mock_settings.proactive_event_enabled = True
            mock_settings.focus_temperature = 0.0
            mock_settings.focus_max_tokens = 128
            # 抽取执行时刻：北京 2026-09-02 08:00（消息是昨晚的）
            mock_dt.now.return_value = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
            asyncio.run(tracker._extract(messages, []))
        kwargs = provider.complete.call_args.kwargs
        return kwargs["messages"][0]["content"], kwargs["messages"][1]["content"]

    def test_dialog_line_carries_each_message_date(self) -> None:
        """每条对话行前缀是该消息自己的绝对日期，而非抽取时刻的日期。"""
        history = [
            # 北京 2026-09-01 23:00 —— 昨晚
            _msg("m1", "user", "今天上班好累", datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)),
            # 北京 2026-09-02 07:50 —— 今早
            _msg("m2", "user", "刚睡醒", datetime(2026, 9, 1, 23, 50, tzinfo=timezone.utc)),
        ]
        _, dialog_text = self._run_extract(history)
        assert "[2026-09-01 23:00] 对方: 今天上班好累" in dialog_text
        assert "[2026-09-02 07:50] 对方: 刚睡醒" in dialog_text

    def test_date_hint_declares_message_time_base(self) -> None:
        """date_hint 明示当前日期只是执行时刻，相对时间须以消息自身日期为基准。"""
        history = [
            _msg("m1", "user", "明天要面试", datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)),
        ]
        system_prompt, _ = self._run_extract(history)
        assert "当前日期：2026年09月02日" in system_prompt
        assert "这是提取执行时刻，不是对话发生时刻" in system_prompt
        assert "必须以该条消息自己的日期为基准" in system_prompt

    def test_naive_message_time_does_not_break(self) -> None:
        """存量 naive 消息时间按 UTC 归一，抽取不报错。"""
        history = [_msg("m1", "user", "在吗", datetime(2026, 9, 1, 15, 0))]
        _, dialog_text = self._run_extract(history)
        assert "[2026-09-01 23:00] 对方: 在吗" in dialog_text


class TestEventsAddendum:
    """事件抽取指令改为要求绝对日期串（治 D5 的日期精度）。"""

    def test_addendum_requires_absolute_date(self) -> None:
        assert '"date"' in _EVENTS_ADDENDUM
        assert "YYYY-MM-DD" in _EVENTS_ADDENDUM

    def test_addendum_no_longer_requires_integer_days(self) -> None:
        """days_from_now 不再是提示词要求的输出字段，仅作为解析回落。"""
        assert "days_from_now" not in _EVENTS_ADDENDUM


class TestParseEventDate:
    """绝对日期串解析为 due_at（UTC aware）。"""

    NOW = datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc)  # 北京 14:30

    @pytest.mark.parametrize("raw,expected_local", [
        ("2026-09-10", datetime(2026, 9, 10, 14, 30, tzinfo=_TZ8)),
        ("2026/09/10", datetime(2026, 9, 10, 14, 30, tzinfo=_TZ8)),
        ("2026年9月10日", datetime(2026, 9, 10, 14, 30, tzinfo=_TZ8)),
        ("09-10", datetime(2026, 9, 10, 14, 30, tzinfo=_TZ8)),
    ])
    def test_parses_absolute_date(self, raw: str, expected_local: datetime) -> None:
        result = FocusTracker._parse_event_date(raw, _TZ8, self.NOW)
        assert result is not None
        assert result == expected_local.astimezone(timezone.utc)

    def test_no_year_far_past_rolls_to_next_year(self) -> None:
        """无年份且早于今天超过半年 → 视为跨年后的明年（如 9 月说「03-01」）。"""
        result = FocusTracker._parse_event_date("03-01", _TZ8, self.NOW)
        assert result is not None
        assert result.astimezone(_TZ8).year == 2027

    @pytest.mark.parametrize("raw", ["", "下周三", "不确定", "2026-13-45"])
    def test_unparsable_returns_none(self, raw: str) -> None:
        """无法解析时返回 None，由调用方回落整数逻辑，绝不抛异常。"""
        assert FocusTracker._parse_event_date(raw, _TZ8, self.NOW) is None


class TestSaveEvents:
    """_save_events 的日期解析优先级与回落。"""

    @staticmethod
    def _run(raw_events: list[dict]) -> list[PendingEvent]:
        """跑一次 _save_events，返回落库的事件列表。"""
        tracker = FocusTracker()
        tracker.memory_repo = MagicMock()
        tracker.memory_repo.add_pending_events = AsyncMock()
        asyncio.run(tracker._save_events("svc1", raw_events))
        call = tracker.memory_repo.add_pending_events.call_args
        return list(call.args[1]) if call else []

    def test_absolute_date_preferred(self) -> None:
        """date 可解析时优先用它，忽略 days_from_now。"""
        events = self._run([{
            "content": "体检", "date": "2026-09-10", "days_from_now": 999,
        }])
        assert len(events) == 1
        assert events[0].due_at.astimezone(_TZ8).date().isoformat() == "2026-09-10"

    def test_falls_back_to_days_from_now(self) -> None:
        """date 缺失/非法时回落现有整数逻辑（保持向后兼容）。"""
        events = self._run([{"content": "面试", "days_from_now": 3}])
        assert len(events) == 1
        delta = (events[0].due_at - datetime.now(timezone.utc)).total_seconds()
        assert abs(delta - 3 * 86400) < 120

    def test_both_invalid_treated_as_today(self) -> None:
        """date 与 days_from_now 均无效时当作今天，与原有保守行为一致。"""
        events = self._run([{"content": "约会", "date": "下周三", "days_from_now": "unknown"}])
        assert len(events) == 1
        delta = (events[0].due_at - datetime.now(timezone.utc)).total_seconds()
        assert abs(delta) < 120

    def test_days_clamped_to_reasonable_range(self) -> None:
        """回落整数被 clamp 到 0~365，防止模型输出异常值造出遥远事件。"""
        events = self._run([{"content": "远期", "days_from_now": 99999}])
        assert events[0].due_at.year - datetime.now(timezone.utc).year <= 1


class TestPendingEventsInjection:
    """【待跟进的事】注入：区分已过去/即将发生（治 D5）。"""

    NOW = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)

    def setup_method(self) -> None:
        self._saved = (settings.message_time_enabled, settings.default_timezone_offset)
        settings.message_time_enabled = True
        settings.default_timezone_offset = 8

    def teardown_method(self) -> None:
        settings.message_time_enabled, settings.default_timezone_offset = self._saved

    def _event(self, content: str, days: float, followed_up: bool = False) -> PendingEvent:
        """构造相对 NOW 偏移 days 天的事件。"""
        return PendingEvent(
            content=content,
            due_at=self.NOW + timedelta(days=days),
            followed_up=followed_up,
        )

    def test_past_event_marked_as_passed(self) -> None:
        """已过去的事件标注「已过去 N 天，可关心结果，不要当作即将发生」。"""
        with patch("app.conversation.engine.datetime") as mock_dt:
            mock_dt.now.return_value = self.NOW
            text = ConversationEngine._render_pending_events(
                [self._event("身份8月26日有面试", -7)]
            )
        assert "【待跟进的事】" in text
        assert "8月26日" in text
        assert "已过去 7 天" in text
        assert "不要当作即将发生" in text

    def test_future_event_marked_as_upcoming(self) -> None:
        """即将发生的事件标注「还有 N 天」。"""
        with patch("app.conversation.engine.datetime") as mock_dt:
            mock_dt.now.return_value = self.NOW
            text = ConversationEngine._render_pending_events(
                [self._event("身份9月10日体检", 8)]
            )
        assert "9月10日" in text
        assert "还有 8 天" in text

    def test_mixed_events_rendered_together(self) -> None:
        """过去与未来事件同段渲染，各自带正确的时间语义。"""
        with patch("app.conversation.engine.datetime") as mock_dt:
            mock_dt.now.return_value = self.NOW
            text = ConversationEngine._render_pending_events([
                self._event("身份8月26日有面试", -7),
                self._event("身份9月10日体检", 8),
            ])
        assert "已过去 7 天" in text and "还有 8 天" in text

    def test_time_disabled_renders_plain_list(self) -> None:
        """时间感知总开关关闭时退化为纯事件列表，不带相对天数（优雅降级）。"""
        settings.message_time_enabled = False
        with patch("app.conversation.engine.datetime") as mock_dt:
            mock_dt.now.return_value = self.NOW
            text = ConversationEngine._render_pending_events(
                [self._event("身份8月26日有面试", -7)]
            )
        assert "【待跟进的事】" in text
        assert "- 身份8月26日有面试" in text
        assert "已过去" not in text

    def test_empty_events_render_nothing(self) -> None:
        assert ConversationEngine._render_pending_events([]) == ""

    def test_select_skips_followed_up_and_naive_due(self) -> None:
        """已跟进事件与无到期时间的事件不注入；naive due_at 按 UTC 归一不报错。"""
        naive = PendingEvent(content="无时区事件", due_at=self.NOW + timedelta(days=1))
        naive.due_at = naive.due_at.replace(tzinfo=None)
        with patch("app.conversation.engine.datetime") as mock_dt:
            mock_dt.now.return_value = self.NOW
            selected = ConversationEngine._select_pending_events([
                self._event("已跟进的面试", -3, followed_up=True),
                naive,
            ])
        assert [e.content for e in selected] == ["无时区事件"]

    def test_select_caps_and_orders_by_closeness(self) -> None:
        """超出上限时优先保留与当前时间距离最近的事件。"""
        events = [self._event(f"事件{i}", days) for i, days in enumerate([100, 1, 50, 2, 80, 3])]
        with patch("app.conversation.engine.datetime") as mock_dt:
            mock_dt.now.return_value = self.NOW
            selected = ConversationEngine._select_pending_events(events)
        assert len(selected) == 5
        # 距离最近者优先：1 天、2 天、3 天在前
        assert selected[0].content == "事件1"
        assert selected[1].content == "事件3"
        assert selected[2].content == "事件5"


class TestTimeAuditLog:
    """time_ctx 审计日志字段完整（治 D11）。"""

    def test_audit_log_contains_all_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="app.conversation.engine")
        audit = TimeAudit(
            anchor="2026-09-02", now="08:00", tz="UTC+08:00",
            idle="9 小时", tagged=2, separators=1,
        )
        ConversationEngine._log_time_audit(
            audit, identity_id="u1", service_id="s1",
            conversation_id="c1", pending_event_count=2,
        )
        record = next(
            r for r in caplog.records if r.getMessage().startswith("time_ctx:")
        )
        message = record.getMessage()
        for fragment in (
            "anchor=2026-09-02", "now=08:00", "tz=UTC+08:00", "idle=9 小时",
            "tagged=2", "separators=1", "events=2",
            "user=u1", "service=s1", "conv=c1",
        ):
            assert fragment in message

    def test_audit_log_marks_missing_values(self, caplog: pytest.LogCaptureFixture) -> None:
        """未注入的字段以 '-' 留痕，便于从日志直接区分「注入失败」与「推理失败」。"""
        caplog.set_level(logging.INFO, logger="app.conversation.engine")
        ConversationEngine._log_time_audit(
            TimeAudit(), identity_id="u1", service_id="s1",
            conversation_id="c1", pending_event_count=0,
        )
        record = next(
            r for r in caplog.records if r.getMessage().startswith("time_ctx:")
        )
        message = record.getMessage()
        assert "anchor=-" in message
        assert "idle=-" in message
