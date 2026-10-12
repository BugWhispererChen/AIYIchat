"""Admin 套餐计费路由。

提供额度套餐模板的管理与账号开通台账：
- /admin/billing            套餐模板管理 + 开通台账
- /admin/billing/templates/create|update|delete  模板增删改
- /admin/accounts/{id}/apply-template            为账号开通套餐（写额度+记台账）

本期为运营手动开通，不接第三方支付；amount 为可选记账金额。
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _account_repo, _render
from app.db.repositories.order_repo import OrderRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository
from app.models.order import OrderCreate
from app.models.quota import AccountQuotaUpdate, QuotaEntry
from app.models.quota_template import QuotaTemplateCreate, QuotaTemplateUpdate

billing_router = APIRouter(tags=["admin"])
_template_repo = QuotaTemplateRepository()
_order_repo = OrderRepository()
_quota_repo = QuotaRepository()

_PAGE_SIZE = 30


def _to_int(value, default: int = 0) -> int:
    """安全解析表单整数。"""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return default


@billing_router.get("/billing", response_class=HTMLResponse)
async def billing_page(
    request: Request,
    page: int = 1,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """套餐模板管理 + 开通台账页。"""
    templates = await _template_repo.list_all()

    total = await _order_repo.count()
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    skip = (page - 1) * _PAGE_SIZE
    orders = await _order_repo.list_recent(skip=skip, limit=_PAGE_SIZE)
    revenue_30d = await _order_repo.sum_amount(days=30)

    return _render(request, "billing.html", {
        "templates": templates,
        "orders": orders,
        "revenue_30d": revenue_30d,
        "order_total": total,
        "page": page,
        "total_pages": total_pages,
        "active_page": "billing",
    })


@billing_router.post("/billing/templates/create", response_class=HTMLResponse)
async def template_create(
    request: Request, _s: str = Depends(require_admin)
) -> RedirectResponse:
    """创建额度套餐模板。"""
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if name:
        validity_raw = str(form.get("validity_days", "")).strip()
        await _template_repo.create(
            QuotaTemplateCreate(
                name=name,
                text_limit=_to_int(form.get("text_limit")),
                voice_limit=_to_int(form.get("voice_limit")),
                image_limit=_to_int(form.get("image_limit")),
                validity_days=_to_int(validity_raw) if validity_raw else None,
            )
        )
    return RedirectResponse(
        url="/admin/billing", status_code=status.HTTP_303_SEE_OTHER
    )


@billing_router.post(
    "/billing/templates/{template_id}/update", response_class=HTMLResponse
)
async def template_update(
    request: Request, template_id: str, _s: str = Depends(require_admin)
) -> RedirectResponse:
    """编辑额度套餐模板。"""
    form = await request.form()
    validity_raw = str(form.get("validity_days", "")).strip()
    await _template_repo.update(
        template_id,
        QuotaTemplateUpdate(
            name=str(form.get("name", "")).strip() or None,
            text_limit=_to_int(form.get("text_limit")),
            voice_limit=_to_int(form.get("voice_limit")),
            image_limit=_to_int(form.get("image_limit")),
            validity_days=_to_int(validity_raw) if validity_raw else None,
        ),
    )
    return RedirectResponse(
        url="/admin/billing", status_code=status.HTTP_303_SEE_OTHER
    )


@billing_router.post(
    "/billing/templates/{template_id}/delete", response_class=HTMLResponse
)
async def template_delete(
    template_id: str, _s: str = Depends(require_admin)
) -> RedirectResponse:
    """删除额度套餐模板。"""
    await _template_repo.delete(template_id)
    return RedirectResponse(
        url="/admin/billing", status_code=status.HTTP_303_SEE_OTHER
    )


@billing_router.post(
    "/accounts/{account_id}/apply-template", response_class=HTMLResponse
)
async def apply_template(
    request: Request, account_id: str, _s: str = Depends(require_admin)
) -> RedirectResponse:
    """为账号开通套餐：写入三类额度（重置 used）+ 计算有效期 + 记台账。"""
    form = await request.form()
    template_id = str(form.get("template_id", "")).strip()

    account = await _account_repo.find_by_id(account_id)
    template = (
        await _template_repo.find_by_id(template_id) if template_id else None
    )
    if account is None or template is None:
        return RedirectResponse(
            url=f"/admin/accounts/{account_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # 计算过期时间（None=永久）
    expires_at = None
    if template.validity_days and template.validity_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=template.validity_days
        )

    # 写入额度：开通即重置已用量，按套餐设定上限与有效期
    await _quota_repo.create_or_update(
        account_id,
        AccountQuotaUpdate(
            text_quota=QuotaEntry(
                limit=template.text_limit, used=0, expires_at=expires_at
            ),
            voice_quota=QuotaEntry(
                limit=template.voice_limit, used=0, expires_at=expires_at
            ),
            image_quota=QuotaEntry(
                limit=template.image_limit, used=0, expires_at=expires_at
            ),
        ),
    )

    # 记台账
    await _order_repo.create(
        OrderCreate(
            account_id=account_id,
            account_name=account.name,
            template_id=template_id,
            template_name=template.name,
            text_limit=template.text_limit,
            voice_limit=template.voice_limit,
            image_limit=template.image_limit,
            validity_days=template.validity_days,
            amount=float(str(form.get("amount", "0") or "0")),
            remark=str(form.get("remark", "")).strip(),
        )
    )

    return RedirectResponse(
        url=f"/admin/accounts/{account_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
