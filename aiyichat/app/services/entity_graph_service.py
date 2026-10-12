"""实体关系图服务：MongoDB 为主存储，Kuzu 嵌入式图数据库为可选增强层。

架构原则：
- MongoDB 是事实源（source of truth），所有写操作先落 MongoDB；
- Kuzu 是查询加速层，启用后实体/关系异步同步到图文件，多跳查询走 Cypher；
- Kuzu 未启用或异常时自动降级为 MongoDB 应用层递归（一跳展开）；
- Kuzu 为可选依赖（try/except ImportError），未安装时所有操作降级。
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.models.service_memory import MemoryEntity

logger = logging.getLogger(__name__)

# Kuzu 可选导入：未安装时 _KUZU_AVAILABLE=False，所有图操作降级
try:
    import kuzu

    _KUZU_AVAILABLE = True
except ImportError:
    kuzu = None  # type: ignore[assignment]
    _KUZU_AVAILABLE = False
    logger.info("kuzu 未安装，实体关系图服务将使用 MongoDB 降级模式")


class EntityGraphService:
    """实体关系图服务：统一抽象 MongoDB 与 Kuzu 的图操作。

    启用状态动态读取 settings.memory_graph_enabled，支持后台热切换；
    连接初始化/查询失败时置 _broken 临时禁用（避免反复报错），
    开关关闭再开启或调用 reset 后允许重试。
    """

    def __init__(self) -> None:
        self._kuzu_db: "kuzu.Database | None" = None
        self._kuzu_conn: "kuzu.Connection | None" = None
        self._broken = False  # 初始化/查询失败标记（临时禁用）
        self._schema_initialized = False

    @property
    def enabled(self) -> bool:
        """图服务是否启用（kuzu 可用 + 配置开启 + 未处于故障禁用状态）。"""
        return _KUZU_AVAILABLE and settings.memory_graph_enabled and not self._broken

    def _ensure_kuzu_conn(self) -> "kuzu.Connection | None":
        """懒加载 Kuzu 连接并初始化 schema。"""
        if not self.enabled or kuzu is None:
            return None
        if self._kuzu_conn is not None:
            return self._kuzu_conn
        try:
            self._kuzu_db = kuzu.Database(settings.memory_graph_path)
            self._kuzu_conn = kuzu.Connection(self._kuzu_db)
            if not self._schema_initialized:
                self._init_schema()
                self._schema_initialized = True
            return self._kuzu_conn
        except Exception as e:
            logger.warning("Kuzu 连接初始化失败: %s", e)
            self._broken = True
            return None

    def _init_schema(self) -> None:
        """初始化 Kuzu 图 schema（幂等）。"""
        if self._kuzu_conn is None:
            return
        # 节点表：实体（service_id + name 联合主键）
        self._kuzu_conn.execute("""
            CREATE NODE TABLE IF NOT EXISTS Entity (
                service_id STRING,
                name STRING,
                entity_type STRING,
                aliases STRING[],
                sentiment STRING,
                mention_count INT64,
                PRIMARY KEY (service_id, name)
            )
        """)
        # 关系表：实体间关系边
        self._kuzu_conn.execute("""
            CREATE REL TABLE IF NOT EXISTS Relation (
                FROM Entity TO Entity,
                relation STRING,
                valid BOOL,
                updated_at TIMESTAMP
            )
        """)

    async def sync_entity(self, service_id: str, entity: "MemoryEntity") -> None:
        """同步实体到图（fire-and-forget，内部吃异常）。"""
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(self._sync_entity_sync, service_id, entity)
        except Exception as e:
            logger.warning("同步实体到图失败: service=%s, name=%s, error=%s", service_id, entity.name, e)

    def _sync_entity_sync(self, service_id: str, entity: "MemoryEntity") -> None:
        """同步实体的同步实现（在线程池中执行）。"""
        conn = self._ensure_kuzu_conn()
        if conn is None:
            return
        # MERGE 实体节点（存在则更新属性）
        conn.execute(
            """
            MERGE (e:Entity {service_id: $service_id, name: $name})
            SET e.entity_type = $entity_type,
                e.aliases = $aliases,
                e.sentiment = $sentiment,
                e.mention_count = $mention_count
            """,
            {
                "service_id": service_id,
                "name": entity.name,
                "entity_type": entity.entity_type,
                "aliases": entity.aliases,
                "sentiment": entity.sentiment,
                "mention_count": entity.mention_count,
            },
        )
        # 同步关系边
        for rel in entity.relations:
            if not rel.valid:
                continue
            self._sync_relation_sync(service_id, entity.name, rel.target, rel.relation)

    async def sync_relation(
        self, service_id: str, src_name: str, dst_name: str, relation: str
    ) -> None:
        """同步关系边到图（fire-and-forget）。"""
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(
                self._sync_relation_sync, service_id, src_name, dst_name, relation
            )
        except Exception as e:
            logger.warning(
                "同步关系到图失败: service=%s, %s->%s, error=%s",
                service_id, src_name, dst_name, e,
            )

    def _sync_relation_sync(
        self, service_id: str, src_name: str, dst_name: str, relation: str
    ) -> None:
        """同步关系边的同步实现。"""
        conn = self._ensure_kuzu_conn()
        if conn is None:
            return
        # 确保目标实体存在（可能尚未同步）
        conn.execute(
            """
            MERGE (e:Entity {service_id: $service_id, name: $name})
            """,
            {"service_id": service_id, "name": dst_name},
        )
        # MERGE 关系边
        conn.execute(
            """
            MATCH (a:Entity {service_id: $service_id, name: $src_name}),
                  (b:Entity {service_id: $service_id, name: $dst_name})
            MERGE (a)-[r:Relation {relation: $relation}]->(b)
            SET r.valid = true, r.updated_at = $updated_at
            """,
            {
                "service_id": service_id,
                "src_name": src_name,
                "dst_name": dst_name,
                "relation": relation,
                "updated_at": datetime.now(timezone.utc),
            },
        )

    async def query_related(
        self, service_id: str, entity_name: str, max_depth: int = 2
    ) -> list[dict]:
        """多跳关系查询：Kuzu 启用时走 Cypher，否则降级 MongoDB 递归。

        Returns:
            关系路径列表，如 [{"path": ["小红", "哥哥", "小明"], "depth": 1}, ...]
        """
        if self.enabled:
            try:
                return await asyncio.to_thread(
                    self._query_kuzu, service_id, entity_name, max_depth
                )
            except Exception as e:
                logger.warning("Kuzu 查询失败，降级 MongoDB: %s", e)
                self._broken = True
        return await self._query_mongo_fallback(service_id, entity_name, max_depth)

    def _query_kuzu(
        self, service_id: str, entity_name: str, max_depth: int
    ) -> list[dict]:
        """Kuzu Cypher 多跳查询（同步实现，在线程池中执行）。"""
        conn = self._ensure_kuzu_conn()
        if conn is None:
            return []
        # 可变长度路径查询：1 到 max_depth 跳
        result = conn.execute(
            f"""
            MATCH path = (a:Entity {{service_id: $service_id, name: $name}})
                         -[r:Relation*1..{max_depth}]->(b:Entity)
            WHERE ALL rel IN r WHERE rel.valid = true
            RETURN [n IN nodes(path) | n.name] AS path_names,
                   [rel IN r | rel.relation] AS relations,
                   length(path) AS depth
            ORDER BY depth
            """,
            {"service_id": service_id, "name": entity_name},
        )
        paths: list[dict] = []
        while result.has_next():
            row = result.get_next()
            path_names = row[0]
            relations = row[1]
            depth = row[2]
            # 构造路径描述：["小红", "哥哥", "小明"] → "小红 -哥哥-> 小明"
            path_desc = [path_names[0]]
            for i, rel in enumerate(relations):
                path_desc.append(f"-{rel}->")
                path_desc.append(path_names[i + 1])
            paths.append({
                "path": path_names,
                "relations": relations,
                "depth": depth,
                "description": " ".join(path_desc),
            })
        return paths

    async def _query_mongo_fallback(
        self, service_id: str, entity_name: str, max_depth: int
    ) -> list[dict]:
        """MongoDB 应用层递归（降级方案，仅支持一跳展开）。"""
        from app.db.repositories.service_memory_repo import ServiceMemoryRepository

        repo = ServiceMemoryRepository()
        memory = await repo.find_by_service(service_id)
        if memory is None:
            return []
        # 查找目标实体
        target = next(
            (e for e in memory.entities if e.name == entity_name), None
        )
        if target is None:
            return []
        # 一跳展开：仅返回直接关系
        paths: list[dict] = []
        for rel in target.relations:
            if not rel.valid:
                continue
            paths.append({
                "path": [entity_name, rel.target],
                "relations": [rel.relation],
                "depth": 1,
                "description": f"{entity_name} -{rel.relation}-> {rel.target}",
            })
        return paths


# 全局单例
_entity_graph_service: EntityGraphService | None = None


def get_entity_graph_service() -> EntityGraphService:
    """获取实体关系图服务单例。"""
    global _entity_graph_service
    if _entity_graph_service is None:
        _entity_graph_service = EntityGraphService()
    return _entity_graph_service


def reset_entity_graph_service() -> None:
    """重置图服务单例（后台开关保存后调用，使热切换立即生效）。"""
    global _entity_graph_service
    _entity_graph_service = None
