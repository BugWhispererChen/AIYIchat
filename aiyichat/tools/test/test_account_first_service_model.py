"""账号直连与服务模型迁移脚本测试。"""

import asyncio

import mongomock_motor
from bson import ObjectId

from tools.migrations.account_first_service_model import migrate


async def _seed(db) -> tuple[str, str]:
    account_id = "acc-1"
    identity_id = "identity-1"
    profile_id = ObjectId()
    await db.accounts.insert_one({"id": account_id, "name": "web:alice"})
    await db.identities.insert_one({
        "id": identity_id,
        "account_id": account_id,
        "platform": "web",
        "platform_id": "alice",
        "timezone": "Asia/Shanghai",
    })
    await db.web_credentials.insert_one({"id": "cred-1", "identity_id": identity_id})
    await db.profiles.insert_one({
        "_id": profile_id,
        "slug": "role-1",
        "life_routine": {"timezone": "Asia/Shanghai", "segments": []},
    })
    await db.bot_configs.insert_many([
        {"id": "web-bot", "account_id": account_id, "platform": "web"},
        {"id": "qq-bot", "account_id": account_id, "platform": "qq"},
    ])
    await db.services.insert_many([
        {
            "_id": ObjectId(),
            "id": "svc-web",
            "account_id": account_id,
            "identity_id": identity_id,
            "bot_id": "web-bot",
            "profile_id": "role-1",
            "timezone": "Asia/Shanghai",
        },
        {
            "_id": ObjectId(),
            "id": "svc-qq",
            "account_id": account_id,
            "identity_id": identity_id,
            "bot_id": "qq-bot",
            "profile_id": str(profile_id),
        },
    ])
    await db.identities.create_index(
        [("platform", 1), ("platform_id", 1)],
        unique=True,
        name="platform_1_platform_id_1",
    )
    await db.identities.create_index(
        [("platform", 1), ("platform_id", 1), ("bot_id", 1)],
        unique=True,
        name="platform_1_platform_id_1_bot_id_1",
    )
    return account_id, identity_id


def test_migration_dry_run_and_apply() -> None:
    async def body() -> None:
        db = mongomock_motor.AsyncMongoMockClient()["migration_full"]
        account_id, identity_id = await _seed(db)

        assert await migrate(db, apply=False) == 0
        credential = await db.web_credentials.find_one({"id": "cred-1"})
        assert credential["identity_id"] == identity_id
        assert (await db.identities.find_one({"id": identity_id}))["platform"] == "web"

        assert await migrate(db, apply=True) == 0
        credential = await db.web_credentials.find_one({"id": "cred-1"})
        assert credential["account_id"] == account_id
        assert "identity_id" not in credential
        identity = await db.identities.find_one({"id": identity_id})
        assert identity["platform"] is None and identity["platform_id"] == ""
        assert "timezone" not in identity
        services = {doc["id"]: doc async for doc in db.services.find({})}
        assert services["svc-web"]["profile_id"] == services["svc-qq"]["profile_id"]
        assert services["svc-qq"]["single_service_bot_id"] == "qq-bot"
        assert "single_service_bot_id" not in services["svc-web"]
        assert "timezone" not in services["svc-web"]
        profile = await db.profiles.find_one({"slug": "role-1"})
        assert "timezone" not in profile["life_routine"]
        indexes = await db.identities.index_information()
        assert "platform_1_platform_id_1" not in indexes
        assert "platform_1_platform_id_1_bot_id_1" not in indexes
        assert "identity_external_binding_unique" in indexes
        assert "service_single_bot_unique" in await db.services.index_information()

    asyncio.run(body())


def test_duplicate_non_web_services_block_apply() -> None:
    async def body() -> None:
        db = mongomock_motor.AsyncMongoMockClient()["migration_duplicate"]
        account_id, _ = await _seed(db)
        await db.services.insert_one({
            "_id": ObjectId(),
            "id": "svc-qq-duplicate",
            "account_id": account_id,
            "bot_id": "qq-bot",
            "profile_id": "role-1",
        })
        assert await migrate(db, apply=True) == 1
        assert (await db.identities.find_one({"id": "identity-1"}))["platform"] == "web"
        credential = await db.web_credentials.find_one({"id": "cred-1"})
        assert credential["identity_id"] == "identity-1"

    asyncio.run(body())
