"""Admin 主动关怀管理路由。

展示主动消息（沉默召回 + 事件跟进）的发送记录与效果统计，
让原本"黑盒"运行的主动关怀能力可观测：发送量、成功率、阶段分布；
同时支持配置免打扰时段、用户时区与分享型消息比例（保存即热生效）。
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _render, _identity_repo
from app.db.repositories.proactive_repo import ProactiveRecordRepository

proactive_router = APIRouter(tags=["admin"])
_proactive_repo = ProactiveRecordRepository()

_PAGE_SIZE = 30

# 记录清理默认保留天数（页面表单默认值与预览口径保持一致）
_DEFAULT_RETAIN_DAYS = 30

# 阶段编号 → 中文说明（0=事件驱动，1-4=渐进召回）
_STAGE_LABELS: dict[int, str] = {
    0: "事件跟进",
    1: "阶段1·轻松问候",
    2: "阶段2·分享话题",
    3: "阶段3·温柔关心",
    4: "阶段4·暖心鼓励",
}

# 内容类型 → 中文徽标（旧记录无该字段时默认关怀型）
_CONTENT_TYPE_LABELS: dict[str, str] = {
    "care": "关怀",
    "news": "时事分享",
    "trivia": "趣事分享",
}


@proactive_router.get("/proactive", response_class=HTMLResponse)
async def proactive_page(
    request: Request,
    status: str = "",
    page: int = 1,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """主动关怀管理页（记录分页 + 近 7 天统计）。"""
    filter_status = status or None
    total = await _proactive_repo.count_all(filter_status)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    skip = (page - 1) * _PAGE_SIZE

    records = await _proactive_repo.list_recent(
        skip=skip, limit=_PAGE_SIZE, status=filter_status
    )

    # 补充用户展示名与阶段中文
    record_infos = []
    for rec in records:
        display = rec.platform_openid
        if rec.identity_id:
            try:
                identity = await _identity_repo.find_by_id(rec.identity_id)
                if identity:
                    display = identity.nickname or identity.name or rec.platform_openid
            except Exception:
                pass
        record_infos.append({
            "record": rec,
            "identity_display": display,
            "stage_label": _STAGE_LABELS.get(rec.stage, f"阶段{rec.stage}"),
            "type_label": _CONTENT_TYPE_LABELS.get(
                getattr(rec, "content_type", "care"), "关怀"
            ),
        })

    summary = await _proactive_repo.stats_summary(days=7)
    # 按阶段分布补充中文标签，便于模板渲染
    stage_dist = [
        {"label": _STAGE_LABELS.get(s, f"阶段{s}"), "count": c}
        for s, c in sorted(summary.get("by_stage", {}).items())
    ]

    # 清理预览：默认保留天数下可删除的记录数（全部 / 仅失败）
    cleanup_preview = await _proactive_repo.count_older_than(_DEFAULT_RETAIN_DAYS)
    cleanup_preview_failed = await _proactive_repo.count_older_than(
        _DEFAULT_RETAIN_DAYS, only_failed=True
    )
    # 删除后的回显条数（由清理/单条删除跳转携带）
    deleted_param = request.query_params.get("deleted", "")
    deleted = int(deleted_param) if deleted_param.isdigit() else 0

    from app.config import settings

    return _render(request, "proactive.html", {
        "record_infos": record_infos,
        "summary": summary,
        "stage_dist": stage_dist,
        "status": status,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "quiet_start": settings.proactive_quiet_hour_start,
        "quiet_end": settings.proactive_quiet_hour_end,
        # 时区取单一源 default_timezone_offset（对话时间感知与免打扰共用同一配置）
        "tz_offset": settings.default_timezone_offset,
        "share_enabled": settings.proactive_share_enabled,
        "share_ratio": settings.proactive_share_ratio,
        "share_news_ratio": settings.proactive_share_news_ratio,
        "saved": request.query_params.get("saved") == "1",
        "deleted": deleted,
        "retain_days": _DEFAULT_RETAIN_DAYS,
        "cleanup_preview": cleanup_preview,
        "cleanup_preview_failed": cleanup_preview_failed,
        "active_page": "proactive",
    })


@proactive_router.post("/proactive/quiet-hours")
async def save_quiet_hours(
    quiet_start: int = Form(22),
    quiet_end: int = Form(8),
    tz_offset: int = Form(8),
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """保存免打扰时段与用户时区：落库 -> 热重载（调度器运行时读取，无需重启）。

    时区只写单一源 default_timezone_offset，对话时间感知与主动关怀共用它，
    避免运营改了主动关怀时区后对话侧仍用旧时区。
    """
    from app.config import settings
    from app.db.repositories.app_settings_repo import AppSettingsRepository

    tz = max(-12, min(14, tz_offset))
    await AppSettingsRepository().upsert({
        "proactive_quiet_hour_start": max(0, min(23, quiet_start)),
        "proactive_quiet_hour_end": max(0, min(23, quiet_end)),
        "default_timezone_offset": tz,
    })
    await settings.reload()

    return RedirectResponse(url="/admin/proactive?saved=1", status_code=303)


@proactive_router.post("/proactive/share-settings")
async def save_share_settings(
    share_enabled: str = Form(""),
    share_ratio: float = Form(0.5),
    share_news_ratio: float = Form(0.5),
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """保存分享型消息配置：落库 -> 热重载（调度器运行时读取，无需重启）。"""
    from app.config import settings
    from app.db.repositories.app_settings_repo import AppSettingsRepository

    await AppSettingsRepository().upsert({
        "proactive_share_enabled": share_enabled == "on",
        "proactive_share_ratio": max(0.0, min(1.0, share_ratio)),
        "proactive_share_news_ratio": max(0.0, min(1.0, share_news_ratio)),
    })
    await settings.reload()

    return RedirectResponse(url="/admin/proactive?saved=1", status_code=303)


def _redirect_with_deleted(count: int, status: str) -> RedirectResponse:
    """跳回列表页并携带删除条数与原状态筛选。"""
    suffix = f"&status={status}" if status in ("sent", "failed", "pending") else ""
    return RedirectResponse(
        url=f"/admin/proactive?deleted={count}{suffix}", status_code=303
    )


@proactive_router.post("/proactive/records/cleanup")
async def cleanup_records(
    retain_days: int = Form(_DEFAULT_RETAIN_DAYS),
    only_failed: str = Form(""),
    status: str = Form(""),
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """按保留天数批量清理老旧主动消息记录（可选仅清理失败记录）。

    保留天数钳制到 1~3650，避免误传 0 导致全量删除；
    仅清理失败记录时可保留成功记录，不影响月配额与阶段判定。
    """
    days = max(1, min(3650, retain_days))
    deleted = await _proactive_repo.delete_older_than(
        days, only_failed=only_failed == "on"
    )
    return _redirect_with_deleted(deleted, status)


@proactive_router.post("/proactive/records/{record_id}/delete")
async def delete_record(
    record_id: str,
    status: str = Form(""),
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """删除单条主动消息记录（清理误发/测试记录）。"""
    deleted = 1 if await _proactive_repo.delete_by_id(record_id) else 0
    return _redirect_with_deleted(deleted, status)
