"""TTS 模型族注册表。

阿里云百炼各语音合成模型的音色选择机制不同，按模型名前缀划分为"模型族"，
每族定义：调用接口类型、默认音色、音色兼容规则、是否支持语速参数。

关键约束：模型与音色严格成套，跨族混用必然失败（如 cosyvoice-v1 配
longshange_v3 会静默返回空数据）。因此提供音色兜底能力：人设配置的音色
与当前模型族不匹配时，自动回退到该族默认音色。
"""

from dataclasses import dataclass

# 接口类型
API_WEBSOCKET = "websocket"  # CosyVoice：dashscope.audio.tts_v2.SpeechSynthesizer
API_MULTIMODAL = "multimodal"  # Qwen-TTS：dashscope.MultiModalConversation（返回音频URL）

# 自定义创建向导的预置音色候选（音色须与当前 TTS 模型成套，
# 来源：阿里云 Qwen-Audio-TTS 官方音色列表）。
# 默认 TTS 模型为 qwen-audio-3.1-tts-plus，系统音色必须带 _v3.1 后缀；
# 基础音色采用 qwen-audio-3.0-tts-plus-{后缀} 命名（实测 3.1 仍兼容），
# resolve_voice 按复刻音色规则直接放行；如需增删音色，集中在此处维护。
PRESET_VOICE_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("longanlingxin_v3.1", "龙安灵心 3.1（女）", "知心温暖音 · 社交陪伴旗舰音色"),
    ("longanqian_v3.1", "龙安谦 3.1（男）", "清澈少年音 · 通用自然"),
    ("longanhuan_v3.1", "龙安欢 3.1（女）", "明亮亲切 · 多方言多语种"),
    ("longanfengyue_v3.1", "龙安风悦 3.1（女）", "温柔治愈 · 多方言多语种"),
    ("xunanchuan_v3.1", "许南川 3.1（男）", "沉稳男声 · 内容创作"),
    ("qwen-audio-3.0-tts-plus-longyinghaikai", "龙应海凯（男）", "标准通用型客服 · 稳重专业"),
)


@dataclass(frozen=True)
class TTSModelFamily:
    """一个 TTS 模型族的调用约定。"""

    name: str  # 族名（日志用）
    api_kind: str  # 接口类型（API_WEBSOCKET / API_MULTIMODAL）
    default_voice: str  # 该族默认音色
    voice_suffixes: tuple[str, ...]  # 兼容音色的后缀标记（空串=无后缀要求）
    voice_prefixes: tuple[str, ...]  # 兼容音色的前缀标记（如 Qwen 用大写英文名）
    supports_instruction: bool  # 是否支持自然语言指令控制（情感/语气/语速）
    supports_emotion_tags: bool  # 是否支持文本内嵌情感标签（如 [empathetic]）


# CosyVoice 预置音色统一以 long/loong 开头；后缀约定：v1 无后缀，v2 为 _v2，v3 为 _v3

_FAMILIES: tuple[TTSModelFamily, ...] = (
    TTSModelFamily(
        name="qwen-audio-v31",
        api_kind=API_WEBSOCKET,
        default_voice="longanlingxin_v3.1",  # 中文社交陪伴旗舰音色
        voice_suffixes=("_v3.1",),
        voice_prefixes=("long", "loong", "xunanchuan"),
        supports_instruction=True,  # qwen-audio-3.1 系列支持自然语言指令控制
        supports_emotion_tags=True,  # 仅该系列支持文本内嵌情感标签/富语言标签
    ),
    TTSModelFamily(
        name="qwen-audio",
        api_kind=API_WEBSOCKET,
        default_voice="longanlingxin",  # 中文社交陪伴旗舰音色
        voice_suffixes=("",),  # 音色后缀不规整（如 _v3.6），单独校验见 resolve_voice
        voice_prefixes=("long", "loong"),
        supports_instruction=True,  # qwen-audio-3.0 系列支持自然语言指令控制
        supports_emotion_tags=True,  # 仅该系列支持文本内嵌情感标签/富语言标签
    ),
    TTSModelFamily(
        name="cosyvoice-v3",
        api_kind=API_WEBSOCKET,
        default_voice="longxiaoxia_v3",
        voice_suffixes=("_v3",),
        voice_prefixes=("long", "loong"),
        supports_instruction=True,  # v3-flash/v3.5 支持（复刻音色可任意指令）
        supports_emotion_tags=False,
    ),
    TTSModelFamily(
        name="cosyvoice-v2",
        api_kind=API_WEBSOCKET,
        default_voice="longxiaochun_v2",
        voice_suffixes=("_v2",),
        voice_prefixes=("long", "loong"),
        supports_instruction=False,
        supports_emotion_tags=False,
    ),
    TTSModelFamily(
        name="cosyvoice-v1",
        api_kind=API_WEBSOCKET,
        default_voice="longxiaochun",
        voice_suffixes=("",),  # v1 音色无版本后缀
        voice_prefixes=("long", "loong"),
        supports_instruction=False,
        supports_emotion_tags=False,
    ),
    TTSModelFamily(
        name="qwen-tts",
        api_kind=API_MULTIMODAL,
        default_voice="Cherry",
        voice_suffixes=("",),
        voice_prefixes=(),  # Qwen 音色为大写开头英文名，无 long 前缀
        supports_instruction=False,  # 仅 instruct 系列支持，运行时按模型名判定见下
        supports_emotion_tags=False,
    ),
)


