"""文本向量化服务 — 阿里云百炼 text-embedding (OpenAI兼容)。

用于长期记忆的语义检索与语义去重。当 embedding_enabled 关闭或
dashscope_api_key 未配置时，embed 返回空向量，调用方自动降级到规则逻辑，
因此本服务对现网零副作用。

降级可观测：请求失败时记录失败计数与最近错误，通过 status() 暴露给
仪表盘，避免配置错误被静默降级掩盖。
"""

import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

from app.config import settings
from app.llm.model_registry import resolve_model_config

logger = logging.getLogger(__name__)

# 百炼 text-embedding 单批最大条数（v3/v4 兼容模式限制）
_BATCH_LIMIT = 10
# 单条文本截断长度，避免超出模型输入上限
_MAX_INPUT_CHARS = 2048


class EmbeddingService:
    """文本向量化：文本 -> 向量。复用百炼兼容模式端点。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model: str = ""
        # 降级可观测：累计失败次数与最近一次错误
        self._fail_count: int = 0
        self._last_error: str = ""
        self._last_error_at: datetime | None = None

    def _record_failure(self, error: Exception) -> None:
        """记录一次降级（失败计数 + 最近错误），供仪表盘展示。"""
        self._fail_count += 1
        self._last_error = str(error)
        self._last_error_at = datetime.now(timezone.utc)

    def status(self) -> dict:
        """返回服务健康状态（仪表盘降级告警用）。"""
        return {
            "enabled": self.enabled,
            "fail_count": self._fail_count,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "degraded": self.enabled and self._fail_count > 0,
        }

    def _ensure_client(self) -> tuple[AsyncOpenAI, str]:
        """按 embedding 功能引用的基础配置构造客户端与模型名（首次惰性创建）。"""
        if self._client is None:
            cfg = resolve_model_config("embedding")
            self._client = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                timeout=20.0,
            )
            self._model = cfg.model
        return self._client, self._model

    @property
    def enabled(self) -> bool:
        """是否启用向量化（开关开启且引用的基础配置已配密钥）。"""
        if not settings.embedding_enabled:
            return False
        try:
            cfg = resolve_model_config("embedding")
        except RuntimeError:
            return False
        return bool(cfg.api_key)

    async def embed(self, text: str) -> list[float]:
        """将单条文本转为向量，失败或未启用返回空列表。"""
        if not self.enabled or not text or not text.strip():
            return []
        try:
            client, model = self._ensure_client()
            resp = await client.embeddings.create(
                model=model,
                input=text[:_MAX_INPUT_CHARS],
            )
            return list(resp.data[0].embedding)
        except Exception as e:
            self._record_failure(e)
            logger.warning("Embedding 单条失败: %s", e)
            return []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，保持与输入等长；空文本或失败项返回空向量。"""
        if not self.enabled or not texts:
            return [[] for _ in texts]

        # 记录非空文本的原始下标，便于结果回填保序
        pending: list[tuple[int, str]] = [
            (i, t) for i, t in enumerate(texts) if t and t.strip()
        ]
        result: list[list[float]] = [[] for _ in texts]
        if not pending:
            return result

        try:
            client, model = self._ensure_client()
            # 分批请求，规避单批条数上限
            for start in range(0, len(pending), _BATCH_LIMIT):
                chunk = pending[start:start + _BATCH_LIMIT]
                resp = await client.embeddings.create(
                    model=model,
                    input=[t[:_MAX_INPUT_CHARS] for _, t in chunk],
                )
                # 百炼/OpenAI 返回的每个 data 项带 index 字段（标识其对应的输入位置），
                # API 不保证返回顺序与输入一致；按 index 回填避免向量错位。
                if len(resp.data) != len(chunk):
                    logger.warning(
                        "Embedding 返回数量(%d)与请求(%d)不一致",
                        len(resp.data), len(chunk),
                    )
                for item in resp.data:
                    idx = getattr(item, "index", None)
                    if idx is None or not (0 <= idx < len(chunk)):
                        continue
                    orig_idx = chunk[idx][0]
                    result[orig_idx] = list(item.embedding)
            return result
        except Exception as e:
            self._record_failure(e)
            logger.warning("批量 Embedding 失败: %s", e)
            return [[] for _ in texts]


_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """获取全局 EmbeddingService 单例。"""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


def reset_embedding_service() -> None:
    """重置单例（运行时切换配置或测试时使用）。"""
    global _embedding_service
    _embedding_service = None
