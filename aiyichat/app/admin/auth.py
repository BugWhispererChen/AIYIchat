import base64
import hashlib
import hmac
import logging
import secrets
import time

from fastapi import Cookie, HTTPException, Request, Response, status

from app.config import settings

logger = logging.getLogger(__name__)

# 内存 session 存储: {token: expiry_timestamp}
_sessions: dict[str, float] = {}

# PBKDF2 哈希参数：60 万迭代 + 16 字节随机盐（标准库实现，零新增依赖）
_PBKDF2_PREFIX = "pbkdf2"
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16


def _cleanup_expired() -> None:
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        _sessions.pop(t, None)


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


def hash_password(password: str) -> str:
    """生成 PBKDF2-HMAC-SHA256 哈希，格式：pbkdf2$<iterations>$<salt_b64>$<hash_b64>。"""
    salt = secrets.token_bytes(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "{}${}${}${}".format(
        _PBKDF2_PREFIX,
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def _verify_hash(password: str, stored: str) -> bool:
    """按 pbkdf2$ 格式验算密码，使用 compare_digest 防时序攻击。"""
    try:
        _prefix, iterations_s, salt_b64, hash_b64 = stored.split("$", 3)
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


def _is_hashed(value: str) -> bool:
    return value.startswith(_PBKDF2_PREFIX + "$")


async def _migrate_plaintext_to_hash(plaintext: str) -> None:
    """明文密码登录成功后透明迁移：哈希化回写 DB 并就地更新运行时配置。"""
    try:
        from app.db.repositories.app_settings_repo import AppSettingsRepository

        hashed = hash_password(plaintext)
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
    """
    stored = settings.admin_password
    if not stored:
        return False
    if username != settings.admin_username:
        return False
    if _is_hashed(stored):
        return _verify_hash(password, stored)
    # 明文模式：恒定时间比对后触发透明迁移
    if not hmac.compare_digest(password, stored):
        return False
    await _migrate_plaintext_to_hash(password)
    return True


def destroy_session(response: Response, token: str) -> None:
    _sessions.pop(token, None)
    response.delete_cookie(key="admin_session", path="/admin")


async def require_admin(request: Request, admin_session: str = Cookie(default="")) -> str:
    if not settings.admin_password:
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
