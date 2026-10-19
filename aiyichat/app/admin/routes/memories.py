"""Admin 记忆管理路由。

展示各对话服务的长期记忆（话题焦点、记忆事实、待跟进事件），
支持删除单条事实与清空服务记忆，用于运营排查与身份隐私删除诉求。
只读 + 删除，不提供编辑（记忆由焦点提取链路自动维护）。
另提供记忆注入观测与质量评估指标（命中率 / 有效率）。
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _render
from app.db.repositories.injection_snapshot_repo import InjectionSnapshotRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository

logger = logging.getLogger(__name__)

memories_router = APIRouter(tags=["admin"])
_memory_repo = ServiceMemoryRepository()
_service_repo = ServiceRepository()
_snapshot_repo = InjectionSnapshotRepository()


async def _service_display(service_id: str) -> str:
    """获取服务展示名（兼容共享组 key；失败回退到 service_id 前缀）。"""
    try:
        service = await _service_repo.find_by_id(service_id)
        if service and service.name:
            return service.name
        # 非服务 ID 时尝试按共享组解析（共享组记忆以组 ID 作为归属 key）
        members = await _service_repo.list_by_share_group(service_id)
        if members:
            names = "、".join(m.name or m.id[:8] for m in members[:3])
            return f"共享组（{len(members)}个服务：{names}）"
    except Exception:
        pass
    return f"服务 {service_id[:8]}"


@memories_router.get("/memories", response_class=HTMLResponse)
async def memories_page(
    request: Request, _s: str = Depends(require_admin),
) -> HTMLResponse:
    """记忆总览页：列出所有存在长期记忆的服务及规模概况。"""
    memories = await _memory_repo.list_all()
    memory_infos = []
    for mem in memories:
        pending_open = sum(
            1 for e in mem.pending_events if not e.followed_up
        )
        memory_infos.append({
            "memory": mem,
            "service_display": await _service_display(mem.service_id),
            "fact_count": len(mem.facts),
            "embedded_count": sum(1 for f in mem.facts if f.embedding),
            "pending_open": pending_open,
        })

    return _render(request, "memories.html", {
        "memory_infos": memory_infos,
        "active_page": "memories",
    })


@memories_router.get(
    "/memories/{service_id}", response_class=HTMLResponse,
)
async def memory_detail(
    request: Request,
    service_id: str,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """记忆详情页：展示指定服务的话题脉络、事实列表与待跟进事件。"""
    memory = await _memory_repo.find_by_service(service_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="该服务暂无记忆")

    # 事实按最后引用时间倒序展示（与检索淘汰顺序一致）
    facts = sorted(
        memory.facts, key=lambda f: f.last_referenced_at, reverse=True
    )

    return _render(request, "memory_detail.html", {
        "detail": {
            "memory": memory,
            "facts": facts,
            "service_display": await _service_display(service_id),
        },
        "saved": request.query_params.get("saved") == "1",
        "active_page": "memories",
    })


@memories_router.post("/memories/{service_id}/delete-fact")
async def delete_fact(
    service_id: str,
    content: str = Form(...),
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """删除指定内容的记忆事实（运营纠错 / 身份隐私删除诉求）。"""
    await _memory_repo.delete_fact(service_id, content)
    logger.info("后台删除记忆事实: service=%s, content=%s", service_id, content[:30])
    return RedirectResponse(
        url=f"/admin/memories/{service_id}?saved=1", status_code=303,
    )


@memories_router.post("/memories/{service_id}/clear")
async def clear_memory(
    service_id: str,
    _s: str = Depends(require_admin),
) -> RedirectResponse:
    """清空该服务的全部记忆（话题、事实、待跟进事件一并删除）。"""
    await _memory_repo.delete_by_service(service_id)
    logger.info("后台清空服务记忆: service=%s", service_id)
    return RedirectResponse(url="/admin/memories", status_code=303)


@memories_router.get(
    "/memories/{service_id}/injections", response_class=HTMLResponse,
)
async def memory_injections(
    request: Request,
    service_id: str,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """记忆注入观测页：展示指定服务最近每轮对话的记忆注入明细与 token 占比。"""
    snapshots = await _snapshot_repo.list_by_service(service_id, limit=50)
    service_display = await _service_display(service_id)
    # 最新一条用于顶部 Prompt Breakdown 堆叠图
    latest = snapshots[0] if snapshots else None
    metrics = _compute_memory_metrics(snapshots)
    return _render(request, "memory_injections.html", {
        "snapshots": snapshots,
        "latest": latest,
        "metrics": metrics,
        "service_id": service_id,
        "service_display": service_display,
        "active_page": "memories",
    })


def _compute_memory_metrics(snapshots) -> dict:
    """基于注入快照计算记忆质量评估指标。

    - 记忆命中率：注入的记忆中，内容在本轮回复中出现（被实际用上）的比例
    - 记忆有效率：近 7 天内被引用过的记忆占比（反向反映“死记忆”规模）
    """
    if not snapshots:
        return {"hit_rate": None, "hit_count": 0, "total_injected": 0,
                "active_rate": None, "active_facts": 0, "total_facts": 0}

    # ── 命中率：注入事实内容是否出现在本轮回复中（粗判：去空白后子串包含）──
    total_injected = 0
    hit_count = 0
    for snap in snapshots:
        reply = (snap.reply_text or "").replace(" ", "").replace("\n", "")
        for f in snap.injected_facts:
            total_injected += 1
            content = (f.content or "").replace(" ", "").replace("\n", "")
            # 命中判定：记忆内容的关键片段（前 8 字）出现在回复中
            if content and len(content) >= 4 and content[:8] in reply:
                hit_count += 1
    hit_rate = (hit_count / total_injected) if total_injected else None

    # ── 有效率：近 7 天内有快照注入过的不同记忆占比 ──
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    recent_facts: set[str] = set()
    all_facts: set[str] = set()
    for snap in snapshots:
        for f in snap.injected_facts:
            all_facts.add(f.content)
            created = snap.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= week_ago:
                recent_facts.add(f.content)
    total_facts = len(all_facts)
    active_facts = len(recent_facts)
    active_rate = (active_facts / total_facts) if total_facts else None

    return {
        "hit_rate": hit_rate,
        "hit_count": hit_count,
        "total_injected": total_injected,
        "active_rate": active_rate,
        "active_facts": active_facts,
        "total_facts": total_facts,
    }
