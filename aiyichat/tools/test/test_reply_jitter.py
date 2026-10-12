"""reply_jitter 复读检测单元测试。

覆盖：完全相同 / 空白差异 / 近似 / 完全不同 / 空输入 / 阈值边界。
"""

from types import SimpleNamespace

from app.config import settings
from app.conversation.reply_jitter import is_repeat_reply, normalize_text


class TestNormalizeText:
    """文本归一化行为。"""

    def test_strip_and_collapse_whitespace(self) -> None:
        """首尾空白去除、连续空白折叠为单空格。"""
        assert normalize_text("  你好\n\n  世界  ") == "你好 世界"

    def test_empty(self) -> None:
        assert normalize_text("   ") == ""


class TestIsRepeatReply:
    """复读判定行为。"""

    def test_exact_match(self) -> None:
        """逐字相同判定为复读（本次故障形态）。"""
        old = "哇！看着好好吃呀！😍\n这是哪家店的外卖吗？"
        assert is_repeat_reply(old, [old]) is True

    def test_whitespace_difference_still_repeat(self) -> None:
        """仅排版空白差异不影响复读判定。"""
        old = "哇！看着好好吃呀！这是哪家店的外卖吗？"
        new = "哇！看着好好吃呀！\n这是哪家店的 外卖吗？"
        assert is_repeat_reply(new, [old]) is True

    def test_different_reply_not_repeat(self) -> None:
        """内容不同的回复不判定为复读。"""
        old = "哇！看着好好吃呀！这是哪家店的外卖吗？"
        new = "泡面？！你又在糊弄肚子是不是？"
        assert is_repeat_reply(new, [old]) is False

    def test_similar_below_threshold_not_repeat(self) -> None:
        """相似度低于阈值不判定为复读。"""
        old = "今天天气真好，要不要出去走走？"
        new = "今天天气不错，在家休息也挺好的。"
        assert is_repeat_reply(new, [old], threshold=0.9) is False

    def test_threshold_boundary(self) -> None:
        """阈值可调：仅多一字的近似文本在 0.95 命中、1.0 不命中。"""
        old = "今天天气真好，要不要出去走走？"
        new = "今天天气真好，要不要出去走走呀？"
        assert is_repeat_reply(new, [old], threshold=0.95) is True
        assert is_repeat_reply(new, [old], threshold=1.0) is False

    def test_empty_new_text_not_repeat(self) -> None:
        """空回复不判定为复读（交由上层兜底逻辑处理）。"""
        assert is_repeat_reply("", ["任意历史回复"]) is False
        assert is_repeat_reply("   ", ["任意历史回复"]) is False

    def test_empty_history_not_repeat(self) -> None:
        """无历史消息时不判定为复读。"""
        assert is_repeat_reply("任意新回复", []) is False

    def test_empty_history_entry_skipped(self) -> None:
        """历史中的空消息被跳过，不影响其余比对。"""
        old = "这条历史回复会被用来比对。"
        assert is_repeat_reply(old, ["", old]) is True

    def test_multiple_history_any_match(self) -> None:
        """多条历史中任一条命中即判定复读。"""
        old1 = "第一条历史回复内容。"
        old2 = "第二条历史回复内容。"
        assert is_repeat_reply(old2, [old1, old2]) is True


class TestEngineRepeatDetection:
    """引擎层复读检测（窗口扩大 + 分层阈值）。"""

    def _make_history(self, texts: list[str]) -> list:
        """构造模拟历史消息（assistant 角色）。"""
        return [
            SimpleNamespace(role="assistant", content=t) for t in texts
        ]

    def test_window_expansion(self) -> None:
        """窗口从 2 条扩大到配置值。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine()
        old_window = settings.reply_jitter_window
        settings.reply_jitter_window = 4
        try:
            # 4 条历史，第 4 条（最旧）与待检测文本相同，但最近 2 条不同
            history = self._make_history([
                "今天天气真好",  # 最旧的一条，与待检测文本相同
                "是啊适合出门",
                "你去哪里玩",
                "今天天气真好",  # 最近的一条，与待检测文本相同
            ])
            # 窗口=4 时应该检测到（最旧的一条命中）
            assert engine._is_repeat_of_recent("今天天气真好", history) is True

            # 窗口=2 时也能检测到（最近的一条命中）
            settings.reply_jitter_window = 2
            assert engine._is_repeat_of_recent("今天天气真好", history) is True

            # 构造最近 2 条不同的历史
            history2 = self._make_history([
                "今天天气真好",  # 最旧的一条，与待检测文本相同
                "是啊适合出门",
                "你去哪里玩",
                "明天再说",  # 最近的一条，与待检测文本不同
            ])
            # 窗口=4 时应该检测到（最旧的一条命中）
            settings.reply_jitter_window = 4
            assert engine._is_repeat_of_recent("今天天气真好", history2) is True
            # 窗口=2 时检测不到（最近 2 条都不相同）
            settings.reply_jitter_window = 2
            assert engine._is_repeat_of_recent("今天天气真好", history2) is False
        finally:
            settings.reply_jitter_window = old_window

    def test_get_repeat_similarity(self) -> None:
        """最高相似度计算。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine()
        old_window = settings.reply_jitter_window
        settings.reply_jitter_window = 4
        try:
            history = self._make_history(["你好", "今天天气不错"])
            # 与"你好"完全相同
            sim = engine._get_repeat_similarity("你好", history)
            assert sim == 1.0
            # 与"你好"部分相似
            sim = engine._get_repeat_similarity("你好呀", history)
            assert 0.5 < sim < 1.0
            # 无历史
            assert engine._get_repeat_similarity("你好", []) == 0.0
        finally:
            settings.reply_jitter_window = old_window

    def test_similarity_thresholds(self) -> None:
        """分层阈值判定。"""
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine()
        old_sim = settings.reply_jitter_similarity
        old_warn = settings.reply_jitter_similarity_warn
        settings.reply_jitter_similarity = 0.9
        settings.reply_jitter_similarity_warn = 0.75
        try:
            history = self._make_history(["今天天气真好适合出门散步"])
            # 严重复读（≥0.9）
            assert engine._is_repeat_of_recent(
                "今天天气真好适合出门散步", history
            ) is True
            # 轻微重复（0.75 ≤ sim < 0.9）→ _is_repeat_of_recent 返回 False
            # 但 _get_repeat_similarity 能捕获到
            sim = engine._get_repeat_similarity(
                "今天天气真好适合出门走走", history
            )
            assert sim >= 0.75
            assert engine._is_repeat_of_recent(
                "今天天气真好适合出门走走", history
            ) is False  # 未达 0.9 阈值
        finally:
            settings.reply_jitter_similarity = old_sim
            settings.reply_jitter_similarity_warn = old_warn
