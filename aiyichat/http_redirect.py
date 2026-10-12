"""HTTP→HTTPS 重定向服务。

在容器内与主服务（uvicorn HTTPS）并存运行：
- 监听 HTTP_PORT 环境变量指定的端口（默认 80）
- 将所有请求 301 永久重定向到 HTTPS
- 跳转目标端口取 PUBLIC_HTTPS_PORT 环境变量（默认 443，为 443 时省略端口号）

仅依赖标准库，作为独立进程放在项目根目录（而非 app/ 下），由容器 CMD 单独启动。
"""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RedirectHandler(BaseHTTPRequestHandler):
    """将任意 HTTP 请求 301 重定向到对应的 HTTPS 地址。"""

    def _redirect(self) -> None:
        """构造 Location 并返回 301（保留原路径与查询串）。"""
        # Host 头可能携带端口，仅取主机名部分
        host = (self.headers.get("Host") or "localhost").split(":")[0]
        https_port = int(os.environ.get("PUBLIC_HTTPS_PORT", "443"))
        port_suffix = "" if https_port == 443 else f":{https_port}"
        location = f"https://{host}{port_suffix}{self.path}"
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # 常见方法统一跳转（跳转场景无需区分方法与请求体）
    def do_GET(self) -> None:
        self._redirect()

    def do_HEAD(self) -> None:
        self._redirect()

    def do_POST(self) -> None:
        self._redirect()

    def do_PUT(self) -> None:
        self._redirect()

    def do_DELETE(self) -> None:
        self._redirect()

    def do_OPTIONS(self) -> None:
        self._redirect()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """静默逐请求日志，避免刷屏（与标准库签名保持一致）。"""


def main() -> None:
    """启动重定向服务（前台阻塞运行）。"""
    port = int(os.environ.get("HTTP_PORT", "80"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RedirectHandler)
    print(f"[http_redirect] HTTP→HTTPS 重定向服务已启动，监听端口 {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
