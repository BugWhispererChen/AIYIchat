"""长期记忆打分排序单元测试。

覆盖 score_memory_fact：
- 相同类别时，最近被引用的记忆得分更高（时间衰减）
- 相同引用时间时，类别权重更高的记忆得分更高
- 半衰期处得分约为初始的一半
- 综合打分用于排序时能兼顾时间与类别
"""

from datetime import datetime, timedelta, timezone

from app.models.service_memory import (
    _RECENCY_HALFLIFE_DAYS,
    MemoryFact,
    score_memory_fact,
)


def _fact(category: str, days_ago: float) -> MemoryFact:
    ref = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return MemoryFact(content="x", category=category, last_referenced_at=ref)


class TestScoreMemoryFact:
    """score_memory_fact 打分逻辑测试集。"""

    def test_recency_decreases_score(self) -> None:
        """同类别下，越久未引用得分越低。"""
        now = datetime.now(timezone.utc)
        recent = _fact("general", 0)
        old = _fact("general", 30)
        assert score_memory_fact(recent, now) > score_memory_fact(old, now)

    def test_category_weight_affects_score(self) -> None:
        """相同引用时间下，重要类别得分更高。"""
        now = datetime.now(timezone.utc)
        life = _fact("life_event", 1)
        general = _fact("general", 1)
        assert score_memory_fact(life, now) > score_memory_fact(general, now)

    def test_halflife_halves_recency(self) -> None:
        """半衰期天数处，时间衰减因子约为 0.5。"""
        now = datetime.now(timezone.utc)
        fresh = _fact("general", 0)
        half = _fact("general", _RECENCY_HALFLIFE_DAYS)
        ratio = score_memory_fact(half, now) / score_memory_fact(fresh, now)
        assert abs(ratio - 0.5) < 0.01

    def test_naive_datetime_handled(self) -> None:
        """last_referenced_at 为 naive datetime 时不应报错。"""
        naive = datetime.now()  # 无 tzinfo
        fact = MemoryFact(content="x", category="general", last_referenced_at=naive)
        # 不抛异常且返回正数
        assert score_memory_fact(fact) > 0

    def test_sorting_prioritizes_recent_important(self) -> None:
        """综合排序时，近期的重要记忆应排在前面。"""
        now = datetime.now(timezone.utc)
        facts = [
            _fact("general", 20),       # 旧且低权重
            _fact("life_event", 1),     # 新且高权重
            _fact("general", 1),        # 新但低权重
        ]
        ordered = sorted(
            facts, key=lambda f: score_memory_fact(f, now), reverse=True
        )
        assert ordered[0].category == "life_event"
        assert ordered[-1].last_referenced_at < ordered[0].last_referenced_at
