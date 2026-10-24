"""预置定妆照 seed 与媒体 public_id 迁移的相互约束。

`seed_profiles` 每次启动都会跑。历史实现只用 ObjectId 判断媒体是否存在、插入媒体也不写
`public_id`，于是 `backfill_media_public_id` 把 URL 改成 public_id 之后，下一次启动 seed
又把它写回 `/api/media/<ObjectId>`——媒体接口默认只允许管理员读 ObjectId 引用，
普通访问者重新看不到定妆照。这三条用例把"seed 不得倒退回 ObjectId"钉住。
"""

import asyncio
import uuid

import mongomock_motor
from bson import ObjectId

from app.db import mongo
from app.db.mongo import get_database
from app.db.repositories.profile_repo import ProfileRepository
from app.models.profile import PromptProfileCreate, PromptProfileUpdate
from tools.seed_companion_profiles import _PORTRAIT_SLUGS, _ensure_reference_images

_SLUG = _PORTRAIT_SLUGS[0]


def _run(corofn) -> None:
    asyncio.run(corofn)


async def _prepare(reference_image_url: str) -> None:
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_seed_media"]
    await ProfileRepository().create(PromptProfileCreate(slug=_SLUG, name="预置角色"))
    if reference_image_url:
        await ProfileRepository().update(
            _SLUG, PromptProfileUpdate(reference_image_url=reference_image_url)
        )


async def _url_of() -> str:
    profile = await ProfileRepository().find_by_slug(_SLUG)
    assert profile is not None
    return profile.reference_image_url


def test_seed_keeps_migrated_public_id_url() -> None:
    """URL 已是 public_id 且媒体存在：seed 必须跳过，不能写回 ObjectId。"""
    public_id = uuid.uuid4().hex

    async def case() -> None:
        await _prepare(f"/api/media/{public_id}")
        await get_database().media.insert_one({
            "_id": ObjectId(),
            "public_id": public_id,
            "data": b"jpg-bytes",
            "content_type": "image/jpeg",
            "filename": f"{_SLUG}.jpg",
        })
        await _ensure_reference_images(ProfileRepository())
        assert await _url_of() == f"/api/media/{public_id}"
        # 也不该重复插入同一张定妆照
        assert await get_database().media.count_documents({}) == 1

    _run(case())


def test_seed_keeps_legacy_object_id_url_when_media_exists() -> None:
    """历史 ObjectId 引用且媒体仍在：同样判定为"已有定妆照"，不重写。"""
    media_id = ObjectId()

    async def case() -> None:
        await _prepare(f"/api/media/{media_id}")
        await get_database().media.insert_one({
            "_id": media_id,
            "data": b"jpg-bytes",
            "content_type": "image/jpeg",
            "filename": f"{_SLUG}.jpg",
        })
        await _ensure_reference_images(ProfileRepository())
        assert await _url_of() == f"/api/media/{media_id}"

    _run(case())


def test_fresh_seed_writes_public_id_url_not_object_id() -> None:
    """全新安装：媒体要带 public_id，URL 也必须是 32 位 public_id 形态。"""

    async def case() -> None:
        await _prepare("")
        await _ensure_reference_images(ProfileRepository())
        url = await _url_of()
        assert url.startswith("/api/media/")
        ref = url.rsplit("/", 1)[-1]
        assert len(ref) == 32, f"seed 又写回 ObjectId 形态：{url}"
        doc = await get_database().media.find_one({"public_id": ref})
        assert doc is not None, "定妆照媒体缺少 public_id"

    _run(case())
