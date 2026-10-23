"""Admin 账号管理路由测试（mongomock + ASGI 内存客户端）。

覆盖 accounts.py 的列表/创建/详情/编辑/删除、额度更新、
身份管理、Bot 管理与服务管理（含共享组校验与级联删除保护）。
"""

import asyncio
import uuid

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.profile import PromptProfileCreate
from app.models.service import ServiceCreate
from app.models.identity import IdentityCreate, IdentityUpdate


async def _setup_app():
    """内存数据库 + 免登录的 ASGI 应用。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_admin_accounts"]

    from app.admin.auth import require_admin
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: "admin"
    return app


async def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _run(coro) -> None:
    asyncio.run(coro)


async def _seed_account() -> str:
    """创建一个测试账号并返回其 ID。"""
    acc = await AccountRepository().create(AccountCreate(name="测试账号"))
    return acc.id


class TestAccountsCrud:
    """账号列表/创建/详情/编辑/删除。"""

    def test_list_create_detail_edit_delete(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    # 空列表
                    resp = await client.get("/admin/accounts")
                    assert resp.status_code == 200

                    # 创建：空名称报错留在列表页
                    resp = await client.post("/admin/accounts/create", data={"name": " "})
                    assert resp.status_code == 200
                    assert "账号名称不能为空" in resp.text

                    # 创建成功 → 303
                    resp = await client.post(
                        "/admin/accounts/create",
                        data={"name": "账号A", "remark": "备注"},
                    )
                    assert resp.status_code == 303

                    accounts = await AccountRepository().list_all()
                    acc_id = accounts[0].id

                    # 列表（含额度信息）与搜索
                    resp = await client.get("/admin/accounts")
                    assert resp.status_code == 200 and "账号A" in resp.text
                    resp = await client.get("/admin/accounts", params={"q": "账号A"})
                    assert resp.status_code == 200 and "账号A" in resp.text

                    # 详情页；不存在的账号重定向回列表
                    resp = await client.get(f"/admin/accounts/{acc_id}")
                    assert resp.status_code == 200
                    resp = await client.get("/admin/accounts/no-such")
                    assert resp.status_code == 303

                    # 编辑
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/edit",
                        data={"name": "账号A2", "remark": ""},
                    )
                    assert resp.status_code == 303
                    updated = await AccountRepository().find_by_id(acc_id)
                    assert updated is not None and updated.name == "账号A2"

                    # 额度更新
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/quota",
                        data={
                            "text_limit": "100", "text_used": "1",
                            "voice_limit": "10", "voice_used": "0",
                            "image_limit": "5", "image_used": "0",
                        },
                    )
                    assert resp.status_code == 303
                    quota = await QuotaRepository().find_by_account(acc_id)
                    assert quota is not None and quota.text_quota.limit == 100

                    # 级联删除：先灌入 7 个周边集合，后台删账号要与 C 端注销同口径
                    db = mongo.get_database()
                    await db.identities.insert_one({"id": "adm-u-1", "account_id": acc_id})
                    await db.services.insert_one({"id": "adm-svc", "account_id": acc_id})
                    await db.web_credentials.insert_one(
                        {"id": "adm-cred", "identity_id": "adm-u-1"}
                    )
                    await db.web_sessions.insert_one({"credential_id": "adm-cred"})
                    await db.orders.insert_one({"id": "adm-order", "account_id": acc_id})
                    await db.proactive_records.insert_one({"identity_id": "adm-u-1"})
                    await db.safety_events.insert_one({"account_id": acc_id})
                    # 快照按 context_key 归属（无共享组时即服务自身 id）
                    await db.injection_snapshots.insert_one({"service_id": "adm-svc"})

                    resp = await client.post(f"/admin/accounts/{acc_id}/delete")
                    assert resp.status_code == 303
                    assert await AccountRepository().find_by_id(acc_id) is None
                    for coll in (
                        "web_credentials", "web_sessions", "orders",
                        "proactive_records", "safety_events", "injection_snapshots",
                        "services", "identities",
                    ):
                        assert await db[coll].count_documents({}) == 0, coll
            finally:
                await _teardown_app(app)

        _run(flow())


class TestAccountIdentities:
    """账号下身份管理。"""

    def test_user_create_delete_toggle(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                acc_id = await _seed_account()
                identity_repo = IdentityRepository()
                async with _client(app) as client:
                    # 创建身份
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/create",
                        data={"name": "身份甲", "platform": "qq", "is_minor": "on"},
                    )
                    assert resp.status_code == 303
                    identities = await identity_repo.list_by_account(acc_id)
                    assert len(identities) == 1 and identities[0].is_minor is True
                    uid = identities[0].id

                    # 有服务引用时删除被拒并渲染错误
                    await ServiceRepository().create(ServiceCreate(
                        account_id=acc_id, identity_id=uid, bot_id="b-x",
                        profile_id="0" * 24,  # 合法 ObjectId 占位，避免错误页回查崩溃
                    ))
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/{uid}/delete"
                    )
                    assert resp.status_code == 200 and "请先删除服务" in resp.text

                    # 有服务引用时禁用同样被拒
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/{uid}/toggle-block",
                        data={"block_reason": "违规"},
                    )
                    assert resp.status_code == 200 and "请先删除服务" in resp.text

                    # 删除服务后可禁用/启用
                    svc = await ServiceRepository().find_by_identity_and_bot(uid, "b-x")
                    assert svc is not None
                    await ServiceRepository().delete(svc.id)
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/{uid}/toggle-block",
                        data={"block_reason": "违规"},
                    )
                    assert resp.status_code == 303
                    blocked = await identity_repo.find_by_id(uid)
                    assert blocked is not None and blocked.is_blocked is True
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/{uid}/toggle-block"
                    )
                    assert resp.status_code == 303

                    # 不存在的身份 toggle 直接重定向
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/no-such/toggle-block"
                    )
                    assert resp.status_code == 303

                    # 无引用后删除成功
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/identities/{uid}/delete"
                    )
                    assert resp.status_code == 303
                    assert await identity_repo.find_by_id(uid) is None
            finally:
                await _teardown_app(app)

        _run(flow())


class TestAccountBots:
    """账号下 Bot 管理。"""

    def test_bot_create_edit_delete_toggle(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                acc_id = await _seed_account()
                bot_repo = BotConfigRepository()
                async with _client(app) as client:
                    # 空名称直接重定向（不创建）
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/create", data={"name": " "}
                    )
                    assert resp.status_code == 303
                    assert await bot_repo.count() == 0

                    # 创建 QQ Bot
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/create",
                        data={
                            "name": "QQ机器人", "platform": "qq",
                            "app_id": "app-1", "sandbox_mode": "on",
                        },
                    )
                    assert resp.status_code == 303
                    bots = await bot_repo.list_by_account(acc_id)
                    assert len(bots) == 1
                    bid = bots[0].id

                    # 编辑
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/{bid}/edit",
                        data={"name": "改名Bot", "app_id": "app-2"},
                    )
                    assert resp.status_code == 303
                    edited = await bot_repo.find_by_id(bid)
                    assert edited is not None and edited.name == "改名Bot"

                    # 有服务引用时删除/禁用被拒
                    await ServiceRepository().create(ServiceCreate(
                        account_id=acc_id, identity_id="u-x", bot_id=bid,
                        profile_id="0" * 24,  # 合法 ObjectId 占位，避免错误页回查崩溃
                    ))
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/{bid}/delete"
                    )
                    assert resp.status_code == 200 and "请先删除服务" in resp.text
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/{bid}/toggle-block",
                        data={"block_reason": "违规"},
                    )
                    assert resp.status_code == 200 and "请先删除服务" in resp.text

                    # 删除服务后禁用/启用成功
                    svc = await ServiceRepository().find_by_identity_and_bot("u-x", bid)
                    assert svc is not None
                    await ServiceRepository().delete(svc.id)
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/{bid}/toggle-block",
                        data={"block_reason": "违规"},
                    )
                    assert resp.status_code == 303
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/{bid}/toggle-block"
                    )
                    assert resp.status_code == 303

                    # 不存在的 Bot toggle 直接重定向
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/no-such/toggle-block"
                    )
                    assert resp.status_code == 303

                    # 删除成功
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/bots/{bid}/delete"
                    )
                    assert resp.status_code == 303
                    assert await bot_repo.find_by_id(bid) is None
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_bot_edit_secret_masked_and_preserved(self) -> None:
        """详情页不再提供 App Secret 入口；后端直连轮换仍不覆盖脱敏串/空值。"""
        real_secret = "abcd1234wxyz"

        async def flow() -> None:
            app = await _setup_app()
            try:
                acc_id = await _seed_account()
                bot_repo = BotConfigRepository()
                bot = await bot_repo.create(BotConfigCreate(
                    account_id=acc_id, name="脱敏Bot", platform="qq",
                    app_id="app-mask", app_secret=real_secret,
                ))
                edit_url = f"/admin/accounts/{acc_id}/bots/{bot.id}/edit"
                async with _client(app) as client:
                    resp = await client.get(f"/admin/accounts/{acc_id}")
                    assert resp.status_code == 200
                    assert real_secret not in resp.text, "明文凭据仍被渲染进 HTML"
                    assert "abcd****wxyz" not in resp.text, "详情页仍回显脱敏凭据"
                    assert 'name="app_secret"' not in resp.text, "详情页仍提供手填入口"

                    # 提交脱敏串或留空：一律保持库内原值
                    for payload in ({"app_secret": "abcd****wxyz"}, {"app_secret": "  "}):
                        resp = await client.post(edit_url, data=payload)
                        assert resp.status_code == 303
                        kept = await bot_repo.find_by_id(bot.id)
                        assert kept is not None and kept.app_secret == real_secret

                    # 只有真值才替换（凭据轮换走的就是这条路）
                    resp = await client.post(
                        edit_url, data={"app_secret": "brandnewsecret0000"}
                    )
                    assert resp.status_code == 303
                    rotated = await bot_repo.find_by_id(bot.id)
                    assert rotated is not None and rotated.app_secret == "brandnewsecret0000"
            finally:
                await _teardown_app(app)

        _run(flow())


class TestAccountServices:
    """账号下服务管理（含共享组）。"""

    async def _seed_entities(self, acc_id: str) -> tuple[str, str, str]:
        """创建可用的身份、Bot、角色，返回 (user_id, bot_id, profile_slug)。"""
        identity = await IdentityRepository().create(IdentityCreate(account_id=acc_id, name="甲"))
        bot = await BotConfigRepository().create(
            BotConfigCreate(account_id=acc_id, name="机器人")
        )
        profile = await ProfileRepository().create(
            PromptProfileCreate(slug=f"role-{uuid.uuid4().hex[:6]}", name="角色")
        )
        return identity.id, bot.id, profile.slug

    def test_web_bot_allows_multiple_services(self) -> None:
        """网页 Bot 是载体通道，允许承载多个伙伴服务；QQ/微信仍保持 1:1。"""

        async def flow() -> None:
            app = await _setup_app()
            try:
                acc_id = await _seed_account()
                uid, _, slug = await self._seed_entities(acc_id)
                web_bot = await BotConfigRepository().create(
                    BotConfigCreate(account_id=acc_id, name="网页助手", platform="web")
                )
                async with _client(app) as client:
                    for _ in range(2):
                        resp = await client.post(
                            f"/admin/accounts/{acc_id}/services",
                            data={"identity_id": uid, "bot_id": web_bot.id, "profile_id": slug},
                        )
                        assert resp.status_code == 303
                    assert await ServiceRepository().count_by_bot(web_bot.id) == 2
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_service_create_validations(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                acc_id = await _seed_account()
                uid, bid, slug = await self._seed_entities(acc_id)
                async with _client(app) as client:
                    # 缺少字段直接重定向
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services", data={"identity_id": uid}
                    )
                    assert resp.status_code == 303

                    # 正常创建
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services",
                        data={"identity_id": uid, "bot_id": bid, "profile_id": slug},
                    )
                    assert resp.status_code == 303
                    assert await ServiceRepository().count_by_bot(bid) == 1

                    # 1 Bot = 1 Service 约束
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services",
                        data={"identity_id": uid, "bot_id": bid, "profile_id": slug},
                    )
                    assert resp.status_code == 200
                    assert "一个渠道只允许创建一个服务" in resp.text

                    # 已绑定真实平台账号的身份不能再建服务
                    uid2, bid2, slug2 = await self._seed_entities(acc_id)
                    await IdentityRepository().update(
                        uid2, IdentityUpdate(platform_id="real-openid")
                    )
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services",
                        data={"identity_id": uid2, "bot_id": bid2, "profile_id": slug2},
                    )
                    assert resp.status_code == 200
                    assert "已绑定真实平台账号" in resp.text

                    # 被禁用的身份
                    uid3, bid3, slug3 = await self._seed_entities(acc_id)
                    await IdentityRepository().set_blocked(uid3, True, "违规")
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services",
                        data={"identity_id": uid3, "bot_id": bid3, "profile_id": slug3},
                    )
                    assert resp.status_code == 200
                    assert "该身份已被禁用" in resp.text

                    # 被禁用的 Bot
                    uid4, bid4, slug4 = await self._seed_entities(acc_id)
                    await BotConfigRepository().set_blocked(bid4, True, "违规")
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services",
                        data={"identity_id": uid4, "bot_id": bid4, "profile_id": slug4},
                    )
                    assert resp.status_code == 200
                    assert "该渠道已被禁用" in resp.text

                    # 被禁用的角色
                    uid5, bid5, slug5 = await self._seed_entities(acc_id)
                    await ProfileRepository().delete(slug5)  # 软删除=禁用
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services",
                        data={"identity_id": uid5, "bot_id": bid5, "profile_id": slug5},
                    )
                    assert resp.status_code == 200
                    assert "该角色已被禁用" in resp.text
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_service_edit_share_group_and_delete(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                acc_id = await _seed_account()
                svc_repo = ServiceRepository()
                uid, bid, slug = await self._seed_entities(acc_id)
                svc1 = await svc_repo.create(ServiceCreate(
                    account_id=acc_id, identity_id=uid, bot_id=bid, profile_id=slug,
                ))
                # 另一个账号下、不同角色的服务（用于共享组校验失败分支）
                other = await svc_repo.create(ServiceCreate(
                    account_id="other-acc", identity_id="u-o", bot_id="b-o",
                    profile_id="p-o", share_group_id="grp-1",
                ))
                async with _client(app) as client:
                    # 共享组校验失败：组内已有不同账号/角色的成员
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services/{svc1.id}/edit",
                        data={
                            "name": "服务1", "is_active": "on",
                            "profile_id": slug, "share_group_id": "grp-1",
                        },
                    )
                    assert resp.status_code == 200
                    assert "共享组校验失败" in resp.text

                    # 正常编辑（禁用并记录原因）
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services/{svc1.id}/edit",
                        data={
                            "name": "服务1", "profile_id": slug,
                            "disable_reason": "暂停使用",
                        },
                    )
                    assert resp.status_code == 303
                    edited = await svc_repo.find_by_id(svc1.id)
                    assert edited is not None and edited.is_active is False
                    assert edited.disable_reason == "暂停使用"

                    # 加入合法共享组（同账号同角色成员）
                    partner = await svc_repo.create(ServiceCreate(
                        account_id=acc_id, identity_id="u-p", bot_id="b-p",
                        profile_id=slug, share_group_id="grp-2",
                    ))
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services/{svc1.id}/edit",
                        data={
                            "name": "服务1", "is_active": "on",
                            "profile_id": slug, "share_group_id": "grp-2",
                        },
                    )
                    assert resp.status_code == 303

                    # 删除非最后成员：组数据保留
                    from app.db.repositories.conversation_repo import (
                        ConversationRepository,
                    )
                    from app.db.repositories.message_repo import MessageRepository
                    conv_repo = ConversationRepository()
                    group_conv = await conv_repo.create(uid, slug, "grp-2")
                    await MessageRepository().create(group_conv.id, "user", "hi")

                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services/{svc1.id}/delete"
                    )
                    assert resp.status_code == 303
                    assert await conv_repo.find_by_id(group_conv.id) is not None

                    # 删除最后成员：组数据一并清理
                    resp = await client.post(
                        f"/admin/accounts/{acc_id}/services/{partner.id}/delete"
                    )
                    assert resp.status_code == 303
                    assert await conv_repo.list_by_service("grp-2") == []

                    # 删除后身份绑定被释放（platform_id 重置为 UUID）
                    released = await IdentityRepository().find_by_id(uid)
                    assert released is not None
                    uuid.UUID(released.platform_id)  # 不抛异常即为 UUID

                    # 清理另一个账号的服务
                    resp = await client.post(
                        f"/admin/accounts/other-acc/services/{other.id}/delete"
                    )
                    assert resp.status_code == 303
            finally:
                await _teardown_app(app)

        _run(flow())
