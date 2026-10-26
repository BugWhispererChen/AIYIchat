"""C 端克隆回复反馈接口（像/不像），仅入库供离线评审，不直接在线改人格。"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth_api import (
    AuthError,
    require_account_service,
    require_current_identity,
)
from app.db.repositories.clone_repo import CloneFeedbackRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.models.clone import CloneFeedbackInDB

logger = logging.getLogger(__name__)
web_clone_router = APIRouter()

_ALLOWED_REASONS = frozenset({"tone", "length", "memory", "facts", "other"})


class CloneFeedbackPayload(BaseModel):
    service_id: str
    conversation_id: str = ""
    rating: Literal["like", "dislike"]
    reasons: list[str] = Field(default_factory=list, max_length=5)
    edited_reply: str = Field(default="", max_length=1000)


@web_clone_router.post("/feedback")
async def submit_clone_feedback(
    request: Request, payload: CloneFeedbackPayload
) -> JSONResponse:
    """记录用户对最近一条克隆回复的像/不像评价。

    服务归属按 account_id 校验；消息从服务最近会话反查，不接受客户端伪造
    message_id/profile_id。
    """
    try:
        principal = await require_current_identity(request)
        service = await require_account_service(principal, payload.service_id)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    profile_repo = ProfileRepository()
    profile = await profile_repo.find_by_id(service.profile_id)
    if profile is None:
        profile = await profile_repo.find_by_slug(service.profile_id)
    if profile is None or profile.clone_status != "active" or not profile.clone_enabled:
        raise HTTPException(status_code=400, detail="当前角色未启用克隆反馈")

    conv_repo = ConversationRepository()
    conv = (
        await conv_repo.find_by_id(payload.conversation_id)
        if payload.conversation_id else None
    )
    if conv is None and not payload.conversation_id:
        convs = await conv_repo.list_by_service(service.id)
        if service.share_group_id:
            convs += await conv_repo.list_by_service(service.share_group_id)
        convs.sort(key=lambda c: c.last_message_at, reverse=True)
        conv = convs[0] if convs else None
    if conv is None:
        raise HTTPException(status_code=404, detail="没有可反馈的对话")
    if conv.service_id not in {service.id, service.share_group_id}:
        raise HTTPException(status_code=403, detail="无权反馈该对话")

    recent = await MessageRepository().get_recent(conv.id, 10)
    assistant = next((m for m in reversed(recent) if m.role == "assistant"), None)
    if assistant is None:
        raise HTTPException(status_code=404, detail="没有可反馈的回复")

    reasons = [r for r in payload.reasons if r in _ALLOWED_REASONS]
    feedback = await CloneFeedbackRepository().create(CloneFeedbackInDB(
        account_id=principal.account_id,
        service_id=service.id,
        conversation_id=conv.id,
        message_id=assistant.id,
        profile_id=profile.id,
        clone_version=profile.clone_version,
        rating=payload.rating,
        reasons=reasons,
        edited_reply=payload.edited_reply.strip(),
    ))
    logger.info(
        "克隆反馈: account=%s profile=%s version=%d rating=%s reasons=%s",
        principal.account_id, profile.id, profile.clone_version,
        payload.rating, reasons,
    )
    return JSONResponse(status_code=201, content={
        "id": feedback.id, "status": feedback.status,
    })
