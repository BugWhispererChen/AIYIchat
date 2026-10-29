"""一键构建 Windows 安装包：PyInstaller 产物 + Inno Setup 编译。

流程：
1. 若 dist/AIYIchatService 不存在则先执行 build_app.py
2. 确保 vendor/mongodb/bin/mongod.exe 存在（缺失时下载 MongoDB 免安装 zip）
3. 调用 ISCC 编译 installer.iss，产出 dist/AIYIchat_Setup_<分支>_<日期>.exe

依赖：
- pip install -r tools/windows_service/requirements-windows.txt
- Inno Setup 6（缺失时提示：winget install JRSoftware.InnoSetup）

环境变量：
- AIYICHAT_BRANCH：指定产物名中的分支标识（默认取当前 Git 分支）
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime

APP_VERSION = "1.0.0"
DEFAULT_BRANCH = "developing"
# 构建日期（YYYYMMDD）：随产物文件名发布，便于区分不同日期的安装包

# MongoDB 免安装提取（Windows x64）：下载官方 MSI 后用 msiexec /a 解压 mongod。
# 选用 7.0 LTS：8.x 在部分 Windows 10 环境存在 DLL 入口点缺失（0xC0000139）。
# zip 直链在国内常被 403 拦截，而 MSI 可达。
MONGO_VERSION = "7.0.28"
MONGO_URL = (
    "https://fastdl.mongodb.org/windows/"
    f"mongodb-windows-x86_64-{MONGO_VERSION}-signed.msi"
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DIST_DIR = os.path.join(_SCRIPT_DIR, "dist")
_VENDOR_DIR = os.path.join(_SCRIPT_DIR, "vendor")
_APP_DIR = os.path.join(_DIST_DIR, "AIYIchatService")
_MONGO_DIR = os.path.join(_VENDOR_DIR, "mongodb")

_ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def _resolve_branch() -> str:
    """解析构建分支：环境变量优先，否则取当前 Git 分支。"""
    name = os.environ.get("AIYICHAT_BRANCH", "").strip()
    if not name:
        result = subprocess.run(
            ["git", "-C", _SCRIPT_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            name = result.stdout.strip()
    if not name or name == "HEAD":
        name = DEFAULT_BRANCH
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", name).strip("-")
    return cleaned or DEFAULT_BRANCH


def _find_iscc() -> str:
    """定位 ISCC.exe：环境变量 > PATH > 常见安装路径。"""
    env_path = os.environ.get("INNOSETUP_ISCC")
    if env_path and os.path.exists(env_path):
        return env_path
    found = shutil.which("iscc")
    if found:
        return found
    for path in _ISCC_CANDIDATES:
        if os.path.exists(path):
            return path
    print("[ERROR] 未找到 Inno Setup 编译器（ISCC.exe）")
    print("  安装方式: winget install JRSoftware.InnoSetup")
    print("  或设置环境变量 INNOSETUP_ISCC 指向 ISCC.exe 完整路径")
    sys.exit(1)


def _ensure_app_dir() -> None:
    """确保 PyInstaller 产物存在，缺失时先执行 build_app.py。"""
    if os.path.exists(os.path.join(_APP_DIR, "AIYIchatService.exe")):
        print(f"[OK] PyInstaller 产物已存在: {_APP_DIR}")
        return
    print("[1/3] PyInstaller 产物不存在，先执行 build_app.py ...")
    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPT_DIR, "build_app.py")]
    )
    if result.returncode != 0:
        print("[ERROR] build_app.py 执行失败")
        sys.exit(1)


def _copy_from_installed() -> bool:
    """从本机已安装的 MongoDB 7.x（winget/MSI 默认路径）复制 mongod.exe。

    仅复制可执行文件：pdb 符号文件解压后超 1GB，会撑爆目标磁盘且运行无需。
    仅采用 7.x：8.x 在部分 Windows 10 环境存在 DLL 入口点缺失问题。
    """
    for mongod in sorted(
        glob.glob(r"C:\Program Files\MongoDB\Server\7.*\bin\mongod.exe"),
        reverse=True,
    ):
        dst_bin = os.path.join(_MONGO_DIR, "bin")
        os.makedirs(dst_bin, exist_ok=True)
        shutil.copy2(mongod, os.path.join(dst_bin, "mongod.exe"))
        print(f"  已从本机安装复制 mongod.exe: {mongod}")
        return True
    return False


def _ensure_mongodb() -> None:
    """确保 vendor/mongodb/bin/mongod.exe 存在。

    获取顺序：vendor 缓存 > 本机已安装目录 > 官方 zip 下载（国内可能 403）。
    """
    mongod = os.path.join(_MONGO_DIR, "bin", "mongod.exe")
    if os.path.exists(mongod):
        print(f"[OK] MongoDB 已缓存: {mongod}")
        return
    os.makedirs(_VENDOR_DIR, exist_ok=True)
    if _copy_from_installed():
        print(f"[OK] MongoDB 就绪: {mongod}")
        return
    msi_path = os.path.join(_VENDOR_DIR, os.path.basename(MONGO_URL))
    if not os.path.exists(msi_path):
        print(f"[2/3] 下载 MongoDB {MONGO_VERSION} MSI（约 600MB）...")
        print(f"  URL: {MONGO_URL}")
        try:
            req = urllib.request.Request(
                MONGO_URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(
                msi_path, "wb"
            ) as f:
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                while chunk := resp.read(1024 * 512):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r  已下载 {done * 100 // total}%",
                              end="", flush=True)
            print()
        except OSError as e:
            print(f"\n[ERROR] MongoDB 下载失败: {e}")
            print("  国内网络可改用本机安装后复制：")
            print("  1. winget install MongoDB.Server --version 7.0.28")
            print("  2. 重新运行本脚本（自动从安装目录复制）")
            print(f"  或手动下载 MSI 放置到: {msi_path}")
            sys.exit(1)
    # msiexec /a 管理安装模式：仅解压内容，不写注册表/不装服务
    print("  解压 MSI 提取 mongod.exe ...")
    extract_dir = os.path.join(_VENDOR_DIR, "msi_extract")
    result = subprocess.run(
        ["msiexec", "/a", msi_path, "/qn",
         f"TARGETDIR={os.path.abspath(extract_dir)}"],
    )
    if result.returncode != 0:
        print(f"[ERROR] MSI 解压失败（exit={result.returncode}）")
        sys.exit(1)
    for root, _dirs, files in os.walk(extract_dir):
        if "mongod.exe" in files:
            dst_bin = os.path.join(_MONGO_DIR, "bin")
            os.makedirs(dst_bin, exist_ok=True)
            shutil.copy2(os.path.join(root, "mongod.exe"),
                         os.path.join(dst_bin, "mongod.exe"))
            break
    shutil.rmtree(extract_dir, ignore_errors=True)
    if not os.path.exists(mongod):
        print("[ERROR] 解压后未找到 mongod.exe")
        sys.exit(1)
    print(f"[OK] MongoDB 就绪: {mongod}")


def main() -> None:
    """执行安装包构建。"""
    branch = _resolve_branch()
    print("=" * 60)
    print(f"  爱易聊 Windows 安装包构建（分支 {branch} / 版本 {APP_VERSION}）")
    print("=" * 60)

    build_date = datetime.now().strftime("%Y%m%d")

    iscc = _find_iscc()
    _ensure_app_dir()
    _ensure_mongodb()

    print(f"[3/3] 编译安装包: {iscc}")
    cmd = [
        iscc,
        f"/DMyAppVersion={APP_VERSION}",
        f"/DMyBranch={branch}",
        f"/DMyBuildDate={build_date}",
        f"/DAppSourceDir={_APP_DIR}",
        f"/DMongoDir={_MONGO_DIR}",
        f"/DOutputDir={_DIST_DIR}",
        os.path.join(_SCRIPT_DIR, "installer.iss"),
    ]
    result = subprocess.run(cmd, cwd=_SCRIPT_DIR)
    if result.returncode != 0:
        print("[ERROR] ISCC 编译失败")
        sys.exit(1)

    installer = os.path.join(
        _DIST_DIR, f"AIYIchat_Setup_{branch}_{build_date}.exe"
    )
    if os.path.exists(installer):
        size_mb = os.path.getsize(installer) / 1024 / 1024
        print(f"\n构建成功: {installer} ({size_mb:.1f}MB)")
    else:
        print("[ERROR] 安装包产物不存在")
        sys.exit(1)


if __name__ == "__main__":
    main()
