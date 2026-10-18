"""迁移脚本 rename_user_to_identity 的纯逻辑单测（不连数据库）。

覆盖三件最容易出错、且一旦出错代价很高的事：
- 僵尸索引清理白名单是否漏了某个被改名的 (集合, 键)
- 操作顺序是否真的先腾空了 identity_id 这个键名（否则同名字段会同时指向两张表）
- 回滚序列是否与正向严格互逆
另外守住协议边界：user_id 在 wechat_context_tokens 里存的是平台 openid，属合法字段，
迁移绝不能碰。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "rename_user_to_identity.py"
)


def _load() -> Any:
    """按文件路径加载迁移脚本（tools/migrations 不是包，无法直接 import）。

    必须先登记进 sys.modules 再执行：脚本用了 @dataclass 与
    `from __future__ import annotations`，dataclasses 解析字符串注解时会回查
    cls.__module__ 对应的模块字典，未登记则取到 None 而崩溃。
    """
    spec = importlib.util.spec_from_file_location("rename_user_to_identity", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _field_ops() -> list[Any]:
    return [op for op in mod.OPS if op.__class__.__name__ == "RenameFields"]


class TestIndexWhitelist:
    """索引清理白名单与改名操作保持同步。"""

    def test_whitelist_covers_every_renamed_key(self) -> None:
        """每个被改名的 (集合, 旧键) 都必须在清理白名单里，否则留下僵尸索引拖慢写入。"""
        assert mod.index_targets_from_ops(mod.OPS) == mod.DEPRECATED_INDEX_TARGETS

    def test_profile_persona_rename_is_listed(self) -> None:
        """profiles.identity -> persona 是内嵌文档改名，最容易被漏掉。"""
        assert ("profiles", "identity") in mod.DEPRECATED_INDEX_TARGETS


class TestOperationOrder:
    """顺序约束：先腾空键名，再引入同名键。"""

    def test_identity_id_is_freed_before_being_reintroduced(self) -> None:
        """web_sessions.identity_id 必须先改成 credential_id。

        否则迁移过程中 identity_id 会同时存在"指向 web_identities"（旧）与
        "指向 identities"（新）两种含义，读写会串到错误的表。
        """
        free_at = next(
            i
            for i, op in enumerate(_field_ops())
            if ("identity_id", "credential_id") in op.pairs
        )
        create_at = next(
            i
            for i, op in enumerate(_field_ops())
            if ("user_id", "identity_id") in op.pairs
        )
        assert free_at < create_at

    def test_collection_renamed_before_its_fields(self) -> None:
        """字段改名发生在集合改名之后，否则操作的集合名不存在会被静默跳过。"""
        names = [
            (getattr(op, "old", None), getattr(op, "collection", None)) for op in mod.OPS
        ]
        ren_web_cred = next(i for i, (old, _) in enumerate(names) if old == "web_identities")
        fld_user_id = next(
            i
            for i, (_, coll) in enumerate(names)
            if coll == "web_credentials"
        )
        assert ren_web_cred < fld_user_id
        assert "web_credentials" not in {c for _, c in names[:ren_web_cred]}

    def test_users_collection_renamed_before_identity_fields(self) -> None:
        """users -> identities 必须先于 identities 上的字段改名。"""
        idx_users = next(
            i for i, op in enumerate(mod.OPS) if getattr(op, "old", None) == "users"
        )
        idx_desc = next(
            i
            for i, op in enumerate(mod.OPS)
            if getattr(op, "collection", None) == "identities"
        )
        assert idx_users < idx_desc


class TestProtocolBoundary:
    """外部协议字段禁止迁移改名。"""

    def test_wechat_context_tokens_user_id_untouched(self) -> None:
        """wechat_context_tokens.user_id 存的是微信 uid，不是本项目身份主键。"""
        colls = {getattr(op, "collection", None) for op in mod.OPS}
        assert "wechat_context_tokens" not in colls

    def test_messages_role_untouched(self) -> None:
        """messages.role 的值是 OpenAI 协议取值，且已入库并建索引。"""
        for op in _field_ops():
            if op.collection == "messages":
                assert all(new != "role" for _, new in op.pairs)


class TestRollback:
    """回滚与正向严格互逆。"""

    def test_pairs_are_swapped_and_order_reversed(self) -> None:
        inv = mod.inverted_ops(mod.OPS)
        assert len(inv) == len(mod.OPS)
        first_fwd, last_inv = mod.OPS[0], inv[-1]
        assert first_fwd.pairs == tuple((n, o) for o, n in last_inv.pairs)

    def test_collection_rollback_is_symmetric(self) -> None:
        inv = mod.inverted_ops(mod.OPS)
        fwd_cols = [(op.old, op.new) for op in mod.OPS if op.__class__.__name__ == "RenameCollection"]
        inv_cols = [(op.old, op.new) for op in inv if op.__class__.__name__ == "RenameCollection"]
        assert inv_cols == [(n, o) for o, n in reversed(fwd_cols)]
