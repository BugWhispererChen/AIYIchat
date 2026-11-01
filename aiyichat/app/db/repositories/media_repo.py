import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from bson import ObjectId

from app.db.mongo import get_database

# 消息 media_url 形如 /api/media/{public_id 或旧 ObjectId}。新文档使用 32 位随机
# public_id 作为不可枚举的能力令牌；旧数据仍可能是 24 位 ObjectId（仅管理员可读）。
_MEDIA_URL_RE = re.compile(r"/api/media/([A-Za-z0-9_-]{20,})")

# 每账号媒体累计配额（落库字节）。共享表情包/公开角色图不占个人配额。
MAX_ACCOUNT_MEDIA_BYTES = 1024 * 1024 * 1024

# 配额检查与清理按事件循环串行化：单实例下避免同一账号并发上传同时超额。
# key 用 loop id，避免 pytest 每个用例新建事件循环后复用已绑定旧循环的锁。
_quota_locks: dict[int, asyncio.Lock] = {}


def _quota_lock() -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock = _quota_locks.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _quota_locks[loop_id] = lock
    return lock


class MediaQuotaExceeded(Exception):
    """账号媒体配额已满且无法通过清理腾出空间。"""


def media_ids_from_message_docs(messages: Sequence[Any]) -> list[str]:
    """从消息（ORM 对象或投影后的 dict）的 media_url 反解出媒体文档 ID。

    同时吃两种入参是因为账号级联删除走的是裸集合投影，而账号中心走的是
    `MessageRepository.list_by_conversation_ids` 返回的 `MessageInDB`。
    """
    ids: list[str] = []
    for item in messages:
        if isinstance(item, dict):
            urls = item.get("media_urls") or []
            single = item.get("media_url") or ""
        else:
            urls = getattr(item, "media_urls", []) or []
            single = getattr(item, "media_url", "") or ""
        for url in [single, *urls]:
            ids.extend(_MEDIA_URL_RE.findall(url or ""))
    return ids


