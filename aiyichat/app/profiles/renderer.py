from app.llm.prompt_assembler import PromptAssembler
from app.models.profile import PromptProfileInDB


class ProfileRenderer:
    """Profile渲染器 - 将Profile文档渲染为系统提示词字符串"""

    def __init__(self):
        self._assembler = PromptAssembler()

    def render(self, profile: PromptProfileInDB, emotion_hint: str | None = None) -> str:
        """将Profile渲染为系统提示词"""
        return self._assembler._render_system_prompt(profile, emotion_hint)

    def render_brief(self, profile: PromptProfileInDB) -> str:
        """渲染简短的角色介绍（用于角色列表展示）"""
        identity = profile.identity
        return (
            f"{profile.name} - {profile.source}\n"
            f"  {identity.background[:50]}{'...' if len(identity.background) > 50 else ''}\n"
            f"  性格：{profile.personality.care_style}"
        )
