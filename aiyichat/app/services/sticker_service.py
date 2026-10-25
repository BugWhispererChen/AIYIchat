"""表情包检索服务。

核心能力：
- 按语义描述检索最匹配的表情包（[表情:描述] 标签 → embedding 余弦相似度）
- 未启用向量化或库内无向量时，降级为标签关键词命中计分
- 斗图场景的随机选图（均限定在角色绑定的分组内）
- 进程内缓存启用表情列表与分组列表，后台改动/seed 完成后失效重建
"""

import logging
import random

from app.config import settings
from app.db.repositories.sticker_group_repo import StickerGroupRepository
from app.db.repositories.sticker_repo import StickerRepository
from app.models.service_memory import cosine_similarity
from app.models.sticker import StickerDocument
from app.models.sticker_group import StickerGroupDocument

logger = logging.getLogger(__name__)

_repo = StickerRepository()
_group_repo = StickerGroupRepository()

# 进程内缓存：启用表情包列表与分组列表（None 表示未加载，空列表表示库为空）
_sticker_cache: list[StickerDocument] | None = None
_group_cache: list[StickerGroupDocument] | None = None


def invalidate_sticker_cache() -> None:
    """失效表情包与分组缓存（后台上传/启停/删除、分组变更、seed 完成后调用）。"""
    global _sticker_cache, _group_cache
    _sticker_cache = None
    _group_cache = None


async def get_enabled_stickers() -> list[StickerDocument]:
    """获取启用表情包列表（进程内缓存，惰性加载）。"""
    global _sticker_cache
    if _sticker_cache is None:
        try:
            _sticker_cache = await _repo.list_enabled()
        except Exception as e:
            logger.warning("表情包列表加载失败: %s", e)
            _sticker_cache = []
    return _sticker_cache


async def get_all_groups() -> list[StickerGroupDocument]:
    """获取全部分组列表（含停用，进程内缓存，惰性加载）。"""
    global _group_cache
    if _group_cache is None:
        try:
            _group_cache = await _group_repo.list_all()
        except Exception as e:
            logger.warning("表情包分组列表加载失败: %s", e)
            _group_cache = []
    return _group_cache


async def get_stickers_for_groups(
    group_ids: list[str],
) -> list[StickerDocument]:
    """返回指定分组内的启用表情包（限定启用分组）。

    空列表入参返回空；绑定的分组均已停用或未归组表情时同样返回空。
    """
    if not group_ids:
        return []
    stickers = await get_enabled_stickers()
    if not stickers:
        return []
    groups = await get_all_groups()
    enabled_group_ids = {g.id for g in groups if g.enabled}
    wanted = set(group_ids) & enabled_group_ids
    if not wanted:
        return []
    return [s for s in stickers if s.group_id in wanted]


async def sticker_library_available(group_ids: list[str]) -> bool:
    """角色绑定的分组内表情库是否可用（非空），供 Prompt 注入与引擎开关判断。

    未绑定任何分组（空列表）时直接返回 False，Prompt 不注入表情包能力。
    """
    return bool(await get_stickers_for_groups(group_ids))


def _keyword_score(sticker: StickerDocument, query: str) -> int:
    """关键词兜底计分：名称/标签/描述与查询文本的相互包含命中数。"""
    score = 0
    q = query.lower()
    for kw in [sticker.name, *sticker.tags]:
        kw = kw.lower().strip()
        if not kw:
            continue
        if kw in q or q in kw:
            score += 1
    # 描述中的命中弱计分（避免长描述刷分，最多 +1）
    if sticker.description and any(
        t.lower() in sticker.description.lower() for t in query.split() if len(t) >= 2
    ):
        score += 1
    return score


async def search_sticker(
    desc: str, group_ids: list[str]
) -> StickerDocument | None:
    """按语义描述在角色绑定分组内检索最匹配的表情包。

    策略：
    1. 向量化可用且库内有向量时，按余弦相似度取最高分，
       低于阈值（sticker_similarity_threshold）视为未命中；
    2. 降级链路：按标签关键词命中数计分，取得分最高者随机选取；
    3. 全部未命中返回 None（宁可不发，不发无关表情）。
    """
    desc = (desc or "").strip()
    if not desc:
        return None
    stickers = await get_stickers_for_groups(group_ids)
    if not stickers:
        return None

    # 语义检索：查询向量化 + 与库内向量逐一算相似度
    from app.services.embedding_service import embed_within_budget, get_embedding_service

    emb_service = get_embedding_service()
    # 表情包在文字已经送达之后才发，端点长挂同样会把整轮收尾拖死 → 共用检索超时预算
    query_vec = await embed_within_budget(emb_service, desc, what="表情包检索")
    if query_vec:
        best: StickerDocument | None = None
        best_score = 0.0
        for s in stickers:
            if not s.embedding:
                continue
            score = cosine_similarity(query_vec, s.embedding)
            if score > best_score:
                best, best_score = s, score
        if best is not None and best_score >= settings.sticker_similarity_threshold:
            await _bump_usage(best)
            logger.info(
                "表情包语义命中: desc=%s, name=%s, score=%.3f",
                desc[:30], best.name, best_score,
            )
            return best
        # 向量全部缺失时继续走关键词兜底

    # 关键词兜底：按命中数取最高分组内随机
    scored = [
        (s, _keyword_score(s, desc)) for s in stickers
    ]
    top_score = max(score for _, score in scored)
    if top_score > 0:
        candidates = [s for s, score in scored if score == top_score]
        hit = random.choice(candidates)
        await _bump_usage(hit)
        logger.info(
            "表情包关键词命中: desc=%s, name=%s, score=%d",
            desc[:30], hit.name, top_score,
        )
        return hit
    return None


async def pick_random_sticker(group_ids: list[str]) -> StickerDocument | None:
    """在角色绑定分组内随机选取一张启用表情包（斗图以图回图场景）。"""
    stickers = await get_stickers_for_groups(group_ids)
    if not stickers:
        return None
    hit = random.choice(stickers)
    await _bump_usage(hit)
    return hit


async def _bump_usage(sticker: StickerDocument) -> None:
    """使用计数 +1（失败仅告警，不影响发送）。"""
    try:
        await _repo.incr_usage_count(sticker.id)
    except Exception as e:
        logger.warning("表情包使用计数更新失败: %s", e)
