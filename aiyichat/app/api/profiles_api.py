from typing import Optional

from fastapi import APIRouter, HTTPException

from app.models.profile import PromptProfileCreate, PromptProfileUpdate
from app.profiles.service import ProfileService

profiles_router = APIRouter()
_service = ProfileService()


@profiles_router.get("")
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


@profiles_router.get("/{slug}")
async def get_profile(slug: str):
    """获取角色详情"""
    profile = await _service.get_by_slug(slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found")
    return profile.model_dump(by_alias=False)


@profiles_router.post("", status_code=201)
async def create_profile(data: PromptProfileCreate):
    """创建新角色"""
    existing = await _service.get_by_slug(data.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"Profile '{data.slug}' already exists")
    profile = await _service.create(data)
    return {"slug": profile.slug, "name": profile.name, "id": profile.id}


@profiles_router.put("/{slug}")
async def update_profile(slug: str, data: PromptProfileUpdate):
    """更新角色"""
    profile = await _service.update(slug, data)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found")
    return {"slug": profile.slug, "name": profile.name, "updated": True}


@profiles_router.delete("/{slug}")
async def delete_profile(slug: str):
    """删除角色（软删除）"""
    success = await _service.delete(slug)
    if not success:
        raise HTTPException(status_code=404, detail=f"Profile '{slug}' not found")
    return {"slug": slug, "deleted": True}



