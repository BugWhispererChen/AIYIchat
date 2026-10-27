"""C 端网页凭据的数据库访问层。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_database
from app.models.web_credential import CredentialStatus, WebCredentialCreate, WebCredentialInDB


class WebCredentialRepository:
    """管理网页账号凭据的增删改查与登录失败计数。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().web_credentials

    async def insert(self, credential: WebCredentialCreate) -> Optional[WebCredentialInDB]:
        """写入新身份；用户名已被占用时返回 None。

        依赖 `username` 唯一索引做原子占位：并发注册同一用户名时只有一个人成功，
        另一个在此处得到 None（上层转 409），无需先查后插的竞态判断。
        """
        now = datetime.now(timezone.utc)
        doc = credential.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        try:
            result = await self._collection().insert_one(doc)
        except DuplicateKeyError:
            return None
        doc["_id"] = result.inserted_id
        return WebCredentialInDB.from_mongo(doc)

    async def find_by_username(self, username: str) -> Optional[WebCredentialInDB]:
        """按规范化用户名查询身份。"""
        doc = await self._collection().find_one({"username": username})
        if doc is None:
            return None
        return WebCredentialInDB.from_mongo(doc)

    async def find_by_id(self, credential_id: str) -> Optional[WebCredentialInDB]:
        """按 ID 查询身份。"""
        doc = await self._collection().find_one({"id": credential_id})
        if doc is None:
            return None
        return WebCredentialInDB.from_mongo(doc)

    async def find_by_account_id(self, account_id: str) -> list[WebCredentialInDB]:
        """查询账号下的网页登录凭据。"""
        cursor = self._collection().find({"account_id": account_id})
        return [WebCredentialInDB.from_mongo(doc) async for doc in cursor]

    async def set_account_id(self, credential_id: str, account_id: str) -> bool:
        """注册创建账号后回填归属。"""
        result = await self._collection().update_one(
            {"id": credential_id},
            {"$set": {"account_id": account_id, "updated_at": datetime.now(timezone.utc)}},
        )
        return result.matched_count > 0

    async def set_status(
        self,
        credential_id: str,
        status: CredentialStatus,
        reviewed_by: str = "",
        review_note: str = "",
        review: bool = False,
    ) -> bool:
        """更新账号状态。

        `review=True` 时同时记录审核人与审核时间（审核通过/拒绝路径），
        拒绝冷却期依赖 `reviewed_at` 判定；纯封禁/启用等治理操作不覆盖既有审核留痕。
        """
        update: dict = {"status": status, "updated_at": datetime.now(timezone.utc)}
        if review:
            update["reviewed_by"] = reviewed_by
            update["reviewed_at"] = datetime.now(timezone.utc)
            update["review_note"] = review_note
        if status == "active":
            # 状态转为可用即视为登录凭据就绪，清空历史失败计数与锁定
            update["failed_count"] = 0
            update["locked_until"] = None
        result = await self._collection().update_one({"id": credential_id}, {"$set": update})
        return result.modified_count > 0

    async def set_password(self, credential_id: str, password_hash: str) -> bool:
        """重置密码（管理员后台通道），同时解除登录失败锁定。"""
        result = await self._collection().update_one(
            {"id": credential_id},
            {
                "$set": {
                    "password_hash": password_hash,
                    "failed_count": 0,
                    "locked_until": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count > 0

    async def register_login_failure(
        self, credential_id: str, max_fail: int, lockout_minutes: int
    ) -> Optional[datetime]:
        """累加登录失败次数，达到阈值时置锁定并返回锁定到期时间。

        使用 `$inc` 原子累加，避免并发失败读改写丢计数。
        """
        now = datetime.now(timezone.utc)
        result = await self._collection().update_one(
            {"id": credential_id}, {"$inc": {"failed_count": 1}, "$set": {"updated_at": now}}
        )
        if result.modified_count == 0:
            return None
        credential = await self.find_by_id(credential_id)
        if credential is None or credential.failed_count < max_fail:
            return None
        locked_until = now + timedelta(minutes=lockout_minutes)
        await self._collection().update_one(
            {"id": credential_id},
            {
                "$set": {
                    "locked_until": locked_until,
                    "failed_count": 0,  # 锁定时重置计数，解锁后重新计
                    "updated_at": now,
                }
            },
        )
        return locked_until

    async def reset_login_failures(self, credential_id: str) -> bool:
        """登录成功后清零失败计数与锁定。"""
        result = await self._collection().update_one(
            {"id": credential_id},
            {
                "$set": {
                    "failed_count": 0,
                    "locked_until": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count > 0

    async def list_by_status(
        self, status: CredentialStatus, skip: int = 0, limit: int = 50
    ) -> list[WebCredentialInDB]:
        """按状态分页查询（后台待审核列表）。"""
        cursor = (
            self._collection()
            .find({"status": status})
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [WebCredentialInDB.from_mongo(doc) async for doc in cursor]

    async def list_all(self, skip: int = 0, limit: int = 50) -> list[WebCredentialInDB]:
        """分页查询全部身份。"""
        cursor = self._collection().find().sort("created_at", -1).skip(skip).limit(limit)
        return [WebCredentialInDB.from_mongo(doc) async for doc in cursor]

    async def count_by_status(self, status: CredentialStatus) -> int:
        """统计指定状态身份数（上线后监控审核积压）。"""
        return await self._collection().count_documents({"status": status})

    async def find_recent_by_username(
        self, username: str, within_hours: int, status: CredentialStatus
    ) -> Optional[WebCredentialInDB]:
        """查询该用户名在冷却期内是否已被指定状态占用（拒绝后 24h 内不可重注）。"""
        since = datetime.now(timezone.utc) - timedelta(hours=within_hours)
        doc = await self._collection().find_one(
            {"username": username, "status": status, "reviewed_at": {"$gt": since}}
        )
        if doc is None:
            return None
        return WebCredentialInDB.from_mongo(doc)

    async def delete_by_id(self, credential_id: str) -> bool:
        """删除身份（注册回滚与管理员删除共用）。"""
        result = await self._collection().delete_one({"id": credential_id})
        return result.deleted_count > 0
