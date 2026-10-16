"""验证脚本：对话时间感知（日期锚点 + 时刻/间隔语义 + 时间标签 + 跨天分隔符 + 记忆时间后缀）。

容器内运行：独立进程需先 connect_db + load_settings_from_db 拿到运行时配置。

  docker exec aiyichat-app-1 python /app/tools/test/verify_message_time.py

分两部分：
1. 结构验证（不依赖 LLM）：打印组装出的消息数组与 time_ctx 审计字段
2. 推理探针（依赖真实 LLM）：时间推理探针 + 陈旧事实探针，未配置模型时自动跳过
"""
import asyncio
from datetime import datetime, timedelta, timezone


async def main() -> None:
    from app.config import load_settings_from_db, settings
    from app.db.mongo import connect_db

    await connect_db()
    await load_settings_from_db()
    print(
        "config: "
        f"message_time_enabled={settings.message_time_enabled}, "
        f"gap_minutes={settings.message_time_gap_minutes}, "
        f"tz_offset={settings.default_timezone_offset}, "
        f"anchor={settings.time_anchor_enabled}, "
        f"idle_hint={settings.time_idle_hint_enabled}, "
        f"day_separator={settings.time_day_separator_enabled}, "
        f"tag_year={settings.message_time_tag_year_enabled}, "
        f"memory_suffix={settings.memory_inject_time_suffix}"
    )

    from app.db.repositories.profile_repo import ProfileRepository
    from app.llm.prompt_assembler import PromptAssembler, TimeAudit
    from app.models.message import MessageInDB
    from app.models.service_memory import MemoryFact, PendingEvent

    repo = ProfileRepository()
    active = await repo.list_active()
    profile = active[0] if active else None
    if profile is None:
        print("FAIL: no available profile")
        return

    now = datetime.now(timezone.utc)
    # 模拟：前晚聊天 -> 昨晚道晚安 -> 今早起床后继续发消息（跨两天会话间隔）
    history = [
        MessageInDB(_id="v0", conversation_id="verify", role="user",
                    content="今天上班好累",
                    created_at=now - timedelta(days=1, hours=11)),
        MessageInDB(_id="v1", conversation_id="verify", role="user",
                    content="晚安，我睡了",
                    created_at=now - timedelta(days=1, hours=2)),
        MessageInDB(_id="v2", conversation_id="verify", role="assistant",
                    content="晚安，好梦",
                    created_at=now - timedelta(days=1, hours=2)),
        MessageInDB(_id="v3", conversation_id="verify", role="user",
                    content="刚睡醒", created_at=now - timedelta(minutes=5)),
    ]

    # 记忆事实：一条 7 天前录入的一次性事件 + 一条长期偏好
    facts = [
        MemoryFact(content="用户8月26日有面试", category="life_event",
                   created_at=now - timedelta(days=7)),
        MemoryFact(content="用户喜欢喝咖啡", category="preference",
                   created_at=now - timedelta(days=200)),
    ]
    # 待跟进事件：一条已过去（陈旧）、一条即将发生
    events = [
        PendingEvent(content="用户8月26日有面试", due_at=now - timedelta(days=7)),
        PendingEvent(content="用户9月10日体检", due_at=now + timedelta(days=8)),
    ]

    from app.conversation.engine import ConversationEngine

    topic_context = ConversationEngine._render_pending_events(events)

    assembler = PromptAssembler()
    audit = TimeAudit()
    msgs = assembler.assemble(
        profile=profile, history=history, user_input="早上好",
        memory_facts=[f.content for f in facts], memory_fact_objs=facts,
        topic_context=topic_context, time_audit=audit,
    )

    print(f"\n=== 结构验证：total messages={len(msgs)} ===")
    for m in msgs:
        content = m["content"].replace("\n", " | ")
        print(f"[{m['role']}] {content[:160]}")

    print("\n=== time_ctx 审计字段 ===")
    print(
        f"anchor={audit.anchor or '-'} now={audit.now or '-'} tz={audit.tz or '-'} "
        f"idle={audit.idle or '-'} tagged={audit.tagged} separators={audit.separators}"
    )
    checks = {
        "锚点在 messages[0] 首个段落": msgs[0]["content"].startswith("【时间基准】"),
        "含当前时刻": any("【当前时刻】" in m["content"] for m in msgs),
        "含对话间隔": any("【对话间隔】" in m["content"] for m in msgs),
        "含跨天分隔符": audit.separators > 0,
        "含时间标签": audit.tagged > 0,
        "记忆带时间后缀": any("记录于" in m["content"] for m in msgs),
        "长期偏好标注": any("长期信息" in m["content"] for m in msgs),
        "待跟进事件注入": any("【待跟进的事】" in m["content"] for m in msgs),
        "过期事件标注已过去": any("不要当作即将发生" in m["content"] for m in msgs),
        "审计字段完整": all([audit.anchor, audit.now, audit.tz]),
    }
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # ── 推理探针（需真实 LLM；未配置模型时跳过，不算失败）──
    from app.llm.provider_factory import get_llm_provider

    provider = get_llm_provider()
    if not getattr(provider, "api_key", "") and not _model_configured():
        print("\n=== 推理探针：SKIP（未配置对话模型）===")
        return

    print("\n=== 推理探针（真实 LLM）===")
    probe_msgs = assembler.assemble(
        profile=profile, history=history, user_input="我上次说面试是什么时候？距离现在几天？",
        memory_facts=[f.content for f in facts], memory_fact_objs=facts,
        topic_context=topic_context,
    )
    answer = await provider.complete(
        messages=probe_msgs,
        temperature=settings.chat_temperature,
        max_tokens=256,
    )
    print(f"[时间推理探针] {answer}")
    print(f"  {'PASS' if '8月26日' in answer or '8 月 26 日' in answer else 'CHECK'} 回答基于注入日期")

    stale_msgs = assembler.assemble(
        profile=profile, history=history, user_input="我明天有什么安排吗？",
        memory_facts=[facts[0].content], memory_fact_objs=[facts[:1]],
        topic_context=ConversationEngine._render_pending_events(events[:1]),
    )
    stale_answer = await provider.complete(
        messages=stale_msgs,
        temperature=settings.chat_temperature,
        max_tokens=256,
    )
    print(f"[陈旧事实探针] {stale_answer}")
    says_tomorrow = "明天面试" in stale_answer or "你明天有面试" in stale_answer
    print(f"  {'FAIL' if says_tomorrow else 'PASS'} 未把已过期事件当作明天发生")


def _model_configured() -> bool:
    """判断是否已配置可用的对话模型组（无配置时跳过真实 LLM 探针）。"""
    from app.config import settings

    for group in getattr(settings, "model_configs", []) or []:
        data = group if isinstance(group, dict) else getattr(group, "__dict__", {})
        if data.get("id") == getattr(settings, "chat_config_id", "") and data.get("api_key"):
            return True
    return False


if __name__ == "__main__":
    asyncio.run(main())
