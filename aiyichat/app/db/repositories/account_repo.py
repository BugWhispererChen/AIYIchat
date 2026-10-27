"""账号的数据库访问层。"""

from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import get_database
from app.db.repositories.media_repo import (
    MediaRepository,
    media_ids_from_message_docs,
)
from app.models.account import AccountCreate, AccountInDB, AccountUpdate


class AccountRepository:
    """管理账号的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().accounts

    async def find_by_id(self, account_id: str) -> Optional[AccountInDB]:
        """根据 ID 查询账号。"""
        doc = await self._collection().find_one({"id": account_id})
        if doc is None:
            return None
        return AccountInDB.from_mongo(doc)

    async def create(self, account: AccountCreate) -> AccountInDB:
        """创建新账号。"""
        now = datetime.now(timezone.utc)
        doc = account.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return AccountInDB.from_mongo(doc)

    async def update(
        self, account_id: str, update: AccountUpdate
    ) -> Optional[AccountInDB]:
        """更新账号信息。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(account_id)
        update_data["updated_at"] = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"id": account_id}, {"$set": update_data}
        )
        return await self.find_by_id(account_id)

    async def delete(self, account_id: str) -> bool:
        """删除账号。"""
        result = await self._collection().delete_one({"id": account_id})
        return result.deleted_count > 0

    async def delete_cascade(
        self,
        account_id: str,
        *,
        credential_id: str | None = None,
    ) -> dict[str, int]:
        """级联删除账号及其关联数据（凭据→周边记录→媒体/快照→消息→对话→记忆→服务→身份→Bot→额度→角色→账号）。

        与 C 端自助注销共用本方法，后台删账号不得再留下网页凭据、会话、订单、主动记录、
        安全事件、注入快照、媒体这 7 个集合的孤儿文档——它们都装着用户可见内容。

        两条不变量（改动时勿回退）：
        - **预扫描必须早于任何删除**：identities 一删就取不到 identity_id，
          messages 一删就反解不出媒体 ID（media 文档不记归属，只能靠引用方定位）。
        - **凭据最先删**：用户名当场释放、旧令牌立即失效，
          中途失败不会留下"数据删了一半却仍可登录"的半删账号。
        """
        db = get_database()
        stats: dict[str, int] = {}

        # 0. 预扫描：后续每一步都要用的 ID 集合
        identity_ids: list[str] = [
            doc["id"]
            async for doc in db.identities.find({"account_id": account_id}, {"id": 1})
        ]

        # 微信上下文 token 按 bot_id 存：bot 一删就查不到归属，必须在预扫描阶段取 ID
        bot_ids: list[str] = [
            doc["id"]
            async for doc in db.bot_configs.find({"account_id": account_id}, {"id": 1})
        ]
        # 私有角色定妆照的媒体只能从角色文档反解，删角色后就找不到归属
        private_portrait_urls: list[str] = [
            doc["reference_image_url"]
            async for doc in db.profiles.find(
                {"visibility": "private", "account_id": account_id},
                {"reference_image_url": 1},
            )
            if doc.get("reference_image_url")
        ]

        svc_docs: list[dict[str, Any]] = [
            doc
            async for doc in db.services.find(
                {"account_id": account_id}, {"id": 1, "share_group_id": 1}
            )
        ]
        svc_ids: list[str] = [doc["id"] for doc in svc_docs]
        # 注入快照按 context_key 存（共享组优先于服务自身 id），不是 service.id
        context_keys: list[str] = list(
            {doc.get("share_group_id") or doc["id"] for doc in svc_docs}
        )

        # 会话同时按 service_id 与 identity_id 收，后者能捞出不挂在任何服务上的孤儿会话
        conv_filters: list[dict[str, Any]] = []
        if svc_ids:
            conv_filters.append({"service_id": {"$in": svc_ids}})
        if identity_ids:
            conv_filters.append({"identity_id": {"$in": identity_ids}})
        conv_ids: set[str] = set()
        if conv_filters:
            conv_ids = {
                str(doc["_id"])
                async for doc in db.conversations.find({"$or": conv_filters}, {"_id": 1})
            }

        media_ids: list[str] = [
            mid
            for url in private_portrait_urls
            for mid in media_ids_from_message_docs([{"media_url": url}])
        ]
        if conv_ids:
            msg_docs: list[dict[str, Any]] = [
                doc
                async for doc in db.messages.find(
                    {"conversation_id": {"$in": list(conv_ids)}}, {"media_url": 1}
                )
            ]
            media_ids = media_ids_from_message_docs(msg_docs)

        cred_ids: list[str] = [
            doc["id"]
            async for doc in db.web_credentials.find(
                {"account_id": account_id}, {"id": 1}
            )
        ]
        if credential_id and credential_id not in cred_ids:
            cred_ids.append(credential_id)

        # 1. 网页会话与凭据（最先删，见上面第二条不变量）
        stats["web_sessions"] = (
            await db.web_sessions.delete_many({"credential_id": {"$in": cred_ids}})
        ).deleted_count if cred_ids else 0
        stats["web_credentials"] = (
            await db.web_credentials.delete_many({"id": {"$in": cred_ids}})
        ).deleted_count if cred_ids else 0

        # 2. 主动关怀记录 / 订单台账 / 安全事件（事件里带身份输入摘要，属个人数据）
        stats["proactive_records"] = (
            await db.proactive_records.delete_many(
                {"identity_id": {"$in": identity_ids}}
            )
        ).deleted_count if identity_ids else 0
        stats["orders"] = (
            await db.orders.delete_many({"account_id": account_id})
        ).deleted_count
        stats["safety_events"] = (
            await db.safety_events.delete_many({"account_id": account_id})
        ).deleted_count

        # 3. 媒体与注入快照：快照存着本轮 AI 回复与被注入的记忆正文，
        #    不删就等于给注销过的账号留了一份完整副本
        stats["media"] = await MediaRepository().delete_by_ids(media_ids)
        snapshot_count = 0
        if context_keys:
            snapshot_count += (await db.injection_snapshots.delete_many(
                {"service_id": {"$in": context_keys}}
            )).deleted_count
        if conv_ids:
            snapshot_count += (await db.injection_snapshots.delete_many(
                {"conversation_id": {"$in": list(conv_ids)}}
            )).deleted_count
        stats["injection_snapshots"] = snapshot_count

        # 4. 消息与对话
        stats["messages"] = (
            await db.messages.delete_many({"conversation_id": {"$in": list(conv_ids)}})
        ).deleted_count if conv_ids else 0
        stats["conversations"] = (
            await db.conversations.delete_many({"$or": conv_filters})
        ).deleted_count if conv_filters else 0

        # 1b. 批量删除所有服务的长期记忆：记忆按 context_key（共享组优先）存储，
        # 同时保留 service.id 一并清理，兼容共享组功能上线前按服务 ID 落库的存量文档。
        memory_keys = list(set(svc_ids) | set(context_keys))
        if memory_keys:
            stats["service_memories"] = (await db.service_memories.delete_many(
                {"service_id": {"$in": memory_keys}}
            )).deleted_count
        else:
            stats["service_memories"] = 0

        # 2. 删除服务
        stats["services"] = (await db.services.delete_many({"account_id": account_id})).deleted_count
        # 3. 删除身份
        stats["identities"] = (await db.identities.delete_many({"account_id": account_id})).deleted_count
        # 4. 删除 Bot（连同其微信上下文 token：token 里存着对方与机器人的会话凭据）
        stats["wechat_context_tokens"] = (
            await db.wechat_context_tokens.delete_many({"bot_id": {"$in": bot_ids}})
        ).deleted_count if bot_ids else 0
        stats["bots"] = (await db.bot_configs.delete_many({"account_id": account_id})).deleted_count
        # 5. 删除额度
        stats["quotas"] = (await db.account_quotas.delete_many({"account_id": account_id})).deleted_count
        # 6. 删除聊天记录克隆语料与反馈（片段含原聊天上下文，必须随账号清除）
        stats["clone_episodes"] = (
            await db.clone_episodes.delete_many({"account_id": account_id})
        ).deleted_count
        stats["clone_feedback"] = (
            await db.clone_feedback.delete_many({"account_id": account_id})
        ).deleted_count
        stats["clone_datasets"] = (
            await db.clone_datasets.delete_many({"account_id": account_id})
        ).deleted_count
        # 7. 删除私有角色
        stats["private_profiles"] = (
            await db.profiles.delete_many({"visibility": "private", "account_id": account_id})
        ).deleted_count
        # 8. 删除账号本身
        stats["account"] = (await self._collection().delete_one({"id": account_id})).deleted_count

        return stats

    async def list_all(
        self, skip: int = 0, limit: int = 50
    ) -> list[AccountInDB]:
        """分页查询账号列表。"""
        cursor = (
            self._collection()
            .find()
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [AccountInDB.from_mongo(doc) async for doc in cursor]

    async def search(self, q: str, limit: int = 50) -> list[AccountInDB]:
        """按名称或备注搜索账号。"""
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            self._collection()
            .find({"$or": [{"name": regex}, {"remark": regex}]})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [AccountInDB.from_mongo(doc) async for doc in cursor]

    async def count(self) -> int:
        """统计账号总数。"""
        return await self._collection().count_documents({})
