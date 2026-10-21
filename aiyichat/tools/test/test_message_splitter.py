"""message_splitter 拆分逻辑单元测试。

覆盖：含分隔符拆分 / 无分隔符回退 / 空段过滤 / 相邻重复段去重 /
超限截断 / 空输入。
"""

from types import SimpleNamespace

import pytest

from app.conversation.message_splitter import (
    MESSAGE_DELIMITER,
    SplitResult,
    limit_messages,
    split_for_pacing,
    split_messages,
)


class TestSplitMessages:
    """split_messages 行为测试。"""

    def test_split_with_delimiter(self) -> None:
        """含分隔符的文本拆分为多条消息。"""
        text = f"你好呀{MESSAGE_DELIMITER}今天过得怎么样{MESSAGE_DELIMITER}想聊聊吗"
        result = split_messages(text)
        assert result.messages == ["你好呀", "今天过得怎么样", "想聊聊吗"]
        assert result.was_split is True

    def test_no_delimiter_returns_single(self) -> None:
        """无分隔符时回退为单条消息，was_split 为 False。"""
        text = "这是一条完整的长回复，没有任何分隔符。"
        result = split_messages(text)
        assert result.messages == [text]
        assert result.was_split is False

    def test_empty_segments_filtered(self) -> None:
        """空段（连续分隔符/首尾分隔符）被过滤。"""
        text = f"{MESSAGE_DELIMITER} 你好 {MESSAGE_DELIMITER}{MESSAGE_DELIMITER}在吗{MESSAGE_DELIMITER}"
        result = split_messages(text)
        assert result.messages == ["你好", "在吗"]
        assert result.was_split is True

    def test_max_messages_limit(self) -> None:
        """超过 max_messages 时限制条数，溢出内容回并到最后一条（不丢字）。"""
        text = MESSAGE_DELIMITER.join(["一", "二", "三", "四", "五", "六"])
        result = split_messages(text, max_messages=3)
        assert len(result.messages) == 3
        assert "".join(result.messages).count("六") == 1

    def test_only_delimiter_yields_empty_list(self) -> None:
        """纯分隔符输入拆分后为空列表。"""
        result = split_messages(MESSAGE_DELIMITER * 3)
        assert result.messages == []
        assert result.was_split is False

    def test_single_nonempty_segment_not_split(self) -> None:
        """分隔符包裹的唯一非空段：1 条消息，was_split 为 False。"""
        text = f"{MESSAGE_DELIMITER}只有一段{MESSAGE_DELIMITER}"
        result = split_messages(text)
        assert result.messages == ["只有一段"]
        assert result.was_split is False

    def test_paragraph_fallback_split(self) -> None:
        """无分隔符但含空行分段时，按空行拆分（LLM 自然分段兑底）。"""
        text = "辛苦啦，先深呼吸一下~\n\n今天先别想工作了\n\n你平时喜欢怎么放松呀？"
        result = split_messages(text)
        assert result.messages == [
            "辛苦啦，先深呼吸一下~", "今天先别想工作了", "你平时喜欢怎么放松呀？",
        ]
        assert result.was_split is True

    def test_paragraph_fallback_respects_limit(self) -> None:
        """空行分段兑底同样受 max_messages 限制，溢出部分回并保留。"""
        text = "一\n\n二\n\n三\n\n四\n\n五"
        result = split_messages(text, max_messages=2)
        assert len(result.messages) == 2
        assert "五" in result.messages[-1]

    def test_single_newline_not_split(self) -> None:
        """单个换行不触发拆分（保持单条消息）。"""
        text = "第一行\n第二行"
        result = split_messages(text)
        assert result.messages == [text]
        assert result.was_split is False

    def test_delimiter_takes_priority_over_paragraph(self) -> None:
        """显式分隔符优先于空行分段。"""
        text = f"甲{MESSAGE_DELIMITER}乙\n\n丙"
        result = split_messages(text)
        assert result.messages == ["甲", "乙\n\n丙"]
        assert result.was_split is True

    def test_split_result_is_frozen(self) -> None:
        """SplitResult 为不可变 dataclass。"""
        result = SplitResult(messages=["a"], was_split=False)
        try:
            result.was_split = True  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("SplitResult 应为 frozen，不允许赋值")

    def test_adjacent_duplicate_segments_deduped(self) -> None:
        """相邻完全相同的分段去重（防 LLM 输出 R|||R 被逐条发送）。"""
        text = f"重复段{MESSAGE_DELIMITER}重复段{MESSAGE_DELIMITER}不同段"
        result = split_messages(text)
        assert result.messages == ["重复段", "不同段"]
        assert result.was_split is True

    def test_non_adjacent_duplicates_kept(self) -> None:
        """非相邻的相同分段保留（只去相邻重复）。"""
        text = f"甲{MESSAGE_DELIMITER}乙{MESSAGE_DELIMITER}甲"
        result = split_messages(text)
        assert result.messages == ["甲", "乙", "甲"]

    def test_adjacent_duplicate_paragraphs_deduped(self) -> None:
        """空行分段兑底路径同样去相邻重复。"""
        text = "同一段\n\n同一段\n\n另一段"
        result = split_messages(text)
        assert result.messages == ["同一段", "另一段"]
        assert result.was_split is True

    def test_all_segments_same_collapses_to_single(self) -> None:
        """所有分段完全相同：收敛为单条，was_split 为 False。"""
        text = f"唯一内容{MESSAGE_DELIMITER}唯一内容{MESSAGE_DELIMITER}唯一内容"
        result = split_messages(text)
        assert result.messages == ["唯一内容"]
        assert result.was_split is False


