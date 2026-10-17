"""文本完整性校验：拦截因提交端编码错误而产生的替换字符。"""

REPLACEMENT_CHAR = "\ufffd"


def reject_replacement_char(value: str | None) -> str | None:
    """拒绝含 U+FFFD 替换字符的文本，原样返回合法值。

    提交端若以非 UTF-8（典型如 Windows 控制台 GBK 码页）发送表单，服务端解码会把
    坏字节静默替换成 U+FFFD 并永久落库，页面随即显示不可逆的乱码。此处显式报错，
    把问题挡在写入之前。
    """
    if value is not None and REPLACEMENT_CHAR in value:
        raise ValueError(
            f"文本含无法解码的字符（U+{ord(REPLACEMENT_CHAR):04X}），"
            "请确认提交端使用 UTF-8 编码"
        )
    return value
