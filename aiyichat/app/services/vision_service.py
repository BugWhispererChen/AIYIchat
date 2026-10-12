"""Vision 图片理解服务 -- 阿里云百炼 Qwen-VL (OpenAI兼容)。"""

import logging

from openai import AsyncOpenAI

from app.llm.model_registry import resolve_model_config

logger = logging.getLogger(__name__)


class VisionService:
    """多模态图片理解：图片URL -> 一句话描述。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model: str = ""

    def _ensure_client(self) -> tuple[AsyncOpenAI, str]:
        """按 vision 功能引用的基础配置构造客户端与模型名（首次惰性创建）。"""
        if self._client is None:
            cfg = resolve_model_config("vision")
            self._client = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                timeout=30.0,
            )
            self._model = cfg.model
        return self._client, self._model

    async def describe_image(self, image_url: str) -> str:
        """
        调用 Qwen-VL 描述图片内容。

        Args:
            image_url: 图片的可访问 URL

        Returns:
            图片描述文本，失败返回空字符串
        """
        return await self.describe_image_with_hint(image_url, is_likely_sticker=False)

    async def describe_image_with_hint(
        self, image_url: str, is_likely_sticker: bool = False
    ) -> str:
        """
        调用 Qwen-VL 描述图片内容，支持表情包预判提示。

        Args:
            image_url: 图片的可访问 URL
            is_likely_sticker: 是否疑似表情包（小尺寸图片），使用更强的专用提示词

        Returns:
            图片描述文本，失败返回空字符串
        """
        try:
            client, model = self._ensure_client()
        except RuntimeError as e:
            logger.warning("Vision 未正确配置: %s", e)
            return ""

        if is_likely_sticker:
            prompt_text = (
                "这是一张表情包/贴纸，请按格式输出："
                "[表情包] 含义：<它表达的意思和情绪>。"
                "只需输出含义描述，不要输出其他内容。"
            )
        else:
            prompt_text = (
                "如果这是一张表情包或贴纸，请按格式输出："
                "[表情包] 含义：<它表达的意思和情绪>；"
                "否则，请用一句简洁的话描述这张图片的"
                "内容和情感氛围。"
            )

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                            {
                                "type": "text",
                                "text": prompt_text,
                            },
                        ],
                    }
                ],
                max_tokens=200,
                temperature=0.3,
            )
            desc = response.choices[0].message.content or ""
            logger.info("Vision 识别成功: %s", desc[:80])
            return desc.strip()
        except Exception as e:
            logger.error("Vision 识别失败: %s", e)
            return ""


_vision_service: VisionService | None = None


def get_vision_service() -> VisionService:
    """获取全局 VisionService 单例。"""
    global _vision_service
    if _vision_service is None:
        _vision_service = VisionService()
    return _vision_service


def reset_vision_service() -> None:
    """重置单例（运行时切换配置或测试时使用）。"""
    global _vision_service
    _vision_service = None