def detect_family(model: str) -> TTSModelFamily:
    """按模型名识别所属模型族，未匹配时回退 cosyvoice-v1（保守兼容）。"""
    m = (model or "").lower()
    if m.startswith("qwen-audio") and "tts" in m:
        if m.startswith("qwen-audio-3.1"):
            return _FAMILIES[0]
        return _FAMILIES[1]
    if m.startswith("qwen") and "tts" in m:
        return _FAMILIES[5]
    if "cosyvoice-v3" in m:
        return _FAMILIES[2]
    if "cosyvoice-v2" in m:
        return _FAMILIES[3]
    if "cosyvoice-v1" in m:
        return _FAMILIES[4]
    # 未识别模型按 cosyvoice-v1 处理（保守回退，保证可用性）
    return _FAMILIES[4]


def supports_instruction(family: TTSModelFamily, model: str) -> bool:
    """判断当前模型是否支持自然语言指令控制。

    多数模型族按族标志判定；qwen-tts 族内仅 instruct 系列（如
    qwen3-tts-instruct-flash）支持，需按模型名运行时判定。
    """
    if family.name == "qwen-tts":
        return "instruct" in (model or "").lower()
    return family.supports_instruction


# qwen-audio-3.0 族官方系统音色（仅收录 qwen-audio-3.0-tts-plus 支持的，
# 见官方 Qwen-Audio-TTS 音色列表；flash 专属音色不在其中）；
# longanqian 为官方基础音色（清澈少年向），已实测可用（2026-09 探测验证）
_QWEN_AUDIO_SYSTEM_VOICES = frozenset(
    {"longanlingxin", "longanlufeng", "longanqian"}
)

# qwen-audio-3.1 族官方系统音色（2026-10 在测试服逐个探测验证）：
# 3.1 只接受带 _v3.1 后缀的系统音色，裸 3.0 音色会报 Engine error 411
_QWEN_AUDIO_V31_SYSTEM_VOICES = frozenset(
    {
        "longanlingxin_v3.1",
        "longanqian_v3.1",
        "longanhuan_v3.1",
        "longanfengyue_v3.1",
        "xunanchuan_v3.1",
    }
)

# 3.0 代音色 → 3.1 可用音色的迁移映射：
# - 3.1 没有 longanlufeng / longanhuan_v3.6 对应版本，映射到音色最接近的
#   3.1 系统音色（longanlufeng 为男声，落到 xunanchuan_v3.1）
# - 3.0 基础/复刻音色（qwen-audio-3.0-...）实测可跨代使用，不在此表内
_QWEN_AUDIO_V31_LEGACY_VOICES = {
    "longanlingxin": "longanlingxin_v3.1",
    "longanqian": "longanqian_v3.1",
    "longanhuan": "longanhuan_v3.1",
    "longanhuan_v3.6": "longanhuan_v3.1",
    "longanlufeng": "xunanchuan_v3.1",
    "longanlufeng_v3.1": "xunanchuan_v3.1",
}


def resolve_voice(family: TTSModelFamily, voice_id: str) -> tuple[str, bool]:
    """校验人设音色与模型族是否匹配，不匹配时回退族默认音色。

    Returns:
        (最终音色, 是否发生了兜底回退)
    """
    vid = (voice_id or "").strip()
    if not vid:
        return family.default_voice, False

    # qwen-audio-3.1 族：系统音色白名单 + 3.0 代音色迁移映射；
    # 其余（如 CosyVoice 裸音色）一律回退默认，避免 Engine error 411
    if family.name == "qwen-audio-v31":
        if vid in _QWEN_AUDIO_V31_SYSTEM_VOICES:
            return vid, False
        # 基础/复刻音色 ID（qwen-audio-3.0-...）实测可跨代使用
        if "qwen-audio" in vid.lower():
            return vid, False
        mapped = _QWEN_AUDIO_V31_LEGACY_VOICES.get(vid)
        if mapped is not None:
            return mapped, False
        return family.default_voice, True

    # 复刻/设计的自定义音色 ID 形如 cosyvoice-v3-myvoice-xxx 或
    # qwen-audio-3.0-tts-plus-myvoice-xxx，含族标记即视为匹配
    if "cosyvoice" in vid.lower() or "qwen-audio" in vid.lower():
        return vid, False

    # qwen-audio 族：仅支持官方系统音色、带版本号的预置音色（如
    # longanhuan_v3.6）与复刻音色；CosyVoice 裸音色/_v2/_v3 音色不兼容
    # （会报 Engine error 411），回退族默认音色
    if family.name == "qwen-audio":
        if "_v3." in vid or vid in _QWEN_AUDIO_SYSTEM_VOICES:
            return vid, False
        return family.default_voice, True

    # Qwen-TTS 族：预置音色为大写英文名（Cherry/Serena/Ethan...），
    # 复刻音色为自定义名（如 guanyu），仅拒绝 CosyVoice 形态音色
    if family.api_kind == API_MULTIMODAL:
        if vid.lower().startswith(("long", "loong")):
            return family.default_voice, True
        return vid, False

    # CosyVoice 族：必须带 long/loong 前缀且版本后缀匹配
    if not vid.lower().startswith(family.voice_prefixes):
        return family.default_voice, True
    if family.voice_suffixes == ("",):
        # v1 族：音色不应带任何版本后缀
        if vid.endswith(("_v2", "_v3")):
            return family.default_voice, True
    elif not vid.endswith(family.voice_suffixes):
        return family.default_voice, True
    return vid, False
