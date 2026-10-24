import asyncio
from datetime import datetime, timezone
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_database
from app.db.repositories.object_id import (
    id_variants_list,
    to_object_id,
    to_object_id_list,
)
from app.models.conversation import ConversationInDB



# 按 (identity, profile, service) 串行化"查不到就建"，键数与活跃会话同量级
_find_or_create_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_LOCK_MAP_CAP = 4096


class ConversationRepository:
    """管理对话的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().conversations

    async def find_active(
        self, identity_id: str, profile_id: str, service_id: str
    ) -> Optional[ConversationInDB]:
        """按身份ID、角色ID和服务ID查询活跃对话。"""
        doc = await self._collection().find_one({
            "identity_id": identity_id,
            "profile_id": profile_id,
            "service_id": service_id,
            "status": "active",
        })
        if doc is None:
            return None
        return ConversationInDB.from_mongo(doc)

    async def find_by_id(self, conversation_id: str) -> Optional[ConversationInDB]:
        """根据ID查询对话。"""
        oid = to_object_id(conversation_id)
        if oid is None:
            return None
        doc = await self._collection().find_one({"_id": oid})
        if doc is None:
            return None
        return ConversationInDB.from_mongo(doc)

    async def create(
        self, identity_id: str, profile_id: str, service_id: str, title: str = ""
    ) -> ConversationInDB:
        """创建新对话。"""
        now = datetime.now(timezone.utc)
        doc = {
            "identity_id": identity_id,
            "profile_id": profile_id,
            "service_id": service_id,
            "title": title,
            "summary": None,
            "message_count": 0,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
        }
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return ConversationInDB.from_mongo(doc)

    async def find_or_create(
        self, identity_id: str, profile_id: str, service_id: str
    ) -> ConversationInDB:
        """查找或创建活跃对话（同 key 串行，避免并发建出两条活跃会话）。

        "先查再插"不是原子操作：网页端同一会话的并发首轮请求会各自查不到、各自插入，
        其中一条之后永远读不到（历史与计数都挂在另一条上）。单实例部署下按 key
        加进程内锁即可闭合；多实例水平扩展要改成 `status=active` 的部分唯一索引 + upsert。
        """
        key = (identity_id, profile_id, service_id)
        if len(_find_or_create_locks) > _LOCK_MAP_CAP:
            # 键随活跃会话增长，异常膨胀（大量一次性身份）时清掉未占用的锁
            for stale_key, stale in [
                (k, v) for k, v in _find_or_create_locks.items() if not v.locked()
            ]:
                _find_or_create_locks.pop(stale_key, None)
        lock = _find_or_create_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _find_or_create_locks[key] = lock
        async with lock:
            conv = await self.find_active(identity_id, profile_id, service_id)
            if conv is not None:
                return conv
            try:
                return await self.create(identity_id, profile_id, service_id)
            except DuplicateKeyError:
                # 另一个进程/实例抢在同一步建好了（靠 conversations 上的部分唯一索引才可能
                # 撞上）：用它那条，绝不留下第二条活跃会话。进程内锁挡不住跨实例竞态。
                raced = await self.find_active(identity_id, profile_id, service_id)
                if raced is not None:
                    return raced
                raise

    async def find_active_by_key(
        self, context_key: str
    ) -> Optional[ConversationInDB]:
        """按归属 key 查询活跃对话（共享组场景不限定身份，跨服务复用同一会话）。"""
        doc = await self._collection().find_one({
            "service_id": context_key,
            "status": "active",
        })
        if doc is None:
            return None
        return ConversationInDB.from_mongo(doc)

    async def find_or_create_by_key(
        self, identity_id: str, profile_id: str, context_key: str
    ) -> ConversationInDB:
        """按归属 key 查找活跃对话，不存在时以当前身份与角色创建。"""
        conv = await self.find_active_by_key(context_key)
        if conv is not None:
            return conv
        return await self.create(identity_id, profile_id, context_key)

    async def update_after_message(self, conversation_id: str, count: int = 1) -> None:
        """更新对话消息计数和最后消息时间。"""
        oid = to_object_id(conversation_id)
        if oid is None:
            return
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"_id": oid},
            {
                "$inc": {"message_count": count},
                "$set": {"last_message_at": now, "updated_at": now},
            },
        )

    async def set_summary(
        self,
        conversation_id: str,
        summary: str,
        summary_message_count: int = 0,
        summary_start_at: datetime | None = None,
        summary_end_at: datetime | None = None,
    ) -> None:
        """设置对话摘要，同时记录摘要已覆盖的消息数量与时间范围。

        时间范围使摘要注入时能渲染为“【之前的对话摘要（覆盖 X ~ Y）】”，
        避免模型面对一段无时间坐标的摘要文本。
        """
        update_fields: dict = {
            "summary": summary,
            "updated_at": datetime.now(timezone.utc),
        }
        if summary_message_count > 0:
            update_fields["summary_message_count"] = summary_message_count
        if summary_start_at is not None:
            update_fields["summary_start_at"] = summary_start_at
        if summary_end_at is not None:
            update_fields["summary_end_at"] = summary_end_at
        oid = to_object_id(conversation_id)
        if oid is None:
            return
        await self._collection().update_one(
            {"_id": oid},
            {"$set": update_fields},
        )

    async def list_by_identity(self, identity_id: str) -> list[ConversationInDB]:
        """按身份ID查询对话列表。"""
        cursor = self._collection().find(
            {"identity_id": identity_id}
        ).sort("last_message_at", -1)
        return [ConversationInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_service(self, service_id: str) -> list[ConversationInDB]:
        """按服务ID查询对话列表。"""
        cursor = self._collection().find(
            {"service_id": service_id}
        ).sort("last_message_at", -1)
        return [ConversationInDB.from_mongo(doc) async for doc in cursor]

    async def list_recent(self, skip: int = 0, limit: int = 50) -> list[ConversationInDB]:
        """查询最近对话（分页）。"""
        cursor = (
            self._collection()
            .find()
            .sort("last_message_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [ConversationInDB.from_mongo(doc) async for doc in cursor]

    async def delete(self, conversation_id: str) -> bool:
        """删除指定对话。"""
        oid = to_object_id(conversation_id)
        if oid is None:
            return False
        result = await self._collection().delete_one({"_id": oid})
        return result.deleted_count > 0

    async def search(self, q: str, limit: int = 50) -> list[ConversationInDB]:
        """按标题、摘要或身份ID搜索对话。"""
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            self._collection()
            .find({"$or": [{"title": regex}, {"summary": regex}, {"identity_id": regex}]})
            .sort("last_message_at", -1)
            .limit(limit)
        )
        return [ConversationInDB.from_mongo(doc) async for doc in cursor]

    async def count(self) -> int:
        """统计对话总数。"""
        return await self._collection().count_documents({})

    async def list_ids_by_service(self, service_id: str) -> list[str]:
        """获取指定服务下所有对话的 ID 列表。"""
        cursor = self._collection().find(
            {"service_id": service_id}, {"_id": 1}
        )
        return [str(doc["_id"]) async for doc in cursor]

    async def list_ids_by_identity(self, identity_id: str) -> list[str]:
        """获取某身份名下所有对话的 ID 列表。

        账号中心「清空对话记录」按身份取数：共享组场景下 service_id 是组 key，
        只删 identity_id 命中本人的对话，才不会连带删掉同组其他人的会话。
        """
        cursor = self._collection().find({"identity_id": identity_id}, {"_id": 1})
        return [str(doc["_id"]) async for doc in cursor]

    async def delete_by_ids(self, conversation_ids: list[str]) -> int:
        """按 ID 列表批量删除对话（先删消息、再删对话）。"""
        if not conversation_ids:
            return 0
        oids = to_object_id_list(conversation_ids)
        if not oids:
            return 0
        result = await self._collection().delete_many({"_id": {"$in": oids}})
        return result.deleted_count

    async def delete_cascade_by_ids(self, conversation_ids: list[str]) -> dict[str, int]:
        """按对话彻底删除：消息 + 注入快照 + 消息引用的媒体 + 对话本身。

        两条不变量：
        - 媒体必须在消息还挂着时反解引用：media 文档不记归属，删了消息就找不到图；
        - 注入快照存着本轮 AI 回复与被注入的记忆正文，只删对话等于给已删会话留一份副本。
        """
        from app.db.repositories.injection_snapshot_repo import InjectionSnapshotRepository
        from app.db.repositories.media_repo import (
            MediaRepository,
            media_ids_from_message_docs,
        )
        from app.db.repositories.message_repo import MessageRepository

        empty = {"messages": 0, "injection_snapshots": 0, "media": 0, "conversations": 0}
        if not conversation_ids:
            return empty
        docs = [
            doc
            async for doc in get_database().messages.find(
                {"conversation_id": {"$in": id_variants_list(conversation_ids)}},
                {"media_url": 1},
            )
        ]
        return {
            "messages": await MessageRepository().delete_by_conversation_ids(
                conversation_ids
            ),
            "injection_snapshots": await InjectionSnapshotRepository()
            .delete_by_conversation_ids(conversation_ids),
            "media": await MediaRepository().delete_by_ids(
                media_ids_from_message_docs(docs)
            ),
            "conversations": await self.delete_by_ids(conversation_ids),
        }

    async def delete_by_service(self, service_id: str) -> int:
        """删除指定服务下的所有对话。"""
        result = await self._collection().delete_many(
            {"service_id": service_id}
        )
        return result.deleted_count

    async def find_latest_by_service(
        self, service_id: str
    ) -> Optional[ConversationInDB]:
        """查找服务下最近活跃的对话（用于主动消息上下文获取）。"""
        doc = await self._collection().find_one(
            {"service_id": service_id, "status": "active"},
            sort=[("last_message_at", -1)],
        )
        if doc is None:
            return None
        return ConversationInDB.from_mongo(doc)
