"""套餐开通订单（台账）的数据库访问层。"""

from typing import Optional

from app.db.mongo import get_database
from app.models.order import OrderCreate, OrderInDB


class OrderRepository:
    """管理套餐开通订单台账。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().orders

    async def create(self, order: OrderCreate) -> str:
        """写入一条开通订单。"""
        doc = order.model_dump()
        doc["_id"] = doc.pop("id")
        from datetime import datetime, timezone
        doc["created_at"] = datetime.now(timezone.utc)
        await self._collection().insert_one(doc)
        return order.id

    async def list_recent(
        self, skip: int = 0, limit: int = 30, account_id: Optional[str] = None
    ) -> list[OrderInDB]:
        """分页查询订单台账（按创建时间倒序，可按账号过滤）。"""
        query: dict = {}
        if account_id:
            query["account_id"] = account_id
        cursor = (
            self._collection()
            .find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [OrderInDB.from_mongo(doc) async for doc in cursor]

    async def count(self, account_id: Optional[str] = None) -> int:
        """统计订单数量（可按账号过滤）。"""
        query: dict = {}
        if account_id:
            query["account_id"] = account_id
        return await self._collection().count_documents(query)

    async def delete_by_account(self, account_id: str) -> int:
        """删除某账号的订单台账（用户自助注销时调用）。

        当前订单为运营手动开通的记账流水、未接第三方支付，随账号一并物理删除；
        一旦接入真实支付，此处必须改为保留并脱敏，否则财务对账链条会断。
        """
        if not account_id:
            return 0
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def sum_amount(self, days: int = 30) -> float:
        """统计近 N 天订单记账金额合计（元）。"""
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ]
        async for row in self._collection().aggregate(pipeline):
            return round(float(row.get("total", 0.0)), 2)
        return 0.0
