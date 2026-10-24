"""配置键三处登记一致性测试。

背景：运行配置存在 MongoDB 的 app_settings 文档，启动时**覆盖**代码默认值。
一个键要真能被后台改动静默生效，必须同时满足三处登记：
``_RuntimeSettings`` 类属性 + ``_fields()`` 重载白名单 + ``AppSettingsDocument`` 字段。
本项目已经吃过两次亏：
- ``focus_max_tokens`` 改了代码默认值但 DB 存着旧值，运行时仍是旧值；
- ``multi_message_*`` 等键压根不在 AppSettingsDocument 里，``to_flat_dict`` 不产出该键，
  后台写入被 pydantic 静默丢弃——等于这些拟人参数根本不可调。

本文件把这条约束固化成测试：新增配置漏登记时立刻失败。
"""

import pytest

from app.admin.routes.features import FEATURE_TOGGLES, _pacing_context
from app.config import settings
from app.models.app_settings import AppSettingsDocument

# settings 的类型即 _RuntimeSettings：类属性就是代码侧默认值
_Settings = type(settings)

# DB 文档里的结构性字段，不参与运行配置覆盖
_STRUCTURAL_FIELDS = {"id", "updated_at"}


def _document_fields() -> set[str]:
    return set(AppSettingsDocument.model_fields)


def _admin_writable_keys() -> set[str]:
    """后台「功能实验室」能写入的全部键。"""
    return {f.key for f in FEATURE_TOGGLES} | set(_pacing_context(settings))


class TestAdminWritableKeysReachDb:
    """后台能改的每一个键，必须真的能落进 DB 文档并被重载生效。"""

    @pytest.mark.parametrize("key", sorted(_admin_writable_keys()))
    def test_key_exists_on_settings(self, key: str) -> None:
        assert hasattr(settings, key), f"{key} 在 Settings 上不存在，后台改它没有意义"

    @pytest.mark.parametrize("key", sorted(_admin_writable_keys()))
    def test_key_in_document(self, key: str) -> None:
        assert (
            key in _document_fields()
        ), f"{key} 未登记进 AppSettingsDocument：to_flat_dict 不产出它，后台写入被静默丢弃"

    @pytest.mark.parametrize("key", sorted(_admin_writable_keys()))
    def test_key_in_reload_whitelist(self, key: str) -> None:
        assert (
            key in set(_Settings._fields())
        ), f"{key} 不在重载白名单：能写进 DB 却永远不会在启动/热重载时生效"


class TestDefaultsAgreeAcrossLayers:
    """后台可调键的默认值，代码侧与 DB 文档侧必须逐字一致。

    不一致时"DB 里没有这个键"和"DB 里存着默认值"两种状态会给出不同运行值，
    排查现网行为时极易误判。只覆盖标量类型（bool/int/float/str），
    结构性字段（dict/list/None 默认）不在本约束范围内。
    """

    @pytest.mark.parametrize("key", sorted(_admin_writable_keys()))
    def test_default_matches(self, key: str) -> None:
        doc_default = AppSettingsDocument.model_fields[key].default
        code_default = getattr(_Settings, key)
        if not isinstance(doc_default, (bool, int, float, str)):
            pytest.skip(f"{key} 默认值为非标量，交由业务侧校验")
        # bool 与 int 在 Python 里相等（True == 1），因此连类型一起比
        assert type(doc_default) is type(code_default), (
            f"{key} 默认值类型不一致：{type(doc_default)} vs {type(code_default)}"
        )
        assert doc_default == code_default, (
            f"{key} 默认值漂移：DB 文档 {doc_default!r} vs 代码 {code_default!r}"
        )


class TestFlatDictCoversDocument:
    """to_flat_dict 是 DB → 运行时的唯一通道，漏字段等于该键永远读不到。"""

    def test_flat_dict_covers_every_nonstructural_field(self) -> None:
        flat = set(AppSettingsDocument().to_flat_dict)
        missing = _document_fields() - _STRUCTURAL_FIELDS - flat
        assert not missing, f"to_flat_dict 未导出这些字段：{sorted(missing)}"

    def test_flat_dict_values_match_fields(self) -> None:
        doc = AppSettingsDocument()
        for key, value in doc.to_flat_dict.items():
            assert getattr(doc, key) == value, f"{key} 扁平化后值不一致"


