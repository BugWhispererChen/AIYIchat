"""Admin 路由公共依赖与工具函数。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.utils.assets import asset

# ── 模板配置 ──────────────────────────────────────────────

_template_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))

_beijing_tz = timezone(timedelta(hours=8))


def _to_beijing(dt):
    """UTC datetime → 北京时间。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_beijing_tz)


def _format_quota(value: Any) -> str:
    """格式化额度数字：>=10亿显示为'无限'，>=1万按万显示，其余按千分位。"""
    if not isinstance(value, (int, float)):
        return str(value)
    if value >= 1_000_000_000:
        return "无限"
    if value >= 10_000:
        return f"{value / 10000:.1f}万"
    return f"{value:,}"


def _relative_time(dt: Any) -> str:
    """相对时间显示：刚刚 / x分钟前 / x小时前 / x天前；超过7天显示 北京时间日期。"""
    if dt is None:
        return "-"
    now = datetime.now(timezone.utc)
    # 兼容 naive datetime（按 UTC 处理）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 0:
        # 时钟漂移或未来时间，统一视为刚刚
        return "刚刚"
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    if seconds < 604800:
        return f"{seconds // 86400}天前"
    # 超过7天显示北京时间日期
    beijing = dt.astimezone(_beijing_tz)
    return beijing.strftime('%m-%d %H:%M')


templates.env.filters["to_beijing"] = _to_beijing
templates.env.filters["format_quota"] = _format_quota
templates.env.filters["relative_time"] = _relative_time
# 静态资源 URL 版本戳（admin 各路由页与 admin/router.py 用的是不同实例，需各自注册）
templates.env.globals["asset"] = asset


# ── Repository 实例（重构第二阶段再消除单例）───────────────

_profile_repo = ProfileRepository()
_identity_repo = IdentityRepository()
_conv_repo = ConversationRepository()
_msg_repo = MessageRepository()
_bot_config_repo = BotConfigRepository()
_account_repo = AccountRepository()
_service_repo = ServiceRepository()


# ── 渲染辅助 ──────────────────────────────────────────────

def _render(request: Request, name: str, ctx: dict | None = None, **kwargs):
    """渲染 Jinja2 模板，自动注入 request 对象。"""
    context = ctx or {}
    context["request"] = request
    return templates.TemplateResponse(request=request, name=name, context=context, **kwargs)
