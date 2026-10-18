"""核心仓库层单元测试（mongomock 内存库）。

覆盖：
- db/mongo：connect_db / get_database / close_db / 索引创建
- AccountRepository / UserRepository / ServiceRepository / BotConfigRepository
- QuotaRepository / QuotaTemplateRepository / OrderRepository
"""

import pytest

from app.db import mongo
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.order_repo import OrderRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.models.account import AccountCreate, AccountUpdate
from app.models.bot_config import BotConfigCreate, BotConfigUpdate
from app.models.order import OrderCreate
from app.models.quota import AccountQuotaUpdate, QuotaEntry
from app.models.quota_template import QuotaTemplateCreate, QuotaTemplateUpdate
from app.models.service import ServiceCreate, ServiceUpdate
from app.models.identity import IdentityCreate, IdentityUpdate
from tools.test.db_test_utils import run_with_db


class TestMongoModule:
    """db/mongo 连接管理测试集。"""

    def test_get_database_before_connect_raises(self) -> None:
        """未连接时获取数据库应抛出 RuntimeError。"""
        # 确保处于未连接状态
        mongo._client = None
        mongo._db = None
        with pytest.raises(RuntimeError):
            mongo.get_database()

    def test_connect_and_close(self) -> None:
        """连接 mock 库后可获取数据库，关闭后复位。"""

        async def body():
            db = mongo.get_database()
            assert db is not None

        run_with_db(body)
        assert mongo._db is None

    def test_index_conflict_downgraded_to_debug(self, caplog) -> None:
        """可容忍的索引规格冲突降为 debug，其余命令失败仍报 ERROR。"""
        from types import SimpleNamespace

        listener = mongo.SlowQueryListener()

        def _event(command_name: str, failure):
            return SimpleNamespace(
                database_name="aiyichat",
                command_name=command_name,
                failure=failure,
            )

        # 索引同名规格冲突（code 86）→ 不再误报 ERROR
        with caplog.at_level("DEBUG", logger="app.db.mongo"):
            listener.failed(_event("createIndexes", {
                "code": 86,
                "codeName": "IndexKeySpecsConflict",
                "errmsg": "An existing index has the same name as the requested index",
            }))
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
        assert "index conflict ignored" in caplog.text

        # 仅凭错误文本也能识别（无 code 字段时）
        caplog.clear()
        with caplog.at_level("DEBUG", logger="app.db.mongo"):
            listener.failed(_event("createIndexes", {
                "errmsg": "An existing index has the same name as the requested index",
            }))
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

        # 其他命令失败 / 其他索引错误 → 仍按 ERROR 记录
        caplog.clear()
        with caplog.at_level("DEBUG", logger="app.db.mongo"):
            listener.failed(_event("insert", {"code": 11000, "errmsg": "duplicate key"}))
            listener.failed(_event("createIndexes", {
                "code": 85, "errmsg": "Index already exists with different options",
            }))
        assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 2


class TestAccountRepository:
    """账号仓库测试集。"""

    def test_crud_and_search(self) -> None:
        async def body():
            repo = AccountRepository()
            acc = await repo.create(AccountCreate(name="测试账号", remark="备注A"))
            assert acc.name == "测试账号"

            found = await repo.find_by_id(acc.id)
            assert found is not None and found.id == acc.id
            assert await repo.find_by_id("no-such") is None

            # 空更新返回原值
            same = await repo.update(acc.id, AccountUpdate())
            assert same is not None and same.name == "测试账号"
            updated = await repo.update(acc.id, AccountUpdate(name="新名"))
            assert updated is not None and updated.name == "新名"

            assert await repo.count() == 1
            assert len(await repo.list_all()) == 1
            assert len(await repo.search("备注")) == 1
            assert len(await repo.search("不存在")) == 0

            assert await repo.delete(acc.id) is True
            assert await repo.delete(acc.id) is False

        run_with_db(body)

    def test_delete_cascade(self) -> None:
        """级联删除应清理账号下服务、对话、消息、用户、Bot、额度与私有角色。"""

        async def body():
            db = mongo.get_database()
            repo = AccountRepository()
            acc = await repo.create(AccountCreate(name="级联账号"))
            await db.services.insert_one({"id": "svc-1", "account_id": acc.id})
            await db.conversations.insert_one({"service_id": "svc-1"})
            conv = await db.conversations.find_one({"service_id": "svc-1"})
            await db.messages.insert_one(
                {"conversation_id": str(conv["_id"]), "content": "hi"}
            )
            await db.service_memories.insert_one({"service_id": "svc-1"})
            await db.identities.insert_one({"id": "u-1", "account_id": acc.id})
            await db.bot_configs.insert_one({"id": "b-1", "account_id": acc.id})
            await db.account_quotas.insert_one({"account_id": acc.id})
            await db.profiles.insert_one(
                {"slug": "p-1", "visibility": "private", "account_id": acc.id}
            )

            stats = await repo.delete_cascade(acc.id)
            assert stats["messages"] == 1
            assert stats["conversations"] == 1
            assert stats["service_memories"] == 1
            assert stats["services"] == 1
            assert stats["identities"] == 1
            assert stats["bots"] == 1
            assert stats["quotas"] == 1
            assert stats["private_profiles"] == 1
            assert stats["account"] == 1

        run_with_db(body)


