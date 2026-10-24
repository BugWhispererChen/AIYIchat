"""孤儿清理脚本的删除语义用例。

钉住两件事（都是 2026-10-25 在测试服 apply 时真踩到的）：
1. 同一集合里 `_id` 两种形态并存时，`delete_by_ids` 必须都删得掉——
   脚本原先一律 `ObjectId(i)`，遇到 proactive_records 的 UUID 字符串 id 直接抛 InvalidId，
   把已删了一半的清理流程崩在中间；
2. 删除只命中给定的 id，不能顺手把活数据带走。
"""

from __future__ import annotations

import asyncio
import uuid

import mongomock_motor
from bson import ObjectId

from tools.migrations.purge_orphans import collect, delete_by_ids


def _db() -> object:
    return mongomock_motor.AsyncMongoMockClient()[f"t_purge_{uuid.uuid4().hex[:8]}"]


class TestDeleteByIds:
    def test_both_id_forms_deleted(self) -> None:
        """字符串 id 与 ObjectId 混在一份列表里也要全部命中。"""

        async def body() -> None:
            db = _db()
            uuid_id = str(uuid.uuid4())
            oid = ObjectId()
            await db.proactive_records.insert_many([
                {"_id": uuid_id, "note": "字符串主键"},
                {"_id": str(oid), "note": "同一条的字符串写法"},
                {"_id": oid, "note": "ObjectId 主键"},
                {"_id": "keep-me", "note": "不该被删"},
            ])
            deleted = await delete_by_ids(
                db, "proactive_records", [uuid_id, str(oid), ObjectId(oid)]
            )
            # 三条都是独立文档：UUID 字符串主键、同值但存成字符串的主键、ObjectId 主键
            assert deleted == 3, deleted
            left = [d["_id"] async for d in db.proactive_records.find({})]
            assert left == ["keep-me"], left

        asyncio.run(body())

    def test_empty_list_touches_nothing(self) -> None:
        async def body() -> None:
            db = _db()
            await db.messages.insert_one({"_id": "m1"})
            assert await delete_by_ids(db, "messages", []) == 0
            assert await db.messages.count_documents({}) == 1

        asyncio.run(body())


class TestCollectOrphans:
    def test_media_referenced_by_public_id_is_not_orphan(self) -> None:
        """迁移后消息只按 public_id 引用媒体：只按 ObjectId 反查会把活图全报成孤儿。

        引用是从 `media_url` 字段里取出来的（不是正文），fixture 要与脚本的取法一致。
        """

        async def body() -> None:
            db = _db()
            public = "a" * 32
            await db.media.insert_one({"_id": ObjectId(), "public_id": public})
            await db.messages.insert_one({
                "_id": "msg1",
                "conversation_id": "conv1",
                "media_url": f"/api/media/{public}",
            })
            await db.conversations.insert_one({"_id": "conv1", "id": "conv1"})
            orphans = await collect(db)
            assert orphans.get("media", []) == [], orphans
            assert orphans.get("messages", []) == [], orphans

        asyncio.run(body())
