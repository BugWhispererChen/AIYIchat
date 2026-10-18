"""对话轮次记录器。

负责将身份消息和 AI 回复持久化到数据库，并更新对话元数据。
"""

from datetime import datetime
from typing import Optional

from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.models.message import MessageInDB


class TurnRecorder:
    """记录一轮对话中的身份输入与 AI 输出。"""

    def __init__(
        self,
        message_repo: MessageRepository | None = None,
        conversation_repo: ConversationRepository | None = None,
    ):
        self.message_repo = message_repo or MessageRepository()
        self.conversation_repo = conversation_repo or ConversationRepository()

    async def record_identity_message(
        self,
        conversation_id: str,
        text: str,
        emotion_tag: Optional[str],
        media_url: Optional[str] = None,
        content_type: str = "text",
        created_at: Optional[datetime] = None,
    ) -> MessageInDB:
        """持久化身份消息。

        created_at 应传入消息接收时刻：身份消息在 LLM 回复后才落库，
        若取写入时间会与 AI 回复时间相同甚至更晚，导致历史顺序错乱。
        """
        return await self.message_repo.create(
            conversation_id=conversation_id,
            role="user",
            content=text,
            emotion_tag=emotion_tag,
            media_url=media_url,
            content_type=content_type,
            created_at=created_at,
        )

    async def record_assistant_message(
        self,
        conversation_id: str,
        clean_text: str,
        llm_model: str,
        latency_ms: int,
    ) -> MessageInDB:
        """持久化 AI 回复。"""
        return await self.message_repo.create(
            conversation_id=conversation_id,
            role="assistant",
            content=clean_text,
            llm_model=llm_model,
            latency_ms=latency_ms,
        )

    async def update_conversation_meta(self, conversation_id: str) -> None:
        """更新对话元数据（计数 +2）。"""
        await self.conversation_repo.update_after_message(
            conversation_id, count=2
        )
