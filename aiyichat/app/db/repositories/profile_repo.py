from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from app.db.mongo import get_database
from app.models.profile import PromptProfileCreate, PromptProfileInDB, PromptProfileUpdate


class ProfileRepository:
    def _collection(self):
        return get_database().profiles

    async def find_by_slug(self, slug: str) -> Optional[PromptProfileInDB]:
        doc = await self._collection().find_one({"slug": slug})
        if doc is None:
            return None
        return PromptProfileInDB.from_mongo(doc)

    async def find_by_id(self, profile_id: str) -> Optional[PromptProfileInDB]:
        doc = await self._collection().find_one({"_id": ObjectId(profile_id)})
        if doc is None:
            return None
        return PromptProfileInDB.from_mongo(doc)

    async def list_active(
        self, category: Optional[str] = None, account_id: Optional[str] = None
    ) -> list[PromptProfileInDB]:
        """列出对指定账号可见的活跃角色（public + 该账号的 private）。

        兼容旧数据：无 visibility 字段的 profile 默认视为 public。
        """
        query: dict = {"is_active": True}
        if category:
            query["category"] = category
        # 可见性过滤：public、无 visibility 字段的旧数据、或属于该账号的 private
        visibility_query: list = [
            {"visibility": "public"},
            {"visibility": {"$exists": False}},
        ]
        if account_id:
            visibility_query.append({
                "visibility": "private",
                "account_id": account_id,
            })
        query["$or"] = visibility_query
        cursor = self._collection().find(query).sort("name", 1)
        return [PromptProfileInDB.from_mongo(doc) async for doc in cursor]

    async def list_all(self) -> list[PromptProfileInDB]:
        # 排除自定义创建向导的草稿角色（兼容无该字段的旧数据）
        cursor = (
            self._collection()
            .find({"is_draft": {"$ne": True}})
            .sort("name", 1)
        )
        return [PromptProfileInDB.from_mongo(doc) async for doc in cursor]

    async def list_public(self) -> list[PromptProfileInDB]:
        """列出所有公共角色。"""
        cursor = (
            self._collection()
            .find({"visibility": "public"})
            .sort("name", 1)
        )
        return [PromptProfileInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_account(self, account_id: str) -> list[PromptProfileInDB]:
        """列出指定账号的私有角色。"""
        cursor = (
            self._collection()
            .find({"visibility": "private", "account_id": account_id})
            .sort("name", 1)
        )
        return [PromptProfileInDB.from_mongo(doc) async for doc in cursor]

    async def create(self, profile: PromptProfileCreate) -> PromptProfileInDB:
        now = datetime.now(timezone.utc)
        doc = profile.model_dump()
        doc["version"] = 1
        doc["created_at"] = now
        doc["updated_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return PromptProfileInDB.from_mongo(doc)

    async def upsert_by_slug(self, profile: PromptProfileCreate) -> PromptProfileInDB:
        now = datetime.now(timezone.utc)
        doc = profile.model_dump()
        doc["updated_at"] = now
        await self._collection().update_one(
            {"slug": profile.slug},
            {
                "$set": doc,
                "$setOnInsert": {"created_at": now, "version": 1},
            },
            upsert=True,
        )
        result = await self.find_by_slug(profile.slug)
        assert result is not None  # upsert 后必定存在
        return result

    async def update(self, slug: str, update: PromptProfileUpdate) -> Optional[PromptProfileInDB]:
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_slug(slug)
        # 嵌套对象需要转为dict
        for key in ("identity", "language_style", "personality", "dialogue_strategy"):
            if key in update_data and hasattr(update_data[key], "model_dump"):
                update_data[key] = update_data[key].model_dump()
        update_data["updated_at"] = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"slug": slug}, {"$set": update_data}
        )
        return await self.find_by_slug(slug)

    async def delete(self, slug: str) -> bool:
        result = await self._collection().update_one(
            {"slug": slug}, {"$set": {"is_active": False}}
        )
        return result.modified_count > 0

    async def hard_delete(self, slug: str) -> bool:
        """物理删除角色。

        与 delete 的软删除（仅置 is_active=False）不同：用户自助清理自建私有角色时
        需要真正释放文档与配额名额，调用方必须先校验归属与服务引用。
        """
        result = await self._collection().delete_one({"slug": slug})
        return result.deleted_count > 0

    async def search(self, q: str, limit: int = 50) -> list[PromptProfileInDB]:
        """按名称、slug 或分类搜索角色（排除草稿）。"""
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            self._collection()
            .find({
                "is_draft": {"$ne": True},
                "$or": [{"name": regex}, {"slug": regex}, {"category": regex}],
            })
            .sort("name", 1)
            .limit(limit)
        )
        return [PromptProfileInDB.from_mongo(doc) async for doc in cursor]

    async def count(self) -> int:
        return await self._collection().count_documents({})
