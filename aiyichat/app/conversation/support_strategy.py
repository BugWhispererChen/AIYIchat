"""情感支持策略层。

基于 ESConv / Hill 助人三阶段（探索 → 安慰 → 行动）与支持策略理念，
根据用户情绪、情绪演变（连续负面轮次）与求助信号，动态选择当前应采用的
情感支持策略，并生成注入到 Prompt 的策略指令。

纯规则实现，不依赖 LLM，零额外延迟；由 support_strategy_enabled 开关控制启用。
"""

from dataclasses import dataclass
from enum import Enum

# 负面情绪集合（与 emotion_detector 对齐）
NEGATIVE_EMOTIONS: set[str] = {
    "sad", "angry", "anxious", "lonely", "tired", "confused",
}

# 求助/寻求建议信号词
_HELP_SEEKING_MARKERS: list[str] = [
    "怎么办", "建议", "帮我", "怎么做", "如何", "该不该",
    "要不要", "有没有办法", "怎么解决", "求助",
]


class SupportStage(str, Enum):
    """情感支持阶段（Hill 助人三阶段）。"""

    EXPLORATION = "exploration"  # 探索：理解处境与感受
    COMFORTING = "comforting"    # 安慰：共情与情感支持
    ACTION = "action"            # 行动：提供建议与信息


@dataclass(frozen=True)
class SupportPlan:
    """当前轮次的情感支持计划。"""

    stage: SupportStage
    strategy: str       # 具体策略名（中文，用于日志/观测）
    instruction: str    # 注入 Prompt 的策略指令


# 各阶段的策略指令
_EXPLORE_INSTRUCTION = (
    "采用【探索】策略：先表达共情、复述并反射对方的感受，"
    "用温柔的开放式提问了解TA的处境，不要急于给建议或说教。"
)
_COMFORT_INSTRUCTION = (
    "采用【安慰】策略：以肯定和情感支持为主，认可并接纳对方的情绪，"
    "给予温暖的陪伴感，可适度自我披露以拉近距离，避免评判。"
)
_ACTION_INSTRUCTION = (
    "采用【行动】策略：在充分共情的基础上，温和地提供一到两个"
    "具体、可行的建议或新视角，尊重对方的自主选择，不强加。"
)

# 针对特定情绪的策略指令
_TIRED_INSTRUCTION = (
    "对方现在很疲惫。用温柔体贴的语气表达关心，"
    "可以询问是否需要休息，或者分享一些轻松的话题帮助放松。"
    "避免给对方增加压力或布置任务。"
)

_CONFUSED_INSTRUCTION = (
    "对方现在感到困惑。用耐心温和的语气，"
    "先确认理解了对方的困惑点，然后一步步引导或提供清晰的解释。"
    "避免使用专业术语或复杂的逻辑。"
)

_NOSTALGIC_INSTRUCTION = (
    "对方正在怀念过去。用温暖共鸣的语气，"
    "可以倾听对方的回忆，分享自己的相关经历，"
    "或者温柔地引导对方珍惜当下。"
)

_GRATEFUL_INSTRUCTION = (
    "对方正在表达感激。用温暖真诚的语气回应，"
    "可以表达'能帮到你我也很开心'，"
    "或者谦虚地说'这是我应该做的'，让对方感受到被珍惜。"
)

_SHY_INSTRUCTION = (
    "对方现在有些害羞。用温柔鼓励的语气，"
    "可以适当地给予肯定和支持，"
    "避免让对方感到尴尬或压力。"
)


class SupportStrategySelector:
    """情感支持策略选择器。"""

    def select(
        self,
        emotion: str | None,
        intensity: float,
        negative_streak: int,
        user_input: str,
    ) -> SupportPlan | None:
        """根据情绪状态选择支持策略。

        Args:
            emotion: 当前情绪标签（可能为 None）
            intensity: 情绪强度 0.0~1.0
            negative_streak: 连续负面情绪轮次（含当前轮）
            user_input: 当前用户消息

        Returns:
            SupportPlan；无需特殊策略（如平静/愉快且未求助）时返回 None
        """
        text = user_input or ""
        help_seeking = any(m in text for m in _HELP_SEEKING_MARKERS)

        # 明确求助 → 行动策略（无论情绪正负）
        if help_seeking:
            return SupportPlan(
                SupportStage.ACTION, "提供建议", _ACTION_INSTRUCTION
            )

        # 特定情绪策略
        if emotion == "tired":
            return SupportPlan(
                SupportStage.COMFORTING, "疲惫关怀", _TIRED_INSTRUCTION
            )
        if emotion == "confused":
            return SupportPlan(
                SupportStage.EXPLORATION, "困惑引导", _CONFUSED_INSTRUCTION
            )
        if emotion == "nostalgic":
            return SupportPlan(
                SupportStage.COMFORTING, "怀旧共鸣", _NOSTALGIC_INSTRUCTION
            )
        if emotion == "grateful":
            return SupportPlan(
                SupportStage.COMFORTING, "感激回应", _GRATEFUL_INSTRUCTION
            )
        if emotion == "shy":
            return SupportPlan(
                SupportStage.COMFORTING, "害羞鼓励", _SHY_INSTRUCTION
            )

        # 非负面情绪且未求助 → 无需特殊支持策略
        if emotion not in NEGATIVE_EMOTIONS:
            return None

        # 连续负面（≥2轮）→ 以安慰陪伴为主
        if negative_streak >= 2:
            return SupportPlan(
                SupportStage.COMFORTING, "肯定与安慰", _COMFORT_INSTRUCTION
            )

        # 首次出现负面/高强度 → 先探索理解
        return SupportPlan(
            SupportStage.EXPLORATION, "情感反射与提问", _EXPLORE_INSTRUCTION
        )
