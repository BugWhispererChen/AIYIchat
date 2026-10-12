"""Admin 内容安全审计路由。

展示对话过程中触发的内容安全事件（心理危机干预、违禁内容拦截），
提供分页列表与近 7 天统计，供运营合规审计与风险追溯；
同时支持维护运营追加词表（与内置词库合并，保存即热生效）。
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _render, _user_repo
from app.conversation.safety_checker import (
    builtin_word_counts,
    reset_safety_checker,
)
from app.db.repositories.safety_event_repo import SafetyEventRepository

safety_router = APIRouter(tags=["admin"])
_safety_repo = SafetyEventRepository()

_PAGE_SIZE = 30


def _parse_words(raw: str) -> list[str]:
    """将换行/逗号分隔的词表文本解析为去重保序的词列表。"""
    parts: list[str] = []
    for line in raw.replace("，", ",").replace(",", "\n").splitlines():
        word = line.strip()
        if word:
            parts.append(word)
    return list(dict.fromkeys(parts))


@safety_router.get("/safety", response_class=HTMLResponse)
async def safety_page(
    request: Request,
    event_type: str = "",
    page: int = 1,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """内容安全事件审计页（分页 + 近 7 天统计）。"""
    filter_type = event_type or None
    total = await _safety_repo.count(filter_type)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    skip = (page - 1) * _PAGE_SIZE

    events = await _safety_repo.list_recent(
        skip=skip, limit=_PAGE_SIZE, event_type=filter_type
    )

    # 补充用户展示名（昵称/名称），失败不阻断
    event_infos = []
    for ev in events:
        display = ev.platform_id
        if ev.user_id:
            try:
                user = await _user_repo.find_by_id(ev.user_id)
                if user:
                    display = user.nickname or user.name or ev.platform_id
            except Exception:
                pass
        event_infos.append({"event": ev, "user_display": display})

    stats_7d = await _safety_repo.count_recent_by_type(days=7)

    # 词表维护区数据：内置规模 + 当前追加词
    from app.config import settings

    builtin_crisis, builtin_forbidden = builtin_word_counts()

    return _render(request, "safety.html", {
        "event_infos": event_infos,
        "stats_7d": stats_7d,
        "crisis_7d": stats_7d.get("crisis", 0),
        "forbidden_7d": stats_7d.get("forbidden", 0),
        "minor_block_7d": stats_7d.get("minor_block", 0),
        "event_type": event_type,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "builtin_crisis_count": builtin_crisis,
        "builtin_forbidden_count": builtin_forbidden,
        "crisis_extra_text": "\n".join(settings.safety_crisis_extra),
        "forbidden_extra_text": "\n".join(settings.safety_forbidden_extra),
        "saved": request.query_params.get("saved") == "1",
        "active_page": "safety",
    })


@safety_router.post("/safety/wordlists")
async def save_wordlists(
    crisis_extra: str = Form(""),
    forbidden_extra: str = Form(""),
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """保存运营追加词表：落库 -> 热重载配置 -> 重建检查器。"""
    from app.config import settings
    from app.db.repositories.app_settings_repo import AppSettingsRepository

    await AppSettingsRepository().upsert({
        "safety_crisis_extra": _parse_words(crisis_extra),
        "safety_forbidden_extra": _parse_words(forbidden_extra),
    })
    await settings.reload()
    reset_safety_checker()

    return RedirectResponse(url="/admin/safety?saved=1", status_code=303)
