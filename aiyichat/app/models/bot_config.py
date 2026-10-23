"""Bot 配置的数据库模型。"""

from datetime import datetime, timezone
from typing import Literal, Optional, Self
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from app.utils.text_integrity import reject_replacement_char


class BotConfigCreate(BaseModel):
    """创建 Bot 配置。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    account_id: str
    name: str  # 必填：Bot 显示名称
    platform: Literal["qq", "wechat", "web"] = Field(default="qq")
    # 渠道来源：admin=管理员后台创建，web=C 端用户自助创建
    origin: Literal["admin", "web"] = "admin"
    app_id: str = ""
    app_secret: str = ""
    sandbox_mode: bool = True
    # 微信专用字段
    status: Literal["offline", "online", "pending_qr"] = "offline"
    qrcode_url: str = ""
    bot_token: str = ""  # iLink bot_token（扫码登录获得）
    context_token: str = ""  # iLink context_token（从消息中获取）
    token_expire_at: Optional[datetime] = None
    is_blocked: bool = False  # 是否被禁用
    block_reason: str = ""  # 禁用原因

    @field_validator("name")
    @classmethod
    def _reject_corrupted_text(cls, value: str) -> str:
        """拒绝提交端编码错误产生的乱码名称。"""
        return reject_replacement_char(value)  # type: ignore[return-value]


class BotConfigInDB(BotConfigCreate):
    """数据库中的 Bot 配置。"""

    account_id: str = ""  # 兼容旧数据：允许空值作为默认值
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造 Bot 配置对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容旧数据：origin / is_blocked / block_reason 可能不存在
        doc.setdefault("origin", "admin")
        doc.setdefault("is_blocked", False)
        doc.setdefault("block_reason", "")
        return cls(**doc)


class BotConfigUpdate(BaseModel):
    """更新 Bot 配置。"""

    account_id: Optional[str] = None
    name: Optional[str] = None
    platform: Optional[Literal["qq", "wechat", "web"]] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    sandbox_mode: Optional[bool] = None
    # 微信专用字段
    status: Optional[Literal["offline", "online", "pending_qr"]] = None
    qrcode_url: Optional[str] = None
    bot_token: Optional[str] = None
    context_token: Optional[str] = None
    token_expire_at: Optional[datetime] = None
    is_blocked: Optional[bool] = None
    block_reason: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _reject_corrupted_text(cls, value: Optional[str]) -> Optional[str]:
        """拒绝提交端编码错误产生的乱码名称。"""
        return reject_replacement_char(value)
