#!/usr/bin/env python3
"""
数据迁移脚本：回填 users.last_active_at 字段。

业务背景：
    主动消息调度依赖 ``users.last_active_at`` 判断用户活跃度，但部分历史用户的
    ``last_active_at`` 仍是注册时间，未被消息流转更新。本脚本扫描每个用户的服务
    及其会话，取最新的 ``last_message_at`` 回填到 ``users.last_active_at``。

数据来源说明：
    - ``services`` 集合本身没有 ``last_message_at`` 字段（兼容预留）；
    - ``last_message_at`` 实际位于 ``conversations`` 集合，按 ``service_id``
      关联到具体服务，再通过 ``service.user_id`` 归属到用户。
    - 因此本脚本的取值优先级为：
        1) services 集合中如显式存在 last_message_at（某些自定义部署可能有），优先使用；
        2) 否则通过 conversations 集合按 service_id 反查最新 last_message_at。

执行方式（容器内）：
    docker exec aiyichat-app-1 python /app/tools/migrations/backfill_last_active.py [--dry-run] [--yes]

执行方式（本地，需已配置 .env）：
    cd aiyichat && python tools/migrations/backfill_last_active.py [--dry-run] [--yes]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 确保可以从仓库根目录导入 ``app`` 包（脚本独立运行时）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase  # noqa: E402


def _resolve_mongo_config() -> tuple[str, str]:
    """解析 MongoDB 连接配置。

    优先从环境变量读取，其次尝试加载项目 ``app.config.settings``。

    Returns:
        (mongodb_uri, mongodb_database)
    """
    uri = os.environ.get("MONGODB_URI")
    db_name = os.environ.get("MONGODB_DATABASE")
    if uri and db_name:
        return uri, db_name

    try:
        # 复用项目的 .env 解析逻辑，保持与应用一致
        from app.config import settings  # type: ignore[import-not-found]
        return settings.mongodb_uri, settings.mongodb_database  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - 容错兜底
        print(f"[警告] 无法加载 app.config: {exc}，回退到默认 mongodb://localhost:27017")
        return (
            uri or "mongodb://localhost:27017",
            db_name or "aiyichat",
        )


async def _latest_message_time_for_service(
    db: AsyncIOMotorDatabase, service_doc: dict
) -> Optional[datetime]:
    """获取单个服务的最新消息时间。

    取值顺序：
        1) service_doc 中显式的 ``last_message_at``（兼容字段）；
        2) conversations 集合中 service_id 匹配的最新 ``last_message_at``；
        3) None（该服务无任何会话或消息）。
    """
    explicit = service_doc.get("last_message_at")
    if isinstance(explicit, datetime):
        return _ensure_utc(explicit)

    service_id = service_doc.get("id")
    if not service_id:
        return None

    conv_doc = await db.conversations.find_one(
        {"service_id": service_id, "last_message_at": {"$exists": True}},
        sort=[("last_message_at", -1)],
        projection={"last_message_at": 1},
    )
    if not conv_doc:
        return None
    val = conv_doc.get("last_message_at")
    return _ensure_utc(val) if isinstance(val, datetime) else None


def _ensure_utc(dt: datetime) -> datetime:
    """统一将无时区信息的 datetime 视为 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _latest_active_time_for_user(
    db: AsyncIOMotorDatabase, user_id: str
) -> Optional[datetime]:
    """汇总用户全部服务，返回最新消息时间。"""
    latest: Optional[datetime] = None
    cursor = db.services.find({"user_id": user_id})
    async for svc in cursor:
        ts = await _latest_message_time_for_service(db, svc)
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


async def _confirm_or_abort(message: str, *, assume_yes: bool) -> None:
    """终端确认提示；``--yes`` 时直接通过。"""
    if assume_yes:
        return
    if not sys.stdin.isatty():
        print(f"[提示] 非交互终端，未传 --yes，已自动跳过确认：{message}")
        return
    answer = input(f"{message} [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("[中止] 用户取消执行")
        raise SystemExit(2)


async def run_backfill(*, dry_run: bool, assume_yes: bool) -> int:
    """执行回填主流程。"""
    uri, db_name = _resolve_mongo_config()
    print(f"[配置] MongoDB URI: {uri}")
    print(f"[配置] MongoDB DB : {db_name}")
    print(f"[配置] 模式       : {'DRY-RUN（仅预演，不写库）' if dry_run else '正式写入'}")

    if not dry_run:
        await _confirm_or_abort(
            "即将正式写入 users.last_active_at，确认继续？",
            assume_yes=assume_yes,
        )

    client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
    try:
        db: AsyncIOMotorDatabase = client[db_name]

        total_users = await db.users.count_documents({})
        print(f"\n[开始] 共扫描 {total_users} 个用户\n")

        processed = 0
        updated = 0
        skipped_no_data = 0
        skipped_not_newer = 0
        errors = 0

        cursor = db.users.find({}, projection={"id": 1, "last_active_at": 1, "nickname": 1})
        async for user in cursor:
            processed += 1
            user_id = user.get("id")
            nickname = user.get("nickname") or "(无昵称)"
            current = user.get("last_active_at")
            current_utc = _ensure_utc(current) if isinstance(current, datetime) else None

            try:
                latest = await _latest_active_time_for_user(db, user_id) if user_id else None
            except Exception as exc:
                errors += 1
                print(f"  [错误] user_id={user_id} nickname={nickname} 查询失败: {exc}")
                continue

            if latest is None:
                skipped_no_data += 1
                if processed % 50 == 0 or processed == total_users:
                    print(f"  [进度] {processed}/{total_users} 已处理")
                continue

            if current_utc is not None and latest <= current_utc:
                skipped_not_newer += 1
                if processed % 50 == 0 or processed == total_users:
                    print(f"  [进度] {processed}/{total_users} 已处理")
                continue

            print(
                f"  [更新] user_id={user_id} nickname={nickname} "
                f"{current_utc.isoformat() if current_utc else 'None'} -> {latest.isoformat()}"
            )

            if not dry_run:
                await db.users.update_one(
                    {"id": user_id},
                    {"$set": {"last_active_at": latest}},
                )
            updated += 1

            if processed % 50 == 0 or processed == total_users:
                print(f"  [进度] {processed}/{total_users} 已处理")

        # ── 汇总统计 ────────────────────────────────────────────
        print("\n[完成] 迁移结束")
        print(f"  总用户数            : {total_users}")
        print(f"  实际处理            : {processed}")
        print(f"  成功更新            : {updated}{'（dry-run，未写库）' if dry_run else ''}")
        print(f"  跳过（无消息记录）  : {skipped_no_data}")
        print(f"  跳过（非更新时间）  : {skipped_not_newer}")
        print(f"  失败                : {errors}")
        return 0 if errors == 0 else 1
    finally:
        client.close()


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="回填 users.last_active_at（基于 services + conversations 的最新消息时间）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预演模式，仅打印将更新的内容，不写入数据库",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="跳过交互确认（CI/容器内静默执行时使用）",
    )
    return parser.parse_args()


def main() -> None:
    """脚本入口。"""
    args = _parse_args()
    exit_code = asyncio.run(run_backfill(dry_run=args.dry_run, assume_yes=args.yes))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
