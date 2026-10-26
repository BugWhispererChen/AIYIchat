"""Admin 表情包库管理路由。

提供表情库列表、上传、启停、删除，以及分组管理（创建/启停/删除/归组）。
上传时描述留空则调 Vision 自动生成；保存即生成语义向量并失效检索缓存，热生效。
"""

import base64
import logging
import re
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _render

logger = logging.getLogger(__name__)

stickers_router = APIRouter(tags=["admin"])

# 上传大小上限（2MB，表情包通常远小于此值）
_MAX_UPLOAD_SIZE = 2 * 1024 * 1024

# 允许上传的图片类型
_ALLOWED_CONTENT_TYPES = {
    "image/gif", "image/png", "image/jpeg", "image/webp",
}

# 列表筛选与分页：200 张量级在内存里过滤即可，无需下推到 Mongo
_PAGE_SIZE = 60
_STATE_FILTERS = {"all", "enabled", "disabled", "novector"}
_SORT_OPTIONS = {"default", "usage", "name"}


def _parse_tags(raw: str) -> list[str]:
    """解析标签输入：支持逗号、分号、空白分隔，去重去空。"""
    parts = re.split(r"[，,;；、\s]+", raw or "")
    seen: set[str] = set()
    tags: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            tags.append(p)
    return tags


async def _auto_describe(data: bytes, content_type: str) -> str:
    """调 Vision 为上传的表情包自动生成语义描述（失败或未启用返回空）。"""
    from app.config import settings

    if not settings.vision_enabled:
        return ""
    try:
        from app.services.vision_service import get_vision_service

        b64 = base64.b64encode(data).decode()
        desc = await get_vision_service().describe_image(
            f"data:{content_type};base64,{b64}"
        )
        # Vision 按表情包格式输出时去掉前缀，只保留含义描述
        if desc.startswith("[表情包]"):
            desc = desc.replace("[表情包]", "", 1)
            desc = desc.replace("含义：", "", 1).replace("含义:", "", 1)
        return desc.strip()
    except Exception as e:
        logger.warning("表情包自动描述失败: %s", e)
        return ""


async def _gen_embedding(text: str) -> list[float]:
    """为描述文本生成语义向量（未启用或失败返回空，走关键词兜底）。"""
    if not text.strip():
        return []
    from app.services.embedding_service import get_embedding_service

    emb = get_embedding_service()
    if not emb.enabled:
        return []
    try:
        return await emb.embed(text)
    except Exception as e:
        logger.warning("表情包向量生成失败: %s", e)
        return []


