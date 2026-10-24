"""外部 ID 字符串 → ObjectId 的安全转换。"""

from bson import ObjectId
from bson.errors import InvalidId


def to_object_id(value: str) -> ObjectId | None:
    """把 URL/表单里传来的 ID 转成 ObjectId，转不了返回 None。

    直接 `ObjectId(x)` 会让 `GET /api/conversations/missing/messages` 这类输入错误的请求
    抛 InvalidId 变成 500：本该是 404 的"记录不存在"被报成服务端故障，
    还把异常原文回显给匿名调用方。调用方拿到 None 后按"查无此记录"处理即可。
    """
    if not value:
        return None
    try:
        return ObjectId(value)
    except InvalidId:
        return None


def to_object_id_list(values: list[str]) -> list[ObjectId]:
    """批量转换并丢弃非法项（用于 `$in` 查询，一个坏 ID 不该让整批失败）。"""
    converted = [to_object_id(v) for v in values]
    return [oid for oid in converted if oid is not None]

def id_variants(value: str) -> list[object]:
    """同一个 ID 在库里的两种形态：字符串与 ObjectId。

    历史数据两种都写过（测试服实测 messages 603 条字符串 + 89 条 ObjectId）。
    只匹配字符串会让 ObjectId 形态的行既读不到也删不掉，级联删除因此留下孤儿。
    """
    oid = to_object_id(value)
    return [value, oid] if oid is not None else [value]


def id_variants_query(field: str, value: str) -> dict:
    """按字段的两种形态匹配的查询条件（等价 `{field: value}`，但兼容 ObjectId 存量）。"""
    return {field: {"$in": id_variants(value)}}


def id_variants_list(values: list[str]) -> list[object]:
    """批量展开成 `$in` 可用的形态列表（`$in` 自带去重语义，这里不必额外去重）。"""
    out: list[object] = []
    for value in values:
        out.extend(id_variants(value))
    return out

