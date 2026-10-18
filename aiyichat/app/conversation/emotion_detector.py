"""情绪检测模块

基于关键词匹配的轻量情绪检测，支持：
- 否定词前缀过滤
- 情绪强度分级
- 历史情绪演变分析
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class EmotionResult:
    """情绪检测结果"""

    emotion: str  # "sad", "happy" 等
    intensity: float  # 0.0-1.0 (低/中/高)
    hint: str  # 中文提示词（融入强度和演变信息）


# 情绪关键词映射表
_EMOTION_PATTERNS: dict[str, list[str]] = {
    "sad": [
        r"难过", r"伤心", r"心情不好", r"不开心", r"想哭", r"哭了",
        r"痛苦", r"绝望", r"郁闷", r"沮丧", r"失落", r"悲伤",
        r"崩溃", r"受不了", r"好累", r"活着没意思", r"不想活",
        r"😢", r"😭", r"🥺", r"💔",
    ],
    "angry": [
        r"生气", r"愤怒", r"气死", r"烦死", r"讨厌", r"恨",
        r"混蛋", r"太过分", r"不公平", r"凭什么",
        r"😡", r"🤬", r"💢",
    ],
    "anxious": [
        r"焦虑", r"紧张", r"害怕", r"担心", r"不安", r"恐惧",
        r"压力大", r"睡不着", r"失眠", r"烦躁",
        r"😰", r"😥", r"😨",
    ],
    "happy": [
        r"开心", r"高兴", r"快乐", r"幸福", r"太好了", r"棒",
        r"感动", r"温暖", r"😊", r"😄", r"🥰", r"❤️",
    ],
    "lonely": [
        r"孤独", r"寂寞", r"一个人", r"没人陪", r"没有朋友",
        r"被抛弃", r"不被理解", r"没人在乎",
    ],
    "tired": [
        r"累", r"疲惫", r"困", r"没精神", r"熬夜", r"加班",
        r"撑不住", r"精疲力尽", r"想休息", r"😴", r"🥱",
    ],
    "excited": [
        r"兴奋", r"激动", r"期待", r"迫不及待", r"太棒了",
        r"好消息", r"终于", r"🎉", r"🤩", r"✨",
    ],
    "confused": [
        r"困惑", r"迷茫", r"不知道", r"搞不懂", r"不明白",
        r"怎么办", r"犹豫", r"纠结", r"🤔", r"😵",
    ],
    "nostalgic": [
        r"怀念", r"想起以前", r"回忆", r"过去", r"曾经",
        r"小时候", r"以前", r"那时候", r"📷", r"🕰️",
    ],
    "grateful": [
        r"感谢", r"谢谢", r"感激", r"多亏", r"幸好",
        r"感恩", r"谢谢有你", r"🙏", r"💝",
    ],
    "shy": [
        r"害羞", r"不好意思", r"难为情", r"脸红",
        r"尴尬", r"紧张", r"😳", r"🙈",
    ],
}

# 否定词前缀列表（匹配到关键词时检查前方是否有否定词）
_NEGATION_PREFIXES: list[str] = [
    "不是", "没有", "并不", "并非", "不太", "不算",
    "不", "没", "别", "无", "非", "未", "莫",
]

# 高强度关键词（命中则 intensity = 1.0）
_HIGH_INTENSITY_WORDS: set[str] = {
    "想死", "活着没意思", "崩溃", "绝望", "恨死", "不想活", "受不了",
}

# 低强度前缀（文本以这些开头时降权）
_LOW_INTENSITY_PREFIXES: list[str] = [
    "有点", "有些", "稍微", "略微", "一点点",
]

# 情感强度描述词映射（将离散强度值映射为更细腻的中文表达）
_INTENSITY_DESCRIPTORS: list[tuple[float, str]] = [
    (0.0, "略微"),
    (0.3, "有些"),
    (0.5, "比较"),
    (0.7, "相当"),
    (0.85, "非常"),
    (0.95, "极度"),
]


def _intensity_descriptor(intensity: float) -> str:
    """将强度值映射为中文描述词。"""
    for threshold, desc in reversed(_INTENSITY_DESCRIPTORS):
        if intensity >= threshold:
            return desc
    return "略微"

# 情绪标签到中文描述的映射（未命中专属分支时的兜底文案）
EMOTION_LABELS: dict[str, str] = {
    "sad": "悲伤低落",
    "angry": "愤怒不满",
    "anxious": "焦虑不安",
    "happy": "开心愉悦",
    "lonely": "孤独寂寞",
    "tired": "疲惫困倦",
    "excited": "兴奋激动",
    "confused": "困惑迷茫",
    "nostalgic": "怀念回忆",
    "grateful": "感激感恩",
    "shy": "害羞腼腆",
}

# 负面情绪集合（用于演变分析）
_NEGATIVE_EMOTIONS: set[str] = {
    "sad", "angry", "anxious", "lonely", "tired", "confused",
}


def _is_negated(text: str, keyword: str) -> bool:
    """检查关键词在原文中是否被否定词前缀修饰

    Args:
        text: 原始文本
        keyword: 匹配到的关键词

    Returns:
        True 表示该关键词被否定，应排除
    """
    # 找到关键词在文本中的位置
    idx = text.find(keyword)
    if idx < 0:
        return False

    # 检查关键词前方2-3个字符是否包含否定前缀
    # 取关键词前最多3个字符的子串
    prefix_start = max(0, idx - 3)
    prefix_text = text[prefix_start:idx]

    for neg in _NEGATION_PREFIXES:
        if prefix_text.endswith(neg):
            return True
    return False


def _compute_intensity(text: str, match_count: int) -> float:
    """计算情绪强度

    规则：
    - 基础强度 = min(匹配数 * 0.3, 0.6)
    - 命中高强度词 → 1.0
    - 文本以低强度前缀开头 → 降权 * 0.5
    - 最终 clamp 到 [0.1, 1.0]

    Args:
        text: 对方消息文本
        match_count: 关键词匹配数量

    Returns:
        强度值 0.1-1.0
    """
    # 基础强度
    intensity = min(match_count * 0.3, 0.6)

    # 检查高强度词
    for word in _HIGH_INTENSITY_WORDS:
        if word in text:
            intensity = 1.0
            break

    # 检查低强度前缀（文本以其开头则降权）
    for prefix in _LOW_INTENSITY_PREFIXES:
        if text.startswith(prefix):
            intensity *= 0.5
            break

    # clamp 到 [0.1, 1.0]
    return max(0.1, min(1.0, intensity))


def _get_intensity_hint(emotion: str, intensity: float) -> str:
    """根据情绪类型和强度生成中文提示

    Args:
        emotion: 情绪标签
        intensity: 强度值 0.0-1.0

    Returns:
        中文提示词
    """
    if emotion == "sad":
        if intensity >= 0.8:
            return "非常悲伤痛苦，需要特别温柔和安慰"
        elif intensity >= 0.5:
            return "悲伤低落，需要关心和理解"
        else:
            return "略有些低落"

    elif emotion == "angry":
        if intensity >= 0.8:
            return "非常愤怒激动，需要耐心倾听和安抚"
        elif intensity >= 0.5:
            return "愤怒不满，需要理解和疏导"
        else:
            return "略有些不满"

    elif emotion == "anxious":
        if intensity >= 0.8:
            return "非常焦虑恐惧，需要安全感和镇定"
        elif intensity >= 0.5:
            return "焦虑不安，需要安慰和支持"
        else:
            return "略有些紧张不安"

    elif emotion == "lonely":
        if intensity >= 0.8:
            return "极度孤独无助，需要陪伴和温暖"
        elif intensity >= 0.5:
            return "孤独寂寞，需要关注和陪伴"
        else:
            return "略感孤单"

    elif emotion == "happy":
        return "开心愉悦"

    elif emotion == "tired":
        if intensity >= 0.8:
            return "精疲力尽，需要温柔的理解和休息建议"
        elif intensity >= 0.5:
            return "疲惫困倦，需要关心和体贴"
        else:
            return "略感疲惫"

    elif emotion == "excited":
        if intensity >= 0.8:
            return "非常兴奋激动，可以一起分享喜悦"
        elif intensity >= 0.5:
            return "兴奋期待，值得庆祝"
        else:
            return "略感兴奋"

    elif emotion == "confused":
        if intensity >= 0.8:
            return "非常困惑迷茫，需要耐心引导和清晰解释"
        elif intensity >= 0.5:
            return "困惑不解，需要解答和支持"
        else:
            return "略感困惑"

    elif emotion == "nostalgic":
        return "怀念过去，可以倾听和共鸣"

    elif emotion == "grateful":
        return "心怀感激，可以回应温暖和珍惜"

    elif emotion == "shy":
        return "害羞腼腆，需要温柔鼓励"

    # 兜底
    return EMOTION_LABELS.get(emotion, "")


def _detect_core(text: str) -> tuple[str, float] | None:
    """内部核心检测逻辑：关键词匹配 + 否定词过滤 + 强度计算

    Args:
        text: 对方消息文本

    Returns:
        (emotion, intensity) 元组，未检测到返回 None
    """
    text_lower = text.lower()
    scores: dict[str, int] = {}

    for emotion, patterns in _EMOTION_PATTERNS.items():
        count = 0
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                # 获取匹配到的关键词文本
                matched_keyword = match.group()
                # 否定词前缀检测：如果被否定则跳过
                if _is_negated(text_lower, matched_keyword):
                    continue
                count += 1
        if count > 0:
            scores[emotion] = count

    if not scores:
        return None

    # 返回匹配最多的情绪
    detected = max(scores, key=lambda k: scores[k])
    intensity = _compute_intensity(text_lower, scores[detected])

    logger.debug(
        "Emotion detected: %s (intensity: %.2f, scores: %s)",
        detected, intensity, scores,
    )
    return (detected, intensity)


def detect_emotion(text: str) -> str | None:
    """基于关键词匹配的轻量情绪检测（只返回标签，不含强度与演变提示）

    Args:
        text: 对方消息文本

    Returns:
        检测到的情绪标签，如 "sad"、"happy" 等，未检测到返回 None
    """
    result = _detect_core(text)
    if result is None:
        return None
    return result[0]


def detect_emotion_with_history(
    text: str,
    recent_emotions: list[str | None],
) -> EmotionResult | None:
    """带历史上下文的情绪检测

    先进行核心检测，再结合最近的情绪历史分析情绪演变趋势，
    生成包含演变信息的提示。

    Args:
        text: 当前对方消息
        recent_emotions: 最近3-5条消息的emotion_tag列表（从旧到新）

    Returns:
        EmotionResult 或 None
    """
    core_result = _detect_core(text)
    if core_result is None:
        return None

    emotion, intensity = core_result
    hint = _get_intensity_hint(emotion, intensity)

    # 分析情绪演变
    evolution_suffix = _analyze_evolution(emotion, recent_emotions)
    if evolution_suffix:
        hint += evolution_suffix

    return EmotionResult(emotion=emotion, intensity=intensity, hint=hint)


def _analyze_evolution(
    current_emotion: str,
    recent_emotions: list[str | None],
) -> str:
    """分析情绪演变趋势

    Args:
        current_emotion: 当前检测到的情绪
        recent_emotions: 最近的情绪历史列表（从旧到新）

    Returns:
        演变描述后缀（为空字符串表示无明显演变）
    """
    if not recent_emotions:
        return ""

    # 过滤掉 None，获取有效的历史情绪
    valid_history = [e for e in recent_emotions if e is not None]

    if not valid_history:
        return ""

    # 检查：连续3条相同负面情绪 → 持续低落
    if len(valid_history) >= 3:
        last_three = valid_history[-3:]
        if (
            all(e == last_three[0] for e in last_three)
            and last_three[0] in _NEGATIVE_EMOTIONS
            and current_emotion == last_three[0]
        ):
            return "，对方情绪持续低落"

    # 检查：之前是负面情绪，现在是 happy → 情绪好转
    recent_valid = valid_history[-1] if valid_history else None
    if recent_valid in _NEGATIVE_EMOTIONS and current_emotion == "happy":
        return "，对方情绪正在好转"

    # 检查：之前是 happy，现在是负面 → 情绪突然变差
    if recent_valid == "happy" and current_emotion in _NEGATIVE_EMOTIONS:
        return "，对方情绪突然变差"

    return ""


# LLM 情绪识别提示词（关键词未命中时的降级路径，可识别隐含情绪）
_LLM_EMOTION_SYSTEM_PROMPT = (
    "你是情绪分析助手。分析对方这句话的情绪（可能是隐含的，如'又加班到十点'暗含疲惫）。\n"
    "emotion 只能取：sad/angry/anxious/happy/lonely/tired/excited/confused/nostalgic/grateful/shy/neutral 之一。\n"
    "intensity 为 0~1 的小数。hint 用一句中文描述情绪状态与关怀重点。\n"
    '严格输出 JSON：{"emotion": "tired", "intensity": 0.7, "hint": "疲惫不堪，需要被理解和关心"}'
)

_VALID_LLM_EMOTIONS: set[str] = {
    "sad", "angry", "anxious", "happy", "lonely",
    "tired", "excited", "confused", "nostalgic", "grateful", "shy",
}


async def detect_emotion_llm(text: str) -> EmotionResult | None:
    """辅助 LLM 细粒度情绪识别（关键词未命中时的降级路径）。

    用于识别无显式情绪词的隐含情绪。失败或判定为中性时返回 None。
    由调用方通过 emotion_llm_fallback_enabled 开关控制是否启用。

    Args:
        text: 对方消息文本

    Returns:
        EmotionResult 或 None
    """
    import json

    from app.config import settings
    from app.llm.provider_factory import get_llm_provider

    if not text or not text.strip():
        return None
    try:
        provider = get_llm_provider("emotion")
        raw = await provider.complete(
            messages=[
                {"role": "system", "content": _LLM_EMOTION_SYSTEM_PROMPT},
                {"role": "user", "content": text[:500]},
            ],
            temperature=settings.emotion_temperature,
            max_tokens=settings.emotion_max_tokens,
            response_format={"type": "json_object"},
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(
                ln for ln in raw.split("\n") if not ln.strip().startswith("```")
            )
        data = json.loads(raw)
        emotion = str(data.get("emotion", "")).lower()
        if emotion not in _VALID_LLM_EMOTIONS:
            return None
        intensity = float(data.get("intensity", 0.5))
        intensity = max(0.1, min(1.0, intensity))
        hint = str(data.get("hint", "")).strip() or _get_intensity_hint(
            emotion, intensity
        )
        return EmotionResult(emotion=emotion, intensity=intensity, hint=hint)
    except Exception as e:
        logger.warning("LLM 情绪识别失败: %s", e)
        return None
