"""C 端网页用户凭据的数据库模型。

凭据独立成集合（不放进 `users`）：`user` 对象会被 prompt 组装与记忆提取全量消费，
密码哈希不应进入 LLM 上下文。
"""

from datetime import datetime, timezone
from typing import Literal, Optional, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# 身份状态：pending 待审核 / active 可用 / rejected 审核拒绝 / disabled 管理员封禁
IdentityStatus = Literal["pending", "active", "rejected", "disabled"]

# 当前合规文本版本：条款或隐私政策改版时递增，注册留痕按版本记录
TERMS_VERSION = "v1.0"
PRIVACY_VERSION = "v1.0"


def normalize_username(username: str) -> str:
    """用户名规范化：去空白并转小写。

    集合内 `username` 存规范化值并建唯一索引，使大小写不同的同名注册在数据库层
    直接被拒绝（而不是靠应用层先查后插的竞态判断）。
    """
    return username.strip().lower()


class WebConsent(BaseModel):
    """注册合规留痕：条款/隐私版本确认与年龄确认。"""

    terms_version: str = TERMS_VERSION
    terms_accepted_at: Optional[datetime] = None
    privacy_version: str = PRIVACY_VERSION
    privacy_accepted_at: Optional[datetime] = None
    age_confirmed: bool = False  # 用户勾选"我已满 14 周岁"
    age_confirmed_at: Optional[datetime] = None


class WebIdentityCreate(BaseModel):
    """创建网页身份。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = ""  # 关联 users.id；注册先占用户名，四件套建齐后回填
    username: str
    password_hash: str
    status: IdentityStatus = "pending"
    failed_count: int = 0  # 连续登录失败次数（成功后清零）
    locked_until: Optional[datetime] = None  # 登录锁定到期时间
    reviewed_by: str = ""  # 审核操作人（管理员用户名）
    reviewed_at: Optional[datetime] = None  # 审核时间（也用于拒绝冷却判定）
    review_note: str = ""  # 审核备注/拒绝理由
    created_ip: str = ""
    consent: WebConsent = Field(default_factory=WebConsent)


class WebIdentityInDB(WebIdentityCreate):
    """数据库中的网页身份记录。"""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造网页身份对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容存量数据：可选字段缺失时回落默认值
        doc.setdefault("user_id", "")
        doc.setdefault("failed_count", 0)
        doc.setdefault("locked_until", None)
        doc.setdefault("reviewed_by", "")
        doc.setdefault("reviewed_at", None)
        doc.setdefault("review_note", "")
        doc.setdefault("created_ip", "")
        doc.setdefault("updated_at", doc.get("created_at", datetime.now(timezone.utc)))
        consent = doc.get("consent")
        doc["consent"] = WebConsent(**consent) if isinstance(consent, dict) else WebConsent()
        return cls(**doc)

    @property
    def is_active(self) -> bool:
        """是否处于可用状态（仅 active 允许登录与会话签发）。"""
        return self.status == "active"

    @property
    def is_locked(self) -> bool:
        """登录失败锁定是否仍在生效。"""
        locked_until = self.locked_until
        if locked_until is None:
            return False
        # 兼容 PyMongo 返回的 naive datetime：统一按 UTC 处理后再比较
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        return locked_until > datetime.now(timezone.utc)
