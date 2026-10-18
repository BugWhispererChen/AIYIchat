"""内容安全检查器。

对身份输入做轻量规则校验，覆盖两类合规场景：
1. 心理危机识别（自杀/自伤倾向）→ 返回温暖的心理援助引导，并留痕告警
2. 违禁内容拦截（政治/暴力/色情等）→ 婉拒回复

纯规则实现，零额外延迟；危机识别优先级高于违禁拦截（生命安全优先）。
危机词库与 emotion_detector 的高强度词有交集，此处独立维护以便运营调整。
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class SafetyAction(str, Enum):
    """安全检查后的处置动作。"""

    PASS = "pass"            # 放行
    CRISIS = "crisis"        # 危机干预（优先返回援助引导）
    BLOCK = "block"          # 违禁拦截（婉拒）


@dataclass(frozen=True)
class SafetyVerdict:
    """安全检查结论。"""

    action: SafetyAction
    matched: str = ""        # 命中的关键词（用于留痕核查）
    reply: str = ""          # 需直接回复给身份的内容（危机/拦截时非空）


# 心理危机信号词（自杀/自伤倾向）——命中即触发干预
# 危机干预话术本身是共情引导，误报代价低，因此可包含隐性表述
_CRISIS_PATTERNS: list[str] = [
    # 直接表述
    "想死", "不想活", "活着没意思", "活不下去", "结束生命", "了结自己",
    "自杀", "自残", "割腕", "割脉", "跳楼", "轻生", "不想活了", "解脱算了",
    "没有活下去的意义", "生无可恋", "自我了断", "了却此生", "想自杀",
    # 方式探询（高危信号）
    "安眠药吃多少", "烧炭", "怎么死不痛苦", "怎样死得快",
    # 隐性/告别式表述
    "不想醒来", "睡过去就好了", "想消失", "从这个世界消失",
    "没有我大家会更好", "没人会想我", "撑不下去了", "熬不下去了",
    "不想撑了", "遗书", "最后一条消息", "跟你告别",
]

# 违禁内容关键词（内置基础集合，运营可在后台追加）——命中即婉拒
# 说明：采用"短语级"表述（如"买毒品"而非"毒品"），降低情感倾诉场景的误伤；
# 仅做最小可用的兜底防护，生产环境建议叠加外部内容安全服务。
_FORBIDDEN_PATTERNS: list[str] = [
    # 暴恐/武器
    "制造炸弹", "制作炸药", "炸弹配方", "爆炸物制作", "枪支买卖",
    "买枪", "卖枪", "自制枪", "自制手枪", "买炸药",
    "杀人方法", "怎么杀人", "投毒",
    # 色情引导/交易
    "涉黄", "裸聊", "约炮", "招嫖", "嫖娼", "卖淫", "色情服务",
    "福利姬", "黄色网站", "成人网站", "色情资源",
    # 毒品
    "毒品交易", "贩毒", "买毒品", "冰毒", "摇头丸", "海洛因",
    "买大麻", "吸毒渠道", "制毒",
    # 赌博
    "赌博网站", "网赌平台", "菠菜平台", "百家乐平台", "博彩代理",
    # 诈骗/违法交易
    "洗钱", "跑分", "代开发票", "办假证", "买假币", "银行卡四件套",
    "公民信息买卖", "查开房记录", "黑客接单", "盗号服务", "电信诈骗话术",
]

# 心理危机干预话术：共情 + 明确的求助资源
_CRISIS_REPLY = (
    "我能感觉到你现在真的很难受，这些情绪是真实且沉重的，谢谢你愿意说出来。💗\n"
    "但有些事我没办法替你扛住——如果你有伤害自己的念头，请一定联系专业的人：\n"
    "· 全国24小时心理援助热线：400-161-9995\n"
    "· 北京心理危机研究与干预中心：010-82951332\n"
    "· 紧急情况请拨打 110 / 120。\n"
    "我会一直在这里陪着你，你愿意再多和我说说现在发生了什么吗？"
)

# 违禁内容婉拒话术
_BLOCK_REPLY = "这个话题我没办法陪你聊呢，我们换点别的说说好不好？"


class SafetyChecker:
    """内容安全检查器（纯规则）。

    词表 = 内置基础词库 + 运营追加词（后台"内容安全"页维护，存 app_settings），
    配置变更后通过 reset_safety_checker() 热生效。
    """

    def __init__(
        self,
        crisis_extra: list[str] | None = None,
        forbidden_extra: list[str] | None = None,
    ) -> None:
        # 合并内置词库与追加词（去空、去重、保序）
        crisis = list(dict.fromkeys(
            _CRISIS_PATTERNS
            + [w.strip() for w in (crisis_extra or []) if w.strip()]
        ))
        forbidden = list(dict.fromkeys(
            _FORBIDDEN_PATTERNS
            + [w.strip() for w in (forbidden_extra or []) if w.strip()]
        ))
        # 预编译为整词/子串匹配的正则，提升批量匹配效率
        self._crisis_re = re.compile("|".join(map(re.escape, crisis)))
        self._forbidden_re = re.compile(
            "|".join(map(re.escape, forbidden))
        )

    def check_input(self, text: str) -> SafetyVerdict:
        """检查身份输入，返回处置结论。

        优先级：危机 > 违禁 > 放行（生命安全优先于内容合规）。

        Args:
            text: 身份消息文本

        Returns:
            SafetyVerdict（action=PASS 时 reply 为空）
        """
        if not text or not text.strip():
            return SafetyVerdict(SafetyAction.PASS)

        crisis_hit = self._crisis_re.search(text)
        if crisis_hit:
            logger.warning("安全检查：命中危机信号词 '%s'", crisis_hit.group())
            return SafetyVerdict(
                action=SafetyAction.CRISIS,
                matched=crisis_hit.group(),
                reply=_CRISIS_REPLY,
            )

        forbidden_hit = self._forbidden_re.search(text)
        if forbidden_hit:
            logger.info("安全检查：命中违禁词 '%s'", forbidden_hit.group())
            return SafetyVerdict(
                action=SafetyAction.BLOCK,
                matched=forbidden_hit.group(),
                reply=_BLOCK_REPLY,
            )

        return SafetyVerdict(SafetyAction.PASS)


# 全局单例
_safety_checker: SafetyChecker | None = None


def get_safety_checker() -> SafetyChecker:
    """获取全局 SafetyChecker 单例（惰性创建，合并运营追加词）。"""
    global _safety_checker
    if _safety_checker is None:
        from app.config import settings

        _safety_checker = SafetyChecker(
            crisis_extra=getattr(settings, "safety_crisis_extra", []),
            forbidden_extra=getattr(settings, "safety_forbidden_extra", []),
        )
    return _safety_checker


def reset_safety_checker() -> None:
    """重置单例（追加词变更或配置热重载后调用，下次使用时重建）。"""
    global _safety_checker
    _safety_checker = None


def builtin_word_counts() -> tuple[int, int]:
    """返回内置词库规模（危机词数, 违禁词数），供后台页面展示。"""
    return len(_CRISIS_PATTERNS), len(_FORBIDDEN_PATTERNS)
