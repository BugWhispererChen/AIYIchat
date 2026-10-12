"""用户的数据库模型。"""

from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    """创建用户。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    account_id: str
    name: str = ""
    platform_id: str = Field(default_factory=lambda: str(uuid4()))
    platform: Literal["qq", "wechat", "web"] = "qq"
    nickname: str = ""
    identity_description: str = ""  # 用户身份描述，供 LLM 参考
    timezone: str = ""  # IANA 时区名（如 Asia/Shanghai），空串回落全局默认偏移
    is_minor: bool = False  # 是否为未成年人（启用防沉迷与内容限制）
    is_blocked: bool = False  # 是否被禁用
    block_reason: str = ""  # 禁用原因


class UserInDB(UserCreate):
    """数据库中的用户记录。"""

    account_id: str = ""  # 兼容旧数据：允许空值作为默认值
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # bot_id → 该 Bot（appid）下的平台 openid，群聊为 group_id::member_openid。
    # QQ 的 openid 按 appid 生成，同一用户在不同 Bot 下不同；
    # 主动消息需按 Bot 路由，避免 platform_id 被最近互动的 Bot 覆盖后发错目标
    bot_openids: dict[str, str] = {}

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造用户对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容旧数据：identity_description / is_blocked / block_reason / is_minor 可能不存在
        doc.setdefault("identity_description", "")
        doc.setdefault("timezone", "")
        doc.setdefault("is_minor", False)
        doc.setdefault("is_blocked", False)
        doc.setdefault("block_reason", "")
        doc.setdefault("bot_openids", {})
        return cls(**doc)


class UserUpdate(BaseModel):
    """更新用户信息。"""

    account_id: str | None = None
    name: str | None = None
    platform_id: str | None = None
    platform: Literal["qq", "wechat", "web"] | None = None
    nickname: str | None = None
    identity_description: str | None = None
    timezone: str | None = None  # IANA 时区名，空串回落全局默认偏移
    is_minor: bool | None = None
    is_blocked: bool | None = None
    block_reason: str | None = None
