"""静态护栏：全仓禁用危险调用，并要求"外部词表进正则必须转义"。

背景：本仓库的提交审查是按 diff 走的，而开源初始提交（`16c7903`）里带进来的代码
只要后续没被改过，就永远不会出现在任何 diff 里。这类"从未被触碰的代码"需要
一次性全仓扫描——而扫描结论会随时间失效，所以把它钉成契约。

口径与 `test_no_credential_logging.py` 一致：只断言"不该出现的形态"，不设数量阈值。
"""

import ast
import re
from pathlib import Path
from typing import Iterator

import pytest

_APP_DIR = Path(__file__).resolve().parents[2] / "app"

# 名称(正则) → 说明
BANNED = {
    r"yaml\.load\((?![^)]*Safe)": "yaml.load 必须用 SafeLoader（或 safe_load）",
    r"\b(eval|exec)\s*\(": "禁止 eval/exec 动态执行代码",
    r"\bpickle\.(loads|load)\(|marshal\.loads": "禁止反序列化 pickle/marshal",
    r"os\.system\(|os\.popen\(": "禁止 os.system/os.popen 拼接命令执行",
    r"shell\s*=\s*True": "subprocess 不许 shell=True（参数含外部输入即成命令注入）",
    r"\$where": "Mongo 查询不许用 $where（等于把 JS 执行权交给查询条件）",
}


def _python_files() -> Iterator[Path]:
    for path in sorted(_APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(_APP_DIR)))
def test_no_dangerous_calls(path: Path) -> None:
    """禁用反序列化 / 动态执行 / shell 拼接 / Mongo `$where` 这类一等危险调用。"""
    offenders: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, reason in BANNED.items():
            if re.search(pattern, line):
                offenders.append(f"{path.name}:{lineno} {reason}：{stripped[:80]}")
    assert not offenders, "全仓危险调用护栏命中：\n" + "\n".join(offenders)


def _module_constants(tree: ast.Module) -> set[str]:
    """模块级全大写常量名：随仓库评审过的固定字面量，不属于"外部词表"。"""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    names.add(target.id)
    return names


def _regex_join_violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    consts = _module_constants(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "compile"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "re"):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if ".join(" not in segment:
            continue
        if "re.escape" in segment:
            continue
        # join 的直接参数是大写常量表（模块级、仓库内评审过的固定字面量）→ 允许不转义；
        # 只有来自配置/参数的词表才要求 re.escape
        joined = re.search(r"join\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", segment)
        if joined is not None:
            name = joined.group(1)
            if name in consts or re.fullmatch(r"[A-Z_][A-Z0-9_]{2,}", name):
                continue
        offenders.append(
            f"{path.name}:{node.lineno} 外部词表未 re.escape 就拼进正则：{segment[:80]}"
        )
    return offenders


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(_APP_DIR)))
def test_word_list_regex_is_escaped(path: Path) -> None:
    """词表进正则必须 `re.escape`。

    词表来自角色配置与安全规则（后台可改）：未转义的一个 `(` 就让整条聊天链路抛
    `re.error`，写成 `.*` 之类更是把 ReDoS 放进热路径。
    模块内全大写常量表（如 `_ROLE_BREAK_PATTERNS`）是仓库里评审过的固定字面量，不在本约束内。
    """
    assert not _regex_join_violations(path), "\n".join(_regex_join_violations(path))


def test_guard_actually_fires(tmp_path: Path) -> None:
    """护栏不能是永不发火的摆设。"""
    probe = tmp_path / "probe.py"
    probe.write_text("import yaml\n\nyaml.load(open('x.yml'))\n", encoding="utf-8")
    assert any(
        re.search(pattern, line)
        for line in probe.read_text(encoding="utf-8").splitlines()
        for pattern in BANNED
    )

    probe.write_text(
        "import re\n\n\ndef build(forbidden):\n"
        "    return re.compile('|'.join(forbidden))\n",
        encoding="utf-8",
    )
    assert _regex_join_violations(probe), "未转义的外部词表应被判违规"

    probe.write_text(
        "import re\n\n\ndef build(forbidden):\n"
        "    return re.compile('|'.join(re.escape(w) for w in forbidden))\n",
        encoding="utf-8",
    )
    assert not _regex_join_violations(probe), "已 re.escape 的写法不该误报"
