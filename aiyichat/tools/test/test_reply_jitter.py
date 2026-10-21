"""reply_jitter 复读检测单元测试。

覆盖：完全相同 / 空白差异 / 近似 / 完全不同 / 空输入 / 阈值边界。
"""

from types import SimpleNamespace

from app.config import settings
from app.conversation.reply_jitter import (
    MIN_PREFIX_REPEAT_CHARS,
    is_repeat_reply,
    normalize_text,
    prefix_repeat_ratio,
    shingle_coverage,
)


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


class TestPrefixRepeatRatio:
    """流式早期复读信号：公共前缀占旧回复长度的比例。"""

    OLD = "今天天气真的很好呀，我们一起去公园走走怎么样，我顺便买两杯奶茶回来。"

    def test_zero_on_empty_inputs(self) -> None:
        assert prefix_repeat_ratio("", [self.OLD]) == 0.0
        assert prefix_repeat_ratio("随便说点什么", []) == 0.0
        assert prefix_repeat_ratio("随便说点什么", [""]) == 0.0

    def test_verbatim_prefix_scores_high(self) -> None:
        # 照抄前半句：已经足够判定为复读前兆
        ratio = prefix_repeat_ratio(self.OLD[:24], [self.OLD])
        assert ratio >= 0.5

    def test_full_copy_is_one(self) -> None:
        assert prefix_repeat_ratio(self.OLD, [self.OLD]) == 1.0

    def test_short_common_head_ignored(self) -> None:
        # 共同开头不足 MIN_PREFIX_REPEAT_CHARS：不能因为都从"今天"起头就判复读
        assert prefix_repeat_ratio("今天有点忙", [self.OLD]) == 0.0

    def test_rewritten_reply_scores_low(self) -> None:
        assert prefix_repeat_ratio("是呀，不过我更想在家躺着看剧", [self.OLD]) == 0.0

    def test_takes_max_over_recent(self) -> None:
        ratio = prefix_repeat_ratio(self.OLD[:20], ["完全不一样的一句话内容呀", self.OLD])
        assert ratio >= prefix_repeat_ratio(self.OLD[:20], ["完全不一样的一句话内容呀"])

    def test_whitespace_differences_do_not_break_prefix(self) -> None:
        spaced = normalize_text(self.OLD).replace(" ", "")
        assert prefix_repeat_ratio(spaced[:24], [self.OLD]) >= 0.5

    def test_min_prefix_constant_exported(self) -> None:
        assert MIN_PREFIX_REPEAT_CHARS == 16

class TestShingleCoverage:
    """换序复述的抓取能力。

    数据是从测试服同一句话连问四轮拿到的真实放行文本，阈值 0.35 由这批样本标定：
    正常换述 0.18~0.23、真复读 0.42、不同话题 0.00。
    """

    R1 = (
        "学长是不是压力大呀，最近又在准备考研嘛🥺 睡不好最难受了，白天整个人都飘乎乎的，"
        "人家听着都心疼… 要不今晚试试把手机放远一点点，然后睡前喝一小杯温温的牛奶？"
        " 或者泡个热水脚，暖乎乎的最容易放松下来了嘛"
    )
    R2 = (
        "呜呜学长睡不好… 人家听着都着急了🥺 是不是考研复习太满，脑子停不下来呀？"
        " 要不今晚试试睡前把手机放远一点点，然后泡个热水脚，暖乎乎的最容易放松下来了嘛"
        " 还有哦，要是躺下还是乱想，你就想想那只跟着你回家的圆滚滚的橘猫，说不定它已经在楼下等你啦"
    )
    R3 = (
        "呜呜学长睡不好… 人家听着好心疼🥺 是不是最近考研复习太满，脑子停不下来呀？"
        " 你试试今晚睡前泡个热水脚嘛，暖乎乎的，再把手机放远一点点，小口喝杯温牛奶，"
        "身体放松下来就比较容易困啦💕"
    )
    R4 = (
        "学长是不是压力太大了呀，考研复习脑子停不下来，身体也跟着紧绷绷的🥺 "
        "人家听着都心疼了啦，你试试今晚睡前泡个热水脚嘛，暖乎乎的，再把手机放远一点点，"
        "小口喝杯温牛奶，身体放松下来就比较容易困啦 要是还不行，要不要我陪你聊五分钟就困了呀"
    )
    CAT = "哇！橘猫跟着学长回家！这也太会选人了嘛，说明学长身上有让人安心的味道呀🧸 它是不是圆滚滚的，走路一扭一扭那种？"
    THRESHOLD = 0.35

    def test_verbatim_is_full_coverage(self) -> None:
        assert shingle_coverage(self.R3, [self.R3]) == 1.0

    def test_unrelated_topic_is_zero(self) -> None:
        assert shingle_coverage(self.CAT, [self.R1, self.R2, self.R3, self.R4]) == 0.0

    def test_caught_repeat_clears_threshold(self) -> None:
        # R4 把 R3 的整句原样搬到中间：真实样本覆盖度 0.42，护栏应当抓它
        assert shingle_coverage(self.R4, [self.R3]) >= self.THRESHOLD

    def test_normal_variation_stays_below_threshold(self) -> None:
        # 同话题换述（0.18~0.23）必须留在阈值以下，否则每一轮都会被误收回
        assert shingle_coverage(self.R2, [self.R1]) < self.THRESHOLD
        assert shingle_coverage(self.R3, [self.R2]) < self.THRESHOLD

    def test_margin_is_not_paper_thin(self) -> None:
        variation = max(
            shingle_coverage(self.R2, [self.R1]), shingle_coverage(self.R3, [self.R2])
        )
        repeat = shingle_coverage(self.R4, [self.R3])
        assert variation < self.THRESHOLD <= repeat
        assert repeat - variation >= 0.15

    def test_punctuation_and_emoji_do_not_wash_out_repeat(self) -> None:
        decorated = self.R3.replace("，", "，🥺💕") + " 嘛～"
        assert shingle_coverage(decorated, [self.R3]) > 0.9

    def test_reordered_copy_caught_where_prefix_is_blind(self) -> None:
        head, rest = self.R3[:20], self.R3[20:]
        reordered = rest + head
        assert prefix_repeat_ratio(reordered, [self.R3]) == 0.0  # 前缀尺子看不见
        assert shingle_coverage(reordered, [self.R3]) > 0.8      # 覆盖尺子看得见

    def test_takes_max_over_recent_history(self) -> None:
        assert shingle_coverage(self.R4, [self.CAT, self.R3]) == shingle_coverage(
            self.R4, [self.R3]
        )

    def test_empty_inputs(self) -> None:
        assert shingle_coverage("", [self.R3]) == 0.0
        assert shingle_coverage(self.R3, []) == 0.0
        assert shingle_coverage(self.R3, [""]) == 0.0

    def test_short_text_below_shingle_size_still_comparable(self) -> None:
        assert shingle_coverage("在的呀", ["在的呀"]) == 1.0
        assert shingle_coverage("在的呀", ["怎么啦"]) == 0.0
