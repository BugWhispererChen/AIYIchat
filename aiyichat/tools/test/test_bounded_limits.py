"""请求可控的 `limit` 必须有界。

Mongo 的 `limit(0)` 语义是"不限条数"，负数则抛 `ValueError`：
调用方只要传 `?limit=0` 就能把整张表读进内存再序列化，传 `?limit=-1` 得到一个 500。
所以每个把 `limit` 透传给仓库的路由都必须先夹紧——既有行为用例，也有静态护栏。
"""

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ROUTE_DIRS = ["app/api", "app/admin/routes", "app/web"]


def _route_files() -> list[Path]:
    out: list[Path] = []
    for rel in _ROUTE_DIRS:
        out.extend(sorted((_ROOT / rel).rglob("*.py")))
    return out


def _unclamped_limit_functions(path: Path) -> list[str]:
    """带 `limit` 参数、把它透传给下游、却没做任何夹紧的函数。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if not ({"limit", "page_size", "size"} & {a.arg for a in node.args.args}):
            continue
        body = ast.unparse(node)
        passes_down = "limit=" in body or ".limit(" in body or "to_list(" in body
        clamped = "min(" in body or "max(" in body
        if passes_down and not clamped:
            offenders.append(f"{path.name}: {node.name}() 未夹紧透传给仓库的条数上限")
    return offenders


@pytest.mark.parametrize("path", _route_files(), ids=lambda p: str(p.relative_to(_ROOT)))
def test_route_limit_params_are_clamped(path: Path) -> None:
    offenders = _unclamped_limit_functions(path)
    assert not offenders, "\n".join(offenders)


def test_guard_actually_fires(tmp_path: Path) -> None:
    """护栏要能命中：未夹紧的路由函数应被判违规。"""
    probe = tmp_path / "probe_route.py"
    probe.write_text(
        "async def list_records(limit: int = 20):\n"
        "    return await repo.find_latest_by_identity('a', 'b', limit=limit)\n",
        encoding="utf-8",
    )
    assert _unclamped_limit_functions(probe), "未夹紧的写法应被判定为违规"

    probe.write_text(
        "async def list_records(limit: int = 20):\n"
        "    bounded = max(1, min(int(limit or 20), 200))\n"
        "    return await repo.find_latest_by_identity('a', 'b', limit=bounded)\n",
        encoding="utf-8",
    )
    assert not _unclamped_limit_functions(probe), "已夹紧的写法不该误报"


def test_proactive_records_endpoint_clamps_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实测 `?limit=` 的 0 / 负数 / 巨大值都落在 1..200。"""
    import app.api.proactive_api as proactive_api
    import app.db.repositories.proactive_repo as proactive_repo

    seen: list[int] = []

    async def fake_find(self: Any, identity_id: str, bot_id: str, *, limit: int) -> list:
        seen.append(limit)
        return []

    monkeypatch.setattr(
        proactive_repo.ProactiveRecordRepository, "find_latest_by_identity", fake_find
    )

    async def flow() -> None:
        for raw in (0, -5, 99999, 20):
            await proactive_api.list_proactive_records(
                identity_id="id-1", bot_id="bot-1", limit=raw
            )

    asyncio.run(flow())
    assert seen == [20, 1, 200, 20], seen
