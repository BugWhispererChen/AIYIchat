from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ProfilePersona(BaseModel):
    """角色人设背景（出身、时代、外貌、价值观）。

    注意与下文的 `ProfilePersonality`（性格特征：MBTI/特质/关怀方式）区分：
    persona 回答"这个角色是谁、从哪来"，personality 回答"这个角色怎么待人"。
    """

    background: str = ""
    era: str = ""
    age_appearance: str = ""
    appearance: str = ""
    core_values: list[str] = Field(default_factory=list)
    relationships: list[dict] = Field(default_factory=list)


class ProfileLanguageStyle(BaseModel):
    """角色语言风格。"""

    vocabulary: list[str] = Field(default_factory=list)
    sentence_patterns: list[str] = Field(default_factory=list)
    tone: str = ""
    emoji_habits: list[str] = Field(default_factory=list)
    forbidden_words: list[str] = Field(default_factory=list)
    example_utterances: list[str] = Field(default_factory=list)


class ProfilePersonality(BaseModel):
    """角色性格特征。"""

    mbti_hint: str = ""
    traits: dict[str, float] = Field(default_factory=dict)
    care_style: str = ""


class ProfileDialogueStrategy(BaseModel):
    """角色对话策略。"""

    greeting: str = ""
    on_negative_emotion: str = ""
    on_positive_emotion: str = ""
    on_silence: str = ""
    boundaries: list[str] = Field(default_factory=list)
    safety_guardrails: str = ""


class ProfileLifeSegment(BaseModel):
    """角色一天里的一段作息（按**角色本地时**）。

    `start_hour` 含、`end_hour` 不含；跨午夜写成 start > end（如 22~6）。
    `days` 为空表示每天，否则 0=周一 … 6=周日。
    """

    start_hour: int = 9
    end_hour: int = 12
    activity: str = ""  # 这段时间在做什么（如"上课"）
    phrase: str = ""  # 怎么说（如"刚下课，脑子还嗡嗡的"），留空则只用 activity
    days: list[int] = Field(default_factory=list)


class ProfileLifeRoutine(BaseModel):
    """角色作息（C1）。

    只用于生成"此刻你在做什么"的处境注入，**不改变消息是否送达、也不延迟回复**——
    作息是说话的色彩，不是在线状态。
    """

    timezone: str = ""  # IANA 时区名（如 Asia/Shanghai），空则回落全局默认偏移
    segments: list[ProfileLifeSegment] = Field(default_factory=list)


class PromptProfileCreate(BaseModel):
    """创建角色档案。"""

    slug: str
    name: str
    source: str = ""
    category: Literal[
        "literary", "original", "custom",
        "celebrity", "film_tv", "public_figure",
    ] = "literary"
    avatar_url: str = ""
    # 定妆照：标准参考肖像（/api/media/{id}），用于人物一致性图片生成
    reference_image_url: str = ""
    is_active: bool = True
    visibility: Literal["public", "private"] = "public"
    account_id: str | None = None  # private 时必填
    disable_reason: str = ""  # 禁用原因
    persona: ProfilePersona = Field(default_factory=ProfilePersona)
    language_style: ProfileLanguageStyle = Field(default_factory=ProfileLanguageStyle)
    personality: ProfilePersonality = Field(default_factory=ProfilePersonality)
    dialogue_strategy: ProfileDialogueStrategy = Field(default_factory=ProfileDialogueStrategy)
    # 作息时段表：为空即完全不注入，行为与改动前一致
    life_routine: ProfileLifeRoutine = Field(default_factory=ProfileLifeRoutine)
    meta_instructions: str = ""
    voice_id: str = ""
    # 回复风格：凝练/标准/健谈，联动回复长度指令与 max_tokens/截断上限缩放
    verbosity: Literal["concise", "standard", "verbose"] = "standard"
    # 声音复刻状态："" / "active" / "failed"
    voice_clone_status: str = ""
    # 复刻时绑定的目标模型（须与合成模型一致，如 qwen3-tts-vc 系列）
    voice_clone_target_model: str = ""
    # 草稿标记：自定义创建向导中间态，不出现在角色列表，确认后才转正
    is_draft: bool = False
    # 绑定的表情包分组 ID 列表（空列表表示不发送表情包且 Prompt 不注入表情能力）
    sticker_group_ids: list[str] = Field(default_factory=list)


class PromptProfileInDB(PromptProfileCreate):
    """数据库中的角色档案记录。"""

    id: str = Field(alias="_id")
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> "PromptProfileInDB":
        """从 MongoDB 文档构造角色档案对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容旧数据：disable_reason / sticker_group_ids 可能不存在
        doc.setdefault("disable_reason", "")
        doc.setdefault("sticker_group_ids", [])
        return cls(**doc)


class PromptProfileUpdate(BaseModel):
    """更新角色档案。"""

    name: Optional[str] = None
    source: Optional[str] = None
    category: Optional[Literal[
        "literary", "original", "custom",
        "celebrity", "film_tv", "public_figure",
    ]] = None
    avatar_url: Optional[str] = None
    reference_image_url: Optional[str] = None
    is_active: Optional[bool] = None
    visibility: Optional[Literal["public", "private"]] = None
    account_id: Optional[str] = None
    persona: Optional[ProfilePersona] = None
    language_style: Optional[ProfileLanguageStyle] = None
    personality: Optional[ProfilePersonality] = None
    dialogue_strategy: Optional[ProfileDialogueStrategy] = None
    life_routine: Optional[ProfileLifeRoutine] = None
    meta_instructions: Optional[str] = None
    voice_id: Optional[str] = None
    verbosity: Optional[Literal["concise", "standard", "verbose"]] = None
    voice_clone_status: Optional[str] = None
    voice_clone_target_model: Optional[str] = None
    disable_reason: Optional[str] = None
    is_draft: Optional[bool] = None
    sticker_group_ids: Optional[list[str]] = None