class TestIdentityRepository:
    """用户仓库测试集。"""

    def test_crud_and_query(self) -> None:
        async def body():
            repo = IdentityRepository()
            identity = await repo.create(
                IdentityCreate(account_id="acc-1", platform="qq", platform_id="oid-1", nickname="小明")
            )
            assert await repo.find_by_id(identity.id) is not None
            assert await repo.find_by_id("no-such") is None
            assert await repo.find_by_platform("qq", "oid-1") is not None
            assert await repo.find_by_platform("qq", "none") is None

            # find_or_create：已存在时复用并刷新活跃时间
            again = await repo.find_or_create("qq", "oid-1", "acc-1")
            assert again.id == identity.id
            # 不存在时创建
            created = await repo.find_or_create("wechat", "oid-2", "acc-1", "小红")
            assert created.platform == "wechat"

            # 更新
            same = await repo.update(identity.id, IdentityUpdate())
            assert same is not None
            updated = await repo.update(identity.id, IdentityUpdate(nickname="新昵称"))
            assert updated is not None and updated.nickname == "新昵称"

            # 列表与统计
            assert await repo.count() == 2
            assert await repo.count_by_account("acc-1") == 2
            assert len(await repo.list_by_account("acc-1")) == 2
            assert len(await repo.list_all()) == 2
            assert len(await repo.search("小红")) == 1

            # 禁用/启用与活跃时间
            assert await repo.set_blocked(identity.id, True, "违规") is True
            blocked = await repo.find_by_id(identity.id)
            assert blocked is not None and blocked.is_blocked is True
            assert await repo.set_blocked(identity.id, False) is True
            assert await repo.update_last_active(identity.id) is True

            # 绑定平台 openid
            await repo.bind_platform_id(identity.id, "real-oid", "真实昵称")
            bound = await repo.find_by_id(identity.id)
            assert bound is not None and bound.platform_id == "real-oid"

            # 按 Bot 维度记录 openid 映射（多 Bot 场景下主动消息路由）
            assert await repo.bind_bot_openid(identity.id, "bot-a", "oid-a") is True
            # 幂等：值未变化时不产生写入
            assert await repo.bind_bot_openid(identity.id, "bot-a", "oid-a") is False
            # 同一用户在不同 Bot 下的 openid 各自独立保存
            assert await repo.bind_bot_openid(identity.id, "bot-b", "oid-b") is True
            mapped = await repo.find_by_id(identity.id)
            assert mapped is not None
            assert mapped.bot_openids == {"bot-a": "oid-a", "bot-b": "oid-b"}
            # 空参数直接跳过
            assert await repo.bind_bot_openid(identity.id, "", "oid-x") is False
            assert await repo.bind_bot_openid("no-such-user", "bot-a", "oid") is False

            # 删除
            assert await repo.delete(identity.id) is True
            assert await repo.delete_by_account("acc-1") == 1

        run_with_db(body)


class TestServiceRepository:
    """对话服务仓库测试集。"""

    def test_crud_and_query(self) -> None:
        async def body():
            repo = ServiceRepository()
            svc = await repo.create(ServiceCreate(
                account_id="acc-1", identity_id="u-1", bot_id="b-1", profile_id="p-1",
            ))
            assert svc.context_key == svc.id

            assert await repo.find_by_id(svc.id) is not None
            assert await repo.find_by_id("no-such") is None
            assert await repo.find_by_identity_and_bot("u-1", "b-1") is not None
            assert await repo.find_active_by_bot("b-1") is not None

            # 列表
            assert len(await repo.list_by_account("acc-1")) == 1
            assert len(await repo.list_by_identity("u-1")) == 1
            assert len(await repo.list_by_bot("b-1")) == 1
            assert len(await repo.list_all_active()) == 1
            assert await repo.list_active_bot_ids() == {"b-1"}

            # 引用统计
            assert await repo.count_by_identity("u-1") == 1
            assert await repo.count_by_bot("b-1") == 1
            assert await repo.count_by_profile("p-1") == 1

            # 空更新与实际更新
            same = await repo.update(svc.id, ServiceUpdate())
            assert same is not None
            updated = await repo.update(
                svc.id, ServiceUpdate(share_group_id="grp-1")
            )
            assert updated is not None and updated.context_key == "grp-1"
            assert len(await repo.list_by_share_group("grp-1")) == 1

            # 禁用后可通过 find_disabled 查询
            await repo.update(svc.id, ServiceUpdate(is_active=False, disable_reason="停用"))
            assert await repo.find_by_identity_and_bot("u-1", "b-1") is None
            disabled = await repo.find_disabled_by_identity_and_bot("u-1", "b-1")
            assert disabled is not None and disabled.disable_reason == "停用"

            assert await repo.delete(svc.id) is True
            assert await repo.delete_by_account("acc-1") == 0

        run_with_db(body)


