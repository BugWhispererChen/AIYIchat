"""Admin 角色（Profile）管理路由。

仅负责 HTTP 参数解析与模板渲染，业务逻辑（表单转换、启停校验、
定妆照设置、声音复刻）下沉到 app.profiles.service.ProfileService。
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _account_repo, _profile_repo, _render
from app.profiles.service import ProfileInUseError, ProfileService

logger = logging.getLogger(__name__)

profiles_router = APIRouter(tags=["admin"])
_profile_service = ProfileService()


async def _sticker_groups():
    """加载表情包分组列表（角色编辑表单多选上下文）。"""
    from app.db.repositories.sticker_group_repo import StickerGroupRepository

    return await StickerGroupRepository().list_all()

# 定妆照生成模板：正面半身肖像，便于参考图编辑提取面部特征
REF_PORTRAIT_PROMPT = (
    "realistic photo, frontal half-body portrait, facing camera, "
    "looking at camera, neutral simple background, soft natural lighting, "
    "clear facial features, no filter, "
)


@profiles_router.get("/profiles", response_class=HTMLResponse)
async def profiles_list(
    request: Request,
    q: str = "",
    vis: str = "",
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """展示所有角色列表，支持搜索与可见范围筛选。

    `vis=private` 用于巡查网页用户自建的角色：自助创建走内容安全前置校验，
    后台按此筛选做事后复核与禁用，是 C 端开放自建能力的合规兜底入口。
    """
    profiles = await _profile_repo.search(q, limit=100) if q.strip() else await _profile_repo.list_all()
    if vis in ("private", "public"):
        profiles = [p for p in profiles if (p.visibility or "public") == vis]
    accounts = {a.id: a for a in await _account_repo.list_all()}
    return _render(
        request, "profiles.html",
        {"profiles": profiles, "accounts": accounts, "q": q, "vis": vis},
    )


@profiles_router.get("/profiles/{slug}", response_class=HTMLResponse)
async def profile_detail(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """展示单个角色详情页。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")
    accounts = {a.id: a for a in await _account_repo.list_all()}
    return _render(request, "profile_detail.html", {
        "profile": profile,
        "accounts": accounts,
        "sticker_groups": await _sticker_groups(),
    })


