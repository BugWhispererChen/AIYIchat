"""准备自签名 HTTPS 证书：`certs/` 缺失时生成，已有则原样跳过。

背景：镜像 CMD 硬要求 `/app/certs/selfsigned.key` 与 `.crt`，而 `certs/` 既不
入 Git 也不入部署包。照 README 用 `docker compose up` 直跑的部署者会在 uvicorn
装配 TLS 时撞上 `FileNotFoundError`——且该行报错不指明缺哪个文件。本脚本由
compose 的一次性 `certgen` 服务调用，把"自动生成证书"的承诺覆盖到 Docker 直跑
这条路径（Windows 面板与 deploy.sh/deploy.ps1 两条路径已有各自实现）。

用法（容器内由 compose 调用，也可手工执行）：
    python tools/ensure_certs.py
    python tools/ensure_certs.py --dir /srv/aiyichat/certs --extra-hosts 192.0.2.20,chat.local

环境变量：
    CERT_DIR          证书目录，默认 /app/certs
    CERT_EXTRA_HOSTS  追加进 SAN 的 DNS 名或 IP，逗号分隔（局域网 IP 直访时用）

已存在的证书一律不覆盖：生产部署者可能在部署前放入正式证书，静默替换等于删掉
他们的证书。私钥落盘权限收紧到 0600（Windows 的 chmod 只作用于只读位，无副作用）。
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CERT_DIR = "/app/certs"
CERT_NAME = "selfsigned"
VALID_DAYS = 3650


def _split_hosts(raw: str) -> list[str]:
    """拆分逗号分隔的主机清单，去空白与重复、保持顺序。"""
    seen: list[str] = []
    for part in raw.split(","):
        host = part.strip()
        if host and host not in seen:
            seen.append(host)
    return seen


def _dns(host: str):
    """DNS 名类型的 SAN 条目。"""
    from cryptography import x509

    return x509.DNSName(host)


def _ip(host: str):
    """IP 地址类型的 SAN 条目（非法字面值抛 ValueError 供调用方分流）。"""
    from cryptography import x509

    return x509.IPAddress(ipaddress.ip_address(host))


def _looks_like_hostname(host: str) -> bool:
    """粗判是否为可放进 SAN 的 DNS 名（RFC1035 字面集的实用子集）。"""
    if len(host) > 253 or host.startswith(".") or host.endswith("."):
        return False
    return all(c.isalnum() or c in ".-_" for c in host)


def _build_san(extra_hosts: list[str]) -> tuple[list, list[str]]:
    """构造 SAN 条目：固定 localhost + 127.0.0.1，附加项按 IP/DNS 分类。

    Returns:
        (SAN 条目列表, 被丢弃的非法附加项)
    """
    entries: list = [_dns("localhost"), _ip("127.0.0.1")]
    rejected: list[str] = []
    for host in extra_hosts:
        try:
            entries.append(_ip(host))
            continue
        except ValueError:
            pass
        if _looks_like_hostname(host):
            entries.append(_dns(host))
        else:
            rejected.append(host)
    return entries, rejected


def ensure_certs(cert_dir: Path, extra_hosts: list[str]) -> str:
    """确保证书存在，返回一句人类可读的结果说明。

    Raises:
        RuntimeError: 目录不可创建或证书写入失败
    """
    key_path = cert_dir / f"{CERT_NAME}.key"
    crt_path = cert_dir / f"{CERT_NAME}.crt"
    if key_path.exists() and crt_path.exists():
        return f"证书已存在，跳过生成：{crt_path} / {key_path}"

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    san_entries, rejected = _build_san(extra_hosts)
    if rejected:
        print(f"警告：忽略非法 SAN 条目 {', '.join(rejected)}", file=sys.stderr)

    try:
        cert_dir.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    except OSError as exc:
        raise RuntimeError(f"证书写入失败：{cert_dir}（{exc}）") from exc
    except Exception as exc:  # noqa: BLE001 - 生成失败要给出一条可读错误
        raise RuntimeError(f"证书生成失败：{exc}") from exc

    # 私钥只属主可读；Windows 上该调用仅影响只读位，Linux/容器内是真实收紧
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        print(f"警告：无法收紧私钥权限，请手工确认 {key_path} 不被其他用户读取", file=sys.stderr)

    san_text = ", ".join(str(entry.value) for entry in san_entries)
    return f"自签名证书已生成（有效期 {VALID_DAYS} 天，SAN：{san_text}）：{crt_path}"


def main(argv: list[str] | None = None) -> int:
    """命令行入口：成功返回 0，失败返回 1 并打印可读原因。"""
    parser = argparse.ArgumentParser(description="准备自签名 HTTPS 证书（已有则跳过）")
    parser.add_argument(
        "--dir",
        default=os.environ.get("CERT_DIR", DEFAULT_CERT_DIR),
        help=f"证书目录（默认 {DEFAULT_CERT_DIR}）",
    )
    parser.add_argument(
        "--extra-hosts",
        default=os.environ.get("CERT_EXTRA_HOSTS", ""),
        help="追加进 SAN 的 DNS 名或 IP，逗号分隔",
    )
    args = parser.parse_args(argv)

    try:
        message = ensure_certs(Path(args.dir), _split_hosts(args.extra_hosts))
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
