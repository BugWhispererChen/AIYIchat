"""P2 加固项的契约用例：安全响应头、文档暴露范围、后台模板文案注入面、条数上限与会话竞态。"""

import asyncio
import re
from pathlib import Path
from typing import Any

import mongomock_motor

from app.config import settings
from app.db import mongo

_APP_DIR = Path(__file__).resolve().parents[2] / "app"

EXPECTED_HEADERS = {
    "x-frame-options": "SAMEORIGIN",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def _client():
    """免登录访问后台所需的内存应用（复用 admin 测试的依赖覆盖套路）。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def test_security_headers_on_every_response() -> None:
    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_sec_headers"]
        old_env = settings.app_env
        settings.app_env = "production"
        try:
            async with _client() as client:
                for path in ("/admin/login", "/web/login", "/api/health/live"):
                    resp = await client.get(path)
                    for header, value in EXPECTED_HEADERS.items():
                        assert resp.headers.get(header) == value, (path, header)
                    assert "frame-ancestors 'self'" in resp.headers.get(
                        "content-security-policy", ""
                    ), path
                    assert resp.headers.get("strict-transport-security") == "max-age=31536000"
        finally:
            settings.app_env = old_env
            mongo._db = None

    asyncio.run(case())


def test_api_docs_not_public_in_production() -> None:
    """production 下 `/docs` 与 `/openapi.json` 不注册：路由清单本身就是攻击面目录。"""
    from app.bootstrap import create_app

    old_env = settings.app_env
    try:
        settings.app_env = "production"
        app = create_app()
        assert app.docs_url is None
        assert app.openapi_url is None
        settings.app_env = "development"
        dev_app = create_app()
        assert dev_app.docs_url == "/docs"
    finally:
        settings.app_env = old_env


def test_admin_templates_do_not_inject_upstream_text_into_html() -> None:
    """后台模板不许把上游接口的 message 拼进 innerHTML（第三方接口可控的 DOM 注入面）。"""
    offender = re.compile(r"innerHTML\s*=\s*'[^']*'\+\s*\(?(\w+)\.message")
    hits = [
        f"{html.relative_to(_APP_DIR)}: {offender.search(text).group(0)}"
        for html in sorted(_APP_DIR.rglob("*.html"))
        for text in [html.read_text(encoding="utf-8")]
        if offender.search(text)
    ]
    assert not hits, "模板把上游文案拼进了 innerHTML：\n" + "\n".join(hits)

def test_legal_pages_render_with_version() -> None:
    """《用户协议》《隐私政策》必须真能打开，且版本号与凭据里记录的同意版本同源。"""
    from app.models.web_credential import PRIVACY_VERSION, TERMS_VERSION

    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_legal_pages"]
        try:
            async with _client() as client:
                for path, title, version in (
                    ("/web/terms", "用户协议", TERMS_VERSION),
                    ("/web/privacy", "隐私政策", PRIVACY_VERSION),
                ):
                    resp = await client.get(path)
                    assert resp.status_code == 200, path
                    assert title in resp.text, path
                    assert f"版本 {version}" in resp.text, path
                # 隐私政策必须回答"数据存在哪、会发给谁"，否则等于没披露
                privacy = (await client.get("/web/privacy")).text
                for key in ("PBKDF2", "百炼", "备份", "Cookie", "删除"):
                    assert key in privacy, key
        finally:
            mongo._db = None

    asyncio.run(case())

def test_display_stream_cleaner_holds_partial_tags() -> None:
    """增量下发不能把半截内部标签推给浏览器，且拼起来等于整段清洗结果。"""
    from app.conversation.response_processor import (
        DisplayStreamCleaner,
        ResponseProcessor,
    )

    processor = ResponseProcessor()
    full = "（微笑）好的，看[IMG:realistic photo, sunset]这片海[表情:开心]"
    cleaner = DisplayStreamCleaner(processor)
    pieces: list[str] = []
    for char in full:  # 最坏情况：一个字一个增量
        pieces += cleaner.push(char)
    pieces += cleaner.flush()
    joined = "".join(pieces)

    assert "[IMG:" not in joined and "[表情:" not in joined, joined
    assert "realistic photo" not in joined, joined
    assert "微笑" not in joined.replace("😊", ""), joined
    assert joined == processor.clean_display(full), (joined, processor.clean_display(full))
    assert "好的" in joined and "这片海" in joined


def test_display_stream_cleaner_emits_promptly() -> None:
    """没有未闭合标签时必须立刻下发，不能把流式体验憋成整段。"""
    from app.conversation.response_processor import DisplayStreamCleaner, ResponseProcessor

    cleaner = DisplayStreamCleaner(ResponseProcessor())
    assert cleaner.push("今天过得怎么样") == ["今天过得怎么样"]
    # 标签闭合后其内容一并下发（动作标签转 emoji 或删除）
    emitted = "".join(cleaner.push("（点头）嗯"))
    cleaner.flush()
    assert "点头" not in emitted, emitted

def test_concurrent_find_or_create_creates_one_conversation() -> None:
    """并发首轮消息只能建一条活跃会话：多出来的那些永远读不到历史与计数。"""
    from app.db.repositories.conversation_repo import ConversationRepository

    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_conv_race"]
        try:
            repo = ConversationRepository()
            convs = await asyncio.gather(
                *(repo.find_or_create("id-1", "p-1", "svc-1") for _ in range(6))
            )
            assert len({str(c.id) for c in convs}) == 1, [str(c.id) for c in convs]
        finally:
            mongo._db = None

    asyncio.run(case())


def test_chat_text_length_is_bounded() -> None:
    """一条消息塞进整本小说会被拒：提示词与计费都会被拖爆。"""
    from app.api.conversations_api import _MAX_CHAT_CHARS

    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_text_bound"]
        try:
            from app.main import app

            async with _client() as client:
                resp = await client.post(
                    "/api/conversations/chat/stream",
                    json={"text": "x" * (_MAX_CHAT_CHARS + 1)},
                    headers={"authorization": "Bearer nope"},
                )
                # 结构校验在鉴权之前：超长直接 422，不进入模型调用
                assert resp.status_code == 422, resp.status_code
        finally:
            mongo._db = None
            app.dependency_overrides.clear()

    asyncio.run(case())


def test_qr_poll_messages_are_not_raw_exception_text() -> None:
    """扫码轮询的 message 会经后台与 C 端两条接口原样返回，不许塞异常原文。

    行为用例要铺 session/bot/manager 一整套替身，这里先钉住形态：
    把 `str(exc)` 塞进 `message` 的写法会立刻红，改回去也立刻看得见。
    """
    source = (_APP_DIR / "services" / "bot_qr_service.py").read_text(encoding="utf-8")
    banned = [
        '"message": str(exc)',
        '"message": msg',
        'message=f"{exc}"',
    ]
    offenders = [item for item in banned if item in source]
    assert not offenders, "bot_qr_service 又把异常原文回显给调用方了：" + ", ".join(offenders)

def test_duplicate_key_in_create_falls_back_to_existing_conversation() -> None:
    """跨实例竞态：别人先建成功时用它那条，而不是把 DuplicateKeyError 抛给用户。

    进程内锁只能挡单实例；多进程/多实例下靠 conversations 的部分唯一索引兜底，
    所以捕获分支必须真的回到"查活跃会话"。
    """
    from pymongo.errors import DuplicateKeyError

    from app.db.repositories.conversation_repo import ConversationRepository

    async def case() -> None:
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_race_fallback"]
        try:
            repo = ConversationRepository()
            winner = await repo.create("id-1", "p-1", "svc-1")

            async def _boom(*args: Any, **kwargs: Any):
                raise DuplicateKeyError("E11000 duplicate key error")

            original_create = repo.create
            repo.create = _boom  # type: ignore[method-assign]
            try:
                got = await repo.find_or_create("id-1", "p-1", "svc-1")
            finally:
                repo.create = original_create  # type: ignore[method-assign]
            assert str(got.id) == str(winner.id), (got.id, winner.id)
        finally:
            mongo._db = None

    asyncio.run(case())

def test_unclosed_bracket_cannot_stall_the_stream() -> None:
    """模型吐出永不闭合的 `[` 时，屏幕不能整条空白：扣住量有上限。"""
    from app.conversation.response_processor import (
        _MAX_DISPLAY_HOLD,
        DisplayStreamCleaner,
        ResponseProcessor,
    )

    cleaner = DisplayStreamCleaner(ResponseProcessor())
    emitted: list[str] = []
    # 一个开括号后面跟超长正文，全程没有任何闭括号
    payload = "[没有闭合" + "字" * (_MAX_DISPLAY_HOLD * 3)
    for char in payload:
        emitted += cleaner.push(char)
    joined = "".join(emitted)
    assert len(joined) > _MAX_DISPLAY_HOLD, (
        f"扣住量失控：只下发了 {len(joined)} 字"
    )
    assert len("".join(cleaner.flush())) <= _MAX_DISPLAY_HOLD



def test_long_image_tag_is_held_until_closed() -> None:
    """长 [IMG:…] 提示词不能被扣住上限提前放出，否则英文画面描述会漏到屏幕。"""
    from app.conversation.response_processor import (
        DisplayStreamCleaner,
        ResponseProcessor,
    )

    tag = "[IMG:" + "画面描述" * 80 + "]"
    cleaner = DisplayStreamCleaner(ResponseProcessor())
    emitted: list[str] = []
    for char in ("你好" + tag + "收到"):
        emitted += cleaner.push(char)
    emitted += cleaner.flush()
    joined = "".join(emitted)
    assert "画面描述" not in joined, joined[:200]
    assert "你好" in joined and "收到" in joined
