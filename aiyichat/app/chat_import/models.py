"""聊天记录导入相关的数据模型定义。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """解析后的单条聊天消息。"""

    timestamp: datetime
    sender: str
    content: str
    is_target: bool = False  # 是否为目标联系人的消息
    # 原 JSON 数组下标：画像和片段证据回指用；排序后仍保持稳定
    source_index: int = 0
    # text=可分析文本；media=图片/语音等占位，作为表达方式证据保留
    message_type: Literal["text", "media"] = "text"


class ParseResult(BaseModel):
    """JSON文件解析结果摘要。"""

    total_count: int = 0          # 总消息数
    target_count: int = 0         # 目标人消息数
    other_count: int = 0          # 对方消息数
    time_span: str = ""           # 时间跨度描述
    target_name: str = ""         # 目标联系人名称（从消息中推断）
    sample_messages: list[str] = Field(default_factory=list)  # 示例消息


class AnalysisTask(BaseModel):
    """LLM分析任务状态追踪。"""

    task_id: str
    status: Literal["parsing", "analyzing", "done", "error"] = "parsing"
    progress: int = 0             # 0-100 进度百分比
    progress_text: str = ""       # 进度描述文字
    result: dict | None = None    # 分析结果（PromptProfileCreate 的 dict 形式）
    error_message: str = ""
    file_path: str = ""           # 落盘的JSON文件路径
    parse_result: ParseResult | None = None  # 解析摘要
    # 分块覆盖率：任何一块失败都不能再静默显示“分析完成”
    chunk_total: int = 0
    chunk_succeeded: int = 0
    failed_chunk_indexes: list[int] = Field(default_factory=list)
    # 只有用户明确选择保留语料时才写入；pending 记录由 save 激活
    clone_dataset_id: str = ""
    clone_episode_count: int = 0
