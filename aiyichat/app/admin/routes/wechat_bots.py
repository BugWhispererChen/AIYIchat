"""Admin 微信渠道扫码登录 API。

账号详情页扫码登录所需的 3 个端点：
- POST /wechat-bots/{config_id}/qrcode        获取登录二维码
- GET  /wechat-bots/{config_id}/poll-qr       轮询扫码状态
- POST /wechat-bots/{config_id}/complete-login 完成登录

扫码、轮询与 client 重建已收敛到 app/services/bot_qr_service.py，
后台与 C 端共用；本模块只保留 HTML / JSON 响应与后台兼容逻辑。
"""

import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _bot_config_repo, render_qr_fragment
from app.services.bot_qr_service import (
    create_wechat_qr,
    ensure_wechat_client,
    poll_wechat_login,
)

logger = logging.getLogger(__name__)
wechat_bots_router = APIRouter(tags=["admin"])


@wechat_bots_router.post("/wechat-bots/{config_id}/qrcode", response_class=HTMLResponse)
async def get_wechat_qrcode(
    config_id: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """获取登录二维码，返回 HTML 片段。"""
    cfg = await _bot_config_repo.find_by_id(config_id)
    if not cfg:
        return HTMLResponse(
            "<div class='alert alert-error'>渠道不存在</div>",
            status_code=404,
        )

    try:
        qr_data = await create_wechat_qr(config_id)
    except Exception as e:
        return HTMLResponse(
            f"<div class='alert alert-error'>获取二维码失败: {e}</div>",
            status_code=500,
        )
    if qr_data is None:
        return HTMLResponse(
            "<div class='alert alert-error'>渠道不存在</div>",
            status_code=404,
        )

    qrcode_url = qr_data.get("qrcode_url", "")
    qrcode = qr_data.get("qrcode", "")
    html = render_qr_fragment(
        lead="请使用微信扫码登录：",
        image_src=qrcode_url,
        alt="登录二维码",
        hint="扫码后自动刷新状态...",
        data_qrcode=qrcode,
    ) + f"""
    <script>
        setTimeout(() => pollQRStatus('{config_id}', '{qrcode}'), 2000);
    </script>
    """
    return HTMLResponse(html)


@wechat_bots_router.get("/wechat-bots/{config_id}/poll-qr")
async def poll_wechat_qr(
    config_id: str,
    qrcode: str,
    verify_code: str = "",
    _s: str = Depends(require_admin),
) -> JSONResponse:
    """轮询扫码状态，可携带手机验证码并直接启动已确认渠道。"""
    if await _bot_config_repo.find_by_id(config_id) is None:
        return JSONResponse({"error": "渠道不存在"}, status_code=404)
    return JSONResponse(await poll_wechat_login(config_id, qrcode, verify_code))


@wechat_bots_router.post("/wechat-bots/{config_id}/complete-login")
async def complete_wechat_login(
    config_id: str, bot_token: str = Form(...), _s: str = Depends(require_admin)
) -> JSONResponse:
    """完成登录，使用 bot_token 获取 context_token 并启动渠道。"""
    from app.adapters.wechat_clawbot import get_wechat_manager

    client = await ensure_wechat_client(config_id)
    if client is None:
        return JSONResponse({"error": "渠道不存在"}, status_code=404)

    manager = get_wechat_manager()
    try:
        client.set_bot_token(bot_token)
        await client.complete_login(bot_token)
        await _bot_config_repo.update_status(config_id, "online")

        # 如果已有轮询任务，先取消避免重复
        await manager.cancel_bot_task(config_id)

        manager.start_bot_task(config_id, client)
        logger.info("[WeChatBot] Bot %s 登录成功，已启动消息轮询", config_id)
        return JSONResponse({"status": "success", "message": "登录成功"})
    except Exception as e:
        logger.error("[WeChatBot] Complete login error: %s", e)
        return JSONResponse({"status": "error", "message": str(e)})
