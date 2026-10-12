import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.adapters.qq_adapter import qq_adapter
from app.config import settings
from app.conversation.engine import ConversationEngine
from app.utils.tasks import spawn

logger = logging.getLogger(__name__)

qq_webhook_router = APIRouter()

# 对话引擎实例
engine = ConversationEngine()


@qq_webhook_router.post("/qq/webhook")
async def qq_webhook(request: Request):
    """
    QQ Bot Webhook接收端点。

    处理流程：
    1. 解析验证请求（QQ平台URL验证）
    2. 解析消息事件
    3. 处理指令或对话
    4. 返回/发送回复
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # QQ webhook验证请求
    op = body.get("op")
    if op == 13:
        # URL验证回调
        d = body.get("d", {})
        return JSONResponse({
            "plain_token": d.get("plain_token", ""),
            "signature": _compute_verification_signature(d),
        })

    # 解析消息事件
    msg = await qq_adapter.parse_incoming(body)
    if msg is None:
        return JSONResponse({"status": "ignored"})

    content = msg.content.strip()
    if not content:
        return JSONResponse({"status": "empty"})

    # 指令处理
    if content.startswith("/") or content.startswith("／"):
        response_text = await _handle_command(content, msg)
    else:
        # 普通对话 - 异步处理以避免5秒超时（spawn 保存任务引用，防止被 GC 提前取消）
        spawn(_process_and_reply(msg))
        # 立即返回200，避免QQ平台超时
        return JSONResponse({"status": "processing"})

    # 指令的响应直接返回
    if response_text:
        await qq_adapter.send_message(
            user_id=msg.user_id,
            content=response_text,
            msg_id=msg.message_id,
            is_group=msg.is_group,
            group_id=msg.group_id,
        )

    return JSONResponse({"status": "ok"})


async def _process_and_reply(msg):
    """异步处理对话并回复。"""
    try:
        # 使用 bot_id 隔离的引擎实例
        bot_engine = ConversationEngine(bot_id=msg.bot_id)
        result = await bot_engine.handle_message(
            platform=msg.platform,
            platform_id=msg.user_id,
            text=msg.content,
            nickname=msg.nickname,
            is_group=msg.is_group,
            group_id=msg.group_id,
        )
        # 拟人化多消息回复：优先使用引擎拆分后的消息列表，逐条发送
        if (
            settings.multi_message_enabled
            and result.texts
        ):
            texts = [
                t for t in result.texts if t.strip()
            ][: settings.multi_message_max_count]
        else:
            texts = [result.text]
        for i, text in enumerate(texts):
            if i > 0:
                # 模拟打字延迟：按上一条消息长度计算，封顶 multi_message_max_delay
                delay = min(
                    len(texts[i - 1]) * settings.multi_message_delay_per_char,
                    settings.multi_message_max_delay,
                )
                await asyncio.sleep(delay)
            await qq_adapter.send_message(
                user_id=msg.user_id,
                content=text,
                msg_id=msg.message_id,
                is_group=msg.is_group,
                group_id=msg.group_id,
            )
    except Exception as e:
        logger.error("Process and reply failed: %s", e)
        await qq_adapter.send_message(
            user_id=msg.user_id,
            content="网络有点开小差了，你可以再发一次吗？",
            msg_id=msg.message_id,
            is_group=msg.is_group,
            group_id=msg.group_id,
        )


async def _handle_command(content: str, msg) -> str:
    """处理用户文本指令。"""
    # 统一全角/半角斜杠
    cmd = content.replace("／", "/").strip()
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    # 使用 bot_id 隔离的引擎实例
    bot_engine = ConversationEngine(bot_id=msg.bot_id)

    if command in ("/list", "/角色列表", "/ls"):
        profiles = await bot_engine.list_available_profiles()
        if not profiles:
            return "暂无可用角色。"
        lines = ["可用角色列表：\n"]
        for i, p in enumerate(profiles, 1):
            lines.append(f"{i}. {p.name} ({p.source})")
        lines.append("\n发送 /select 角色名 来选择角色")
        return "\n".join(lines)

    elif command in ("/select", "/切换", "/choose"):
        if not arg:
            return "请指定角色名。例如：/select 林黛玉"

        # 先尝试按名称匹配
        profiles = await bot_engine.list_available_profiles()
        target = None

        # 按数字序号选择
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(profiles):
                target = profiles[idx]

        # 按名称模糊匹配
        if target is None:
            for p in profiles:
                if arg in p.name or arg == p.slug:
                    target = p
                    break

        if target is None:
            return f"未找到角色「{arg}」。发送 /list 查看可用角色。"

        result = await bot_engine.switch_profile(
            msg.platform, msg.user_id, msg.bot_id, target.slug
        )
        if result:
            greeting = result.dialogue_strategy.greeting or f"你好，我是{result.name}。"
            return f"已切换到角色「{result.name}」\n来自：{result.source}\n\n{greeting}"
        return "切换失败，请稍后重试。"

    elif command in ("/help", "/帮助"):
        return (
            "爱易聊 使用帮助\n"
            "━━━━━━━━━━\n"
            "/list - 查看可用角色列表\n"
            "/select 角色名 - 切换对话角色\n"
            "/help - 显示此帮助信息\n"
            "━━━━━━━━━━\n"
            "直接发送文字即可与当前角色对话"
        )

    else:
        return f"未知指令：{command}\n发送 /help 查看帮助"


def _compute_verification_signature(d: dict) -> str:
    """计算QQ webhook URL验证的签名。"""
    import hashlib
    import hmac

    from app.config import settings

    plain_token = d.get("plain_token", "")
    event_ts = d.get("event_ts", "")
    msg = f"{event_ts}{plain_token}"

    signature = hmac.new(
        settings.qq_app_secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signature
