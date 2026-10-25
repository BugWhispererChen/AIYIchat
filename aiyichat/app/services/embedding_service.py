"""文本向量化服务 — 阿里云百炼 text-embedding (OpenAI兼容)。

用于长期记忆的语义检索与语义去重。当 embedding_enabled 关闭或
dashscope_api_key 未配置时，embed 返回空向量，调用方自动降级到规则逻辑，
因此本服务对现网零副作用。

降级可观测：请求失败时记录失败计数与最近错误，通过 status() 暴露给
仪表盘，避免配置错误被静默降级掩盖。

失败熔断：连续失败达窗口阈值（如免费额度耗尽返回 400 AllocationQuota）后暂停请求，
冷却期到点半开试探一次。注意 enabled 不因熔断而变 False——否则调用方永远不会
再发起请求，熔断器就失去进入半开、自动恢复的机会，变成永久锁死。
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from app.config import settings
from app.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpen,
)
from app.llm.model_registry import resolve_model_config

logger = logging.getLogger(__name__)

# 百炼 text-embedding 单批最大条数（v3/v4 兼容模式限制）
_BATCH_LIMIT = 10
# 单条文本截断长度，避免超出模型输入上限
_MAX_INPUT_CHARS = 2048
# 向量化 HTTP 客户端超时（秒）：检索链路另有 memory_recall_timeout_seconds 预算，
# 这里只兜住没有预算护栏的调用（管理页、批量回填），取略大于预算值避免抢跑
_CLIENT_TIMEOUT_SECONDS = 8.0

# 熔断参数：窗口 10 次、失败率 0.6（连续 5 次失败即触发，覆盖额度耗尽这种 100% 失败），
# 冷却 600s 后半开放行 1 次试探，成功即恢复。
# 慢成功阈值 5s：明显慢于检索预算（memory_recall_timeout_seconds，默认 3s）的成功
# 对实时链路已经没有价值，2026-10 测试服实测端点偶发单次挂起 80s+，只按异常计数的
# 熔断器抓不到这种"慢成功"。
# 必须是工厂而非模块级单例：CircuitBreaker 按引用持有 config，共享实例会让
# 任一服务改参数就波及全部服务（也包括同进程后续测试）。
def _new_breaker_config() -> CircuitBreakerConfig:
    """构造向量化端点专用的熔断参数。"""
    return CircuitBreakerConfig(
        failure_threshold=0.6,
        window_size=10,
        recovery_timeout=600.0,
        half_open_max_calls=1,
        success_threshold=1,
        slow_failure_seconds=5.0,
    )


class EmbeddingService:
    """文本向量化：文本 -> 向量。复用百炼兼容模式端点。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model: str = ""
        # 降级可观测：累计失败次数与最近一次错误
        self._fail_count: int = 0
        self._last_error: str = ""
        self._last_error_code: str = ""
        self._last_error_at: datetime | None = None
        # 端点级隔离：不复用 get_circuit_breaker("chat")，避免 LLM 故障连带关闭语义检索
        self._breaker = CircuitBreaker(_new_breaker_config())

    @staticmethod
    def _error_fingerprint(error: Exception) -> str:
        """从异常里抽结构化错误码（如 `400:AllocationQuota.FreeTierOnly`）。

        `last_error` 是整串文本，仪表盘按 120 字符截断展示，长报错恰好会把关键的错误码
        挤出可视区——而额度耗尽/限流这类问题**只靠错误码区分**。这里单独抽一份：
        openai 系异常取 `status_code` + body 内的 code（兼容扁平与 `error.code` 两种形态），
        其余异常回落到截断后的原文。
        """
        if not isinstance(error, APIStatusError):
            return str(error)[:200]
        status_code = getattr(error, "status_code", None)
        prefix = f"{status_code}:" if status_code is not None else ""
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            code = str(body.get("code") or "")
            if not code:
                inner = body.get("error")
                if isinstance(inner, dict):
                    code = str(inner.get("code") or "")
            if code:
                return f"{prefix}{code}"
        elif body is not None:
            return f"{prefix}{type(body).__name__}"
        return f"{prefix}{str(error)[:200]}"

    def _record_failure(self, error: Exception) -> None:
        """记录一次降级（失败计数 + 最近错误与错误码），供仪表盘展示。"""
        self._fail_count += 1
        self._last_error = str(error)
        self._last_error_code = self._error_fingerprint(error)
        self._last_error_at = datetime.now(timezone.utc)

    def status(self) -> dict:
        """返回服务健康状态（仪表盘降级告警用）。"""
        return {
            "enabled": self.enabled,
            "fail_count": self._fail_count,
            "last_error": self._last_error,
            "last_error_code": self._last_error_code,
            "last_error_at": self._last_error_at,
            "degraded": self.enabled and self._fail_count > 0,
            # 熔断状态：OPEN 表示已进入冷却期，recovery_in_seconds 到 0 时试探一次
            "breaker_state": self._breaker.state.value,
            "recovery_in_seconds": round(self._breaker.recovery_seconds),
        }

    def _ensure_client(self) -> tuple[AsyncOpenAI, str]:
        """按 embedding 功能引用的基础配置构造客户端与模型名（首次惰性创建）。"""
        if self._client is None:
            cfg = resolve_model_config("embedding")
            self._client = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                # 口径与 chat provider 一致：超时后立刻失败走降级，
                # 不用 SDK 默认重试（20s×3 + 退避会把一次检索拖到一分钟以上）
                timeout=_CLIENT_TIMEOUT_SECONDS,
                max_retries=0,
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

    async def _create(
        self, client: AsyncOpenAI, model: str, inputs: list[str]
    ) -> Any:
        """真正发起向量化请求（熔断器包裹的最小单元）。"""
        return await client.embeddings.create(model=model, input=inputs)

    async def _guarded_create(
        self, client: AsyncOpenAI, model: str, inputs: list[str]
    ) -> Any:
        """按开关决定是否经熔断器调用。

        熔断打开时抛 CircuitBreakerOpen，由调用方降级为空向量——关键收益是
        **不再发出 HTTP 请求**，而不是让失败静默堆积在日志里。
        """
        if not settings.embedding_breaker_enabled:
            return await self._create(client, model, inputs)
        return await self._breaker.call(self._create, client, model, inputs)

    async def embed(self, text: str) -> list[float]:
        """将单条文本转为向量，失败或未启用返回空列表。"""
        if not self.enabled or not text or not text.strip():
            return []
        try:
            client, model = self._ensure_client()
            resp = await self._guarded_create(
                client, model, [text[:_MAX_INPUT_CHARS]]
            )
            return list(resp.data[0].embedding)
        except CircuitBreakerOpen as e:
            # 冷却期内被熔断挡住：不是新故障，不再计入失败数（仪表盘已显示熔断状态）
            logger.debug("Embedding 熔断冷却中，跳过请求: %s", e)
            return []
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
                resp = await self._guarded_create(
                    client,
                    model,
                    [t[:_MAX_INPUT_CHARS] for _, t in chunk],
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
        except CircuitBreakerOpen as e:
            # 冷却期内被熔断挡住：保留已成功的那几批，其余走规则排序降级
            logger.debug("Embedding 熔断冷却中，跳过批量请求: %s", e)
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


async def embed_within_budget(
    service: EmbeddingService, text: str, *, what: str
) -> list[float]:
    """带超时预算的单条向量化：超时返回空向量，调用方按既有逻辑降级为规则排序。

    端点偶发长挂（2026-10 测试服实测 80s+）时，未设预算的调用会把整轮回复一起拖住；
    检索类向量化再慢也不该比回复本身更值钱，超时即降级。
    """
    if not service.enabled:
        return []
    try:
        return await asyncio.wait_for(
            service.embed(text), timeout=settings.memory_recall_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.warning(
            "向量化超时(%s, %.1fs)，本轮降级为规则排序",
            what, settings.memory_recall_timeout_seconds,
        )
        return []


async def embed_batch_within_budget(
    service: EmbeddingService, texts: list[str], *, what: str
) -> list[list[float]]:
    """批量版预算护栏：超时返回等长空向量，调用方的回填/检索按降级跳过。"""
    if not service.enabled:
        return [[] for _ in texts]
    try:
        return await asyncio.wait_for(
            service.embed_batch(texts), timeout=settings.memory_recall_timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.warning(
            "批量向量化超时(%s, %.1fs)，本轮跳过向量处理: %d 条",
            what, settings.memory_recall_timeout_seconds, len(texts),
        )
        return [[] for _ in texts]
