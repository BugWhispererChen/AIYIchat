"""密码哈希工具。

从 `app/admin/auth.py` 抽取，供管理后台与 C 端网页凭据（`web_identities`）共用同一套
PBKDF2-HMAC-SHA256 参数（60 万迭代 + 16 字节随机盐，标准库实现，零新增依赖）。
"""

import base64
import hashlib
import hmac
import secrets

_PBKDF2_PREFIX = "pbkdf2"
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16


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


def verify_password(password: str, stored: str) -> bool:
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


def is_hashed(value: str) -> bool:
    """判断存储值是否已是 pbkdf2$ 形态的哈希（否则按明文兼容处理）。"""
    return value.startswith(_PBKDF2_PREFIX + "$")
