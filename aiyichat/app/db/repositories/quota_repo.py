"""账号额度数据库访问层。"""

from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import get_database
from app.models.quota import (
    AccountQuotaCreate,
    AccountQuotaInDB,
    AccountQuotaUpdate,
    QuotaEntry,
)


class QuotaRepository:
    """管理账号按量计费额度。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().account_quotas

    async def find_by_account(
        self, account_id: str
    ) -> Optional[AccountQuotaInDB]:
        """查询指定账号的额度记录。"""
        doc = await self._collection().find_one({"account_id": account_id})
        if doc is None:
            return None
        return AccountQuotaInDB.from_mongo(doc)

    async def create_or_update(
        self, account_id: str, update: AccountQuotaUpdate
    ) -> AccountQuotaInDB:
        """创建或更新账号额度记录。"""
        update_data = update.model_dump(exclude_none=True)
        update_data["updated_at"] = datetime.now(timezone.utc)

        doc = await self._collection().find_one_and_update(
            {"account_id": account_id},
            {
                "$set": update_data,
                "$setOnInsert": {
                    "account_id": account_id,
                    "created_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
            return_document=True,
        )
        return AccountQuotaInDB.from_mongo(doc)

    async def deduct(
        self, account_id: str, content_type: str, amount: int
    ) -> tuple[bool, str]:
        """
        原子扣除额度。

        使用条件查询确保 used + amount <= limit 且未过期。
        返回 (success, reason)。
        """
        quota_field = f"{content_type}_quota"
        now = datetime.now(timezone.utc)

        # 构建条件查询：额度足够且未过期
        filter_doc = {
            "account_id": account_id,
            "$expr": {
                "$and": [
                    {
                        "$lte": [
                            {"$add": [f"${quota_field}.used", amount]},
                            f"${quota_field}.limit",
                        ]
                    },
                    {
                        "$or": [
                            {
                                # 使用 $ifNull 兼容字段缺失（missing）和显式 null
                                "$eq": [
                                    {
                                        "$ifNull": [
                                            f"${quota_field}.expires_at",
                                            None,
                                        ]
                                    },
                                    None,
                                ]
                            },
                            {
                                "$gte": [
                                    f"${quota_field}.expires_at",
                                    now,
                                ]
                            },
                        ]
                    },
                ]
            },
        }

        result = await self._collection().update_one(
            filter_doc,
            {
                "$inc": {f"{quota_field}.used": amount},
                "$set": {"updated_at": now},
            },
        )

        if result.matched_count == 0:
            # 检查是因为额度不足还是记录不存在/过期
            existing = await self.find_by_account(account_id)
            if existing is None:
                return False, "额度记录不存在"
            q = getattr(existing, quota_field, None)
            if q is None:
                return False, "额度类型不存在"
            if q.expires_at is not None:
                # 兼容 PyMongo 返回的 naive datetime：统一按 UTC 处理后再比较
                exp = q.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < now:
                    return False, "额度已过期"
            if q.used + amount > q.limit:
                return False, "额度不足"
            return False, "扣除失败"

        return True, ""

    async def charge(
        self, account_id: str, content_type: str, amount: int
    ) -> tuple[bool, str]:
        """按实际用量记账，允许超过上限。

        用于 LLM 调用后的结算：预检可能低估真实消耗，若这里继续用 deduct 的
        `used + amount <= limit` 条件，超额请求会扣费失败且 used 不增长，
        用户随后每一轮都能通过预检并获得免费额度。记账成功后 used 允许大于
        limit，下一次预检会自然拒绝。
        """
        if amount <= 0:
            return True, ""
        quota_field = f"{content_type}_quota"
        now = datetime.now(timezone.utc)
        result = await self._collection().update_one(
            {"account_id": account_id},
            {
                "$inc": {f"{quota_field}.used": amount},
                "$set": {"updated_at": now},
            },
        )
        if result.matched_count == 0:
            return False, "额度记录不存在"
        return True, ""

    async def refund(
        self, account_id: str, content_type: str, amount: int
    ) -> bool:
        """返还（回滚）已扣额度。

        用于扣费成功但后续操作失败（如 LLM 调用异常）时的补偿，避免身份为
        失败的请求付费。仅在同一请求内成功扣费后调用，故用 $inc 负值原子返还即可，
        不会使 used 变为负数。
        """
        if amount <= 0:
            return False
        quota_field = f"{content_type}_quota"
        result = await self._collection().update_one(
            {"account_id": account_id},
            {
                "$inc": {f"{quota_field}.used": -amount},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        return result.modified_count > 0

    async def ensure_quota(self, account_id: str) -> AccountQuotaInDB:
        """确保账号存在额度记录，不存在则创建无限额度默认值。"""
        existing = await self.find_by_account(account_id)
        if existing is not None:
            return existing

        unlimited = QuotaEntry(limit=10**9, used=0, expires_at=None)
        create_data = AccountQuotaCreate(
            account_id=account_id,
            text_quota=unlimited,
            voice_quota=unlimited,
            image_quota=unlimited,
        )
        doc = create_data.model_dump()
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return AccountQuotaInDB.from_mongo(doc)

    async def delete_by_account(self, account_id: str) -> int:
        """删除指定账号的额度记录。"""
        result = await self._collection().delete_many(
            {"account_id": account_id}
        )
        return result.deleted_count
