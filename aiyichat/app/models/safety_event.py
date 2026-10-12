"""内容安全事件模型。

记录对话过程中触发的内容安全事件（违禁内容拦截、危机干预），
用于后台审计、风险追溯与合规留痕。
"""

from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, Field

# 事件类型：
# - crisis：疑似自伤/自杀等心理危机信号，已触发干预话术
# - forbidden：命中违禁词（政治/暴力/色情等），已拦截
# - minor_block：未成年人访问受限内容被拦截
SafetyEventType = Literal["crisis", "forbidden", "minor_block"]


class SafetyEvent(BaseModel):
    """单条内容安全事件记录。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: SafetyEventType
    platform: str = ""          # qq / wechat / web
    platform_id: str = ""       # 平台用户标识
    user_id: str = ""           # 系统内部用户ID（可能为空：未绑定场景）
    account_id: str = ""        # 归属账号
    bot_id: str = ""            # 触发所用的 Bot
    matched: str = ""           # 命中的关键词/类别（用于运营核查，不存完整原文）
    excerpt: str = ""           # 用户输入摘要（截断，最多 60 字）
    handled: str = ""           # 处置方式说明（如"已返回心理援助引导"）
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class SafetyEventInDB(SafetyEvent):
    """数据库中的安全事件记录。"""

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造事件对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        return cls(**doc)
