"""Admin 克隆反馈评审入口（只读查看 + 标记已评审/驳回）。"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _render
from app.db.repositories.clone_repo import CloneFeedbackRepository

clone_feedback_router = APIRouter(tags=["admin"])
_feedback_repo = CloneFeedbackRepository()


@clone_feedback_router.get("/clone-feedback", response_class=HTMLResponse)
async def clone_feedback_page(
    request: Request, _s: str = Depends(require_admin)
) -> HTMLResponse:
    items = await _feedback_repo.list_recent(limit=200)
    return _render(request, "clone_feedback.html", {
        "active_page": "profiles",
        "items": items,
        "pending": sum(1 for item in items if item.status == "pending"),
    })


@clone_feedback_router.post("/clone-feedback/{feedback_id}/status")
async def clone_feedback_status(
    feedback_id: str,
    status: str = Form(...),
    _s: str = Depends(require_admin),
):
    if status not in {"pending", "reviewed", "rejected"}:
        raise HTTPException(status_code=400, detail="非法反馈状态")
    await _feedback_repo.set_status(feedback_id, status)
    return RedirectResponse(url="/admin/clone-feedback", status_code=303)
