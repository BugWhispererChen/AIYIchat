"""统一服务装配核心的归属、绑定与唯一性测试。"""

import pytest

from app.db import mongo
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.models.account import AccountCreate
from app.models.bot_config import BotConfigCreate
from app.models.identity import IdentityCreate
from app.models.profile import PromptProfileCreate
from app.db.repositories.account_repo import AccountRepository
from app.services.service_provisioning import (
    ServiceProvisionError,
    create_service,
    has_external_service,
)
from tools.test.db_test_utils import run_with_db


async def _seed(account_suffix: str = "a"):
    account = await AccountRepository().create(AccountCreate(name=f"账号-{account_suffix}"))
    identity = await IdentityRepository().create(
        IdentityCreate(account_id=account.id, name="普通身份")
    )
    profile = await ProfileRepository().create(
        PromptProfileCreate(slug=f"role-{account_suffix}", name="角色", visibility="public")
    )
    return account, identity, profile


def test_web_service_canonicalizes_profile_and_shares_identity() -> None:
    async def body() -> None:
        account, identity, profile = await _seed("web")
        bot = await BotConfigRepository().create(
            BotConfigCreate(account_id=account.id, name="网页", platform="web")
        )
        first = await create_service(
            account_id=account.id,
            bot_id=bot.id,
            identity_id=identity.id,
            profile_ref=profile.slug,
        )
        second = await create_service(
            account_id=account.id,
            bot_id=bot.id,
            identity_id=identity.id,
            profile_ref=profile.id,
        )
        assert first.profile.id == profile.id
        assert first.service.profile_id == profile.id
        assert first.service.single_service_bot_id is None
        assert first.service.name == "普通身份 · 角色 · 网页"
        assert second.service.identity_id == first.service.identity_id

    run_with_db(body)


def test_non_web_single_service_and_external_presence() -> None:
    async def body() -> None:
        account, identity, profile = await _seed("single")
        web_bot = await BotConfigRepository().create(
            BotConfigCreate(account_id=account.id, name="网页", platform="web")
        )
        qq_bot = await BotConfigRepository().create(
            BotConfigCreate(account_id=account.id, name="QQ", platform="qq")
        )
        await create_service(
            account_id=account.id,
            bot_id=web_bot.id,
            identity_id=identity.id,
            profile_ref=profile.id,
        )
        assert not await has_external_service(identity.id)
        created = await create_service(
            account_id=account.id,
            bot_id=qq_bot.id,
            identity_id=identity.id,
            profile_ref=profile.id,
        )
        assert created.service.single_service_bot_id == qq_bot.id
        assert await has_external_service(identity.id)
        with pytest.raises(ServiceProvisionError) as err:
            await create_service(
                account_id=account.id,
                bot_id=qq_bot.id,
                identity_id=identity.id,
                profile_ref=profile.id,
            )
        assert err.value.code == "bot_has_service"

        await ServiceRepository().delete(created.service.id)
        assert not await has_external_service(identity.id)

    run_with_db(body)


def test_cross_account_and_bound_identity_rejected() -> None:
    async def body() -> None:
        account, _, profile = await _seed("owner")
        other, _, _ = await _seed("other")
        foreign_identity = await IdentityRepository().create(
            IdentityCreate(account_id=other.id, name="他人身份")
        )
        bot = await BotConfigRepository().create(
            BotConfigCreate(account_id=account.id, name="QQ", platform="qq")
        )
        with pytest.raises(ServiceProvisionError) as foreign:
            await create_service(
                account_id=account.id,
                bot_id=bot.id,
                identity_id=foreign_identity.id,
                profile_ref=profile.id,
            )
        assert foreign.value.code == "identity_not_found"

        bound = await IdentityRepository().create(
            IdentityCreate(
                account_id=account.id,
                name="已绑定",
                platform="qq",
                platform_id="real-openid",
            )
        )
        with pytest.raises(ServiceProvisionError) as occupied:
            await create_service(
                account_id=account.id,
                bot_id=bot.id,
                identity_id=bound.id,
                profile_ref=profile.id,
            )
        assert occupied.value.code == "identity_unavailable"
        assert await ServiceRepository().count_by_bot(bot.id) == 0

    run_with_db(body)


def test_indexes_enforce_external_binding_and_single_service() -> None:
    async def body() -> None:
        db = mongo.get_database()
        identity_indexes = await db.identities.index_information()
        service_indexes = await db.services.index_information()
        assert identity_indexes["identity_external_binding_unique"]["unique"] is True
        assert service_indexes["service_single_bot_unique"]["unique"] is True
        assert service_indexes["service_single_bot_unique"]["partialFilterExpression"] == {
            "single_service_bot_id": {"$type": "string"}
        }

    run_with_db(body)
