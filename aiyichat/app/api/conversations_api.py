import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth_api import (
    AuthError,
    WebAccountPrincipal,
    require_account_service,
    require_web_account,
)
from app.config import settings
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.conversation import ConversationInDB
from app.models.service import ServiceInDB
from app.services.voice_input import (
    ALLOWED_AUDIO_TYPES,
    MAX_TRANSCRIPT_CHARS,
    MAX_VOICE_BYTES,
    VOICE_RATE_LIMIT,
    VoiceInputError,
    normalize_audio_type,
    prepare_audio,
    transcribe_wav,
)
from app.utils.rate_limit import RateLimited, check_rate_limit

logger = logging.getLogger(__name__)

conversations_router = APIRouter()
_conv_repo = ConversationRepository()
_msg_repo = MessageRepository()
_service_repo = ServiceRepository()


_MAX_CHAT_CHARS = 4000


class StreamChatRequest(BaseModel):
    """流式对话请求体：显式选择服务，身份与渠道均由服务端解析。"""

    # 上限保护：一条消息就能塞进整本小说的话，提示词与计费都会被拖爆
    text: str = Field(max_length=_MAX_CHAT_CHARS)
    service_id: str = ""  # 鉴权后强制校验，避免未登录请求先返回 422


async def _require_web_account_or_401(request: Request) -> WebAccountPrincipal:
    """解析网页登录账号，失败转 HTTPException。"""
    try:
        return await require_web_account(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


async def _require_account_service_http(
    principal: WebAccountPrincipal, service_id: str
) -> ServiceInDB:
    """按账号归属校验服务，失败转 HTTPException。"""
    try:
        return await require_account_service(principal, service_id)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


async def _resolve_stream_target(
    principal: WebAccountPrincipal,
    *,
    service_id: str,
) -> tuple[str, str, str, str, str]:
    """返回所选服务的 Bot、身份及网页渠道调用方信息。"""
    if not service_id.strip():
        raise HTTPException(status_code=400, detail="请选择对话服务")
    svc = await _require_account_service_http(principal, service_id.strip())
    if not svc.is_active:
        raise HTTPException(status_code=409, detail="该服务已停用")
    identity = await IdentityRepository().find_by_id(svc.identity_id)
    if identity is None or identity.account_id != principal.account_id:
        raise HTTPException(status_code=404, detail="服务身份不存在")
    if identity.is_blocked:
        raise HTTPException(status_code=409, detail="服务身份已停用")
    return (
        svc.bot_id,
        svc.identity_id,
        svc.id,
        principal.credential.id,
        identity.nickname or identity.name or principal.nickname,
    )


async def _conversation_readable(
    conv: ConversationInDB, principal: WebAccountPrincipal
) -> bool:
    """会话必须挂在当前账号的服务或共享组上。"""
    if not conv.service_id:
        return False
    svc = await _service_repo.find_by_id(conv.service_id)
    if svc is not None:
        return svc.account_id == principal.account_id
    peers = await _service_repo.list_by_share_group(conv.service_id)
    return any(peer.account_id == principal.account_id for peer in peers)


@conversations_router.get("")
async def list_conversations(request: Request, service_id: str = ""):
    """列出所选服务的会话；共享组会话按同一账号归属补充。"""
    principal = await _require_web_account_or_401(request)
    if not service_id.strip():
        raise HTTPException(status_code=400, detail="请选择对话服务")
    service = await _require_account_service_http(principal, service_id)
    convs = await _conv_repo.list_by_service(service.id)
    if service.share_group_id:
        convs += await _conv_repo.list_by_service(service.share_group_id)
        convs.sort(key=lambda c: c.last_message_at, reverse=True)
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

    始终要求 Bearer 会话，并且只允许读取本人或同账号服务名下的对话。
    """
    principal = await _require_web_account_or_401(request)
    conv = await _conv_repo.find_by_id(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await _conversation_readable(conv, principal):
        raise HTTPException(status_code=403, detail="无权读取该对话的消息")

    # limit 来自调用方：不设上限就能一次拖走整段历史（内存与响应体都跟着爆）
    bounded_limit = max(1, min(int(limit or 50), 200))
    messages = await _msg_repo.get_recent(conversation_id, bounded_limit)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "content_type": m.content_type,
            "media_url": m.media_url,
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
    复读重跑用 ``[RESET]`` 让前端收回本轮气泡；流式收尾的图片/表情包/语音用
    ``[MEDIA]<kind>|<url>`` 单帧下发。

    控制帧刻意保持纯文本而不是 JSON——payloadOf 会对每个 payload 无差别地把
    ``\\n`` 反解成真实换行，JSON 载荷会被这一步破坏。
    """
    if isinstance(item, str):
        return [_sse_frame(item)]
    kind = getattr(item, "kind", "msg")
    if kind == "reset":
        return [_sse_frame("[RESET]")]
    if kind == "media":
        media_kind = getattr(item, "media_kind", "")
        media_url = getattr(item, "media_url", "")
        if media_kind not in ("image", "sticker", "audio") or not media_url:
            return []
        # 纯文本控制帧：payloadOf 会把字面 \n 反转义，JSON 载荷不可靠；
        # URL 是服务端生成的能力地址，不含换行，用 | 分隔足够。
        return [_sse_frame(f"[MEDIA]{media_kind}|{media_url}")]
    body = getattr(item, "text", "")
    if not body:
        return []
    return [_sse_frame("[MSG]"), _sse_frame(body)]


@conversations_router.post("/chat/stream")
async def chat_stream(req: StreamChatRequest, request: Request):
    """流式对话接口（SSE）。逐块返回 AI 回复文本，以 data: 帧传输，[DONE] 结束。

    始终要求 Bearer 会话：`platform`/`platform_id`/`bot_id` 一律由令牌反查，
    客户端传入的同名字段一律忽略——这是原"任意人可冒充他人 openid"越权面的封堵点。
    QQ/微信 Bot 走引擎直连，不经本接口。
    """
    from app.conversation.engine import ConversationEngine

    principal = await _require_web_account_or_401(request)
    (
        bot_id,
        engine_identity_id,
        service_id,
        platform_id,
        nickname,
    ) = await _resolve_stream_target(principal, service_id=req.service_id)
    platform = "web"

    engine = ConversationEngine(bot_id=bot_id)

    async def event_gen():
        try:
            async for item in engine.handle_message_stream(
                platform=platform,
                platform_id=platform_id,
                text=req.text,
                nickname=nickname,
                service_id=service_id,
                identity_id=engine_identity_id,
            ):
                for frame in stream_frames(item):
                    yield frame
        except Exception:
            # 原文回显给客户端等于把内部异常（可能含连接串、集合名、路径）交给匿名调用方；
            # 客户端只需要知道"这轮断了"，细节进日志（logger.exception 带完整栈）
            logger.exception("流式回复中断: conversation stream failed")
            yield _sse_frame("[ERROR] 回复中断，请重新发送试试")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@conversations_router.post("/chat/voice/stream")
async def chat_voice_stream(
    request: Request,
    audio: UploadFile = File(...),
    service_id: str = Form(""),
):
    """网页语音输入：上传音频 → ASR → 复用流式对话链路。

    语音额度只按 AI 语音回复扣，这里的 ASR 不额外扣语音额度；但 ASR 是外部
    计费调用，所以在转写之前先做账号级限流与服务归属校验。
    """
    principal = await _require_web_account_or_401(request)
    if not settings.asr_enabled:
        raise HTTPException(status_code=503, detail="语音输入未启用")
    try:
        check_rate_limit("web_voice", principal.account_id, VOICE_RATE_LIMIT)
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    content_type = normalize_audio_type(audio.content_type or "")
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="不支持的音频格式")

    # 归属校验放在 ASR 之前：未授权请求不该消耗外部识别额度
    (
        bot_id,
        engine_identity_id,
        resolved_service_id,
        platform_id,
        nickname,
    ) = await _resolve_stream_target(principal, service_id=service_id)

    try:
        declared_length = int(request.headers.get("content-length", "0") or "0")
    except ValueError:
        declared_length = 0
    if declared_length > MAX_VOICE_BYTES + 64 * 1024:
        raise HTTPException(status_code=413, detail="语音文件过大")

    try:
        raw_audio = await audio.read(MAX_VOICE_BYTES + 1)
    finally:
        await audio.close()
    if not raw_audio:
        raise HTTPException(status_code=400, detail="语音文件为空")
    if len(raw_audio) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="语音文件过大")

    try:
        wav_data, mp3_data, _duration = await prepare_audio(raw_audio)
    except VoiceInputError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.message
        ) from exc

    transcript = await transcribe_wav(wav_data)
    if not transcript:
        raise HTTPException(status_code=422, detail="没听清，请再说一次")
    transcript = transcript[:MAX_TRANSCRIPT_CHARS]

    from app.services.media_storage import save_audio_from_bytes

    try:
        user_media_url = await save_audio_from_bytes(mp3_data, "audio/mpeg")
    except Exception:
        logger.exception("网页语音消息保存失败")
        raise HTTPException(
            status_code=500, detail="语音保存失败，请重试"
        ) from None

    from app.conversation.engine import ConversationEngine

    engine = ConversationEngine(bot_id=bot_id)

    async def event_gen():
        try:
            # 先告诉前端识别结果：本地语音气泡的"识别中…"可立即替换成文字
            yield _sse_frame("[USER_TEXT]" + transcript)
            async for item in engine.handle_message_stream(
                platform="web",
                platform_id=platform_id,
                text=transcript,
                nickname=nickname,
                content_type="voice",
                service_id=resolved_service_id,
                identity_id=engine_identity_id,
                media_url=user_media_url,
                voice_reply=True,
            ):
                for frame in stream_frames(item):
                    yield frame
        except Exception:
            logger.exception("语音流式回复中断: conversation voice stream failed")
            yield _sse_frame("[ERROR] 回复中断，请重新发送试试")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