class TestOverflowMerged:
    """超限回并：超出 max_messages 的内容并入最后一条，绝不静默丢弃。"""

    def test_delimiter_overflow_keeps_tail(self) -> None:
        text = MESSAGE_DELIMITER.join(["一", "二", "三", "四", "五", "六"])
        result = split_messages(text, max_messages=3)
        assert result.messages == ["一", "二", "三\n四\n五\n六"]
        # 内容守恒：所有非空段落都还在
        assert set("一二三四五六") <= set("".join(result.messages))

    def test_paragraph_overflow_keeps_tail(self) -> None:
        result = split_messages("一\n\n二\n\n三\n\n四\n\n五", max_messages=2)
        assert result.messages == ["一", "二\n三\n四\n五"]

    def test_max_messages_nonpositive_is_no_limit(self) -> None:
        text = MESSAGE_DELIMITER.join(["甲", "乙", "丙"])
        assert split_messages(text, max_messages=0).messages == ["甲", "乙", "丙"]


class TestSplitForPacing:
    """节奏分条：超长单条按句末边界继续切，并保护代码块/清单。"""

    def test_short_message_untouched(self) -> None:
        assert split_for_pacing("今天累坏了") == ["今天累坏了"]

    def test_delimiter_result_preserved(self) -> None:
        text = f"你好呀{MESSAGE_DELIMITER}在忙吗"
        assert split_for_pacing(text) == ["你好呀", "在忙吗"]

    def test_long_message_split_at_sentence_ends(self) -> None:
        text = "。".join(["今天加班到十点才吃上饭"] * 12) + "。"
        chunks = split_for_pacing(text, 5, auto_split_threshold=40, target_chunk_chars=30)
        assert len(chunks) > 1
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
        assert all(c.endswith("。") or len(c) < 12 for c in chunks[:-1])

    def test_respects_max_messages_with_merge(self) -> None:
        text = "。".join(["句子略长一点的内容块"] * 40) + "。"
        chunks = split_for_pacing(text, 3, auto_split_threshold=20, target_chunk_chars=20)
        assert len(chunks) == 3
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")

    def test_code_fence_not_split(self) -> None:
        code = "def f():\n    return 1\n"
        text = f"你看这段代码：```{code}```" + "。补充说明。" * 30
        chunks = split_for_pacing(text, 8, auto_split_threshold=40, target_chunk_chars=30)
        assert any("return 1" in c and "def f()" in c for c in chunks)

    def test_unclosed_fence_protected_to_end(self) -> None:
        text = "前半段。" + "很长的未闭合代码 " * 20 + "```\ninner = 1\nstill inside"
        chunks = split_for_pacing(text, 8, auto_split_threshold=40, target_chunk_chars=30)
        assert all("inner = 1" in c or "```" not in c for c in chunks)

    def test_numbered_list_kept_together(self) -> None:
        items = "".join(f"{i}. 第{i}条待办事项说明。\n" for i in range(1, 15))
        chunks = split_for_pacing(items, 8, auto_split_threshold=40, target_chunk_chars=30)
        assert any("1. 第1条" in c and "14. 第14条" in c for c in chunks)

    def test_tiny_fragments_merged_back(self) -> None:
        text = "第一句挺长的内容用来触发切分逻辑。" * 6 + "嗯。"
        chunks = split_for_pacing(text, 8, auto_split_threshold=30, target_chunk_chars=25)
        assert all(len(c) >= 3 for c in chunks)

    def test_empty_input(self) -> None:
        assert split_for_pacing("") == []

    def test_only_delimiter(self) -> None:
        assert split_for_pacing(MESSAGE_DELIMITER * 2) == []


