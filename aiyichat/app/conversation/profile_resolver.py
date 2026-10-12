"""角色（Profile）解析器。"""

from typing import Optional

from app.db.repositories.profile_repo import ProfileRepository
from app.models.profile import PromptProfileInDB


class ProfileResolver:
    """根据锁定配置或用户偏好解析当前活跃角色。"""

    def __init__(
        self,
        profile_repo: ProfileRepository | None = None,
        locked_profile_slug: str = "",
    ):
        self.profile_repo = profile_repo or ProfileRepository()
        self.locked_profile_slug = locked_profile_slug

    async def resolve(
        self, profile_id: Optional[str]
    ) -> Optional[PromptProfileInDB]:
        """
        解析当前角色。

        优先级：
        1. locked_profile_slug（多Bot模式锁定角色）
        2. 用户的 active_profile_id
        均无法解析时返回 None，由上层提示创建角色。
        """
        if self.locked_profile_slug:
            return await self.profile_repo.find_by_slug(
                self.locked_profile_slug
            )

        if profile_id:
            profile = await self.profile_repo.find_by_id(profile_id)
            if profile and profile.is_active:
                return profile

        return None

    def no_profile_message(self) -> str:
        """当角色解析失败时的兜底提示。"""
        return "当前尚未配置角色，请先在管理后台「角色管理」中创建角色后再开始对话。"
