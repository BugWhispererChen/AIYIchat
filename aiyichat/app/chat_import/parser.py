"""聊天记录JSON文件解析器。

负责将EXE工具导出的标准JSON文件解析为ChatMessage列表，
并将上传文件落盘保存到 data/chat_imports/ 目录。
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from app.chat_import.models import ChatMessage, ParseResult
from app.config import settings

logger = logging.getLogger(__name__)

# 上传文件落盘目录
CHAT_IMPORTS_DIR = Path(__file__).parent.parent.parent / "data" / "chat_imports"

# 媒体占位不是正文，但“爱发语音/爱用表情”本身是风格证据，因此保留为 media。
_MEDIA_PLACEHOLDERS = frozenset({
    "[图片]", "[语音]", "[视频]", "[表情]", "[文件]",
    "[红包]", "[转账]", "[位置]", "[名片]", "[链接]",
})
# 系统事件不属于任何一方，不能进入角色画像。
_SYSTEM_PLACEHOLDERS = frozenset({"[系统消息]", "[撤回了一条消息]"})


def _import_tz() -> timezone:
    """按导入时区配置构造 tzinfo（clamp 到 -12~14，避免非法配置抛异常）。

    导出文件中的时间戳是身份本地墙钟时间（无时区），必须按该偏移转 aware UTC，
    否则落库后被当作 UTC 再按对话时区渲染，整体错位 8 小时。
    """
    offset = max(-12, min(14, int(settings.chat_import_timezone_offset)))
    return timezone(timedelta(hours=offset))


def ensure_storage_dir() -> None:
    """确保落盘目录存在，并顺手清掉历史残留的导入文件。

    正常路径在解析完就删原文件；这里兜的是"进程在删除前崩溃"与旧版本留下的文件。
    """
    CHAT_IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sweep_leftover_files()


def save_uploaded_file(file_content: bytes, task_id: str) -> Path:
    """将上传的JSON文件落盘保存。

    Args:
        file_content: 文件二进制内容
        task_id: 任务ID，用作文件名

    Returns:
        落盘后的文件路径
    """
    ensure_storage_dir()
    file_path = CHAT_IMPORTS_DIR / f"{task_id}.json"
    file_path.write_bytes(file_content)
    logger.info("聊天记录文件已保存: %s (%d bytes)", file_path, len(file_content))
    return file_path


def delete_uploaded_file(file_path: Path) -> None:
    """解析结束后立即删除落盘的原始聊天记录。

    分析只需要内存里的 messages 列表；原文件留在盘上（Windows 安装包不会像容器那样
    随重建消失）等于长期保存对方与角色的完整聊天原文。
    """
    try:
        Path(file_path).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("聊天记录临时文件删除失败: %s", e)


def sweep_leftover_files(max_age_minutes: int = 30) -> int:
    """清理落盘目录里残留的导入文件（进程在解析前后崩溃时会留下）。

    只删超过 `max_age_minutes` 的：并发导入正在进行时不能误删别人刚写的文件。
    """
    if not CHAT_IMPORTS_DIR.exists():
        return 0
    cutoff = time.time() - max(1, max_age_minutes) * 60
    removed = 0
    for path in CHAT_IMPORTS_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("已清理残留的聊天记录导入文件 %d 个", removed)
    return removed


def parse_json_file(file_path: Path) -> tuple[list[ChatMessage], ParseResult]:
    """解析落盘的JSON文件为ChatMessage列表。

    JSON格式要求:
    [
        {"timestamp": "2024-03-15T14:23:01", "sender": "张三", "content": "...", "is_target": true},
        ...
    ]

    Args:
        file_path: JSON文件路径

    Returns:
        (消息列表, 解析摘要)

    Raises:
        ValueError: JSON格式不正确时抛出
    """
    raw_text = file_path.read_text(encoding="utf-8")
    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON解析失败: {e}") from e

    if not isinstance(raw_data, list):
        raise ValueError("JSON文件格式错误: 根元素必须是数组")

    if len(raw_data) == 0:
        raise ValueError("JSON文件为空，没有消息数据")

    # 解析每条消息
    messages: list[ChatMessage] = []
    for i, item in enumerate(raw_data):
        if not isinstance(item, dict):
            logger.warning("跳过第 %d 条非字典消息", i)
            continue
        try:
            msg = _parse_message(item, source_index=i)
            if msg is not None:
                messages.append(msg)
        except Exception as e:
            logger.warning("跳过第 %d 条消息(解析失败): %s", i, e)
            continue

    if len(messages) == 0:
        raise ValueError("解析后没有有效消息，请检查JSON文件格式")

    # 按时间排序；同时间戳用原顺序兜底，避免排序不稳定导致证据错位
    messages.sort(key=lambda m: (m.timestamp, m.source_index))

    # 生成解析摘要
    summary = _build_parse_result(messages)
    logger.info(
        "解析完成: 共 %d 条消息, 目标 %d 条, 时间跨度: %s",
        summary.total_count, summary.target_count, summary.time_span,
    )
    return messages, summary


def _parse_message(item: dict, source_index: int = 0) -> ChatMessage | None:
    """解析单条消息字典为 ChatMessage 对象。

    “嗯/好/哦”等单字回复是口癖和节奏证据，不再按长度丢弃；
    媒体占位保留为 media，避免把“爱发语音/表情”的表达习惯洗掉。
    """
    content = str(item.get("content", "")).strip()

    if not content or content in _SYSTEM_PLACEHOLDERS:
        return None

    message_type: Literal["text", "media"] = "text"
    for pattern in _MEDIA_PLACEHOLDERS:
        if content == pattern or content.startswith(pattern):
            message_type = "media"
            break

    # 解析时间戳
    timestamp_raw = item.get("timestamp", "")
    if isinstance(timestamp_raw, (int, float)):
        # Unix时间戳（秒）：本身就是 UTC 基准，直接转 aware UTC
        timestamp = datetime.fromtimestamp(timestamp_raw, tz=timezone.utc)
    elif isinstance(timestamp_raw, str):
        # ISO 8601 或常见格式：按导入时区转 aware UTC（修正存量 8 小时错位）
        timestamp = _parse_timestamp(timestamp_raw)
    else:
        return None

    sender = str(item.get("sender", "")).strip()
    if not sender:
        return None

    raw_target = item.get("is_target", False)
    if isinstance(raw_target, str):
        is_target = raw_target.strip().lower() in {"1", "true", "yes"}
    else:
        is_target = bool(raw_target)

    return ChatMessage(
        timestamp=timestamp,
        sender=sender,
        content=content,
        is_target=is_target,
        source_index=source_index,
        message_type=message_type,
    )


def _parse_timestamp(ts_str: str) -> datetime:
    """尝试多种格式解析时间戳字符串，按导入时区转 aware UTC。

    导出文件中的时间戳是身份本地墙钟时间（无时区），按 chat_import_timezone_offset
    解释为本地时间后转 UTC 落库，避免被当作 UTC 再渲染时整体错位 8 小时。

    Args:
        ts_str: 时间戳字符串

    Returns:
        UTC aware 的 datetime 对象

    Raises:
        ValueError: 无法解析时抛出
    """
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            naive = datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
        # 按导入时区解释为本地墙钟时间，再转 UTC
        return naive.replace(tzinfo=_import_tz()).astimezone(timezone.utc)
    raise ValueError(f"无法解析时间戳: {ts_str}")


def _build_parse_result(messages: list[ChatMessage]) -> ParseResult:
    """根据消息列表生成解析摘要。

    Args:
        messages: 已排序的消息列表

    Returns:
        ParseResult摘要
    """
    target_msgs = [m for m in messages if m.is_target]
    other_msgs = [m for m in messages if not m.is_target]

    # 推断目标联系人名称（取is_target=True中出现最多的sender）
    target_name = ""
    if target_msgs:
        sender_counts: dict[str, int] = {}
        for m in target_msgs:
            sender_counts[m.sender] = sender_counts.get(m.sender, 0) + 1
        target_name = max(sender_counts, key=sender_counts.get)  # type: ignore[arg-type]

    # 时间跨度
    time_span = ""
    if len(messages) >= 2:
        first_time = messages[0].timestamp.strftime("%Y-%m-%d")
        last_time = messages[-1].timestamp.strftime("%Y-%m-%d")
        days = (messages[-1].timestamp - messages[0].timestamp).days
        time_span = f"{first_time} ~ {last_time} (共{days}天)"

    # 示例消息（取目标人的前5条）
    sample_messages = [m.content[:100] for m in target_msgs[:5]]

    return ParseResult(
        total_count=len(messages),
        target_count=len(target_msgs),
        other_count=len(other_msgs),
        time_span=time_span,
        target_name=target_name,
        sample_messages=sample_messages,
    )
