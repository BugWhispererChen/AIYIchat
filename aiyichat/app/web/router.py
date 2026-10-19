"""C 端网页路由：注册 / 登录 / 极简聊天三张页面。

页面只是静态壳，本模块不接触凭据与数据库：所有业务一律走前端直连
`/api/auth/*` 与 `/api/conversations/chat/stream`，因此这里只做模板渲染与开关拦截。

`settings.web_auth_enabled` 是唯一硬开关，关闭时：
- `/web/*` 一律返回 403 提示页，且不渲染任何身份数据；
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
from app.utils.assets import asset

_template_dir = Path(__file__).parent / "templates"
# 设计令牌只在 admin 的 partials/_design_tokens.html 里定义一次；把 admin 模板目录
# 追加进搜索路径，让 C 端页面 include 同一份令牌，避免两套配色各自演化
_admin_template_dir = Path(__file__).parent.parent / "admin" / "templates"

templates = Jinja2Templates(directory=[str(_template_dir), str(_admin_template_dir)])
# 静态资源 URL 版本戳：C 端与后台共用同一套 /static 资源
templates.env.globals["asset"] = asset

web_router = APIRouter(tags=["web"])

# 未开放提示页：开关关闭时也要能渲染，故不依赖设计令牌与 JS，只用系统配色偏好
_DISABLED_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>未开放 - 爱易聊</title>
<style>
    :root {
        color-scheme: light dark;
        --paper: #f7f5f1;
        --ink: #2a2622;
        --ink-soft: #6b6258;
        --hairline: #e6e1d6;
        --amber: #e08a2b;
        --font-display: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia,
                        "Songti SC", STSong, serif;
        --font-body: system-ui, -apple-system, "Segoe UI", "PingFang SC",
                     "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
        :root {
            --paper: #1a1714;
            --ink: #f0e9df;
            --ink-soft: #b9ada0;
            --hairline: #3a342c;
        }
    }
    body {
        margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
        background: var(--paper); color: var(--ink);
        font-family: var(--font-body); line-height: 1.8;
    }
    .notice-card { max-width: 34rem; padding: 2.5rem 1.5rem; text-align: center; }
    .wordmark {
        font-family: var(--font-display);
        font-size: 1.9rem; letter-spacing: .04em; margin: 0 0 .3rem;
    }
    .wordmark::before {
        content: ""; display: inline-block; width: 7px; height: 7px; margin-right: .5rem;
        border-radius: 50%; background: var(--amber); vertical-align: middle;
    }
    h2 { font-family: var(--font-display); font-weight: 600; font-size: 1.15rem; margin: 1.4rem 0 .4rem; }
    p { color: var(--ink-soft); font-size: .9rem; margin: 0; }
    hr { border: 0; border-top: 1px solid var(--hairline); margin: 1.6rem auto; width: 3rem; }
</style>
</head>
<body>
    <div class="notice-card">
        <p class="wordmark">爱易聊</p>
        <hr>
        <h2>网页身份功能未开放</h2>
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
    """开关关闭时的统一响应：403 + 纯静态提示页（不含任何身份数据）。"""
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


@web_router.get("/web/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request) -> Response:
    """角色自助管理页：列表与编辑表单都在前端直连 `/api/web/profiles` 完成。"""
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(request, "profiles.html")


@web_router.get("/web/services", response_class=HTMLResponse)
async def services_page(request: Request) -> Response:
    """伙伴（对话服务）自助管理页：把角色装配成可选的对话对象。"""
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(request, "services.html")


@web_router.get("/", include_in_schema=False)
async def index() -> Response:
    """根路径入口：开启网页功能时引导到登录页，关闭时维持原有 404。"""
    if not settings.web_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return RedirectResponse(url="/web/login", status_code=status.HTTP_303_SEE_OTHER)
