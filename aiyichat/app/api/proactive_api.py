"""主动消息 API 端点。"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.admin.auth import require_admin

logger = logging.getLogger(__name__)

# 主动消息的触发与记录查询都是内部运维能力：挂在公网 API 前缀下，
# 必须显式要求后台管理员会话，不能依赖"ID 不可猜"来当鉴权。
proactive_router = APIRouter(dependencies=[Depends(require_admin)])


class ProactiveTriggerRequest(BaseModel):
    """手动触发主动消息的请求体。"""

    identity_id: str  # 系统内部身份ID
    bot_id: str  # Bot ID
    content: Optional[str] = None  # 自定义内容（为空则AI生成）
    stage: int = 1  # 发送阶段（1-4）
    content_type: Literal["care", "news", "trivia"] = "care"  # 内容类型：care（关怀）/ news（时事分享）/ trivia（趣事分享）


class ProactiveTriggerResponse(BaseModel):
    """触发响应。"""

    success: bool
    message: str
    record_id: str = ""


@proactive_router.post("/trigger", response_model=ProactiveTriggerResponse)
async def trigger_proactive_message(
    req: ProactiveTriggerRequest,
) -> ProactiveTriggerResponse:
    """
    手动触发主动消息发送（供管理后台使用）。

    可指定自定义内容，或留空由 AI 根据策略生成。
    """
    from app.db.repositories.proactive_repo import ProactiveRecordRepository
    from app.db.repositories.identity_repo import IdentityRepository
    from app.models.proactive_record import ProactiveRecord
    from app.proactive.scheduler import get_proactive_scheduler
    from app.proactive.strategy import QQ_STAGES

    identity_repo = IdentityRepository()
    identity = await identity_repo.find_by_id(req.identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="身份不存在")

    scheduler = get_proactive_scheduler()

    # 确定内容
    content = req.content
    if not content:
        # 从策略中获取对应阶段的提示
        stage_def = next(
            (s for s in QQ_STAGES if s.stage == req.stage), None
        )
        if stage_def is None:
            raise HTTPException(
                status_code=400, detail=f"无效阶段: {req.stage}"
            )
        # 使用简单的默认内容（实际应通过 LLM 生成）
        from app.db.repositories.service_repo import ServiceRepository

        svc_repo = ServiceRepository()
        service = await svc_repo.find_by_identity_and_bot(req.identity_id, req.bot_id)
        if service is None:
            raise HTTPException(status_code=404, detail="身份无对应服务")

        if req.content_type in ("news", "trivia"):
            # 分享型：news 联网时事 / trivia 创作趣事（失败返回空串）
            content = await scheduler._generate_share_content(
                identity=identity, service=service, share_kind=req.content_type
            )
        else:
            content, _referenced = await scheduler._generate_content(
                identity=identity, service=service, stage=stage_def
            )
        if not content:
            return ProactiveTriggerResponse(
                success=False, message="AI内容生成失败"
            )

    # 创建记录
    if identity.platform not in ("qq", "wechat"):
        return ProactiveTriggerResponse(
            success=False, message=f"暂不支持向 {identity.platform} 平台主动推送"
        )
    # 发送目标按 Bot 路由：QQ openid 按 appid 生成，同一身份在不同 Bot 下不同
    target_id = scheduler._resolve_platform_id(identity, req.bot_id)

    repo = ProactiveRecordRepository()
    record = ProactiveRecord(
        identity_id=identity.id,
        platform_openid=target_id,
        platform=identity.platform,
        bot_id=req.bot_id,
        stage=req.stage,
        content=content,
        content_type=req.content_type,
    )
    await repo.create(record)

    # 发送
    try:
        success = await scheduler._send_message(
            platform=identity.platform,
            bot_id=req.bot_id,
            platform_id=target_id,
            content=content,
        )
    except Exception as exc:  # noqa: BLE001 - 手动触发也要落 failed 并返回可读错误
        logger.exception("手动触发主动消息失败: identity=%s", identity.id)
        await repo.mark_failed(record.id, f"发送异常: {exc}")
        return ProactiveTriggerResponse(
            success=False,
            message=f"发送失败：{exc}",
            record_id=record.id,
        )

    if success:
        await repo.mark_sent(record.id, content)
        return ProactiveTriggerResponse(
            success=True,
            message="主动消息发送成功",
            record_id=record.id,
        )
    else:
        detail = scheduler._last_send_detail
        await repo.mark_failed(record.id, detail or "发送接口返回失败")
        return ProactiveTriggerResponse(
            success=False,
            message=f"发送失败：{detail}" if detail else "发送失败，请检查Bot状态",
            record_id=record.id,
        )


@proactive_router.get("/records")
async def list_proactive_records(
    identity_id: str,
    bot_id: str,
    limit: int = 20,
) -> list[dict]:
    """查询身份的主动消息发送记录。"""
    from app.db.repositories.proactive_repo import ProactiveRecordRepository

    repo = ProactiveRecordRepository()
    records = await repo.find_latest_by_identity(identity_id, bot_id, limit=limit)
    return [r.model_dump() for r in records]