class TestBotConfigRepository:
    """Bot 配置仓库测试集。"""

    def test_crud_and_wechat_fields(self) -> None:
        async def body():
            from datetime import datetime, timezone

            repo = BotConfigRepository()
            bot = await repo.create(BotConfigCreate(
                account_id="acc-1", name="QQ机器人", platform="qq", app_id="app-1",
            ))
            assert await repo.find_by_id(bot.id) is not None
            assert await repo.find_by_id("no-such") is None
            assert await repo.find_by_app_id("qq", "app-1") is not None
            assert await repo.find_by_app_id("qq", "none") is None

            assert len(await repo.list_all()) == 1
            assert len(await repo.list_by_account("acc-1")) == 1
            assert len(await repo.list_by_platform("qq")) == 1
            assert await repo.count() == 1

            # 空更新与实际更新
            same = await repo.update(bot.id, BotConfigUpdate())
            assert same is not None
            updated = await repo.update(bot.id, BotConfigUpdate(name="新名字"))
            assert updated is not None and updated.name == "新名字"

            # 微信专用字段更新
            expire = datetime.now(timezone.utc)
            assert await repo.update_wechat_token(bot.id, "ctx-token", expire) is True
            assert await repo.update_wechat_bot_token(bot.id, "bot-token") is True
            assert await repo.update_status(bot.id, "online") is True
            assert await repo.update_qrcode(bot.id, "http://qr") is True
            assert await repo.set_blocked(bot.id, True, "违规") is True
            assert await repo.set_blocked(bot.id, False) is True

            assert await repo.delete(bot.id) is True
            assert await repo.delete_by_account("acc-1") == 0

        run_with_db(body)


class TestQuotaRepository:
    """账号额度仓库测试集。"""

    def test_ensure_and_create_or_update(self) -> None:
        async def body():
            repo = QuotaRepository()
            assert await repo.find_by_account("acc-1") is None

            # ensure：不存在则创建无限额度
            quota = await repo.ensure_quota("acc-1")
            assert quota.text_quota.limit == 10**9
            # 再次 ensure 直接返回已有记录
            again = await repo.ensure_quota("acc-1")
            assert again.account_id == "acc-1"

            # create_or_update 更新指定额度项
            updated = await repo.create_or_update(
                "acc-1",
                AccountQuotaUpdate(text_quota=QuotaEntry(limit=10, used=0)),
            )
            assert updated.text_quota.limit == 10

            assert await repo.delete_by_account("acc-1") == 1

        run_with_db(body)

    def test_deduct_and_refund(self) -> None:
        async def body():
            from datetime import datetime, timedelta, timezone

            repo = QuotaRepository()
            await repo.create_or_update(
                "acc-1",
                AccountQuotaUpdate(
                    text_quota=QuotaEntry(limit=5, used=0),
                    voice_quota=QuotaEntry(limit=1, used=1),
                    image_quota=QuotaEntry(
                        limit=5,
                        used=0,
                        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                    ),
                ),
            )

            # 正常扣减
            ok, reason = await repo.deduct("acc-1", "text", 3)
            assert ok is True and reason == ""
            # 额度不足
            ok, reason = await repo.deduct("acc-1", "voice", 1)
            assert ok is False and reason == "额度不足"
            # 已过期
            ok, reason = await repo.deduct("acc-1", "image", 1)
            assert ok is False and reason == "额度已过期"
            # 记录不存在
            ok, reason = await repo.deduct("no-acc", "text", 1)
            assert ok is False and reason == "额度记录不存在"

            # 返还
            assert await repo.refund("acc-1", "text", 3) is True
            assert await repo.refund("acc-1", "text", 0) is False
            after = await repo.find_by_account("acc-1")
            assert after is not None and after.text_quota.used == 0

        run_with_db(body)


class TestQuotaTemplateRepository:
    """额度模板仓库测试集。"""

    def test_crud(self) -> None:
        async def body():
            repo = QuotaTemplateRepository()
            tpl = await repo.create(QuotaTemplateCreate(
                name="基础套餐", text_limit=100, voice_limit=10, image_limit=5,
            ))
            assert len(await repo.list_all()) == 1
            assert await repo.find_by_id(tpl.id) is not None

            # 空更新与实际更新
            same = await repo.update(tpl.id, QuotaTemplateUpdate())
            assert same is not None
            updated = await repo.update(
                tpl.id, QuotaTemplateUpdate(text_limit=200)
            )
            assert updated is not None and updated.text_limit == 200

            assert await repo.delete(tpl.id) is True

        run_with_db(body)


class TestOrderRepository:
    """订单台账仓库测试集。"""

    def test_create_list_count_sum(self) -> None:
        async def body():
            repo = OrderRepository()
            oid = await repo.create(OrderCreate(
                account_id="acc-1", account_name="账号A",
                text_limit=100, amount=9.9,
            ))
            assert oid
            await repo.create(OrderCreate(account_id="acc-2", amount=0.1))

            assert await repo.count() == 2
            assert await repo.count(account_id="acc-1") == 1
            assert len(await repo.list_recent()) == 2
            assert len(await repo.list_recent(account_id="acc-2")) == 1

            total = await repo.sum_amount(days=30)
            assert total == 10.0

        run_with_db(body)
