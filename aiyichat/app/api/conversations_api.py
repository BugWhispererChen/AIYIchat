from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.service_repo import ServiceRepository

conversations_router = APIRouter()
_conv_repo = ConversationRepository()
_msg_repo = MessageRepository()
_service_repo = ServiceRepository()


class StreamChatRequest(BaseModel):
    """流式对话请求体。"""

    bot_id: str
    platform: str = "web"
    platform_id: str
    text: str
    nickname: str = ""


@conversations_router.get("")
async def list_conversations(
    user_id: Optional[str] = None,
    service_id: Optional[str] = None,
):
    """获取对话列表，可按用户ID或服务ID过滤。"""
    if service_id:
        # 服务设置共享组时，补充组 key 下的共享会话（含加入前的自身历史）
        convs = await _conv_repo.list_by_service(service_id)
        service = await _service_repo.find_by_id(service_id)
        if service and service.share_group_id:
            convs += await _conv_repo.list_by_service(service.share_group_id)
            convs.sort(key=lambda c: c.last_message_at, reverse=True)
    elif user_id:
        convs = await _conv_repo.list_by_user(user_id)
    else:
        # 简单实现：不带过滤条件时返回空列表（生产环境应分页）
        return []
    return [
        {
            "id": c.id,
            "user_id": c.user_id,
            "profile_id": c.profile_id,
            "service_id": c.service_id,
            "message_count": c.message_count,
            "status": c.status,
            "last_message_at": c.last_message_at.isoformat(),
        }
        for c in convs
    ]


@conversations_router.get("/{conversation_id}/messages")
async def get_messages(conversation_id: str, limit: int = 50):
    """获取对话的消息历史。"""
    conv = await _conv_repo.find_by_id(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = await _msg_repo.get_recent(conversation_id, limit)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "emotion_tag": m.emotion_tag,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]


@conversations_router.post("/chat/stream")
async def chat_stream(req: StreamChatRequest):
    """流式对话接口（SSE）。逐块返回 AI 回复文本，以 data: 帧传输，[DONE] 结束。"""
    from app.conversation.engine import ConversationEngine

    engine = ConversationEngine(bot_id=req.bot_id)

    async def event_gen():
        try:
            async for chunk in engine.handle_message_stream(
                platform=req.platform,
                platform_id=req.platform_id,
                text=req.text,
                nickname=req.nickname,
            ):
                # SSE 数据帧：对换行做转义避免破坏协议，客户端反转义
                safe = chunk.replace("\r", "").replace("\n", "\\n")
                yield f"data: {safe}\n\n"
        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
