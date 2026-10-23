"""Admin QQ 渠道扫码绑定 API。

QQ 开放平台"扫码接入"：管理员用手机 QQ 扫码选择机器人后，
平台下发 AppID/AppSecret，自动写入渠道配置；
BotManager 热加载检测到配置变更后自动拉起/重启渠道。

- POST /qq-bots/{config_id}/qrcode        获取绑定二维码
- GET  /qq-bots/{config_id}/poll-qr       轮询绑定状态

扫码会话与节流已收敛到 app/services/bot_qr_service.py，后台与 C 端共用。
"""

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _bot_config_repo, render_qr_fragment
from app.services.bot_qr_service import qq_sessions
from app.services.temp_service import verify_bot_binding

logger = logging.getLogger(__name__)
qq_bots_router = APIRouter(tags=["admin"])

# 兼容既有测试对会话字典的直接引用（与共享 store 指向同一对象）
_pending_sessions = qq_sessions.sessions
_last_poll_at = qq_sessions.last_poll_at
_rate_limited = qq_sessions.rate_limited


@qq_bots_router.post("/qq-bots/{config_id}/qrcode", response_class=HTMLResponse)
async def get_qq_bind_qrcode(
    config_id: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """获取扫码绑定二维码，返回 HTML 片段。"""
    cfg = await _bot_config_repo.find_by_id(config_id)
    if not cfg:
        return HTMLResponse(
            "<div class='alert alert-error'>渠道不存在</div>",
            status_code=404,
        )
    if cfg.platform != "qq":
        return HTMLResponse(
            "<div class='alert alert-error'>仅 QQ 渠道支持扫码绑定</div>",
            status_code=400,
        )

    try:
        session, qrcode_data_uri = await qq_sessions.create(config_id)
    except Exception as e:
        logger.error("[QQBind] 创建绑定任务失败: %s", e)
        return HTMLResponse(
            f"<div class='alert alert-error'>获取二维码失败: {e}</div>",
            status_code=500,
        )

    html = render_qr_fragment(
        lead="请使用手机QQ扫码，选择要绑定的机器人：",
        image_src=qrcode_data_uri,
        alt="绑定二维码",
        hint="扫码确认后自动填入凭据...",
        data_bind_task=session.task_id,
    )
    return HTMLResponse(html)


@qq_bots_router.get("/qq-bots/{config_id}/poll-qr")
async def poll_qq_bind_qr(
    config_id: str, _s: str = Depends(require_admin)
) -> JSONResponse:
    """轮询绑定状态 - 成功后写入凭据，由 BotManager 热加载自动拉起。"""
    result = await qq_sessions.poll(config_id)
    status = result.get("status")
    if status == "missing":
        return JSONResponse(
            {"status": "error", "message": result.get("message", "")},
            status_code=404,
        )
    if status == "success":
        cfg = await _bot_config_repo.find_by_id(config_id)
        if cfg:
            # 后台绑定后做一次连通性验证；C 端自助渠道已有正式服务，不需要此步
            asyncio.create_task(_verify_binding_async(config_id, cfg.account_id))
        return JSONResponse(
            {"status": "verifying", "message": "绑定成功，正在验证渠道状态..."}
        )
    return JSONResponse(result)


async def _verify_binding_async(bot_id: str, account_id: str) -> None:
    """异步验证渠道绑定状态（后台任务）。"""
    try:
        result = await verify_bot_binding(bot_id, account_id)
        logger.info(
            "[QQBind] Bot %s 验证完成: %s - %s",
            bot_id, result["status"], result["message"],
        )
    except Exception as e:
        logger.error("[QQBind] Bot %s 验证异常: %s", bot_id, e, exc_info=True)
