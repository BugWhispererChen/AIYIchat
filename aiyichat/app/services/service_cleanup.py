"""删除对话服务时的个人数据清理。

服务被删除后，长期记忆与注入快照不再有任何入口可达；若继续留在库里，
既无法被账号中心展示，也会在账号注销时因服务行已消失而被漏删。
本模块只清理这两类 service 级数据，**不删除对话/消息/媒体**——现有产品
文案承诺"删除服务后对话历史仍保留"，由账号注销按身份统一清理。
"""

import logging
from collections.abc import Sequence

from app.db.repositories.injection_snapshot_repo import InjectionSnapshotRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.service import ServiceInDB

logger = logging.getLogger(__name__)


async def purge_service_memory(
    services: Sequence[ServiceInDB],
) -> dict[str, int]:
    """删除这批服务独占的长期记忆与注入快照。

    共享组内的记忆/快照仍可能被其他服务使用：只有当组内所有服务都在本次
    删除集合中时才清理，否则保留。返回各类删除数量，便于日志与测试断言。
    """
    if not services:
        return {"service_memories": 0, "injection_snapshots": 0}

    repo = ServiceRepository()
    deleting_ids = {svc.id for svc in services}
    checked_groups: set[str] = set()
    context_keys: set[str] = set()

    for svc in services:
        group_key = svc.share_group_id
        if not group_key:
            context_keys.add(svc.context_key)
            continue
        if group_key in checked_groups:
            continue
        checked_groups.add(group_key)
        peers = await repo.list_by_share_group(group_key)
        if all(peer.id in deleting_ids for peer in peers):
            context_keys.add(group_key)

    if not context_keys:
        return {"service_memories": 0, "injection_snapshots": 0}

    keys = sorted(context_keys)
    memories = await ServiceMemoryRepository().delete_by_service_ids(keys)
    snapshots = await InjectionSnapshotRepository().delete_by_service_ids(keys)
    logger.info(
        "删除服务清理记忆: services=%d, keys=%s, memories=%d, snapshots=%d",
        len(services),
        keys,
        memories,
        snapshots,
    )
    return {"service_memories": memories, "injection_snapshots": snapshots}
