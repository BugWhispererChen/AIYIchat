"""聊天记录导入模块单元测试。

覆盖：
- parser.parse_json_file：正常解析、过滤规则、异常输入
- analyzer 任务持久化：创建落库、缓存命中、缓存未命中时从库恢复
- analyzer._split_into_chunks：分块大小走运行时配置
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.chat_import import analyzer
from app.chat_import.models import AnalysisTask, ChatMessage
from app.chat_import.parser import parse_json_file
from app.config import settings


def _write_json(tmp_path, data) -> "Path":  # noqa: F821
    file_path = tmp_path / "chat.json"
    file_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return file_path


class TestParseJsonFile:
    """JSON 解析器测试集。"""

    def test_parse_normal_messages(self, tmp_path) -> None:
        """正常消息解析：数量、目标推断、时间排序。"""
        file_path = _write_json(tmp_path, [
            {"timestamp": "2024-03-15T14:23:01", "sender": "张三",
             "content": "今天吃了火锅", "is_target": True},
            {"timestamp": "2024-03-15T14:22:00", "sender": "我",
             "content": "在干嘛呢", "is_target": False},
        ])
        messages, result = parse_json_file(file_path)
        assert result.total_count == 2
        assert result.target_count == 1
        assert result.target_name == "张三"
        # 按时间排序：早的在前
        assert messages[0].sender == "我"

    def test_keeps_short_and_media_messages(self, tmp_path) -> None:
        """媒体占位与单字回复都保留：前者是表达习惯，后者是口癖证据。"""
        file_path = _write_json(tmp_path, [
            {"timestamp": "2024-03-15T14:23:01", "sender": "张三",
             "content": "[图片]", "is_target": True},
            {"timestamp": "2024-03-15T14:23:02", "sender": "张三",
             "content": "嗯", "is_target": True},
            {"timestamp": "2024-03-15T14:23:03", "sender": "张三",
             "content": "这条是有效消息", "is_target": True},
        ])
        messages, result = parse_json_file(file_path)
        assert result.total_count == 3
        assert [m.content for m in messages] == ["[图片]", "嗯", "这条是有效消息"]
        assert messages[0].message_type == "media"
        assert messages[1].source_index == 1

    def test_invalid_json_raises(self, tmp_path) -> None:
        """非法 JSON → ValueError。"""
        file_path = tmp_path / "bad.json"
        file_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON解析失败"):
            parse_json_file(file_path)

    def test_empty_list_raises(self, tmp_path) -> None:
        """空数组 → ValueError。"""
        file_path = _write_json(tmp_path, [])
        with pytest.raises(ValueError, match="为空"):
            parse_json_file(file_path)


class TestTaskPersistence:
    """分析任务持久化测试集（Repository 全部 mock）。"""

    def setup_method(self) -> None:
        # 清空内存缓存，保证用例间隔离
        analyzer._tasks.clear()

    def test_create_task_persists(self) -> None:
        """创建任务时写入缓存并落库。"""
        with patch.object(analyzer, "_task_repo") as repo:
            repo.upsert = AsyncMock()
            task = asyncio.run(analyzer.create_task("t1", "/tmp/a.json"))
        assert task.task_id == "t1"
        assert analyzer._tasks["t1"] is task
        repo.upsert.assert_awaited_once()

    def test_get_task_hits_memory_cache(self) -> None:
        """缓存命中时不查库。"""
        cached = AnalysisTask(task_id="t2", file_path="x")
        analyzer._tasks["t2"] = cached
        with patch.object(analyzer, "_task_repo") as repo:
            repo.find_by_id = AsyncMock()
            task = asyncio.run(analyzer.get_task("t2"))
        assert task is cached
        repo.find_by_id.assert_not_awaited()

    def test_get_task_recovers_from_db(self) -> None:
        """缓存未命中时从数据库恢复（模拟容器重启场景）。"""
        stored = AnalysisTask(task_id="t3", file_path="y", status="done")
        with patch.object(analyzer, "_task_repo") as repo:
            repo.find_by_id = AsyncMock(return_value=stored)
            task = asyncio.run(analyzer.get_task("t3"))
        assert task is stored
        assert analyzer._tasks["t3"] is stored  # 恢复后回填缓存

    def test_get_task_missing_returns_none(self) -> None:
        """缓存与数据库都没有 → None。"""
        with patch.object(analyzer, "_task_repo") as repo:
            repo.find_by_id = AsyncMock(return_value=None)
            assert asyncio.run(analyzer.get_task("t4")) is None

    def test_save_task_failure_not_raises(self) -> None:
        """落库失败仅告警，不阻断分析流程。"""
        task = AnalysisTask(task_id="t5", file_path="z")
        with patch.object(analyzer, "_task_repo") as repo:
            repo.upsert = AsyncMock(side_effect=RuntimeError("db down"))
            asyncio.run(analyzer.save_task(task))  # 不应抛出


class TestSplitIntoChunks:
    """分块逻辑测试集（分块大小走运行时配置）。"""

    @staticmethod
    def _messages(count: int) -> list[ChatMessage]:
        return [
            ChatMessage(
                timestamp=datetime(2024, 3, 15, 14, 0, i % 60),
                sender="张三",
                content=f"消息{i}",
                is_target=True,
            )
            for i in range(count)
        ]

    def test_chunk_size_from_settings(self, monkeypatch) -> None:
        """chat_import_chunk_size=2 时，5 条目标消息切成 3 块。"""
        monkeypatch.setattr(settings, "chat_import_chunk_size", 2)
        chunks = analyzer._split_into_chunks(self._messages(5), "张三")
        assert len(chunks) == 3

    def test_small_input_single_chunk(self, monkeypatch) -> None:
        """消息数不足一块时返回单块。"""
        monkeypatch.setattr(settings, "chat_import_chunk_size", 100)
        chunks = analyzer._split_into_chunks(self._messages(3), "张三")
        assert len(chunks) == 1
        assert "消息0" in chunks[0]

class TestUploadedFileLifecycle:
    """上传的原始聊天记录解析完就该消失：盘上不该长期留完整聊天原文。"""

    def test_delete_uploaded_file_is_idempotent(self, tmp_path) -> None:
        from app.chat_import import parser as parser_module

        target = tmp_path / "abc123.json"
        target.write_text("{}", encoding="utf-8")
        parser_module.delete_uploaded_file(target)
        assert not target.exists()
        # 已不存在时不报错（重复调用、并发删除都要能吞掉）
        parser_module.delete_uploaded_file(target)

    def test_sweep_removes_only_stale_files(self, tmp_path, monkeypatch) -> None:
        import os
        import time as time_mod

        from app.chat_import import parser as parser_module

        monkeypatch.setattr(parser_module, "CHAT_IMPORTS_DIR", tmp_path)
        fresh = tmp_path / "fresh.json"
        fresh.write_text("{}", encoding="utf-8")
        stale = tmp_path / "stale.json"
        stale.write_text("{}", encoding="utf-8")
        old = time_mod.time() - 3600
        os.utime(stale, (old, old))

        assert parser_module.sweep_leftover_files(max_age_minutes=30) == 1
        assert fresh.exists()
        assert not stale.exists()

    def test_save_uploaded_file_sweeps_stale(self, tmp_path, monkeypatch) -> None:
        """落盘入口顺带清扫历史残留，不需要额外的启动钩子。"""
        import os
        import time as time_mod

        from app.chat_import import parser as parser_module

        monkeypatch.setattr(parser_module, "CHAT_IMPORTS_DIR", tmp_path)
        stale = tmp_path / "stale.json"
        stale.write_text("{}", encoding="utf-8")
        old = time_mod.time() - 7200
        os.utime(stale, (old, old))

        saved = parser_module.save_uploaded_file(b"[]", "task-1")
        assert saved.exists()
        assert not stale.exists()
