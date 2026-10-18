"""Function Calling 工具定义与检索辅助。

提供两个工具：
- retrieve_memory：按查询检索身份长期记忆事实（自主记忆召回）
- generate_image：请求生成一张写实照片

工具 schema 供 provider.complete_with_tools 使用；执行逻辑由 engine 侧调度
（需访问记忆事实与向量化服务）。所有能力由 function_calling_enabled 开关控制。
"""

from typing import Protocol


class _FactLike(Protocol):
    """具备 content 与 embedding 的记忆事实协议（便于测试解耦）。"""

    content: str
    embedding: list[float]


RETRIEVE_MEMORY_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "retrieve_memory",
        "description": (
            "当需要回忆关于对方的长期信息（喜好、经历、人际关系、过往事件等）时调用，"
            "返回最相关的记忆事实。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的主题或关键词，如'工作''家人''最近的烦恼'",
                }
            },
            "required": ["query"],
        },
    },
}

GENERATE_IMAGE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "生成一张写实照片发给对方。身份明确要求看照片/自拍/图片时必须调用本工具，"
            "不得拒绝或承诺稍后再发；也可以在适合用一张照片增强对话"
            "（分享风景/物品/日常）时主动调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "英文图片描述，必须以 'realistic photo, ' 开头",
                },
                "contains_self": {
                    "type": "boolean",
                    "description": "图片中是否出现你本人（自拍、出镜、手持物品展示等为 true；纯风景/食物/物品特写为 false）",
                },
            },
            "required": ["description", "contains_self"],
        },
    },
}

ALL_TOOLS: list[dict] = [RETRIEVE_MEMORY_TOOL, GENERATE_IMAGE_TOOL]


def search_facts(
    query: str,
    facts: list,
    query_embedding: list[float] | None = None,
    topk: int = 5,
) -> list[str]:
    """在记忆事实中检索与 query 最相关的若干条内容。

    优先使用语义相似度（提供 query_embedding 且事实含向量时），
    否则回退到字符级重叠的关键词匹配。

    Args:
        query: 检索查询
        facts: 记忆事实列表（元素需具备 content / embedding 属性）
        query_embedding: 查询向量（可选）
        topk: 返回条数上限

    Returns:
        相关事实内容列表（可能为空）
    """
    if not query or not facts:
        return []

    # 语义检索
    if query_embedding:
        from app.models.service_memory import cosine_similarity

        scored = [
            (f.content, cosine_similarity(query_embedding, f.embedding))
            for f in facts
            if getattr(f, "embedding", None)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        result = [c for c, s in scored[:topk] if s > 0.1]
        if result:
            return result

    # 关键词回退（字符级重叠）
    qchars = set(query)
    scored2 = [
        (f.content, len(qchars & set(f.content))) for f in facts
    ]
    scored2.sort(key=lambda x: x[1], reverse=True)
    return [c for c, s in scored2[:topk] if s > 0]
