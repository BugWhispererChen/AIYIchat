"""LLM角色分析器。

实现两阶段分析流水线：
1. 分块分析：将聊天记录分块发送给LLM提取特征
2. 综合合并：将所有分块结果合并生成完整角色档案
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.chat_import.models import AnalysisTask, ChatMessage
from app.chat_import.prompts import CHUNK_ANALYSIS_PROMPT, MERGE_ANALYSIS_PROMPT
from app.config import settings
from app.db.repositories.analysis_task_repo import AnalysisTaskRepository
from app.llm.base import LLMProvider
from app.llm.provider_factory import get_llm_provider

logger = logging.getLogger(__name__)

# 每块消息数量上限改为运行时配置 settings.chat_import_chunk_size（默认100）


def _format_msg_timestamp(msg: ChatMessage) -> str:
    """格式化消息时间戳：跨年记录带年份，否则 [MM-DD HH:mm]。

    导入记录可能跨越多年（如 2023 与 2024 的聊天合并导入），无年份时模型无法
    区分去年与今年的同月同日；按消息自身年份与当前年份对比决定是否带年份。
    """
    ts = msg.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now_year = datetime.now(timezone.utc).year
    if ts.year != now_year:
        return ts.strftime("%Y-%m-%d %H:%M")
    return ts.strftime("%m-%d %H:%M")

# 内存读缓存（持久化以 MongoDB analysis_tasks 集合为准，重启后可恢复）
_tasks: dict[str, AnalysisTask] = {}

_task_repo = AnalysisTaskRepository()


async def get_task(task_id: str) -> AnalysisTask | None:
    """获取分析任务状态（优先内存缓存，未命中时从数据库恢复）。"""
    task = _tasks.get(task_id)
    if task is not None:
        return task
    try:
        task = await _task_repo.find_by_id(task_id)
    except Exception as e:
        logger.warning("分析任务读库失败: %s (%s)", task_id, e)
        return None
    if task is not None:
        _tasks[task_id] = task
    return task


async def create_task(task_id: str, file_path: str) -> AnalysisTask:
    """创建新的分析任务（写入缓存并落库）。"""
    task = AnalysisTask(task_id=task_id, file_path=file_path)
    _tasks[task_id] = task
    await save_task(task)
    return task


async def save_task(task: AnalysisTask) -> None:
    """持久化任务状态（失败仅告警，不阻断分析流程）。"""
    try:
        await _task_repo.upsert(task)
    except Exception as e:
        logger.warning("分析任务落库失败: %s (%s)", task.task_id, e)


def _get_analysis_provider() -> LLMProvider:
    """获取用于聊天记录分析任务的 LLM Provider（引用 chat_import 功能配置）。"""
    return get_llm_provider("chat_import")


async def run_analysis(
    task_id: str,
    messages: list[ChatMessage],
    role_name: str = "",
    extra_description: str = "",
) -> None:
    """执行完整的角色分析流水线（异步后台任务）。

    Args:
        task_id: 任务ID
        messages: 解析后的聊天消息列表
        role_name: 用户指定的角色名称（可选）
        extra_description: 用户补充的额外描述信息
    """
    task = await get_task(task_id)
    if task is None:
        logger.error("分析任务不存在: %s", task_id)
        return

    try:
        task.status = "analyzing"
        task.progress = 5
        task.progress_text = "准备分析..."
        await save_task(task)

        # 筛选目标联系人消息并推断名称
        target_messages = [m for m in messages if m.is_target]
        if not target_messages:
            raise ValueError("没有找到目标联系人的消息(is_target=true)")

        # 推断目标名称
        sender_counts: dict[str, int] = {}
        for m in target_messages:
            sender_counts[m.sender] = sender_counts.get(m.sender, 0) + 1
        target_name = max(sender_counts, key=sender_counts.get)  # type: ignore[arg-type]

        provider = _get_analysis_provider()

        # 阶段1：分块分析
        chunks = _split_into_chunks(messages, target_name)
        chunk_results: list[dict[str, Any]] = []

        for i, chunk in enumerate(chunks):
            task.progress = 10 + int(70 * i / max(len(chunks), 1))
            task.progress_text = f"分析第 {i + 1}/{len(chunks)} 块..."
            await save_task(task)

            result = await _analyze_chunk(provider, chunk, target_name)
            if result is not None:
                chunk_results.append(result)

        if not chunk_results:
            raise ValueError("所有分块分析均失败，无法生成角色档案")

        # 阶段2：综合合并
        task.progress = 85
        task.progress_text = "综合分析结果，生成角色档案..."
        await save_task(task)

        profile_dict = await _merge_analyses(
            provider, chunk_results, target_name, role_name, extra_description,
        )

        # 填充默认字段
        final_name = role_name if role_name else target_name
        profile_dict["name"] = final_name
        profile_dict["slug"] = _generate_slug(final_name)
        profile_dict["source"] = "聊天记录分析"
        profile_dict["category"] = "custom"
        profile_dict["visibility"] = "private"
        profile_dict["is_active"] = True

        task.result = profile_dict
        task.status = "done"
        task.progress = 100
        task.progress_text = "分析完成"
        await save_task(task)
        logger.info("角色分析完成: %s -> %s", task_id, final_name)

    except Exception as e:
        logger.exception("角色分析失败: %s", task_id)
        task.status = "error"
        task.error_message = str(e)
        task.progress_text = f"分析失败: {e}"
        await save_task(task)


def _split_into_chunks(
    messages: list[ChatMessage],
    target_name: str,
) -> list[str]:
    """将消息列表分块，每块格式化为对话文本。

    每块包含约 settings.chat_import_chunk_size 条目标消息，同时保留上下文中对方的消息。

    Args:
        messages: 完整消息列表
        target_name: 目标联系人名称

    Returns:
        格式化后的对话文本块列表
    """
    # 过滤并格式化消息
    chunk_size = max(1, settings.chat_import_chunk_size)
    formatted_lines: list[str] = []
    target_count = 0
    chunk_texts: list[str] = []

    for msg in messages:
        line = f"[{_format_msg_timestamp(msg)}] {msg.sender}: {msg.content}"
        formatted_lines.append(line)

        if msg.is_target:
            target_count += 1

        # 达到块大小时切分
        if target_count >= chunk_size:
            chunk_texts.append("\n".join(formatted_lines))
            formatted_lines = []
            target_count = 0

    # 处理剩余消息
    if formatted_lines:
        chunk_texts.append("\n".join(formatted_lines))

    # 如果消息太少不需要分块
    if not chunk_texts:
        chunk_texts = ["\n".join(
            f"[{_format_msg_timestamp(m)}] {m.sender}: {m.content}"
            for m in messages
        )]

    return chunk_texts


async def _analyze_chunk(
    provider: LLMProvider,
    chunk_text: str,
    target_name: str,
) -> dict[str, Any] | None:
    """分析单个消息块，提取目标联系人特征。

    Args:
        provider: LLM Provider实例
        chunk_text: 格式化的对话文本
        target_name: 目标联系人名称

    Returns:
        分析结果字典，失败返回None
    """
    system_prompt = CHUNK_ANALYSIS_PROMPT.format(target_name=target_name)

    for attempt in range(3):
        try:
            response = await provider.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": chunk_text},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            result = _parse_json_response(response)
            if result is not None:
                return result
            logger.warning("分块分析JSON解析失败(第%d次尝试)", attempt + 1)
        except Exception as e:
            logger.warning("分块分析LLM调用失败(第%d次尝试): %s", attempt + 1, e)

    return None


async def _merge_analyses(
    provider: LLMProvider,
    chunk_results: list[dict[str, Any]],
    target_name: str,
    role_name: str,
    extra_description: str,
) -> dict[str, Any]:
    """综合所有分块分析结果，生成完整角色档案。

    Args:
        provider: LLM Provider实例
        chunk_results: 所有分块的分析结果
        target_name: 目标联系人名称
        role_name: 用户指定的角色名称
        extra_description: 额外描述信息

    Returns:
        符合PromptProfileCreate结构的角色档案字典

    Raises:
        ValueError: 合并失败时抛出
    """
    # 构建分析摘要文本
    analyses_text = ""
    for i, result in enumerate(chunk_results):
        analyses_text += f"\n--- 第{i + 1}段分析结果 ---\n"
        analyses_text += json.dumps(result, ensure_ascii=False, indent=2)

    # 构建额外描述提示
    extra_hint = ""
    if extra_description:
        extra_hint = f"\n用户提供的额外描述信息（请融入角色设定中）:\n{extra_description}\n"

    display_name = role_name if role_name else target_name
    system_prompt = MERGE_ANALYSIS_PROMPT.format(extra_description=extra_hint)

    identity_content = (
        f"角色名称: {display_name}\n"
        f"原始聊天对象: {target_name}\n\n"
        f"以下是多段分析结果:\n{analyses_text}"
    )

    for attempt in range(3):
        try:
            response = await provider.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": identity_content},
                ],
                temperature=0.5,
                max_tokens=4000,
            )
            merged = _parse_json_response(response)
            if merged is not None:
                # 确保必要的嵌套结构存在
                return _ensure_profile_structure(merged)
            logger.warning("合并分析JSON解析失败(第%d次尝试)", attempt + 1)
        except Exception as e:
            logger.warning("合并分析LLM调用失败(第%d次尝试): %s", attempt + 1, e)

    raise ValueError("综合分析失败: LLM多次返回无法解析的结果")


def _parse_json_response(response: str) -> dict[str, Any] | None:
    """从LLM响应中提取JSON。

    处理可能包含markdown代码块标记的情况。

    Args:
        response: LLM原始响应文本

    Returns:
        解析后的字典，失败返回None
    """
    text = response.strip()

    # 清理markdown代码块标记
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 尝试提取JSON片段（在文字中间的JSON）
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


def _ensure_profile_structure(data: dict[str, Any]) -> dict[str, Any]:
    """确保角色档案字典包含所有必要的嵌套结构。

    Args:
        data: LLM生成的原始字典

    Returns:
        补充默认值后的完整字典
    """
    # 确保四个嵌套结构存在
    data.setdefault("persona", {})
    data.setdefault("language_style", {})
    data.setdefault("personality", {})
    data.setdefault("dialogue_strategy", {})

    # persona 默认值
    persona = data["persona"]
    persona.setdefault("background", "")
    persona.setdefault("era", "现代")
    persona.setdefault("age_appearance", "")
    persona.setdefault("appearance", "")
    persona.setdefault("core_values", [])
    persona.setdefault("relationships", [])

    # language_style 默认值
    ls = data["language_style"]
    ls.setdefault("vocabulary", [])
    ls.setdefault("sentence_patterns", [])
    ls.setdefault("tone", "")
    ls.setdefault("emoji_habits", [])
    ls.setdefault("forbidden_words", [])
    ls.setdefault("example_utterances", [])

    # personality 默认值
    personality = data["personality"]
    personality.setdefault("mbti_hint", "")
    personality.setdefault("traits", {})
    personality.setdefault("care_style", "")
    # 确保 traits 值为浮点数
    for k, v in personality["traits"].items():
        personality["traits"][k] = round(float(v), 2)

    # dialogue_strategy 默认值
    ds = data["dialogue_strategy"]
    ds.setdefault("greeting", "")
    ds.setdefault("on_negative_emotion", "")
    ds.setdefault("on_positive_emotion", "")
    ds.setdefault("on_silence", "")
    ds.setdefault("boundaries", [])
    ds.setdefault("safety_guardrails", "保持友善，不涉及敏感话题，不提供专业医疗/法律建议")

    # 其他顶层默认值（语速已改由 TTS 指令文案控制，无独立字段）
    data.setdefault("meta_instructions", "")
    data.setdefault("voice_id", "")

    return data


def _generate_slug(name: str) -> str:
    """根据角色名称生成slug标识符。

    使用名称的拼音或英文+随机后缀。

    Args:
        name: 角色名称

    Returns:
        slug字符串
    """
    # 简单方案：用名称的ASCII部分或拼音首字母+随机后缀
    safe_chars = re.sub(r"[^a-zA-Z0-9]", "", name)
    if not safe_chars:
        safe_chars = "chat-role"
    suffix = uuid.uuid4().hex[:6]
    return f"{safe_chars.lower()}-{suffix}"
