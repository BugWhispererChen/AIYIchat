"""对话焦点追踪器。

每隔若干轮对话，通过轻量 LLM 调用从最近消息中提取：
1. 当前话题焦点（current_topic）
2. 新发现的用户事实（new_facts）

提取结果保存到 service_memories 集合，供后续 Prompt 注入使用。
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from app.config import settings
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.llm.provider_factory import get_llm_provider
from app.models.message import MessageInDB
from app.models.service_memory import (
    MemoryFact,
    MemoryTier,
    PendingEvent,
    cosine_similarity,
)
from app.services.embedding_service import get_embedding_service

logger = logging.getLogger(__name__)

# 提取 prompt 模板
_EXTRACT_SYSTEM_PROMPT = (
    "你是一个对话分析助手。请分析以下对话，提取两项信息：\n"
    "1. current_topic: 当前对话的核心话题，用一句简短的中文描述（不超过30字）\n"
    "2. new_facts: 从对话中发现的关于用户的新事实（兴趣爱好、生活事件、人际关系、"
    "情绪模式等），每条为对象：\n"
    '   {{"content": "事实描述(不超过20字)", '
    '"valid_at": "该事实发生或开始成立的绝对日期 YYYY-MM-DD，无法确定则为 null"}}\n'
    "注意：提取事实时，如果内容包含\"今天/今日/昨天/明天/后天/大后天\"等相对时间词，"
    "必须替换为绝对日期（如\"8月25日\"），例如：\n"
    "- 用户说\"今天上班好累\" → content 为\"用户8月25日上班感到疲惫\"，"
    "valid_at 为\"2026-08-25\"\n"
    "- 用户说\"明天要面试\" → content 为\"用户8月26日有面试\"，"
    "valid_at 为\"2026-08-26\"\n"
    "- 长期稳定的信息（如\"用户喜欢喝咖啡\"、\"用户对花生过敏\"、\"用户住在杭州\"）"
    "valid_at 返回 null，因为它们不随时间失效\n\n"
    "已知的用户事实（不要重复提取）：\n{existing_facts}\n\n"
    "请严格按以下 JSON 格式输出，不要输出其他内容：\n"
    '{{"current_topic": "话题描述", "new_facts": '
    '[{{"content": "事实1", "valid_at": null}}]}}\n\n'
    "如果没有发现新事实，new_facts 返回空数组。\n"
    "如果对话内容过于琐碎无明确话题，current_topic 返回空字符串。"
)

# 冲突消解相似度阈值
_SIM_DEDUP = 0.90       # 高于此视为重复，仅刷新引用时间
_SIM_AMBIGUOUS = 0.62   # 介于两者之间需 LLM 裁决；低于此直接新增
_MAX_LLM_RESOLVE = 5    # 单次提取最多 LLM 裁决次数（控制 token 成本）

# 冲突消解裁决提示词
_RESOLVE_SYSTEM_PROMPT = (
    "你是记忆管理助手。给定一条「已有记忆」和一条「新事实」，判断关系并决定操作：\n"
    "- ADD：新事实是全新信息，与已有记忆不矛盾\n"
    "- UPDATE：新事实是对已有记忆的更新或纠正（如状态变化、纠错），应替换旧记忆\n"
    "- NOOP：新事实与已有记忆表达相同信息，无需变动\n"
    '严格输出 JSON，不要其他内容：{"action": "ADD"}'
)

# 事件提取附加指令（始终追加；是否主动跟进由 proactive_event_enabled 决定）
# 要求直接输出绝对日期串：LLM 自行推算 days_from_now 整数时易错，
# 且“下周三/9月15号”这类表达无法用整数天数准确表达（解析失败时回落整数逻辑）
_EVENTS_ADDENDUM = (
    "\n\n此外，如果用户提到未来某个时间点会发生、值得日后关心的具体事件"
    "（如面试、考试、约会、体检、旅行等），提取到 events 数组：\n"
    'events: [{{"content": "事件描述(不超过20字)", '
    '"date": "事件发生的绝对日期，格式 YYYY-MM-DD"}}]\n'
    "date 计算规则：以该条消息自己的 [日期] 为基准推算“今天/明天/下周三”，"
    "而不是以当前日期为基准；无法确定具体日期时 date 返回空字符串。\n"
    "没有则 events 返回空数组。"
)

# 实体关系提取附加指令（轻量关系图谱）
_ENTITIES_ADDENDUM = (
    "\n\n此外，如果对话中出现与用户相关的人物/事物实体（如朋友、家人、宠物、物品等），"
    "提取到 entities 数组：\n"
    'entities: [{{"name": "实体名", "relation_to_user": "与用户的关系", '
    '"aliases": ["别名1", "别名2"], "entity_type": "person|pet|thing", '
    '"sentiment": "用户对该实体的情感倾向", '
    '"attributes": {{"属性名": "属性值"}}, '
    '"relations": [{{"target": "目标实体名", "relation": "关系描述"}}]}}]\n'
    "说明：\n"
    "- aliases: 实体的别名/昵称（如'老王'、'王哥'），用于消歧\n"
    "- entity_type: person=人物, pet=宠物, thing=事物\n"
    "- sentiment: 用户对该实体的情感倾向（如'亲近'、'有矛盾'），无则留空\n"
    "- relations: 该实体与其他实体的关系（如'小红的哥哥是小明'），无则留空数组\n"
    "没有则 entities 返回空数组。"
)


def _build_full_output_format(include_events: bool) -> str:
    """拼接完整输出格式说明。"""
    fmt = '{{"current_topic": "...", "new_facts": [...]'
    if include_events:
        fmt += ', "events": [...]'
    fmt += ', "entities": [...]}'
    return fmt


class FocusTracker:
    """对话焦点追踪器，负责提取话题焦点和用户记忆事实。"""

    # 话题转换信号词
    _TOPIC_SHIFT_MARKERS: list[str] = [
        "对了", "顺便", "话说", "另外", "突然想到", "还有件事",
    ]

    def __init__(self) -> None:
        self.memory_repo = ServiceMemoryRepository()

    def should_extract(
        self, message_count: int, last_msg: str, last_extract_time: datetime | None = None
    ) -> bool:
        """自适应判断是否应该进行焦点提取。

        Args:
            message_count: 当前会话的总消息数
            last_msg: 最新一条用户消息内容
            last_extract_time: 上次提取的时间（可选）

        Returns:
            是否应该执行提取
        """
        # 基础条件：至少达到提取间隔
        if message_count % settings.memory_extract_interval != 0:
            # 但如果检测到话题转换信号，提前触发
            if not any(marker in last_msg for marker in self._TOPIC_SHIFT_MARKERS):
                return False

        # 5分钟内不重复提取（防止密集对话过度消耗LLM）
        if last_extract_time:
            # MongoDB 读出的 datetime 可能为 naive（无时区），按 UTC 归一后再相减
            if last_extract_time.tzinfo is None:
                last_extract_time = last_extract_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_extract_time).total_seconds()
            if elapsed < 300:
                return False

        return True

    async def extract_and_save(
        self,
        service_id: str,
        recent_messages: list[MessageInDB],
        existing_facts: list[str],
        current_topic: str = "",
    ) -> None:
        """
        从最近消息中提取焦点和新事实，并保存到数据库（含话题历史追踪）。

        此方法设计为 fire-and-forget 调用，内部捕获所有异常。

        Args:
            service_id: 对话服务ID
            recent_messages: 最近的对话消息列表
            existing_facts: 已有的记忆事实内容列表（用于去重）
            current_topic: 当前话题（用于检测话题变化）
        """
        try:
            # 加载已有实体（用于提取时消歧）
            service_memory = await self.memory_repo.find_by_service(service_id)
            existing_entities = (
                list(service_memory.entities) if service_memory else []
            )
            result = await self._extract(recent_messages, existing_facts, existing_entities)
            if result is None:
                return

            topic = result.get("current_topic", "")
            raw_facts = result.get("new_facts", [])

            # 构造 MemoryFact 对象（含分层 tier 与双时间轴）
            now = datetime.now(timezone.utc)
            tz = timezone(timedelta(hours=settings.default_timezone_offset))
            # provenance：事实来自这批消息，时间基点取最后一条消息的发生时刻
            source_at = self._last_message_at(recent_messages)
            # 拼接原文用于 jionlp 校验（防 LLM 编造日期）
            source_text = "\n".join(
                getattr(m, "content", "") for m in recent_messages[-6:]
            )
            new_facts = []
            for item in raw_facts:
                fact_text, valid_at = self._parse_fact_item(item, tz, now)
                if not fact_text:
                    continue
                # jionlp 校验层：LLM 给出 valid_at 后，检查原文是否真含该时间实体，
                # 推不出则置 None（防幻觉）；jionlp 缺失/异常时静默降级为信任 LLM
                if valid_at is not None and settings.time_jionlp_enabled:
                    if not self._verify_fact_date(fact_text, valid_at, source_text, now, tz):
                        valid_at = None
                category = self._classify_fact(fact_text)
                new_facts.append(
                    MemoryFact(
                        content=fact_text,
                        category=category,
                        created_at=now,
                        last_referenced_at=now,
                        tier=self._tier_for(category),
                        valid_at=valid_at,
                        source_message_at=source_at,
                    )
                )

            # 语义向量化（启用时），失败时 embedding 为空列表自动降级
            emb_service = get_embedding_service()
            if new_facts and emb_service.enabled:
                vectors = await emb_service.embed_batch(
                    [f.content for f in new_facts]
                )
                for fact, vec in zip(new_facts, vectors, strict=True):
                    fact.embedding = vec

            # 加载完整已有事实（含向量）用于冲突消解
            service_memory = await self.memory_repo.find_by_service(service_id)
            existing_fact_objs = (
                list(service_memory.facts) if service_memory else []
            )

            # 更新话题焦点，并对新事实逐条冲突消解后落库
            await self.memory_repo.upsert_topic(service_id, topic)
            await self._resolve_and_apply(
                service_id, new_facts, existing_fact_objs
            )

            # 话题变化时追加到话题历史
            if topic and topic != current_topic:
                await self.memory_repo.append_topic_history(service_id, topic)

            # 提取并保存待跟进事件：与实体一样始终入库，不受 proactive_event_enabled 门控。
            # 该开关的语义应是"是否主动发跟进消息"（扰民风险所在），
            # 而事件抽取共用同一次 LLM 调用、几乎零成本；若把它也门控掉，
            # 开关从关到开之后 pending_events 仍是空的，角色要等很久才"记起"对方的事。
            await self._save_events(service_id, result.get("events", []))

            # 提取并保存实体关系（轻量关系图谱）
            await self._save_entities(service_id, result.get("entities", []))

            logger.info(
                "Focus extracted: service=%s, topic='%s', new_facts=%d",
                service_id, topic[:30], len(new_facts),
            )

        except Exception as e:
            logger.error(
                "Focus extraction failed for service=%s: %s",
                service_id, e,
            )

    async def _save_events(self, service_id: str, raw_events) -> None:
        """解析 LLM 提取的事件并转为 PendingEvent 落库。

        日期解析优先级：
        1. jionlp parse_time 解析 content 中的相对时间（如"下周三""9月15号"）
        2. date 绝对日期串（YYYY-MM-DD 等常见格式）
        3. 回落 days_from_now 整数（兼容旧提示词与模型输出偏差）
        4. 三者均无效时当作今天（days=0），与原有保守行为一致
        """
        if not isinstance(raw_events, list) or not raw_events:
            return
        now = datetime.now(timezone.utc)
        tz = timezone(timedelta(hours=settings.default_timezone_offset))
        events: list[PendingEvent] = []
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            # 优先用 jionlp 解析 content 中的相对时间（治 D5 的日期精度）
            due_at: datetime | None = None
            if settings.time_jionlp_enabled:
                due_at = self._parse_event_with_jionlp(content, now, tz)
            if due_at is None:
                due_at = self._parse_event_date(str(item.get("date", "")).strip(), tz, now)
            if due_at is None:
                # 回落现有整数逻辑：绝对日期解析失败时用 days_from_now
                try:
                    days = int(item.get("days_from_now", 0))
                except (ValueError, TypeError):
                    days = 0
                days = max(0, min(days, 365))  # 限定合理范围
                due_at = now + timedelta(days=days)
            events.append(PendingEvent(content=content, due_at=due_at))
        if events:
            await self.memory_repo.add_pending_events(service_id, events)

    @staticmethod
    def _parse_event_with_jionlp(
        content: str, now_utc: datetime, tz: timezone
    ) -> datetime | None:
        """用 jionlp parse_time 解析事件内容中的相对时间，返回 aware UTC 时间点。

        能正确处理"下周三""9月15号"等表达；解析失败/不可用时返回 None，
        由调用方回落到 date 绝对日期串或 days_from_now 整数逻辑。
        """
        try:
            from app.conversation import time_normalizer
            return time_normalizer.parse_time_point(content, now_utc, tz)
        except Exception:
            return None

    @staticmethod
    def _verify_fact_date(
        fact_text: str,
        valid_at: datetime,
        source_text: str,
        now_utc: datetime,
        tz: timezone,
    ) -> bool:
        """jionlp 校验层：检查 LLM 给出的 valid_at 是否真能从原文推出。

        推不出则返回 False（调用方将 valid_at 置 None，防 LLM 编造日期）；
        jionlp 缺失/异常时返回 True（信任 LLM，不阻断主链路）。
        """
        try:
            from app.conversation import time_normalizer
            return time_normalizer.verify_llm_date(
                valid_at.strftime("%Y-%m-%d"), source_text, now_utc, tz
            )
        except Exception:
            return True

    @staticmethod
    def _parse_event_date(
        raw_date: str, tz: timezone, now_utc: datetime
    ) -> datetime | None:
        """将 LLM 输出的绝对日期串解析为 due_at（UTC aware）。

        时刻部分沿用抽取当下的本地时分，与原有 `now + timedelta(days=N)` 行为等价，
        保障主动关怀调度器的到期判定不变。解析失败返回 None，由调用方回落整数逻辑。
        """
        local_now = now_utc.astimezone(tz)
        parsed = FocusTracker._parse_local_date(raw_date, local_now)
        if parsed is None:
            return None
        due_local = parsed.replace(
            hour=local_now.hour, minute=local_now.minute, tzinfo=tz
        )
        return due_local.astimezone(timezone.utc)
    
    @staticmethod
    def _parse_valid_at(
        raw: object, tz: timezone, now_utc: datetime
    ) -> datetime | None:
        """将 LLM 输出的 valid_at 解析为「该本地日 00:00」对应的 UTC aware 时间。
    
        事实从该日起成立，取当日零点作为现实成立轴的起点。
        解析失败一律返回 None：事实照常入库（仅缺时间轴），不影响主流程。
        """
        parsed = FocusTracker._parse_local_date(raw, now_utc.astimezone(tz))
        if parsed is None:
            return None
        return parsed.replace(tzinfo=tz).astimezone(timezone.utc)
    
    @staticmethod
    def _parse_local_date(raw: object, now_local: datetime) -> datetime | None:
        """把 LLM 输出的日期串解析为 naive 本地日期（时刻归零）。
    
        兼容 YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日 / MM-DD（省略年份按当前年补全，
        补全后早于今天超过半年则视为跳年后的明年）。
        非字符串（数值/布尔）与 null/空串一律返回 None，不抛异常。
        """
        if raw is None or isinstance(raw, (bool, int, float)):
            return None
        text = str(raw).strip()
        if not text or text.lower() in ("null", "none", "-"):
            return None
        candidates = (
            text,
            text.replace("年", "-").replace("月", "-").replace("日", ""),
        )
        for candidate in candidates:
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
                try:
                    parsed = datetime.strptime(candidate.strip(), fmt)
                except ValueError:
                    continue
                if parsed.year == 1900:
                    parsed = parsed.replace(year=now_local.year)
                    if (now_local.date() - parsed.date()).days > 180:
                        parsed = parsed.replace(year=now_local.year + 1)
                return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
        return None
    
    @classmethod
    def _parse_fact_item(
        cls, item: object, tz: timezone, now_utc: datetime
    ) -> tuple[str, datetime | None]:
        """解析单条 new_facts 输出，兼容字符串与对象两种形态。
    
        对象形态携带 valid_at（现实成立轴）；字符串形态（旧提示词或模型输出偏差）
        仍照常入库，仅 valid_at 为 None，保证降级不断链。
    
        Returns:
            (事实文本, valid_at)，无效条目返回 ("", None)
        """
        if isinstance(item, str):
            return item.strip(), None
        if not isinstance(item, dict):
            return "", None
        content = str(item.get("content", "")).strip()
        if not content:
            return "", None
        return content, cls._parse_valid_at(item.get("valid_at"), tz, now_utc)
    
    @staticmethod
    def _last_message_at(messages: list[MessageInDB]) -> datetime | None:
        """取这批消息中最后一条的发生时刻（作为事实的 provenance 时间基点）。
    
        整批消息均无时间戳时返回 None（如导入数据缺时间），不报错。
        """
        for msg in reversed(messages):
            created = getattr(msg, "created_at", None)
            if created is None:
                continue
            return (
                created if created.tzinfo is not None
                else created.replace(tzinfo=timezone.utc)
            )
        return None

    async def _save_entities(self, service_id: str, raw_entities) -> None:
        """解析 LLM 提取的实体关系并落库（消歧合并流程）。

        消歧策略：
        - 第一层：新实体 name/aliases 与已有实体精确匹配命中 → 直接合并
        - 第二层（embedding 启用时）：拼接文本向量与已有实体 embedding 余弦相似度
          ≥ memory_entity_merge_threshold → 视为同一实体合并
        - 合并：更新 relation_to_user/attributes/sentiment，aliases 并集去重，
          mention_count += 1，刷新 last_mentioned_at；新实体写入 embedding
        - relations 合并：同 target 新旧关系语义相似度低于阈值 → 旧关系标 invalid
          保留时间线、新关系写入；相似度高则仅刷新 updated_at
        """
        from app.models.service_memory import EntityRelation, MemoryEntity
        from app.services.entity_graph_service import get_entity_graph_service

        if not isinstance(raw_entities, list) or not raw_entities:
            return

        # 时区用于 jionlp 归一化（attributes 中的相对时间转绝对日期）
        tz = timezone(timedelta(hours=settings.default_timezone_offset))

        # 加载已有实体（用于消歧）
        service_memory = await self.memory_repo.find_by_service(service_id)
        existing_entities: list[MemoryEntity] = (
            list(service_memory.entities) if service_memory else []
        )
        existing_by_name = {e.name: e for e in existing_entities}
        # 别名索引：alias → entity
        existing_by_alias: dict[str, MemoryEntity] = {}
        for e in existing_entities:
            for alias in e.aliases:
                existing_by_alias[alias] = e

        emb_service = get_embedding_service()
        graph_service = get_entity_graph_service()
        merged_entities: list[MemoryEntity] = []

        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue

            # 解析新实体字段
            aliases = [str(a).strip() for a in item.get("aliases", []) if a]
            entity_type_str = str(item.get("entity_type", "person")).strip()
            entity_type: Literal["person", "pet", "thing"] = (
                "person" if entity_type_str not in ("person", "pet", "thing")
                else entity_type_str  # type: ignore[assignment]
            )
            sentiment = str(item.get("sentiment", "")).strip()
            attrs = item.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}
            attrs = {str(k): str(v) for k, v in attrs.items()}
            # jionlp 归一化：把 attributes 中的相对时间（如"生日": "下周三"）
            # 转为绝对日期串，防止三个月后仍读到"生日=下周三"（治 D4）
            if settings.time_jionlp_enabled:
                attrs = self._normalize_attributes(attrs, datetime.now(timezone.utc), tz)
            raw_relations = item.get("relations", [])
            relations: list[EntityRelation] = []
            if isinstance(raw_relations, list):
                for r in raw_relations:
                    if not isinstance(r, dict):
                        continue
                    target = str(r.get("target", "")).strip()
                    relation = str(r.get("relation", "")).strip()
                    if target and relation:
                        relations.append(EntityRelation(target=target, relation=relation))

            # 第一层消歧：名称或别名精确匹配
            matched: MemoryEntity | None = existing_by_name.get(name)
            if matched is None:
                for alias in aliases:
                    matched = existing_by_alias.get(alias)
                    if matched:
                        break

            # 第二层消歧：embedding 相似度（启用时）
            if matched is None and emb_service.enabled:
                new_text = f"{name} {' '.join(aliases)} {item.get('relation_to_user', '')}"
                new_embedding = await emb_service.embed(new_text)
                if new_embedding:
                    best_sim = 0.0
                    for existing in existing_entities:
                        if not existing.embedding:
                            continue
                        sim = cosine_similarity(new_embedding, existing.embedding)
                        if sim > best_sim:
                            best_sim = sim
                            matched = existing
                    if best_sim < settings.memory_entity_merge_threshold:
                        matched = None  # 未达阈值，视为新实体

            if matched is not None:
                # 合并到已有实体
                if item.get("relation_to_user"):
                    matched.relation_to_user = str(item["relation_to_user"]).strip()
                matched.attributes.update(attrs)
                if sentiment:
                    matched.sentiment = sentiment
                # aliases 并集去重
                for alias in aliases:
                    if alias not in matched.aliases:
                        matched.aliases.append(alias)
                matched.mention_count += 1
                matched.last_mentioned_at = datetime.now(timezone.utc)
                # relations 合并：同 target 冲突处理
                for new_rel in relations:
                    existing_rel = next(
                        (r for r in matched.relations if r.target == new_rel.target),
                        None,
                    )
                    if existing_rel is None:
                        matched.relations.append(new_rel)
                    elif existing_rel.relation != new_rel.relation:
                        # 关系变更：旧关系标 invalid 保留时间线，新关系写入
                        existing_rel.valid = False
                        matched.relations.append(new_rel)
                    else:
                        # 相似关系：仅刷新时间
                        existing_rel.updated_at = datetime.now(timezone.utc)
                merged_entities.append(matched)
            else:
                # 新实体
                new_entity = MemoryEntity(
                    name=name,
                    relation_to_user=str(item.get("relation_to_user", "")).strip(),
                    attributes=attrs,
                    entity_type=entity_type,
                    aliases=aliases,
                    sentiment=sentiment,
                    relations=relations,
                    mention_count=1,
                )
                # 写入 embedding（启用时）
                if emb_service.enabled:
                    new_text = f"{name} {' '.join(aliases)} {new_entity.relation_to_user}"
                    new_entity.embedding = await emb_service.embed(new_text)
                merged_entities.append(new_entity)
                existing_entities.append(new_entity)
                existing_by_name[name] = new_entity

        if merged_entities:
            await self.memory_repo.upsert_entities(service_id, merged_entities)
            # 异步同步到图服务（fire-and-forget）
            for entity in merged_entities:
                await graph_service.sync_entity(service_id, entity)

    @staticmethod
    def _normalize_attributes(
        attrs: dict[str, str], now_utc: datetime, tz: timezone
    ) -> dict[str, str]:
        """对 attributes 每个 value 跑 jionlp 归一化，命中相对时间则转为绝对日期串。

        不支持的表达（"前两天"/"昨晚1点"）原样保留，不归一化、不报错；
        jionlp 缺失/异常时静默降级为原样返回（不阻断主链路）。
        """
        try:
            from app.conversation import time_normalizer
        except Exception:
            return attrs
        normalized: dict[str, str] = {}
        for key, value in attrs.items():
            normalized_value = time_normalizer.normalize_relative(value, now_utc, tz)
            normalized[key] = normalized_value if normalized_value else value
        return normalized

    async def _resolve_and_apply(
        self,
        service_id: str,
        new_facts: list[MemoryFact],
        existing_facts: list[MemoryFact],
    ) -> None:
        """对新事实逐条做冲突消解并落库（ADD / UPDATE / NOOP）。

        策略：
        - 与所有已有事实相似度低于 _SIM_AMBIGUOUS → 直接新增
        - 相似度高于 _SIM_DEDUP → 视为重复，仅刷新引用时间
        - 介于两者之间 → LLM 裁决 ADD/UPDATE/NOOP（单次上限 _MAX_LLM_RESOLVE）
        """
        llm_resolve_used = 0
        for nf in new_facts:
            best_ef: MemoryFact | None = None
            best_sim = 0.0
            # 记录本次配对是否由向量得出：字符级降级相似度不足以支撑"失效旧事实"的决定
            best_by_vector = False
            for ef in existing_facts:
                if nf.embedding and ef.embedding:
                    sim = cosine_similarity(nf.embedding, ef.embedding)
                    by_vector = True
                else:
                    sim = self._char_similarity(nf.content, ef.content)
                    by_vector = False
                if sim > best_sim:
                    best_sim, best_ef = sim, ef
                    best_by_vector = by_vector

            if best_ef is None or best_sim < _SIM_AMBIGUOUS:
                # 全新信息
                await self.memory_repo.add_facts(service_id, [nf])
                existing_facts.append(nf)
            elif best_sim >= _SIM_DEDUP:
                # 重复：仅刷新引用时间
                await self.memory_repo.update_fact_reference(
                    service_id, best_ef.content
                )
            elif llm_resolve_used < _MAX_LLM_RESOLVE:
                # 模糊区间：LLM 裁决
                llm_resolve_used += 1
                action = await self._decide_action(best_ef.content, nf.content)
                if (
                    action == "UPDATE"
                    and settings.memory_update_requires_vector
                    and not best_by_vector
                ):
                    # 配对依据只是字符重叠度，不足以断定"同一件事被更新"：
                    # 误失效会静默丢掉仍然有效的记忆，故降级为并存（只增不改）。
                    logger.info(
                        "冲突消解降级：字符配对不支持 UPDATE，改为并存 service=%s old=%s",
                        service_id, best_ef.content[:30],
                    )
                    await self.memory_repo.add_facts(service_id, [nf])
                    existing_facts.append(nf)
                elif action == "UPDATE":
                    await self._apply_update(service_id, best_ef, nf)
                    existing_facts.remove(best_ef)
                    existing_facts.append(nf)
                elif action == "ADD":
                    await self.memory_repo.add_facts(service_id, [nf])
                    existing_facts.append(nf)
                else:  # NOOP
                    await self.memory_repo.update_fact_reference(
                        service_id, best_ef.content
                    )
            else:
                # LLM 裁决预算耗尽，保守处理：当作新信息新增
                await self.memory_repo.add_facts(service_id, [nf])
                existing_facts.append(nf)

    async def _apply_update(
        self, service_id: str, old_fact: MemoryFact, new_fact: MemoryFact
    ) -> None:
        """冲突消解 UPDATE 落库：软失效旧事实（不删除）后追加新事实。

        照搬 Graphiti 语义：old.invalid_at = new.valid_at or now；old.expired_at = now。
        旧事实保留在库中可回溯，仅从注入候选中被硬过滤排除。
        memory_fact_validity_enabled 关闭时退回原有的物理替换（优雅降级）。
        """
        if not settings.memory_fact_validity_enabled:
            await self.memory_repo.replace_fact(service_id, old_fact.content, new_fact)
            return
        now = datetime.now(timezone.utc)
        await self.memory_repo.soft_replace_fact(
            service_id,
            old_fact.content,
            new_fact,
            invalid_at=new_fact.valid_at or now,
            expired_at=now,
        )

    async def _decide_action(
        self, existing_content: str, new_content: str
    ) -> str:
        """调用辅助 LLM 裁决新旧事实关系，返回 ADD/UPDATE/NOOP。"""
        try:
            provider = get_llm_provider("focus")
            raw = await provider.complete(
                messages=[
                    {"role": "system", "content": _RESOLVE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"已有记忆：{existing_content}\n"
                            f"新事实：{new_content}"
                        ),
                    },
                ],
                temperature=settings.focus_temperature,
                max_tokens=settings.focus_max_tokens,
                response_format={"type": "json_object"},
            )
            data = self._parse_response(raw) or {}
            action = str(data.get("action", "ADD")).upper()
            return action if action in ("ADD", "UPDATE", "NOOP") else "ADD"
        except Exception as e:
            logger.warning("冲突消解裁决失败，默认ADD: %s", e)
            return "ADD"

    @staticmethod
    def _char_similarity(text1: str, text2: str) -> float:
        """字符级 Jaccard 相似度（无向量时的降级方案）。"""
        if not text1 or not text2:
            return 0.0
        if text1 == text2:
            return 1.0
        c1, c2 = set(text1), set(text2)
        union = len(c1 | c2)
        return len(c1 & c2) / union if union else 0.0

    async def _extract(
        self,
        recent_messages: list[MessageInDB],
        existing_facts: list[str],
        existing_entities: list | None = None,
    ) -> dict | None:
        """调用 LLM 提取焦点和新事实。

        Args:
            recent_messages: 最近的对话消息列表
            existing_facts: 已有的记忆事实内容列表（用于去重）
            existing_entities: 已有的实体列表（用于消歧，让 LLM 复用已知实体名）
        """
        # 只取最近 6 条消息用于提取（减少 token 消耗）
        messages_for_extract = recent_messages[-6:]
        if not messages_for_extract:
            return None

        # 构建对话文本：每行带该消息自身的绝对日期（时间基点 = 消息时刻，非抽取时刻）
        # 抽取为异步后台任务，recent_messages 可能是昨晚的消息；若只给“当前日期”，
        # 用户昨晚说“今天上班好累”会被 LLM 解析成今天而非昨天（借 Graphiti episode reference timestamp）
        tz = timezone(timedelta(hours=settings.default_timezone_offset))
        now = datetime.now(tz)
        dialog_lines: list[str] = []
        for msg in messages_for_extract:
            role_label = "用户" if msg.role == "user" else "AI"
            created = getattr(msg, "created_at", None)
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                msg_day = created.astimezone(tz).strftime("%Y-%m-%d %H:%M")
                dialog_lines.append(f"[{msg_day}] {role_label}: {msg.content}")
            else:
                dialog_lines.append(f"{role_label}: {msg.content}")
        dialog_text = "\n".join(dialog_lines)

        # 构建已有事实描述
        if existing_facts:
            facts_text = "\n".join(f"- {f}" for f in existing_facts)
        else:
            facts_text = "（暂无已知事实）"

        # 构建已知实体清单（让 LLM 优先复用，避免重复建实体）
        entities_text = ""
        if existing_entities:
            entity_lines = []
            for ent in existing_entities[:20]:  # 最多注入 20 个，控制 token
                line = f"- {ent.name}"
                if ent.aliases:
                    line += f"（别名：{'、'.join(ent.aliases)}）"
                if ent.relation_to_user:
                    line += f"：用户的{ent.relation_to_user}"
                entity_lines.append(line)
            if entity_lines:
                entities_text = (
                    "\n\n已知的实体（提取时优先复用这些名称，避免重复建实体）：\n"
                    + "\n".join(entity_lines)
                )

        # 注入当前日期与时间基点规则：明确告知“下面每条消息的 [日期] 才是该句话的基准”
        date_hint = (
            f"\n\n当前日期：{now.strftime('%Y年%m月%d日')}（这是提取执行时刻，不是对话发生时刻）\n"
            "注意：下列对话中每条消息前的 [日期 时:分] 是该消息实际发生的时间。\n"
            '解析"今天/昨天/明天"等相对时间词时，必须以该条消息自己的日期为基准，'
            "而非上面的当前日期。"
        )

        system_prompt = _EXTRACT_SYSTEM_PROMPT.format(
            existing_facts=facts_text
        ) + entities_text + date_hint
        # 实体关系提取（始终启用）
        system_prompt += _ENTITIES_ADDENDUM
        # 事件提取同样始终启用：它与事实/实体共用同一次 LLM 调用，边际成本只是一段提示词；
        # 真正该门控的是"是否据此主动发消息"（由 proactive_event_enabled 决定），
        # 否则开关从关到开之后事件库仍是空的
        system_prompt += _EVENTS_ADDENDUM
        # 拼接完整输出格式说明（事实 + 实体 + 事件三类字段）
        system_prompt += (
            "\n\n完整输出格式：\n" + _build_full_output_format(True)
        )

        provider = get_llm_provider("focus")
        raw_response = await provider.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": dialog_text},
            ],
            temperature=settings.focus_temperature,
            max_tokens=settings.focus_max_tokens,
            response_format={"type": "json_object"},
        )

        # 解析 JSON 响应
        return self._parse_response(raw_response)

    @staticmethod
    def _parse_response(raw: str) -> dict | None:
        """解析 LLM 返回的 JSON 响应，容错处理。"""
        raw = raw.strip()
        # 移除可能的 markdown 代码块标记
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )
        try:
            result = json.loads(raw)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            logger.warning("Failed to parse focus extraction response: %s", raw[:200])
        return None

    @staticmethod
    def _classify_fact(fact_text: str) -> str:
        """根据事实内容简单分类。"""
        # 安全类（过敏/禁忌/疾病）：归入 safety，进核心层常驻
        if any(kw in fact_text for kw in (
            "过敏", "不能吃", "禁用", "禁忌", "病史", "抑郁", "自伤", "自杀",
        )):
            return "safety"
        # 身份类（姓名/年龄/职业/所在地）：归入 core_identity，进核心层常驻
        if any(kw in fact_text for kw in (
            "我叫", "名字是", "今年", "岁", "职业", "工作在", "住在", "来自",
        )):
            return "core_identity"
        # 基于关键词的轻量分类
        if any(kw in fact_text for kw in ("喜欢", "爱好", "偏好", "最爱", "讨厌")):
            return "preference"
        if any(kw in fact_text for kw in ("朋友", "家人", "父母", "男友", "女友", "同事", "老师")):
            return "relationship"
        if any(kw in fact_text for kw in ("压力", "焦虑", "开心", "难过", "情绪", "心情")):
            return "mood_pattern"
        if any(kw in fact_text for kw in ("工作", "考试", "毕业", "搬家", "旅行", "生病", "考研")):
            return "life_event"
        return "general"

    @staticmethod
    def _tier_for(category: str) -> "MemoryTier":
        """根据类别推导记忆分层。"""
        from app.models.service_memory import CORE_CATEGORIES
        if category in CORE_CATEGORIES:
            return "core"
        if category == "life_event":
            return "episodic"
        return "semantic"
