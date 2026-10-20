import logging
import time

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure
from pymongo.monitoring import CommandListener

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None

# 慢查询阈值（毫秒）
_SLOW_QUERY_THRESHOLD_MS = 100


class SlowQueryListener(CommandListener):
    """MongoDB 命令监听器：记录慢查询。"""

    def __init__(self) -> None:
        """初始化监听器，创建命令开始时间存储字典。"""
        self._start_times: dict[int, float] = {}

    def started(self, event) -> None:
        """命令开始：记录起始时间。"""
        self._start_times[id(event)] = time.monotonic()

    def succeeded(self, event) -> None:
        """命令成功：检查是否慢查询。"""
        start_time = self._start_times.pop(id(event), None)
        if start_time is None:
            return
        duration_ms = (time.monotonic() - start_time) * 1000
        if duration_ms > _SLOW_QUERY_THRESHOLD_MS:
            logger.warning(
                "Slow MongoDB query: %s.%s took %.1fms",
                event.database_name,
                event.command_name,
                duration_ms,
                extra={
                    "duration_ms": duration_ms,
                    "command": event.command,
                },
            )

    def failed(self, event) -> None:
        """命令失败：记录错误。

        索引创建冲突属 _safe_create_index 有意容忍的预期情况
        （存量索引与请求规格同名但属性不同），降级为 debug 避免启动时误报 ERROR。
        """
        start_time = self._start_times.pop(id(event), None)
        if start_time is None:
            duration_ms = 0.0
        else:
            duration_ms = (time.monotonic() - start_time) * 1000
        if self._is_tolerated_index_conflict(event):
            logger.debug(
                "MongoDB index conflict ignored: %s.%s error=%s",
                event.database_name,
                event.command_name,
                event.failure,
            )
            return
        logger.error(
            "MongoDB command failed: %s.%s took %.1fms, error=%s",
            event.database_name,
            event.command_name,
            duration_ms,
            event.failure,
        )

    @staticmethod
    def _is_tolerated_index_conflict(event) -> bool:
        """判定是否为可容忍的索引规格冲突（code 86 / IndexKeySpecsConflict）。"""
        if event.command_name != "createIndexes":
            return False
        failure = event.failure
        if isinstance(failure, dict):
            if (
                failure.get("code") == 86
                or failure.get("codeName") == "IndexKeySpecsConflict"
            ):
                return True
            message = str(failure.get("errmsg", ""))
        else:
            message = str(failure or "")
        return "same name as the requested index" in message


async def connect_db() -> None:
    global _client, _db

    if settings.use_mock_db:
        # 使用 mongomock 内存数据库（无需真实 MongoDB）
        import mongomock_motor

        _client = mongomock_motor.AsyncMongoMockClient()
        _db = _client[settings.mongodb_database]
        logger.info("Using mongomock in-memory database")
    else:
        # 注册慢查询监听器
        from pymongo.monitoring import register

        register(SlowQueryListener())
        _client = AsyncIOMotorClient(settings.mongodb_uri)
        _db = _client[settings.mongodb_database]
        logger.info("Connected to MongoDB: %s", settings.mongodb_uri)

    # 确保 _db 已设置后再创建索引
    if _db is not None:
        await _ensure_indexes(_db)


async def close_db() -> None:
    global _client, _db
    if _client:
        _client.close()
    _client = None
    _db = None


def get_database() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialized. Call connect_db() first.")
    return _db


async def _safe_create_index(collection, keys, **kwargs) -> None:
    """安全创建索引，忽略已存在索引的冲突错误。"""
    try:
        await collection.create_index(keys, **kwargs)
    except OperationFailure as e:
        # 忽略索引已存在或同名索引冲突的错误
        if "same name as the requested index" in str(e) or "already exists" in str(e):
            logger.debug("Index already exists on %s: %s", collection.name, keys)
        else:
            raise


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """创建数据库索引。
    
    Args:
        db: 数据库对象
    """
    # 身份：按 platform + platform_id 唯一，按 account_id 索引
    await _safe_create_index(
        db.identities,
        [("platform", 1), ("platform_id", 1)],
        unique=True,
    )
    await _safe_create_index(db.identities, "account_id")
    # Bot：按 account_id 索引
    await _safe_create_index(db.bot_configs, "account_id")
    # 账号：按 id 唯一
    await _safe_create_index(db.accounts, "id", unique=True)
    # 对话服务：按 account_id、identity_id + bot_id 索引
    await _safe_create_index(db.services, "account_id")
    await _safe_create_index(db.services, [("identity_id", 1), ("bot_id", 1)])
    # 账号额度：按 account_id 唯一
    await _safe_create_index(db.account_quotas, "account_id", unique=True)
    # 角色：按 slug 唯一，按 visibility + account_id 索引
    await _safe_create_index(db.profiles, "slug", unique=True)
    await _safe_create_index(db.profiles, [("visibility", 1), ("account_id", 1)])
    # 对话：按 identity_id + profile_id + service_id + status 索引，按 service_id 索引，按 created_at 索引
    await _safe_create_index(
        db.conversations,
        [("identity_id", 1), ("profile_id", 1), ("service_id", 1), ("status", 1)],
    )
    await _safe_create_index(db.conversations, "service_id")
    await _safe_create_index(db.conversations, "created_at")
    # 消息：按 conversation_id + created_at + _id 索引（_id 为同时间戳的兜底排序键），按 role 索引
    await _safe_create_index(
        db.messages,
        [("conversation_id", 1), ("created_at", 1), ("_id", 1)],
    )
    await _safe_create_index(db.messages, "role")
    # 主动消息记录：按状态+发送时间、创建时间索引（供管理面板统计/分页）
    await _safe_create_index(
        db.proactive_records,
        [("status", 1), ("sent_at", -1)],
    )
    await _safe_create_index(db.proactive_records, "created_at")
    # 主动消息记录：按身份清理（C 端账号注销）
    await _safe_create_index(db.proactive_records, "identity_id")
    # 套餐订单台账：按账号查询与清理
    await _safe_create_index(db.orders, "account_id")
    # 内容安全事件：按账号查询与清理
    await _safe_create_index(db.safety_events, "account_id")
    # 记忆注入快照：按服务（含共享组 key）查询与清理
    await _safe_create_index(db.injection_snapshots, "service_id")
    # 内容安全事件：按类型+时间、创建时间索引（供审计分页/统计）
    await _safe_create_index(
        db.safety_events,
        [("event_type", 1), ("created_at", -1)],
    )
    await _safe_create_index(db.safety_events, "created_at")
    # C 端网页凭据：用户名唯一（注册原子占位），按 identity_id 与 status+created_at 索引
    await _safe_create_index(db.web_credentials, "username", unique=True)
    await _safe_create_index(db.web_credentials, "identity_id")
    await _safe_create_index(
        db.web_credentials,
        [("status", 1), ("created_at", -1)],
    )
    # C 端网页会话：token 唯一，credential_id 供并发上限查询，
    # expires_at 建 TTL 索引由 MongoDB 异步回收过期文档（expireAfterSeconds=0 即按字段值到期）
    await _safe_create_index(db.web_sessions, "token", unique=True)
    await _safe_create_index(db.web_sessions, "credential_id")
    await _safe_create_index(db.web_sessions, "expires_at", expireAfterSeconds=0)
