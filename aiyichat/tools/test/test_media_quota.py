"""账号级媒体配额：用量统计、最旧优先清理、共享与跨账号引用保护。"""

import asyncio
from datetime import datetime, timedelta, timezone

import mongomock_motor
import pytest
from bson import ObjectId

from app.db import mongo
from app.db.mongo import get_database
from app.db.repositories import media_repo
from app.db.repositories.media_repo import (
    MediaQuotaExceeded,
    MediaRepository,
)


def _setup() -> None:
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_media_quota"]


async def _seed_own_conversation() -> tuple[ObjectId, str]:
    db = get_database()
    # 生产数据里 services/identities 的 Mongo _id 是 ObjectId，
    # 会话引用的是业务字段 id；测试必须保持同样的形态。
    await db.services.insert_one({
        "_id": ObjectId(),
        "id": "svc-1",
        "account_id": "acc-1",
    })
    await db.identities.insert_one({
        "_id": ObjectId(),
        "id": "id-1",
        "account_id": "acc-1",
    })
    conv_id = ObjectId()
    await db.conversations.insert_one({
        "_id": conv_id,
        "service_id": "svc-1",
        "identity_id": "id-1",
        "status": "active",
    })
    return conv_id, str(conv_id)


async def _create_media(
    size: int, age_minutes: int, owner: str = "acc-1"
) -> str:
    public_id = await MediaRepository().create(
        b"x" * size, "image/png", owner_account_id=owner
    )
    created_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    await get_database().media.update_one(
        {"public_id": public_id}, {"$set": {"created_at": created_at}}
    )
    return public_id


async def _attach_message(conv_id: str, media_id: str) -> None:
    url = f"/api/media/{media_id}"
    await get_database().messages.insert_one({
        "_id": ObjectId(),
        "conversation_id": conv_id,
        "role": "user",
        "content": "看图",
        "media_url": url,
        "media_urls": [url],
        "media_kinds": ["image"],
    })


