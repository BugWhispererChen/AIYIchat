"""渠道删除/退出登录时的协议旁路状态清理。

这些集合不在服务级数据里，只按 bot_id 归属：渠道行删除后就再也定位不到，
必须在删除前后统一清理，否则消息映射、主动消息授权、引用正文和同步游标
会作为无主数据长期留在库里。
"""

from app.db.mongo import get_database


async def cleanup_bot_side_state(
    bot_id: str, *, include_quote_cache: bool = True
) -> dict[str, int]:
    """按 bot_id 清理渠道旁路集合；返回各类删除数量。"""
    if not bot_id:
        return {}
    db = get_database()
    stats: dict[str, int] = {}
    stats["wechat_context_tokens"] = (
        await db.wechat_context_tokens.delete_many({"bot_id": bot_id})
    ).deleted_count
    stats["wechat_sync_state"] = (
        await db.wechat_sync_state.delete_many({"bot_id": bot_id})
    ).deleted_count
    if include_quote_cache:
        stats["wechat_quote_cache"] = (
            await db.wechat_quote_cache.delete_many({"bot_id": bot_id})
        ).deleted_count
    stats["qq_message_bindings"] = (
        await db.qq_message_bindings.delete_many({"bot_id": bot_id})
    ).deleted_count
    stats["qq_proactive_authorizations"] = (
        await db.qq_proactive_authorizations.delete_many({"bot_id": bot_id})
    ).deleted_count
    return stats
