"""C 端网页路由：注册 / 登录 / 极简聊天三张页面。

页面只是静态壳，本模块不接触凭据与数据库：所有业务一律走前端直连
`/api/auth/*` 与 `/api/conversations/chat/stream`，因此这里只做模板渲染与开关拦截。

`settings.web_auth_enabled` 是唯一硬开关，关闭时：
- `/web/*` 一律返回 403 提示页，且不渲染任何用户数据；
- 根路径保持 FastAPI 原有的 404 行为，既不重定向也不新增可见页面。
后者是"存量部署升级无感知"的产品硬约束：老实例的访问者看不到任何变化。
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from app.config import settings

_template_dir = Path(__file__).parent / "templates"
# 设计令牌只在 admin 的 partials/_design_tokens.html 里定义一次；把 admin 模板目录
# 追加进搜索路径，让 C 端页面 include 同一份令牌，避免两套配色各自演化
_admin_template_dir = Path(__file__).parent.parent / "admin" / "templates"

templates = Jinja2Templates(directory=[str(_template_dir), str(_admin_template_dir)])

web_router = APIRouter(tags=["web"])

# 未开放提示页：开关关闭时也要能渲染，故不依赖设计令牌与 JS，只用系统配色偏好
_DISABLED_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>未开放 - 爱易聊</title>
<style>
    :root { color-scheme: light dark; }
    body {
        margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
        font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .notice-card { max-width: 90vw; padding: 2rem 1rem; text-align: center; }
    p { color: gray; font-size: 0.92rem; }
</style>
</head>
<body>
    <div class="notice-card">
        <h2>网页用户功能未开放</h2>
        <p>本实例未开启网页注册与登录，请通过 QQ / 微信机器人与 AI 角色对话。</p>
    </div>
</body>
</html>"""


def _render(request: Request, name: str, ctx: dict[str, Any] | None = None) -> Response:
    """按 admin 路由的写法渲染模板：request 必须进上下文，供模板拼相对路径与主题。"""
    context: dict[str, Any] = {"request": request}
    context.update(ctx or {})
    return templates.TemplateResponse(request=request, name=name, context=context)


def _disabled_page() -> Response:
    """开关关闭时的统一响应：403 + 纯静态提示页（不含任何用户数据）。"""
    return HTMLResponse(content=_DISABLED_HTML, status_code=status.HTTP_403_FORBIDDEN)


@web_router.get("/web/register", response_class=HTMLResponse)
async def register_page(request: Request) -> Response:
    """自助注册页。

    注册/验证码子开关只影响页面提示与前端拦截，表单结构始终完整渲染：
    服务端仍是唯一裁判，页面提前给出可解释的说明比藏起表单更少让人困惑。
    """
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(
        request,
        "register.html",
        {
            "register_enabled": settings.web_register_enabled,
            "captcha_enabled": settings.web_captcha_enabled,
        },
    )


@web_router.get("/web/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """网页登录页：登录成功后由前端把令牌写入 localStorage 并跳转聊天页。"""
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(request, "login.html", {"captcha_enabled": settings.web_captcha_enabled})


@web_router.get("/web/chat", response_class=HTMLResponse)
async def chat_page(request: Request) -> Response:
    """极简聊天页：静态壳，身份与额度由前端带 Bearer 令牌调 `/api/auth/me` 获取。"""
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(request, "chat.html")


@web_router.get("/", include_in_schema=False)
async def index() -> Response:
    """根路径入口：开启网页功能时引导到登录页，关闭时维持原有 404。"""
    if not settings.web_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return RedirectResponse(url="/web/login", status_code=status.HTTP_303_SEE_OTHER)
