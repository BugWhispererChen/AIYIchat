"""实体关系图服务的降级路径测试（kuzu 可选依赖缺失 / 连接故障 / MongoDB 兜底）。

为什么要单独钉这个文件：`entity_graph_service` 此前**零测试覆盖**（只有归档里的手工脚本），
而它是本轮 mypy 改动最集中的文件之一（kuzu 的可选导入与 Cypher 参数字典展开）。
没有回归网就无法证明"补类型窄化没改变运行行为"，故这里只测纯逻辑、不连真图库。

约定：kuzu 在本机 `.venv` 里根本没装（模块变量为 None），因此这些用例天然覆盖
"未安装可选依赖"的降级分支；连接故障分支用假模块注入触发，不依赖真实 kuzu。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import entity_graph_service as mod
from app.services.entity_graph_service import EntityGraphService


def _run(coro):
    return asyncio.run(coro)


class _BrokenKuzu:
    """假 kuzu 模块：Database 构造即抛错，用来驱动"连接初始化失败"分支。"""

    @staticmethod
    def Database(_path: str):
        raise RuntimeError("图文件被占用")

    @staticmethod
    def Connection(_db):  # pragma: no cover - Database 已先抛错，不会走到这里
        return SimpleNamespace()


@pytest.fixture
def _graph_switch_on(monkeypatch) -> None:
    """后台开关显式打开：本文件测的是"kuzu 可用但连不上"，不依赖残留配置值。"""
    monkeypatch.setattr(settings, "memory_graph_enabled", True)


def test_disabled_when_kuzu_missing(monkeypatch) -> None:
    """可选依赖没装（`kuzu` 为 None）时 enabled 必须为 False，一切走降级。"""
    monkeypatch.setattr(mod, "_KUZU_AVAILABLE", False)
    svc = EntityGraphService()
    assert svc.enabled is False
    assert svc._ensure_kuzu_conn() is None


def test_kuzu_connect_failure_marks_broken(monkeypatch, _graph_switch_on) -> None:
    """连接初始化失败要置 `_broken` 临时禁用，此后不再反复尝试、也不再报同类错。"""
    monkeypatch.setattr(mod, "_KUZU_AVAILABLE", True)
    monkeypatch.setattr(mod, "kuzu", _BrokenKuzu)
    svc = EntityGraphService()

    assert svc._ensure_kuzu_conn() is None
    assert svc._broken is True
    # _broken 之后 enabled 转 False，属性判断而非异常：调用方据此静默降级
    assert svc.enabled is False
    assert svc._ensure_kuzu_conn() is None


def test_sync_entity_is_silent_when_disabled(monkeypatch) -> None:
    """未启用时写入实体不得抛错（对话主链路会并发调用它）。"""
    monkeypatch.setattr(mod, "_KUZU_AVAILABLE", False)
    svc = EntityGraphService()
    entity = SimpleNamespace(name="小红", entity_type="person", aliases=[])

    async def body():
        assert await svc.sync_entity("svc-1", entity) is None

    _run(body())
    assert svc._broken is False


def test_query_related_uses_mongo_fallback(monkeypatch) -> None:
    """kuzu 不可用时多跳查询降级为 MongoDB 一跳展开，且只返回 valid 的关系。"""
    from app.db.repositories.service_memory_repo import ServiceMemoryRepository

    monkeypatch.setattr(mod, "_KUZU_AVAILABLE", False)
    memory = SimpleNamespace(entities=[
        SimpleNamespace(name="小红", relations=[
            SimpleNamespace(target="小明", relation="哥哥", valid=True),
            SimpleNamespace(target="小刚", relation="同学", valid=False),
        ])
    ])
    monkeypatch.setattr(
        ServiceMemoryRepository, "find_by_service",
        AsyncMock(return_value=memory),
    )
    svc = EntityGraphService()

    async def body():
        return await svc.query_related("svc-1", "小红", max_depth=2)

    paths = _run(body())
    assert len(paths) == 1
    assert paths[0]["path"] == ["小红", "小明"]
    assert paths[0]["depth"] == 1
    assert "哥哥" in paths[0]["description"]


def test_query_related_empty_when_entity_unknown(monkeypatch) -> None:
    """记忆里没有该实体时返回空列表，不抛 KeyError（注入侧按空结果跳过）。"""
    from app.db.repositories.service_memory_repo import ServiceMemoryRepository

    monkeypatch.setattr(mod, "_KUZU_AVAILABLE", False)
    monkeypatch.setattr(
        ServiceMemoryRepository, "find_by_service",
        AsyncMock(return_value=None),
    )
    svc = EntityGraphService()

    async def body():
        return await svc.query_related("svc-none", "不存在")

    assert _run(body()) == []
