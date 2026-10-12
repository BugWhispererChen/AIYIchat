import logging
from datetime import timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin.auth import (
    create_session,
    destroy_session,
    require_admin,
    verify_credentials,
)
from app.admin.routes.accounts import accounts_router
from app.admin.routes.billing import billing_router
from app.admin.routes.conversations import conversations_router
from app.admin.routes.features import features_router
from app.admin.routes.memories import memories_router
from app.admin.routes.metrics import metrics_router
from app.admin.routes.password import password_router
from app.admin.routes.profiles import profiles_router
from app.admin.routes.profile_create import profile_create_router
from app.admin.routes.proactive import proactive_router
from app.admin.routes.chat_import import chat_import_router
from app.admin.routes.safety import safety_router
from app.admin.routes.stickers import stickers_router
from app.admin.routes.qq_bots import qq_bots_router
from app.admin.routes.wechat_bots import wechat_bots_router
from app.admin.stats import get_dashboard_charts, get_dashboard_stats
from app.admin.system_metrics import get_all_metrics
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.user_repo import UserRepository

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

_beijing_tz = timezone(timedelta(hours=8))


def _to_beijing(dt):
    """UTC datetime → 北京时间"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_beijing_tz)


templates.env.filters["to_beijing"] = _to_beijing

admin_router = APIRouter(prefix="/admin", tags=["admin"])

_profile_repo = ProfileRepository()
_user_repo = UserRepository()
_conv_repo = ConversationRepository()
_msg_repo = MessageRepository()
_bot_config_repo = BotConfigRepository()


def _render(request: Request, name: str, ctx: dict | None = None, **kwargs):
    context = ctx or {}
    context["request"] = request
    return templates.TemplateResponse(request=request, name=name, context=context, **kwargs)


# ── 认证相关 ──────────────────────────────────────────────


@admin_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # 修改密码成功后跳转登录页时携带 msg=password_changed，给出成功提示
    msg = "密码已修改，请使用新密码重新登录" if request.query_params.get("msg") == "password_changed" else None
    return _render(request, "login.html", {"success": msg})


@admin_router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not await verify_credentials(username, password):
        return _render(
            request,
            "login.html",
            {"error": "用户名或密码错误"},
        )
    response = RedirectResponse(url="/admin/", status_code=status.HTTP_303_SEE_OTHER)
    create_session(response)
    return response


@admin_router.get("/logout")
async def logout(request: Request, session: str = Depends(require_admin)):
    response = RedirectResponse(url="/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    destroy_session(response, session)
    return response


# ── 仪表盘 ────────────────────────────────────────────────


@admin_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _s: str = Depends(require_admin)):
    stats = await get_dashboard_stats()
    charts = await get_dashboard_charts(days=14)
    # 语义检索服务健康状态（降级时顶部告警，避免配置错误被静默掩盖）
    from app.services.embedding_service import get_embedding_service

    embedding_status = get_embedding_service().status()
    return _render(request, "dashboard.html", {
        "stats": stats,
        "charts": charts,
        "embedding_status": embedding_status,
    })


# ── AI模型与多媒体设置 ────────────────────────────────────


def _mask_api_key(api_key: str) -> str:
    """对 API Key 做脱敏：长度≥8 显示前4+****+后4，否则等长度全部用 * 替代。"""
    if not api_key:
        return ""
    if len(api_key) >= 8:
        return f"{api_key[:4]}****{api_key[-4:]}"
    return "*" * len(api_key)


def _group_field(group, key: str) -> str:
    """从一组基础配置（dict 或对象）读取字段。"""
    val = group.get(key, "") if isinstance(group, dict) else getattr(group, key, "")
    return val if val is not None else ""


def _masked_model_configs(configs) -> list[dict]:
    """将基础配置组的 api_key 脱敏后传给模板。"""
    result: list[dict] = []
    for g in configs or []:
        result.append({
            "id": _group_field(g, "id"),
            "name": _group_field(g, "name"),
            "base_url": _group_field(g, "base_url"),
            "api_key_masked": _mask_api_key(_group_field(g, "api_key")),
            "has_key": bool(_group_field(g, "api_key")),
            "default_model": _group_field(g, "default_model"),
        })
    return result


@admin_router.get("/multimedia", response_class=HTMLResponse)
async def multimedia_page(request: Request, _s: str = Depends(require_admin)) -> HTMLResponse:
    """AI 模型与多媒体配置页面（GET）。"""
    from app.config import settings
    return _render(request, "multimedia.html", {
        "cfg": settings,
        "model_configs": _masked_model_configs(settings.model_configs),
    })


def _set_int_field(form, updates: dict, name: str) -> None:
    """解析表单整数字段，空或非法则跳过（不覆盖原值）。"""
    raw = (form.get(name) or "").strip()
    if raw:
        try:
            updates[name] = int(raw)
        except ValueError:
            pass


def _set_float_field(form, updates: dict, name: str) -> None:
    """解析表单浮点字段，空或非法则跳过（不覆盖原值）。"""
    raw = (form.get(name) or "").strip()
    if raw:
        try:
            updates[name] = float(raw)
        except ValueError:
            pass


_TEXT_TASKS = ("chat", "proactive", "focus", "emotion", "summary", "chat_import")
_MEDIA_TASKS = ("asr", "tts", "vision", "imagegen", "imagegen_edit", "embedding")


def _parse_model_configs(form, settings) -> list[dict]:
    """解析前端提交的基础配置组 JSON，按 id 合并（空/脱敏密钥保留原值）。"""
    import json

    raw_json = form.get("model_configs_json") or "[]"
    try:
        incoming = json.loads(raw_json)
    except (ValueError, TypeError):
        incoming = []

    existing_by_id: dict[str, str] = {}
    for g in (settings.model_configs or []):
        gid = _group_field(g, "id")
        if gid:
            existing_by_id[gid] = _group_field(g, "api_key")

    merged: list[dict] = []
    for g in incoming:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        api_key = str(g.get("api_key") or "")
        # 空或含脱敏字符则保留原密钥
        if (not api_key) or ("*" in api_key):
            api_key = existing_by_id.get(gid, "")
        merged.append({
            "id": gid,
            "name": str(g.get("name") or "").strip(),
            "base_url": str(g.get("base_url") or "").strip(),
            "api_key": api_key,
            "default_model": str(g.get("default_model") or "").strip(),
        })
    return merged


@admin_router.post("/multimedia", response_class=HTMLResponse)
async def multimedia_save(request: Request, _s: str = Depends(require_admin)) -> HTMLResponse:
    """保存 AI 模型配置到数据库并热加载，重置 Provider 与 Embedding 缓存。"""
    from app.config import settings
    from app.db.repositories.app_settings_repo import AppSettingsRepository
    from app.llm.provider_factory import reset_provider
    from app.services.embedding_service import reset_embedding_service
    from app.services.vision_service import reset_vision_service

    form = await request.form()
    updates: dict[str, object] = {}

    # 基础配置组
    updates["model_configs"] = _parse_model_configs(form, settings)

    # 文本功能字段
    for t in _TEXT_TASKS:
        updates[f"{t}_config_id"] = form.get(f"{t}_config_id", "")
        _set_float_field(form, updates, f"{t}_temperature")
        _set_int_field(form, updates, f"{t}_max_tokens")
        _set_int_field(form, updates, f"{t}_timeout_seconds")

    # 多媒体功能字段
    for m in _MEDIA_TASKS:
        updates[f"{m}_enabled"] = form.get(f"{m}_enabled") == "on"
        updates[f"{m}_config_id"] = form.get(f"{m}_config_id", "")

    repo = AppSettingsRepository()
    await repo.upsert(updates)

    # 热加载并重置缓存（Provider / Vision / Embedding 惰性重建）
    await settings.reload()
    reset_provider()
    reset_vision_service()
    reset_embedding_service()

    return _render(request, "multimedia.html", {
        "cfg": settings,
        "model_configs": _masked_model_configs(settings.model_configs),
        "success": "AI 模型配置已保存并生效",
    })


# ── 配置热加载 ─────────────────────────────────────────────


@admin_router.post("/api/reload-settings")
async def reload_settings(_s: str = Depends(require_admin)):
    """热重载配置（不重启服务），并重置 Provider / Vision / Embedding 缓存。"""
    from app.config import settings
    from app.llm.provider_factory import reset_provider
    from app.services.embedding_service import reset_embedding_service
    from app.services.vision_service import reset_vision_service

    changed = await settings.reload()

    # 配置变更后统一重置缓存，Provider / Vision / Embedding 下次调用时惰性重建
    if changed:
        reset_provider()
        reset_vision_service()
        reset_embedding_service()

    return JSONResponse({
        "status": "ok",
        "changed_fields": sorted(changed),
        "count": len(changed),
    })


# ── 系统监控 API ───────────────────────────────────────────


@admin_router.get("/api/metrics", response_class=HTMLResponse)
async def api_metrics(request: Request, _s: str = Depends(require_admin)):
    """返回系统资源监控 HTML 片段（供 HTMX 轮询加载）。"""
    metrics = get_all_metrics()
    return _render(request, "partials/metrics.html", {"metrics": metrics})


# ── 挂载重构后的模块化子路由 ───────────────────────────────

admin_router.include_router(accounts_router)
# profile_create_router 必须先于 profiles_router 注册，
# 确保 /profiles/create 优先于 /profiles/{slug} 匹配
admin_router.include_router(profile_create_router)
admin_router.include_router(profiles_router)
admin_router.include_router(conversations_router)
admin_router.include_router(wechat_bots_router)
admin_router.include_router(qq_bots_router)
admin_router.include_router(chat_import_router)
admin_router.include_router(features_router)
admin_router.include_router(stickers_router)
admin_router.include_router(safety_router)
admin_router.include_router(proactive_router)
admin_router.include_router(memories_router)
admin_router.include_router(metrics_router)
admin_router.include_router(billing_router)
admin_router.include_router(password_router)
