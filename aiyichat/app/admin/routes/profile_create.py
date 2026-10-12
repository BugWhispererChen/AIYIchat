"""Admin 自定义角色创建向导路由。

四步向导：LLM 生成角色信息（草稿入库）→ 定妆照设置 → 声音设置
（上传复刻 / 预置音色试听选择）→ 确认可见性转正。
路由仅做参数解析与模板渲染，业务逻辑复用 ProfileService /
VoiceCloneService / ImageGenService / TTSService。
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _account_repo, _profile_repo, _render
from app.admin.routes.profiles import REF_PORTRAIT_PROMPT
from app.models.profile import PromptProfileInDB, PromptProfileUpdate
from app.profiles.service import ProfileService

logger = logging.getLogger(__name__)

profile_create_router = APIRouter(tags=["admin"])
_profile_service = ProfileService()

# 声音复刻允许的音频 content_type（与角色详情页保持一致）
_VOICE_CLONE_TYPES = frozenset({
    "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3",
    "audio/mp4", "audio/x-m4a", "audio/m4a",
})


async def _get_draft(slug: str) -> PromptProfileInDB:
    """获取草稿角色，不存在或非草稿时抛 404。"""
    profile = await _profile_repo.find_by_slug(slug)
    if not profile or not profile.is_draft:
        raise HTTPException(status_code=404, detail="草稿角色不存在")
    return profile


def _render_step(
    request: Request, profile: PromptProfileInDB, step: int, **extra
) -> HTMLResponse:
    """渲染向导页面（Step 2/3/4 共用骨架模板）。"""
    ctx: dict = {"profile": profile, "step": step, "active_page": "profiles"}
    ctx.update(extra)
    return _render(request, "profile_create_wizard.html", ctx)


# ── Step 1：基本信息（名称 + 概述）─────────────────────────


@profile_create_router.get("/profiles/create", response_class=HTMLResponse)
async def profile_create_page(
    request: Request, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """向导第一步：输入角色名称与概述。"""
    return _render(request, "profile_create.html", {"active_page": "profiles"})


@profile_create_router.post("/profiles/create/generate")
async def profile_create_generate(
    request: Request, _s: str = Depends(require_admin)
):
    """调用 LLM 生成角色信息并创建草稿，成功后进入定妆照步骤。"""
    form = await request.form()
    name = str(form.get("name", "")).strip()
    description = str(form.get("description", "")).strip()
    # 回复风格（非法值回退 standard）
    verbosity_raw = str(form.get("verbosity", "standard")).strip()
    verbosity = (
        verbosity_raw
        if verbosity_raw in ("concise", "standard", "verbose")
        else "standard"
    )
    if not name or not description:
        return _render(request, "profile_create.html", {
            "active_page": "profiles",
            "error": "角色名称与角色概述均为必填项",
            "name": name, "description": description,
            "verbosity": verbosity,
        })

    from app.profiles.creator import generate_profile

    try:
        draft = await generate_profile(name, description)
    except ValueError as e:
        return _render(request, "profile_create.html", {
            "active_page": "profiles",
            "error": str(e),
            "name": name, "description": description,
            "verbosity": verbosity,
        })

    created = await _profile_repo.create(draft)
    # 表单选择的回复风格写入草稿（LLM 生成不代填该字段）
    if verbosity != "standard":
        await _profile_repo.update(
            created.slug,
            PromptProfileUpdate(verbosity=verbosity),  # type: ignore[arg-type]
        )
    logger.info("自定义角色草稿已创建: slug=%s, name=%s", created.slug, name)
    return RedirectResponse(
        url=f"/admin/profiles/create/{created.slug}/image", status_code=303
    )


# ── Step 2：定妆照 ────────────────────────────────────────


@profile_create_router.get(
    "/profiles/create/{slug}/image", response_class=HTMLResponse
)
async def wizard_image(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """向导第二步：设置定妆照（上传 / AI 生成候选 / 跳过）。"""
    profile = await _get_draft(slug)
    return _render_step(request, profile, step=2)


@profile_create_router.post("/profiles/create/{slug}/image/upload")
async def wizard_image_upload(
    request: Request,
    slug: str,
    file: UploadFile = File(...),
    _s: str = Depends(require_admin),
):
    """上传图片并直接设为定妆照，随后进入声音步骤。"""
    profile = await _get_draft(slug)

    content_type = (file.content_type or "").lower()
    if not content_type.startswith("image/"):
        return _render_step(
            request, profile, step=2, error="请上传图片文件（png/jpg/webp）"
        )
    data = await file.read()
    if not data:
        return _render_step(request, profile, step=2, error="上传文件为空")

    from app.services.media_storage import save_image_from_bytes

    media_url = await save_image_from_bytes(data, content_type)
    await _profile_service.set_reference_image(slug, media_url)
    return RedirectResponse(
        url=f"/admin/profiles/create/{slug}/voice", status_code=303
    )


@profile_create_router.post(
    "/profiles/create/{slug}/image/generate", response_class=HTMLResponse
)
async def wizard_image_generate(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """用外貌描述生成定妆照候选（页面内确认后生效）。"""
    profile = await _get_draft(slug)

    appearance = (
        profile.identity.appearance or profile.identity.age_appearance
    ).strip()
    if not appearance:
        return _render_step(
            request, profile, step=2,
            error="角色缺少外貌描述，请在角色详情页补充后再生成，或直接上传照片",
        )

    try:
        from app.services.imagegen_service import ImageGenService
        from app.services.media_storage import save_image_from_bytes

        img_bytes = await ImageGenService().generate(
            REF_PORTRAIT_PROMPT + appearance
        )
        media_url = await save_image_from_bytes(img_bytes)
    except Exception as e:
        logger.error("向导定妆照候选生成失败: %s", e, exc_info=True)
        return _render_step(request, profile, step=2, error=f"生成失败：{e}")

    updated = await _get_draft(slug)
    return _render_step(
        request, updated, step=2,
        ref_candidate_url=media_url,
        success="候选定妆照已生成，请预览后确认或重新生成",
    )


@profile_create_router.post("/profiles/create/{slug}/image/confirm")
async def wizard_image_confirm(
    request: Request, slug: str, _s: str = Depends(require_admin)
):
    """确认候选图为定妆照，进入声音步骤。"""
    await _get_draft(slug)

    form = await request.form()
    media_url = str(form.get("media_url", "")).strip()
    if not media_url.startswith("/api/media/"):
        profile = await _get_draft(slug)
        return _render_step(request, profile, step=2, error="无效的候选图地址")
    await _profile_service.set_reference_image(slug, media_url)
    return RedirectResponse(
        url=f"/admin/profiles/create/{slug}/voice", status_code=303
    )


# ── Step 3：声音设置 ──────────────────────────────────────


async def _render_voice_step(
    request: Request, slug: str, **extra
) -> HTMLResponse:
    """渲染声音步骤（附带预置音色清单）。"""
    from app.services.tts_registry import PRESET_VOICE_OPTIONS

    profile = await _get_draft(slug)
    return _render_step(
        request, profile, step=3, preset_voices=PRESET_VOICE_OPTIONS, **extra
    )


@profile_create_router.get(
    "/profiles/create/{slug}/voice", response_class=HTMLResponse
)
async def wizard_voice(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """向导第三步：上传复刻 / 选择预置音色 / 跳过。"""
    return await _render_voice_step(request, slug)


@profile_create_router.post(
    "/profiles/create/{slug}/voice/clone", response_class=HTMLResponse
)
async def wizard_voice_clone(
    request: Request,
    slug: str,
    voice_file: UploadFile = File(...),
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """上传参考音频并调用阿里云声音复刻 API 创建专属音色。"""
    profile = await _get_draft(slug)

    content_type = (voice_file.content_type or "").lower()
    if content_type not in _VOICE_CLONE_TYPES:
        return await _render_voice_step(
            request, slug,
            error="请上传音频文件（WAV / MP3 / M4A，10-20 秒清晰人声）",
        )
    data = await voice_file.read()
    if not data:
        return await _render_voice_step(request, slug, error="上传文件为空")

    try:
        from app.llm.model_registry import resolve_model_config
        from app.services.voice_clone_service import (
            VoiceCloneError,
            VoiceCloneService,
        )

        # 复刻与合成必须绑定同一 TTS 模型
        tts_cfg = resolve_model_config("tts")
        target_model = tts_cfg.model
        prefix = _profile_service.build_clone_prefix(profile.name)
        voice_id = await VoiceCloneService().create_voice(
            data, content_type, target_model, prefix
        )
        await _profile_service.set_voice_clone(slug, voice_id, target_model)
    except VoiceCloneError as e:
        logger.error("向导声音复刻失败: %s", e, exc_info=True)
        await _profile_service.set_voice_clone_failed(slug)
        return await _render_voice_step(
            request, slug, error=f"声音复刻失败：{e}"
        )
    except ValueError as e:
        return await _render_voice_step(request, slug, error=str(e))
    except Exception as e:
        logger.error("向导声音复刻异常: %s", e, exc_info=True)
        await _profile_service.set_voice_clone_failed(slug)
        return await _render_voice_step(
            request, slug, error=f"声音复刻异常：{e}"
        )

    return await _render_voice_step(
        request, slug,
        success=f"声音复刻成功，音色 ID: {voice_id}，可试听确认后继续",
    )


@profile_create_router.post(
    "/profiles/create/{slug}/voice/clone-preview", response_class=HTMLResponse
)
async def wizard_voice_clone_preview(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """试听复刻音色效果。"""
    profile = await _get_draft(slug)
    if not profile.voice_id:
        return await _render_voice_step(
            request, slug, error="当前角色未配置音色，无法试听"
        )

    try:
        from app.services.media_storage import save_audio_from_bytes
        from app.services.voice_clone_service import VoiceCloneService

        audio_bytes = await VoiceCloneService().preview_voice(profile.voice_id)
        preview_url = await save_audio_from_bytes(audio_bytes, "audio/mpeg")
    except Exception as e:
        logger.error("向导试听复刻音色失败: %s", e, exc_info=True)
        return await _render_voice_step(request, slug, error=f"试听失败：{e}")

    return await _render_voice_step(
        request, slug,
        voice_preview_url=preview_url,
        success="试听音频已生成，点击播放",
    )


@profile_create_router.post(
    "/profiles/create/{slug}/voice/preset-preview", response_class=HTMLResponse
)
async def wizard_voice_preset_preview(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """试听预置音色：用所选音色合成固定文案。"""
    from app.services.tts_registry import PRESET_VOICE_OPTIONS

    valid_ids = {vid for vid, _, _ in PRESET_VOICE_OPTIONS}
    form = await request.form()
    voice_id = str(form.get("voice_id", "")).strip()
    if voice_id not in valid_ids:
        return await _render_voice_step(request, slug, error="无效的预置音色")

    try:
        from app.services.media_storage import save_audio_from_bytes
        from app.services.tts_service import TTSService

        audio_bytes = await TTSService().synthesize(
            "你好，我是你的AI伙伴，很高兴见到你。", voice_id=voice_id
        )
        preview_url = await save_audio_from_bytes(audio_bytes, "audio/mpeg")
    except Exception as e:
        logger.error("向导预置音色试听失败: %s", e, exc_info=True)
        return await _render_voice_step(request, slug, error=f"试听失败：{e}")

    return await _render_voice_step(
        request, slug,
        voice_preview_url=preview_url,
        preset_selected=voice_id,
        success="试听音频已生成，点击播放；满意后点击「使用该音色」",
    )


@profile_create_router.post(
    "/profiles/create/{slug}/voice/select", response_class=HTMLResponse
)
async def wizard_voice_select(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """选定预置音色：写入 voice_id 并清空复刻状态。"""
    from app.services.tts_registry import PRESET_VOICE_OPTIONS

    valid_ids = {vid for vid, _, _ in PRESET_VOICE_OPTIONS}
    form = await request.form()
    voice_id = str(form.get("voice_id", "")).strip()
    if voice_id not in valid_ids:
        return await _render_voice_step(request, slug, error="无效的预置音色")

    await _profile_repo.update(
        slug,
        PromptProfileUpdate(
            voice_id=voice_id,
            voice_clone_status="",
            voice_clone_target_model="",
        ),
    )
    return await _render_voice_step(
        request, slug,
        preset_selected=voice_id,
        success=f"已选定预置音色（{voice_id}），可进入下一步",
    )


# ── Step 4：确认与转正 ────────────────────────────────────


@profile_create_router.get(
    "/profiles/create/{slug}/confirm", response_class=HTMLResponse
)
async def wizard_confirm(
    request: Request, slug: str, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """向导第四步：浏览角色信息，选择可见性。"""
    profile = await _get_draft(slug)
    accounts = {a.id: a for a in await _account_repo.list_all()}
    return _render_step(request, profile, step=4, accounts=accounts)


@profile_create_router.post("/profiles/create/{slug}/confirm")
async def wizard_confirm_submit(
    request: Request, slug: str, _s: str = Depends(require_admin)
):
    """转正草稿：写入可见性、启用角色并清除草稿标记。"""
    profile = await _get_draft(slug)

    form = await request.form()
    visibility = str(form.get("visibility", "public")).strip()
    if visibility not in ("public", "private"):
        visibility = "public"
    account_id_raw = str(form.get("account_id", "")).strip()

    if visibility == "private" and not account_id_raw:
        accounts = {a.id: a for a in await _account_repo.list_all()}
        return _render_step(
            request, profile, step=4, accounts=accounts,
            error="私有角色必须选择所属账号",
        )

    await _profile_repo.update(
        slug,
        PromptProfileUpdate(
            is_draft=False,
            is_active=True,
            visibility=visibility,  # type: ignore[arg-type]
            account_id=account_id_raw if visibility == "private" else None,
        ),
    )
    logger.info("自定义角色创建完成: slug=%s, visibility=%s", slug, visibility)
    return RedirectResponse(url=f"/admin/profiles/{slug}", status_code=303)


@profile_create_router.post("/profiles/create/{slug}/cancel")
async def wizard_cancel(
    request: Request, slug: str, _s: str = Depends(require_admin)
):
    """取消创建：删除草稿角色（软删除），返回角色列表。"""
    await _get_draft(slug)
    await _profile_repo.delete(slug)
    logger.info("自定义角色草稿已取消: slug=%s", slug)
    return RedirectResponse(url="/admin/profiles", status_code=303)
