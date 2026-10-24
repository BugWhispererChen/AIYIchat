"""无效 ObjectId 的守卫：输入错误必须是"查无此记录"，不是 500。

真实仓库里 `ObjectId(外部ID)` 会抛 `bson.errors.InvalidId`，一路冒泡成 500
（`GET /api/conversations/missing/messages` 实测如此）。此前的用例用 `_FakeConvRepo`
替身绕过了真实转换，所以一直没抓到。这里直接对着真实仓库钉住契约。
"""

import asyncio

import mongomock_motor

from app.db import mongo
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository

BAD_IDS = ("missing", "not-an-object-id", "zzzzzzzzzzzzzzzzzzzzzzzz", "")


def _setup() -> None:
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_invalid_oid"]


def test_repositories_treat_invalid_id_as_not_found() -> None:
    async def case() -> None:
        _setup()
        for bad in BAD_IDS:
            assert await ConversationRepository().find_by_id(bad) is None, bad
            assert await ConversationRepository().delete(bad) is False, bad
            assert await ConversationRepository().delete_by_ids([bad]) == 0, bad
            assert await MessageRepository().delete(bad) is False, bad
            assert await ProfileRepository().find_by_id(bad) is None, bad
            assert await QuotaTemplateRepository().find_by_id(bad) is None, bad
            assert await QuotaTemplateRepository().delete(bad) is False, bad
            # 写路径同样不许炸：无效 ID 就是没有可更新的记录
            assert (
                await ConversationRepository().update_after_message(bad) is None
            ), bad

    try:
        asyncio.run(case())
    finally:
        mongo._db = None


def test_valid_id_still_works() -> None:
    """守卫不能把合法请求一起挡掉。"""
    from app.models.profile import PromptProfileCreate

    async def case() -> None:
        _setup()
        repo = ConversationRepository()
        conv = await repo.create("id-1", "profile-1", "service-1")
        assert await repo.find_by_id(str(conv.id)) is not None
        assert await repo.delete(str(conv.id)) is True

        profile = await ProfileRepository().create(
            PromptProfileCreate(slug="guard-ok", name="守卫用例")
        )
        assert await ProfileRepository().find_by_id(str(profile.id)) is not None

    try:
        asyncio.run(case())
    finally:
        mongo._db = None
