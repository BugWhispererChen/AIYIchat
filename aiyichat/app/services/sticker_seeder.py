"""预置表情包资源 seed 机制。

应用启动时扫描 data/stickers/ 目录（图片 + manifest.json），
按内容哈希幂等导入 MongoDB stickers 集合，并后台回填语义向量；
manifest 中的 group 字段按名称 upsert 到 sticker_groups 集合并写入
表情包的 group_id，存量哈希命中的老表情包若 group_id 为空则幂等回填。
素材与 manifest 在开发期一次性生成后随仓库分发，运行时不依赖外网。
"""

import hashlib
import json
import logging
from pathlib import Path

from app.config import settings
from app.models.sticker import StickerDocument

logger = logging.getLogger(__name__)

# 预置资源目录：aiyichat/data/stickers/
STICKERS_DIR = Path(__file__).parent.parent.parent / "data" / "stickers"

# 文件后缀 → MIME 类型映射（GIF 保留原图以维持动画）
_CONTENT_TYPE_MAP: dict[str, str] = {
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _load_manifest() -> list[dict]:
    """读取 manifest.json，失败返回空列表。"""
    manifest_path = STICKERS_DIR / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        return entries if isinstance(entries, list) else []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("表情包 manifest 读取失败: %s", e)
        return []


async def seed_stickers() -> None:
    """启动时导入预置表情包（幂等：按内容哈希去重，已存在则跳过）。"""
    if not settings.sticker_seed_enabled:
        logger.info("表情包 seed 已关闭（sticker_seed_enabled=False）")
        return

    entries = _load_manifest()
    if not entries:
        logger.info("未发现预置表情包资源（%s）", STICKERS_DIR)
        return

    from app.db.repositories.media_repo import MediaRepository
    from app.db.repositories.sticker_group_repo import StickerGroupRepository
    from app.db.repositories.sticker_repo import StickerRepository
    from app.models.sticker_group import StickerGroupDocument
    from app.services.sticker_service import invalidate_sticker_cache

    media_repo = MediaRepository()
    sticker_repo = StickerRepository()
    group_repo = StickerGroupRepository()

    # 分组名称 → ID 缓存（按名称 upsert，重名不重复创建）
    group_id_cache: dict[str, str] = {}

    async def _resolve_group_id(group_name: str) -> str:
        if not group_name:
            return ""
        if group_name not in group_id_cache:
            group = await group_repo.find_by_name(group_name)
            if group:
                group_id_cache[group_name] = group.id
            else:
                group_id_cache[group_name] = await group_repo.create(
                    StickerGroupDocument(name=group_name)
                )
                logger.info("seed 创建表情包分组: %s", group_name)
        return group_id_cache[group_name]

    imported = 0
    skipped = 0
    backfilled = 0
    imported_ids: list[str] = []

    for entry in entries:
        try:
            filename = str(entry.get("file", ""))
            file_path = STICKERS_DIR / filename
            if not filename or not file_path.exists():
                logger.warning("预置表情包文件缺失，跳过: %s", filename)
                continue

            data = file_path.read_bytes()
            content_hash = hashlib.sha256(data).hexdigest()
            group_id = await _resolve_group_id(str(entry.get("group", "")).strip())

            # 幂等去重：同内容已导入则跳过（仅幂等回填缺失的分组）
            existing = await sticker_repo.get_by_hash(content_hash)
            if existing:
                skipped += 1
                if group_id and not existing.group_id:
                    await sticker_repo.update_fields(
                        existing.id, {"group_id": group_id}
                    )
                    backfilled += 1
                continue

            content_type = _CONTENT_TYPE_MAP.get(
                file_path.suffix.lower(), "image/jpeg"
            )
            media_id = await media_repo.create(
                data=data, content_type=content_type, filename=filename
            )
            sticker = StickerDocument(
                name=str(entry.get("name", "")) or file_path.stem,
                tags=[str(t) for t in entry.get("tags", []) if str(t).strip()],
                description=str(entry.get("description", "")).strip(),
                media_url=f"/api/media/{media_id}",
                content_type=content_type,
                content_hash=content_hash,
                source=str(entry.get("source", "preset")),
                group_id=group_id,
                enabled=bool(entry.get("enabled", True)),
            )
            sticker_id = await sticker_repo.create(sticker)
            imported_ids.append(sticker_id)
            imported += 1
        except Exception as e:
            logger.warning("预置表情包导入失败（%s）: %s", entry, e)

    logger.info(
        "预置表情包 seed 完成: 导入 %d 张, 跳过 %d 张（已存在）, "
        "分组回填 %d 张, 分组总数 %d",
        imported, skipped, backfilled, len(group_id_cache),
    )

    # 新数据入库后失效检索缓存，使后台改动即时生效
    invalidate_sticker_cache()

    # 后台回填语义向量（不阻塞启动；未配置向量化时静默跳过）
    if imported_ids:
        from app.utils.tasks import spawn

        spawn(_backfill_embeddings(imported_ids))


async def _backfill_embeddings(sticker_ids: list[str]) -> None:
    """后台为新导入表情包批量生成语义向量（fire-and-forget）。"""
    try:
        from app.db.repositories.sticker_repo import StickerRepository
        from app.services.embedding_service import get_embedding_service

        emb_service = get_embedding_service()
        if not emb_service.enabled:
            logger.info("Embedding 未启用，跳过表情包向量回填（走关键词兜底）")
            return

        repo = StickerRepository()
        pending: list[tuple[str, str]] = []  # (sticker_id, 向量化文本)
        for sid in sticker_ids:
            s = await repo.find_by_id(sid)
            if s and not s.embedding:
                text = s.description or "；".join(s.tags) or s.name
                if text.strip():
                    pending.append((sid, text))

        if not pending:
            return

        vectors = await emb_service.embed_batch([t for _, t in pending])
        ok = 0
        for (sid, _), vec in zip(pending, vectors, strict=True):
            if vec:
                await repo.set_embedding(sid, vec)
                ok += 1
        logger.info("表情包向量回填完成: %d/%d", ok, len(pending))
    except Exception as e:
        logger.warning("表情包向量回填失败: %s", e)
