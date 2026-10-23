"""C 端账号中心的业务编排：资料、会话、额度、数据与隐私、注销。

设计要点：
- 归属一律由会话令牌反查（`require_web_account` 传入 credential/identity），本模块不接受
  客户端给的 account_id / identity_id；
- 长期记忆与对话按 `ServiceInDB.context_key` 归属：设了共享组时该 key 是组 ID，
  多个身份共用一份上下文，任何"清空"动作都必须先确认组内没有别人的服务；
- 注销按"先锁账号、再删数据"的顺序执行：凭据一旦删除，用户名立即释放且旧令牌全部失效，
  后续级联即便失败也不会让用户继续以半删状态对话，失败详情进日志供管理员补删。
  该顺序现在由 `AccountRepository.delete_cascade` 内部保证，注销与后台删账号共用同一份实现。
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bson import ObjectId

from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.injection_snapshot_repo import InjectionSnapshotRepository
from app.db.repositories.media_repo import (
    MediaRepository,
    media_ids_from_message_docs,
)
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.order_repo import OrderRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.web_credential_repo import WebCredentialRepository
from app.db.repositories.web_session_repo import WebSessionRepository
from app.models.identity import IdentityInDB, IdentityUpdate
from app.models.order import OrderInDB
from app.models.profile import PromptProfileInDB
from app.models.quota import AccountQuotaInDB, QuotaEntry
from app.models.service import ServiceInDB
from app.models.service_memory import ServiceMemoryInDB
from app.models.web_credential import WebCredentialInDB, normalize_username
from app.models.web_session import WebSessionInDB
from app.services.auth_service import AuthError

logger = logging.getLogger(__name__)

# 资料字段上限：昵称进对话上下文与后台列表，自我描述直接进 prompt，均需硬上限防文档膨胀
_MAX_NICKNAME = 30
_MAX_DESCRIPTION = 600
# 导出消息条数上限：单账号可能有数万条消息，全量导出会把内存与响应体打爆
_MAX_EXPORT_MESSAGES = 20000
# 记忆事实展示上限：账号中心只做"看得见 + 可清空"，不做记忆编辑器
_MAX_VISIBLE_FACTS = 200

# 控制字符（不含常规换行/制表）会破坏列表渲染与 prompt 结构
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _as_utc(value: datetime) -> datetime:
    """把 Mongo 读回的 naive 时间统一按 UTC 归一（存量数据不带时区）。"""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str:
    """序列化为 ISO8601（前端按本地时区渲染），空值返回空串。"""
    return _as_utc(value).isoformat() if value is not None else ""


def _clean_text(raw: str, limit: int, empty_code: str, empty_msg: str,
                long_code: str, long_msg: str) -> str:
    """通用文本清洗：去控制字符与首尾空白，再校验非空与长度。"""
    text = _CONTROL_CHARS_RE.sub("", raw or "").strip()
    if not text:
        raise AuthError(empty_code, empty_msg, 400)
    if len(text) > limit:
        raise AuthError(long_code, long_msg, 400)
    return text


def validate_timezone(raw: str) -> str:
    """校验 IANA 时区名；空串表示"跟随系统默认"，非法名称直接拒绝。"""
    name = (raw or "").strip()
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 名称非法与 tzdata 缺失同样不可用
        raise AuthError("invalid_timezone", "时区名称无效，例如 Asia/Shanghai", 400) from None
    return name


# ── 资料 ────────────────────────────────────────────────


def profile_payload(credential: WebCredentialInDB, identity: IdentityInDB) -> dict[str, Any]:
    """组装账号中心所需的身份资料（不含任何凭据字段）。"""
    return {
        "username": credential.username,
        "nickname": identity.nickname,
        "self_description": identity.self_description,
        "timezone": identity.timezone,
        "status": credential.status,
        "created_at": _iso(credential.created_at),
        "last_active_at": _iso(identity.last_active_at),
    }


async def update_profile(
    identity_id: str, *, nickname: str, self_description: str, timezone_name: str
) -> dict[str, Any]:
    """更新昵称 / 自我描述 / 时区：只允许这三个字段进入 IdentityUpdate。

    用户名与归属账号不可改；platform/platform_id 是引擎路由依据，接受客户端入参会
    让网页身份被改绑到别人的 openid 上。
    """
    clean_nickname = _clean_text(
        nickname, _MAX_NICKNAME,
        "invalid_nickname", "昵称不能为空",
        "invalid_nickname", f"昵称最长 {_MAX_NICKNAME} 字",
    )
    clean_description = (self_description or "").strip()
    clean_description = _CONTROL_CHARS_RE.sub("", clean_description)
    if len(clean_description) > _MAX_DESCRIPTION:
        raise AuthError("invalid_description", f"自我描述最长 {_MAX_DESCRIPTION} 字", 400)
    tz = validate_timezone(timezone_name)

    identity = await IdentityRepository().update(
        identity_id,
        IdentityUpdate(nickname=clean_nickname, self_description=clean_description, timezone=tz),
    )
    if identity is None:
        raise AuthError("identity_not_found", "账号数据异常，请重新登录", 401)
    credential = await WebCredentialRepository().find_by_identity_id(identity.id)
    if credential is None:
        raise AuthError("identity_not_found", "账号数据异常，请重新登录", 401)
    logger.info("网页身份 %s 更新账号资料（昵称=%s，时区=%s）", credential.username,
                clean_nickname, tz or "系统默认")
    return profile_payload(credential, identity)


# ── 登录设备 ────────────────────────────────────────────


def mask_ip(ip: str) -> str:
    """IP 脱敏：保留定位到网段的前缀，避免设备列表泄露完整来源地址。"""
    if not ip or ip == "unknown":
        return "未知"
    if ":" in ip:  # IPv6：保留前两组
        return ":".join(ip.split(":")[:2]) + ":*"
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return ip[:6] + "*"


def device_label(user_agent: str) -> str:
    """把 User-Agent 压成一句人话，够用户分辨"这是我哪台设备"即可。"""
    ua = (user_agent or "").lower()
    if not ua:
        return "未知设备"
    os_name = (
        "Windows" if "windows" in ua
        else "macOS" if ("mac os" in ua or "macosx" in ua or "macintosh" in ua)
        else "iPhone/iPad" if ("iphone" in ua or "ipad" in ua)
        else "Android" if "android" in ua
        else "Linux" if "linux" in ua
        else "其他系统"
    )
    browser = (
        "Edge" if "edg/" in ua or "edge/" in ua
        else "Chrome" if "chrome" in ua or "crios" in ua
        else "Firefox" if "firefox" in ua or "fxios" in ua
        else "微信" if "micromessenger" in ua
        else "Safari" if "safari" in ua
        else "其他浏览器"
    )
    return f"{browser} · {os_name}"


def sessions_payload(sessions: list[WebSessionInDB], current_session_id: str) -> list[dict[str, Any]]:
    """会话列表：过滤已过期、标出当前设备，绝不返回令牌。

    排序在模型对象上按 last_activity 倒序完成后再生成字典：dict 的取值是 object，
    直接拿它当 sort key 既丢类型也易错。
    """
    ordered = sorted(
        (s for s in sessions if not s.expired),
        key=lambda s: _as_utc(s.last_activity),
        reverse=True,
    )
    return [
        {
            "id": s.id,
            "current": s.id == current_session_id,
            "ip": mask_ip(s.ip),
            "device": device_label(s.user_agent),
            "user_agent": (s.user_agent or "")[:120],
            "created_at": _iso(s.created_at),
            "last_activity": _iso(s.last_activity),
            "expires_at": _iso(s.expires_at),
        }
        for s in ordered
    ]


async def revoke_session(credential_id: str, session_id: str, current_session_id: str) -> None:
    """下线指定会话；当前会话必须走"退出登录"，否则前端会拿不到可撤销的凭证。"""
    if session_id == current_session_id:
        raise AuthError("cannot_revoke_current", "当前设备请使用「退出登录」", 400)
    if not await WebSessionRepository().delete_by_id(credential_id, session_id):
        raise AuthError("session_not_found", "该设备不在线或已下线", 404)


# ── 额度 ────────────────────────────────────────────────


def _quota_entry(entry: QuotaEntry) -> dict[str, Any]:
    """单项额度：剩余量、使用百分比与到期状态（过期后剩余不可用）。"""
    now = datetime.now(timezone.utc)
    expired = entry.expires_at is not None and _as_utc(entry.expires_at) <= now
    remaining = 0 if expired else max(0, entry.limit - entry.used)
    percent = round(entry.used / entry.limit * 100, 1) if entry.limit > 0 else 0.0
    return {
        "limit": entry.limit,
        "used": entry.used,
        "remaining": remaining,
        "percent": min(100.0, percent),
        "expires_at": _iso(entry.expires_at),
        "expired": expired,
    }


def quota_payload(quota: Optional[AccountQuotaInDB]) -> Optional[dict[str, Any]]:
    """三类额度（文字按 Token、语音/图片按次/张）。额度记录缺失时返回 None 供前端提示。"""
    if quota is None:
        return None
    return {
        "text": _quota_entry(quota.text_quota),
        "voice": _quota_entry(quota.voice_quota),
        "image": _quota_entry(quota.image_quota),
    }


def order_payload(orders: list[OrderInDB]) -> list[dict[str, Any]]:
    """发放记录白名单字段：operator / account_id / account_name 属运营信息，不下发。"""
    return [
        {
            "template_name": o.template_name or "自定义开通",
            "text_limit": o.text_limit,
            "voice_limit": o.voice_limit,
            "image_limit": o.image_limit,
            "validity_days": o.validity_days,
            "amount": o.amount,
            "remark": o.remark,
            "created_at": _iso(o.created_at),
        }
        for o in orders
    ]


# ── 数据与隐私 ──────────────────────────────────────────


async def _profile_name(profile_ref: str) -> str:
    """解析服务角色名（历史数据里 profile_id 可能是 ObjectId 或 slug）。"""
    repo = ProfileRepository()
    profile = await repo.find_by_id(profile_ref) if ObjectId.is_valid(profile_ref) else None
    if profile is None:
        profile = await repo.find_by_slug(profile_ref)
    return profile.name if profile else "（角色已失效）"


async def _group_is_exclusive(service: ServiceInDB, identity_id: str) -> bool:
    """共享组是否只服务于当前身份：组内有他人服务时，其对话与记忆不能连带删除。"""
    if not service.share_group_id:
        return True
    peers = await ServiceRepository().list_by_share_group(service.share_group_id)
    return all(peer.identity_id == identity_id for peer in peers)


async def _owned_services(identity: IdentityInDB) -> list[ServiceInDB]:
    """取当前身份名下的服务（对话服务），按创建时间正序。"""
    services = await ServiceRepository().list_by_identity(identity.id)
    services.sort(key=lambda s: s.created_at)
    return services


async def data_overview(identity: IdentityInDB) -> dict[str, Any]:
    """数据概览：对话/消息/记忆总量与按服务的分布。"""
    services = await _owned_services(identity)
    conversations = await ConversationRepository().list_by_identity(identity.id)
    conv_by_key: dict[str, list[Any]] = {}
    total_messages = 0
    for conv in conversations:
        conv_by_key.setdefault(conv.service_id, []).append(conv)
        total_messages += conv.message_count

    memory_repo = ServiceMemoryRepository()
    rows: list[dict[str, Any]] = []
    fact_total = 0
    for svc in services:
        memory = await memory_repo.find_by_service(svc.context_key)
        facts = len(memory.facts) if memory else 0
        fact_total += facts
        convs = conv_by_key.get(svc.context_key, [])
        rows.append(
            {
                "id": svc.id,
                "name": svc.name,
                "profile_name": await _profile_name(svc.profile_id),
                "is_active": svc.is_active,
                "conversation_count": len(convs),
                "message_count": sum(c.message_count for c in convs),
                "memory_facts": facts,
                "shared": bool(svc.share_group_id) and not await _group_is_exclusive(svc, identity.id),
                "memory_updated_at": _iso(memory.updated_at) if memory else "",
            }
        )
    return {
        "counts": {
            "services": len(services),
            "conversations": len(conversations),
            "messages": total_messages,
            "facts": fact_total,
        },
        "services": rows,
    }


async def memories_payload(identity: IdentityInDB) -> list[dict[str, Any]]:
    """按服务列出长期记忆明细（排除向量与评分内部字段）。"""
    repo = ServiceMemoryRepository()
    items: list[dict[str, Any]] = []
    for svc in await _owned_services(identity):
        memory = await repo.find_by_service(svc.context_key)
        if memory is None:
            continue
        items.append(_memory_item(svc, memory))
    return items


def _memory_item(service: ServiceInDB, memory: ServiceMemoryInDB) -> dict[str, Any]:
    """记忆条目渲染：facts 截断展示，实体只留人名与关系。"""
    return {
        "service_id": service.id,
        "service_name": service.name,
        "shared": bool(service.share_group_id),
        "current_topic": memory.current_topic,
        "topic_history": memory.topic_history[-10:],
        "updated_at": _iso(memory.updated_at),
        "facts_total": len(memory.facts),
        "facts": [
            {
                "content": fact.content,
                "category": fact.category,
                "tier": fact.tier,
                "created_at": _iso(fact.created_at),
            }
            for fact in memory.facts[:_MAX_VISIBLE_FACTS]
        ],
        "entities": [
            {
                "name": entity.name,
                "relation_to_identity": entity.relation_to_identity,
                "entity_type": entity.entity_type,
            }
            for entity in memory.entities[:50]
        ],
    }


async def clear_conversations(identity: IdentityInDB) -> dict[str, int]:
    """清空本人全部对话记录：消息 → 注入快照 → 对话 → 关联媒体。

    按 identity_id 取数而非按 service_id，共享组下别人的会话不会被误删。
    """
    conv_repo = ConversationRepository()
    conv_ids = await conv_repo.list_ids_by_identity(identity.id)
    if not conv_ids:
        return {"conversations": 0, "messages": 0, "snapshots": 0, "media": 0}
    messages = await MessageRepository().list_by_conversation_ids(conv_ids)
    media_ids = media_ids_from_message_docs(messages)
    msg_deleted = await MessageRepository().delete_by_conversation_ids(conv_ids)
    snap_deleted = await InjectionSnapshotRepository().delete_by_conversation_ids(conv_ids)
    conv_deleted = await conv_repo.delete_by_ids(conv_ids)
    media_deleted = await MediaRepository().delete_by_ids(media_ids)
    logger.info(
        "网页身份 %s 清空对话记录：会话 %d / 消息 %d / 快照 %d / 媒体 %d",
        identity.nickname or identity.id, conv_deleted, msg_deleted, snap_deleted, media_deleted,
    )
    return {
        "conversations": conv_deleted,
        "messages": msg_deleted,
        "snapshots": snap_deleted,
        "media": media_deleted,
    }


async def clear_memory(identity: IdentityInDB, service: ServiceInDB) -> dict[str, Any]:
    """清空某个服务的长期记忆（含其注入快照）；共享组含他人服务时拒绝。"""
    if not await _group_is_exclusive(service, identity.id):
        raise AuthError(
            "shared_context", "该服务与其他身份共享上下文，请联系管理员处理", 409
        )
    key = service.context_key
    memory = await ServiceMemoryRepository().find_by_service(key)
    facts = len(memory.facts) if memory else 0
    deleted = await ServiceMemoryRepository().delete_by_service(key)
    snapshots = await InjectionSnapshotRepository().delete_by_service_ids([key])
    logger.info(
        "网页身份 %s 清空服务「%s」的记忆：事实 %d，快照 %d",
        identity.nickname or identity.id, service.name, facts, snapshots,
    )
    return {"service_id": service.id, "facts": facts, "deleted": bool(deleted), "snapshots": snapshots}


def _owned_profile_payload(profile: PromptProfileInDB) -> dict[str, Any]:
    """导出用的自建角色：只取人设内容，剔除音色、定妆照与运营字段。"""
    return {
        "slug": profile.slug,
        "name": profile.name,
        "category": profile.category,
        "is_active": profile.is_active,
        "verbosity": profile.verbosity,
        "persona": profile.persona.model_dump(),
        "language_style": profile.language_style.model_dump(),
        "personality": profile.personality.model_dump(),
        "dialogue_strategy": profile.dialogue_strategy.model_dump(),
        "meta_instructions": profile.meta_instructions,
        "created_at": _iso(profile.created_at),
        "updated_at": _iso(profile.updated_at),
    }


async def export_payload(
    credential: WebCredentialInDB, identity: IdentityInDB
) -> tuple[dict[str, Any], bool]:
    """组装个人数据包：账号资料、额度、发放记录、服务、自建角色、对话与记忆。

    Returns:
        (导出数据, 消息是否被截断)
    """
    account_id = identity.account_id
    quota = await QuotaRepository().find_by_account(account_id)
    orders = await OrderRepository().list_recent(account_id=account_id, limit=50)
    services = await _owned_services(identity)
    profiles = [
        _owned_profile_payload(p)
        for p in await ProfileRepository().list_by_account(account_id)
        if p.visibility == "private" and not p.is_draft
    ]

    conv_repo = ConversationRepository()
    conversations = await conv_repo.list_by_identity(identity.id)
    messages = await MessageRepository().list_by_conversation_ids(
        [c.id for c in conversations], limit=_MAX_EXPORT_MESSAGES
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        grouped.setdefault(msg.conversation_id, []).append(
            {
                "role": msg.role,
                "content": msg.content,
                "content_type": msg.content_type,
                "media_url": msg.media_url or "",
                "emotion_tag": msg.emotion_tag or "",
                "created_at": _iso(msg.created_at),
            }
        )
    conv_items = [
        {
            "id": conv.id,
            "service_id": conv.service_id,
            "profile_id": conv.profile_id,
            "title": conv.title,
            "message_count": conv.message_count,
            "created_at": _iso(conv.created_at),
            "last_message_at": _iso(conv.last_message_at),
            "messages": grouped.get(conv.id, []),
        }
        for conv in conversations
    ]

    memory_repo = ServiceMemoryRepository()
    memory_items: list[dict[str, Any]] = []
    for svc in services:
        memory = await memory_repo.find_by_service(svc.context_key)
        if memory is None:
            continue
        item = _memory_item(svc, memory)
        item["facts"] = [
            {
                "content": fact.content,
                "category": fact.category,
                "tier": fact.tier,
                "created_at": _iso(fact.created_at),
                "valid_at": _iso(fact.valid_at),
                "invalid_at": _iso(fact.invalid_at),
            }
            for fact in memory.facts
        ]
        item["pending_events"] = [
            {"content": ev.content, "due_at": _iso(ev.due_at), "followed_up": ev.followed_up}
            for ev in memory.pending_events
        ]
        memory_items.append(item)

    payload = {
        "schema_version": 1,
        "exported_at": _iso(datetime.now(timezone.utc)),
        "account": {
            "username": credential.username,
            "nickname": identity.nickname,
            "self_description": identity.self_description,
            "timezone": identity.timezone,
            "registered_at": _iso(credential.created_at),
            "last_active_at": _iso(identity.last_active_at),
            "consent": {
                "terms_version": credential.consent.terms_version,
                "terms_accepted_at": _iso(credential.consent.terms_accepted_at),
                "privacy_version": credential.consent.privacy_version,
                "privacy_accepted_at": _iso(credential.consent.privacy_accepted_at),
            },
        },
        "quota": quota_payload(quota),
        "orders": order_payload(orders),
        "services": [
            {
                "id": svc.id,
                "name": svc.name,
                "profile_id": svc.profile_id,
                "is_active": svc.is_active,
                "timezone": svc.timezone,
                "created_at": _iso(svc.created_at),
            }
            for svc in services
        ],
        "profiles": profiles,
        "conversations": conv_items,
        "memories": memory_items,
    }
    return payload, len(messages) >= _MAX_EXPORT_MESSAGES


# ── 注销 ────────────────────────────────────────────────


async def delete_account(
    credential: WebCredentialInDB, identity: IdentityInDB
) -> dict[str, Any]:
    """立即硬删除账号：凭据 → 周边记录 → 账号级联。

    安全前置：账号下若还挂着其他身份（QQ / 微信或管理员补建的网页身份）或其他身份的服务，
    一律拒绝——级联删除按 account_id 清扫，会把它们的数据一起抹掉。

    Raises:
        AuthError: 存在其他关联身份/服务（409），或级联删除异常（500）
    """
    account_id = identity.account_id
    identities = await IdentityRepository().list_by_account(account_id)
    if len(identities) != 1 or identities[0].id != identity.id:
        raise AuthError(
            "linked_identity_exists",
            "该账号还关联了其他渠道身份，请联系管理员处理后再注销",
            409,
        )
    services = await ServiceRepository().list_by_account(account_id)
    if any(svc.identity_id != identity.id for svc in services):
        raise AuthError(
            "linked_identity_exists",
            "该账号还关联了其他身份的服务，请联系管理员处理后再注销",
            409,
        )

    stats: dict[str, int] = {}
    try:
        # 单一口径：预扫描、凭据先删（用户名当场释放、旧令牌立即失效）、7 个周边集合
        # 与主链路一并由 delete_cascade 内部保证。注销路径不再自己重写一遍删除序列——
        # 正是"两份实现各删各的"让后台删账号漏掉了那些集合。
        stats.update(
            await AccountRepository().delete_cascade(
                account_id, credential_id=credential.id, identity_id=identity.id
            )
        )
    except Exception as exc:  # noqa: BLE001 - 注销失败必须回 500 并留完整现场
        logger.exception(
            "账号注销中途失败，需管理员按 account_id 补删: username=%s account_id=%s stats=%s",
            credential.username, account_id, stats,
        )
        raise AuthError("delete_failed", "注销未完成，请联系管理员确认", 500) from exc

    logger.info(
        "网页身份 %s 自助注销完成: account_id=%s stats=%s",
        credential.username, account_id, stats,
    )
    return stats


async def find_owned_service(identity: IdentityInDB, service_id: str) -> ServiceInDB:
    """取回归本人的服务；不存在或属于他人一律 404（不区分，避免枚举他人服务 ID）。"""
    svc = await ServiceRepository().find_by_id(service_id)
    if svc is None or svc.account_id != identity.account_id or svc.identity_id != identity.id:
        raise AuthError("service_not_found", "服务不存在", 404)
    return svc


def normalize_confirm_username(raw: str, expected: str) -> None:
    """注销二次确认：输入的规范化用户名必须与登录身份一致。"""
    if normalize_username(raw or "") != expected:
        raise AuthError("username_mismatch", "输入的用户名与当前账号不一致", 400)
