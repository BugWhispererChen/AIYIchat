"""证书自举契约测试（不需要 Docker，也不起浏览器）。

守住 P0-1 修好的那条链：镜像 CMD 硬要求 `certs/selfsigned.{key,crt}`，而 `certs/`
不入 Git 也不入部署包 —— 一旦以下任一处漂移，照 README 直接 `docker compose up`
的部署者就会重新撞上那句不指名文件的 `FileNotFoundError`：

1. `tools/ensure_certs.py` 真能产出 uvicorn 可用的 key/cert 配对，且不覆盖已有证书；
2. `docker-compose.yml` 仍用一次性 `certgen` 服务跑它，并让 `app` 等它成功退出；
3. `Dockerfile`、Linux 部署包生成器与包校验脚本三处都带着这个脚本。
"""

import hashlib
import ipaddress
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tools.ensure_certs import VALID_DAYS, _build_san, _split_hosts, ensure_certs, main

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_pair(dir_path: Path) -> tuple[x509.Certificate, rsa.RSAPublicKey, rsa.RSAPublicKey]:
    """读取证书与私钥，返回 (证书, 证书公钥, 私钥公钥)。"""
    cert = x509.load_pem_x509_certificate((dir_path / "selfsigned.crt").read_bytes())
    key = serialization.load_pem_private_key(
        (dir_path / "selfsigned.key").read_bytes(), password=None
    )
    assert isinstance(cert, x509.Certificate)
    assert isinstance(key, rsa.RSAPrivateKey)
    return cert, cert.public_key(), key.public_key()


def _san_values(cert: x509.Certificate) -> list[str]:
    """取出证书的 SAN 条目字面值。"""
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return [str(item.value) for item in ext.value]


class TestGenerateCertificates:
    """生成器本身的产物必须是 uvicorn 能直接吃下去的一对 PEM。"""

    def test_first_run_creates_usable_pair(self, tmp_path: Path) -> None:
        target = tmp_path / "fresh"
        assert main(["--dir", str(target)]) == 0
        assert (target / "selfsigned.key").exists()
        assert (target / "selfsigned.crt").exists()

        cert, from_cert, from_key = _load_pair(target)
        assert from_cert.public_numbers() == from_key.public_numbers(), "证书与私钥不配对"
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert cn == "localhost"
        assert cert.not_valid_after_utc > cert.not_valid_before_utc
        span = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
        assert VALID_DAYS <= span <= VALID_DAYS + 2

    def test_san_covers_localhost(self, tmp_path: Path) -> None:
        target = tmp_path / "san"
        ensure_certs(target, [])
        san = _san_values(_load_pair(target)[0])
        assert "localhost" in san and "127.0.0.1" in san

    def test_extra_hosts_land_in_san(self, tmp_path: Path) -> None:
        target = tmp_path / "extra"
        assert main(["--dir", str(target), "--extra-hosts", "192.0.2.10,chat.local"]) == 0
        san = _san_values(_load_pair(target)[0])
        assert {"localhost", "127.0.0.1", "192.0.2.10", "chat.local"} <= set(san)

    def test_illegal_extra_host_is_dropped_not_fatal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 非法附加项只该被丢弃并告警：不能因为它把首启卡住
        target = tmp_path / "illegal"
        assert main(["--dir", str(target), "--extra-hosts", "bad host!!"]) == 0
        san = _san_values(_load_pair(target)[0])
        assert all("!" not in item for item in san)
        assert "非法 SAN" in capsys.readouterr().err


class TestNeverClobberExistingCerts:
    """部署者可能预先放了正式证书，任何路径都不该静默替换它。"""

    def test_second_run_keeps_bytes_identical(self, tmp_path: Path) -> None:
        target = tmp_path / "idem"
        ensure_certs(target, [])
        before = hashlib.sha256((target / "selfsigned.crt").read_bytes()).hexdigest()
        message = ensure_certs(target, ["192.0.2.10"])
        after = hashlib.sha256((target / "selfsigned.crt").read_bytes()).hexdigest()
        assert before == after
        assert "跳过" in message

    def test_missing_half_of_the_pair_is_completed(self, tmp_path: Path) -> None:
        # 只有 crt 没有 key：证书对不可用，补全整对（与 deploy.sh 的跳过条件一致）
        target = tmp_path / "partial"
        target.mkdir()
        (target / "selfsigned.crt").write_text("placeholder")
        ensure_certs(target, [])
        assert _san_values(_load_pair(target)[0])


class TestFailureIsReadable:
    """失败要留下一句能看懂的中文，而不是 traceback。"""

    def test_unusable_path_returns_nonzero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("我不是目录")
        assert main(["--dir", str(blocker / "sub")]) == 1
        assert "证书写入失败" in capsys.readouterr().err


class TestHostListHelpers:
    """SAN 附加项解析：去空去重、IP 与 DNS 分型。"""

    def test_split_hosts_dedups_and_trims(self) -> None:
        assert _split_hosts(" a , ,b, a ,c ") == ["a", "b", "c"]
        assert _split_hosts(",,  ") == []

    def test_ipv6_is_an_ip_entry(self) -> None:
        entries, rejected = _build_san(["::1", "example.com", "bad host!"])
        values = [str(item.value) for item in entries]
        assert "::1" in values and "example.com" in values
        assert any(isinstance(item.value, ipaddress.IPv6Address) for item in entries)
        assert rejected == ["bad host!"]


class TestComposeWiring:
    """compose 必须真的在启动链上调用它，否则前面全白做。"""

    def test_certgen_service_runs_the_bootstrap_script(self) -> None:
        text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "certgen:" in text
        assert "tools/ensure_certs.py" in text
        # 一次性任务不能被拉起成常驻服务，否则 restart 会让它反复重建证书
        assert 'restart: "no"' in text

    def test_app_waits_for_certgen_and_keeps_certs_read_only(self) -> None:
        text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "service_completed_successfully" in text
        # 写证书是 certgen 的权限，应用进程只读即可
        assert "./certs:/app/certs:ro" in text


class TestImageAndPackageCarryTheScript:
    """镜像与部署包都要带上脚本：漏一处，那条部署路径就还是起不来。"""

    def test_dockerfile_copies_the_script(self) -> None:
        text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY tools/ensure_certs.py" in text

    def test_package_generator_syncs_dockerfile_and_file(self) -> None:
        text = (
            PROJECT_ROOT / "tools" / "deploy_package" / "package_deploy.py"
        ).read_text(encoding="utf-8")
        # 部署包用的是内嵌 Dockerfile 副本，不同步就等于包内镜像缺脚本
        assert "COPY tools/ensure_certs.py" in text
        assert 'output_dir / "tools" / "ensure_certs.py"' in text

    def test_package_verifier_requires_it(self) -> None:
        text = (
            PROJECT_ROOT / "tools" / "deploy_package" / "verify_package.sh"
        ).read_text(encoding="utf-8")
        assert "tools/ensure_certs" in text
        block = text[text.index("tools/ensure_certs") - 400 : text.index("tools/ensure_certs") + 400]
        assert "FAIL" in block, "包校验缺该脚本时必须是 FAIL，不能只是 WARN"


class TestDocsKeepThePromise:
    """文档说"自动生成证书"，就得在 Docker 直跑这条路径上也成立。"""

    @pytest.mark.parametrize("readme", ["../README.md", "README.md"])
    def test_readme_mentions_compose_cert_generation(self, readme: str) -> None:
        text = (PROJECT_ROOT / readme).read_text(encoding="utf-8")
        assert "ensure_certs" in text or "certgen" in text
