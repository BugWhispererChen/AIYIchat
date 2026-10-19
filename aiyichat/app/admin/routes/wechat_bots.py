"""Admin 微信 Bot 扫码登录 API。

注意：原有的微信 Bot 管理页面（/wechat-bots、create、delete）已整合到
账号管理（account_detail.html）中，这里仅保留账号详情页扫码登录所需的
3 个 API 端点：
- POST /wechat-bots/{config_id}/qrcode        获取登录二维码
- GET  /wechat-bots/{config_id}/poll-qr       轮询扫码状态
- POST /wechat-bots/{config_id}/complete-login 完成登录
"""

import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _bot_config_repo, render_qr_fragment

logger = logging.getLogger(__name__)
wechat_bots_router = APIRouter(tags=["admin"])


@wechat_bots_router.post("/wechat-bots/{config_id}/qrcode", response_class=HTMLResponse)
async def get_wechat_qrcode(
    config_id: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """获取登录二维码，返回 HTML 片段。"""
    from app.adapters.wechat_clawbot import WeChatClient, get_wechat_manager

    manager = get_wechat_manager()
    client = manager.get_client(config_id)

    if not client:
        # 尝试从数据库加载
        cfg = await _bot_config_repo.find_by_id(config_id)
        if not cfg:
            return HTMLResponse(
                "<div class='alert alert-error'>Bot not found</div>",
                status_code=404,
            )
        client = WeChatClient(
            bot_id=cfg.id,
            on_token_update=manager._on_token_update,
        )
        manager._pending_clients[cfg.id] = client

    try:
        qr_data = await client.get_login_qrcode()
        await _bot_config_repo.update_qrcode(config_id, qr_data.get("qrcode_url", ""))
        await _bot_config_repo.update_status(config_id, "pending_qr")

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
    except Exception as e:
        return HTMLResponse(
            f"<div class='alert alert-error'>获取二维码失败: {e}</div>",
            status_code=500,
        )


@wechat_bots_router.get("/wechat-bots/{config_id}/poll-qr")
async def poll_wechat_qr(
    config_id: str, qrcode: str, _s: str = Depends(require_admin)
) -> JSONResponse:
    """轮询扫码状态 - 扫码成功后直接启动 Bot。"""
    from app.adapters.wechat_clawbot import get_wechat_manager

    manager = get_wechat_manager()
    client = manager.get_client(config_id)

    if not client:
        return JSONResponse({"error": "Bot not found"}, status_code=404)

    try:
        bot_token = await client.poll_qrcode_status(qrcode)

        if bot_token:
            # 防止重复启动
            if manager.is_bot_running(config_id):
                logger.info("[WeChatBot] Bot %s 已有运行任务，跳过重复启动", config_id)
                return JSONResponse({"status": "success", "message": "登录成功"})

            logger.info("[WeChatBot] Bot %s 扫码成功，保存 bot_token 并启动", config_id)
            await _bot_config_repo.update_wechat_bot_token(config_id, bot_token)
            await _bot_config_repo.update_status(config_id, "online")

            manager.start_bot_task(config_id, client)
            logger.info("[WeChatBot] Bot %s 登录完成，已启动消息轮询", config_id)
            return JSONResponse({"status": "success", "message": "登录成功"})

        return JSONResponse({"status": "waiting", "message": "等待扫码..."})
    except Exception as e:
        logger.error("[WeChatBot] Poll QR error: %s", e, exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)})


@wechat_bots_router.post("/wechat-bots/{config_id}/complete-login")
async def complete_wechat_login(
    config_id: str, bot_token: str = Form(...), _s: str = Depends(require_admin)
) -> JSONResponse:
    """完成登录，使用 bot_token 获取 context_token 并启动 Bot。"""
    from app.adapters.wechat_clawbot import get_wechat_manager

    manager = get_wechat_manager()
    client = manager.get_client(config_id)

    if not client:
        return JSONResponse({"error": "Bot not found"}, status_code=404)

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
