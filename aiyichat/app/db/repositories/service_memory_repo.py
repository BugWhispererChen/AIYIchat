"""服务级长期记忆的数据库访问层。

管理 service_memories 集合的增删改查，
每个 Service 对应一条记忆文档，包含话题焦点和记忆事实列表。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.db.mongo import get_database
from app.models.service_memory import (
    CharacterMood,
    MemoryEntity,
    MemoryFact,
    PendingEvent,
    ServiceMemoryInDB,
)

logger = logging.getLogger(__name__)


def _fact_ref_time(fact: dict) -> datetime:
    """取事实的最后引用时间并归一为 UTC aware（兼容存量 naive 数据，避免排序报错）。"""
    dt = fact.get("last_referenced_at") or fact.get("created_at")
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class ServiceMemoryRepository:
    """管理服务级长期记忆的数据访问。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().service_memories

    async def find_by_service(self, service_id: str) -> Optional[ServiceMemoryInDB]:
        """根据 service_id 查询记忆文档。"""
        doc = await self._collection().find_one({"service_id": service_id})
        if doc is None:
            return None
        return ServiceMemoryInDB.from_mongo(doc)

    async def list_all(self) -> list[ServiceMemoryInDB]:
        """列出全部记忆文档（后台记忆管理页使用，按更新时间倒序）。"""
        cursor = self._collection().find({}).sort("updated_at", -1)
        docs: list[ServiceMemoryInDB] = []
        async for doc in cursor:
            parsed = ServiceMemoryInDB.from_mongo(doc)
            if parsed is not None:
                docs.append(parsed)
        return docs

    async def upsert_topic(self, service_id: str, topic: str) -> None:
        """更新当前话题焦点（不存在则创建文档）。"""
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"service_id": service_id},
            {
                "$set": {
                    "current_topic": topic,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "service_id": service_id,
                    "facts": [],
                },
            },
            upsert=True,
        )

    async def clear_current_topic(self, service_id: str, expected_topic: str) -> None:
        # 不覆盖并发提取的新话题，也不刷新焦点提取的节流时间。
        await self._collection().update_one(
            {"service_id": service_id, "current_topic": expected_topic},
            {"$set": {"current_topic": ""}},
        )

    async def add_facts(
        self, service_id: str, new_facts: list[MemoryFact]
    ) -> None:
        """
        追加新记忆事实到文档中。

        去重逻辑：跳过 content 完全相同的事实。
        上限控制：超过 memory_max_facts 时淘汰最久未引用的。
        """
        if not new_facts:
            return

        now = datetime.now(timezone.utc)

        # 获取或创建记忆文档
        doc = await self._collection().find_one({"service_id": service_id})
        if doc is None:
            # 首次创建
            await self._collection().insert_one({
                "service_id": service_id,
                "facts": [f.model_dump() for f in new_facts[:settings.memory_max_facts]],
                "current_topic": "",
                "updated_at": now,
            })
            return

        # 去重：跳过已存在的 content
        existing_contents = {f["content"] for f in doc.get("facts", [])}
        unique_facts = [
            f for f in new_facts if f.content not in existing_contents
        ]
        if not unique_facts:
            return

        # 追加新事实
        all_facts = doc.get("facts", []) + [f.model_dump() for f in unique_facts]

        # 上限控制：core 层（安全/身份）永不淘汰；仅对非 core 层按最后引用时间淘汰
        max_facts = settings.memory_max_facts
        if len(all_facts) > max_facts:
            core_facts = [f for f in all_facts if f.get("tier") == "core"]
            other_facts = [f for f in all_facts if f.get("tier") != "core"]
            other_facts.sort(key=_fact_ref_time, reverse=True)
            # 非 core 层保留名额 = 上限 - core 层数量（至少保留 core 层）
            other_quota = max(0, max_facts - len(core_facts))
            all_facts = core_facts + other_facts[:other_quota]

        await self._collection().update_one(
            {"service_id": service_id},
            {"$set": {"facts": all_facts, "updated_at": now}},
        )

    async def update_topic_and_facts(
        self,
        service_id: str,
        topic: str,
        new_facts: list[MemoryFact],
    ) -> None:
        """一次性更新话题焦点并追加新记忆事实（模糊去重）。"""
        await self.upsert_topic(service_id, topic)
        if not new_facts:
            return

        # 模糊去重：对每个 new_fact 检查是否与已有事实相似
        doc = await self._collection().find_one({"service_id": service_id})
        existing_facts = doc.get("facts", []) if doc else []
        existing_contents = [f.get("content", "") for f in existing_facts]

        unique_facts: list[MemoryFact] = []
        for fact in new_facts:
            similar_found = False
            for existing_content in existing_contents:
                if self._is_similar(fact.content, existing_content):
                    # 相似事实：更新引用时间而非重复存储
                    await self.update_fact_reference(service_id, existing_content)
                    similar_found = True
                    break
            if not similar_found:
                unique_facts.append(fact)

        # 只追加真正新的事实
        if unique_facts:
            await self.add_facts(service_id, unique_facts)

    async def update_mood(self, service_id: str, mood: CharacterMood) -> None:
        """更新角色当前心情（不存在则创建文档）。"""
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"service_id": service_id},
            {
                "$set": {
                    "mood": mood.model_dump(),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "service_id": service_id,
                    "facts": [],
                },
            },
            upsert=True,
        )

    async def update_fact_reference(self, service_id: str, fact_content: str) -> None:
        """更新指定事实的最后引用时间，并强化记忆强度（艾宾浩斯正向反馈）。

        强度强化：strength ×1.2（上限 3.0），被频繁引用的记忆衰减更慢。
        使用聚合管道更新以读取当前 strength 值进行条件计算。
        """
        from app.models.service_memory import reinforce_strength

        now = datetime.now(timezone.utc)
        # 先读出当前强度，计算强化后的值（MongoDB 无法直接在 $set 中做条件运算；
        # 不用位置投影以兼容 mongomock 测试环境）
        doc = await self._collection().find_one(
            {"service_id": service_id},
            {"facts": 1},
        )
        current = 1.0
        for f in (doc or {}).get("facts", []):
            if f.get("content") == fact_content:
                current = f.get("strength", 1.0)
                break
        new_strength = reinforce_strength(current)
        await self._collection().update_one(
            {"service_id": service_id, "facts.content": fact_content},
            {"$set": {
                "facts.$.last_referenced_at": now,
                "facts.$.strength": new_strength,
                "updated_at": now,
            }},
        )

    async def update_facts_reference(
        self, service_id: str, fact_contents: list[str]
    ) -> None:
        """批量更新多个事实的最后引用时间并强化记忆强度。

        使用 bulk_write 减少网络往返：N 条 facts 从 N 次 RT 降为 1 次。
        适用于 engine._touch_and_backfill_memory 的批量刷新场景。

        Args:
            service_id: 服务 ID
            fact_contents: 要更新的事实内容列表
        """
        if not fact_contents:
            return

        from app.models.service_memory import reinforce_strength
        from pymongo import UpdateOne

        now = datetime.now(timezone.utc)

        # 先读出当前文档，构建 content -> strength 映射
        doc = await self._collection().find_one(
            {"service_id": service_id},
            {"facts": 1},
        )
        if not doc:
            return

        strength_map: dict[str, float] = {}
        for f in doc.get("facts", []):
            content = f.get("content")
            if content in fact_contents:
                strength_map[content] = f.get("strength", 1.0)

        # 构建批量更新操作
        operations = []
        for content in fact_contents:
            current = strength_map.get(content, 1.0)
            new_strength = reinforce_strength(current)
            operations.append(
                UpdateOne(
                    {"service_id": service_id, "facts.content": content},
                    {"$set": {
                        "facts.$.last_referenced_at": now,
                        "facts.$.strength": new_strength,
                        "updated_at": now,
                    }},
                )
            )

        if operations:
            await self._collection().bulk_write(operations, ordered=False)

    async def append_topic_history(self, service_id: str, topic: str) -> None:
        """追加话题到历史记录（最多保存10条）。"""
        if not topic or not topic.strip():
            return
        now = datetime.now(timezone.utc)
        # push 新话题，$slice -10 保留最近10条
        await self._collection().update_one(
            {"service_id": service_id},
            {
                "$push": {"topic_history": {"$each": [topic.strip()], "$slice": -10}},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )

    async def replace_fact(
        self, service_id: str, old_content: str, new_fact: MemoryFact
    ) -> None:
        """用新事实物理替换旧事实（冲突消解 UPDATE 的降级路径）。

        仅在 memory_fact_validity_enabled 关闭时使用；开启时应走 soft_replace_fact，
        保留旧事实的可回溯时间线。
        """
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"service_id": service_id},
            {
                "$pull": {"facts": {"content": old_content}},
                "$set": {"updated_at": now},
            },
        )
        await self.add_facts(service_id, [new_fact])

    async def soft_replace_fact(
        self,
        service_id: str,
        old_content: str,
        new_fact: MemoryFact,
        invalid_at: datetime,
        expired_at: datetime,
    ) -> None:
        """软失效替换旧事实（冲突消解 UPDATE）：只标记失效不删除，再追加新事实。

        照搬 Graphiti/Zep 语义：old.invalid_at = new.valid_at or now；old.expired_at = now。
        旧事实保留在库中可回溯（支持时间旅行查询），仅被注入前的硬过滤排除；
        已标失效的事实不再刷新 last_referenced_at，避免“越记越牢”的自我强化循环。
        """
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"service_id": service_id, "facts.content": old_content},
            {"$set": {
                "facts.$.invalid_at": invalid_at,
                "facts.$.expired_at": expired_at,
                "updated_at": now,
            }},
        )
        await self.add_facts(service_id, [new_fact])

    async def delete_fact(self, service_id: str, content: str) -> None:
        """删除指定内容的记忆事实（冲突消解 DELETE，如事实已过时）。"""
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"service_id": service_id},
            {
                "$pull": {"facts": {"content": content}},
                "$set": {"updated_at": now},
            },
        )

    async def set_fact_embedding(
        self, service_id: str, content: str, embedding: list[float]
    ) -> None:
        """为存量事实回填语义向量（渐进式向量化）。"""
        if not embedding:
            return
        await self._collection().update_one(
            {"service_id": service_id, "facts.content": content},
            {"$set": {"facts.$.embedding": embedding}},
        )

    async def add_pending_events(
        self, service_id: str, events: list[PendingEvent]
    ) -> None:
        """追加待跟进事件（去重：跳过内容相同的未完成事件），上限保留10条。"""
        if not events:
            return
        now = datetime.now(timezone.utc)
        doc = await self._collection().find_one({"service_id": service_id})
        if doc is None:
            await self._collection().insert_one({
                "service_id": service_id,
                "facts": [],
                "current_topic": "",
                "pending_events": [e.model_dump() for e in events[:10]],
                "updated_at": now,
            })
            return
        existing = doc.get("pending_events", [])
        # 仅对未完成事件去重
        pending_contents = {
            e.get("content") for e in existing if not e.get("followed_up")
        }
        new_events = [e for e in events if e.content not in pending_contents]
        if not new_events:
            return
        all_events = existing + [e.model_dump() for e in new_events]
        # 上限控制：保留最近10条
        all_events = all_events[-10:]
        await self._collection().update_one(
            {"service_id": service_id},
            {"$set": {"pending_events": all_events, "updated_at": now}},
        )

    async def mark_event_followed_up(
        self, service_id: str, content: str
    ) -> None:
        """将指定事件标记为已跟进。"""
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"service_id": service_id, "pending_events.content": content},
            {
                "$set": {
                    "pending_events.$.followed_up": True,
                    "updated_at": now,
                }
            },
        )

    async def upsert_entities(
        self, service_id: str, entities: list[MemoryEntity]
    ) -> None:
        """按名称 upsert 实体关系（存在则合并属性并刷新提及时间）。

        合并策略：
        - 新字段（entity_type/aliases/sentiment/relations/mention_count/embedding）全量合并
        - aliases 并集去重；relations 按 target 合并（含 invalid 标记）
        - mention_count 累加；last_mentioned_at 刷新
        - 超 memory_max_entities 时按活跃度（mention_count + last_mentioned_at）淘汰
        """
        if not entities:
            return
        now = datetime.now(timezone.utc)
        doc = await self._collection().find_one({"service_id": service_id})
        existing: list[dict] = (doc or {}).get("entities", [])
        by_name = {e.get("name"): e for e in existing}
        for ent in entities:
            if ent.name in by_name:
                # 合并：更新关系与属性，刷新提及时间
                cur = by_name[ent.name]
                if ent.relation_to_identity:
                    cur["relation_to_identity"] = ent.relation_to_identity
                cur.setdefault("attributes", {}).update(ent.attributes)
                cur["last_mentioned_at"] = now
                # 新字段合并
                cur["entity_type"] = ent.entity_type
                # aliases 并集去重
                existing_aliases = set(cur.get("aliases", []))
                for alias in ent.aliases:
                    if alias not in existing_aliases:
                        cur.setdefault("aliases", []).append(alias)
                if ent.sentiment:
                    cur["sentiment"] = ent.sentiment
                # relations 按 target 合并（含 invalid 标记）
                existing_relations = cur.get("relations", [])
                for new_rel in ent.relations:
                    existing_rel = next(
                        (r for r in existing_relations if r.get("target") == new_rel.target),
                        None,
                    )
                    if existing_rel is None:
                        existing_relations.append(new_rel.model_dump())
                    elif existing_rel.get("relation") != new_rel.relation:
                        # 关系变更：旧关系标 invalid 保留时间线，新关系写入
                        existing_rel["valid"] = False
                        existing_relations.append(new_rel.model_dump())
                    else:
                        # 相似关系：仅刷新时间
                        existing_rel["updated_at"] = new_rel.updated_at
                cur["relations"] = existing_relations
                # mention_count 累加
                cur["mention_count"] = cur.get("mention_count", 0) + ent.mention_count
                # embedding 更新（新实体有向量时覆盖）
                if ent.embedding:
                    cur["embedding"] = ent.embedding
            else:
                by_name[ent.name] = ent.model_dump()
        # 上限控制：超 memory_max_entities 时按活跃度淘汰最不活跃者
        merged = list(by_name.values())
        max_entities = settings.memory_max_entities
        if len(merged) > max_entities:
            # 活跃度排序：mention_count 降序 + last_mentioned_at 降序
            merged.sort(
                key=lambda e: (
                    e.get("mention_count", 0),
                    e.get("last_mentioned_at", now),
                ),
                reverse=True,
            )
            merged = merged[:max_entities]
        await self._collection().update_one(
            {"service_id": service_id},
            {
                "$set": {"entities": merged, "updated_at": now},
                "$setOnInsert": {"service_id": service_id, "facts": []},
            },
            upsert=True,
        )

    @staticmethod
    def _is_similar(text1: str, text2: str, threshold: float = 0.6) -> bool:
        """基于字符级 Jaccard 相似度判断两个文本是否相似。"""
        if not text1 or not text2:
            return False
        if text1 == text2:
            return True
        chars1 = set(text1)
        chars2 = set(text2)
        intersection = len(chars1 & chars2)
        union = len(chars1 | chars2)
        if union == 0:
            return False
        return (intersection / union) >= threshold

    async def delete_by_service(self, service_id: str) -> bool:
        """删除指定服务的所有记忆（Service 级联删除时调用）。"""
        result = await self._collection().delete_one({"service_id": service_id})
        return result.deleted_count > 0

    async def delete_by_service_ids(self, service_ids: list[str]) -> int:
        """批量删除多个服务的记忆（账号级联删除时调用）。"""
        if not service_ids:
            return 0
        result = await self._collection().delete_many(
            {"service_id": {"$in": service_ids}}
        )
        return result.deleted_count
