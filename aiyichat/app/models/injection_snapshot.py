"""记忆注入快照的数据模型。

每轮对话组装 Prompt 后记录一次注入明细（哪些记忆被注入、各占多少 token、
上下文信号等），用于记忆系统的可观测性调优。
快照为只读审计数据，写入采用 fire-and-forget，不阻塞主链路。
"""

from datetime import datetime, timezone
from typing import Self

from pydantic import BaseModel, Field


class InjectedFact(BaseModel):
    """单条被注入的记忆事实明细。"""

    content: str  # 记忆内容
    category: str = "general"  # 分类
    score: float = 0.0  # 注入时的相关性评分
    token_cost: int = 0  # 该条记忆的 token 估算


class InjectionSnapshotInDB(BaseModel):
    """数据库中的记忆注入快照记录。"""

    id: str = Field(alias="_id")
    service_id: str  # 对话服务 ID（共享组为组 key）
    conversation_id: str = ""  # 会话 ID
    injected_facts: list[InjectedFact] = Field(default_factory=list)
    # Prompt Breakdown：各部分 token 估算
    system_prompt_tokens: int = 0
    memory_tokens: int = 0  # 记忆注入段落 token
    history_tokens: int = 0  # 历史对话 token
    summary_tokens: int = 0  # 摘要 token
    total_tokens: int = 0  # 整体估算
    # 上下文信号
    emotion_hint: str = ""
    current_topic: str = ""
    mood_label: str = ""
    reply_text: str = ""  # 本轮 AI 回复（清洗后），用于离线命中判定
    # 人际关系网络：本轮注入的实体明细
    injected_entities: list[str] = Field(default_factory=list)  # 实体名列表
    entity_tokens: int = 0  # 实体注入段落 token 估算
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造快照对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        doc.setdefault("injected_facts", [])
        doc.setdefault("system_prompt_tokens", 0)
        doc.setdefault("memory_tokens", 0)
        doc.setdefault("history_tokens", 0)
        doc.setdefault("summary_tokens", 0)
        doc.setdefault("total_tokens", 0)
        doc.setdefault("emotion_hint", "")
        doc.setdefault("current_topic", "")
        doc.setdefault("mood_label", "")
        doc.setdefault("reply_text", "")
        doc.setdefault("conversation_id", "")
        doc.setdefault("injected_entities", [])
        doc.setdefault("entity_tokens", 0)
        return cls(**doc)
