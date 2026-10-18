"""主动消息发送记录模型。"""

from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, Field


class ProactiveRecord(BaseModel):
    """
    主动消息发送记录。

    每条记录代表一次主动消息的发送尝试，用于：
    - 防止重复发送（同一身份同一阶段不重复）
    - 追踪配额使用（QQ每月4条限制）
    - 统计发送效果
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    identity_id: str  # 目标身份ID（系统内部ID）
    platform_openid: str  # 平台身份标识（openid / wechat identity_id）
    platform: Literal["qq", "wechat"] = "qq"
    bot_id: str  # 发送所用的Bot ID
    stage: int  # 发送阶段（1-4）
    content: str = ""  # 发送的消息内容
    # 内容类型：关怀型（默认，旧记录兼容）/ 联网时事分享 / 创作趣事分享
    content_type: Literal["care", "news", "trivia"] = "care"
    status: Literal["pending", "sent", "failed"] = "pending"
    error_msg: str = ""  # 失败时的错误信息
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    sent_at: datetime | None = None  # 实际发送时间


class ProactiveRecordInDB(ProactiveRecord):
    """数据库中的主动消息记录。"""

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造记录对象。"""
        if doc is None:
            raise ValueError("Document is None")
        # 写入时记录 id 被改名为 _id（见 repository.create），读回时需映射回 id，
        # 否则 id 会由 default_factory 重新生成随机值，导致按 id 删除/查询失效
        doc["id"] = str(doc.pop("_id"))
        return cls(**doc)
