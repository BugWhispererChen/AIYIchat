"""OpenAI 兼容 LLM Provider：集成熔断器与流式背压控制。"""

import asyncio
import logging
from typing import AsyncIterator

from openai import AsyncOpenAI

from app.llm.base import LLMProvider, LLMUsage, ToolCall, ToolCompletion
from app.llm.circuit_breaker import CircuitBreakerOpen, get_circuit_breaker

logger = logging.getLogger(__name__)

# 流式背压控制：内部缓冲队列大小
_STREAM_BUFFER_SIZE = 100


class OpenAICompatibleProvider(LLMProvider):
    """
    OpenAI兼容的LLM提供者。

    通过配置 base_url 即可切换不同的大模型后端：
    - DeepSeek: https://api.deepseek.com/v1
    - 通义千问: https://dashscope.aliyuncs.com/compatible-mode/v1
    - 文心一言: 对应的兼容端点
    - 本地模型(Ollama): http://localhost:11434/v1
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 30,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

        # 混合推理模型（Qwen3 / DeepSeek 系列）默认先吐 reasoning chunk，实测会把
        # token 预算吃满（长上下文下正文 0 字）并拖慢首包，对话场景无需思考链。
        # enable_thinking 是百炼系端点（DashScope / MaaS 工作空间）的私有参数，
        # 其它 OpenAI 兼容后端（含 DeepSeek 官方 API）不认识它，只在百炼系端点下发。
        is_bailian_endpoint = (
            "dashscope" in self._base_url or "maas.aliyuncs.com" in self._base_url
        )
        self._extra_body: dict | None = (
            {"enable_thinking": False}
            if is_bailian_endpoint
            and self._model.lower().startswith(("qwen3", "deepseek"))
            else None
        )

        self._client = AsyncOpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=float(self._timeout),
            # 异步消息场景快速降级优先：超时后立即失败走兜底，
            # 避免 SDK 默认重试导致 45s+ 假死等待
            max_retries=0,
        )
        # 熔断器：按任务类型隔离（chat/summary/focus 等）
        self._circuit_breaker = get_circuit_breaker("chat")

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
        response_format: dict | None = None,
        web_search: bool = False,
    ) -> str:
        """发送消息列表到LLM，返回完整回复文本（带熔断保护）。"""
        text, _usage = await self.complete_with_usage(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            response_format=response_format,
            web_search=web_search,
        )
        return text

    async def complete_with_usage(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
        response_format: dict | None = None,
        web_search: bool = False,
        presence_penalty: float | None = None,
    ) -> tuple[str, LLMUsage]:
        """与 complete 相同，额外返回本次调用的 token usage（带熔断保护）。

        用于按量计费：调用方以 usage.total 为结算依据；部分后端不返回
        usage 时回退为零值。
        presence_penalty: 采样层防重复参数（0~2），正值鼓励新话题/新表达。
        """
        try:
            return await self._circuit_breaker.call(
                self._complete_internal,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                response_format=response_format,
                web_search=web_search,
                presence_penalty=presence_penalty,
            )
        except CircuitBreakerOpen as e:
            logger.warning("LLM call blocked by circuit breaker: %s", e)
            raise RuntimeError("LLM service temporarily unavailable") from e

    async def _complete_internal(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
        response_format: dict | None = None,
        web_search: bool = False,
        presence_penalty: float | None = None,
    ) -> tuple[str, LLMUsage]:
        """内部实现：实际调用 LLM API，返回 (回复文本, usage)。"""
        # 计算实际请求超时：优先使用调用方指定的 timeout，否则使用客户端默认值
        request_timeout = float(timeout) if timeout else float(self._timeout)
        logger.debug(
            "LLM request: model=%s, messages=%d, temp=%.2f, timeout=%.1fs",
            self._model, len(messages), temperature, request_timeout,
        )
        # 组装可选参数：仅在调用方传入 response_format 时才传递，避免影响不支持的后端
        extra: dict = {}
        if response_format is not None:
            extra["response_format"] = response_format
        # presence_penalty：采样层防重复，仅在显式传入时传递（避免影响不支持的后端）
        if presence_penalty is not None:
            extra["presence_penalty"] = presence_penalty
        extra_body = dict(self._extra_body) if self._extra_body else None
        # DashScope 兼容模式支持 enable_search 联网搜索；非 DashScope 后端静默忽略，
        # 不影响其他调用方（与 enable_thinking 等既有 extra_body 项合并）
        if web_search and "dashscope" in self._base_url:
            extra_body = {**(extra_body or {}), "enable_search": True}
        if extra_body:
            extra["extra_body"] = extra_body
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=request_timeout,
            **extra,
        )
        content = response.choices[0].message.content or "" if response.choices else ""
        logger.debug("LLM response: %d chars", len(content))
        return content, _parse_usage(getattr(response, "usage", None))

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        usage_sink: dict | None = None,
        presence_penalty: float | None = None,
    ) -> AsyncIterator[str]:
        """流式生成回复（带背压控制）。

        背压策略：内部维护缓冲队列，生产者（LLM 流）与消费者（SSE 推送）解耦；
        队列满时丢弃最旧 chunk 并记录 dropped_chunks 指标，防止慢消费者阻塞 LLM 连接。
        usage_sink 传入可变字典时，流结束后将 token usage 写入其中，
        供调用方按量结算；后端不支持/不返回 usage 时写入零值。
        presence_penalty: 采样层防重复参数（0~2），正值鼓励新话题/新表达。
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_STREAM_BUFFER_SIZE)
        dropped_count = 0

        async def _producer() -> None:
            """生产者：从 LLM 流读取并放入队列。"""
            nonlocal dropped_count
            try:
                stream = await self._client.chat.completions.create(  # type: ignore[call-overload]
                    model=self._model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    # 要求后端在流末尾返回 usage 统计块（不支持的后端会忽略）
                    stream_options={"include_usage": True},
                    **({"extra_body": self._extra_body} if self._extra_body else {}),
                    **({"presence_penalty": presence_penalty} if presence_penalty is not None else {}),
                )
                async for chunk in stream:
                    # 部分 OpenAI 兼容后端（如 DashScope/Qwen）会发送 choices 为空的块
                    # （仅含 usage 统计或心跳），直接取 [0] 会抩 IndexError 中断流式输出；
                    # 此类块恰好携带 usage 统计，抓取后写入 usage_sink
                    if not chunk.choices:
                        if usage_sink is not None and getattr(chunk, "usage", None):
                            usage = _parse_usage(chunk.usage)
                            usage_sink["prompt_tokens"] = usage.prompt_tokens
                            usage_sink["completion_tokens"] = (
                                usage.completion_tokens
                            )
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        try:
                            queue.put_nowait(delta.content)
                        except asyncio.QueueFull:
                            # 队列满：丢弃最旧 chunk，放入新 chunk
                            try:
                                queue.get_nowait()
                                dropped_count += 1
                                queue.put_nowait(delta.content)
                            except asyncio.QueueEmpty:
                                pass
            except Exception as e:
                logger.error("Stream producer error: %s", e)
            finally:
                # 发送结束标记
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    # 队列满时强制清理并放入结束标记
                    try:
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait(None)
                    except asyncio.QueueEmpty:
                        pass

        # 启动生产者任务
        producer_task = asyncio.create_task(_producer())

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:  # 结束标记
                    break
                yield chunk
        finally:
            # 清理：取消生产者任务并记录丢弃统计
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except asyncio.CancelledError:
                    pass
            if dropped_count > 0:
                logger.warning(
                    "Stream backpressure: dropped %d chunks due to slow consumer",
                    dropped_count,
                )

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ToolCompletion:
        """带工具调用的补全，返回正文与模型请求的工具调用列表。"""
        request_timeout = float(timeout) if timeout else float(self._timeout)
        response = await self._client.chat.completions.create(  # type: ignore[call-overload]
            model=self._model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=request_timeout,
            **({"extra_body": self._extra_body} if self._extra_body else {}),
        )
        msg = response.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments or "{}",
                )
            )
        return ToolCompletion(
            content=msg.content or "",
            tool_calls=tool_calls,
            usage=_parse_usage(getattr(response, "usage", None)),
        )


def _parse_usage(usage) -> LLMUsage:
    """将 API 返回的 usage 对象转为 LLMUsage；缺失/空值回退为零值。

    部分 OpenAI 兼容后端不返回 usage，此时返回零值由上层按策略处理。
    """
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )
