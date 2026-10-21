from typing import Optional

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.auth_api import AuthError, WebPrincipal, require_current_identity
from app.config import settings
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.web_credential import normalize_username

logger = logging.getLogger(__name__)

conversations_router = APIRouter()
_conv_repo = ConversationRepository()
_msg_repo = MessageRepository()
_service_repo = ServiceRepository()


class StreamChatRequest(BaseModel):
    """流式对话请求体。

    开启 `web_auth_enabled` 后 `platform`/`platform_id`/`bot_id` 全部由会话令牌反查，
    此处字段仅为开关关闭时的旧客户端保留；网页端传空串即可。
    `service_id` 用于在同一 Bot 的多个服务间指定当前角色（网页端角色切换）。
    """

    bot_id: str = ""
    platform: str = "web"
    platform_id: str = ""
    text: str
    nickname: str = ""
    service_id: str = ""


async def _require_current_identity_or_401(request: Request) -> WebPrincipal:
    """开启网页鉴权时解析会话身份，失败转 HTTPException。"""
    try:
        return await require_current_identity(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@conversations_router.get("")
async def list_conversations(
    request: Request,
    identity_id: Optional[str] = None,
    service_id: Optional[str] = None,
):
    """获取对话列表，可按身份ID或服务ID过滤。

    开启网页鉴权后只允许访问自己的数据：入参 identity_id 与令牌身份不符即 403，
    service_id 必须归属当前身份（历史越权读取面的封堵点）。
    """
    if settings.web_auth_enabled:
        principal = await _require_current_identity_or_401(request)
        if identity_id is not None and identity_id != principal.identity_id:
            raise HTTPException(status_code=403, detail="无权访问其他身份的数据")
        if service_id is not None:
            svc = await _service_repo.find_by_id(service_id)
            if svc is None or svc.identity_id != principal.identity_id:
                raise HTTPException(status_code=403, detail="无权访问该服务的数据")
        identity_id = principal.identity_id
    if service_id:
        # 服务设置共享组时，补充组 key 下的共享会话（含加入前的自身历史）
        convs = await _conv_repo.list_by_service(service_id)
        service = await _service_repo.find_by_id(service_id)
        if service and service.share_group_id:
            convs += await _conv_repo.list_by_service(service.share_group_id)
            convs.sort(key=lambda c: c.last_message_at, reverse=True)
    elif identity_id:
        convs = await _conv_repo.list_by_identity(identity_id)
    else:
        # 简单实现：不带过滤条件时返回空列表（生产环境应分页）
        return []
    return [
        {
            "id": c.id,
            "identity_id": c.identity_id,
            "profile_id": c.profile_id,
            "service_id": c.service_id,
            "message_count": c.message_count,
            "status": c.status,
            "last_message_at": c.last_message_at.isoformat(),
        }
        for c in convs
    ]


@conversations_router.get("/{conversation_id}/messages")
async def get_messages(request: Request, conversation_id: str, limit: int = 50):
    """获取对话的消息历史。

    开启网页鉴权后仅允许读取本人对话（消息历史泄露即本次要封堵的越权面之一）。
    """
    conv = await _conv_repo.find_by_id(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if settings.web_auth_enabled:
        principal = await _require_current_identity_or_401(request)
        if conv.identity_id != principal.identity_id:
            raise HTTPException(status_code=403, detail="无权读取该对话的消息")

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


def _sse_frame(payload: str) -> str:
    """SSE 数据帧：换行转义避免破坏协议，客户端 payloadOf 再反解回来。"""
    safe = payload.replace("\r", "").replace("\n", "\\n")
    return f"data: {safe}\n\n"


def stream_frames(item: object) -> list[str]:
    """把引擎的一个产出编码成若干 SSE 帧。

    两种产出都要能吃：未开放行闸门时引擎 yield 裸 str，输出与改动前**逐字节一致**；
    开闸门时 yield StreamEvent，气泡边界用 ``[MSG]`` 控制帧表达、正文另起一帧，
    复读重跑用 ``[RESET]`` 让前端收回本轮气泡。

    控制帧刻意保持纯文本而不是 JSON——payloadOf 会对每个 payload 无差别地把
    ``\\n`` 反解成真实换行，JSON 载荷会被这一步破坏。
    """
    if isinstance(item, str):
        return [_sse_frame(item)]
    kind = getattr(item, "kind", "msg")
    if kind == "reset":
        return [_sse_frame("[RESET]")]
    body = getattr(item, "text", "")
    if not body:
        return []
    return [_sse_frame("[MSG]"), _sse_frame(body)]


@conversations_router.post("/chat/stream")
async def chat_stream(req: StreamChatRequest, request: Request):
    """流式对话接口（SSE）。逐块返回 AI 回复文本，以 data: 帧传输，[DONE] 结束。

    开启 `web_auth_enabled` 后，`platform`/`platform_id`/`bot_id` 一律由 Bearer 会话
    令牌反查，客户端入参不再被信任——这是原"任意人可冒充他人 openid"越权面的封堵点。
    开关关闭时保持旧行为，确保既有部署升级无感知（QQ/微信 Bot 走引擎直连，不经本接口）。
    """
    from app.conversation.engine import ConversationEngine

    if settings.web_auth_enabled:
        principal = await _require_current_identity_or_401(request)
        # 显式传入却不匹配 → 视为冒充尝试：拒绝并留审计日志（比静默忽略更能暴露攻击）
        if req.platform_id and normalize_username(req.platform_id) != principal.username:
            logger.warning(
                "越权尝试：登录身份 %s 试图以 platform_id=%s 冒充他人会话",
                principal.username,
                req.platform_id,
            )
            raise HTTPException(status_code=403, detail="platform_id 与登录身份不匹配")
        if req.bot_id and req.bot_id != principal.bot_id:
            logger.warning(
                "越权尝试：登录身份 %s 试图指定未授权的 bot_id=%s", principal.username, req.bot_id
            )
            raise HTTPException(status_code=403, detail="无权使用该 Bot")
        bot_id = principal.bot_id
        platform, platform_id = "web", principal.username
        # 昵称一律取会话身份（账号中心可改），不接受客户端入参：
        # 否则任何人只要在请求体里换一个 nickname 就能冒充他人的称呼写进 prompt
        nickname = principal.nickname or principal.username
        # 指定服务时校验归属：只能对话自己名下的服务（多角色切换的越权面）
        service_id = req.service_id.strip()
        if service_id:
            svc = await _service_repo.find_by_id(service_id)
            if svc is None or svc.identity_id != principal.identity_id:
                logger.warning(
                    "越权尝试：登录身份 %s 试图使用他人服务 service_id=%s",
                    principal.username,
                    service_id,
                )
                raise HTTPException(status_code=403, detail="无权使用该服务")
            service_id = svc.id
        else:
            service_id = ""
    else:
        bot_id = req.bot_id
        platform, platform_id, nickname = req.platform, req.platform_id, req.nickname
        service_id = ""
    if not bot_id or not platform_id:
        raise HTTPException(status_code=400, detail="缺少 bot_id 或 platform_id")

    engine = ConversationEngine(bot_id=bot_id)

    async def event_gen():
        try:
            async for item in engine.handle_message_stream(
                platform=platform,
                platform_id=platform_id,
                text=req.text,
                nickname=nickname,
                service_id=service_id,
            ):
                for frame in stream_frames(item):
                    yield frame
        except Exception as e:
            yield _sse_frame(f"[ERROR] {e}")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
