"""后台路由必须逐个挂 require_admin——按 AST 判定，不靠人记。

为什么用 AST 而不是 grep 或 `inspect.getsource` 找关键字：
挂载写法有三种合法形态，任一都会被文本匹配漏掉或误判——
1) `_s: str = Depends(require_admin)`（默认值）；
2) `_s: Annotated[str, Depends(require_admin)]`（注解）；
3) 子路由器构造时 `APIRouter(dependencies=[Depends(require_admin)])`。
漏判的后果不是测试变红，而是**一个未鉴权的后台写接口被当成正常代码上线**。
"""

import ast
from pathlib import Path
from typing import Iterator

import pytest

_APP_DIR = Path(__file__).resolve().parents[2] / "app"
_ADMIN_ROUTES = _APP_DIR / "admin" / "routes"
_HTTP_DECOS = {"get", "post", "put", "delete", "patch", "websocket", "route"}


def _depends_targets(call: ast.Call) -> set[str]:
    """`Depends(x)` / `Depends(a.b)` 里被注入对象的名字。"""
    out: set[str] = set()
    for arg in call.args + [kw.value for kw in call.keywords]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                out.add(sub.attr)
    return out


def _is_depends(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        getattr(node.func, "attr", "") == "Depends"
        or getattr(node.func, "id", "") == "Depends"
    )


def _router_level_deps(tree: ast.Module) -> dict[str, set[str]]:
    """模块级 `xxx_router = APIRouter(dependencies=[...])` 声明的依赖名。"""
    out: dict[str, set[str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name):
            continue
        names: set[str] = set()
        for kw in stmt.value.keywords:
            if kw.arg != "dependencies":
                continue
            for sub in ast.walk(kw.value):
                if _is_depends(sub):
                    names |= _depends_targets(sub)
        out[target.id] = names
    return out


def _route_decorator_routers(func: ast.AST) -> list[str]:
    """返回 `@xxx_router.get(...)` 里的路由器名。"""
    routers: list[str] = []
    for deco in getattr(func, "decorator_list", []):
        call = deco if isinstance(deco, ast.Call) else None
        node = call.func if call else deco
        if isinstance(node, ast.Attribute) and node.attr in _HTTP_DECOS:
            routers.append(getattr(node.value, "attr", "") or getattr(node.value, "id", ""))
    return routers


def unguarded_admin_handlers(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    router_deps = _router_level_deps(tree)
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        routers = _route_decorator_routers(node)
        if not routers:
            continue
        if any("require_admin" in router_deps.get(r, set()) for r in routers):
            continue
        guarded = False
        for default in list(node.args.defaults) + [
            d for d in node.args.kw_defaults if d is not None
        ]:
            if _is_depends(default) and "require_admin" in _depends_targets(default):
                guarded = True
        annotations = [a.annotation for group in (
            node.args.posonlyargs, node.args.args, node.args.kwonlyargs
        ) for a in group]
        for ann in annotations:
            for sub in ast.walk(ann):
                if _is_depends(sub) and "require_admin" in _depends_targets(sub):
                    guarded = True
        if not guarded:
            missing.append(f"{path.name}:{node.lineno} {node.name}()")
    return missing


def _admin_route_files() -> Iterator[Path]:
    yield from sorted(_ADMIN_ROUTES.rglob("*.py"))


@pytest.mark.parametrize(
    "path", list(_admin_route_files()), ids=lambda p: str(p.relative_to(_APP_DIR))
)
def test_admin_handlers_require_admin(path: Path) -> None:
    offenders = unguarded_admin_handlers(path)
    assert not offenders, "这些后台处理函数没有任何鉴权依赖：\n" + "\n".join(offenders)


def test_handler_count_is_not_zero() -> None:
    """护栏不能对着空集合自我安慰：后台路由文件必须确实扫到了处理函数。"""
    total = 0
    for path in _admin_route_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and _route_decorator_routers(node)
        )
    assert total > 80, f"只扫到 {total} 个后台处理函数，扫描范围可能失效"


def test_guard_actually_fires(tmp_path: Path) -> None:
    probe = tmp_path / "probe_router.py"
    probe.write_text(
        "from fastapi import APIRouter, Depends\n"
        "from app.admin.auth import require_admin\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/leak')\n"
        "async def leak():\n"
        "    return {}\n\n"
        "@router.get('/ok')\n"
        "async def ok(_s: str = Depends(require_admin)):\n"
        "    return {}\n",
        encoding="utf-8",
    )
    offenders = unguarded_admin_handlers(probe)
    assert any("leak()" in item for item in offenders), offenders
    assert not any("ok()" in item for item in offenders), offenders
