"""异步 HTTP 客户端伪造工具（aiohttp / httpx），供 adapter 测试使用。

- FakeAiohttpSession：模拟 aiohttp.ClientSession 的 get/post 异步上下文管理器
- FakeHttpxResponse / patch_httpx：模拟 httpx.AsyncClient 的 post/get
"""

from typing import Any


class FakeAiohttpResponse:
    """伪造 aiohttp 响应，支持 text()/read()/status/headers。"""

    def __init__(
        self,
        *,
        text: str = "",
        read: bytes = b"",
        status: int = 200,
        headers: dict | None = None,
    ) -> None:
        self._text = text
        self._read = read
        self.status = status
        self.headers = headers or {}

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._read

    async def __aenter__(self) -> "FakeAiohttpResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeAiohttpSession:
    """伪造 aiohttp.ClientSession。

    responses 为按调用顺序返回的响应队列；单个响应会被重复使用。
    也可传入 Exception 实例以模拟请求抛错。
    """

    def __init__(self, responses: list | None = None) -> None:
        self._responses = list(responses or [])
        self.closed = False
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if self._responses:
            resp = (
                self._responses.pop(0)
                if len(self._responses) > 1
                else self._responses[0]
            )
        else:
            resp = FakeAiohttpResponse(text="{}")
        if isinstance(resp, Exception):
            raise resp
        return resp

    def get(self, url: str, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._next("POST", url, **kwargs)

    async def close(self) -> None:
        self.closed = True
