"""一次性迁移：为存量媒体补 public_id，并改写引用 URL。

P0 媒体越权修复后，新媒体的 URL 使用不可枚举的 32 位 `public_id`；旧媒体只有
可枚举的 ObjectId，接口默认只允许管理员会话读取。本脚本把存量媒体补上 public_id，
并把 messages / profiles / stickers 中精确指向 `/api/media/<ObjectId>` 的 URL
改成 public_id，使旧图片重新对普通访问者可用。

用法（在 app 容器内执行，默认只统计不写库）：
    docker compose exec app python -m tools.migrations.backfill_media_public_id
    docker compose exec app python -m tools.migrations.backfill_media_public_id --apply

可重复执行、可续跑：映射按"全部媒体"建立，`public_id` 只在缺失时写入，URL 改写只命中
仍以 ObjectId 引用的记录。**不要**按"缺 public_id"筛媒体就提前返回——历史上出现过
"媒体已补 public_id、URL 还没改写"的半完成状态，那种状态下旧脚本会直接跳过。
"""

import argparse
import asyncio
import os
import re
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# ObjectId 形态：24 位十六进制（public_id 是 32 位，两者靠长度即可区分）
_OBJECT_ID_RE = re.compile(r"/api/media/[0-9a-fA-F]{24}$")

_TARGETS = (
    ("messages", "media_url"),
    ("profiles", "reference_image_url"),
    ("stickers", "media_url"),
)


async def run(apply: bool) -> int:
    from app.db.mongo import close_db, connect_db, get_database

    await connect_db()
    try:
        db = get_database()
        docs = await db.media.find({}, {"_id": 1, "public_id": 1}).to_list(None)
        mapping: dict[str, str] = {}
        missing: list[str] = []
        for doc in docs:
            public_id = doc.get("public_id") or uuid.uuid4().hex
            old_id = str(doc["_id"])
            mapping[old_id] = public_id
            if not doc.get("public_id"):
                missing.append(old_id)
        print(f"媒体总数: {len(mapping)}，其中待补 public_id: {len(missing)}")

        stale = 0
        for collection_name, field in _TARGETS:
            count = 0
            for old_id in mapping:
                count += await db[collection_name].count_documents(
                    {field: f"/api/media/{old_id}"}
                )
            stale += count
            print(f"  {collection_name}.{field} 仍按 ObjectId 引用: {count}")
        if not missing and not stale:
            print("已经是迁移后的状态，无需改写。")
            return 0
        if not apply:
            print("dry-run：未写库。确认后加 --apply 执行。")
            return 0

        # 1) 补 public_id（条件更新：已有值的绝不覆盖，避免并发写入被回滚）
        from bson import ObjectId

        for old_id in missing:
            await db.media.update_one(
                {"_id": ObjectId(old_id), "public_id": {"$exists": False}},
                {"$set": {"public_id": mapping[old_id]}},
            )

        # 2) 改写引用（只命中仍指向 ObjectId 的记录，重复执行为 0 条）
        rewritten: dict[str, int] = {}
        for collection_name, field in _TARGETS:
            count = 0
            for old_id, public_id in mapping.items():
                result = await db[collection_name].update_many(
                    {field: f"/api/media/{old_id}"},
                    {"$set": {field: f"/api/media/{public_id}"}},
                )
                count += int(result.modified_count)
            rewritten[collection_name] = count
        print(f"URL 改写完成: {rewritten}")

        # 3) 复核：不应再有 ObjectId 形态的媒体 URL
        leftovers = []
        for collection_name, field in _TARGETS:
            async for doc in db[collection_name].find({field: {"$exists": True}}, {field: 1}):
                value = doc.get(field) or ""
                if isinstance(value, str) and _OBJECT_ID_RE.search(value):
                    leftovers.append(f"{collection_name}.{field}={value}")
        print(f"复核：仍以 ObjectId 引用 {len(leftovers)} 条")
        for item in leftovers[:10]:
            print(f"  {item}")
        return 1 if leftovers else 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="存量媒体 public_id 回填")
    parser.add_argument("--apply", action="store_true", help="实际写库")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__ == "__main__":
    main()
