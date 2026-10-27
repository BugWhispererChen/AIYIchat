"""身份的数据库模型。"""

from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, Field


class IdentityCreate(BaseModel):
    """账号下的对话身份；平台字段仅描述 QQ/微信外部绑定。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    account_id: str
    name: str = ""
    platform_id: str = ""
    platform: Literal["qq", "wechat"] | None = None
    nickname: str = ""
    self_description: str = ""  # 自我描述，供 LLM 参考
    is_minor: bool = False  # 是否为未成年人（启用防沉迷与内容限制）
    is_blocked: bool = False  # 是否被禁用
    block_reason: str = ""  # 禁用原因


class IdentityInDB(IdentityCreate):
    """数据库中的身份对象。"""

    account_id: str = ""  # 兼容旧数据：允许空值作为默认值
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # bot_id → 该 Bot（appid）下的平台 openid，群聊为 group_id::member_openid。
    # QQ 的 openid 按 appid 生成，同一身份在不同 Bot 下不同；
    # 主动消息需按 Bot 路由，避免 platform_id 被最近互动的 Bot 覆盖后发错目标
    bot_openids: dict[str, str] = {}

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造身份对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 旧 web 身份在读取时即转为无平台绑定的普通身份，原 id 与业务引用不变
        if doc.get("platform") == "web":
            doc["platform"] = None
            doc["platform_id"] = ""
        doc.setdefault("platform", None)
        doc.setdefault("platform_id", "")
        doc.setdefault("self_description", "")
        doc.setdefault("is_minor", False)
        doc.setdefault("is_blocked", False)
        doc.setdefault("block_reason", "")
        doc.setdefault("bot_openids", {})
        return cls(**doc)


class IdentityUpdate(BaseModel):
    """更新身份信息。"""

    account_id: str | None = None
    name: str | None = None
    platform_id: str | None = None
    platform: Literal["qq", "wechat"] | None = None
    nickname: str | None = None
    self_description: str | None = None
    is_minor: bool | None = None
    is_blocked: bool | None = None
    block_reason: str | None = None
