"""Admin QQ Bot 扫码绑定 API。

QQ 开放平台"扫码接入"：管理员用手机 QQ 扫码选择机器人后，
平台下发 AppID/AppSecret，自动写入 Bot 配置；
BotManager 热加载检测到配置变更后自动拉起/重启 Bot。

- POST /qq-bots/{config_id}/qrcode        获取绑定二维码
- GET  /qq-bots/{config_id}/poll-qr       轮询绑定状态
"""

import logging
import time

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from app.adapters.qq_bind import (
    QQBindSession,
    create_bind_task,
    poll_bind_result,
)
from app.admin.auth import require_admin
from app.admin.routes.common import _bot_config_repo
from app.models.bot_config import BotConfigUpdate
from app.services.temp_service import verify_bot_binding

logger = logging.getLogger(__name__)
qq_bots_router = APIRouter(tags=["admin"])

# 进行中的绑定会话（一次性短生命周期操作，无需落库；容器重启即失效）
_pending_sessions: dict[str, QQBindSession] = {}
# 每个会话最近一次调用平台轮询接口的时间戳，用于服务端节流
_last_poll_at: dict[str, float] = {}
# 被平台限流的会话集合：限流后最小轮询间隔提升为 _RATE_LIMITED_INTERVAL
_rate_limited: set[str] = set()
# 被平台限流后的最小轮询间隔（秒）
_RATE_LIMITED_INTERVAL = 6.0
# 正常最小轮询间隔（秒），与前端轮询节奏匹配
_NORMAL_INTERVAL = 2.5


def _pop_session(config_id: str) -> None:
    """清理会话及其节流状态。"""
    _pending_sessions.pop(config_id, None)
    _last_poll_at.pop(config_id, None)
    _rate_limited.discard(config_id)


def _render_qr_image(qr_url: str) -> str:
    """将二维码内容渲染为 PNG data URI；qrcode 库缺失时返回空串。"""
    try:
        import base64
        import io

        import qrcode as qrcode_lib  # type: ignore[import-untyped]

        qr = qrcode_lib.QRCode(version=1, box_size=10, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        img_b64 = base64.b64encode(buffer.getvalue()).decode()
        return "data:image/png;base64,%s" % img_b64
    except ImportError:
        logger.warning("qrcode 库未安装，无法生成二维码图片")
        return ""


@qq_bots_router.post("/qq-bots/{config_id}/qrcode", response_class=HTMLResponse)
async def get_qq_bind_qrcode(
    config_id: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """获取扫码绑定二维码，返回 HTML 片段。"""
    cfg = await _bot_config_repo.find_by_id(config_id)
    if not cfg:
        return HTMLResponse(
            "<div class='alert alert-error'>Bot not found</div>",
            status_code=404,
        )
    if cfg.platform != "qq":
        return HTMLResponse(
            "<div class='alert alert-error'>仅 QQ Bot 支持扫码绑定</div>",
            status_code=400,
        )

    try:
        session = await create_bind_task()
    except Exception as e:
        logger.error("[QQBind] 创建绑定任务失败: %s", e)
        return HTMLResponse(
            f"<div class='alert alert-error'>获取二维码失败: {e}</div>",
            status_code=500,
        )

    _pending_sessions[config_id] = session
    qrcode_data_uri = _render_qr_image(session.qr_url)

    img_style = (
        "max-width: 200px; border: 1px solid #ddd; border-radius: 8px;"
    )
    p_style = (
        "font-size: 0.85rem; color: var(--pico-muted-color); margin-top: 0.5rem;"
    )
    html = f"""
    <div class="qr-container">
        <p style="margin-bottom: 0.5rem;">请使用手机QQ扫码，选择要绑定的机器人：</p>
        <img src="{qrcode_data_uri}" alt="绑定二维码" style="{img_style}"
             data-bind-task="{session.task_id}">
        <p style="{p_style}">扫码确认后自动填入凭据...</p>
    </div>
    """
    return HTMLResponse(html)


@qq_bots_router.get("/qq-bots/{config_id}/poll-qr")
async def poll_qq_bind_qr(
    config_id: str, _s: str = Depends(require_admin)
) -> JSONResponse:
    """轮询绑定状态 - 成功后写入凭据，由 BotManager 热加载自动拉起。"""
    session = _pending_sessions.get(config_id)
    if not session:
        return JSONResponse(
            {"status": "error", "message": "绑定会话不存在，请重新获取二维码"},
            status_code=404,
        )

    # 服务端节流：距上次调用平台接口不足最小间隔时直接返回 waiting
    now = time.monotonic()
    last_at = _last_poll_at.get(config_id, 0.0)
    min_interval = (
        _RATE_LIMITED_INTERVAL if config_id in _rate_limited else _NORMAL_INTERVAL
    )
    if now - last_at < min_interval:
        return JSONResponse({"status": "waiting", "message": "等待扫码..."})
    _last_poll_at[config_id] = now

    try:
        result = await poll_bind_result(session)
    except Exception as e:
        msg = str(e)
        logger.error("[QQBind] Poll error: %s", msg, exc_info=True)
        # 平台限流不中断流程：提升该会话轮询间隔，提示前端退避后继续等待
        if "频率" in msg or "频繁" in msg:
            _rate_limited.add(config_id)
            return JSONResponse({"status": "rate_limited", "message": msg})
        return JSONResponse({"status": "error", "message": msg})

    status = result.get("status")
    if status == "success":
        await _bot_config_repo.update(
            config_id,
            BotConfigUpdate(
                app_id=str(result.get("app_id", "")),
                app_secret=str(result.get("app_secret", "")),
            ),
        )
        _pop_session(config_id)
        logger.info("[QQBind] Bot %s 扫码绑定成功，凭据已保存", config_id)

        # 获取Bot所属账号ID用于创建临时服务
        cfg = await _bot_config_repo.find_by_id(config_id)
        if cfg:
            # 异步执行验证流程，不阻塞响应
            import asyncio
            asyncio.create_task(_verify_binding_async(config_id, cfg.account_id))

        return JSONResponse({
            "status": "verifying",
            "message": "绑定成功，正在验证Bot状态..."
        })
    if status == "expired":
        _pop_session(config_id)
        return JSONResponse(
            {"status": "expired", "message": "二维码已过期，请重新获取"}
        )
    return JSONResponse({"status": status, "message": result.get("message", "")})


async def _verify_binding_async(bot_id: str, account_id: str) -> None:
    """异步验证Bot绑定状态（后台任务）。"""
    try:
        result = await verify_bot_binding(bot_id, account_id)
        logger.info(
            "[QQBind] Bot %s 验证完成: %s - %s",
            bot_id, result["status"], result["message"]
        )
    except Exception as e:
        logger.error("[QQBind] Bot %s 验证异常: %s", bot_id, e, exc_info=True)
