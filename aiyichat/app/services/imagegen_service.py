"""文生图与参考图编辑服务。

- 纯文生图：阿里云百炼 通义万相（现状链路，Prompt -> 写实照片）。
- 参考图编辑（人物一致性）：以角色定妆照为参考图 + 场景编辑指令生成，
  支持多提供商自动路由：DashScope（qwen-image-edit / wan2.x）、
  OpenAI 兼容 images/edits、Gemini 原生图像生成。
- 降级链：参考图编辑未启用/未配置/调用失败时，自动回退纯文生图
  （使用含外貌文字描述的 fallback prompt），保证功能不中断。
"""

import asyncio
import base64
import logging
from http import HTTPStatus

import httpx

from app.conversation.image_task import ImageTask
from app.llm.model_registry import ResolvedModelConfig, resolve_model_config

logger = logging.getLogger(__name__)

# 参考图编辑调用超时（秒），图像生成型接口普遍较慢
_EDIT_TIMEOUT = 120.0


def _detect_provider(cfg: ResolvedModelConfig) -> str:
    """按模型名与 base_url 识别参考图编辑提供商：dashscope / gemini / openai。"""
    model = (cfg.model or "").lower()
    base = (cfg.base_url or "").lower()
    if model.startswith("gemini") or "generativelanguage" in base:
        return "gemini"
    if not base or "dashscope" in base or "aliyuncs" in base:
        return "dashscope"
    return "openai"