@profiles_router.post("/profiles/{slug}/toggle", response_class=HTMLResponse)
async def profile_toggle(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """切换角色启用/禁用状态（HTMX 局部更新）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    form = await request.form()
    disable_reason = str(form.get("disable_reason", "")).strip()
    try:
        updated = await _profile_service.toggle_active(profile, disable_reason)
    except ProfileInUseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 行内 partial 会展示私有角色的归属账号，局部更新时同样需要账号表
    accounts = {a.id: a for a in await _account_repo.list_all()}
    return _render(request, "partials/profile_row.html", {"p": updated, "accounts": accounts})


@profiles_router.post("/profiles/{slug}/edit", response_class=HTMLResponse)
async def profile_edit(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """保存角色编辑表单（表单→模型转换在 ProfileService 中完成）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    form = await request.form()
    updated = await _profile_service.update_from_form(profile, form)
    accounts = {a.id: a for a in await _account_repo.list_all()}
    return _render(
        request, "profile_detail.html",
        {
            "profile": updated,
            "accounts": accounts,
            "sticker_groups": await _sticker_groups(),
            "success": "角色信息已更新",
        }
    )


# ── 定妆照（人物一致性参考肖像）────────────────────


async def _render_detail(
    request: Request, slug: str, **extra
) -> HTMLResponse:
    """重新渲染角色详情页（定妆照操作后的统一出口）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")
    accounts = {a.id: a for a in await _account_repo.list_all()}
    ctx: dict = {
        "profile": profile,
        "accounts": accounts,
        "sticker_groups": await _sticker_groups(),
    }
    ctx.update(extra)
    return _render(request, "profile_detail.html", ctx)


@profiles_router.post(
    "/profiles/{slug}/reference-image/upload", response_class=HTMLResponse
)
async def reference_image_upload(
    request: Request,
    slug: str,
    file: UploadFile = File(...),
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """上传图片并直接设为定妆照。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        return await _render_detail(
            request, slug, error="请上传图片文件（png/jpg/webp）"
        )
    data = await file.read()
    if not data:
        return await _render_detail(request, slug, error="上传文件为空")

    from app.services.media_storage import save_image_from_bytes

    media_url = await save_image_from_bytes(data, content_type)
    await _profile_service.set_reference_image(slug, media_url)
    return await _render_detail(request, slug, success="定妆照已更新")


@profiles_router.post(
    "/profiles/{slug}/reference-image/generate", response_class=HTMLResponse
)
async def reference_image_generate(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """用外貌描述生成定妆照候选（需管理员确认后才生效）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    appearance = (
        profile.identity.appearance or profile.identity.age_appearance
    ).strip()
    if not appearance:
        return await _render_detail(
            request, slug,
            error="请先在身份信息中填写外貌特征（英文）后再生成",
        )

    try:
        from app.services.imagegen_service import ImageGenService
        from app.services.media_storage import save_image_from_bytes

        img_bytes = await ImageGenService().generate(
            REF_PORTRAIT_PROMPT + appearance
        )
        media_url = await save_image_from_bytes(img_bytes)
    except Exception as e:
        logger.error("定妆照候选生成失败: %s", e, exc_info=True)
        return await _render_detail(
            request, slug, error=f"生成失败：{e}"
        )
    return await _render_detail(
        request, slug,
        ref_candidate_url=media_url,
        success="候选定妆照已生成，请预览后确认或重新生成",
    )


@profiles_router.post(
    "/profiles/{slug}/reference-image/confirm", response_class=HTMLResponse
)
async def reference_image_confirm(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """确认将候选图设为定妆照。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    form = await request.form()
    media_url = str(form.get("media_url", "")).strip()
    if not media_url.startswith("/api/media/"):
        return await _render_detail(request, slug, error="无效的候选图地址")
    await _profile_service.set_reference_image(slug, media_url)
    return await _render_detail(request, slug, success="定妆照已更新")


@profiles_router.post(
    "/profiles/{slug}/reference-image/clear", response_class=HTMLResponse
)
async def reference_image_clear(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """清除定妆照（本人出镜图片将退回外貌文字描述链路）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")
    await _profile_service.set_reference_image(slug, "")
    return await _render_detail(request, slug, success="定妆照已清除")


# ── 声音复刻 ─────────────────────────────────────────────

# 声音复刻允许的音频 content_type
_VOICE_CLONE_TYPES = frozenset({
    "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3",
    "audio/mp4", "audio/x-m4a", "audio/m4a",
})


@profiles_router.post(
    "/profiles/{slug}/voice-clone/upload", response_class=HTMLResponse
)
async def voice_clone_upload(
    request: Request,
    slug: str,
    voice_file: UploadFile = File(...),
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """上传参考音频并调用阿里云声音复刻 API 创建专属音色（base64 直传）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    # 校验文件类型
    content_type = (voice_file.content_type or "").lower()
    if content_type not in _VOICE_CLONE_TYPES:
        return await _render_detail(
            request, slug,
            error="请上传音频文件（WAV / MP3 / M4A，10-20 秒清晰人声）",
        )

    data = await voice_file.read()
    if not data:
        return await _render_detail(request, slug, error="上传文件为空")

    try:
        from app.services.voice_clone_service import (
            VoiceCloneError,
            VoiceCloneService,
        )

        # 1. 确定目标模型（使用当前 TTS 配置模型，复刻与合成必须一致）
        from app.llm.model_registry import resolve_model_config
        tts_cfg = resolve_model_config("tts")
        target_model = tts_cfg.model

        # 2. 生成音色前缀
        prefix = _profile_service.build_clone_prefix(profile.name)

        # 3. 调用阿里云声音复刻 API（音频 base64 直传，无需公网 URL）
        vc = VoiceCloneService()
        voice_id = await vc.create_voice(
            data, content_type, target_model, prefix
        )

        # 4. 更新 Profile
        await _profile_service.set_voice_clone(slug, voice_id, target_model)

    except VoiceCloneError as e:
        logger.error("声音复刻失败: %s", e, exc_info=True)
        await _profile_service.set_voice_clone_failed(slug)
        return await _render_detail(
            request, slug, error=f"声音复刻失败：{e}"
        )
    except ValueError as e:
        return await _render_detail(request, slug, error=str(e))
    except Exception as e:
        logger.error("声音复刻异常: %s", e, exc_info=True)
        await _profile_service.set_voice_clone_failed(slug)
        return await _render_detail(
            request, slug, error=f"声音复刻异常：{e}"
        )

    return await _render_detail(
        request, slug,
        success=f"声音复刻成功，音色 ID: {voice_id}",
    )


@profiles_router.post(
    "/profiles/{slug}/voice-clone/preview", response_class=HTMLResponse
)
async def voice_clone_preview(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """试听复刻音色效果。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")

    voice_id = profile.voice_id
    if not voice_id:
        return await _render_detail(
            request, slug, error="当前角色未配置音色，无法试听"
        )

    try:
        from app.services.voice_clone_service import VoiceCloneService

        vc = VoiceCloneService()
        audio_bytes = await vc.preview_voice(voice_id)

        # 将试听音频保存到媒体存储，返回可播放的 URL
        from app.services.media_storage import save_audio_from_bytes
        preview_url = await save_audio_from_bytes(audio_bytes, "audio/mpeg")

    except Exception as e:
        logger.error("试听音色失败: %s", e, exc_info=True)
        return await _render_detail(
            request, slug, error=f"试听失败：{e}"
        )

    return await _render_detail(
        request, slug,
        voice_preview_url=preview_url,
        success="试听音频已生成，点击播放",
    )


@profiles_router.post(
    "/profiles/{slug}/voice-clone/clear", response_class=HTMLResponse
)
async def voice_clone_clear(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """清除声音复刻状态（不删除云端音色，仅解除本地关联）。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="角色不存在")
    await _profile_service.clear_voice_clone(slug)
    return await _render_detail(request, slug, success="声音复刻状态已清除")
