"""记忆治理单测：指称归一、点状事实时效、改述判定。

全部为纯规则判定，不触库、不调模型。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.conversation.memory_hygiene import (
    audit_memory,
    is_punctually_stale,
    is_referent_dirty,
    is_restatement,
    normalize_referent,
    plan_temporal_refresh,
    stamp_punctual,
)
from app.models.service_memory import MemoryFact

_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def _fact(content: str, **kw) -> MemoryFact:
    return MemoryFact(content=content, **kw)


class TestNormalizeReferent:
    """存量与新增事实的第三人称/出戏写法归一。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("用户近期工作频繁加班", "对方近期工作频繁加班"),
            ("身份喜欢在周末爬山", "对方喜欢在周末爬山"),
            ("用户与AI关系亲密，互称老公/老婆", "对方与我关系亲密，互称老公/老婆"),
            ("对方对AI发的消息感到生气", "对方对我发的消息感到生气"),
            # 期望值取自测试服真实存量语料（28 条脏指称里的三种形态）
            ("用户称呼AI为老公/女朋友，存在拟人化亲密关系",
             "对方称呼我为老公/女朋友，存在拟人化亲密关系"),
            ("用户9月5日凌晨因言语伤害AI而道歉", "对方9月5日凌晨因言语伤害我而道歉"),
            ("用户曾称呼AI为小朋友", "对方曾称呼我为小朋友"),
        ],
    )
    def test_dirty_rewritten(self, raw: str, expected: str) -> None:
        changed_text, changed = normalize_referent(raw)
        assert changed_text == expected
        assert changed is True
        assert is_referent_dirty(changed_text) is False

    def test_ai_as_product_not_rewritten(self) -> None:
        # "AI源代码"里的 AI 指产品本身，不是角色——泛化替换会把它改成第一人称，
        # 造出一条语义错误的记忆
        text = "用户将AI源代码上传GitHub开源"
        assert normalize_referent(text)[0] == "对方将AI源代码上传GitHub开源"

    def test_clean_untouched(self) -> None:
        text = "对方10月21日开会被领导批评"
        assert normalize_referent(text) == (text, False)


class TestStampPunctual:
    """点状标记只能在写入时刻判定。"""

    def test_future_event_is_punctual(self) -> None:
        # 8月25日说"明天有面试" → 事实描述的是未来的某个时刻
        fact = _fact(
            "对方8月26日有面试",
            valid_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc),
        )
        assert stamp_punctual(fact, datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc))
        assert fact.punctual is True

    def test_same_day_event_is_not_punctual(self) -> None:
        # 当天回顾当天发生的事，是情景记忆而非过期安排，不该被剔除
        msg_at = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
        fact = _fact("对方10月21日开会被领导批评", valid_at=msg_at, created_at=msg_at)
        assert stamp_punctual(fact, msg_at) is False
        assert is_punctually_stale(fact, _NOW) is False

    def test_undated_fact_is_not_punctual(self) -> None:
        fact = _fact("对方喜欢喝咖啡")
        assert stamp_punctual(fact, _NOW) is False
        assert is_punctually_stale(fact, _NOW) is False


class TestPunctualStaleness:
    """过期安排在宽限期后被剔除，宽限期内保留。"""

    def test_stale_after_grace(self) -> None:
        fact = _fact(
            "对方8月26日有面试",
            valid_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
            punctual=True,
        )
        assert is_punctually_stale(fact, _NOW) is True
        # 只判内存对象，不写库：落库时点由 plan_temporal_refresh 给出
        assert plan_temporal_refresh(fact, _NOW) == datetime(
            2026, 8, 26, 9, 0, tzinfo=timezone.utc
        ) + timedelta(days=settings.memory_punctual_grace_days)

    def test_within_grace_kept(self) -> None:
        valid = _NOW - timedelta(days=1)
        fact = _fact("对方昨天有面试", valid_at=valid, punctual=True)
        assert is_punctually_stale(fact, _NOW) is False
        assert plan_temporal_refresh(fact, _NOW) is None

    def test_already_invalidated_not_replanned(self) -> None:
        fact = _fact(
            "对方8月26日有面试",
            valid_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
            invalid_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            punctual=True,
        )
        assert plan_temporal_refresh(fact, _NOW) is None


class TestRestatement:
    """改述合并的重述应判为重复，而不是当全新信息新增。

    期望值取自测试服真实语料：同一事件的两条改述实测 0.652，
    其余任意两条两两比对均 < 0.25。
    """

    def test_merged_restatement_detected(self) -> None:
        a = "对方10月21日下午开会时被领导批评"
        b = "对方10月21日下午开会被领导批评且感到委屈"
        assert is_restatement(a, b) is True

    def test_distinct_facts_not_merged(self) -> None:
        assert is_restatement("对方近期睡眠质量不佳", "对方正在准备考研复习") is False
        assert is_restatement("对方喜欢在周末爬山", "对方近期工作频繁加班") is False
        # 事件与其评价是两条独立记忆，不能并成一条
        assert is_restatement(
            "对方10月21日开会被领导批评", "对方认为被批评并非自身过错"
        ) is False


class TestAuditMemory:
    """治理尺子与迁移脚本共用的体检口径。"""

    def test_counts(self) -> None:
        facts = [
            _fact("用户近期工作频繁加班"),
            _fact("身份喜欢在周末爬山"),
            _fact(
                "对方8月26日有面试",
                valid_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
                punctual=True,
            ),
            _fact("对方喜欢喝咖啡"),
            _fact("对方喜欢喝咖啡"),
        ]
        report = audit_memory(facts, _NOW)
        assert report["total"] == 5
        assert report["dirty_referent"] == 2
        assert report["punctual_stale"] == 1
        assert report["exact_duplicate"] == 1

    def test_empty(self) -> None:
        assert audit_memory([], _NOW)["total"] == 0
