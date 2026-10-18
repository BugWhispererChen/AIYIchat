"""表情包功能单元测试。

覆盖：
- response_processor [表情:描述] 标签提取与清洗
- sticker_service 关键词兜底检索 / 语义检索 / 随机选图（均限定分组）
- 分组过滤：空分组返回空、停用分组不可用、检索限定绑定分组
- engine 以图回图触发判定与任务组装（含未绑定分组零开销路径）
- sticker_seeder 启动导入幂等去重 + 分组 upsert 与回填（mongomock）
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.conversation.engine import ConversationEngine
from app.conversation.response_processor import ResponseProcessor
from app.conversation.sticker_task import STICKER_TRANSCRIBE_PREFIX
from app.db.repositories.sticker_group_repo import StickerGroupRepository
from app.db.repositories.sticker_repo import StickerRepository
from app.models.sticker import StickerDocument
from app.models.sticker_group import StickerGroupDocument
from app.services import sticker_service
from app.services.sticker_service import (
    get_stickers_for_groups,
    invalidate_sticker_cache,
    pick_random_sticker,
    search_sticker,
    sticker_library_available,
)
from tools.test.db_test_utils import run_with_db


def _make_sticker(
    name: str, tags: list[str], desc: str,
    embedding: list[float] | None = None,
    group_id: str = "",
) -> StickerDocument:
    """构造测试用表情包文档（不依赖真实媒体）。"""
    return StickerDocument(
        name=name,
        tags=tags,
        description=desc,
        media_url="/api/media/fake",
        content_type="image/gif",
        embedding=embedding or [],
        group_id=group_id,
    )


async def _seed_grouped_docs(
    sticker_repo: StickerRepository, group_repo: StickerGroupRepository
) -> tuple[str, str]:
    """写入两个分组与三条表情包，返回 (分组A, 分组B) 的 ID。

    分组A「萌猫日常」：开心大笑、晚安；分组B「开心鸭」：难过哭泣。
    """
    gid_a = await group_repo.create(StickerGroupDocument(name="萌猫日常"))
    gid_b = await group_repo.create(StickerGroupDocument(name="开心鸭"))
    await sticker_repo.create(_make_sticker(
        "开心大笑", ["开心", "大笑", "哈哈"], "开心地大笑，心情非常好",
        group_id=gid_a,
    ))
    await sticker_repo.create(_make_sticker(
        "晚安", ["晚安", "睡觉"], "说晚安准备睡觉了",
        group_id=gid_a,
    ))
    await sticker_repo.create(_make_sticker(
        "难过哭泣", ["难过", "哭", "伤心"], "难过地哭泣，需要安慰",
        group_id=gid_b,
    ))
    return gid_a, gid_b


def _fake_profile(group_ids: list[str]):
    """构造引擎测试用角色（仅含分组相关字段）。"""
    return SimpleNamespace(sticker_group_ids=group_ids)


class TestResponseProcessorStickerTag:
    """[表情:描述] 标签的提取与清洗。"""

    def test_extract_sticker_specs(self) -> None:
        """提取标签描述并从正文中移除。"""
        p = ResponseProcessor()
        clean, image_specs, sticker_specs = p.process(
            "哈哈太好笑了[表情:大笑]改天再聊"
        )
        assert sticker_specs == ["大笑"]
        assert "[表情" not in clean
        assert "哈哈太好笑了" in clean
        assert image_specs == []

    def test_sticker_tag_fullwidth_colon(self) -> None:
        """兼容全角冒号与 STICKER 英文形式。"""
        p = ResponseProcessor()
        _, _, specs = p.process("[表情：委屈撒娇]\n[sticker:good night]")
        assert specs == ["委屈撒娇", "good night"]

    def test_no_sticker_tag(self) -> None:
        """无标签时返回空列表且文本不变。"""
        p = ResponseProcessor()
        clean, _, specs = p.process("今天天气不错😊")
        assert specs == []
        assert clean == "今天天气不错😊"


class TestStickerGroupFilter:
    """分组过滤：空入参、停用分组、未归组表情不参与发送。"""

    def test_empty_group_ids_returns_empty(self) -> None:
        """未绑定任何分组时返回空（不发送表情包）。"""
        async def body() -> None:
            gid_a, _ = await _seed_grouped_docs(
                StickerRepository(), StickerGroupRepository()
            )
            invalidate_sticker_cache()
            assert await get_stickers_for_groups([]) == []
            assert not await sticker_library_available([])
            assert await search_sticker("很开心", []) is None
            assert await pick_random_sticker([]) is None
            # 绑定有效分组后非空
            assert await sticker_library_available([gid_a])

        run_with_db(body)

    def test_disabled_group_unavailable(self) -> None:
        """停用分组后该分组内表情不可检索。"""
        async def body() -> None:
            group_repo = StickerGroupRepository()
            _, gid_b = await _seed_grouped_docs(
                StickerRepository(), group_repo
            )
            invalidate_sticker_cache()
            assert len(await get_stickers_for_groups([gid_b])) == 1

            await group_repo.update_fields(gid_b, {"enabled": False})
            invalidate_sticker_cache()
            assert await get_stickers_for_groups([gid_b]) == []
            assert not await sticker_library_available([gid_b])
            assert await search_sticker("很难过", [gid_b]) is None

        run_with_db(body)

    def test_ungrouped_sticker_excluded(self) -> None:
        """group_id 为空的表情仅后台可见，不参与分组检索。"""
        async def body() -> None:
            repo = StickerRepository()
            gid_a, _ = await _seed_grouped_docs(
                repo, StickerGroupRepository()
            )
            await repo.create(_make_sticker(
                "未归组表情", ["开心"], "开心地跳起来"
            ))
            invalidate_sticker_cache()
            hits = await get_stickers_for_groups([gid_a])
            assert all(s.group_id == gid_a for s in hits)
            assert "未归组表情" not in [s.name for s in hits]

        run_with_db(body)


class TestStickerKeywordSearch:
    """未启用向量化时的关键词兜底检索（mongomock，限定分组）。"""

    def test_keyword_hit_within_group(self) -> None:
        async def body() -> None:
            gid_a, gid_b = await _seed_grouped_docs(
                StickerRepository(), StickerGroupRepository()
            )
            invalidate_sticker_cache()

            old_enabled = settings.embedding_enabled
            settings.embedding_enabled = False
            try:
                hit = await search_sticker("我现在特别开心", [gid_a])
                assert hit is not None
                assert hit.name == "开心大笑"

                hit2 = await search_sticker("晚安啦", [gid_a, gid_b])
                assert hit2 is not None
                assert hit2.name == "晚安"
            finally:
                settings.embedding_enabled = old_enabled

        run_with_db(body)

    def test_search_limited_to_bound_groups(self) -> None:
        """检索限定在绑定分组内：未绑定分组的关键词命中返回 None。"""
        async def body() -> None:
            gid_a, gid_b = await _seed_grouped_docs(
                StickerRepository(), StickerGroupRepository()
            )
            invalidate_sticker_cache()

            old_enabled = settings.embedding_enabled
            settings.embedding_enabled = False
            try:
                # 「难过」仅存在于分组B，绑定分组A时检索不到
                assert await search_sticker("特别难过想哭", [gid_a]) is None
                hit = await search_sticker("特别难过想哭", [gid_b])
                assert hit is not None and hit.name == "难过哭泣"
            finally:
                settings.embedding_enabled = old_enabled

        run_with_db(body)

    def test_keyword_miss_returns_none(self) -> None:
        """全部未命中时返回 None（宁可不发）。"""
        async def body() -> None:
            gid_a, _ = await _seed_grouped_docs(
                StickerRepository(), StickerGroupRepository()
            )
            invalidate_sticker_cache()

            old_enabled = settings.embedding_enabled
            settings.embedding_enabled = False
            try:
                assert await search_sticker(
                    "量子纠缠的测量问题", [gid_a]
                ) is None
            finally:
                settings.embedding_enabled = old_enabled

        run_with_db(body)

    def test_random_pick_limited_to_group(self) -> None:
        """随机选图：限定绑定分组；空分组或空库返回 None。"""
        async def body() -> None:
            invalidate_sticker_cache()  # 清除前序测试残留缓存（新内存库为空）
            assert await pick_random_sticker([]) is None  # 未绑定分组
            gid_a, gid_b = await _seed_grouped_docs(
                StickerRepository(), StickerGroupRepository()
            )
            invalidate_sticker_cache()
            # 分组A内随机只可能取到两张猫系表情
            for _ in range(5):
                hit = await pick_random_sticker([gid_a])
                assert hit is not None
                assert hit.name in ("开心大笑", "晚安")
            # 分组B仅一张
            hit = await pick_random_sticker([gid_b])
            assert hit is not None and hit.name == "难过哭泣"

        run_with_db(body)


class TestStickerSemanticSearch:
    """启用向量化时的语义检索（monkeypatch embedding 服务）。"""

    def test_semantic_hit_and_threshold(self) -> None:
        async def body() -> None:
            group_repo = StickerGroupRepository()
            gid = await group_repo.create(StickerGroupDocument(name="测试组"))
            repo = StickerRepository()
            await repo.create(_make_sticker(
                "开心大笑", ["开心"], "开心地大笑",
                embedding=[1.0, 0.0, 0.0],
                group_id=gid,
            ))
            invalidate_sticker_cache()

            # 伪向量服务：查询词含"开心"时返回与库向量一致的向量
            async def fake_embed(t: str) -> list[float]:
                return _fake_embed(t)

            fake = SimpleNamespace(enabled=True, embed=fake_embed)
            import app.services.embedding_service as emb_mod

            old_fn = emb_mod.get_embedding_service
            emb_mod.get_embedding_service = lambda: fake  # type: ignore[method-assign,assignment,return-value]

            old_threshold = settings.sticker_similarity_threshold
            settings.sticker_similarity_threshold = 0.28
            try:
                hit = await search_sticker("很开心", [gid])
                assert hit is not None and hit.name == "开心大笑"
                # 无关查询：向量为 0 向量相似度不足，且无关键词命中 → None
                assert await search_sticker("完全无关的语句", [gid]) is None
            finally:
                emb_mod.get_embedding_service = old_fn
                settings.sticker_similarity_threshold = old_threshold

        run_with_db(body)


def _fake_embed(text: str) -> list[float]:
    """伪向量：命中"开心"返回 [1,0,0]，否则零向量。"""
    if "开心" in text:
        return [1.0, 0.0, 0.0]
    return [0.0, 0.0, 0.0]


class TestEngineStickerResolve:
    """引擎侧表情包任务组装与以图回图判定（限定角色绑定分组）。"""

    def test_is_sticker_only_message(self) -> None:
        """纯表情包消息判定（单个/多个标签/微信转写/普通文本）。"""
        assert ConversationEngine._is_sticker_only_message("[表情:微笑]")
        assert ConversationEngine._is_sticker_only_message("[表情][表情:大哭]")
        assert ConversationEngine._is_sticker_only_message(
            f"{STICKER_TRANSCRIBE_PREFIX}，含义：开心]"
        )
        assert not ConversationEngine._is_sticker_only_message("你好呀")
        assert not ConversationEngine._is_sticker_only_message("[表情:微笑] 哈哈")
        assert not ConversationEngine._is_sticker_only_message("")

    def test_resolve_tasks_from_llm_tag(self) -> None:
        """LLM 标签 → 检索命中组装任务，受 max_per_turn 限制。"""
        async def body() -> None:
            engine = ConversationEngine(bot_id="test-bot")
            doc = _make_sticker("开心大笑", ["开心"], "开心地大笑")
            doc.id = "64b000000000000000000001"

            async def fake_available(group_ids: list[str]) -> bool:
                return bool(group_ids)

            async def fake_search(desc: str, group_ids: list[str]):
                return doc

            old_enabled = settings.sticker_enabled
            old_max = settings.sticker_max_per_turn
            settings.sticker_enabled = True
            settings.sticker_max_per_turn = 1
            old_available = sticker_service.sticker_library_available
            old_search = sticker_service.search_sticker
            sticker_service.sticker_library_available = fake_available  # type: ignore[method-assign]
            sticker_service.search_sticker = fake_search  # type: ignore[method-assign]
            try:
                tasks = await engine._resolve_sticker_tasks(
                    ["大笑", "晚安"], "今天真高兴", _fake_profile(["g1"])
                )
                assert len(tasks) == 1  # max_per_turn=1 截断
                assert tasks[0].description == "大笑"
                assert tasks[0].sticker_id == doc.id
            finally:
                settings.sticker_enabled = old_enabled
                settings.sticker_max_per_turn = old_max
                sticker_service.sticker_library_available = old_available
                sticker_service.search_sticker = old_search

        run_with_db(body)

    def test_resolve_tasks_doutu_random(self) -> None:
        """以图回图：纯表情消息 + 概率命中 → 随机任务；普通消息不触发。"""
        async def body() -> None:
            engine = ConversationEngine(bot_id="test-bot")
            doc = _make_sticker("晚安", ["晚安"], "说晚安")
            doc.id = "64b000000000000000000002"

            async def fake_available(group_ids: list[str]) -> bool:
                return bool(group_ids)

            async def fake_random(group_ids: list[str]):
                return doc

            async def fake_search(desc: str, group_ids: list[str]):
                return None

            old_enabled = settings.sticker_enabled
            old_prob = settings.sticker_reply_probability
            old_casual = settings.sticker_casual_probability
            settings.sticker_enabled = True
            settings.sticker_reply_probability = 1.0
            # 关闭概率性穿插：否则"普通文本不触发"这一断言会以 15% 概率随机失败
            settings.sticker_casual_probability = 0.0
            old_available = sticker_service.sticker_library_available
            old_random = sticker_service.pick_random_sticker
            old_search = sticker_service.search_sticker
            sticker_service.sticker_library_available = fake_available  # type: ignore[method-assign]
            sticker_service.pick_random_sticker = fake_random  # type: ignore[method-assign]
            sticker_service.search_sticker = fake_search  # type: ignore[method-assign]
            try:
                profile = _fake_profile(["g1"])
                tasks = await engine._resolve_sticker_tasks(
                    [], "[表情:微笑]", profile
                )
                assert len(tasks) == 1
                assert tasks[0].description == "以图回图"
                # 普通文本消息不触发斗图
                assert await engine._resolve_sticker_tasks(
                    [], "你好", profile
                ) == []
            finally:
                settings.sticker_enabled = old_enabled
                settings.sticker_reply_probability = old_prob
                settings.sticker_casual_probability = old_casual
                sticker_service.sticker_library_available = old_available
                sticker_service.pick_random_sticker = old_random
                sticker_service.search_sticker = old_search

        run_with_db(body)

    def test_resolve_tasks_no_group_zero_cost(self) -> None:
        """角色未绑定分组时直接返回空列表，不触碰检索服务。"""
        async def body() -> None:
            engine = ConversationEngine(bot_id="test-bot")

            async def fail_available(group_ids: list[str]) -> bool:
                raise AssertionError("未绑定分组不应调用检索服务")

            old_enabled = settings.sticker_enabled
            settings.sticker_enabled = True
            old_available = sticker_service.sticker_library_available
            sticker_service.sticker_library_available = fail_available  # type: ignore[method-assign]
            try:
                assert await engine._resolve_sticker_tasks(
                    ["大笑"], "[表情:微笑]", _fake_profile([])
                ) == []
            finally:
                settings.sticker_enabled = old_enabled
                sticker_service.sticker_library_available = old_available

        run_with_db(body)

    def test_resolve_tasks_disabled_zero_cost(self) -> None:
        """开关关闭时直接返回空列表，不触碰检索服务。"""
        async def body() -> None:
            engine = ConversationEngine(bot_id="test-bot")
            old_enabled = settings.sticker_enabled
            settings.sticker_enabled = False
            try:
                assert await engine._resolve_sticker_tasks(
                    ["大笑"], "[表情:微笑]", _fake_profile(["g1"])
                ) == []
            finally:
                settings.sticker_enabled = old_enabled

        run_with_db(body)


class TestStickerSeeder:
    """启动 seed：幂等导入 + 分组 upsert/回填 + 向量回填挂载点（mongomock）。"""

    def test_seed_with_group_upsert(self, tmp_path: Path) -> None:
        """manifest 带 group：按名称 upsert 分组并写入 group_id；二次幂等。"""
        fake_img = b"\x89PNG fake image bytes" * 10
        (tmp_path / "st_001_test.gif").write_bytes(fake_img)
        (tmp_path / "manifest.json").write_text(json.dumps([
            {
                "file": "st_001_test.gif",
                "name": "测试表情",
                "tags": ["测试"],
                "description": "测试用表情包",
                "source": "ChineseBQB",
                "group": "萌猫日常",
                "enabled": True,
            }
        ]), encoding="utf-8")

        async def body() -> None:
            import app.services.sticker_seeder as seeder_mod
            import app.utils.tasks as tasks_mod

            def fake_spawn(coro) -> None:
                # 吞掉后台回填协程，避免事件循环关闭告警
                coro.close()

            old_dir = seeder_mod.STICKERS_DIR
            old_spawn = tasks_mod.spawn
            old_seed_enabled = settings.sticker_seed_enabled
            seeder_mod.STICKERS_DIR = tmp_path
            tasks_mod.spawn = fake_spawn  # type: ignore[assignment]
            settings.sticker_seed_enabled = True
            try:
                await seeder_mod.seed_stickers()
                sticker_repo = StickerRepository()
                group_repo = StickerGroupRepository()
                stickers = await sticker_repo.list_enabled()
                assert len(stickers) == 1
                groups = await group_repo.list_all()
                assert [g.name for g in groups] == ["萌猫日常"]
                assert stickers[0].group_id == groups[0].id

                # 第二次导入：哈希命中跳过，分组不重复创建
                await seeder_mod.seed_stickers()
                assert len(await sticker_repo.list_enabled()) == 1
                assert len(await group_repo.list_all()) == 1
            finally:
                seeder_mod.STICKERS_DIR = old_dir
                tasks_mod.spawn = old_spawn
                settings.sticker_seed_enabled = old_seed_enabled

        run_with_db(body)

    def test_seed_backfill_group_of_legacy(self, tmp_path: Path) -> None:
        """存量哈希命中且 group_id 为空的老表情包，按 manifest 幂等回填。"""
        fake_img = b"\x89PNG legacy image bytes" * 10
        (tmp_path / "st_001_legacy.gif").write_bytes(fake_img)
        (tmp_path / "manifest.json").write_text(json.dumps([
            {
                "file": "st_001_legacy.gif",
                "name": "存量表情",
                "tags": ["存量"],
                "description": "存量表情包",
                "source": "ChineseBQB",
                "group": "开心鸭",
                "enabled": True,
            }
        ]), encoding="utf-8")

        async def body() -> None:
            import hashlib

            import app.services.sticker_seeder as seeder_mod
            import app.utils.tasks as tasks_mod
            from app.models.sticker import StickerDocument

            def fake_spawn(coro) -> None:
                coro.close()

            # 预置一条无分组的老记录（哈希与文件一致）
            sticker_repo = StickerRepository()
            content_hash = hashlib.sha256(fake_img).hexdigest()
            await sticker_repo.create(StickerDocument(
                name="存量表情", media_url="/api/media/legacy",
                content_hash=content_hash, source="ChineseBQB",
            ))

            old_dir = seeder_mod.STICKERS_DIR
            old_spawn = tasks_mod.spawn
            old_seed_enabled = settings.sticker_seed_enabled
            seeder_mod.STICKERS_DIR = tmp_path
            tasks_mod.spawn = fake_spawn  # type: ignore[assignment]
            settings.sticker_seed_enabled = True
            try:
                await seeder_mod.seed_stickers()
                # 未新增（哈希命中），但 group_id 已回填
                stickers = await sticker_repo.list_enabled()
                assert len(stickers) == 1
                assert stickers[0].group_id != ""
                group = await StickerGroupRepository().find_by_name("开心鸭")
                assert group is not None
                assert stickers[0].group_id == group.id
            finally:
                seeder_mod.STICKERS_DIR = old_dir
                tasks_mod.spawn = old_spawn
                settings.sticker_seed_enabled = old_seed_enabled

        run_with_db(body)

    def test_seed_disabled(self, tmp_path: Path) -> None:
        """sticker_seed_enabled=False 时跳过导入。"""
        (tmp_path / "manifest.json").write_text("[]", encoding="utf-8")

        async def body() -> None:
            import app.services.sticker_seeder as seeder_mod

            old_dir = seeder_mod.STICKERS_DIR
            old_seed_enabled = settings.sticker_seed_enabled
            seeder_mod.STICKERS_DIR = tmp_path
            settings.sticker_seed_enabled = False
            try:
                await seeder_mod.seed_stickers()
                assert await StickerRepository().list_enabled() == []
            finally:
                seeder_mod.STICKERS_DIR = old_dir
                settings.sticker_seed_enabled = old_seed_enabled

        run_with_db(body)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
