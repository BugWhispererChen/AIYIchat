"""写侧凭据脱敏工具：日志只允许出现掩码后的值或白名单字段。

为什么改在写侧，而不是加一个全局 logging.Filter 兜底：过滤器只能靠事后正则遮罩，
新出现的泄漏点会静默通过、给人"已经防护"的错觉，而且要为热路径每条记录做字符串扫描。
凭据打印点位是有限可枚举的，改净写侧 + 用 AST 静态护栏（见
`tools/test/test_no_credential_logging.py`）把"不许把响应整体 dump 进日志"钉成契约，
比在读侧打补丁更可信。

约定：新增任何"打印第三方接口响应"的代码，一律只打 `list(data.keys())` 或本模块的
`brief_json`，凭据值本身用 `mask_secret` / `mask_uri` 处理后再输出。
"""

from urllib.parse import urlsplit

# 掩码后的凭据占位：不保留任何长度的原文信息，避免缩短长度本身成为线索
_MASK = "***"


def mask_secret(value: str) -> str:
    """密钥/令牌类字符串的掩码：长度≥8 显示前4+****+后4，更短则等长度全 `*`。

    与后台展示层的口径一致（`app/admin/router.py` 复用本函数），差别只在这里允许
    出现在日志与诊断输出里。空值返回空串，便于调用方直接拼接。
    """
    if not value:
        return ""
    if len(value) >= 8:
        return f"{value[:4]}****{value[-4:]}"
    return "*" * len(value)


def mask_uri(uri: str) -> str:
    """连接串脱敏：含凭据时遮掉用户名/密码/主机并丢弃查询串，只保留协议与库名。

    主机一并遮是因为本仓库为公开仓库，日志会被截图流转；不含凭据的本地连接串
    （如 `mongodb://localhost:27017`）原样返回，排查时仍能看到实例地址。
    """
    if not uri:
        return ""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "*" * len(uri)
    netloc = parts.netloc
    if not netloc or "@" not in netloc:
        return uri
    userinfo = netloc.rsplit("@", 1)[0]
    # user:pass 与纯 user 两种写法分别给出对应形态的占位，便于看出配置是否漏了密码
    masked_user = f"{_MASK}:{_MASK}" if ":" in userinfo else _MASK
    return f"{parts.scheme}://{masked_user}@{_MASK}{parts.path}"


def brief_json(data: object, keys: tuple[str, ...] = ()) -> str:
    """把响应体压成"键名清单 + 白名单字段值"，永不输出全量内容。

    `keys` 只应填明确非敏感的字段（如 errcode/errmsg/ret），填凭据字段等于绕过本模块。
    """
    if not isinstance(data, dict):
        return f"type={type(data).__name__}"
    names = ",".join(sorted(str(k) for k in data))
    picked = ", ".join(f"{k}={data.get(k)!r}" for k in keys if k in data)
    brief = f"keys=[{names}]"
    return f"{brief} {picked}" if picked else brief
