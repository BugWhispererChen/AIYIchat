"""应用启动引导模块：包含 FastAPI 应用工厂、生命周期管理与全局异常处理。

本模块由 main.py 迁入，集中承载启动编排逻辑。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db.mongo import close_db, connect_db
from app.utils.assets import STATIC_DIR


def setup_logging() -> None:
    """配置日志输出，强制使用 UTF-8 编码避免中文乱码。"""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    # 强制 stdout 使用 utf-8 编码
    if hasattr(handler.stream, "reconfigure"):
        handler.stream.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        handlers=[handler],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting AIYIchat service...")

    await connect_db()
    logger.info("MongoDB connected")

    # 从数据库加载运行时配置（空库时自动从 .env seed）
    from app.config import load_settings_from_db

    await load_settings_from_db()
    logger.info("Runtime settings loaded")

    # tiktoken 的 BPE 文件首次使用需联网下载（境内实测 80s+）：放后台线程预热，
    # 请求路径只用已就绪的编码器，未就绪按字符近似，避免第一轮回复替它买单。
    import asyncio

    from app.conversation.context_manager import warm_up_encoder
    from app.utils.tasks import spawn

    spawn(asyncio.to_thread(warm_up_encoder))

    # 导入随包预置的表情包资源（幂等去重；失败不阻塞启动）
    try:
        from app.services.sticker_seeder import seed_stickers

        await seed_stickers()
    except Exception as e:
        logger.warning("预置表情包 seed 失败（不影响启动）: %s", e)

    # 导入预置角色（幂等 upsert；身份已修改的角色跳过覆盖，仅补齐空定妆照）
    try:
        from tools.seed_companion_profiles import seed_profiles

        await seed_profiles()
    except Exception as e:
        logger.warning("预置角色 seed 失败（不影响启动）: %s", e)

    # 检查可用角色：新部署可能尚未创建角色，此时对话将提示创建角色
    from app.db.repositories.profile_repo import ProfileRepository

    active_profiles = [
        p for p in await ProfileRepository().list_active() if not p.is_draft
    ]
    if not active_profiles:
        logger.warning(
            "当前无任何可用角色，身份对话将提示创建角色，"
            "请尽快在管理后台「角色管理」中创建角色"
        )

    # 从 MongoDB 加载 Bot 配置，仅启动被活跃服务引用且凭据齐全的 QQ 渠道
    from app.bot_config import async_load_bots_config
    from app.db.repositories.service_repo import ServiceRepository
    from app.adapters.qq_websocket import BotManager, is_runnable_qq

    bot_configs = await async_load_bots_config()
    active_bot_ids = await ServiceRepository().list_active_bot_ids()
    qq_configs = [
        c for c in bot_configs
        if c.id in active_bot_ids and is_runnable_qq(c)
    ]

    # 始终启动 QQ BotManager（即使初始无 Bot 也需要监控循环感知新服务）
    qq_manager = BotManager()
    await qq_manager.start(qq_configs)
    if qq_configs:
        logger.info("QQ BotManager started: %d bot(s)", len(qq_configs))
    else:
        logger.info("QQ BotManager started: 无活跃服务引用的 Bot，持续监控中")

    # 启动微信 BotManager
    from app.adapters.wechat_clawbot import get_wechat_manager

    wechat_manager = get_wechat_manager()
    await wechat_manager.load_bots()
    wechat_bots = wechat_manager.list_bots()
    if wechat_bots:
        logger.info("WeChat BotManager started: %d bot(s)", len(wechat_bots))

    # 启动主动消息调度器
    from app.proactive.scheduler import get_proactive_scheduler

    proactive_scheduler = get_proactive_scheduler()
    proactive_scheduler.set_managers(qq_manager, wechat_manager)
    await proactive_scheduler.start()
    logger.info("ProactiveScheduler started")

    yield

    await proactive_scheduler.stop()
    logger.info("ProactiveScheduler stopped")

    await qq_manager.shutdown()
    logger.info("QQ BotManager stopped")

    await wechat_manager.shutdown()
    logger.info("WeChat BotManager stopped")

    await close_db()
    logger.info("MongoDB disconnected. Service stopped.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="爱易聊 AIYIchat",
        description="基于Prompt Profile的角色扮演对话系统",
        version="0.1.0",
        lifespan=lifespan,
        # 接口文档只在非 production 暴露：公开部署下 /docs 等于把全部路由与参数结构
        # 摊给扫描器，后台路由清单本身就是攻击面目录
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
        openapi_url=(None if settings.app_env == "production" else "/openapi.json"),
    )

    # 前端静态资源：CSS/JS/图标与本地化的第三方库统一走 /static，
    # 不再引用外部 CDN，保证内网与离线部署下界面完整
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    # 注册 Trace ID 中间件（最外层，确保所有请求都有 Trace ID）
    from app.middleware.security import SecurityHeadersMiddleware
    from app.middleware.trace import TraceMiddleware

    app.add_middleware(TraceMiddleware)
    # 安全响应头：后台全站 Cookie 会话 + 表单操作，缺 frame 限制就是点击劫持的入口
    app.add_middleware(SecurityHeadersMiddleware)

    from app.admin.router import admin_router
    from app.api.router import api_router
    from app.web.router import web_router

    app.include_router(api_router, prefix="/api")
    app.include_router(admin_router)
    # C 端网页页面：路由内部按 settings.web_auth_enabled 拦截，关闭时不暴露任何页面
    app.include_router(web_router)

    @app.exception_handler(HTTPException)
    async def admin_auth_redirect(request: Request, exc: HTTPException):
        """Admin 路由认证失败处理：HTMX/fetch 返回适当响应，浏览器导航重定向到登录页。"""
        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and request.url.path.startswith("/admin")
        ):
            # HTMX 请求：返回 HTML 错误提示，避免将登录页插入目标元素
            if request.headers.get("HX-Request"):
                from fastapi.responses import HTMLResponse
                return HTMLResponse(
                    content="<div class='alert alert-error'>会话已过期，请刷新页面重新登录</div>",
                    status_code=200
                )
            # fetch/XHR 等非浏览器导航请求：返回 JSON 401
            # 浏览器导航的 Accept 一般以 text/html 开头
            accept = request.headers.get("accept", "")
            if "text/html" not in accept:
                from fastapi.responses import JSONResponse as JR
                return JR(
                    status_code=401,
                    content={"detail": "会话已过期，请重新登录"},
                )
            return RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    return app
