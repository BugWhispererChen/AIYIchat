import asyncio
import hmac
import logging
import math
import secrets
import threading
import time

from fastapi import Cookie, HTTPException, Request, Response, status

from app.config import settings
from app.utils.password_hashing import (
    hash_password,
    is_hashed as _is_hashed,
    is_insecure_admin_password,
    verify_password as _verify_hash,
)

logger = logging.getLogger(__name__)

# 内存 session 存储: {token: expiry_timestamp}
_sessions: dict[str, float] = {}

# 后台登录失败计数：{用户名: (连续失败次数, 锁定截止的 monotonic 时间)}
# 与 C 端凭据的锁定相互独立；进程重启即清空，单实例部署下足够。
_admin_failures: dict[str, tuple[int, float]] = {}
_admin_failure_lock = threading.Lock()

# 密码哈希实现已抽取到 app.utils.password_hashing（与 C 端网页凭据共用），
# 此处以别名 re-export 保持 `from app.admin.auth import hash_password` 等既有引用不断链。
__all__ = [
    "admin_lock_remaining",
    "admin_password_is_configured",
    "create_session",
    "destroy_session",
    "hash_password",
    "is_admin_session",
    "register_admin_failure",
    "require_admin",
    "reset_admin_failures",
    "reset_admin_guards",
    "reset_all_sessions",
    "verify_credentials",
]


def admin_password_is_configured() -> bool:
    """后台是否配了一个能用的口令。

    空值与随源码公开的模板值（`admin123` / `your-admin-password`）都算"没配"：
    后者一旦被放行，等于把后台白送给任何读过 README 的人。
    """
    return not is_insecure_admin_password(settings.admin_password)


def _cleanup_expired() -> None:
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        _sessions.pop(t, None)


def admin_lock_remaining(username: str) -> int:
    """返回该用户名剩余的锁定秒数；未锁定返回 0。"""
    now = time.monotonic()
    with _admin_failure_lock:
        _count, locked_until = _admin_failures.get(username, (0, 0.0))
        if locked_until > now:
            return max(1, math.ceil(locked_until - now))
        # 锁定到期后清零重新计数；仅有失败次数（尚未锁定）时不能清掉，
        # 否则每次登录前查询都会把累计次数重置为 0。
        if locked_until:
            _admin_failures.pop(username, None)
    return 0


def register_admin_failure(username: str) -> int:
    """记录一次登录失败；达到阈值时锁定并返回剩余秒数，否则返回 0。"""
    now = time.monotonic()
    max_fail = max(1, settings.admin_login_max_fail)
    lock_seconds = max(1, settings.admin_login_lockout_minutes) * 60
    with _admin_failure_lock:
        count, locked_until = _admin_failures.get(username, (0, 0.0))
        if locked_until > now:
            return max(1, math.ceil(locked_until - now))
        count += 1
        if count >= max_fail:
            locked_until = now + lock_seconds
            _admin_failures[username] = (0, locked_until)
            return lock_seconds
        _admin_failures[username] = (count, 0.0)
    return 0


def reset_admin_failures(username: str) -> None:
    """登录成功后清空该用户名的失败计数。"""
    with _admin_failure_lock:
        _admin_failures.pop(username, None)


def reset_admin_guards() -> None:
    """清空全部后台登录防护状态（供测试与运维手动解封）。"""
    with _admin_failure_lock:
        _admin_failures.clear()


def create_session(response: Response) -> str:
    _cleanup_expired()
    token = secrets.token_urlsafe(32)
    expiry = time.time() + settings.admin_session_hours * 3600
    _sessions[token] = expiry
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
        max_age=settings.admin_session_hours * 3600,
    )
    return token


async def _migrate_plaintext_to_hash(plaintext: str) -> None:
    """明文密码登录成功后透明迁移：哈希化回写 DB 并就地更新运行时配置。"""
    try:
        from app.db.repositories.app_settings_repo import AppSettingsRepository

        hashed = await asyncio.to_thread(hash_password, plaintext)
        await AppSettingsRepository().upsert({"admin_password": hashed})
        settings.admin_password = hashed
        logger.info("Admin password migrated from plaintext to PBKDF2 hash")
    except Exception as e:
        # 迁移失败不影响本次登录，下次登录会重试
        logger.warning("Failed to migrate admin password to hash: %s", e)


async def verify_credentials(username: str, password: str) -> bool:
    """校验管理员凭据。

    admin_password 支持两种存储形态：
    - pbkdf2$ 前缀：按 PBKDF2 哈希验算；
    - 其他：按明文比对（兼容 .env/旧 DB），登录成功后自动迁移为哈希存储。

    用户名与密码都做恒定时间比较，且无论用户名是否匹配都执行密码校验，
    避免通过响应耗时枚举管理员用户名。PBKDF2 是纯 CPU 工作，放到线程池执行，
    防止 60 万次迭代阻塞事件循环。
    """
    stored = settings.admin_password
    if not stored:
        return False
    if is_insecure_admin_password(stored):
        # 口令仍是随源码公开的模板值：不比对，直接拒绝（调用方另有过期锁定计数）
        logger.error(
            "后台管理员口令仍为模板默认值，已拒绝登录：请设置 ADMIN_PASSWORD 后重启"
        )
        return False
    username_ok = hmac.compare_digest(
        username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    hashed = _is_hashed(stored)
    if hashed:
        password_ok = await asyncio.to_thread(_verify_hash, password, stored)
    else:
        password_ok = hmac.compare_digest(
            password.encode("utf-8"), stored.encode("utf-8")
        )
        if username_ok and password_ok:
            await _migrate_plaintext_to_hash(password)
    return username_ok and password_ok


def destroy_session(response: Response, token: str) -> None:
    _sessions.pop(token, None)
    # 删除必须与 create_session 同 path，否则浏览器继续回传旧 cookie
    response.delete_cookie(key="admin_session", path="/")


def is_admin_session(token: str) -> bool:
    """后台会话令牌当前是否有效（供健康检查这类"可选鉴权"场景使用）。

    与 `require_admin` 的区别是不抛 401：这些接口对匿名访问仍有合法响应，
    只是少了只该给管理员看的内容。
    """
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if expiry < time.time():
        _sessions.pop(token, None)
        return False
    return True


def reset_all_sessions() -> int:
    """作废全部后台会话（改密后调用：被窃取的旧令牌不该比新口令活得更久）。"""
    count = len(_sessions)
    _sessions.clear()
    return count


async def require_admin(request: Request, admin_session: str = Cookie(default="")) -> str:
    if not admin_password_is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin password not configured",
        )
    if not admin_session or admin_session not in _sessions:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if _sessions[admin_session] < time.time():
        _sessions.pop(admin_session, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return admin_session
