"""Jinja 模板过滤器与全局函数的唯一定义处。

后台存在三个 Jinja2Templates 实例（app/admin/router.py、app/admin/routes/common.py、
app/web/router.py），而 partials/_ui.html 这类共享片段会被所有实例渲染。只要某个实例
少登记一个过滤器，该实例在**编译期**就会抛 "No filter named ..."，整页 500。
因此过滤器集中在此定义，各实例统一调用 configure_templates()，杜绝逐处手工登记造成的漂移。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jinja2 import Environment

from app.utils.assets import asset

_BEIJING_TZ = timezone(timedelta(hours=8))

# 额度达到该量级即视为"无限"，模板与配额展示共用同一判定
UNLIMITED_QUOTA = 1_000_000_000


def to_beijing(dt: Any) -> Any:
    """UTC datetime → 北京时间；None 原样返回。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_BEIJING_TZ)


def format_quota(value: Any) -> str:
    """格式化额度数字：>=10亿显示为'无限'，>=1万按万显示，其余按千分位。"""
    if not isinstance(value, (int, float)):
        return str(value)
    if value >= UNLIMITED_QUOTA:
        return "无限"
    if value >= 10_000:
        return f"{value / 10000:.1f}万"
    return f"{value:,}"


def relative_time(dt: Any) -> str:
    """相对时间显示：刚刚 / x分钟前 / x小时前 / x天前；超过7天显示 北京时间日期。"""
    if dt is None:
        return "-"
    now = datetime.now(timezone.utc)
    # 兼容 naive datetime（按 UTC 处理）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((now - dt).total_seconds())
    if seconds < 0:
        # 时钟漂移或未来时间，统一视为刚刚
        return "刚刚"
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    if seconds < 604800:
        return f"{seconds // 86400}天前"
    # 超过7天显示北京时间日期
    return dt.astimezone(_BEIJING_TZ).strftime('%m-%d %H:%M')


def is_unlimited(limit: Any) -> bool:
    """额度上限是否属于"无限"档：无限额度不应绘制进度条。"""
    return bool(limit) and limit >= UNLIMITED_QUOTA


def configure_templates(env: Environment) -> None:
    """把共享过滤器与全局函数登记到给定 Jinja 环境。"""
    env.filters["to_beijing"] = to_beijing
    env.filters["format_quota"] = format_quota
    env.filters["relative_time"] = relative_time
    env.globals["asset"] = asset
    # 供模板判断"无限额度不画进度条"，避免把 10 亿这个魔数抄进每个页面
    env.globals["quota_is_unlimited"] = is_unlimited
