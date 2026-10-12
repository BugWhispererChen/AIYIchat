"""Function Calling 工具检索单元测试。

覆盖：
- 工具 schema 完整性
- search_facts 空输入、关键词回退、语义检索
"""

from app.llm.tools import ALL_TOOLS, search_facts
from app.models.service_memory import MemoryFact


class TestToolSchemas:
    """工具 schema 测试集。"""

    def test_all_tools_have_names(self) -> None:
        names = {t["function"]["name"] for t in ALL_TOOLS}
        assert "retrieve_memory" in names
        assert "generate_image" in names


class TestSearchFacts:
    """记忆检索测试集。"""

    def test_empty_inputs_return_empty(self) -> None:
        assert search_facts("", []) == []
        assert search_facts("工作", []) == []

    def test_keyword_fallback(self) -> None:
        facts = [
            MemoryFact(content="用户在准备考研"),
            MemoryFact(content="用户喜欢喝奶茶"),
        ]
        hits = search_facts("考研复习", facts)
        assert any("考研" in h for h in hits)

    def test_semantic_search_prefers_relevant(self) -> None:
        facts = [
            MemoryFact(content="a", embedding=[1.0, 0.0]),
            MemoryFact(content="b", embedding=[0.0, 1.0]),
        ]
        hits = search_facts("q", facts, query_embedding=[1.0, 0.0], topk=1)
        assert hits == ["a"]
