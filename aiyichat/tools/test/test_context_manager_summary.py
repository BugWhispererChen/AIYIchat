"""ContextManager 异步摘要调度判定单元测试。

覆盖 schedule_summary_if_needed 的触发条件：
- 总消息数未超过 summary_trigger_count 时不生成
- 新增待摘要消息数未超过已覆盖数时不生成
- 满足条件时调用 _generate_incremental_summary

以及增量摘要的时间脉络保障（_generate_incremental_summary）：
- 摘要 system prompt 含覆盖时间范围与“禁止相对表述”硬要求
- 对话行日期前缀跨年带年份
- 持久化时写入 summary_start_at / summary_end_at
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.conversation.context_manager import ContextManager
from app.models.message import MessageInDB


def _make_manager() -> ContextManager:
    """构造 ContextManager 并以 mock 替换其仓库依赖。"""
    cm = ContextManager()
    cm.message_repo = AsyncMock()
    cm.conversation_repo = AsyncMock()
    return cm


class TestScheduleSummaryIfNeeded:
    """schedule_summary_if_needed 判定逻辑测试集。"""

    @patch("app.conversation.context_manager.settings")
    def test_skip_when_below_trigger(self, mock_settings) -> None:
        """总消息数未超过触发阈值时不应生成摘要。"""
        mock_settings.context_window_size = 20
        mock_settings.summary_trigger_count = 40
        cm = _make_manager()
        cm.message_repo.count_by_conversation.return_value = 30
        cm._generate_incremental_summary = AsyncMock()

        asyncio.run(cm.schedule_summary_if_needed("conv1", None, 0))

        cm._generate_incremental_summary.assert_not_called()

    @patch("app.conversation.context_manager.settings")
    def test_skip_when_no_new_messages(self, mock_settings) -> None:
        """待摘要消息数未超过上次已覆盖数时不应重新生成。"""
        mock_settings.context_window_size = 20
        mock_settings.summary_trigger_count = 40
        cm = _make_manager()
        # total=50, window=20 -> messages_to_summarize=30, 已覆盖=30 => 不触发
        cm.message_repo.count_by_conversation.return_value = 50
        cm._generate_incremental_summary = AsyncMock()

        asyncio.run(cm.schedule_summary_if_needed("conv1", "旧摘要", 30))

        cm._generate_incremental_summary.assert_not_called()

    @patch("app.conversation.context_manager.settings")
    def test_generate_when_needed(self, mock_settings) -> None:
        """满足条件时应调用增量摘要生成。"""
        mock_settings.context_window_size = 20
        mock_settings.summary_trigger_count = 40
        cm = _make_manager()
        # total=60, window=20 -> messages_to_summarize=40 > 已覆盖=30 => 触发
        cm.message_repo.count_by_conversation.return_value = 60
        cm._generate_incremental_summary = AsyncMock()

        asyncio.run(cm.schedule_summary_if_needed("conv1", "旧摘要", 30))

        cm._generate_incremental_summary.assert_awaited_once()
        # 校验传入的 new_summary_count = total - window = 40
        args = cm._generate_incremental_summary.call_args.args
        assert args[-1] == 40

    @patch("app.conversation.context_manager.settings")
    def test_high_emotion_widens_window(self, mock_settings) -> None:
        """情绪强烈时窗口扩大为 1.5 倍，影响待摘要消息数。"""
        mock_settings.context_window_size = 20
        mock_settings.summary_trigger_count = 40
        cm = _make_manager()
        # total=60, emotion>=0.7 -> window=30 -> messages_to_summarize=30
        cm.message_repo.count_by_conversation.return_value = 60
        cm._generate_incremental_summary = AsyncMock()

        asyncio.run(
            cm.schedule_summary_if_needed(
                "conv1", "旧摘要", 20, emotion_intensity=0.8
            )
        )

        cm._generate_incremental_summary.assert_awaited_once()
        args = cm._generate_incremental_summary.call_args.args
        assert args[-1] == 30  # 60 - 30(扩大后窗口)

    def test_get_context_returns_two_tuple(self) -> None:
        """get_context 应仅返回 (messages, summary) 二元组，不阻塞生成摘要。"""
        cm = _make_manager()
        cm.message_repo.get_recent.return_value = ["m1", "m2"]

        result = asyncio.run(cm.get_context("conv1", "已有摘要"))

        assert result == (["m1", "m2"], "已有摘要")


def _msg(mid: str, role: str, content: str, at: datetime) -> MessageInDB:
    """构造测试消息。"""
    return MessageInDB(
        _id=mid, conversation_id="conv1", role=role, content=content, created_at=at,
    )


class TestSummaryTimeline:
    """增量摘要的时间脉络保障（治 D8：摘要丢失时间线）。"""

    # 跨 3 天的 6 条消息（前 4 条待摘要，后 2 条在窗口内），均为北京时间上午
    _BASE = datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc)  # 北京 08-01 09:00

    def _messages(self) -> list[MessageInDB]:
        return [
            _msg("a1", "user", "今天面试好紧张", self._BASE),
            _msg("a2", "assistant", "加油", self._BASE + timedelta(minutes=5)),
            _msg("a3", "user", "昨天体检报告出来了", self._BASE + timedelta(days=1)),
            _msg("a4", "user", "明天要去旅行", self._BASE + timedelta(days=2)),
            _msg("a5", "user", "在吗", self._BASE + timedelta(days=2, hours=1)),
            _msg("a6", "assistant", "在的", self._BASE + timedelta(days=2, hours=1)),
        ]

    def _run(self, current_summary: str | None) -> tuple[MagicMock, AsyncMock]:
        """在 mock 依赖下跑一次增量摘要，返回 (provider.complete, set_summary)。"""
        cm = _make_manager()
        cm.message_repo.get_all_for_conversation = AsyncMock(return_value=self._messages())
        provider = MagicMock()
        provider.complete = AsyncMock(return_value="8月1日面试，8月2日拿到体检报告，8月3日计划旅行。")
        with patch("app.conversation.context_manager.settings") as mock_settings, \
                patch(
                    "app.conversation.context_manager.get_llm_provider",
                    return_value=provider,
                ), \
                patch("app.conversation.context_manager.datetime") as mock_dt:
            mock_settings.message_time_enabled = True
            mock_settings.default_timezone_offset = 8
            mock_settings.summary_temperature = 0.3
            mock_settings.summary_max_tokens = 1024
            # 冻结“当前时间”为 2026-08-05，使年份规则断言与机器时钟无关
            mock_dt.now.return_value = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)
            # window_size=2, last_summary_count=0, new_summary_count=4
            asyncio.run(cm._generate_incremental_summary("conv1", current_summary, 0, 2, 4))
        return provider.complete, cm.conversation_repo.set_summary

    def test_summary_prompt_requires_timeline(self) -> None:
        """摘要 system prompt 必须含覆盖时间范围与禁止相对表述的硬要求。"""
        complete, _ = self._run(None)
        messages = complete.call_args.kwargs["messages"]
        system_content = messages[0]["content"]
        assert "必须保留时间脉络" in system_content
        # 覆盖范围按北京时区渲染：8月1日 至 8月3日
        assert "8月1日" in system_content and "8月3日" in system_content
        assert "禁止使用“今天/昨天/明天/最近”等相对表述" in system_content
        assert "写明绝对日期" in system_content

    def test_summary_prompt_keeps_timeline_when_merging(self) -> None:
        """合并旧摘要时同样携带时间脉络要求。"""
        complete, _ = self._run("旧摘要内容")
        system_content = complete.call_args.kwargs["messages"][0]["content"]
        assert "已有摘要与新增对话合并" in system_content
        assert "必须保留时间脉络" in system_content

    def test_dialog_line_carries_date_prefix(self) -> None:
        """待摘要对话行带 [MM-DD] 日期前缀，使 LLM 能保留时间脉络。"""
        complete, _ = self._run(None)
        identity_content = complete.call_args.kwargs["messages"][1]["content"]
        assert "[08-01] 身份: 今天面试好紧张" in identity_content
        assert "[08-02] 身份: 昨天体检报告出来了" in identity_content
        assert "[08-03] 身份: 明天要去旅行" in identity_content

    def test_summary_range_persisted(self) -> None:
        """持久化时写入摘要覆盖的首尾时间，供注入时渲染时间坐标。"""
        _, set_summary = self._run(None)
        set_summary.assert_awaited_once()
        kwargs = set_summary.call_args.kwargs
        assert kwargs["summary_message_count"] == 4
        assert kwargs["summary_start_at"] == self._BASE
        # 覆盖段末条为 a4（北京 08-03 09:00 == UTC 08-03 01:00）
        assert kwargs["summary_end_at"] == self._BASE + timedelta(days=2)

    def test_dialog_line_cross_year_carries_year(self) -> None:
        """跨年消息的日期前缀带年份，否则模型无法区分去年与今年的同月同日。"""
        cm = _make_manager()
        msgs = [
            # 北京 2025-12-31 23:00
            _msg("y1", "user", "跨年快乐", datetime(2025, 12, 31, 15, 0, tzinfo=timezone.utc)),
            # 北京 2026-01-01 11:00：与上条跨越年界线
            _msg("y2", "user", "新年新计划", datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)),
            _msg("y3", "user", "在吗", datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)),
            _msg("y4", "assistant", "在的", datetime(2026, 1, 1, 4, 1, tzinfo=timezone.utc)),
        ]
        cm.message_repo.get_all_for_conversation = AsyncMock(return_value=msgs)
        provider = MagicMock()
        provider.complete = AsyncMock(return_value="跨年夜到元旦的对话摘要内容。")
        with patch("app.conversation.context_manager.settings") as mock_settings, \
                patch(
                    "app.conversation.context_manager.get_llm_provider",
                    return_value=provider,
                ), \
                patch(
                    "app.conversation.context_manager.datetime"
                ) as mock_dt:
            mock_settings.message_time_enabled = True
            mock_settings.default_timezone_offset = 8
            mock_settings.summary_temperature = 0.3
            mock_settings.summary_max_tokens = 1024
            # 冻结“当前年”为 2026（北京 2026-01-02）
            mock_dt.now.return_value = datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc)
            # window_size=2, last=0, new_summary_count=2 → 只摘要前两条
            asyncio.run(cm._generate_incremental_summary("conv1", None, 0, 2, 2))
        identity_content = provider.complete.call_args.kwargs["messages"][1]["content"]
        assert "[2025-12-31] 身份: 跨年快乐" in identity_content
        assert "[2026-01-01] 身份: 新年新计划" in identity_content