class MediaRepository:
    def _collection(self):
        return get_database().media

    async def create(
        self,
        data: bytes,
        content_type: str,
        filename: str = "",
        owner_account_id: str = "",
    ) -> str:
        """保存媒体文件到 MongoDB，返回不可枚举的 public_id。

        public_id 是 URL 中的能力令牌：持有完整 URL 才能读取，替代可被推算的
        ObjectId；``_id`` 仍保留用于内部定位与旧数据兼容。
        """
        if not filename:
            ext = "png" if "png" in content_type else "gif" if "gif" in content_type else "jpg"
            filename = f"{uuid.uuid4().hex}.{ext}"

        public_id = uuid.uuid4().hex
        doc = {
            "public_id": public_id,
            "data": data,
            "content_type": content_type,
            "filename": filename,
            "size": len(data),
            # 空归属用于表情包、公开角色图等共享资源，不计入个人配额
            "owner_account_id": owner_account_id,
            "created_at": datetime.now(timezone.utc),
        }
        await self._collection().insert_one(doc)
        return public_id

    async def usage_bytes(self, account_id: str) -> int:
        """统计账号自有媒体的落库字节数。"""
        if not account_id:
            return 0
        pipeline = [
            {"$match": {"owner_account_id": account_id}},
            {"$group": {"_id": None, "total": {"$sum": "$size"}}},
        ]
        async for row in self._collection().aggregate(pipeline):
            return int(row.get("total") or 0)
        return 0

    async def _account_conversation_ids(self, account_id: str) -> set[str]:
        """账号独占的会话 ID。

        共享组会话不纳入：组内历史可能被其他账号引用，自动清理不能影响他人。
        组会话里的媒体因此会被视为“有外部引用”而永不驱逐，空间不足时宁可
        拒收新媒体，也不跨账号破坏历史。
        """
        db = get_database()
        service_docs = [
            doc async for doc in db.services.find(
                {"account_id": account_id}, {"_id": 1, "id": 1}
            )
        ]
        # 会话引用的是 services.id / identities.id，不是 Mongo 的 _id
        service_ids = [
            doc.get("id") or str(doc["_id"]) for doc in service_docs
        ]
        shared_group_keys = [
            doc["share_group_id"]
            async for doc in db.services.find(
                {"share_group_id": {"$ne": ""}},
                {"share_group_id": 1},
            )
            if doc.get("share_group_id")
        ]
        identity_ids = [
            doc.get("id") or str(doc["_id"])
            async for doc in db.identities.find(
                {"account_id": account_id}, {"_id": 1, "id": 1}
            )
        ]
        clauses: list[dict] = []
        # 组会话的 service_id 可能就是成员服务自身的 id（context_key 取
        # share_group_id or id），两条子句都要排除，否则组内他人引用会被
        # 当成自有会话放行，清理就跨账号写坏了别人的历史。
        if service_ids:
            service_filter: dict = {"$in": service_ids}
            if shared_group_keys:
                service_filter["$nin"] = shared_group_keys
            clauses.append({"service_id": service_filter})
        if identity_ids:
            identity_clause: dict = {"identity_id": {"$in": identity_ids}}
            if shared_group_keys:
                identity_clause["service_id"] = {
                    "$nin": shared_group_keys
                }
            clauses.append(identity_clause)
        if not clauses:
            return set()
        return {
            str(doc["_id"])
            async for doc in db.conversations.find(
                {"$or": clauses}, {"_id": 1}
            )
        }

    async def _message_refs(self, url: str) -> list[str]:
        """消息对某媒体的所有引用（会话 ID 字符串）。"""
        db = get_database()
        return [
            str(doc.get("conversation_id"))
            async for doc in db.messages.find(
                {"$or": [{"media_url": url}, {"media_urls": url}]},
                {"conversation_id": 1},
            )
        ]

    async def _is_shared_media(self, url: str) -> bool:
        """表情包、角色图与引用缓存还是共享/临时资源，禁止配额清理。"""
        db = get_database()
        for collection, field in (
            (db.stickers, "media_url"),
            (db.profiles, "reference_image_url"),
            (db.wechat_quote_cache, "media_url"),
        ):
            if await collection.find_one({field: url}, {"_id": 1}):
                return True
        return False

    async def _strip_message_refs(self, url: str, conversation_ids: set[str]) -> None:
        """从账号自己会话的消息里摘除被清理的媒体 URL，保留正文与其他媒体。"""
        if not conversation_ids:
            return
        db = get_database()
        async for doc in db.messages.find(
            {"$or": [{"media_url": url}, {"media_urls": url}]}
        ):
            # 只动自有会话：同一 URL 也可能被他人消息引用，
            # 按 URL 全库改写会把别人的历史清成残句。
            if str(doc.get("conversation_id") or "") not in conversation_ids:
                continue
            urls = list(doc.get("media_urls") or [])
            content_type = doc.get("content_type") or "text"
            if not urls and doc.get("media_url") == url:
                urls = [url]
            kinds = list(doc.get("media_kinds") or [])
            if len(kinds) < len(urls):
                kinds.extend([content_type] * (len(urls) - len(kinds)))
            pairs = [
                (item_url, kinds[index])
                for index, item_url in enumerate(urls)
                if item_url != url
            ]
            new_urls = [pair[0] for pair in pairs]
            new_kinds = [pair[1] for pair in pairs]
            await db.messages.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "media_urls": new_urls,
                    "media_kinds": new_kinds,
                    "media_url": new_urls[0] if new_urls else "",
                }},
            )

    async def ensure_capacity(self, account_id: str, incoming_size: int) -> None:
        """确保账号还能放下新文件；不足时按最旧优先清理自己的媒体。

        保护顺序：共享表情包/角色图/引用缓存、其他账号消息引用的媒体永不驱逐；
        仍腾不出空间时抛 `MediaQuotaExceeded`，由调用方拒收新媒体并保留文本。
        """
        if incoming_size <= 0 or not account_id:
            return
        if incoming_size > MAX_ACCOUNT_MEDIA_BYTES:
            raise MediaQuotaExceeded("单个媒体超过账号配额上限")
        async with _quota_lock():
            usage = await self.usage_bytes(account_id)
            if usage + incoming_size <= MAX_ACCOUNT_MEDIA_BYTES:
                return
            needed = usage + incoming_size - MAX_ACCOUNT_MEDIA_BYTES
            own_conversations = await self._account_conversation_ids(account_id)
            freed = 0
            async for doc in self._collection().find(
                {"owner_account_id": account_id},
                {"public_id": 1, "size": 1, "created_at": 1},
            ).sort("created_at", 1):
                media_id = doc.get("public_id") or str(doc["_id"])
                url = f"/api/media/{media_id}"
                if await self._is_shared_media(url):
                    continue
                refs = await self._message_refs(url)
                if any(conv_id not in own_conversations for conv_id in refs):
                    continue
                await self._collection().delete_one({"_id": doc["_id"]})
                await self._strip_message_refs(url, own_conversations)
                freed += int(doc.get("size") or 0)
                if freed >= needed:
                    return
            raise MediaQuotaExceeded("账号媒体配额已满，且无法清理出足够空间")

    @staticmethod
    def _lookup_filter(media_id: str) -> Optional[dict]:
        """构造兼容新旧 ID 的查询条件；非法 ID 返回 None。"""
        clauses: list[dict] = [{"public_id": media_id}]
        if ObjectId.is_valid(media_id):
            clauses.append({"_id": ObjectId(media_id)})
        if not media_id:
            return None
        return {"$or": clauses}

    async def find_by_id(self, media_id: str) -> Optional[dict]:
        """按 public_id 或旧 ObjectId 读取媒体文件，返回含 data 字段的原始 dict。"""
        query = self._lookup_filter(media_id)
        if query is None:
            return None
        return await self._collection().find_one(query)

    async def delete(self, media_id: str) -> bool:
        query = self._lookup_filter(media_id)
        if query is None:
            return False
        result = await self._collection().delete_one(query)
        return result.deleted_count > 0

    async def _referenced_ids(
        self, collection: Any, field: str, media_ids: set[str]
    ) -> set[str]:
        """返回媒体 ID 中被指定集合字段引用的子集。

        媒体文档不记归属，只能反查引用方；按 URL 的精确形态查询，避免把
        整个集合扫一遍。
        """
        if not media_ids:
            return set()
        urls = [f"/api/media/{mid}" for mid in media_ids]
        referenced: set[str] = set()
        async for doc in collection.find({field: {"$in": urls}}, {field: 1}):
            referenced.update(_MEDIA_URL_RE.findall(doc.get(field) or ""))
        return referenced & media_ids

    async def delete_by_ids(
        self,
        media_ids: list[str],
        *,
        owned_profile_media_ids: Iterable[str] = (),
    ) -> int:
        """按 public_id 或旧 ObjectId 批量删除媒体文件。

        media 文档不记归属（只有二进制与上传时间），删除只能由持有消息的一方驱动：
        账号中心清空对话/注销时先从消息的 media_url 反解出 ID，再走这里清理，
        否则用户删完对话，生成的图片仍整份留在库里。

        表情包与角色定妆照是全局/账号级资源，可能同时被历史消息引用：
        默认保护所有被它们引用的媒体，避免清空某个账号的对话就把全库共享的
        表情包删成破图。账号注销需要回收自己的私有定妆照时，通过
        `owned_profile_media_ids` 显式放行这些 ID；表情包保护始终生效。
        """
        ids = {mid for mid in media_ids if mid}
        if not ids:
            return 0
        db = get_database()
        sticker_refs = await self._referenced_ids(db.stickers, "media_url", ids)
        profile_refs = await self._referenced_ids(
            db.profiles, "reference_image_url", ids
        )
        owned_profiles = set(owned_profile_media_ids)
        protected = sticker_refs | (profile_refs - owned_profiles)
        deletable = [mid for mid in ids if mid not in protected]
        if not deletable:
            return 0
        object_ids = [ObjectId(mid) for mid in deletable if ObjectId.is_valid(mid)]
        clauses: list[dict] = [{"public_id": {"$in": deletable}}]
        if object_ids:
            clauses.append({"_id": {"$in": object_ids}})
        result = await self._collection().delete_many({"$or": clauses})
        return result.deleted_count
