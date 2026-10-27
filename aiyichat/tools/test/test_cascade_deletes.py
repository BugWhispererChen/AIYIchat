"""级联删除的完整性：删一处，不能留下一地装着用户内容的孤儿。

审查（2026-10-24）发现的缺口都在这里逐个钉住：
- 历史消息的 `conversation_id` 有 ObjectId 形态，只按字符串匹配就删不掉、读不到；
- 删对话/删服务不清注入快照与消息引用的媒体；
- 删身份只删 identities 一行，网页凭据/会话/主动关怀记录留下；
- 注销账号不删微信上下文 token 与私有角色定妆照媒体。
"""

import asyncio
import uuid

import mongomock_motor
from bson import ObjectId

from app.db import mongo
from app.db.mongo import get_database
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.message_repo import MessageRepository


def _setup() -> None:
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_cascade"]


async def _seed_conversation(*, conv_id: ObjectId, as_object_id: bool) -> str:
    """一条对话 + 一条带图片的消息 + 一条注入快照 + 一张媒体。"""
    db = get_database()
    media_id = uuid.uuid4().hex
    await db.media.insert_one({"_id": ObjectId(), "public_id": media_id, "data": b"img"})
    key = conv_id if as_object_id else str(conv_id)
    await db.messages.insert_one({
        "_id": ObjectId(),
        "conversation_id": key,
        "role": "assistant",
        "content": "看图",
        "media_url": f"/api/media/{media_id}",
    })
    await db.injection_snapshots.insert_one({
        "_id": ObjectId(),
        "conversation_id": str(conv_id),
        "service_id": "svc-1",
        "reply_text": "看图",
    })
    return media_id


def test_message_repo_matches_objectid_conversation_id() -> None:
    """同一会话的 ObjectId 形态历史消息必须读得到、删得掉。"""
    conv_id = ObjectId()

    async def case() -> None:
        _setup()
        await get_database().conversations.insert_one({
            "_id": conv_id, "identity_id": "id-1", "service_id": "svc-1", "status": "active",
        })
        await _seed_conversation(conv_id=conv_id, as_object_id=True)
        repo = MessageRepository()
        msgs = await repo.get_recent(str(conv_id))
        assert len(msgs) == 1, "ObjectId 形态的历史消息读不到"
        assert await repo.count_by_conversation(str(conv_id)) == 1
        assert await repo.delete_by_conversation(str(conv_id)) == 1
        assert await get_database().messages.count_documents({}) == 0

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_conversation_cascade_removes_messages_snapshots_and_media() -> None:
    conv_id = ObjectId()

    async def case() -> None:
        _setup()
        db = get_database()
        await db.conversations.insert_one({
            "_id": conv_id, "identity_id": "id-1", "service_id": "svc-1", "status": "active",
        })
        media_id = await _seed_conversation(conv_id=conv_id, as_object_id=False)
        stats = await ConversationRepository().delete_cascade_by_ids([str(conv_id)])
        assert stats["messages"] == 1, stats
        assert stats["injection_snapshots"] == 1, stats
        assert stats["media"] == 1, stats
        assert stats["conversations"] == 1, stats
        assert await db.media.count_documents({"public_id": media_id}) == 0, "媒体没跟着删：孤儿二进制会长期占空间"
        assert await db.injection_snapshots.count_documents({}) == 0

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_identity_cascade_preserves_web_login_and_removes_records() -> None:
    conv_id = ObjectId()

    async def case() -> None:
        _setup()
        db = get_database()
        await db.identities.insert_one({"_id": ObjectId(), "id": "id-1", "account_id": "acc-1"})
        await db.web_credentials.insert_one({"_id": ObjectId(), "id": "cred-1", "account_id": "acc-1"})
        await db.web_sessions.insert_one({"_id": ObjectId(), "credential_id": "cred-1"})
        await db.proactive_records.insert_one({"_id": ObjectId(), "identity_id": "id-1"})
        await db.conversations.insert_one({
            "_id": conv_id, "identity_id": "id-1", "service_id": "svc-1", "status": "active",
        })
        await _seed_conversation(conv_id=conv_id, as_object_id=False)

        stats = await IdentityRepository().delete_cascade("id-1")
        assert "web_credentials" not in stats and "web_sessions" not in stats
        assert stats["proactive_records"] == 1, stats
        assert stats["identity"] == 1, stats
        assert await db.web_credentials.count_documents({}) == 1
        assert await db.web_sessions.count_documents({}) == 1
        assert await db.identities.count_documents({}) == 0
        assert await db.conversations.count_documents({}) == 0
        assert await db.messages.count_documents({}) == 0

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_account_cascade_removes_wechat_tokens_and_portrait_media() -> None:
    async def case() -> None:
        _setup()
        db = get_database()
        portrait_media_id = uuid.uuid4().hex
        await db.media.insert_one({
            "_id": ObjectId(), "public_id": portrait_media_id, "data": b"portrait",
        })
        await db.accounts.insert_one({"_id": ObjectId(), "id": "acc-1"})
        await db.bot_configs.insert_one({"_id": ObjectId(), "id": "bot-1", "account_id": "acc-1"})
        await db.wechat_context_tokens.insert_one({
            "_id": ObjectId(), "bot_id": "bot-1", "user_id": "wx-1", "context_token": "secret",
        })
        await db.profiles.insert_one({
            "_id": ObjectId(), "id": "p-1", "visibility": "private", "account_id": "acc-1",
            "reference_image_url": f"/api/media/{portrait_media_id}",
        })

        stats = await AccountRepository().delete_cascade("acc-1")
        assert stats["bots"] == 1, stats
        assert stats["wechat_context_tokens"] == 1, stats
        assert await db.wechat_context_tokens.count_documents({}) == 0, "微信上下文 token 残留"
        assert await db.media.count_documents({"public_id": portrait_media_id}) == 0, "定妆照媒体残留"

    try:
        asyncio.run(case())
    finally:
        mongo._db = None

