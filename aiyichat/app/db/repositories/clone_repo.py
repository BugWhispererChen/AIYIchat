"""克隆语料、角色经历与反馈的数据访问层。"""

from datetime import datetime, timezone
from typing import Any

from app.db.mongo import get_database
from app.models.clone import (
    CloneDatasetInDB,
    CloneEpisodeInDB,
    CloneFeedbackInDB,
    CloneRoleMemory,
)


class CloneDatasetRepository:
    def _collection(self):
        return get_database().clone_datasets

    async def create(self, dataset: CloneDatasetInDB) -> CloneDatasetInDB:
        doc = dataset.model_dump()
        await self._collection().insert_one(doc)
        return dataset

    async def find_by_id(self, dataset_id: str) -> CloneDatasetInDB | None:
        doc = await self._collection().find_one({"id": dataset_id})
        return CloneDatasetInDB(**doc) if doc else None

    async def find_by_task(self, task_id: str) -> CloneDatasetInDB | None:
        doc = await self._collection().find_one({"task_id": task_id})
        return CloneDatasetInDB(**doc) if doc else None

    async def update(self, dataset_id: str, **fields: Any) -> None:
        await self._collection().update_one({"id": dataset_id}, {"$set": fields})

    async def activate(
        self,
        dataset_id: str,
        *,
        account_id: str,
        profile_id: str,
        version: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"id": dataset_id},
            {
                "$set": {
                    "status": "active",
                    "account_id": account_id,
                    "profile_id": profile_id,
                    "version": version,
                    "activated_at": now,
                    "expires_at": None,
                }
            },
        )

    async def next_version(self, profile_id: str) -> int:
        docs = await self._collection().find(
            {"profile_id": profile_id, "status": {"$in": ["active", "disabled"]}}
        ).to_list(length=None)
        return max((int(d.get("version", 0)) for d in docs), default=0) + 1

    async def disable_for_profile(self, profile_id: str) -> int:
        result = await self._collection().update_many(
            {"profile_id": profile_id, "status": "active"},
            {"$set": {"status": "disabled"}},
        )
        return result.modified_count

    async def mark_error(self, dataset_id: str, message: str) -> None:
        await self.update(dataset_id, status="error", analysis_error=message)

    async def delete_by_ids(self, dataset_ids: list[str]) -> int:
        if not dataset_ids:
            return 0
        result = await self._collection().delete_many({"id": {"$in": dataset_ids}})
        return result.deleted_count

    async def delete_by_account(self, account_id: str) -> int:
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def delete_by_profile(self, profile_id: str) -> int:
        result = await self._collection().delete_many({"profile_id": profile_id})
        return result.deleted_count

    async def set_role_memories(
        self, dataset_id: str, memories: list[CloneRoleMemory]
    ) -> None:
        await self.update(
            dataset_id,
            role_memories=[m.model_dump() for m in memories],
        )


class CloneEpisodeRepository:
    def _collection(self):
        return get_database().clone_episodes

    async def create_many(self, episodes: list[CloneEpisodeInDB]) -> int:
        if not episodes:
            return 0
        await self._collection().insert_many([e.model_dump() for e in episodes])
        return len(episodes)

    async def list_runtime(
        self,
        *,
        profile_id: str,
        clone_version: int,
        account_id: str,
        limit: int = 400,
    ) -> list[CloneEpisodeInDB]:
        query = {
            "profile_id": profile_id,
            "clone_version": clone_version,
            "account_id": account_id,
            "disabled": False,
        }
        docs = await (
            self._collection()
            .find(query)
            .sort("quality", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        return [CloneEpisodeInDB(**doc) for doc in docs]

    async def list_by_dataset(self, dataset_id: str) -> list[CloneEpisodeInDB]:
        docs = await self._collection().find(
            {"dataset_id": dataset_id}
        ).sort("started_at", 1).to_list(length=None)
        return [CloneEpisodeInDB(**doc) for doc in docs]

    async def update_embedding(
        self, episode_id: str, embedding: list[float]
    ) -> None:
        if embedding:
            await self._collection().update_one(
                {"id": episode_id}, {"$set": {"context_embedding": embedding}}
            )

    async def bind_dataset(
        self, dataset_id: str, *, account_id: str, profile_id: str, version: int
    ) -> int:
        result = await self._collection().update_many(
            {"dataset_id": dataset_id},
            {
                "$set": {
                    "account_id": account_id,
                    "profile_id": profile_id,
                    "clone_version": version,
                },
                "$unset": {"expires_at": ""},
            },
        )
        return result.modified_count

    async def disable(self, episode_id: str) -> bool:
        result = await self._collection().update_one(
            {"id": episode_id}, {"$set": {"disabled": True}}
        )
        return result.modified_count > 0

    async def delete_by_dataset_ids(self, dataset_ids: list[str]) -> int:
        if not dataset_ids:
            return 0
        result = await self._collection().delete_many(
            {"dataset_id": {"$in": dataset_ids}}
        )
        return result.deleted_count

    async def delete_by_account(self, account_id: str) -> int:
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def delete_by_profile(self, profile_id: str) -> int:
        result = await self._collection().delete_many({"profile_id": profile_id})
        return result.deleted_count


class CloneFeedbackRepository:
    def _collection(self):
        return get_database().clone_feedback

    async def create(self, feedback: CloneFeedbackInDB) -> CloneFeedbackInDB:
        await self._collection().insert_one(feedback.model_dump())
        return feedback

    async def list_recent(self, limit: int = 100) -> list[CloneFeedbackInDB]:
        docs = await (
            self._collection()
            .find({})
            .sort("created_at", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        return [CloneFeedbackInDB(**doc) for doc in docs]

    async def list_by_profile(
        self, profile_id: str, limit: int = 50
    ) -> list[CloneFeedbackInDB]:
        docs = await (
            self._collection()
            .find({"profile_id": profile_id})
            .sort("created_at", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        return [CloneFeedbackInDB(**doc) for doc in docs]

    async def set_status(self, feedback_id: str, status: str) -> bool:
        result = await self._collection().update_one(
            {"id": feedback_id}, {"$set": {"status": status}}
        )
        return result.modified_count > 0

    async def delete_by_account(self, account_id: str) -> int:
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def delete_by_profile(self, profile_id: str) -> int:
        result = await self._collection().delete_many({"profile_id": profile_id})
        return result.deleted_count
