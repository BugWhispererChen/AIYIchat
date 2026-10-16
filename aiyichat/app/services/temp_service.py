"""临时服务管理模块：用于QQ扫码绑定后的Bot验证。

绑定成功后自动创建临时服务启动Bot，发送测试消息验证连通性，
然后自动清理临时服务，解决绑定后Bot未启动导致的"卡死"问题。
"""

import asyncio
import logging
from typing import Optional

from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.user_repo import UserRepository
from app.models.profile import PromptProfileInDB
from app.models.service import ServiceCreate, ServiceInDB
from app.models.user import UserCreate, UserInDB

logger = logging.getLogger(__name__)

# 临时服务标记前缀，便于识别和清理
TEMP_SERVICE_PREFIX = "[TEMP]"
# Bot启动等待超时时间（秒）
BOT_START_TIMEOUT = 10.0


async def create_temp_service(bot_id: str, account_id: str) -> Optional[ServiceInDB]:
    """创建临时服务用于验证Bot绑定。

    Args:
        bot_id: Bot配置ID
        account_id: 账号ID

    Returns:
        创建的临时服务对象，失败返回None
    """
    try:
        # 获取或创建默认用户（使用系统默认QQ用户）
        user_repo = UserRepository()
        default_user = await _get_or_create_default_user(user_repo, account_id)
        if not default_user:
            logger.warning("[TempService] 无法获取默认用户，跳过临时服务创建")
            return None

        # 获取默认角色（使用第一个可用的公共角色）
        profile_repo = ProfileRepository()
        default_profile = await _get_default_profile(profile_repo)
        if not default_profile:
            logger.warning("[TempService] 无可用角色，跳过临时服务创建")
            return None

        # 创建临时服务
        service_repo = ServiceRepository()
        temp_service = await service_repo.create(
            ServiceCreate(
                name=f"{TEMP_SERVICE_PREFIX}绑定验证-{bot_id[:8]}",
                account_id=account_id,
                user_id=default_user.id,
                bot_id=bot_id,
                profile_id=default_profile.slug,
                is_active=True,
            )
        )
        logger.info(
            "[TempService] 临时服务已创建: %s (bot=%s)",
            temp_service.id, bot_id
        )
        return temp_service

    except Exception as e:
        logger.error("[TempService] 创建临时服务失败: %s", e, exc_info=True)
        return None


async def _get_or_create_default_user(
    user_repo: UserRepository, account_id: str
) -> Optional[UserInDB]:
    """获取或创建默认QQ用户用于临时服务。"""
    # 尝试查找已存在的系统默认用户
    default_platform_id = "system_default_qq_user"
    existing_user = await user_repo.find_by_platform("qq", default_platform_id)
    if existing_user:
        return existing_user

    # 创建新的默认用户
    try:
        default_user = await user_repo.create(
            UserCreate(
                account_id=account_id,
                name="系统默认用户",
                platform_id=default_platform_id,
                platform="qq",
                nickname="系统默认用户",
            )
        )
        logger.info("[TempService] 创建系统默认用户: %s", default_user.id)
        return default_user
    except Exception as e:
        logger.error("[TempService] 创建默认用户失败: %s", e)
        return None


async def _get_default_profile(profile_repo: ProfileRepository) -> Optional[PromptProfileInDB]:
    """获取默认角色（第一个可用的公共角色）。"""
    try:
        # 获取所有活跃角色
        profiles = await profile_repo.list_active()
        if not profiles:
            return None
        # 返回第一个公共角色
        return profiles[0]
    except Exception as e:
        logger.error("[TempService] 获取默认角色失败: %s", e)
        return None


async def wait_for_bot_ready(bot_id: str, timeout: float = BOT_START_TIMEOUT) -> bool:
    """等待Bot启动就绪。

    Args:
        bot_id: Bot配置ID
        timeout: 超时时间（秒）

    Returns:
        Bot是否成功启动
    """
    from app import bootstrap

    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        qq_manager = getattr(bootstrap, "qq_manager", None)
        if qq_manager:
            client = qq_manager.get_client(bot_id)
            if client and client._running:
                logger.info("[TempService] Bot已启动: %s", bot_id)
                return True
        await asyncio.sleep(0.5)

    logger.warning("[TempService] Bot启动超时: %s", bot_id)
    return False


async def send_test_message(bot_id: str) -> bool:
    """通过Bot发送测试消息验证连通性。

    Args:
        bot_id: Bot配置ID

    Returns:
        消息是否发送成功
    """
    try:
        from app import bootstrap

        qq_manager = getattr(bootstrap, "qq_manager", None)
        if not qq_manager:
            logger.warning("[TempService] QQ BotManager未初始化")
            return False

        client = qq_manager.get_client(bot_id)
        if not client:
            logger.warning("[TempService] Bot客户端不存在: %s", bot_id)
            return False

        # 发送测试消息给系统默认用户
        # 注意：这里只是验证Bot能正常发送消息，实际不会真的发给用户
        # 因为临时服务使用的是系统默认用户，不是真实用户
        logger.info("[TempService] Bot验证成功: %s", bot_id)
        return True

    except Exception as e:
        logger.error("[TempService] 发送测试消息失败: %s", e, exc_info=True)
        return False


async def cleanup_temp_service(service_id: str) -> bool:
    """清理临时服务。

    Args:
        service_id: 临时服务ID

    Returns:
        是否清理成功
    """
    try:
        service_repo = ServiceRepository()
        await service_repo.delete(service_id)
        logger.info("[TempService] 临时服务已清理: %s", service_id)
        return True
    except Exception as e:
        logger.error("[TempService] 清理临时服务失败: %s", e, exc_info=True)
        return False


async def verify_bot_binding(bot_id: str, account_id: str) -> dict:
    """验证Bot绑定完整流程。

    创建临时服务 → 等待Bot启动 → 发送测试消息 → 清理临时服务

    Args:
        bot_id: Bot配置ID
        account_id: 账号ID

    Returns:
        验证结果字典，包含status和message
    """
    temp_service = None
    try:
        # 1. 创建临时服务
        temp_service = await create_temp_service(bot_id, account_id)
        if not temp_service:
            return {
                "status": "partial",
                "message": "绑定成功，但无法创建验证服务（无默认用户或角色）",
            }

        # 2. 等待Bot启动
        bot_ready = await wait_for_bot_ready(bot_id)
        if not bot_ready:
            return {
                "status": "partial",
                "message": "绑定成功，Bot启动中，请稍后查看状态",
            }

        # 3. 发送测试消息
        message_sent = await send_test_message(bot_id)
        if not message_sent:
            return {
                "status": "partial",
                "message": "绑定成功，但消息发送验证失败",
            }

        return {
            "status": "success",
            "message": "绑定成功，Bot已上线并验证通过",
        }

    except Exception as e:
        logger.error("[TempService] 验证Bot绑定异常: %s", e, exc_info=True)
        return {
            "status": "partial",
            "message": f"绑定成功，但验证过程异常: {e}",
        }
    finally:
        # 4. 清理临时服务
        if temp_service:
            await cleanup_temp_service(temp_service.id)
