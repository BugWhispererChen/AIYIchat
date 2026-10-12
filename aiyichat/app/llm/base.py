from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class ToolCall:
    """模型发起的单次工具调用。"""

    id: str
    name: str
    arguments: str  # JSON 字符串


@dataclass
class LLMUsage:
    """LLM API 返回的 token 用量统计（用于按量计费结算）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        """本轮调用消耗的总 token 数。"""
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolCompletion:
    """带工具调用的补全结果。"""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMProvider(ABC):
    """LLM提供者抽象接口"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """当前使用的模型名称"""
        ...

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
        response_format: dict | None = None,
        web_search: bool = False,
    ) -> str:
        """
        发送消息列表到LLM，返回完整回复文本。

        Args:
            messages: OpenAI格式的消息列表 [{"role": "system", "content": "..."}]
            temperature: 生成温度
            max_tokens: 最大生成token数
            timeout: 单次请求超时秒数，None 时使用客户端默认值
            response_format: 可选的响应格式（如 {"type": "json_object"}）
            web_search: 是否启用联网搜索（仅 DashScope 后端支持，其余静默忽略）

        Returns:
            LLM生成的回复文本
        """
        ...

    @abstractmethod
    def stream_complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        usage_sink: dict | None = None,
        presence_penalty: float | None = None,
    ) -> AsyncIterator[str]:
        """流式生成回复（Web 会话与测试场景使用）。

        usage_sink 传入可变字典时，流结束后将 token usage 写入其中，供按量计费结算。
        presence_penalty: 采样层防重复参数（0~2），正值鼓励新话题/新表达。
        """
        ...

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
        """与 complete 相同，额外返回本次调用的 token usage（用于按量计费）。

        默认未实现；支持的后端（如 OpenAI 兼容）在子类中重写。
        presence_penalty: 采样层防重复参数（0~2），正值鼓励新话题/新表达。
        """
        raise NotImplementedError("该 provider 不支持 usage 采集")

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ToolCompletion:
        """带工具调用的补全（Function Calling）。

        默认未实现；支持的后端（如 OpenAI 兼容）在子类中重写。
        """
        raise NotImplementedError("该 provider 不支持工具调用")
