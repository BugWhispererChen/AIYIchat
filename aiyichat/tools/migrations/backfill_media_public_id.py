"""一次性迁移：为存量媒体补 public_id，并改写引用 URL。

P0 媒体越权修复后，新媒体的 URL 使用不可枚举的 32 位 `public_id`；旧媒体只有
可枚举的 ObjectId，接口默认只允许管理员会话读取。本脚本把存量媒体补上 public_id，
并把 messages / profiles / stickers 中精确指向 `/api/media/<ObjectId>` 的 URL
改成 public_id，使旧图片重新对普通访问者可用。

用法（在 app 容器内执行，默认只统计不写库）：
    docker compose run --rm app python -m tools.migrations.backfill_media_public_id
确认后加 --apply 实际改写。脚本可重复执行（只处理缺 public_id 的媒体）。
"""

import argparse
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)


async def run(apply: bool) -> int:
    from app.db.mongo import close_db, connect_db, get_database

    await connect_db()
    try:
        db = get_database()
        docs = await db.media.find(
            {"public_id": {"$exists": False}}, {"_id": 1}
        ).to_list(None)
        mapping = {str(doc["_id"]): uuid.uuid4().hex for doc in docs}
        print(f"待补 public_id 的媒体: {len(mapping)} 条")
        if not mapping:
            return 0
        if not apply:
            print("dry-run：未写库。确认后加 --apply 执行。")
            return 0

        # 1) 先给媒体补 public_id（条件更新，避免覆盖并发写入的值）
        for old_id, public_id in mapping.items():
            from bson import ObjectId

            await db.media.update_one(
                {"_id": ObjectId(old_id), "public_id": {"$exists": False}},
                {"$set": {"public_id": public_id}},
            )

        # 2) 改写引用这些媒体的已知字段
        targets = (
            ("messages", "media_url"),
            ("profiles", "reference_image_url"),
            ("stickers", "media_url"),
        )
        rewritten: dict[str, int] = {}
        for collection_name, field in targets:
            collection = db[collection_name]
            count = 0
            for old_id, public_id in mapping.items():
                result = await collection.update_many(
                    {field: f"/api/media/{old_id}"},
                    {"$set": {field: f"/api/media/{public_id}"}},
                )
                count += int(result.modified_count)
            rewritten[collection_name] = count
        print(f"URL 改写完成: {rewritten}")
        return 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="存量媒体 public_id 回填")
    parser.add_argument("--apply", action="store_true", help="实际写库")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__ == "__main__":
    main()
