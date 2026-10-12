"""情感支持策略层单元测试。

覆盖 SupportStrategySelector：
- 正面情绪/无情绪且未求助 → 不返回策略
- 求助信号 → 行动策略
- 首次负面 → 探索策略
- 连续负面 → 安慰策略
"""

from app.conversation.support_strategy import (
    SupportStage,
    SupportStrategySelector,
)


class TestSupportStrategySelector:
    """情感支持策略选择测试集。"""

    def setup_method(self) -> None:
        self.selector = SupportStrategySelector()

    def test_positive_emotion_no_strategy(self) -> None:
        """正面情绪且未求助时不启用特殊策略。"""
        assert self.selector.select("happy", 0.3, 0, "今天好开心") is None

    def test_none_emotion_no_strategy(self) -> None:
        """无情绪且未求助时不启用特殊策略。"""
        assert self.selector.select(None, 0.0, 0, "今天天气不错") is None

    def test_help_seeking_triggers_action(self) -> None:
        """明确求助 → 行动策略。"""
        plan = self.selector.select("sad", 0.6, 1, "我该怎么办呀")
        assert plan is not None
        assert plan.stage == SupportStage.ACTION

    def test_first_negative_triggers_exploration(self) -> None:
        """首次出现负面情绪 → 探索策略。"""
        plan = self.selector.select("sad", 0.7, 1, "今天好难过")
        assert plan is not None
        assert plan.stage == SupportStage.EXPLORATION

    def test_sustained_negative_triggers_comforting(self) -> None:
        """连续负面情绪（≥2轮）→ 安慰策略。"""
        plan = self.selector.select("sad", 0.6, 3, "还是很难过")
        assert plan is not None
        assert plan.stage == SupportStage.COMFORTING

    def test_instruction_non_empty(self) -> None:
        """返回策略时必须带有非空指令。"""
        plan = self.selector.select("anxious", 0.5, 1, "好焦虑")
        assert plan is not None
        assert plan.instruction
