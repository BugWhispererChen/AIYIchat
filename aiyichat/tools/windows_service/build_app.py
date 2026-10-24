"""PyInstaller 打包脚本：将 Windows 服务面板打包为 onedir 产物。

产出：dist/AIYIchatService/（launcher.exe + app 包 + data/stickers + certs + ffmpeg
+ 配置模板），供 installer.iss 制作安装包，也可直接绿色运行。

使用方法：
    pip install -r tools/windows_service/requirements-windows.txt
    python build_app.py
"""

import os
import shutil
import subprocess
import sys

# 仓库根目录的捆绑 ffmpeg（完整二进制，随包分发）
_FFMPEG_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "ffmpeg_full", "ffmpeg-master-latest-win64-gpl", "bin",
)

_APP_NAME = "AIYIchatService"

# launcher 无顶层应用依赖导入（uvicorn 字符串加载 app），
# PyInstaller 无法自动分析，需显式收集 app 的全部第三方依赖
_COLLECT_MODULES = [
    "fastapi", "starlette", "uvicorn",
    "motor", "pymongo", "dns",
    "pydantic", "pydantic_settings", "pydantic_core",
    "jinja2", "yaml", "multipart",
    "tiktoken", "openai", "httpx", "httpcore",
    "dashscope", "aiohttp", "websockets",
    "pilk", "PIL", "qrcode",
    "Crypto", "cryptography", "anyio", "sniffio",
    "pystray", "psutil",
]


def _copy_third_party_notices(dist_dir: str) -> None:
    """把第三方许可说明复制进产物目录。

    安装包分发 ffmpeg（GPL/LGPL 视构建配置）与 MongoDB Community（SSPL），
    只有二进制而没有许可说明 = 再分发时违反这两份许可；installer.iss 的 [Files]
    段按 AppSourceDir 通配递归收录，因此放进产物目录即随安装包一起交付。
    """
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets",
                       "THIRD_PARTY_NOTICES.txt")
    if not os.path.exists(src):
        print("[WARN] 第三方许可说明不存在，安装包将不含 LICENSE/NOTICE")
        return
    shutil.copy2(src, os.path.join(dist_dir, "THIRD_PARTY_NOTICES.txt"))
    print(f"  第三方许可说明已复制: {dist_dir}")


def _copy_ffmpeg(dist_dir: str) -> None:
    """将捆绑 ffmpeg 复制到产物目录 ffmpeg/bin（仅 ffmpeg.exe，减小体积）。"""
    target = os.path.join(dist_dir, "ffmpeg", "bin")
    os.makedirs(target, exist_ok=True)
    src = os.path.join(_FFMPEG_SRC, "ffmpeg.exe")
    if not os.path.exists(src):
        print(f"[WARN] 捆绑 ffmpeg 不存在: {src}，语音转换功能将不可用")
        return
    shutil.copy2(src, os.path.join(target, "ffmpeg.exe"))
    print(f"  ffmpeg 已复制: {target}")


def _write_windows_env(dist_dir: str, project_root: str) -> None:
    """基于 .env.example 生成 Windows 版默认配置 .env.default。

    差异：MongoDB 指向 localhost、HTTPS 端口默认 8443（避开需管理员的 443）、
    管理后台口令留空。安装包首次安装时将其复制为 .env（已存在则不覆盖），
    面板首次启动会用随机强口令填空并只在日志里显示一次（见 launcher._ensure_admin_password）——
    随源码分发的固定口令等于把后台白送给读过 README 的人，因此绝不能再写回默认值。
    """
    src = os.path.join(project_root, ".env.example")
    with open(src, encoding="utf-8") as f:
        content = f.read()
    content = content.replace(
        "HTTPS_PORT=443", "HTTPS_PORT=8443"
    ).replace(
        "MONGODB_URI=mongodb://mongo:27017",
        "MONGODB_URI=mongodb://localhost:27017",
    ).replace(
        "ADMIN_PASSWORD=your-admin-password", "ADMIN_PASSWORD=",
    )
    dst = os.path.join(dist_dir, ".env.default")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Windows 默认配置已生成: {dst}")


def main() -> None:
    """执行打包流程。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
    entry_py = os.path.join(script_dir, "launcher.py")
    dist_root = os.path.join(script_dir, "dist")
    dist_dir = os.path.join(dist_root, _APP_NAME)

    print("=" * 60)
    print("  爱易聊 Windows 服务面板 — PyInstaller 打包")
    print("=" * 60)

    try:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[ERROR] PyInstaller 未安装，请先运行: pip install pyinstaller")
        sys.exit(1)

    print("\n[1/3] PyInstaller 打包（onedir）...")
    icon_file = os.path.join(script_dir, "assets", "app_icon.ico")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", _APP_NAME,
        "--distpath", dist_root,
        "--workpath", os.path.join(script_dir, "build"),
        "--specpath", script_dir,
        # customtkinter 依赖 JSON 主题与字体资源，整体收集
        "--collect-all", "customtkinter",
    ]
    # app 依赖的第三方库整体收集子模块（launcher 无顶层 import，需显式声明）
    for mod in _COLLECT_MODULES:
        cmd += ["--collect-submodules", mod]
    cmd += [
        # app 包不被 PyInstaller 分析（uvicorn 字符串导入），以数据文件整体携带
        "--add-data", os.pathsep.join(
            [os.path.join(project_root, "app"), "app"]
        ),
        # 预置表情包资源（sticker_seeder 运行时按 __file__ 上溯三级定位到
        # _internal/data/stickers，须随包携带，否则启动时 seed 跳过）
        "--add-data", os.pathsep.join(
            [os.path.join(project_root, "data", "stickers"), "data/stickers"]
        ),
        # 预置角色定妆照资源（seed_companion_profiles 运行时按 __file__ 上溯定位）
        "--add-data", os.pathsep.join(
            [os.path.join(project_root, "data", "preset_portraits"), "data/preset_portraits"]
        ),
        # 预置角色种子脚本（启动时自动 seed：bootstrap.py lifespan 调用）
        "--add-data", os.pathsep.join(
            [os.path.join(project_root, "tools", "seed_companion_profiles.py"), "tools"]
        ),
    ]
    # 程序图标：exe 自身图标 + 随包 assets（托盘/窗口图标运行时读取）
    if os.path.exists(icon_file):
        cmd += [
            "--icon", icon_file,
            "--add-data", os.pathsep.join([
                os.path.join(script_dir, "assets"), "assets",
            ]),
        ]
    cmd.append(entry_py)
    result = subprocess.run(cmd, cwd=script_dir)
    if result.returncode != 0:
        print("[ERROR] PyInstaller 打包失败")
        sys.exit(1)

    exe_path = os.path.join(dist_dir, f"{_APP_NAME}.exe")
    if not os.path.exists(exe_path):
        print(f"[ERROR] 打包产物不存在: {exe_path}")
        sys.exit(1)

    print("\n[2/3] 复制随包资源（certs / ffmpeg / 配置模板）...")
    # SSL 证书（含自签名证书；缺失时 launcher 会自动生成）
    certs_src = os.path.join(project_root, "certs")
    if os.path.isdir(certs_src):
        shutil.copytree(certs_src, os.path.join(dist_dir, "certs"),
                        dirs_exist_ok=True)
        print("  certs 已复制")
    _copy_ffmpeg(dist_dir)
    _copy_third_party_notices(dist_dir)
    _write_windows_env(dist_dir, project_root)

    print("\n[3/3] 打包完成")
    print(f"  产物目录: {dist_dir}")
    print(f"  可执行文件: {exe_path}")


if __name__ == "__main__":
    main()
