"""命名边界守卫：守住「用户→身份」改名后的两条红线。

一是**外部协议锁死**的 user 必须原样保留（改了会直接断功能，且部分是已入库的存量值）；
二是**项目自有旧符号**必须改净（漏改会让文档与代码不一致，误导后来者）。

这两类约束靠人记是记不住的：本次改名中就出现过适配层 `[身份发送了一个表情包]`
与引擎判定 `[对方发送了一个表情包]` 分叉、以及提示词要求 LLM 输出 `relation_to_user`
而解析端读 `relation_to_identity` 的静默丢数据。本测试把这些面固化下来。
"""

from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
TEMPLATES = APP / "admin" / "templates"


def _read(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _py_files() -> list[Path]:
    return [p for p in APP.rglob("*.py")]


class TestProtocolLiteralsPreserved:
    """外部协议值：绝对不可改名。"""

    def test_llm_role_user_literal(self) -> None:
        """OpenAI 兼容 role 取值，且已作为存量值写入 messages 并建索引。"""
        assert 'Literal["user", "assistant", "system"]' in _read("models/message.py")

    def test_role_never_renamed_to_identity(self) -> None:
        """协议值不变式：任何地方都不得出现 role="identity"。

        这里断言"不该出现的东西"而非"该出现多少次"——计数阈值会随正常改名漂移
        （本次改名中模板键 "user"→"identity" 就让计数自然减少了 3），
        而"协议值被改掉"永远是错的。
        """
        hits: list[str] = []
        for p in _py_files():
            text = p.read_text(encoding="utf-8")
            for bad in ('role="identity"', "role='identity'", '"role": "identity"',
                        'role == "identity"', "MessageRole"):
                if bad in text:
                    hits.append(f"{p.relative_to(APP)}: {bad}")
        assert not hits, "LLM 协议 role 值被误改:\n" + "\n".join(hits)

    def test_role_user_count_not_shrunk(self) -> None:
        """全库 "user" 字面量计数不得跌破基线（防止批量改名波及协议值）。"""
        total = sum(p.read_text(encoding="utf-8").count('"user"') for p in _py_files())
        assert total >= 25, f'"user" 协议值仅剩 {total} 处，疑似被误改'

    def test_qq_official_paths_and_keys(self) -> None:
        src = _read("adapters/qq_websocket.py")
        assert "/v2/users/" in src          # 官方 URL 路径字面量
        assert "user_openid" in src         # 官方响应键
        assert "member_openid" in src
        assert "group_openid" in src

    def test_wechat_payload_keys(self) -> None:
        src = _read("adapters/wechat_clawbot.py")
        assert "from_user_id" in src        # iLink payload 键
        assert "to_user_id" in src

    def test_login_form_field_username(self) -> None:
        """HTTP 表单字段名保留 username（与 CredentialStatus 字段同名）。"""
        for rel in ["web/templates/login.html", "web/templates/register.html"]:
            assert 'name="username"' in (APP / rel).read_text(encoding="utf-8")

    def test_admin_session_cookie_name(self) -> None:
        assert '"admin_session"' in _read("admin/auth.py")

    def test_user_agent_field_untouched(self) -> None:
        """user_agent 是 HTTP 标准概念，不属"用户"语义，不得被改名波及。"""
        assert "user_agent" in _read("models/web_session.py")


class TestLegacySymbolsRemoved:
    """项目自有旧符号：必须改净。"""

    # (旧符号, 说明) —— 出现在 app/ 任意 py 即为漏改
    FORBIDDEN: list[str] = [
        "UserRepository",
        "UserInDB",
        "UserCreate",
        "require_web_user",
        "ProfileIdentity",
        "identity_description",
        "web_identities",
        "WebIdentityInDB",
        "relation_to_user",
        "platform_user_id",
    ]

    def test_no_legacy_symbols(self) -> None:
        hits: list[str] = []
        for p in _py_files():
            text = p.read_text(encoding="utf-8")
            for sym in self.FORBIDDEN:
                if sym in text:
                    hits.append(f"{p.relative_to(APP)}: {sym}")
        assert not hits, "漏改的旧符号:\n" + "\n".join(hits)

    def test_no_db_users_attribute(self) -> None:
        """集合已改名，`db.users` 会静默读到一个空集合而非报错。"""
        hits = [
            str(p.relative_to(APP))
            for p in _py_files()
            if "db.users" in p.read_text(encoding="utf-8")
        ]
        assert not hits, f"仍引用 db.users: {hits}"

    def test_new_collection_names_in_use(self) -> None:
        src = _read("db/mongo.py")
        assert "db.identities" in src
        assert "db.web_credentials" in src
        assert "db.web_sessions" in src

    def test_identity_id_has_single_meaning(self) -> None:
        """identity_id 只指向 identities；网页凭据的会话外键必须叫 credential_id。"""
        assert '"credential_id"' in _read("db/repositories/web_session_repo.py")


class TestTemplateConsistency:
    """模板取键必须与路由下发的键名一致（不一致会静默渲染空白而非报错）。"""

    def test_no_stale_profile_identity(self) -> None:
        hits = [
            p.name
            for p in TEMPLATES.rglob("*.html")
            if "profile.identity" in p.read_text(encoding="utf-8")
        ]
        assert not hits, f"模板仍引用 profile.identity（已改名 persona）: {hits}"

    def test_no_stale_row_identity(self) -> None:
        hits = [
            p.name
            for p in TEMPLATES.rglob("*.html")
            if "row.identity" in p.read_text(encoding="utf-8")
        ]
        assert not hits, f"模板仍引用 row.identity（应为 row.credential）: {hits}"

    def test_no_stale_users_context_var(self) -> None:
        hits = [
            p.name
            for p in TEMPLATES.rglob("*.html")
            if "{% for u in users %}" in p.read_text(encoding="utf-8")
        ]
        assert not hits, f"模板仍读 users 上下文变量（已改名 identities）: {hits}"

    def test_form_names_match_backend_keys(self) -> None:
        """模板提交的 name 必须与后端读取的键一致。

        这类错不会报错、ruff/mypy 查不出、没有断言该字段的测试也测不到，
        症状是"用户填了但没保存"。本次改名中 account_detail.html 就漏改过
        name="identity_description"（后端已读 self_description）。
        """
        stale = ["identity_description", 'name="user_id"', "profile.identity"]
        hits: list[str] = []
        for tpl in list(TEMPLATES.rglob("*.html")) + list(
            (APP / "web" / "templates").rglob("*.html")
        ):
            text = tpl.read_text(encoding="utf-8")
            for s in stale:
                if s in text:
                    hits.append(f"{tpl.name}: {s}")
        assert not hits, "模板残留改名前的字段名:\n" + "\n".join(hits)

    def test_status_badge_uses_semantic_tone_class(self) -> None:
        """网页身份的状态徽标必须走设计系统的语义档，不得回退到旧命名。

        旧断言盯的是 `badge-role-user`：它当年是后台通用蓝色徽标，与"用户"语义无关，
        立此断言是为了防止身份改名那次清理把它误当成术语改掉。
        前端结构收敛已把 legacy `badge-*` 全量迁到 `.ui-badge--*` 语义档，
        该类别名在本页不复存在，故守卫改为盯新的语义档出口，意图不变：
        状态色是通用视觉档，不是"用户/身份"术语，不许再被改掉。
        """
        text = (TEMPLATES / "web_credentials.html").read_text(encoding="utf-8")
        assert "{{ badge(" in text, "状态徽标应走 partials/_ui.html 的 badge 宏（出口为 .ui-badge 语义档）"
        assert 'class="badge ' not in text, "legacy badge-* 类已收敛，不应再出现在本页"
        assert "badge-role-user" not in text, "legacy badge-* 类已收敛，不应再出现在本页"


class TestStickerTranscriptionSingleSource:
    """转写标记必须单一来源。

    适配器生成、引擎判定，两处各写一份中文字面量时曾出现措辞分叉，
    结果是纯表情包消息判定整体失效（以图回图不再触发）。
    """

    def test_adapters_use_shared_prefix(self) -> None:
        for rel in ["adapters/qq_websocket.py", "adapters/wechat_clawbot.py"]:
            src = _read(rel)
            assert "STICKER_TRANSCRIBE_PREFIX" in src, f"{rel} 未使用共享标记"

    def test_engine_uses_shared_prefix(self) -> None:
        assert "STICKER_TRANSCRIBE_PREFIX" in _read("conversation/engine.py")

    def test_prefix_is_importable_constant(self) -> None:
        from app.conversation.sticker_task import STICKER_TRANSCRIBE_PREFIX

        assert STICKER_TRANSCRIBE_PREFIX.startswith("[")
