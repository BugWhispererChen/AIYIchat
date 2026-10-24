"""Admin 对话管理路由。"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import (
    _account_repo,
    _conv_repo,
    _msg_repo,
    _profile_repo,
    _render,
    _identity_repo,
)

conversations_router = APIRouter(tags=["admin"])


@conversations_router.get("/conversations", response_class=HTMLResponse)
async def conversations_list(
    request: Request,
    q: str = "",
    page: int = 1,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """分页展示对话列表，支持搜索。"""
    per_page = 20
    if q.strip():
        convs = await _conv_repo.search(q, limit=per_page)
        total = len(convs)
        total_pages = 1
        page = 1
    else:
        skip = (page - 1) * per_page
        convs = await _conv_repo.list_recent(skip=skip, limit=per_page)
        total = await _conv_repo.count()
        total_pages = max(1, (total + per_page - 1) // per_page)

    # 为每个对话查询关联的身份、账号、角色信息
    conv_infos: list[dict] = []
    for conv in convs:
        identity = await _identity_repo.find_by_id(conv.identity_id)
        account = None
        if identity and identity.account_id:
            account = await _account_repo.find_by_id(identity.account_id)
        # profile_id 可能是 slug 或 ObjectId
        profile = await _profile_repo.find_by_slug(conv.profile_id)
        if profile is None:
            try:
                profile = await _profile_repo.find_by_id(conv.profile_id)
            except Exception:
                profile = None
        conv_infos.append({
            "conv": conv, "identity": identity, "account": account, "profile": profile,
        })

    return _render(request, "conversations.html", {
        "conv_infos": conv_infos,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "q": q,
    })


# 对话详情每页消息数
_DETAIL_PAGE_SIZE = 50


@conversations_router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(
    request: Request,
    conv_id: str,
    page: int = 1,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """展示单个对话详情及消息记录（分页）。"""
    conv = await _conv_repo.find_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")

    # 分页计算
    total_messages = await _msg_repo.count_by_conversation(conv_id)
    total_pages = max(1, (total_messages + _DETAIL_PAGE_SIZE - 1) // _DETAIL_PAGE_SIZE)
    page = max(1, min(page, total_pages))  # 限制页码范围
    skip = (page - 1) * _DETAIL_PAGE_SIZE

    messages = await _msg_repo.list_paginated(
        conv_id, skip=skip, limit=_DETAIL_PAGE_SIZE
    )

    # HTMX 分页请求只返回消息片段
    if request.headers.get("HX-Request"):
        return _render(request, "partials/message_list.html", {"messages": messages})

    return _render(
        request,
        "conversation_detail.html",
        {
            "conversation": conv,
            "messages": messages,
            "total_messages": total_messages,
            "current_page": page,
            "total_pages": total_pages,
            "page_size": _DETAIL_PAGE_SIZE,
        },
    )


@conversations_router.post(
    "/conversations/{conv_id}/delete", response_class=HTMLResponse
)
async def conversation_delete(
    request: Request,
    conv_id: str,
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """删除整个对话及其所有消息。"""
    conv = await _conv_repo.find_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    # 消息、注入快照、消息引用的媒体一起删，最后删对话
    await _conv_repo.delete_cascade_by_ids([conv_id])
    return RedirectResponse(
        url="/admin/conversations", status_code=status.HTTP_303_SEE_OTHER
    )


@conversations_router.post(
    "/conversations/{conv_id}/messages/{msg_id}/delete",
    response_class=HTMLResponse,
)
async def conversation_message_delete(
    request: Request,
    conv_id: str,
    msg_id: str,
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """删除单条消息。"""
    conv = await _conv_repo.find_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    # 归属校验：msg_id 来自表单，不校验就能通过"任意会话 + 任意消息 ID"删到别处的消息
    message = await _msg_repo.find_by_id(msg_id)
    if message is None or str(message.conversation_id) != str(conv.id):
        raise HTTPException(status_code=404, detail="消息不存在")
    await _msg_repo.delete_with_media(msg_id)
    return RedirectResponse(
        url=f"/admin/conversations/{conv_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