def test_single_message_delete_removes_media_and_checks_ownership() -> None:
    """删单条消息要回收它的媒体；`msg_id` 来自表单，必须校验归属会话。"""
    from app.db.repositories.media_repo import MediaRepository

    conv_a = ObjectId()
    conv_b = ObjectId()

    async def case() -> None:
        _setup()
        db = get_database()
        for cid in (conv_a, conv_b):
            await db.conversations.insert_one({
                "_id": cid, "identity_id": "id-1", "service_id": "svc-1", "status": "active",
            })
        media_id = await _seed_conversation(conv_id=conv_a, as_object_id=False)
        msg = await db.messages.find_one({"conversation_id": str(conv_a)})
        other = await db.messages.find_one({"conversation_id": str(conv_b)})
        assert other is None
        mid = str(msg["_id"])

        # 无效 ID：查不到、删不掉，也不该抛 InvalidId
        repo = MessageRepository()
        assert await repo.find_by_id("not-an-id") is None
        assert await repo.delete_with_media("not-an-id") is False
        assert await repo.delete_with_media(mid) is True
        assert await db.messages.count_documents({}) == 0
        assert await MediaRepository().delete_by_ids([media_id]) == 0, "媒体已随消息删除"

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_message_repo_handles_objectid_typed_ids() -> None:
    """历史行可能把 conversation_id 存成 ObjectId：读取与删除都要吃得下。"""
    conv_id = ObjectId()

    async def case() -> None:
        _setup()
        await _seed_conversation(conv_id=conv_id, as_object_id=True)
        repo = MessageRepository()
        msgs = await repo.get_recent(str(conv_id))
        assert len(msgs) == 1
        assert await repo.find_by_id(str(msgs[0].id)) is not None
        assert await repo.delete_with_media(str(msgs[0].id)) is True
        assert await get_database().messages.count_documents({}) == 0

    try:
        asyncio.run(case())
    finally:
        mongo._db = None

