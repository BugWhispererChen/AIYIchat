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
