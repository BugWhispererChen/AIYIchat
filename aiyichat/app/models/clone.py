"""真人聊天记录克隆的语料、风格画像与反馈模型。

语料只保存用户明确选择保留的最小情境片段，不再保存完整原始导出文件。
"""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

CloneDatasetStatus = Literal["pending", "active", "disabled", "error"]
CloneFeedbackRating = Literal["like", "dislike"]


class CloneStyleStats(BaseModel):
    """一组消息的可解释风格统计（确定性计算，不依赖 LLM）。"""

    count: int = 0
    median_chars: int = 0
    p90_chars: int = 0
    short_rate: float = 0.0
    multi_bubble_rate: float = 0.0
    emoji_rate: float = 0.0
    question_rate: float = 0.0
    ellipsis_rate: float = 0.0


class CloneStyleProfile(BaseModel):
    """角色整体与分场景风格画像。"""

    sample_count: int = 0
    text_count: int = 0
    media_count: int = 0
    overall: CloneStyleStats = Field(default_factory=CloneStyleStats)
    by_scene: dict[str, CloneStyleStats] = Field(default_factory=dict)
    common_emojis: list[str] = Field(default_factory=list)
    common_particles: list[str] = Field(default_factory=list)
    common_phrases: list[str] = Field(default_factory=list)


class CloneEpisodeDraft(BaseModel):
    """尚未绑定角色/数据集的对话片段。"""

    source_indexes: list[int] = Field(default_factory=list)
    started_at: datetime
    ended_at: datetime
    context_text: str
    target_text: str
    scene: str = "daily"
    quality: float = 0.0


class CloneEpisodeInDB(BaseModel):
    """运行期可按情境检索的对话片段。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    dataset_id: str
    account_id: str = ""
    profile_id: str = ""
    clone_version: int = 0
    source_indexes: list[int] = Field(default_factory=list)
    started_at: datetime
    ended_at: datetime
    context_text: str
    target_text: str
    scene: str = "daily"
    quality: float = 0.0
    context_embedding: list[float] = Field(default_factory=list)
    disabled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # pending 导入同一时间过期的兜底；绑定角色时清除，避免误删 active 片段
    expires_at: datetime | None = None


class CloneRoleMemory(BaseModel):
    """角色本人明确表达过的自身事实，与“对方的记忆”分属不同作用域。"""

    fid: str = Field(default_factory=lambda: uuid4().hex)
    content: str
    kind: str = "general"
    valid_at: datetime | None = None
    confidence: float = 0.5
    source_chunk_indexes: list[int] = Field(default_factory=list)
    disabled: bool = False


class CloneDatasetInDB(BaseModel):
    """一次导入产生的不可变语料版本。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    account_id: str = ""
    profile_id: str = ""
    version: int = 0
    status: CloneDatasetStatus = "pending"
    opt_in: bool = False
    source_sha256: str = ""
    target_name: str = ""
    episode_count: int = 0
    scene_counts: dict[str, int] = Field(default_factory=dict)
    style_profile: CloneStyleProfile = Field(default_factory=CloneStyleProfile)
    role_memories: list[CloneRoleMemory] = Field(default_factory=list)
    analysis_error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    activated_at: datetime | None = None
    # 仅 pending 记录有值，由 MongoDB TTL 自动清理未完成导入
    expires_at: datetime | None = None


class CloneFeedbackInDB(BaseModel):
    """用户对某轮克隆回复的“像/不像”反馈，供离线评审。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    account_id: str
    service_id: str
    conversation_id: str = ""
    message_id: str = ""
    profile_id: str = ""
    clone_version: int = 0
    rating: CloneFeedbackRating
    reasons: list[str] = Field(default_factory=list)
    edited_reply: str = ""
    status: Literal["pending", "reviewed", "rejected"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