def _detect_image_mime(data: bytes) -> str:
    """根据文件头（magic bytes）检测图片 MIME 类型。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ImageGenService:
    """文字生成图片：Prompt -> 写实风格照片 bytes。"""

    # 手机竖拍 3:4 比例，接近主流手机默认拍照分辨率
    DEFAULT_SIZE = "1080*1440"

    async def generate_for_task(self, task: ImageTask) -> bytes:
        """
        按结构化图片任务生成图片。

        本人出镜且有定妆照时优先走参考图编辑（保证人物一致性），
        失败或未启用时降级为纯文生图（fallback_prompt 含外貌文字描述）。
        """
        from app.config import settings

        if task.contains_self and task.ref_image_url and settings.imagegen_edit_enabled:
            try:
                return await self.generate_with_reference(
                    task.prompt, task.ref_image_url
                )
            except Exception as e:
                logger.warning(
                    "参考图编辑生成失败，降级为纯文生图: %s", e
                )
        return await self.generate(task.fallback_prompt or task.prompt)

    async def generate(
        self, prompt: str, size: str = ""
    ) -> bytes:
        """
        根据文本描述生成写实风格图片。

        Args:
            prompt: 图片描述 Prompt
            size: 图片尺寸 (默认 1080*1440 竖拍)

        Returns:
            图片二进制数据 (PNG)，失败抛出异常
        """
        if not size:
            size = self.DEFAULT_SIZE

        cfg = resolve_model_config("imagegen")  # 未正确配置时抛 RuntimeError

        # 兜底：确保写实风格前缀 + 手机视角约束
        if "realistic" not in prompt.lower():
            prompt = (
                "realistic photo, eye-level perspective, "
                "shot on handheld smartphone, "
                "natural lighting, high detail, "
                "no aerial view, no bird's eye view, " + prompt
            )
        else:
            # 即使有 realistic 前缀，也补充视角约束
            if "eye-level" not in prompt.lower() and "bird" not in prompt.lower():
                prompt = prompt + (
                    ", eye-level perspective, "
                    "no aerial view, no bird's eye view"
                )

        def _call() -> str:
            import dashscope
            from dashscope import ImageSynthesis, MultiModalConversation

            dashscope.api_key = cfg.api_key

            # 千问图像模型使用 MultiModalConversation 接口
            if cfg.model.startswith("qwen-image"):
                rsp = MultiModalConversation.call(
                    api_key=cfg.api_key,
                    model=cfg.model,
                    messages=[{
                        "role": "user",
                        "content": [{"text": prompt}],
                    }],
                    result_format="message",
                )
                if rsp.status_code == HTTPStatus.OK:
                    contents = rsp.output.choices[0].message.content
                    for item in contents:
                        if isinstance(item, dict) and item.get("image"):
                            return str(item["image"])
                raise RuntimeError(
                    f"ImageGen(Qwen) 生成失败: {rsp.status_code} {rsp.message}"
                )

            # 万相模型使用 ImageSynthesis 接口
            rsp = ImageSynthesis.call(
                model=cfg.model,
                prompt=prompt,
                size=size,
                n=1,
            )
            if rsp.status_code == HTTPStatus.OK:
                results = rsp.output.get("results", [])
                if results:
                    return results[0].get("url", "")
            raise RuntimeError(
                f"ImageGen 生成失败: {rsp.status_code} {rsp.message}"
            )

        # 先调 SDK 获取图片 URL
        image_url = await asyncio.to_thread(_call)
        if not image_url:
            raise RuntimeError("ImageGen 返回空 URL")

        content = await self._download(image_url)
        logger.info(
            "ImageGen 生成成功: %d bytes, prompt=%s",
            len(content),
            prompt[:50],
        )
        return content

    # ── 参考图编辑（人物一致性） ──────────────────────────

    async def generate_with_reference(
        self, prompt: str, ref_image_url: str
    ) -> bytes:
        """
        以角色定妆照为参考图，按场景描述生成人物一致的新图片。

        Args:
            prompt: 场景描述（英文，不含面部特征）
            ref_image_url: 定妆照地址（/api/media/{id} 或 http(s) URL）

        Returns:
            图片二进制数据，失败抛出异常
        """
        cfg = resolve_model_config("imagegen_edit")  # 未配置时抛 RuntimeError
        ref_bytes = await self._load_reference_bytes(ref_image_url)
        mime = _detect_image_mime(ref_bytes)

        # 编辑指令：身份交由参考图保证，明确要求保持面部/发型一致
        edit_prompt = (
            "Generate a new photo of the person in the reference image. "
            "Keep the face, facial features and hairstyle exactly the same "
            "as in the reference image. New scene: " + prompt
        )

        provider = _detect_provider(cfg)
        if provider == "dashscope":
            content = await self._edit_dashscope(cfg, edit_prompt, ref_bytes, mime)
        elif provider == "gemini":
            content = await self._edit_gemini(cfg, edit_prompt, ref_bytes, mime)
        else:
            content = await self._edit_openai(cfg, edit_prompt, ref_bytes, mime)

        logger.info(
            "ImageGenEdit 生成成功: provider=%s, model=%s, %d bytes, prompt=%s",
            provider, cfg.model, len(content), prompt[:50],
        )
        return content

    @staticmethod
    async def _load_reference_bytes(ref_image_url: str) -> bytes:
        """读取定妆照二进制：/api/media/{id} 走 MongoDB，http(s) 走下载。"""
        if ref_image_url.startswith("/api/media/"):
            from app.db.repositories.media_repo import MediaRepository

            media_id = ref_image_url.rsplit("/", 1)[-1]
            doc = await MediaRepository().find_by_id(media_id)
            if not doc or not doc.get("data"):
                raise RuntimeError(f"定妆照不存在: {ref_image_url}")
            return bytes(doc["data"])
        if ref_image_url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True
            ) as client:
                resp = await client.get(ref_image_url)
                resp.raise_for_status()
                return resp.content
        raise RuntimeError(f"不支持的定妆照地址: {ref_image_url}")

    @staticmethod
    async def _download(url: str) -> bytes:
        """下载生成结果图片。"""
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def _edit_dashscope(
        self, cfg: ResolvedModelConfig, prompt: str, ref_bytes: bytes, mime: str
    ) -> bytes:
        """DashScope 图像编辑（qwen-image-edit / wan2.x，多模态对话接口）。"""
        data_url = (
            f"data:{mime};base64,{base64.b64encode(ref_bytes).decode()}"
        )

        def _call() -> str:
            import dashscope
            from dashscope import MultiModalConversation

            dashscope.api_key = cfg.api_key

            rsp = MultiModalConversation.call(
                api_key=cfg.api_key,
                model=cfg.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"image": data_url},
                        {"text": prompt},
                    ],
                }],
                result_format="message",
                watermark=False,
            )
            if rsp.status_code == HTTPStatus.OK:
                contents = rsp.output.choices[0].message.content
                for item in contents:
                    if isinstance(item, dict) and item.get("image"):
                        return str(item["image"])
            raise RuntimeError(
                f"ImageGenEdit(DashScope) 失败: {rsp.status_code} {rsp.message}"
            )

        image_url = await asyncio.to_thread(_call)
        return await self._download(image_url)

    async def _edit_openai(
        self, cfg: ResolvedModelConfig, prompt: str, ref_bytes: bytes, mime: str
    ) -> bytes:
        """OpenAI 兼容 images/edits 端点（gpt-image-1 及各聚合网关）。"""
        base = cfg.base_url.rstrip("/")
        ext = mime.rsplit("/", 1)[-1]
        async with httpx.AsyncClient(timeout=_EDIT_TIMEOUT) as client:
            resp = await client.post(
                f"{base}/images/edits",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                files={"image": (f"reference.{ext}", ref_bytes, mime)},
                data={"model": cfg.model, "prompt": prompt},
            )
            resp.raise_for_status()
            payload = resp.json()
        items = payload.get("data") or []
        if not items:
            raise RuntimeError(f"ImageGenEdit(OpenAI) 返回为空: {payload}")
        first = items[0]
        if first.get("b64_json"):
            return base64.b64decode(first["b64_json"])
        if first.get("url"):
            return await self._download(first["url"])
        raise RuntimeError(f"ImageGenEdit(OpenAI) 无图片数据: {payload}")

    @staticmethod
    async def _edit_gemini(
        cfg: ResolvedModelConfig, prompt: str, ref_bytes: bytes, mime: str
    ) -> bytes:
        """Gemini 原生图像生成（generateContent + inline_data）。"""
        base = (
            cfg.base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(ref_bytes).decode(),
                    }},
                ],
            }],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        async with httpx.AsyncClient(timeout=_EDIT_TIMEOUT) as client:
            resp = await client.post(
                f"{base}/models/{cfg.model}:generateContent",
                headers={"x-goog-api-key": cfg.api_key},
                json=body,
            )
            resp.raise_for_status()
            payload = resp.json()
        for cand in payload.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data") or {}
                if inline.get("data"):
                    return base64.b64decode(inline["data"])
        raise RuntimeError(f"ImageGenEdit(Gemini) 无图片数据: {payload}")
