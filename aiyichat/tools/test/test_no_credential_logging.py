"""静态护栏：禁止把第三方接口响应整体或凭据字段原样打进日志。

本轮清点了三处凭据泄露点位（QQ `Token response`、MongoDB 启动日志打整条 URI、
微信 getuploadurl 失败响应），修法全在写侧。**读侧兜底（全局 logging.Filter）刻意不做**：
事后正则遮罩会让新的泄漏点静默通过、给出"已防护"的错觉，所以这里用 AST 扫描把
"不许 dump 全量响应 / 不许裸打凭据字段"钉成契约，新增代码一犯就红。

口径与 `test_naming_conventions.py` 一致：只断言"不该出现的形态"，不设数量阈值。
"""

import ast
from pathlib import Path
from typing import Iterator

import pytest

_APP_DIR = Path(__file__).resolve().parents[2] / "app"

# logging 的各等级方法名
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# 敏感字段名片段：命中即要求它被包在掩码函数里（大小写不敏感）
_SENSITIVE_PARTS = (
    "access_token",
    "accesstoken",
    "refresh_token",
    "app_secret",
    "clientsecret",
    "bot_token",
    "mongodb_uri",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
)

# 允许把敏感值变成可打形式的掩码函数名
_MASK_FUNCS = frozenset(
    {"mask_secret", "mask_uri", "mask_ip", "brief_json", "_mask_api_key"}
)


def _python_files() -> Iterator[Path]:
    """遍历 app/ 下全部生产代码（跳过 __pycache__）。"""
    for path in sorted(_APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _is_logger_call(node: ast.Call) -> bool:
    """判断是否 `<某 logger>.info(...)` 形态的调用。"""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return False
    holder = func.value
    if isinstance(holder, ast.Name):
        return "log" in holder.id.lower()
    if isinstance(holder, ast.Attribute):
        return "log" in holder.attr.lower()
    # logging.getLogger("x").info(...)
    if isinstance(holder, ast.Call):
        inner = holder.func
        return isinstance(inner, ast.Attribute) and inner.attr == "getLogger"
    return False


def _is_mask_call(node: ast.Call) -> bool:
    """判断是否掩码函数调用（支持 `mod.mask_uri(...)` 写法）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _MASK_FUNCS
    if isinstance(func, ast.Attribute):
        return func.attr in _MASK_FUNCS
    return False


def _sensitive_refs(node: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """产出"数据引用"意义上的敏感名：属性访问、下标键、变量名。

    消息文案里的字符串常量不算（`"access_token 有效期 %d 秒"` 是合规的说明文字），
    只查 `settings.mongodb_uri`、`data["access_token"]`、`app_secret` 这类真实取值。
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            yield sub, sub.attr
        elif isinstance(sub, ast.Name):
            yield sub, sub.id
        elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
            if isinstance(sub.slice.value, str):
                yield sub, sub.slice.value
        elif isinstance(sub, ast.keyword):
            yield sub, sub.arg or ""


def _masked_ancestors(tree: ast.AST, parents: dict[ast.AST, ast.AST], target: ast.AST) -> bool:
    """敏感取值沿父链走到 logger 调用之前，是否被某个掩码函数包住。"""
    cur: ast.AST | None = target
    while cur is not None and cur is not tree:
        if isinstance(cur, ast.Call) and _is_mask_call(cur):
            return True
        cur = parents.get(cur)
    return False


def _build_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """为每个节点建立父指针，供"是否被掩码包住"的向上判断使用。"""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _violations(path: Path) -> list[str]:
    """返回单个文件里的违规描述列表。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _build_parents(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_logger_call(node):
            continue
        for arg in node.args:
            # 规则一：响应体整体 dump 进日志（内容不可枚举，一律禁止）
            for sub in ast.walk(arg):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "dumps"
                ):
                    found.append(
                        f"{path.name}:{sub.lineno} 日志参数里出现 json.dumps，"
                        f"请改为 list(data.keys()) 或 brief_json"
                    )
            # 规则二：敏感取值未被掩码函数包住
            for ref, name in _sensitive_refs(arg):
                lowered = name.lower()
                if not any(part in lowered for part in _SENSITIVE_PARTS):
                    continue
                if _masked_ancestors(tree, parents, ref):
                    continue
                found.append(
                    f"{path.name}:{getattr(ref, 'lineno', 0)} 日志直接引用凭据字段 `{name}`，"
                    f"请用 mask_secret/mask_uri 处理后再打"
                )
    return found


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(_APP_DIR)))
def test_no_raw_credential_in_logging(path: Path) -> None:
    """生产代码不许把凭据原样写进日志。"""
    violations = _violations(path)
    assert not violations, "凭据日志静态护栏命中：\n" + "\n".join(violations)


def test_known_leak_points_are_closed() -> None:
    """本轮修掉的三个点位不许悄悄改回去（连同其历史文案一起钉死）。"""
    cases = {
        "adapters/qq_websocket.py": "Token response:",
        "adapters/wechat_clawbot.py": "响应=%s",
        "db/mongo.py": ", settings.mongodb_uri)",
    }
    for rel, banned in cases.items():
        source = (_APP_DIR / rel).read_text(encoding="utf-8")
        assert banned not in source, f"{rel} 里又出现了凭据打印文案 `{banned}`"