class TestProactivePageKeysReachDb:
    """「主动关怀」页直接 upsert 的数值参数也必须具备三处登记。

    这些键不在 FEATURE_TOGGLES 也不在 _pacing_context 里，上面那组参数化用例覆盖不到；
    而它们正是历史上"看着能调其实调不动"的那一类（漏登记 → 后台写入被静默丢弃）。
    """

    KEYS = (
        "proactive_daily_limit",
        "proactive_monthly_limit",
        "proactive_max_retries",
        "proactive_unyield_threshold",
        "proactive_yield_hard_limit",
    )

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_key_in_all_layers(self, key: str) -> None:
        assert hasattr(settings, key), f"{key} 在 Settings 上不存在"
        assert key in _document_fields(), f"{key} 未登记进 AppSettingsDocument"
        assert key in set(_Settings._fields()), f"{key} 不在重载白名单"
        assert key in AppSettingsDocument().to_flat_dict, f"{key} 不会被读回运行时"

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_default_matches(self, key: str) -> None:
        doc_default = AppSettingsDocument.model_fields[key].default
        code_default = getattr(_Settings, key)
        assert type(doc_default) is type(code_default), (
            f"{key} 默认值类型不一致：{type(doc_default)} vs {type(code_default)}"
        )
        assert doc_default == code_default, (
            f"{key} 默认值漂移：DB 文档 {doc_default!r} vs 代码 {code_default!r}"
        )


class TestWebChannelPageKeysReachDb:
    """C 端渠道管理页 upsert 的键同样必须三处登记。

    `web_max_profiles` / `web_max_services` 就是在这里漏登记的：表单写进 DB、
    `to_flat_dict` 读回来时没有这个字段，页面刷新即回退默认值，重启也回默认——
    看起来"保存成功"其实完全调不动，所以整页的键一起钉住。
    """

    KEYS = (
        "web_auth_enabled", "web_register_enabled", "web_register_require_review",
        "web_captcha_enabled", "web_bot_id", "web_default_profile_slug",
        "web_free_quota_template_id", "web_session_hours",
        "web_session_max_concurrent", "web_login_max_fail",
        "web_login_lockout_minutes", "web_rate_limit_register",
        "web_rate_limit_login", "web_max_profiles", "web_max_services",
        # 同一页一并提交的后台防爆破阈值
        "admin_login_rate_limit", "admin_login_max_fail",
        "admin_login_lockout_minutes", "trust_proxy_headers",
    )

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_key_in_all_layers(self, key: str) -> None:
        assert hasattr(settings, key), f"{key} 在 Settings 上不存在"
        assert key in _document_fields(), f"{key} 未登记进 AppSettingsDocument"
        assert key in set(_Settings._fields()), f"{key} 不在重载白名单"
        assert key in AppSettingsDocument().to_flat_dict, f"{key} 不会被读回运行时"

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_default_matches(self, key: str) -> None:
        doc_default = AppSettingsDocument.model_fields[key].default
        code_default = getattr(_Settings, key)
        assert type(doc_default) is type(code_default), (
            f"{key} 默认值类型不一致：{type(doc_default)} vs {type(code_default)}"
        )
        assert doc_default == code_default, (
            f"{key} 默认值漂移：DB 文档 {doc_default!r} vs 代码 {code_default!r}"
        )


