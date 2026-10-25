"""TTS 语音表现力映射模块。

将对话链路的既有情绪信号（对方情绪检测结果、角色心情）转换为大模型
TTS 接口的表现力参数，使语音在情感、语气、语调、语速上趋近真人：
- 自然语言指令（instruction）：动态控制语气/情感/语速，语速描述内嵌于指令文案
- 情感标签（如 [empathetic]）：拼入合成文本头部，仅 qwen-audio-3.0/3.1 系列支持
- 音调修正（pitch_rate）：角色心情联动的微调

纯函数设计，不依赖运行时状态，便于单测。
"""

import logging

from app.conversation.mood_manager import MoodManager
from app.models.service_memory import CharacterMood

logger = logging.getLogger(__name__)

# 指令文案长度上限（官方约束：100 字符，汉字按 2 字符计）
_INSTRUCTION_MAX_LEN = 100

# 心情显著阈值：|valence| 低于该值视为平静，不产生表现力修饰
_SIGNIFICANT_VALENCE = 0.15

# 音调修正阈值与幅度：心情明显低落/愉悦时微调音调
_PITCH_TRIGGER_VALENCE = 0.3
_PITCH_DOWN = 0.95
_PITCH_UP = 1.05

# 对方情绪 → 指令文案（回复是对对方情绪的回应，以安抚/共情语气优先；
# 语速通过文案内的自然语言描述控制）
_INSTRUCTION_BY_EMOTION: dict[str, str] = {
    "sad": "用温柔共情的语气轻声安慰，语速稍慢",
    "lonely": "用温柔共情的语气轻声安慰，语速稍慢",
    "anxious": "用平和镇定的语气安抚，给人安全感，语速放缓",
    "angry": "用耐心温和的语气倾听疏导，语速放缓",
    "happy": "用轻快愉悦的语气，带一点笑意，语速稍快",
    "tired": "用温柔体贴的语气轻声关心，语速放缓，带一点心疼",
    "excited": "用轻快兴奋的语气，带明显的笑意和活力，语速稍快",
    "confused": "用耐心温和的语气，语速放缓，带一点引导感",
    "nostalgic": "用温暖怀旧的语气，语速适中，带一点感慨",
    "grateful": "用温暖真诚的语气，带一点感动和珍惜",
    "shy": "用温柔鼓励的语气，语速放缓，带一点宠溺",
}

# 对方情绪 → 情感标签（作用于其后全部文本，仅 qwen-audio-3.0/3.1 系列支持）
_EMOTION_TAG_BY_EMOTION: dict[str, str] = {
    "sad": "[empathetic]",
    "lonely": "[empathetic]",
    "anxious": "[empathetic]",
    "angry": "[serious]",
    "happy": "[excited]",
    "tired": "[gentle]",
    "excited": "[excited]",
    "confused": "[thoughtful]",
    "nostalgic": "[nostalgic]",
    "grateful": "[warm]",
    "shy": "[gentle]",
}


def _instruction_len(text: str) -> int:
    """按官方规则计算指令长度（汉字按 2 字符，其余按 1 字符）。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _truncate_instruction(text: str) -> str:
    """超长时按官方长度规则截断指令文案（防御性兜底）。"""
    if _instruction_len(text) <= _INSTRUCTION_MAX_LEN:
        return text
    total = 0
    for i, ch in enumerate(text):
        total += 2 if ord(ch) > 0x2E80 else 1
        if total > _INSTRUCTION_MAX_LEN:
            return text[:i]
    return text


def mood_rate_modifier(mood: CharacterMood | None) -> str:
    """心情联动语速的文案片段（并入指令文案控制快慢）。

    平静时返回空串；低落时放慢、愉悦时轻快。
    """
    if mood is None:
        return ""
    decayed = MoodManager.compute_decay(mood)
    if abs(decayed.valence) < _SIGNIFICANT_VALENCE:
        return ""
    return "，语速放慢" if decayed.valence < 0 else "，语速稍快"


def build_tts_instruction(
    mood: CharacterMood | None, identity_emotion: str | None
) -> str:
    """根据对方情绪与角色心情生成自然语言指令文案。

    优先级：对方情绪（回复是对对方情绪的回应）> 角色心情微调；
    均无显著信号时返回空串（不传指令，走自然音色）。
    """
    # 对方情绪优先：直接使用映射的安抚/共情语气文案（已含语速描述）
    if identity_emotion:
        instruction = _INSTRUCTION_BY_EMOTION.get(identity_emotion)
        if instruction:
            return _truncate_instruction(instruction)

    # 角色心情兜底：语气 + 语速片段组合
    if mood is None:
        return ""
    decayed = MoodManager.compute_decay(mood)
    if abs(decayed.valence) < _SIGNIFICANT_VALENCE:
        return ""
    if decayed.valence < 0:
        base = "语气略带低落，带着一点失落"
    else:
        base = "语气轻快愉悦，带着笑意"
    return _truncate_instruction(base + mood_rate_modifier(mood))


def pick_emotion_tag(
    mood: CharacterMood | None, identity_emotion: str | None
) -> str:
    """根据对方情绪选取文本头部情感标签（如 [empathetic]）。

    标签由模型族能力决定是否实际生效（见 tts_service）；无匹配返回空串。
    """
    if identity_emotion:
        return _EMOTION_TAG_BY_EMOTION.get(identity_emotion, "")
    return ""


def mood_pitch_modifier(mood: CharacterMood | None) -> float:
    """根据心情计算音调修正系数（乘在基准音调 1.0 上）。

    心情明显低落时音调微降、愉悦时微升；平静返回 1.0（承接原
    speech_rate_modifier 的心情联动职责）。
    """
    if mood is None:
        return 1.0
    decayed = MoodManager.compute_decay(mood)
    if decayed.valence <= -_PITCH_TRIGGER_VALENCE:
        return _PITCH_DOWN
    if decayed.valence >= _PITCH_TRIGGER_VALENCE:
        return _PITCH_UP
    return 1.0
