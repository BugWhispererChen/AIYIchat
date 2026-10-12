"""Admin 认证路由：登录、登出。"""

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import (
    create_session,
    destroy_session,
    require_admin,
    verify_credentials,
)
from app.admin.routes.common import _render

auth_router = APIRouter(prefix="/admin", tags=["admin"])


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """渲染登录页面。"""
    return _render(request, "login.html")


@auth_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    """处理登录表单提交。"""
    if not await verify_credentials(username, password):
        return _render(
            request,
            "login.html",
            {"error": "用户名或密码错误"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    response = RedirectResponse(url="/admin/", status_code=status.HTTP_303_SEE_OTHER)
    create_session(response)
    return response


@auth_router.get("/logout")
async def logout(request: Request, session: str = Depends(require_admin)) -> RedirectResponse:
    """处理登出请求，清除会话。"""
    response = RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    destroy_session(response, session)
    return response
