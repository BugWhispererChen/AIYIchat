"""一次性迁移：为存量会话媒体回填 owner_account_id。

账号级媒体配额（1GB）按 `media.owner_account_id` 统计；历史媒体没有归属，
需要按“会话 → 服务/身份 → 账号”反推。脚本可重复执行，只填空值，不覆盖
已有归属。

用法（在 app 容器内执行，默认只统计不写库）：
    docker compose exec app python -m tools.migrations.backfill_media_owner
    docker compose exec app python -m tools.migrations.backfill_media_owner --apply
"""

import argparse
import asyncio
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_MEDIA_URL_RE = re.compile(r"/api/media/([A-Za-z0-9_-]{20,})")


async def run(apply: bool) -> int:
    from app.db.mongo import close_db, connect_db, get_database
    from app.db.repositories.object_id import id_variants_query

    await connect_db()
    try:
        db = get_database()
        # 会话引用的是 services.id / identities.id，不是 Mongo 的 _id；
        # 旧数据缺 id 时再回退到 _id，保证迁移仍可续跑。
        service_accounts = {
            str(doc.get("id") or doc["_id"]): doc.get("account_id") or ""
            async for doc in db.services.find(
                {}, {"_id": 1, "id": 1, "account_id": 1}
            )
        }
        identity_accounts = {
            str(doc.get("id") or doc["_id"]): doc.get("account_id") or ""
            async for doc in db.identities.find(
                {}, {"_id": 1, "id": 1, "account_id": 1}
            )
        }

        media_docs = await db.media.find(
            {}, {"_id": 1, "public_id": 1, "owner_account_id": 1}
        ).to_list(None)
        media_by_key: dict[str, dict] = {}
        for doc in media_docs:
            if doc.get("public_id"):
                media_by_key[str(doc["public_id"])] = doc
            media_by_key[str(doc["_id"])] = doc

        updates: dict[object, str] = {}
        referenced: set[str] = set()
        unowned_conversations = 0

        async for conv in db.conversations.find(
            {}, {"_id": 1, "service_id": 1, "identity_id": 1}
        ):
            account_id = service_accounts.get(str(conv.get("service_id") or ""))
            if not account_id:
                account_id = identity_accounts.get(
                    str(conv.get("identity_id") or "")
                )
            if not account_id:
                unowned_conversations += 1
                continue
            async for message in db.messages.find(
                id_variants_query("conversation_id", str(conv["_id"])),
                {"media_url": 1, "media_urls": 1},
            ):
                urls = [message.get("media_url") or ""]
                urls.extend(message.get("media_urls") or [])
                for url in urls:
                    for media_id in _MEDIA_URL_RE.findall(url or ""):
                        referenced.add(media_id)
                        doc = media_by_key.get(media_id)
                        if doc is None:
                            continue
                        if doc.get("owner_account_id"):
                            continue
                        updates[doc["_id"]] = account_id

        # 私有角色的定妆照也属于账号资产；公开角色/表情包保持空归属
        async for profile in db.profiles.find(
            {"account_id": {"$exists": True, "$ne": None}},
            {"account_id": 1, "reference_image_url": 1},
        ):
            account_id = profile.get("account_id") or ""
            if not account_id:
                continue
            for media_id in _MEDIA_URL_RE.findall(
                profile.get("reference_image_url") or ""
            ):
                doc = media_by_key.get(media_id)
                if doc is None or doc.get("owner_account_id"):
                    continue
                updates[doc["_id"]] = account_id

        already_owned = sum(
            1 for doc in media_docs if doc.get("owner_account_id")
        )
        orphans = len(
            [
                doc
                for doc in media_docs
                if not doc.get("owner_account_id")
                and (doc.get("public_id") or str(doc["_id"])) not in referenced
            ]
        )
        print(
            f"媒体总数: {len(media_docs)}，已有归属: {already_owned}，"
            f"待回填: {len(updates)}，无会话引用: {orphans}"
        )
        print(f"无法定位账号的会话: {unowned_conversations}")
        if not apply:
            print("dry-run：未写库。确认后加 --apply 执行。")
            return 0

        updated = 0
        for doc_id, account_id in updates.items():
            result = await db.media.update_one(
                {"_id": doc_id, "owner_account_id": {"$in": ["", None]}},
                {"$set": {"owner_account_id": account_id}},
            )
            updated += int(result.modified_count)
            if not result.matched_count:
                # 兼容字段缺失的旧文档
                result = await db.media.update_one(
                    {"_id": doc_id, "owner_account_id": {"$exists": False}},
                    {"$set": {"owner_account_id": account_id}},
                )
                updated += int(result.modified_count)
        print(f"回填完成: {updated} 条")
        return 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="存量媒体归属回填")
    parser.add_argument("--apply", action="store_true", help="实际写库")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__ == "__main__":
    main()
