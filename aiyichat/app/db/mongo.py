import logging
import time

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure
from pymongo.monitoring import CommandListener

from app.config import settings
from app.utils.redaction import mask_uri

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
        # URI 可含 user:pass，启动日志不得输出原文（详见 app/utils/redaction.py）
        logger.info("Connected to MongoDB: %s", mask_uri(settings.mongodb_uri))

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
    # 普通身份允许空绑定；仅非空 QQ/微信外部绑定要求 platform + platform_id 唯一
    await _safe_create_index(
        db.identities,
        [("platform", 1), ("platform_id", 1)],
        unique=True,
        name="identity_external_binding_unique",
        partialFilterExpression={"platform_id": {"$gt": ""}},
    )
    await _safe_create_index(db.identities, "account_id")
    # Bot：按 account_id 索引
    await _safe_create_index(db.bot_configs, "account_id")
    # 账号：按 id 唯一
    await _safe_create_index(db.accounts, "id", unique=True)
    # 对话服务：按 account_id、identity_id + bot_id 索引
    await _safe_create_index(db.services, "account_id")
    await _safe_create_index(db.services, [("identity_id", 1), ("bot_id", 1)])
    await _safe_create_index(
        db.services,
        "single_service_bot_id",
        unique=True,
        name="service_single_bot_unique",
        partialFilterExpression={"single_service_bot_id": {"$type": "string"}},
    )
    # 账号额度：按 account_id 唯一
    await _safe_create_index(db.account_quotas, "account_id", unique=True)
    # 角色：按 slug 唯一，按 visibility + account_id 索引
    await _safe_create_index(db.profiles, "slug", unique=True)
    await _safe_create_index(db.profiles, [("visibility", 1), ("account_id", 1)])
    # 聊天记录克隆：数据集/片段/反馈按账号与角色版本隔离；pending 语料 24h 自动过期
    await _safe_create_index(db.clone_datasets, "id", unique=True)
    await _safe_create_index(db.clone_datasets, "task_id")
    await _safe_create_index(
        db.clone_datasets, [("profile_id", 1), ("version", 1)]
    )
    await _safe_create_index(db.clone_datasets, "account_id")
    await _safe_create_index(db.clone_datasets, "expires_at", expireAfterSeconds=0)
    await _safe_create_index(db.clone_episodes, "id", unique=True)
    await _safe_create_index(db.clone_episodes, "dataset_id")
    await _safe_create_index(
        db.clone_episodes, "expires_at", expireAfterSeconds=0
    )
    await _safe_create_index(
        db.clone_episodes,
        [("profile_id", 1), ("clone_version", 1), ("account_id", 1), ("disabled", 1)],
    )
    await _safe_create_index(db.clone_feedback, "account_id")
    await _safe_create_index(db.clone_feedback, "profile_id")
    # 对话：按 identity_id + profile_id + service_id + status 索引，按 service_id 索引，按 created_at 索引
    await _safe_create_index(
        db.conversations,
        [("identity_id", 1), ("profile_id", 1), ("service_id", 1), ("status", 1)],
    )
    await _safe_create_index(db.conversations, "service_id")
    await _safe_create_index(db.conversations, "created_at")
    # 活跃会话唯一性：同一 (身份, 角色, 服务) 只允许一条 status=active 的会话。
    # find_or_create 的"先查再插"在多进程/多实例下必然有竞态，只有 DB 层能兜住——
    # 部分索引只管 active，历史归档会话不受影响。测试服实测 0 组重复，可安全建立。
    await _safe_create_index(
        db.conversations,
        [("identity_id", 1), ("profile_id", 1), ("service_id", 1)],
        unique=True,
        partialFilterExpression={"status": "active"},
    )
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
    # 媒体：public_id 是新的不可枚举能力令牌；旧文档没有该字段，故用稀疏唯一索引
    await _safe_create_index(
        db.media, "public_id", unique=True, sparse=True
    )
    # 记忆注入快照：按服务（含共享组 key）查询与清理
    await _safe_create_index(db.injection_snapshots, "service_id")
    # 内容安全事件：按类型+时间、创建时间索引（供审计分页/统计）
    await _safe_create_index(
        db.safety_events,
        [("event_type", 1), ("created_at", -1)],
    )
    await _safe_create_index(db.safety_events, "created_at")
    # C 端网页凭据：用户名唯一（注册原子占位），按账号与状态索引
    await _safe_create_index(db.web_credentials, "username", unique=True)
    await _safe_create_index(db.web_credentials, "account_id")
    await _safe_create_index(
        db.web_credentials,
        [("status", 1), ("created_at", -1)],
    )
    # C 端网页会话：token 唯一，credential_id 供并发上限查询，
    # expires_at 建 TTL 索引由 MongoDB 异步回收过期文档（expireAfterSeconds=0 即按字段值到期）
    await _safe_create_index(db.web_sessions, "token", unique=True)
    await _safe_create_index(db.web_sessions, "credential_id")
    await _safe_create_index(db.web_sessions, "expires_at", expireAfterSeconds=0)
    # 业务主键查询：各仓库 find_by_id 走的是文档里的 `id` 字段而不是 `_id`，
    # 没索引就是全表扫（后台列表页每行都要查一次）
    await _safe_create_index(db.identities, "id")
    await _safe_create_index(db.services, "id")
    await _safe_create_index(db.bot_configs, "id")
    await _safe_create_index(db.web_credentials, "id")
    # 服务归属与共享组：按 bot/角色筛服务、级联时按组 key 收数据
    await _safe_create_index(db.services, "bot_id")
    await _safe_create_index(db.services, "profile_id")
    await _safe_create_index(db.services, "share_group_id")
    # 表情包：content_hash 用于入库去重，group_id 用于按分组检索
    await _safe_create_index(db.stickers, "content_hash")
    await _safe_create_index(db.stickers, "group_id")
    # 长期记忆与注入快照：级联删除与"清记忆"按这两个字段定位范围
    await _safe_create_index(db.service_memories, "service_id")
    await _safe_create_index(db.injection_snapshots, "conversation_id")
    # 微信上下文 token：按 (bot, 对方) 取回，bot 删除时按 bot_id 反查清理
    await _safe_create_index(
        db.wechat_context_tokens,
        [("bot_id", 1), ("user_id", 1)],
    )
    # 会话列表按最后一条消息时间排序（后台与 C 端历史页）
    await _safe_create_index(db.conversations, "last_message_at")