@stickers_router.get("/stickers", response_class=HTMLResponse)
async def stickers_page(
    request: Request,
    group: str = "",
    q: str = "",
    state: str = "all",
    sort: str = "default",
    page: int = 1,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """表情库管理页面：检索/筛选/分页 + 分组筛选 + 上传与管理面板。"""
    from app.config import settings
    from app.db.repositories.sticker_group_repo import StickerGroupRepository
    from app.db.repositories.sticker_repo import StickerRepository

    all_stickers = await StickerRepository().list_all()
    groups = await StickerGroupRepository().list_all()
    total = len(all_stickers)
    # 各分组表情数量（含未归组），分组胶囊展示用，不受筛选影响
    group_counts: dict[str, int] = {"": 0}
    for s in all_stickers:
        group_counts[s.group_id] = group_counts.get(s.group_id, 0) + 1

    group = group.strip()
    keyword = q.strip()
    state = state if state in _STATE_FILTERS else "all"
    sort = sort if sort in _SORT_OPTIONS else "default"

    filtered = all_stickers
    if group:
        # 筛选：未归组传 "none"，其余按分组 ID 精确匹配
        if group == "none":
            filtered = [s for s in filtered if not s.group_id]
        else:
            filtered = [s for s in filtered if s.group_id == group]
    if keyword:
        needle = keyword.casefold()
        filtered = [
            s for s in filtered
            if needle in s.name.casefold()
            or any(needle in t.casefold() for t in s.tags)
            or needle in (s.description or "").casefold()
        ]
    if state == "enabled":
        filtered = [s for s in filtered if s.enabled]
    elif state == "disabled":
        filtered = [s for s in filtered if not s.enabled]
    elif state == "novector":
        filtered = [s for s in filtered if not s.embedding]
    if sort == "usage":
        filtered = sorted(filtered, key=lambda s: s.usage_count, reverse=True)
    elif sort == "name":
        filtered = sorted(filtered, key=lambda s: s.name)

    filtered_total = len(filtered)
    total_pages = max(1, (filtered_total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * _PAGE_SIZE
    page_stickers = filtered[start:start + _PAGE_SIZE]

    # 链接参数：分组链接用 base_query（不含 group），翻页用 full_query
    filter_params: list[tuple[str, str]] = []
    if group:
        filter_params.append(("group", group))
    if keyword:
        filter_params.append(("q", keyword))
    if state != "all":
        filter_params.append(("state", state))
    if sort != "default":
        filter_params.append(("sort", sort))
    base_query = urlencode([(k, v) for k, v in filter_params if k != "group"])
    full_query = urlencode(filter_params)
    return_params = list(filter_params)
    if page > 1:
        return_params.append(("page", str(page)))
    return_url = "/admin/stickers"
    if return_params:
        return_url += "?" + urlencode(return_params)

    return _render(request, "stickers.html", {
        "stickers": page_stickers,
        "groups": groups,
        "group_counts": group_counts,
        "group_filter": group,
        "q": keyword,
        "state_filter": state,
        "sort": sort,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "filtered_total": filtered_total,
        "base_query": base_query,
        "full_query": full_query,
        "return_url": return_url,
        "panel": request.query_params.get("panel", ""),
        "sticker_enabled": settings.sticker_enabled,
        "active_page": "stickers",
    })


# ── 分组管理 ─────────────────────────────


@stickers_router.post("/stickers/groups/create")
async def sticker_group_create(
    _s: str = Depends(require_admin),
    name: str = Form(""),
    description: str = Form(""),
):
    """创建表情包分组（名称唯一，查重拒绝）。"""
    from app.db.repositories.sticker_group_repo import StickerGroupRepository
    from app.models.sticker_group import StickerGroupDocument
    from app.services.sticker_service import invalidate_sticker_cache

    group_name = name.strip()
    if not group_name:
        return _redirect_with_error("分组名称不能为空", "/admin/stickers?panel=group")
    repo = StickerGroupRepository()
    if await repo.find_by_name(group_name):
        return _redirect_with_error("已存在同名分组，请更换名称", "/admin/stickers?panel=group")
    await repo.create(StickerGroupDocument(
        name=group_name, description=description.strip(),
    ))
    invalidate_sticker_cache()
    logger.info("表情包分组已创建: %s", group_name)
    return RedirectResponse(url="/admin/stickers?panel=group", status_code=303)


@stickers_router.post("/stickers/groups/{group_id}/toggle")
async def sticker_group_toggle(
    group_id: str,
    _s: str = Depends(require_admin),
    next: str = Form(""),
):
    """启用/停用分组（停用后该分组内表情不参与检索与发送）。"""
    from app.db.repositories.sticker_group_repo import StickerGroupRepository
    from app.services.sticker_service import invalidate_sticker_cache

    repo = StickerGroupRepository()
    group = await repo.find_by_id(group_id)
    if group:
        await repo.update_fields(group_id, {"enabled": not group.enabled})
        invalidate_sticker_cache()
    return RedirectResponse(url=_safe_next(next), status_code=303)


@stickers_router.post("/stickers/groups/{group_id}/delete")
async def sticker_group_delete(
    group_id: str,
    _s: str = Depends(require_admin),
    next: str = Form(""),
):
    """删除分组（级联将引用表情包的 group_id 置空，表情本体保留）。"""
    from app.db.repositories.sticker_group_repo import StickerGroupRepository
    from app.db.repositories.sticker_repo import StickerRepository
    from app.services.sticker_service import invalidate_sticker_cache

    repo = StickerGroupRepository()
    if await repo.delete(group_id):
        cleared = await StickerRepository().clear_group(group_id)
        invalidate_sticker_cache()
        logger.info("表情包分组已删除: %s（%d 张表情转为未归组）", group_id, cleared)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@stickers_router.post("/stickers/{sticker_id}/set-group")
async def sticker_set_group(
    sticker_id: str,
    _s: str = Depends(require_admin),
    group_id: str = Form(""),
    next: str = Form(""),
):
    """调整单张表情包的所属分组（空值表示移入未归组）。"""
    from app.db.repositories.sticker_repo import StickerRepository
    from app.services.sticker_service import invalidate_sticker_cache

    repo = StickerRepository()
    if await repo.find_by_id(sticker_id):
        await repo.update_fields(sticker_id, {"group_id": group_id.strip()})
        invalidate_sticker_cache()
    return RedirectResponse(url=_safe_next(next), status_code=303)


@stickers_router.post("/stickers/upload")
async def sticker_upload(
    _s: str = Depends(require_admin),
    file: UploadFile = File(...),
    name: str = Form(""),
    tags: str = Form(""),
    description: str = Form(""),
    group_id: str = Form(""),
    next: str = Form(""),
):
    """上传表情包：图片入库 + 自动描述/向量 + 失效检索缓存。"""
    from app.db.repositories.media_repo import MediaRepository
    from app.db.repositories.sticker_repo import StickerRepository
    from app.models.sticker import StickerDocument
    from app.services.sticker_service import invalidate_sticker_cache

    data = await file.read()
    content_type = (file.content_type or "").split(";")[0].strip().lower()

    if not data or content_type not in _ALLOWED_CONTENT_TYPES:
        return _redirect_with_error("仅支持 GIF / PNG / JPEG / WebP 图片", _safe_next(next))
    if len(data) > _MAX_UPLOAD_SIZE:
        return _redirect_with_error("图片超过 2MB 上限，请压缩后上传", _safe_next(next))

    # 描述留空时调 Vision 自动生成
    desc = description.strip() or await _auto_describe(data, content_type)

    media_id = await MediaRepository().create(
        data=data, content_type=content_type,
        filename=file.filename or "",
    )
    sticker = StickerDocument(
        name=name.strip() or (file.filename or "未命名"),
        tags=_parse_tags(tags),
        description=desc,
        media_url=f"/api/media/{media_id}",
        content_type=content_type,
        source="upload",
        group_id=group_id.strip(),
        embedding=await _gen_embedding(
            desc or "；".join(_parse_tags(tags)) or name
        ),
    )
    await StickerRepository().create(sticker)
    invalidate_sticker_cache()
    logger.info("表情包已上传: name=%s, tags=%s", sticker.name, sticker.tags)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@stickers_router.post("/stickers/{sticker_id}/toggle")
async def sticker_toggle(
    sticker_id: str,
    _s: str = Depends(require_admin),
    next: str = Form(""),
):
    """启用/停用单条表情包（停用后不参与检索与发送）。"""
    from app.db.repositories.sticker_repo import StickerRepository
    from app.services.sticker_service import invalidate_sticker_cache

    repo = StickerRepository()
    sticker = await repo.find_by_id(sticker_id)
    if sticker:
        await repo.update_fields(sticker_id, {"enabled": not sticker.enabled})
        invalidate_sticker_cache()
    return RedirectResponse(url=_safe_next(next), status_code=303)


@stickers_router.post("/stickers/{sticker_id}/delete")
async def sticker_delete(
    sticker_id: str,
    _s: str = Depends(require_admin),
    next: str = Form(""),
):
    """删除表情包（级联删除媒体文件）。"""
    from app.db.repositories.media_repo import MediaRepository
    from app.db.repositories.sticker_repo import StickerRepository
    from app.services.sticker_service import invalidate_sticker_cache

    repo = StickerRepository()
    sticker = await repo.find_by_id(sticker_id)
    if sticker:
        await repo.delete(sticker_id)
        # 级联删除专属媒体文件（上传/seed 均为一对一引用）
        media_id = sticker.media_url.rsplit("/", 1)[-1]
        if media_id:
            try:
                await MediaRepository().delete(media_id)
            except Exception as e:
                logger.warning("表情包媒体删除失败: %s", e)
        invalidate_sticker_cache()
    return RedirectResponse(url=_safe_next(next), status_code=303)


def _safe_next(raw: str) -> str:
    """操作后的回跳地址：只允许本站表情包页，避免开放重定向。"""
    target = (raw or "").strip()
    if (
        target.startswith("/admin/stickers")
        and not target.startswith("//")
        and "://" not in target
        and "\\" not in target
    ):
        return target
    return "/admin/stickers"


def _redirect_with_error(msg: str, next_url: str = "/admin/stickers") -> RedirectResponse:
    """操作失败：带错误参数重定向回列表页（保留筛选条件）。"""
    target = _safe_next(next_url)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        url=f"{target}{sep}error={quote(msg)}", status_code=303
    )
