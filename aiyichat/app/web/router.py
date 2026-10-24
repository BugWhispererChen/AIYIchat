"""C 端网页路由：注册 / 登录 / 极简聊天 / 聊天服务 / 账号中心。

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

from app.models.web_credential import TERMS_VERSION, PRIVACY_VERSION
from app.config import settings
from app.template_filters import configure_templates

_template_dir = Path(__file__).parent / "templates"
# 设计令牌只在 admin 的 partials/_design_tokens.html 里定义一次；把 admin 模板目录
# 追加进搜索路径，让 C 端页面 include 同一份令牌，避免两套配色各自演化
_admin_template_dir = Path(__file__).parent.parent / "admin" / "templates"

templates = Jinja2Templates(directory=[str(_template_dir), str(_admin_template_dir)])
# 静态资源 URL 版本戳：C 端与后台共用同一套 /static 资源
configure_templates(templates.env)

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
        <h2>网页账号功能未开放</h2>
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


# 《用户协议》《隐私政策》正文：注册页的勾选框必须有可读的落点，
# 否则"要求同意一份读不到的文件"在合规上等于没取得同意。
# 版本号复用凭据模型里的常量，保证"同意的版本"与"落库记录的版本"是同一个值。
_LEGAL_SECTIONS_TERMS = [
    {
        "heading": "一、这是一个什么样的服务",
        "body": "本实例是自建（self-hosted）的 AI 角色扮演对话服务：你与一个由部署者配置"
                "的虚拟角色对话。角色不是真实人物，它的所有发言都由大模型生成，"
                "不代表任何真实个体的观点、身份或经历。",
    },
    {
        "heading": "二、账号与昵称",
        "bullets": [
            "你注册的账号名与昵称仅用于在本实例里识别你的会话与你的角色配置。",
            "不得冒用真实人名人、机构或他人身份注册，也不得使用侵犯第三方权利的昵称。",
            "一个账号可以创建多个渠道身份与对话服务，它们同属这一个账号，删除账号会一并清除。",
        ],
    },
    {
        "heading": "三、使用规范",
        "bullets": [
            "不得利用本服务生成、传播违法内容，或用于欺骗、骚扰、诽谤他人。",
            "不得对真实人物进行冒充、污名化或性化描绘；涉及公众人物的内容不得被误当作其本人发言。",
            "须年满 14 周岁；未满 18 周岁应在监护人指导下使用。",
            "不得通过脚本批量注册、压测或以其他滥用方式影响本实例的可用性。",
        ],
    },
    {
        "heading": "四、服务性质与免责",
        "body": "本服务按\"现状\"提供，不保证连续可用、不保证输出准确或适宜。"
                "模型可能生成看似可信但失实的内容，请勿据此做出医疗、法律、财务等现实决策。",
        "bullets": [
            "部署者可以调整角色、限流、暂停或终止服务，也可依据本协议删除违规账号与内容。",
            "对话内容不构成任何专业建议，也不代表部署者立场。",
        ],
    },
    {
        "heading": "五、协议变更",
        "body": "协议更新后会在本页面标注新版本号。若你在变更后继续使用该服务，"
                "视为接受新版本协议；不同意时请停止使用并要求删除账号与数据。",
    },
]

_LEGAL_SECTIONS_PRIVACY = [
    {
        "heading": "一、谁是数据处理者",
        "body": "本实例由其部署者（个人或组织）运行与维护，部署者就是你所填信息的处理者。"
                "开源项目的作者不接收、不转发、也不存储本实例的任何数据。"
                "具体联系与处理途径请咨询本实例管理员。",
    },
    {
        "heading": "二、存了什么、存在哪",
        "bullets": [
            "账号与渠道身份：账号标识、身份标识与所属平台、注册与最近活跃时间。",
            "登录凭据：用户名与经 PBKDF2-HMAC-SHA256 加盐哈希后的口令；不保存明文口令。",
            "对话数据：消息正文、时间标签、你上传或生成的图片与语音文件。",
            "记忆与上下文：长期记忆条目、摘要、注入模型的上下文快照、反思得到的洞见。",
            "运营与安全记录：额度用量、主动关怀发送记录、登录失败与锁定计数、内容安全事件。",
            "全部数据保存在本实例自己的数据库与磁盘目录中，随实例部署位置而定。",
        ],
    },
    {
        "heading": "三、会发给谁",
        "bullets": [
            "为了生成回复，对话上下文会发送给部署者配置的大模型服务（例如阿里云百炼）。"
            "该第三方按它自己的隐私政策处理这些内容，本服务无法代替其作出承诺。",
            "若部署者开启了语音合成/识别、图片生成等能力，相应文本或图片也会发送到对应服务。",
            "除上述模型调用外，本服务不向你所在区域之外的广告、统计或分析平台发送数据；"
            "页面静态资源由本实例自带，不引用外部 CDN。",
        ],
    },
    {
        "heading": "四、保留与删除",
        "bullets": [
            "你可以随时删除单条消息、某个对话、某个对话服务及其记忆，或注销整个账号。",
            "删除按级联执行：会话一并清除其消息、上下文快照与仅被它们引用的媒体文件。",
            "部署者可能按自身策略定期备份数据库；备份有保留期限，删除请求在备份被清理前"
            "可能仍存在于历史备份中，备份同样受本政策约束。",
        ],
    },
    {
        "heading": "五、Cookie 与安全",
        "bullets": [
            "登录后本服务在浏览器写入会话 Cookie（HttpOnly、SameSite=Lax），仅用于维持登录状态；"
            "不用于跨站追踪。",
            "建议使用 HTTPS 访问；口令以哈希形式存储，但请注意：对话内容以明文保存在本实例中，"
            "请勿输入你不愿被部署者读取的信息。",
        ],
    },
    {
        "heading": "六、未成年人保护",
        "body": "未满 14 周岁不得注册使用。监护人发现未成年人使用了本服务，"
                "可要求部署者删除该账号及其全部数据。",
    },
    {
        "heading": "七、政策变更",
        "body": "政策更新会在本页面标注新版本号，重大变更同时会在注册与登录入口提示。"
                "此前记录的同意版本会保留，用于证明你在何时同意了何版内容。",
    },
]


def _legal_doc(title: str, version: str, sections: list) -> dict:
    return {
        "title": title,
        "version": version,
        "updated_at": "",
        "sections": sections,
    }


@web_router.get("/web/terms", response_class=HTMLResponse)
async def terms_page(request: Request) -> Response:
    """《用户协议》正文：注册页勾选框指向此处。

    不随 `web_auth_enabled` 关闭：即便暂时不开放注册，已注册用户与监管询问仍需能读到条款。
    """
    return _render(
        request,
        "legal.html",
        {"doc": _legal_doc("用户协议", TERMS_VERSION, _LEGAL_SECTIONS_TERMS)},
    )


@web_router.get("/web/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request) -> Response:
    """《隐私政策》正文：说明数据存在本机、只在调用模型时出站。"""
    return _render(
        request,
        "legal.html",
        {"doc": _legal_doc("隐私政策", PRIVACY_VERSION, _LEGAL_SECTIONS_PRIVACY)},
    )


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
    return _render(
        request,
        "chat.html",
        {
            "voice_input_enabled": settings.asr_enabled,
            "voice_reply_enabled": settings.tts_enabled,
        },
    )


@web_router.get("/web/profiles", response_class=HTMLResponse)
async def profiles_page(request: Request) -> Response:
    """旧角色页已并入「聊天服务」，保留链接并落到角色分区。"""
    if not settings.web_auth_enabled:
        return _disabled_page()
    return RedirectResponse(url="/web/services#roles", status_code=307)


@web_router.get("/web/services", response_class=HTMLResponse)
async def services_page(request: Request) -> Response:
    """聊天服务自助管理页：渠道、服务与角色三个分区。"""
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(request, "services.html")


@web_router.get("/web/account", response_class=HTMLResponse)
async def account_page(request: Request) -> Response:
    """账号中心：资料、密码与登录设备、额度、数据与隐私、注销。

    页面只是静态壳，账号数据全部由前端带 Bearer 令牌调 `/api/web/account/*` 取，
    因此开关关闭时同样只返回 403 提示页，不下发任何身份数据。
    """
    if not settings.web_auth_enabled:
        return _disabled_page()
    return _render(request, "account.html")


@web_router.get("/", include_in_schema=False)
async def index() -> Response:
    """根路径入口：开启网页功能时引导到登录页，关闭时维持原有 404。"""
    if not settings.web_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return RedirectResponse(url="/web/login", status_code=status.HTTP_303_SEE_OTHER)
