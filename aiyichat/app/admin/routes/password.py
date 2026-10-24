"""Admin 修改密码路由。

密码以 PBKDF2-HMAC-SHA256 哈希形式持久化到 app_settings（见 app.admin.auth），
修改成功后作废全部后台会话并跳转登录页——旧令牌不该比新口令活得更久，
否则被窃取过一次会话的人在你改密后依然能进后台。
"""

import asyncio

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import (
    destroy_session,
    hash_password,
    require_admin,
    reset_all_sessions,
    verify_credentials,
)
from app.admin.routes.common import _render
from app.config import settings
from app.utils.password_hashing import is_insecure_admin_password

password_router = APIRouter(tags=["admin"])

# 新密码最小长度
_MIN_PASSWORD_LEN = 8


@password_router.get("/password", response_class=HTMLResponse)
async def password_page(
    request: Request, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """修改密码页面（GET）。"""
    return _render(request, "password.html", {
        "active_page": "password",
        "admin_username": settings.admin_username,
    })


@password_router.post("/password", response_class=HTMLResponse)
async def password_change(
    request: Request,
    session: str = Depends(require_admin),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
) -> Response:
    """处理修改密码表单提交。"""
    from app.db.repositories.app_settings_repo import AppSettingsRepository

    error: str | None = None
    if not await verify_credentials(settings.admin_username, current_password):
        error = "当前密码不正确"
    elif len(new_password) < _MIN_PASSWORD_LEN:
        error = f"新密码长度至少 {_MIN_PASSWORD_LEN} 位"
    elif is_insecure_admin_password(new_password):
        error = "新密码不能使用随源码公开的默认口令"
    elif new_password != confirm_password:
        error = "两次输入的新密码不一致"
    elif new_password == current_password:
        error = "新密码不能与当前密码相同"

    if error:
        return _render(request, "password.html", {
            "active_page": "password",
            "admin_username": settings.admin_username,
            "error": error,
        })

    # 哈希化持久化并就地更新运行时配置（无需全量 reload）
    # PBKDF2 60 万次迭代是纯 CPU 工作，放线程池执行，避免阻塞事件循环
    hashed = await asyncio.to_thread(hash_password, new_password)
    await AppSettingsRepository().upsert({"admin_password": hashed})
    settings.admin_password = hashed

    # 作废全部后台会话（含本会话），强制用新密码重新登录
    reset_all_sessions()
    response = RedirectResponse(
        url="/admin/login?msg=password_changed",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    destroy_session(response, session)
    return response
