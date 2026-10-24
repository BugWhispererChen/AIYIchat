"""一次性清理"父记录已被删除、子记录还留着"的孤儿文档。

背景：级联删除此前只覆盖部分集合，后台删角色/删服务/注销账号会留下一批装着
用户可见内容的孤儿（消息、主动关怀记录、注入快照、微信上下文 token），
以及不再被任何引用指向的媒体二进制。新的级联逻辑已经堵住新增，存量要一次性清。

用法（在 app 容器内执行，默认只统计不删）：
    docker exec aiyichat-app-1 python -m tools.migrations.purge_orphans
对账确认后加 --apply 实际删除。**执行前先单独备份**：
    docker exec aiyichat-mongo-1 mongodump --db aiyichat --archive=/tmp/pre_purge.tar.gz

删除顺序不可调换（media 文档不记归属，只能靠引用方反查）：
先删消息等引用方，再重新扫一遍引用集，最后才删媒体——否则会把仍被活着的内容
引用的图片一起删掉。
"""

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_MEDIA_REF_RE = re.compile(r"/api/media/([0-9a-fA-F]{24,64})")
_SAMPLE_LIMIT = 5


def _media_refs(value: Any) -> set[str]:
    """从一条 URL 里取出媒体引用（public_id 32 位，历史 ObjectId 24 位）。"""
    if not isinstance(value, str):
        return set()
    return set(_MEDIA_REF_RE.findall(value))


async def _all_values(db, collection: str, field: str) -> set[str]:
    return {
        str(doc[field])
        async for doc in db[collection].find({field: {"$exists": True}}, {field: 1})
    }


async def _referenced_media_ids(db) -> set[str]:
    refs: set[str] = set()
    for collection, field in (
        ("messages", "media_url"),
        ("stickers", "media_url"),
        ("profiles", "reference_image_url"),
    ):
        async for doc in db[collection].find({field: {"$exists": True}}, {field: 1}):
            refs |= _media_refs(doc.get(field))
    return refs


async def collect(db) -> dict[str, list[str]]:
    """收集每个集合的孤儿 ID 清单。"""
    conv_ids = {str(doc["_id"]) async for doc in db.conversations.find({}, {"_id": 1})}
    conv_ids |= {
        str(doc["id"])
        async for doc in db.conversations.find({"id": {"$exists": True}}, {"id": 1})
    }

    orphans: dict[str, list[str]] = defaultdict(list)

    async for doc in db.messages.find({}, {"_id": 1, "conversation_id": 1}):
        if str(doc.get("conversation_id")) not in conv_ids:
            orphans["messages"].append(str(doc["_id"]))

    identity_ids = await _all_values(db, "identities", "id")
    async for doc in db.proactive_records.find({}, {"_id": 1, "identity_id": 1}):
        if doc.get("identity_id") not in identity_ids:
            orphans["proactive_records"].append(str(doc["_id"]))

    # 注入快照按 context_key 存（共享组优先于服务自身 id）
    context_keys = await _all_values(db, "services", "id")
    context_keys |= await _all_values(db, "services", "share_group_id")
    context_keys |= conv_ids
    async for doc in db.injection_snapshots.find(
        {}, {"_id": 1, "service_id": 1, "conversation_id": 1}
    ):
        keys = {str(doc.get("service_id")), str(doc.get("conversation_id"))}
        if not (keys & context_keys):
            orphans["injection_snapshots"].append(str(doc["_id"]))

    bot_ids = await _all_values(db, "bot_configs", "id")
    async for doc in db.wechat_context_tokens.find({}, {"_id": 1, "bot_id": 1}):
        if doc.get("bot_id") not in bot_ids:
            orphans["wechat_context_tokens"].append(str(doc["_id"]))

    # 媒体：未被任何现存引用指向的二进制（体积大头，最后删）
    referenced = await _referenced_media_ids(db)
    media_ids = {
        str(doc["public_id"])
        async for doc in db.media.find({"public_id": {"$exists": True}}, {"public_id": 1})
    }
    legacy_ids = {
        str(doc["_id"]) async for doc in db.media.find({}, {"_id": 1})
    }
    all_ids = media_ids | legacy_ids
    orphans["media"] = sorted(all_ids - referenced)
    return orphans


async def run(apply: bool) -> int:
    from bson import ObjectId

    from app.db.mongo import close_db, connect_db, get_database

    await connect_db()
    try:
        db = get_database()
        orphans = await collect(db)
        total = sum(len(ids) for ids in orphans.values())
        print("孤儿文档统计：")
        for name in (
            "messages", "proactive_records", "injection_snapshots",
            "wechat_context_tokens", "media",
        ):
            ids = orphans.get(name, [])
            print(f"  {name}: {len(ids)}  样例={ids[:_SAMPLE_LIMIT]}")
        if not total:
            print("没有孤儿文档，无需清理。")
            return 0
        if not apply:
            print(f"dry-run：共 {total} 条待删，未写库。备份后加 --apply 执行。")
            return 0

        # 引用方先删；删完重算媒体引用，避免把仍被活内容引用的图删掉
        deleted: dict[str, int] = {}
        for name, ids in orphans.items():
            if name == "media" or not ids:
                continue
            query_ids = [ObjectId(i) for i in ids]
            result = await db[name].delete_many({"_id": {"$in": query_ids}})
            deleted[name] = int(result.deleted_count)

        referenced_after = await _referenced_media_ids(db)
        media_docs = [
            doc
            async for doc in db.media.find({}, {"_id": 1, "public_id": 1})
        ]
        stale_object_ids = [
            doc["_id"]
            for doc in media_docs
            if str(doc["_id"]) not in referenced_after
            and str(doc.get("public_id") or "") not in referenced_after
        ]
        deleted["media"] = int(
            (await db.media.delete_many({"_id": {"$in": stale_object_ids}})).deleted_count
        )
        print(f"已删除: {deleted}")

        remaining = await collect(db)
        left = sum(len(ids) for ids in remaining.values())
        print(f"复核：剩余孤儿 {left} 条")
        return 1 if left else 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="清理父记录已删除的孤儿文档")
    parser.add_argument("--apply", action="store_true", help="实际删除（默认只统计）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__ == "__main__":
    main()
