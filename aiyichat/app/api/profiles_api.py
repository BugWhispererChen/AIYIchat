from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.admin.auth import require_admin
from app.models.profile import PromptProfileCreate, PromptProfileUpdate
from app.profiles.service import ProfileService

profiles_router = APIRouter()
_service = ProfileService()

# 本组端点是内部运维/集成用的全量角色接口（可见与可改范围不受账号约束），
# 因此整体置于管理员鉴权之下；C 端网页用户的自助角色管理走 /api/web/profiles。
_admin = Depends(require_admin)


@profiles_router.get("", dependencies=[_admin])
async def list_profiles(category: Optional[str] = None, active_only: bool = True):
    """获取角色列表"""
    if active_only:
        profiles = await _service.list_active(category)
    else:
        profiles = await _service.list_all()
    return [
        {
            "slug": p.slug,
            "name": p.name,
            "source": p.source,
            "category": p.category,
            "is_active": p.is_active,
        }
        for p in profiles
    ]


@profiles_router.get("/{slug}", dependencies=[_admin])
async def get_profile(slug: str):
    """获取角色详情"""
    profile = await _service.get_by_slug(slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found")
    return profile.model_dump(by_alias=False)


@profiles_router.post("", status_code=201, dependencies=[_admin])
async def create_profile(data: PromptProfileCreate):
    """创建新角色"""
    existing = await _service.get_by_slug(data.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Profile '{data.slug}' already exists")
    profile = await _service.create(data)
    return {"slug": profile.slug, "name": profile.name, "id": profile.id}


@profiles_router.put("/{slug}", dependencies=[_admin])
async def update_profile(slug: str, data: PromptProfileUpdate):
    """更新角色"""
    profile = await _service.update(slug, data)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found")
    return {"slug": profile.slug, "name": profile.name, "updated": True}


@profiles_router.delete("/{slug}", dependencies=[_admin])
async def delete_profile(slug: str):
    """删除角色（软删除）"""
    success = await _service.delete(slug)
    if not success:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found")
    return {"slug": slug, "deleted": True}



