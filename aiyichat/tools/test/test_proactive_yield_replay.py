"""主动性退让（C10 + B22 + 每日上限）的虚拟时钟重放测试。

阶段节奏是 4h/24h/72h/168h，靠线上等几天既不可行也复现不了；
这里用 `time_replay.frozen_clock` 按小时推进，把"被忽略就降档、被回应就恢复"
整条状态机在几毫秒里跑完并逐条核对记录（D5 的验收口径）。
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.proactive_repo import ProactiveRecordRepository
from app.models.proactive_record import ProactiveRecord
from app.proactive.scheduler import ProactiveScheduler
from tools.test.db_test_utils import run_with_db
from tools.test.time_replay import frozen_clock

# 起点：2026-10-01 10:00 UTC（测试里把默认时区偏移设为 0，本地日界即 UTC 日界）
T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
CUTOFF = T0 - timedelta(days=30)


def _make_service() -> SimpleNamespace:
    return SimpleNamespace(
        id="svc-1", identity_id="id-1", account_id="acc-1",
        bot_id="bot-1", profile_id="companion", timezone="",
        last_message_at=None, context_key="svc-1",
    )


def _make_identity(last_active_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id="id-1", platform="qq", platform_id="openid-1",
        is_blocked=False, timezone="", last_active_at=last_active_at,
        bot_openids={},
    )


def _make_scheduler(
    identity: SimpleNamespace, conversation_id: str = "conv-1"
) -> ProactiveScheduler:
    """真实仓储走 mongomock，只把"取对方/发得出/生成得了"这三件外部事实换成替身。"""
    scheduler = ProactiveScheduler()
    scheduler._identity_repo = MagicMock()
    scheduler._identity_repo.find_by_id = AsyncMock(return_value=identity)
    scheduler._conversation_repo = MagicMock()
    scheduler._conversation_repo.list_by_service = AsyncMock(
        return_value=[SimpleNamespace(id=conversation_id)]
    )
    scheduler._can_send = MagicMock(return_value=True)
    scheduler._send_message = AsyncMock(return_value=True)
    scheduler._generate_content = AsyncMock(return_value=("最近怎么样？", ""))
    scheduler._generate_share_content = AsyncMock(return_value="看到个好笑的")
    scheduler._process_pending_events = AsyncMock(return_value=False)
    return scheduler


@contextmanager
def _scenario(**overrides: object) -> Iterator[None]:
    """把主动关怀调成可预测形态：免打扰关闭、分享骰子关闭、事件跟进关闭。"""
    base: dict[str, object] = {
        "proactive_share_enabled": False,
        "proactive_event_enabled": False,
        "proactive_quiet_hour_start": 0,
        "proactive_quiet_hour_end": 0,
        "default_timezone_offset": 0,
        "proactive_yield_enabled": True,
        "proactive_unyield_threshold": 2,
        "proactive_yield_hard_limit": 4,
        "proactive_daily_limit": 1,
        "proactive_monthly_limit": 4,
    }
    base.update(overrides)
    with ExitStack() as stack:
        for key, value in base.items():
            stack.enter_context(patch.object(settings, key, value))
        yield


async def _statuses(scheduler: ProactiveScheduler) -> list[tuple[int, str]]:
    """按 (阶段, 状态) 读出全部记录，按阶段升序返回（断言用）。"""
    records = await ProactiveRecordRepository().find_latest_by_identity(
        "id-1", "bot-1", limit=20
    )
    return sorted((r.stage, r.status) for r in records)


class TestYieldStateMachine:
    """核心链路：敲门两次没人应 → 第三次忍住 → 对方开口即恢复。"""

    def test_two_unanswered_then_yield_and_recover(self) -> None:
        async def body() -> None:
            service = _make_service()
            identity = _make_identity(T0)
            scheduler = _make_scheduler(identity)

            with frozen_clock(T0) as clock, _scenario():
                # 沉默 4h → 阶段1 发出
                clock.advance(hours=4)
                await scheduler._process_service(service, CUTOFF)
                assert (1, "sent") in await _statuses(scheduler)

                # 再过 20h（距上次活跃 24h）：欠账只有 1 条，阶段2 照发
                clock.advance(hours=20)
                await scheduler._process_service(service, CUTOFF)
                assert await _statuses(scheduler) == [(1, "sent"), (2, "sent")]

                # 再过 48h（欠账累计 2 条）：放弃阶段3，只留一条 skipped，不再调用 LLM
                clock.advance(hours=48)
                await scheduler._process_service(service, CUTOFF)
                assert await _statuses(scheduler) == [
                    (1, "sent"), (2, "sent"), (3, "skipped"),
                ]
                assert scheduler._generate_content.await_count == 2

                skipped = [
                    r
                    for r in await ProactiveRecordRepository().find_latest_by_identity(
                        "id-1", "bot-1", limit=20
                    )
                    if r.status == "skipped"
                ]
                assert skipped[0].skip_reason == "yield:streak=2"

                # 同一轮里再过 1h：阶段3 已计入"已尝试"，不能被反复判定刷出第二条 skipped
                clock.advance(hours=1)
                await scheduler._process_service(service, CUTOFF)
                assert await _statuses(scheduler) == [
                    (1, "sent"), (2, "sent"), (3, "skipped"),
                ]

                # 对方开口（messages 里出现晚于 sent_at 的 user 消息 + 活跃时间前移）
                replied_at = clock.now + timedelta(hours=1)
                await MessageRepository().create(
                    conversation_id="conv-1", role="user",
                    content="最近还行，就是忙", created_at=replied_at,
                )
                identity.last_active_at = replied_at

                # 沉默 4h 后重新走一轮：欠账归零，阶段1 恢复发送
                clock.advance(hours=5)
                await scheduler._process_service(service, CUTOFF)
                assert scheduler._generate_content.await_count == 3
                assert sum(
                    1 for stage, status in await _statuses(scheduler)
                    if status == "sent"
                ) == 3

        run_with_db(body)

    def test_hard_limit_also_suppresses_event_followup(self) -> None:
        """欠账达到停发阈值时，连事件跟进这条路径也要压住（否则退让只管一半）。"""
        async def body() -> None:
            service = _make_service()
            identity = _make_identity(T0 - timedelta(hours=200))
            scheduler = _make_scheduler(identity)
            repo = ProactiveRecordRepository()

            with frozen_clock(T0) as clock, _scenario(
                proactive_event_enabled=True, proactive_daily_limit=0
            ):
                for i in range(4):
                    record = ProactiveRecord(
                        identity_id="id-1", platform_openid="openid-1",
                        platform="qq", bot_id="bot-1", stage=i + 1,
                        content="在吗", status="sent",
                    )
                    await repo.create(record)
                    await repo.mark_sent(record.id, "在吗")
                    clock.advance(hours=24)

                clock.advance(hours=2)
                before = await _statuses(scheduler)
                await scheduler._process_service(service, CUTOFF)

                assert scheduler._process_pending_events.await_count == 0
                assert await _statuses(scheduler) == before
                assert scheduler._generate_content.await_count == 0

        run_with_db(body)


class TestDailyLimit:
    """每日上限：与月限并存，按对方本地日界计数。"""

    def test_second_send_same_local_day_is_blocked(self) -> None:
        async def body() -> None:
            service = _make_service()
            # 对方已沉默 30h：阶段1（4h）与阶段2（24h）本轮都到点
            identity = _make_identity(T0 - timedelta(hours=30))
            scheduler = _make_scheduler(identity)

            with frozen_clock(T0) as clock, _scenario():
                await scheduler._process_service(service, CUTOFF)
                assert await _statuses(scheduler) == [(1, "sent")]

                # 越过 1 小时冷却再判：此时只剩日限这一道闸能挡住同日第二条
                clock.advance(hours=2)
                await scheduler._process_service(service, CUTOFF)
                # 同日第二次被日限挡住：既没有新记录，也没白烧一次 LLM
                assert await _statuses(scheduler) == [(1, "sent")]
                assert scheduler._generate_content.await_count == 1

        run_with_db(body)

    def test_daily_limit_zero_means_unlimited(self) -> None:
        """日额填 0 = 不限（后台约定与 stream_max_bubbles 一致，不能被当成"一条都不许发"）。"""
        async def body() -> None:
            service = _make_service()
            identity = _make_identity(T0 - timedelta(hours=30))
            scheduler = _make_scheduler(identity)

            with frozen_clock(T0) as clock, _scenario(proactive_daily_limit=0):
                await scheduler._process_service(service, CUTOFF)
                # 同 Bot 有 1 小时冷却，推进 2h 再试下一条（冷却与日限是两道独立的闸）
                clock.advance(hours=2)
                await scheduler._process_service(service, CUTOFF)
                assert await _statuses(scheduler) == [(1, "sent"), (2, "sent")]

        run_with_db(body)


class TestYieldSwitchOff:
    """总开关关闭即回退旧行为：不判欠账、不写 skipped。"""

    def test_disabled_switch_sends_as_before(self) -> None:
        async def body() -> None:
            service = _make_service()
            identity = _make_identity(T0 - timedelta(hours=200))
            scheduler = _make_scheduler(identity)
            repo = ProactiveRecordRepository()

            with frozen_clock(T0) as clock, _scenario(
                proactive_yield_enabled=False, proactive_daily_limit=0
            ):
                # 三条已发出且无人回应：换作开关开着，这一轮必被退让
                for i in range(3):
                    record = ProactiveRecord(
                        identity_id="id-1", platform_openid="openid-1",
                        platform="qq", bot_id="bot-1", stage=i + 1,
                        content="在吗", status="sent",
                    )
                    await repo.create(record)
                    await repo.mark_sent(record.id, "在吗")

                # 越过同 Bot 1 小时冷却再判
                clock.advance(hours=2)
                await scheduler._process_service(service, CUTOFF)

                statuses = await _statuses(scheduler)
                assert "skipped" not in [status for _, status in statuses]
                assert (4, "sent") in statuses

        run_with_db(body)
