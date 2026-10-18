"""Admin 聊天记录导入与角色生成路由。

提供上传JSON文件、LLM分析、预览编辑、确认入库的完整流程。
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from app.admin.auth import require_admin
from app.admin.routes.common import _account_repo, _profile_repo, _render
from app.chat_import.analyzer import create_task, get_task, run_analysis, save_task
from app.chat_import.parser import parse_json_file, save_uploaded_file
from app.models.profile import (
    ProfileDialogueStrategy,
    ProfilePersona,
    ProfileLanguageStyle,
    ProfilePersonality,
    PromptProfileCreate,
)
from app.utils.tasks import spawn

logger = logging.getLogger(__name__)

chat_import_router = APIRouter(tags=["admin"])

# 上传文件大小限制（50MB）
MAX_FILE_SIZE = 50 * 1024 * 1024


@chat_import_router.get("/chat-import", response_class=HTMLResponse)
async def chat_import_page(
    request: Request, _s: str = Depends(require_admin),
) -> HTMLResponse:
    """聊天记录导入上传页面。"""
    return _render(request, "chat_import.html", {
        "active_page": "profiles",
    })


@chat_import_router.post("/chat-import/upload", response_class=HTMLResponse)
async def chat_import_upload(
    request: Request,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """接收上传的JSON文件，落盘后启动后台LLM分析任务。"""
    form = await request.form()
    upload_file: UploadFile | None = form.get("file")  # type: ignore[assignment]

    if upload_file is None or not upload_file.filename:
        return _render(request, "chat_import.html", {
            "active_page": "profiles",
            "error": "请选择要上传的JSON文件",
        })

    # 检查文件类型
    if not upload_file.filename.endswith(".json"):
        return _render(request, "chat_import.html", {
            "active_page": "profiles",
            "error": "仅支持JSON格式文件",
        })

    # 读取文件内容
    file_content = await upload_file.read()
    if len(file_content) > MAX_FILE_SIZE:
        return _render(request, "chat_import.html", {
            "active_page": "profiles",
            "error": f"文件过大，最大支持 {MAX_FILE_SIZE // 1024 // 1024}MB",
        })

    # 获取表单参数
    role_name = str(form.get("role_name", "")).strip()
    extra_description = str(form.get("extra_description", "")).strip()

    # 生成任务ID并落盘
    task_id = uuid.uuid4().hex[:12]
    try:
        file_path = save_uploaded_file(file_content, task_id)
    except Exception as e:
        logger.exception("文件保存失败")
        return _render(request, "chat_import.html", {
            "active_page": "profiles",
            "error": f"文件保存失败: {e}",
        })

    # 解析JSON文件
    try:
        messages, parse_result = parse_json_file(file_path)
    except ValueError as e:
        return _render(request, "chat_import.html", {
            "active_page": "profiles",
            "error": str(e),
        })

    # 创建分析任务（落库持久化，重启后仍可查进度）
    task = await create_task(task_id, str(file_path))
    task.parse_result = parse_result
    await save_task(task)

    # 启动后台异步分析（spawn 保存任务引用，防止被 GC 提前取消）
    spawn(
        run_analysis(task_id, messages, role_name, extra_description)
    )

    # 返回进度页面
    return _render(request, "chat_import.html", {
        "active_page": "profiles",
        "task_id": task_id,
        "parse_result": parse_result,
        "analyzing": True,
    })


@chat_import_router.get(
    "/chat-import/status/{task_id}", response_class=HTMLResponse,
)
async def chat_import_status(
    request: Request,
    task_id: str,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """查询分析进度（HTMX轮询端点）。"""
    task = await get_task(task_id)
    if task is None:
        return HTMLResponse("<p>任务不存在或已过期</p>")
    return _render(request, "partials/chat_import_progress.html", {"task": task})


@chat_import_router.get(
    "/chat-import/preview/{task_id}", response_class=HTMLResponse,
)
async def chat_import_preview(
    request: Request,
    task_id: str,
    _s: str = Depends(require_admin),
) -> HTMLResponse:
    """预览LLM分析生成的角色档案，支持编辑后入库。"""
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if task.status != "done" or task.result is None:
        raise HTTPException(status_code=400, detail="分析尚未完成")

    accounts = {a.id: a for a in await _account_repo.list_all()}
    return _render(request, "chat_import_preview.html", {
        "active_page": "profiles",
        "profile": task.result,
        "task_id": task_id,
        "accounts": accounts,
    })


@chat_import_router.post("/chat-import/save", response_class=HTMLResponse)
async def chat_import_save(
    request: Request, _s: str = Depends(require_admin),
) -> Response:
    """确认入库：从预览表单收集数据，创建角色档案。"""
    form = await request.form()

    def _lines(key: str) -> list[str]:
        """将多行文本按行分割并去空。"""
        val = form.get(key, "")
        return [line.strip() for line in str(val).split("\n") if line.strip()]

    # 解析 traits
    trait_keys = form.getlist("trait_key")
    trait_values = form.getlist("trait_value")
    traits: dict[str, float] = {}
    for k, v in zip(trait_keys, trait_values):
        k_str = str(k).strip()
        if k_str:
            try:
                traits[k_str] = round(float(str(v)), 2)
            except ValueError:
                traits[k_str] = 0.5

    # 解析可见范围
    visibility = str(form.get("visibility", "private")).strip()
    account_id_raw = str(form.get("account_id", "")).strip()
    account_id = account_id_raw if account_id_raw else None

    slug = str(form.get("slug", "")).strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug 不能为空")

    # 检查slug是否已存在
    existing = await _profile_repo.find_by_slug(slug)
    if existing:
        accounts = {a.id: a for a in await _account_repo.list_all()}
        task_id = str(form.get("task_id", ""))
        task = await get_task(task_id) if task_id else None
        return _render(request, "chat_import_preview.html", {
            "active_page": "profiles",
            "profile": task.result if task else {},
            "task_id": task_id,
            "accounts": accounts,
            "error": f"slug '{slug}' 已存在，请修改后重试",
        })

    # 构建 PromptProfileCreate 对象
    # 解析回复风格（非法值回退 standard）
    verbosity_raw = str(form.get("verbosity", "standard")).strip()
    verbosity = (
        verbosity_raw
        if verbosity_raw in ("concise", "standard", "verbose")
        else "standard"
    )
    profile = PromptProfileCreate(
        slug=slug,
        name=str(form.get("name", "")),
        source=str(form.get("source", "聊天记录分析")),
        category=str(form.get("category", "custom")),  # type: ignore[arg-type]
        is_active=form.get("is_active") == "on",
        visibility=visibility,  # type: ignore[arg-type]
        account_id=account_id,
        persona=ProfilePersona(
            background=str(form.get("persona_background", "")),
            era=str(form.get("persona_era", "")),
            age_appearance=str(form.get("persona_age_appearance", "")),
            core_values=_lines("persona_core_values"),
            relationships=[],
        ),
        language_style=ProfileLanguageStyle(
            vocabulary=_lines("ls_vocabulary"),
            sentence_patterns=_lines("ls_sentence_patterns"),
            tone=str(form.get("ls_tone", "")),
            emoji_habits=_lines("ls_emoji_habits"),
            forbidden_words=_lines("ls_forbidden_words"),
            example_utterances=_lines("ls_example_utterances"),
        ),
        personality=ProfilePersonality(
            mbti_hint=str(form.get("p_mbti_hint", "")),
            traits=traits,
            care_style=str(form.get("p_care_style", "")),
        ),
        dialogue_strategy=ProfileDialogueStrategy(
            greeting=str(form.get("ds_greeting", "")),
            on_negative_emotion=str(form.get("ds_on_negative_emotion", "")),
            on_positive_emotion=str(form.get("ds_on_positive_emotion", "")),
            on_silence=str(form.get("ds_on_silence", "")),
            boundaries=_lines("ds_boundaries"),
            safety_guardrails=str(form.get("ds_safety_guardrails", "")),
        ),
        meta_instructions=str(form.get("meta_instructions", "")),
        voice_id=str(form.get("voice_id", "")),
        verbosity=verbosity,  # type: ignore[arg-type]
    )

    # 入库
    await _profile_repo.create(profile)
    logger.info("聊天记录分析角色已入库: %s (%s)", profile.name, profile.slug)

    # 重定向到角色详情页
    return RedirectResponse(
        url=f"/admin/profiles/{slug}", status_code=303,
    )
