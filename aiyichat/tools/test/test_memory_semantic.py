"""记忆语义检索评分单元测试。

覆盖：
- cosine_similarity：相同/正交/空/长度不等
- score_memory_fact_with_query：无向量退化、语义相关性加权
"""

from datetime import datetime, timezone

from app.models.service_memory import (
    MemoryFact,
    cosine_similarity,
    score_memory_fact,
    score_memory_fact_with_query,
)


class TestCosineSimilarity:
    """余弦相似度测试集。"""

    def test_identical_vectors(self) -> None:
        assert abs(cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6

    def test_orthogonal_vectors(self) -> None:
        assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-6

    def test_empty_returns_zero(self) -> None:
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_length_mismatch_returns_zero(self) -> None:
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0


class TestScoreWithQuery:
    """语义综合评分测试集。"""

    def test_fallback_without_embedding(self) -> None:
        """无 query embedding 时退化为纯时效×类别评分。"""
        now = datetime.now(timezone.utc)
        fact = MemoryFact(content="x", category="general", last_referenced_at=now)
        assert score_memory_fact_with_query(fact, [], now) == score_memory_fact(
            fact, now
        )

    def test_semantic_relevance_boosts_score(self) -> None:
        """语义更相关的记忆得分更高。"""
        now = datetime.now(timezone.utc)
        query = [1.0, 0.0]
        relevant = MemoryFact(
            content="a", last_referenced_at=now, embedding=[1.0, 0.0]
        )
        irrelevant = MemoryFact(
            content="b", last_referenced_at=now, embedding=[0.0, 1.0]
        )
        s_rel = score_memory_fact_with_query(relevant, query, now, 0.6)
        s_irr = score_memory_fact_with_query(irrelevant, query, now, 0.6)
        assert s_rel > s_irr
