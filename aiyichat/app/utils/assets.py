"""静态资源版本戳工具：为零构建的前端提供带版本号的资源 URL。

前端不引入构建链，因此不做文件内容哈希；改用静态目录内所有文件的最新
修改时间作为版本戳。容器每次重建都会刷新 mtime，浏览器自然失效旧缓存，
无需人工维护版本号。
"""

from __future__ import annotations

import threading
from pathlib import Path

# 静态资源根目录：位于 app/ 内，随 Dockerfile 的 `COPY app/ ./app/` 与
# Windows 服务打包 spec 一起进镜像，无需额外配置打包规则
STATIC_DIR: Path = Path(__file__).resolve().parent.parent / "static"

# 对外挂载前缀，与 bootstrap.create_app() 中的 StaticFiles 挂载点保持一致
STATIC_URL_BASE: str = "/static"

_lock = threading.Lock()
_version_cache: str = ""


def _scan_version() -> str:
    """扫描静态目录，返回其中最新文件的 mtime（秒级整数字符串）。"""
    latest = 0
    if STATIC_DIR.is_dir():
        for path in STATIC_DIR.rglob("*"):
            if not path.is_file():
                continue
            modified = int(path.stat().st_mtime)
            if modified > latest:
                latest = modified
    return str(latest) if latest else ""


def asset_version() -> str:
    """取版本戳并缓存；静态文件有改动后由进程重启自动刷新。"""
    global _version_cache
    with _lock:
        if not _version_cache:
            _version_cache = _scan_version()
        return _version_cache


def reset_asset_version() -> None:
    """清空版本戳缓存，供测试在改动静态文件后重新取值。"""
    global _version_cache
    with _lock:
        _version_cache = ""


def asset(path: str) -> str:
    """拼出带版本戳的静态资源绝对 URL，模板用法：`asset('css/admin.css')`。"""
    relative = path.lstrip("/")
    base = f"{STATIC_URL_BASE}/{relative}"
    version = asset_version()
    return f"{base}?v={version}" if version else base