class _FakeSettings:
    """后台表单读写用的最小配置替身：只带节奏相关键。"""

    stream_gate_max_pending = 40
    stream_max_total_delay_seconds = 20.0
    human_typing_ms_per_char = 85
    human_gap_min_ms = 700
    human_gap_max_ms = 2600
    human_startup_min_ms = 250
    human_startup_max_ms = 900
    human_jitter_ratio = 0.2
    human_night_factor = 1.6
    human_long_pause_prob = 0.25
    stream_jitter_precheck_chars = 24
    stream_jitter_prefix_ratio = 0.5
    stream_jitter_overlap_ratio = 0.35
    stream_jitter_reset_max = 1
    multi_message_max_count = 5
    stream_max_bubbles = 5
    pacing_auto_split_threshold = 120
    pacing_target_chunk_chars = 60
    pacing_min_chunk_chars = 12


class TestPacingFormUpdates:
    """后台保存节奏参数时的限幅与容错。"""

    @staticmethod
    def _updates(form: dict):
        from app.admin.routes.features import _pacing_updates

        return _pacing_updates(form, _FakeSettings())

    def test_missing_keys_keep_current_values(self) -> None:
        updates = self._updates({})
        assert updates["human_gap_min_ms"] == 700
        assert updates["stream_gate_max_pending"] == 40

    def test_invalid_values_keep_current(self) -> None:
        updates = self._updates(
            {"human_typing_ms_per_char": "abc", "stream_jitter_reset_max": ""}
        )
        assert updates["human_typing_ms_per_char"] == 85
        assert updates["stream_jitter_reset_max"] == 1

    def test_out_of_range_clamped(self) -> None:
        updates = self._updates(
            {
                "human_typing_ms_per_char": "999999",
                "human_jitter_ratio": "5",
                "human_night_factor": "0.2",
                "stream_jitter_prefix_ratio": "0",
            }
        )
        assert updates["human_typing_ms_per_char"] == 400
        assert updates["human_jitter_ratio"] == 0.9
        assert updates["human_night_factor"] == 1.0
        assert updates["stream_jitter_prefix_ratio"] == 0.1

    def test_inverted_gap_pair_swapped_not_dropped(self) -> None:
        # 运营把下限填得比上限大时交换，而不是静默取一个奇怪的值
        updates = self._updates({"human_gap_min_ms": "5000", "human_gap_max_ms": "800"})
        assert updates["human_gap_min_ms"] == 800
        assert updates["human_gap_max_ms"] == 5000

    def test_inverted_startup_pair_swapped(self) -> None:
        updates = self._updates(
            {"human_startup_min_ms": "3000", "human_startup_max_ms": "200"}
        )
        assert (updates["human_startup_min_ms"], updates["human_startup_max_ms"]) == (
            200, 3000,
        )

    def test_all_pacing_keys_are_returned(self) -> None:
        from app.admin.routes.features import _pacing_context

        assert set(self._updates({})) == set(_pacing_context(_FakeSettings()))

    def test_zero_delay_guard_is_allowed(self) -> None:
        """护栏填 0 = 不限节奏全速下发，不能被当成"非法值"回退成 20。"""
        updates = self._updates({"stream_max_total_delay_seconds": "0"})
        assert updates["stream_max_total_delay_seconds"] == 0.0

    def test_reset_zero_disables_recovery(self) -> None:
        updates = self._updates({"stream_jitter_reset_max": "0"})
        assert updates["stream_jitter_reset_max"] == 0

    def test_split_thresholds_keep_ordering(self) -> None:
        """分条三阈值必须 起切 ≥ 目标 ≥ 碎片下限：填反了整体重排，而不是交给 splitter 一个切不动的组合。"""
        updates = self._updates(
            {
                "pacing_auto_split_threshold": "50",
                "pacing_target_chunk_chars": "80",
                "pacing_min_chunk_chars": "90",
            }
        )
        assert (
            updates["pacing_auto_split_threshold"]
            >= updates["pacing_target_chunk_chars"]
            >= updates["pacing_min_chunk_chars"]
        )
        # 90 被下限幅到 80，三个值按大小重排回三个字段
        assert updates["pacing_auto_split_threshold"] == 80
        assert updates["pacing_target_chunk_chars"] == 80
        assert updates["pacing_min_chunk_chars"] == 50

    def test_split_thresholds_keep_defaults_when_absent(self) -> None:
        updates = self._updates({})
        assert updates["pacing_auto_split_threshold"] == 120
        assert updates["pacing_target_chunk_chars"] == 60
        assert updates["pacing_min_chunk_chars"] == 12

    def test_bubble_cap_zero_means_unlimited(self) -> None:
        """流式气泡上限填 0 = 不限制，不能被限幅成 1 条。"""
        updates = self._updates({"stream_max_bubbles": "0"})
        assert updates["stream_max_bubbles"] == 0

    def test_message_count_cannot_be_zero(self) -> None:
        """Bot 侧条数下限是 1：填 0 会让整轮回复一条都发不出去。"""
        updates = self._updates({"multi_message_max_count": "0"})
        assert updates["multi_message_max_count"] == 1


