"""一次性迁移：把 MongoDB 存量数据里的 user 体系键名改为 identity 体系。

配套代码改名「身份 → 身份」使用。按本项目既有惯例（见 tools/archive/README.md），
仓库没有迁移框架，迁移脚本一次性手工执行后即归档。

操作顺序是**有序的**，存在语义依赖，不可调换：
  1) web_sessions.identity_id -> credential_id
     必须先做。改名后 identity_id 这个键名将专门表示"指向 identities"，
     而它当前指向 web_identities。先腾空旧键名，避免出现同名双含义的歧义窗口。
  2) 集合 web_identities -> web_credentials
  3) web_credentials.user_id -> identity_id（此时再引入该键名，含义已唯一）
  4) 集合 users -> identities
  5) identities.identity_description -> self_description
  6) 其余跨集合外键 user_id -> identity_id 等
  7) service_memories 的 entities 数组元素内 relation_to_user -> relation_to_identity
  8) profiles.identity -> profiles.persona（角色人设，避让"身份"一词）
  9) 清理指向旧字段路径的僵尸索引

用法（在 app 容器内执行，依赖与凭据都已就位）：
    docker compose run --rm app python tools/migrations/rename_user_to_identity.py --dry-run
    docker compose run --rm app python tools/migrations/rename_user_to_identity.py --yes
    docker compose run --rm app python tools/migrations/rename_user_to_identity.py --rollback --yes

执行前必须先备份：服务器 cron 每日 03:00 的 mongodump，或手动跑一次。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, replace
from typing import Any, Union

from pymongo import MongoClient


@dataclass(frozen=True)
class RenameCollection:
    """集合改名（用 renameCollection，数据与索引随行）。"""

    old: str
    new: str


@dataclass(frozen=True)
class RenameFields:
    """字段改名。

    array_field 非空时表示旧键位于该数组字段的元素内部，$rename 打不到，
    需逐文档在 Python 侧改写。
    """

    collection: str
    pairs: tuple[tuple[str, str], ...]
    array_field: str = ""


Op = Union[RenameCollection, RenameFields]

# 集合改名的旧名 -> 新名，供自检与索引清理时换算集合名
ALIAS_COLLECTIONS: dict[str, str] = {
    "web_identities": "web_credentials",
    "users": "identities",
}

# 有序操作序列：集合改名与字段改名交错，列表顺序即执行顺序
OPS: list[Op] = [
    RenameFields("web_sessions", (("identity_id", "credential_id"),)),
    RenameCollection("web_identities", "web_credentials"),
    RenameFields("web_credentials", (("user_id", "identity_id"),)),
    RenameCollection("users", "identities"),
    RenameFields("identities", (("identity_description", "self_description"),)),
    RenameFields("services", (("user_id", "identity_id"),)),
    RenameFields("conversations", (("user_id", "identity_id"),)),
    RenameFields("safety_events", (("user_id", "identity_id"),)),
    RenameFields(
        "proactive_records",
        (("user_id", "identity_id"), ("platform_user_id", "platform_openid")),
    ),
    RenameFields(
        "service_memories",
        (("relation_to_user", "relation_to_identity"),),
        array_field="entities",
    ),
    RenameFields("profiles", (("identity", "persona"),)),
]

# 代码已不再使用的 (集合, 旧键)，用于精确清理僵尸索引。
# 必须按 (集合, 键) 匹配而非按键名全局删：user_id 在 wechat_context_tokens 里
# 仍是合法字段（存平台 openid），按名删会误伤。
DEPRECATED_INDEX_TARGETS: set[tuple[str, str]] = {
    ("identities", "identity_description"),
    ("services", "user_id"),
    ("conversations", "user_id"),
    ("safety_events", "user_id"),
    ("proactive_records", "user_id"),
    ("proactive_records", "platform_user_id"),
    ("web_credentials", "user_id"),
    ("web_sessions", "identity_id"),
    ("service_memories", "relation_to_user"),
    ("profiles", "identity"),
}


def index_targets_from_ops(ops: list[Op]) -> set[tuple[str, str]]:
    """由操作序列推导需清理的 (集合, 旧键)，供单测校验白名单没有漏项。"""
    out: set[tuple[str, str]] = set()
    for op in ops:
        if isinstance(op, RenameFields):
            coll = ALIAS_COLLECTIONS.get(op.collection, op.collection)
            for old, _ in op.pairs:
                out.add((coll, old))
    return out


def inverted_ops(ops: list[Op]) -> list[Op]:
    """反向操作序列：字段先改回、集合再改回，顺序与正向严格相反。"""
    inv: list[Op] = []
    for op in reversed(ops):
        if isinstance(op, RenameCollection):
            inv.append(RenameCollection(op.new, op.old))
        else:
            inv.append(replace(op, pairs=tuple((n, o) for o, n in op.pairs)))
    return inv


def _has_collection(db: Any, name: str) -> bool:
    return name in db.list_collection_names()


def _rename_in_array(db: Any, op: RenameFields, dry: bool) -> None:
    """改写数组字段元素内的键名（$rename 不支持无数组下标的元素内改名）。"""
    coll = db[op.collection]
    query = {f"{op.array_field}.{old}": {"$exists": True} for old, _ in op.pairs}
    changed = 0
    for doc in coll.find(query):
        items = doc.get(op.array_field) or []
        for item in items:
            if not isinstance(item, dict):
                continue
            for old, new in op.pairs:
                if old in item and new not in item:
                    item[new] = item.pop(old)
        if not dry:
            coll.update_one({"_id": doc["_id"]}, {"$set": {op.array_field: items}})
        changed += 1
    for old, new in op.pairs:
        suffix = "（dry-run 未落库）" if dry else ""
        print(f"    {op.collection}.{op.array_field}[].{old} -> {new}: {changed} 个文档{suffix}")


def _run_op(db: Any, database: str, op: Op, dry: bool) -> None:
    """执行单个操作。重复执行为幂等（靠 $exists 判定跳过已改名的文档）。"""
    if isinstance(op, RenameCollection):
        names = db.list_collection_names()
        if op.old not in names:
            print(f"    {op.old}: 不存在，跳过")
            return
        if op.new in names:
            # 绝不允许静默覆盖已存在的目标集合，交人工判断
            print(f"    !! 目标集合 {op.new} 已存在而 {op.old} 仍在，中止")
            sys.exit(1)
        count = db[op.old].count_documents({})
        if not dry:
            db.client.admin.command(
                "renameCollection",
                f"{database}.{op.old}",
                to=f"{database}.{op.new}",
            )
        suffix = "（dry-run 未落库）" if dry else ""
        print(f"    集合 {op.old} -> {op.new}: {count} 个文档{suffix}")
        return

    if not _has_collection(db, op.collection):
        print(f"    {op.collection}: 集合不存在，跳过")
        return
    if op.array_field:
        _rename_in_array(db, op, dry)
        return
    for old, new in op.pairs:
        count = db[op.collection].count_documents({old: {"$exists": True}})
        if count == 0:
            print(f"    {op.collection}.{old}: 无需处理（0 个文档）")
            continue
        if not dry:
            db[op.collection].update_many(
                {old: {"$exists": True}, new: {"$exists": False}},
                {"$rename": {old: new}},
            )
        suffix = "（dry-run 未落库）" if dry else ""
        print(f"    {op.collection}.{old} -> {new}: {count} 个文档{suffix}")


def _run_ops(db: Any, database: str, ops: list[Op], dry: bool) -> None:
    for op in ops:
        _run_op(db, database, op, dry)


def _drop_deprecated_indexes(db: Any, dry: bool) -> None:
    """清理指向旧字段路径的索引。

    app/db/mongo.py 的 _ensure_indexes 只增不删：字段改名后旧索引不会自动回收，
    既用不上又拖慢写入，故按 (集合, 键) 白名单精确删除。
    """
    for coll_name in db.list_collection_names():
        coll = db[coll_name]
        for name, spec in coll.index_information().items():
            if name == "_id_":
                continue
            raw = spec.get("key")
            keys: set[str] = set(raw.keys()) if isinstance(raw, dict) else {k for k, _ in raw}
            if any((coll_name, k) in DEPRECATED_INDEX_TARGETS for k in keys):
                print(f"    drop {coll_name}.{name} keys={sorted(keys)}")
                if not dry:
                    coll.drop_index(name)


def _verify(db: Any) -> list[str]:
    """迁移后自检：旧集合与旧键都不应再存在。"""
    problems: list[str] = []
    names = set(db.list_collection_names())
    for old in ALIAS_COLLECTIONS:
        if old in names:
            problems.append(f"旧集合仍存在: {old}")
    for coll, key in sorted(DEPRECATED_INDEX_TARGETS):
        if coll not in names:
            continue
        n = db[coll].count_documents({key: {"$exists": True}})
        if n:
            problems.append(f"{coll}.{key} 仍有 {n} 个文档")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="user -> identity 存量数据改名")
    parser.add_argument("--dry-run", action="store_true", help="只统计不落库（默认）")
    parser.add_argument("--yes", action="store_true", help="确认执行写操作")
    parser.add_argument("--rollback", action="store_true", help="反向改回 user 体系")
    args = parser.parse_args()

    if args.dry_run and args.yes:
        print("--dry-run 与 --yes 互斥")
        sys.exit(2)
    dry = not args.yes

    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    database = os.getenv("MONGODB_DATABASE", "aiyichat")
    db: Any = MongoClient(uri)[database]

    mode = "回滚" if args.rollback else ("DRY-RUN（不写库）" if dry else "执行")
    print(f"库: {database}  模式: {mode}")

    if args.rollback:
        if dry:
            print("回滚需显式加 --yes")
            sys.exit(2)
        _run_ops(db, database, inverted_ops(OPS), dry=False)
        print("回滚完成（索引将由下次启动按旧字段重建）")
        return

    print("[1/3] 有序字段/集合改名")
    _run_ops(db, database, OPS, dry)
    print("[2/3] 清理僵尸索引")
    _drop_deprecated_indexes(db, dry)

    if dry:
        print("[3/3] dry-run 结束，未写库。核对上述计数后加 --yes 执行。")
        return

    print("[3/3] 自检")
    problems = _verify(db)
    if problems:
        print("!! 自检未通过:")
        for p in problems:
            print("   -", p)
        sys.exit(1)
    print("自检通过。请重启 app 让 _ensure_indexes 建立新字段索引。")


if __name__ == "__main__":
    main()
