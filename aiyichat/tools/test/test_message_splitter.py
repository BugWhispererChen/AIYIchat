"""message_splitter 拆分逻辑单元测试。

覆盖：含分隔符拆分 / 无分隔符回退 / 空段过滤 / 相邻重复段去重 /
超限截断 / 空输入。
"""

from app.conversation.message_splitter import (
    MESSAGE_DELIMITER,
    SplitResult,
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
        """超过 max_messages 时截断到上限条数。"""
        text = MESSAGE_DELIMITER.join(["一", "二", "三", "四", "五", "六"])
        result = split_messages(text, max_messages=3)
        assert result.messages == ["一", "二", "三"]

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
        """空行分段兑底同样受 max_messages 限制。"""
        text = "一\n\n二\n\n三\n\n四\n\n五"
        result = split_messages(text, max_messages=2)
        assert result.messages == ["一", "二"]

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
