"""`app/utils/redaction.py` 的单元测试：掩码口径与"绝不输出原文"的边界。

这些函数是写侧凭据脱敏的唯一实现（后台展示层 `_mask_api_key` 也复用它），
因此掩码规则本身必须由测试钉死：改动口径会同时影响日志与后台页面。
"""

from app.utils.redaction import brief_json, mask_secret, mask_uri


class TestMaskSecret:
    """密钥/令牌掩码：长值留首尾，短值等长度全遮。"""

    def test_empty_returns_empty(self) -> None:
        assert mask_secret("") == ""

    def test_short_value_fully_masked(self) -> None:
        # 短于 8 位不保留首尾，避免"前4+后4"重叠后把原文整体暴露
        assert mask_secret("abc123") == "*" * 6

    def test_long_value_keeps_head_and_tail(self) -> None:
        assert mask_secret("key-1234567890abcdef") == "key-****cdef"

    def test_exactly_eight_chars(self) -> None:
        assert mask_secret("12345678") == "1234****5678"
        # 首尾各 4 位在长度 8 时恰好覆盖全部原文，这是刻意允许的边界（≥8 才走首尾式）

    def test_non_ascii_short_value_is_all_masked(self) -> None:
        # 长度不足 8 一律全遮，中英文同一口径
        assert mask_secret("密钥值很长") == "*" * 5


class TestMaskUri:
    """连接串掩码：有凭据才遮主机，无凭据原样保留便于排查实例。"""

    def test_empty(self) -> None:
        assert mask_uri("") == ""

    def test_user_pass_uri_masks_credentials_and_host(self) -> None:
        out = mask_uri("mongodb://admin:S3cret@10.0.0.7:27017/aiyichat")
        assert "S3cret" not in out
        assert "admin" not in out
        assert "10.0.0.7" not in out
        # 协议与库名不属凭据，保留下来才看得出连的是哪个库
        assert out.startswith("mongodb://***:***@***")
        assert out.endswith("/aiyichat")

    def test_user_only_uri(self) -> None:
        out = mask_uri("mongodb://admin@db.internal:27017/app")
        assert "admin" not in out
        assert "db.internal" not in out
        assert "@***" in out

    def test_query_string_is_dropped(self) -> None:
        # 查询串里可能带 tlsCertificateKeyFilePassword 之类的参数
        out = mask_uri(
            "mongodb://u:p@h:27017/app?authSource=admin&tlsCertificateKeyFilePassword=x"
        )
        assert "tlsCertificateKeyFilePassword" not in out
        assert "?" not in out

    def test_local_uri_without_credentials_is_preserved(self) -> None:
        uri = "mongodb://localhost:27017"
        assert mask_uri(uri) == uri

    def test_mongodb_srv_with_credentials(self) -> None:
        out = mask_uri("mongodb+srv://u:p@cluster0.example.mongodb.net/aiyichat")
        assert "cluster0" not in out
        assert out.startswith("mongodb+srv://***:***@***")

    def test_string_without_scheme_is_left_alone(self) -> None:
        # 解析不出 netloc 就说明不含凭据，原样返回比强行打码更可用
        assert mask_uri("not-a-uri-at-all") == "not-a-uri-at-all"
        assert mask_uri("localhost:27017") == "localhost:27017"


class TestBriefJson:
    """响应体摘要：只给键名与白名单字段，永不 dump 全量。"""

    def test_dict_lists_keys_only(self) -> None:
        out = brief_json({"access_token": "SECRET", "expires_in": 7200})
        assert "SECRET" not in out
        assert "access_token" in out  # 键名可打，值不可打
        assert "expires_in" in out

    def test_whitelist_fields_are_shown(self) -> None:
        out = brief_json({"errcode": 40123, "errmsg": "invalid appid", "x": "y"},
                         keys=("errcode", "errmsg"))
        assert "40123" in out
        assert "invalid appid" in out
        assert "\"y\"" not in out

    def test_non_dict_shows_type_only(self) -> None:
        assert brief_json(["a", "b"]) == "type=list"
        assert brief_json(None) == "type=NoneType"
