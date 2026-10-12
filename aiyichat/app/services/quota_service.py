"""额度校验与扣减服务。"""

from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.user_bot_binding_repo import UserBotBindingRepository
from app.utils.token_estimate import estimate_tokens


class QuotaService:
    """负责校验用户与 Bot 的绑定关系并执行额度扣减。"""

    def __init__(
        self,
        quota_repo: QuotaRepository | None = None,
        binding_repo: UserBotBindingRepository | None = None,
    ):
        """初始化服务，支持注入 Repository 实例。"""
        self.quota_repo = quota_repo or QuotaRepository()
        self.binding_repo = binding_repo or UserBotBindingRepository()

    async def check_and_deduct(
        self,
        user_id: str,
        bot_id: str,
        content_type: str,
        text: str,
        amount: int | None = None,
    ) -> tuple[bool, str]:
        """
        检查用户是否绑定该 Bot，并扣除对应额度。

        Args:
            user_id: 用户 ID
            bot_id: Bot ID
            content_type: 消息类型 (text / voice / image)
            text: 消息文本内容（语音为 ASR 结果）
            amount: 显式指定扣减量；为 None 时按默认规则计算

        Returns:
            (is_allowed, message_or_empty)
        """
        # 1. 检查绑定关系
        binding = await self.binding_repo.find_by_user_and_bot(
            user_id, bot_id
        )
        if binding is None:
            return False, "您没有权限使用该机器人，请联系管理员。"

        # 2. 计算消耗量：图片按条计 1，文字/语音按估算 token 数计
        # （与引擎侧 Token 计费语义一致；调用后按 API 真实 usage 结算）
        if amount is None:
            amount = 1 if content_type == "image" else estimate_tokens(text)

        # 3. 扣除额度
        ok, _reason = await self.quota_repo.deduct(
            user_id, content_type, amount
        )
        if not ok:
            return False, "您的额度已用完，请联系管理员充值。"

        return True, ""
