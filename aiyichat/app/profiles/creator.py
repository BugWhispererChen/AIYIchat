"""自定义角色创建：LLM 角色档案生成服务。

根据对方输入的角色名称与概述，调用 LLM 生成符合系统档案 schema 的
角色信息（草稿态），供管理后台自定义创建向导第一步使用。
"""

import logging
from typing import Any

from app.chat_import.analyzer import (
    _ensure_profile_structure,
    _generate_slug,
    _parse_json_response,
)
from app.llm.provider_factory import get_llm_provider
from app.models.profile import PromptProfileCreate

logger = logging.getLogger(__name__)

# LLM 生成失败时的重试次数（含首次）
_MAX_ATTEMPTS = 2

CUSTOM_PROFILE_PROMPT = """你是一位资深虚拟角色设计专家。请根据对方提供的角色名称与描述，为情感陪伴聊天系统创作一份完整、立体、可直接投入使用的虚拟角色档案。

请严格按照以下JSON schema输出：

{{
  "identity": {{
    "background": "角色背景故事（3-5句话，交代身份、经历与来到对方身边的缘由）",
    "era": "时代背景（如：现代）",
    "age_appearance": "年龄外观描述（中文，如：25岁左右的都市女性）",
    "appearance": "英文外貌描述，用于AI生成肖像照片：必须包含性别、年龄外观、发型发色、五官气质、着装风格，一句话自然描述，不要中文",
    "core_values": ["3-5个核心价值观"],
    "relationships": []
  }},
  "language_style": {{
    "vocabulary": ["5-10个角色常用词汇或口头禅"],
    "sentence_patterns": ["3-5个典型句式特征"],
    "tone": "1-2句话描述整体语气风格",
    "emoji_habits": ["常用emoji或表情习惯，无则空列表"],
    "forbidden_words": [],
    "example_utterances": ["5-8句符合人设的代表性话语示例"]
  }},
  "personality": {{
    "mbti_hint": "MBTI类型（如ENFP）",
    "traits": {{
      "warmth": 0.0到1.0,
      "humor": 0.0到1.0,
      "sensitivity": 0.0到1.0,
      "directness": 0.0到1.0,
      "optimism": 0.0到1.0,
      "independence": 0.0到1.0
    }},
    "care_style": "1-2句话描述角色关心他人的方式"
  }},
  "dialogue_strategy": {{
    "greeting": "角色的开场白（第一人称，符合人设）",
    "on_negative_emotion": "对方情绪低落时的应对策略",
    "on_positive_emotion": "对方开心时的回应方式",
    "on_silence": "对话沉默时的行为",
    "boundaries": ["3-5条角色行为边界规则"],
    "safety_guardrails": "保持友善，不涉及敏感话题，不提供专业医疗/法律建议"
  }},
  "life_routine": {{
    "timezone": "角色生活所在地的IANA时区名（如 Asia/Shanghai），不确定则空字符串",
    "segments": [
      {{"start_hour": 0到23的整数, "end_hour": 0到23的整数, "activity": "这段时间在做什么（如：上课/上班/通勤/做饭/就寝前的放松）", "phrase": "一句话补充此刻的状态（如：刚下课，脑子还嗡嗡的）", "days": []}}
    ]
  }},
  "meta_instructions": "补充的角色扮演指令（一句话概括扮演要点）"
}}

注意：
- 角色设定必须完全贴合对方描述；描述简略时合理补充细节，使角色立体饱满
- identity.appearance 必须输出英文（将直接用于AI生成角色肖像照）
- traits 的浮点数保留2位小数，评分贴合人设，不要默认中间值
- example_utterances 要充分体现语言风格，像角色本人说的话
- 直接输出JSON，不要包含markdown代码块标记或其他文字"""


def _build_profile(name: str, data: dict[str, Any]) -> PromptProfileCreate:
    """LLM 输出字典 → 草稿态 PromptProfileCreate。

    固定 category/source 等管理字段，剥离不应由 LLM 决定的字段。
    """
    data["name"] = name
    data["slug"] = _generate_slug(name)
    data["source"] = "自定义创建"
    data["category"] = "custom"
    data["is_active"] = False
    data["is_draft"] = True
    # 语音与可见性由向导后续步骤决定，不允许 LLM 代填
    for key in (
        "voice_id", "voice_clone_status",
        "voice_clone_target_model", "visibility", "account_id",
        "avatar_url", "reference_image_url",
    ):
        data.pop(key, None)
    return PromptProfileCreate(**data)


async def generate_profile(name: str, description: str) -> PromptProfileCreate:
    """调用 LLM 生成角色档案（草稿）。

    Args:
        name: 角色名称
        description: 对方对角色的概述描述

    Returns:
        草稿态 PromptProfileCreate（is_draft=True, is_active=False）

    Raises:
        ValueError: 多次尝试均失败时抛出，含原因说明
    """
    provider = get_llm_provider("chat_import")
    identity_content = f"角色名称：{name}\n角色描述：{description}"

    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = await provider.complete(
                messages=[
                    {"role": "system", "content": CUSTOM_PROFILE_PROMPT},
                    {"role": "user", "content": identity_content},
                ],
                temperature=0.7,
                max_tokens=4000,
            )
            parsed = _parse_json_response(response)
            if parsed is not None:
                parsed = _ensure_profile_structure(parsed)
                return _build_profile(name, parsed)
            logger.warning("自定义角色生成JSON解析失败(第%d次尝试)", attempt + 1)
        except Exception as e:  # 记录后重试，避免单次偶发失败阻断向导
            last_error = e
            logger.warning("自定义角色生成LLM调用失败(第%d次尝试): %s", attempt + 1, e)

    reason = str(last_error) if last_error else "LLM 输出无法解析"
    raise ValueError(f"角色信息生成失败，请返回重试（{reason}）")