def test_usage_bytes_sums_only_owner_media() -> None:
    async def case() -> None:
        _setup()
        await _create_media(100, 1, owner="acc-1")
        await _create_media(200, 2, owner="acc-1")
        await _create_media(999, 3, owner="acc-2")
        assert await MediaRepository().usage_bytes("acc-1") == 300
        assert await MediaRepository().usage_bytes("acc-2") == 999
        assert await MediaRepository().usage_bytes("") == 0

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_ensure_capacity_evicts_oldest_and_strips_message_refs(
    monkeypatch,
) -> None:
    monkeypatch.setattr(media_repo, "MAX_ACCOUNT_MEDIA_BYTES", 300)

    async def case() -> None:
        _setup()
        conv_id, conv_key = await _seed_own_conversation()
        oldest = await _create_media(100, age_minutes=30)
        await _create_media(100, age_minutes=20)
        await _create_media(100, age_minutes=10)
        await _attach_message(conv_key, oldest)

        await MediaRepository().ensure_capacity("acc-1", 100)

        db = get_database()
        assert await db.media.count_documents({"public_id": oldest}) == 0
        message = await db.messages.find_one({"conversation_id": conv_key})
        assert message["media_urls"] == []
        assert message["media_kinds"] == []
        assert message["media_url"] == ""
        assert await MediaRepository().usage_bytes("acc-1") == 200

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_ensure_capacity_skips_shared_sticker_media(monkeypatch) -> None:
    monkeypatch.setattr(media_repo, "MAX_ACCOUNT_MEDIA_BYTES", 200)

    async def case() -> None:
        _setup()
        await _seed_own_conversation()
        sticker_id = await _create_media(100, age_minutes=30)
        disposable_id = await _create_media(100, age_minutes=10)
        await get_database().stickers.insert_one({
            "_id": ObjectId(),
            "media_url": f"/api/media/{sticker_id}",
        })

        await MediaRepository().ensure_capacity("acc-1", 100)

        db = get_database()
        assert await db.media.count_documents({"public_id": sticker_id}) == 1
        assert await db.media.count_documents({"public_id": disposable_id}) == 0

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_ensure_capacity_protects_media_referenced_by_other_account(
    monkeypatch,
) -> None:
    monkeypatch.setattr(media_repo, "MAX_ACCOUNT_MEDIA_BYTES", 100)

    async def case() -> None:
        _setup()
        db = get_database()
        await _seed_own_conversation()
        await db.services.insert_one({
            "_id": ObjectId(),
            "id": "svc-other",
            "account_id": "acc-2",
        })
        other_conv = ObjectId()
        await db.conversations.insert_one({
            "_id": other_conv,
            "service_id": "svc-other",
            "identity_id": "id-other",
            "status": "active",
        })
        media_id = await _create_media(100, age_minutes=30)
        await _attach_message(str(other_conv), media_id)

        with pytest.raises(MediaQuotaExceeded):
            await MediaRepository().ensure_capacity("acc-1", 50)
        assert await db.media.count_documents({"public_id": media_id}) == 1

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_ensure_capacity_never_evicts_shared_group_media(
    monkeypatch,
) -> None:
    """共享组可能被其他账号引用，配额清理不能跨账号破坏历史。"""
    monkeypatch.setattr(media_repo, "MAX_ACCOUNT_MEDIA_BYTES", 100)

    async def case() -> None:
        _setup()
        db = get_database()
        await db.services.insert_one({
            "_id": ObjectId(),
            "id": "svc-1",
            "account_id": "acc-1",
            "share_group_id": "group-1",
        })
        await db.services.insert_one({
            "_id": ObjectId(),
            "id": "svc-peer",
            "account_id": "acc-2",
            "share_group_id": "group-1",
        })
        await db.identities.insert_one({
            "_id": ObjectId(),
            "id": "id-1",
            "account_id": "acc-1",
        })
        group_conv = ObjectId()
        await db.conversations.insert_one({
            "_id": group_conv,
            "service_id": "group-1",
            "identity_id": "id-1",
            "status": "active",
        })
        media_id = await _create_media(100, age_minutes=30)
        await _attach_message(str(group_conv), media_id)

        with pytest.raises(MediaQuotaExceeded):
            await MediaRepository().ensure_capacity("acc-1", 50)
        assert await db.media.count_documents({"public_id": media_id}) == 1

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_save_media_raises_when_quota_full(monkeypatch) -> None:
    monkeypatch.setattr(media_repo, "MAX_ACCOUNT_MEDIA_BYTES", 50)

    async def case() -> None:
        _setup()
        from app.services.media_storage import save_image_from_bytes

        with pytest.raises(MediaQuotaExceeded):
            await save_image_from_bytes(
                b"x" * 60, "image/png", owner_account_id="acc-1"
            )

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_ensure_capacity_protects_media_in_group_named_after_own_service(
    monkeypatch,
) -> None:
    """组 key 可能等于成员服务自己的 id，这类会话同样不能参与配额清理。"""
    monkeypatch.setattr(media_repo, "MAX_ACCOUNT_MEDIA_BYTES", 100)

    async def case() -> None:
        _setup()
        db = get_database()
        # svc-1 自身没有 share_group_id，但 acc-2 用 svc-1 作为组 key 加入共享
        await db.services.insert_one({
            "_id": ObjectId(),
            "id": "svc-1",
            "account_id": "acc-1",
            "share_group_id": "",
        })
        await db.services.insert_one({
            "_id": ObjectId(),
            "id": "svc-2",
            "account_id": "acc-2",
            "share_group_id": "svc-1",
        })
        await db.identities.insert_one({
            "_id": ObjectId(),
            "id": "id-1",
            "account_id": "acc-1",
        })
        group_conv = ObjectId()
        await db.conversations.insert_one({
            "_id": group_conv,
            "service_id": "svc-1",
            "identity_id": "id-1",
            "status": "active",
        })
        media_id = await _create_media(100, age_minutes=30)
        await _attach_message(str(group_conv), media_id)

        with pytest.raises(MediaQuotaExceeded):
            await MediaRepository().ensure_capacity("acc-1", 50)

        db = get_database()
        assert await db.media.count_documents({"public_id": media_id}) == 1
        message = await db.messages.find_one(
            {"conversation_id": str(group_conv)}
        )
        assert message["media_urls"] == [f"/api/media/{media_id}"]

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_strip_message_refs_leaves_other_account_messages_alone() -> None:
    """摘除引用只动自有会话，同 URL 的他人历史必须原样保留。"""

    async def case() -> None:
        _setup()
        db = get_database()
        own_conv = ObjectId()
        peer_conv = ObjectId()
        media_id = await _create_media(10, age_minutes=1)
        url = f"/api/media/{media_id}"
        for conv in (own_conv, peer_conv):
            await db.messages.insert_one({
                "_id": ObjectId(),
                "conversation_id": str(conv),
                "role": "user",
                "content": "看图",
                "content_type": "image",
                "media_url": url,
                "media_urls": [url],
                "media_kinds": ["image"],
            })

        await MediaRepository()._strip_message_refs(url, {str(own_conv)})

        own = await db.messages.find_one({"conversation_id": str(own_conv)})
        peer = await db.messages.find_one({"conversation_id": str(peer_conv)})
        assert own["media_urls"] == []
        assert own["media_url"] == ""
        assert peer["media_urls"] == [url]
        assert peer["media_url"] == url

    try:
        asyncio.run(case())
    finally:
        mongo._db = None