class TestDotenvOnlyKeysNowReachDb:
    """原先只在 .env 生效的 14 个键必须完成三处登记。

    它们不在后台表单里，所以前两组成员覆盖不到；漏登记的后果是
    "写进库也读不回来"，与 web_max_* 是同一类静默失效。
    """

    KEYS = (
        "chat_presence_penalty", "summary_max_length", "topic_history_max",
        "topic_stall_detection_enabled", "topic_stall_threshold",
        "topic_stall_similarity", "memory_max_facts", "memory_extract_interval",
        "memory_inject_limit", "memory_max_entities", "memory_entity_scan_turns",
        "memory_entity_merge_threshold", "proactive_llm_temperature",
        "sticker_max_dimension",
    )

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_key_in_all_layers(self, key: str) -> None:
        assert hasattr(settings, key), f"{key} 在 Settings 上不存在"
        assert key in _document_fields(), f"{key} 未登记进 AppSettingsDocument"
        assert key in set(_Settings._fields()), f"{key} 不在重载白名单"

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_default_matches(self, key: str) -> None:
        doc_default = AppSettingsDocument.model_fields[key].default
        code_default = getattr(_Settings, key)
        assert type(doc_default) is type(code_default), (
            f"{key} 默认值类型不一致：{type(doc_default)} vs {type(code_default)}"
        )
        assert doc_default == code_default, (
            f"{key} 默认值漂移：DB 文档 {doc_default!r} vs 代码 {code_default!r}"
        )

    def test_stored_view_skips_keys_absent_in_db(self) -> None:
        """库里没这个键时不能用代码默认值覆盖运行时的 .env 配置。

        这是补登记新键的前提：`to_flat_dict` 全量下发的话，老实例一升级，
        部署者在 .env 配好的值就会被文档默认值悄悄改掉。
        """
        doc = AppSettingsDocument(memory_max_facts=99)
        assert "memory_max_facts" in doc.to_stored_dict
        assert "summary_max_length" not in doc.to_stored_dict, "库里没有的键不该下发"
        # 但全量视图仍然完整：展示层与契约用例靠它
        assert "summary_max_length" in doc.to_flat_dict
        assert doc.to_stored_dict["memory_max_facts"] == 99

    def test_load_from_db_keeps_dotenv_value_for_absent_key(self) -> None:
        """走真实加载路径复核：缺失键保留原值，存在键生效。"""
        import asyncio

        import mongomock_motor

        from app.db import mongo
        from app.config import load_settings_from_db

        async def case() -> None:
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_stored_only"]
            old_facts = settings.memory_max_facts
            old_limit = settings.memory_inject_limit
            try:
                settings.memory_inject_limit = 4242  # 模拟 .env 里部署者配的值
                from app.db.repositories.app_settings_repo import AppSettingsRepository

                await AppSettingsRepository().upsert({"memory_max_facts": 12})
                await load_settings_from_db()
                assert settings.memory_max_facts == 12
                assert settings.memory_inject_limit == 4242, (
                    "库里没有 memory_inject_limit，却被代码默认值覆盖了 .env 配置"
                )
            finally:
                settings.memory_max_facts = old_facts
                settings.memory_inject_limit = old_limit
                mongo._db = None

        asyncio.run(case())
