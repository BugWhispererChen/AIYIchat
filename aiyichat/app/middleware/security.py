"""全局安全响应头。

后台是"同源会话 Cookie + 表单"的应用，缺这些头时的实际风险：
- 没有 frame-ancestors/X-Frame-Options：页面可被外站 iframe 套住做点击劫持
  （管理员点一下"删除账号"就是真删）；
- 没有 `X-Content-Type-Options`：浏览器会按内容嗅探执行上传的"图片"；
- 没有 `Referrer-Policy`：从 `/admin/accounts/acc-1` 跳外链时，带内部账号 ID 的完整路径
  会被带进第三方站点的 Referer（本仓库为公开仓库，这类标识不该外泄）。

只加无副作用的头。完整 CSP 上不了：模板里大量内联脚本与 `style=` 是既有契约，
贸然声明 `script-src` 会把后台与 C 端页面整体打挂，需要单独立项灰度——
这里只声明 CSP 里与劫持直接相关的三个指令。
"""

from typing import Any, Callable, Awaitable

_SECURITY_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
    # 服务只在 HTTPS 上跑（compose 里 HTTP 端口仅做跳转），一年内不回落明文
    "Strict-Transport-Security": "max-age=31536000",
}


class SecurityHeadersMiddleware:
    """纯 ASGI 中间件：给每个 HTTP 响应补上安全头。

    已存在的同名头不覆盖，方便个别路由自行放宽（例如将来某个内嵌预览页要放 frame）。
    """

    def __init__(self, app: Callable[[Any, Any, Any], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                existing = {key.decode("latin-1").lower() for key, _ in raw}
                for key, value in _SECURITY_HEADERS.items():
                    if key.lower() not in existing:
                        raw.append((key.encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_headers)
