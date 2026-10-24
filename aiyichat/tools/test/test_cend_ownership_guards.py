"""C 端归属校验的静态护栏：收资源 ID 的处理函数必须把"调用方是谁"带进判定。

规则刻意区分两种合法形态（都是本轮逐条读代码确认过的）：
1) 函数体内直接比较归属（`profile.account_id != principal.account_id`、`_require_owned_*` 等）；
2) 把调用方的 `account_id` / `credential.id` / `identity.id` 作为参数交给服务层，
   由服务层去做归属判定并抛错（`web_channel_service.update_managed_bot` 等）。
只认第 1 种会把第 2 种全部误报；两种都不认的处理函数才是真可疑。

后台专用路由（整路由器挂 `require_admin`）不在本约束内——它按管理员权限而非用户归属判定。
"""

import ast
import re
from pathlib import Path
from typing import Iterator

import pytest

_APP_DIR = Path(__file__).resolve().parents[2] / "app"
_CEND_FILES = [Path("app/api"), Path("app/web")]
_ID_PARAM = re.compile(
    r"(profile_id|service_id|bot_id|conversation_id|session_id|credential_id|"
    r"identity_id|record_id|task_id|message_id|msg_id|conv_id)$"
)
_DIRECT_CHECKS = (
    "_require_owned", "_require_account_service", "get_managed_bot",
    "_conversation_readable", "!= principal", "== principal", ".account_id ==",
    ".account_id !=", ".identity_id !=", "not in allowed", "find_owned",
)
_DELEGATED_TOKENS = (
    "principal.identity.account_id", "principal.account_id",
    "principal.credential.id", "principal.identity.id", "principal.session.id",
)


def _is_http_handler(node: ast.AST) -> bool:
    if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
        return False
    return any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        and d.func.attr in {"get", "post", "put", "delete", "patch"}
        for d in node.decorator_list
    )


def _files() -> Iterator[Path]:
    for root_rel in _CEND_FILES:
        for path in sorted((_APP_DIR.parent / root_rel).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            # proactive_api 整个路由器 require_admin，按管理员权限判定，不属 C 端归属
            if "proactive_api" in path.name:
                continue
            yield path


def unowned_cend_handlers(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not _is_http_handler(node):
            continue
        id_params = sorted(a.arg for a in node.args.args if _ID_PARAM.search(a.arg))
        if not id_params:
            continue
        body = ast.unparse(node)
        direct = any(tok in body for tok in _DIRECT_CHECKS)
        delegated = any(tok in body for tok in _DELEGATED_TOKENS)
        if not (direct or delegated):
            offenders.append(
                f"{path.name}:{node.lineno} {node.name}() 收了 {id_params} 却没有归属判定"
            )
    return offenders


@pytest.mark.parametrize("path", list(_files()), ids=lambda p: str(p.relative_to(_APP_DIR.parent)))
def test_cend_handlers_enforce_ownership(path: Path) -> None:
    offenders = unowned_cend_handlers(path)
    assert not offenders, "C 端接口缺少归属校验：\n" + "\n".join(offenders)


def test_scan_scope_is_not_empty() -> None:
    """扫不到处理函数就等于护栏在空集上自high，必须先确认它看到了东西。"""
    total = 0
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += sum(
            1
            for node in ast.walk(tree)
            if _is_http_handler(node)
            and any(_ID_PARAM.search(a.arg) for a in node.args.args)
        )
    assert total >= 15, f"只扫到 {total} 个按 ID 取资源的 C 端处理函数，扫描范围可能失效"


def test_guard_actually_fires(tmp_path: Path) -> None:
    probe = tmp_path / "probe_api.py"
    probe.write_text(
        "from fastapi import APIRouter, Request\n"
        "router = APIRouter()\n\n"
        "@router.delete('/leak/{bot_id}')\n"
        "async def leak(request: Request, bot_id: str):\n"
        "    return await service.delete(bot_id)\n\n"
        "@router.delete('/ok/{bot_id}')\n"
        "async def ok(request: Request, bot_id: str):\n"
        "    principal = await _principal(request)\n"
        "    return await service.delete(principal.identity.account_id, bot_id)\n",
        encoding="utf-8",
    )
    offenders = unowned_cend_handlers(probe)
    assert any("leak(" in item for item in offenders), offenders
    assert not any("ok(" in item for item in offenders), offenders
