"""一次性迁移：记忆事实的指称归一与点状时效回填（P0 记忆治理）。

为什么需要它：治理开关只作用于**新写入**的事实，而测试服取证到的脏数据全是存量
（"用户与AI关系亲密，互称老公/老婆"、"身份喜欢在周末爬山"）。这类事实会长期注入，
角色读到的是第三人称与"我是AI"，属于持续性出戏，必须就地改写。

三段操作，各自可独立重跑（幂等）：
  1) 指称归一：`用户…`/`身份…` → `对方…`，`与AI…` → `与我…`（与写入路径同一实现）
  2) 点状回填：存量事实没有 `punctual` 标记，但其中 `valid_at` 严格晚于
     `created_at` 的那批，写入当时讲的确实是未来 → 补打标，交由读取路径剔除
  3) 体检报告：调用 audit_memory 打印前后计数（脏指称/点状陈旧/完全重复）

用法（在 app 容器内执行，只统计不写库）：
    docker compose run --rm app python -m tools.migrations.normalize_memory_referents --dry-run
确认无误后去掉 --dry-run 实际改写。脚本逐条打印被改动的原文与改后文本，便于人工抽查。
"""

import argparse
import asyncio
import os
import sys
from datetime import timezone

# 按文件路径执行时 sys.path[0] 是脚本所在目录（tools/migrations），
# 需回到项目根才能导入 app 包：本文件位于 <根>/tools/migrations/xxx.py
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)


async def run(dry_run: bool) -> int:
    from app.conversation.memory_hygiene import (
        audit_memory,
        is_referent_dirty,
        normalize_referent,
    )
    from app.db.mongo import close_db, connect_db, get_database
    from app.models.service_memory import MemoryFact, ServiceMemoryInDB

    await connect_db()
    db = get_database()
    docs = await db.service_memories.find(
        {"facts.0": {"$exists": True}}, {"service_id": 1, "facts": 1}
    ).to_list(None)

    total_changed = 0
    total_marked = 0
    for doc in docs:
        service_id = doc["service_id"]
        raw_facts = doc.get("facts") or []
        # from_mongo 负责给存量事实补 fid（早期文档无此字段）
        facts = list(ServiceMemoryInDB.from_mongo(
            {**doc, "_id": str(doc["_id"])}
        ).facts)
        before = audit_memory(facts)

        updates: list[dict | None] = []
        for raw, fact in zip(raw_facts, facts, strict=True):
            content = raw.get("content") or ""
            new_content, changed = normalize_referent(content)
            patch: dict[str, object] = {}
            if changed:
                patch["content"] = new_content
                print(f"  [指称] {service_id[:8]} {content!r} -> {new_content!r}")
            # 点状回填：valid_at 严格晚于写入时刻 ⇒ 那句话当时讲的是未来
            valid_at, created_at = fact.valid_at, fact.created_at
            if (
                valid_at is not None
                and created_at is not None
                and valid_at.astimezone(timezone.utc)
                > created_at.astimezone(timezone.utc)
                and not fact.punctual
            ):
                patch["punctual"] = True
                total_marked += 1
                print(f"  [点状] {service_id[:8]} {content!r} valid_at={valid_at:%Y-%m-%d}")
            updates.append(patch or None)

        changed_n = sum(1 for u in updates if u)
        if changed_n == 0:
            continue
        total_changed += sum(1 for u in updates if u and "content" in u)
        if dry_run:
            continue
        # 按下标写回：facts 数组内 content 不唯一时（完全重复）无法用 content 寻址
        for i, upd in enumerate(updates):
            if upd:
                await db.service_memories.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {f"facts.{i}.{k}": v for k, v in upd.items()}},
                )

        after_facts = [
            MemoryFact.model_validate({**raw, **(upd or {})})
            for raw, upd in zip(raw_facts, updates, strict=True)
        ]
        after = audit_memory(after_facts)
        dirty_after = sum(
            1 for f in after_facts if is_referent_dirty(f.content)
        )
        print(
            f"  服务 {service_id[:12]}: 脏指称 {before['dirty_referent']}->{after['dirty_referent']}"
            f"  点状陈旧 {before['punctual_stale']}->{after['punctual_stale']}"
            f"  完全重复 {before['exact_duplicate']}"
        )
        assert after["dirty_referent"] == dirty_after, "改写后脏指称计数应自洽"

    await close_db()
    mode = "试运行" if dry_run else "已改写"
    print(
        f"\n[{mode}] 扫描 {len(docs)} 个服务，改写 {total_changed} 条指称，"
        f"回填点状标记 {total_marked} 条"
    )
    if dry_run:
        print("以上为将要执行的操作，去掉 --dry-run 才会真正写库。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆事实指称归一与点状时效回填")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印将要执行的改动，不写库"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
