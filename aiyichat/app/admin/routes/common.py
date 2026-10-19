"""Admin 路由公共依赖与工具函数。"""

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.template_filters import configure_templates

# ── 模板配置 ──────────────────────────────────────────────

_template_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))
# 过滤器统一定义在 app/template_filters.py，此处只做登记
configure_templates(templates.env)


def render_qr_fragment(
    lead: str,
    image_src: str,
    alt: str,
    hint: str,
    data_bind_task: str = "",
    data_qrcode: str = "",
) -> str:
    """渲染扫码二维码 HTML 片段，供 QQ / 微信 Bot 的 htmx 局部返回复用。"""
    return templates.get_template("partials/qr_fragment.html").render(
        lead=lead,
        image_src=image_src,
        alt=alt,
        hint=hint,
        data_bind_task=data_bind_task,
        data_qrcode=data_qrcode,
    )


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