class TestLimitMessages:
    """条数护栏：超出上限回并到最后一条，绝不丢尾（适配器以前自己切片会吞掉尾部）。"""

    def test_overflow_merged_into_last_message(self) -> None:
        msgs = ["一", "二", "三", "四"]
        out = limit_messages(msgs, 3)
        # 前 max-1 条原样保留，超出的一律回并进最后一条
        assert out == ["一", "二", "三\n四"]
        assert "".join(out).replace("\n", "") == "".join(msgs)

    def test_within_limit_unchanged(self) -> None:
        msgs = ["一", "二"]
        assert limit_messages(msgs, 5) == msgs

    def test_zero_or_negative_means_no_cap(self) -> None:
        msgs = ["一", "二", "三"]
        assert limit_messages(msgs, 0) == msgs


class TestEnginePacingWiring:
    """主链路真的接上了节奏分条：split_for_pacing 写完很久却一直没人调用。"""

    @staticmethod
    def _stub() -> object:
        from app.conversation.consistency_checker import ConsistencyChecker
        from app.conversation.response_processor import ResponseProcessor

        return SimpleNamespace(
            response_processor=ResponseProcessor(),
            consistency_checker=ConsistencyChecker(),
        )

    @staticmethod
    def _profile() -> object:
        return SimpleNamespace(
            language_style=SimpleNamespace(forbidden_words=[]),
        )

    def test_long_reply_split_into_bubbles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings
        from app.conversation.engine import ConversationEngine

        monkeypatch.setattr(settings, "pacing_split_enabled", True)
        monkeypatch.setattr(settings, "consistency_check_enabled", False)
        text = "今天加班到十点，眼睛都花了。" * 12
        _, messages, _, _ = ConversationEngine._postprocess_response(
            self._stub(), text, self._profile(), "user-1"
        )
        assert len(messages) > 1
        assert all(len(m) <= settings.pacing_auto_split_threshold for m in messages)
        # 回并而非丢弃：切完的每条拼起来仍是原话
        assert "".join(messages).replace("\n", "") == text

    def test_short_reply_stays_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings
        from app.conversation.engine import ConversationEngine

        monkeypatch.setattr(settings, "pacing_split_enabled", True)
        monkeypatch.setattr(settings, "consistency_check_enabled", False)
        _, messages, _, _ = ConversationEngine._postprocess_response(
            self._stub(), "嗯嗯，知道啦", self._profile(), "user-1"
        )
        assert messages == ["嗯嗯，知道啦"]

    def test_disabled_keeps_old_split_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """关掉开关即回退到只按 ||| 与空行拆分：一整段长文仍是一条。"""
        from app.config import settings
        from app.conversation.engine import ConversationEngine

        monkeypatch.setattr(settings, "pacing_split_enabled", False)
        monkeypatch.setattr(settings, "consistency_check_enabled", False)
        text = "今天加班到十点，眼睛都花了。" * 12
        _, messages, _, _ = ConversationEngine._postprocess_response(
            self._stub(), text, self._profile(), "user-1"
        )
        assert messages == [text]
