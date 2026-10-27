"""迁移到账号直连、普通身份、全局时区与统一服务装配模型。

默认仅预检；备份并确认输出后使用 --apply。执行期间应停止 app 写入。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from typing import Any

from bson import ObjectId


async def _credential_updates(db: Any) -> tuple[list[tuple[str, str]], list[str]]:
    updates: list[tuple[str, str]] = []
    blocked: list[str] = []
    async for credential in db.web_credentials.find(
        {"$or": [{"account_id": {"$exists": False}}, {"account_id": ""}]},
        {"id": 1, "identity_id": 1},
    ):
        identity = await db.identities.find_one(
            {"id": credential.get("identity_id", "")}, {"account_id": 1}
        )
        if identity and identity.get("account_id"):
            updates.append((credential["id"], identity["account_id"]))
        else:
            blocked.append(credential["id"])
    return updates, blocked


async def _profile_updates(db: Any) -> tuple[list[tuple[Any, str]], list[str]]:
    updates: list[tuple[Any, str]] = []
    unresolved: list[str] = []
    async for service in db.services.find({}, {"_id": 1, "id": 1, "profile_id": 1}):
        ref = str(service.get("profile_id") or "")
        if not ref:
            unresolved.append(str(service.get("id") or service["_id"]))
            continue
        profile = None
        if ObjectId.is_valid(ref):
            profile = await db.profiles.find_one({"_id": ObjectId(ref)}, {"_id": 1})
        if profile is None:
            profile = await db.profiles.find_one({"slug": ref}, {"_id": 1})
        if profile is None:
            unresolved.append(str(service.get("id") or service["_id"]))
        elif ref != str(profile["_id"]):
            updates.append((service["_id"], str(profile["_id"])))
    return updates, unresolved


async def _single_service_updates(
    db: Any,
) -> tuple[list[tuple[Any, str]], dict[str, int]]:
    updates: list[tuple[Any, str]] = []
    bot_ids: list[str] = []
    async for service in db.services.find(
        {}, {"_id": 1, "bot_id": 1, "single_service_bot_id": 1}
    ):
        bot_ids.append(str(service.get("bot_id") or ""))
    duplicates = {
        bot_id: count
        for bot_id, count in Counter(bot_ids).items()
        if bot_id and count > 1 and await _is_non_web_bot(db, bot_id)
    }
    async for service in db.services.find(
        {}, {"_id": 1, "bot_id": 1, "single_service_bot_id": 1}
    ):
        bot_id = str(service.get("bot_id") or "")
        if bot_id and await _is_non_web_bot(db, bot_id):
            if service.get("single_service_bot_id") != bot_id:
                updates.append((service["_id"], bot_id))
    return updates, duplicates


async def _is_non_web_bot(db: Any, bot_id: str) -> bool:
    bot = await db.bot_configs.find_one({"id": bot_id}, {"platform": 1})
    return bot is not None and bot.get("platform") != "web"


async def _drop_index_if_exists(db: Any, collection: str, name: str) -> None:
    indexes = await db[collection].index_information()
    if name in indexes:
        await db[collection].drop_index(name)


async def _drop_legacy_identity_indexes(db: Any) -> None:
    """清理所有旧 platform + platform_id 唯一索引及其历史变体。"""
    indexes = await db.identities.index_information()
    for name, spec in indexes.items():
        keys = spec.get("key", [])
        if (
            name != "identity_external_binding_unique"
            and spec.get("unique")
            and keys[:2] == [("platform", 1), ("platform_id", 1)]
        ):
            await db.identities.drop_index(name)


async def migrate(db: Any, apply: bool) -> int:
    credential_updates, blocked_credentials = await _credential_updates(db)
    profile_updates, unresolved_profiles = await _profile_updates(db)
    single_updates, duplicates = await _single_service_updates(db)
    web_identities = await db.identities.count_documents({"platform": "web"})
    legacy_credentials = await db.web_credentials.count_documents(
        {"identity_id": {"$exists": True}}
    )
    timezone_services = await db.services.count_documents(
        {"timezone": {"$nin": ["", None]}}
    )
    timezone_identities = await db.identities.count_documents(
        {"timezone": {"$nin": ["", None]}}
    )
    timezone_routines = await db.profiles.count_documents(
        {"life_routine.timezone": {"$nin": ["", None]}}
    )

    print(f"凭据 account_id 待回填: {len(credential_updates)}")
    print(f"旧 web 身份待转普通身份: {web_identities}")
    print(f"角色引用待规范为 id: {len(profile_updates)}，无法解析: {len(unresolved_profiles)}")
    print(f"非网页单服务约束待回填: {len(single_updates)}，重复渠道: {duplicates}")
    print(f"旧凭据 identity_id 待清理: {legacy_credentials}")
    print(
        "非空旧时区待退役: "
        f"服务={timezone_services} 身份={timezone_identities} 角色作息={timezone_routines}"
    )
    if unresolved_profiles:
        print(f"角色引用异常服务（保留待人工处理）: {unresolved_profiles[:20]}")
    if blocked_credentials:
        print(f"无法回填账号的凭据: {blocked_credentials[:20]}")
    if blocked_credentials or duplicates:
        print("存在阻断项，禁止 apply；请先按清单人工处理。")
        return 1
    if not apply:
        print("dry-run：未写库。备份并停止 app 写入后加 --apply 执行。")
        return 0

    await _drop_legacy_identity_indexes(db)
    for credential_id, account_id in credential_updates:
        await db.web_credentials.update_one(
            {"id": credential_id}, {"$set": {"account_id": account_id}}
        )
    await db.identities.update_many(
        {"platform": "web"}, {"$set": {"platform": None, "platform_id": ""}}
    )
    for service_oid, profile_id in profile_updates:
        await db.services.update_one(
            {"_id": service_oid}, {"$set": {"profile_id": profile_id}}
        )
    for service_oid, bot_id in single_updates:
        await db.services.update_one(
            {"_id": service_oid}, {"$set": {"single_service_bot_id": bot_id}}
        )
    await db.services.update_many({}, {"$unset": {"timezone": ""}})
    await db.identities.update_many({}, {"$unset": {"timezone": ""}})
    await db.profiles.update_many({}, {"$unset": {"life_routine.timezone": ""}})
    await db.web_credentials.update_many({}, {"$unset": {"identity_id": ""}})
    await _drop_index_if_exists(db, "web_credentials", "identity_id_1")

    await db.identities.create_index(
        [("platform", 1), ("platform_id", 1)],
        unique=True,
        name="identity_external_binding_unique",
        partialFilterExpression={"platform_id": {"$gt": ""}},
    )
    await db.services.create_index(
        "single_service_bot_id",
        unique=True,
        name="service_single_bot_unique",
        partialFilterExpression={"single_service_bot_id": {"$type": "string"}},
    )
    await db.web_credentials.create_index("account_id")
    print("迁移完成：账号直连、普通身份、角色 ID、全局时区与唯一索引已生效。")
    return 0


async def run(apply: bool) -> int:
    from app.db.mongo import close_db, connect_db, get_database

    await connect_db()
    try:
        return await migrate(get_database(), apply)
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际写库（默认仅预检）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__ == "__main__":
    main()
