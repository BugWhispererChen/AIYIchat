"""角色档案服务，负责角色的增删改查与后台管理业务逻辑。"""

import logging
import re
from typing import Optional

from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.profile import (
    ProfileDialogueStrategy,
    ProfileIdentity,
    ProfileLanguageStyle,
    ProfilePersonality,
    PromptProfileCreate,
    PromptProfileInDB,
    PromptProfileUpdate,
)

logger = logging.getLogger(__name__)


class ProfileInUseError(Exception):
    """角色仍被对话服务引用，无法禁用。"""

    def __init__(self, service_count: int) -> None:
        self.service_count = service_count
        super().__init__(
            f"该角色下有 {service_count} 个对话服务，请先删除服务后再禁用"
        )


class ProfileService:
    """角色档案服务，提供角色的增删改查与后台表单/启停等业务操作。"""

    def __init__(self) -> None:
        self.repo = ProfileRepository()
        self.service_repo = ServiceRepository()

    async def get_by_slug(self, slug: str) -> Optional[PromptProfileInDB]:
        """根据 slug 获取角色档案。"""
        return await self.repo.find_by_slug(slug)

    async def get_by_id(self, profile_id: str) -> Optional[PromptProfileInDB]:
        """根据 ID 获取角色档案。"""
        return await self.repo.find_by_id(profile_id)

    async def list_active(self, category: Optional[str] = None) -> list[PromptProfileInDB]:
        """列出所有活跃角色，可按分类筛选。"""
        return await self.repo.list_active(category)

    async def list_all(self) -> list[PromptProfileInDB]:
        """列出所有角色。"""
        return await self.repo.list_all()

    async def create(self, profile: PromptProfileCreate) -> PromptProfileInDB:
        """创建新角色档案。"""
        return await self.repo.create(profile)

    async def update(self, slug: str, update: PromptProfileUpdate) -> Optional[PromptProfileInDB]:
        """更新角色档案。"""
        return await self.repo.update(slug, update)

    async def delete(self, slug: str) -> bool:
        """删除角色档案（软删除）。"""
        return await self.repo.delete(slug)

    # ── 后台管理业务逻辑（从 admin 路由下沉）──────────────────

    async def toggle_active(
        self, profile: PromptProfileInDB, disable_reason: str = ""
    ) -> Optional[PromptProfileInDB]:
        """切换角色启用/禁用状态。

        禁用前校验是否被对话服务引用，被引用时抛 ProfileInUseError。
        """
        new_active = not profile.is_active
        if not new_active:
            svc_count = await self.service_repo.count_by_profile(profile.id)
            if svc_count > 0:
                raise ProfileInUseError(svc_count)
            await self.repo.update(
                profile.slug,
                PromptProfileUpdate(
                    is_active=False, disable_reason=disable_reason
                ),
            )
        else:
            await self.repo.update(
                profile.slug,
                PromptProfileUpdate(is_active=True, disable_reason=""),
            )
        return await self.repo.find_by_slug(profile.slug)

    async def update_from_form(
        self, profile: PromptProfileInDB, form
    ) -> Optional[PromptProfileInDB]:
        """从后台编辑表单（starlette FormData 兼容对象）构建更新并保存。

        表单字段命名与 profile_detail.html 一一对应；多行文本按行拆分去空，
        traits 解析失败时回退 0.5。
        """
        update = self._build_update_from_form(profile, form)
        await self.repo.update(profile.slug, update)
        return await self.repo.find_by_slug(profile.slug)

    async def set_reference_image(
        self, slug: str, media_url: str
    ) -> None:
        """设置定妆照 URL（空串表示清除，本人出镜图片退回外貌文字链路）。"""
        await self.repo.update(
            slug, PromptProfileUpdate(reference_image_url=media_url)
        )

    # ── 声音复刻业务逻辑 ────────────────────────────────────

    async def set_voice_clone(
        self,
        slug: str,
        voice_id: str,
        target_model: str,
    ) -> None:
        """声音复刻成功后更新 Profile 的音色与复刻状态。"""
        await self.repo.update(
            slug,
            PromptProfileUpdate(
                voice_id=voice_id,
                voice_clone_status="active",
                voice_clone_target_model=target_model,
            ),
        )

    async def set_voice_clone_failed(self, slug: str) -> None:
        """声音复刻失败时更新状态。"""
        await self.repo.update(
            slug,
            PromptProfileUpdate(voice_clone_status="failed"),
        )

    async def clear_voice_clone(self, slug: str) -> None:
        """清除声音复刻状态（保留 voice_id 以便手动指定其他音色）。"""
        await self.repo.update(
            slug,
            PromptProfileUpdate(
                voice_clone_status="",
                voice_clone_target_model="",
            ),
        )

    @staticmethod
    def build_clone_prefix(name: str) -> str:
        """从角色名生成音色前缀：仅保留字母数字，≤10 字符。"""
        # 去除非字母数字字符，转小写
        prefix = re.sub(r"[^a-zA-Z0-9]", "", name).lower()
        return prefix[:10] if prefix else "myvoice"

    @staticmethod
    def _build_update_from_form(
        profile: PromptProfileInDB, form
    ) -> PromptProfileUpdate:
        """将编辑表单解析为 PromptProfileUpdate（纯转换，无 IO）。"""

        def _lines(key: str) -> list[str]:
            """将多行文本按行分割并去空。"""
            val = form.get(key, "")
            return [line.strip() for line in val.split("\n") if line.strip()]

        # 解析 relationships
        rel_names = form.getlist("rel_name")
        rel_relations = form.getlist("rel_relation")
        rel_attitudes = form.getlist("rel_attitude")
        relationships = []
        for name, rel, att in zip(rel_names, rel_relations, rel_attitudes):
            if name.strip():
                relationships.append({
                    "name": name.strip(),
                    "relation": rel.strip(),
                    "attitude": att.strip(),
                })

        # 解析 traits
        trait_keys = form.getlist("trait_key")
        trait_values = form.getlist("trait_value")
        traits = {}
        for k, v in zip(trait_keys, trait_values):
            if k.strip():
                try:
                    traits[k.strip()] = float(v)
                except ValueError:
                    traits[k.strip()] = 0.5

        # 解析可见范围
        visibility = str(
            form.get("visibility", profile.visibility or "public")
        ).strip()
        account_id_raw = str(form.get("account_id", "")).strip()
        account_id = account_id_raw if account_id_raw else None

        # 解析回复风格（非法值回退 standard）
        verbosity_raw = str(form.get("verbosity", "standard")).strip()
        verbosity = (
            verbosity_raw
            if verbosity_raw in ("concise", "standard", "verbose")
            else "standard"
        )

        return PromptProfileUpdate(
            name=form.get("name", profile.name),
            source=form.get("source", profile.source),
            category=form.get("category", profile.category),
            is_active=form.get("is_active") == "on",
            visibility=visibility,  # type: ignore[arg-type]
            account_id=account_id,
            identity=ProfileIdentity(
                background=form.get("identity_background", ""),
                era=form.get("identity_era", ""),
                age_appearance=form.get("identity_age_appearance", ""),
                appearance=str(form.get("identity_appearance", "")),
                core_values=_lines("identity_core_values"),
                relationships=relationships,
            ),
            language_style=ProfileLanguageStyle(
                vocabulary=_lines("ls_vocabulary"),
                sentence_patterns=_lines("ls_sentence_patterns"),
                tone=form.get("ls_tone", ""),
                emoji_habits=_lines("ls_emoji_habits"),
                forbidden_words=_lines("ls_forbidden_words"),
                example_utterances=_lines("ls_example_utterances"),
            ),
            personality=ProfilePersonality(
                mbti_hint=form.get("p_mbti_hint", ""),
                traits=traits,
                care_style=form.get("p_care_style", ""),
            ),
            dialogue_strategy=ProfileDialogueStrategy(
                greeting=form.get("ds_greeting", ""),
                on_negative_emotion=form.get("ds_on_negative_emotion", ""),
                on_positive_emotion=form.get("ds_on_positive_emotion", ""),
                on_silence=form.get("ds_on_silence", ""),
                boundaries=_lines("ds_boundaries"),
                safety_guardrails=form.get("ds_safety_guardrails", ""),
            ),
            meta_instructions=form.get("meta_instructions", ""),
            voice_id=form.get("voice_id", ""),
            verbosity=verbosity,  # type: ignore[arg-type]
            # 表情包分组多选（未勾选时保存空列表：不发送表情包）
            sticker_group_ids=[
                str(g).strip() for g in form.getlist("sticker_group_ids")
                if str(g).strip()
            ],
        )
