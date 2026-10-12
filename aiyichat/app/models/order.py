"""套餐开通/续费订单（台账）模型。

记录每次为账号开通额度套餐的操作流水，作为运营台账与后续账单基础。
本期为运营侧手动开通，不接第三方支付，amount 为可选的记账金额。
"""

from datetime import datetime, timezone
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    """创建开通订单。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    account_id: str
    account_name: str = ""       # 冗余账号名，便于台账直接展示
    template_id: str = ""        # 关联的套餐模板ID（自定义开通可为空）
    template_name: str = ""      # 冗余套餐名
    text_limit: int = 0          # 本次开通写入的文字额度上限
    voice_limit: int = 0         # 语音额度上限
    image_limit: int = 0         # 图片额度上限
    validity_days: int | None = None  # 有效期天数（None=永久）
    amount: float = 0.0          # 记账金额（元），运营手填，可为0
    operator: str = "admin"      # 操作人（当前为单管理员）
    remark: str = ""             # 备注


class OrderInDB(OrderCreate):
    """数据库中的订单记录。"""

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造订单对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        return cls(**doc)
