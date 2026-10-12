"""部署包打包脚本：导出 + 组装 + 打包为 tar.gz。

默认从 Git 裸仓库拉取最新代码后打包，
保证部署包内容与 Git 仓库完全一致（可追溯）。
仅产出 Linux 部署包（tar.gz + deploy.sh）；Windows 产物形态为单独
EXE 安装包，由本机 tools/windows_service/ 工具链构建，不走本脚本。

用法（在具备 Docker 与 Git 裸仓库访问权限的打包机执行）：
    python tools/deploy_package/package_deploy.py                    # 从 Git 拉取，产出 Linux 包（默认）
    python tools/deploy_package/package_deploy.py --local            # 使用当前目录代码打包

环境变量：
    AIYICHAT_GIT_BARE_DIR  Git 裸仓库路径（默认 <bare-repo-path>）
    AIYICHAT_GIT_BRANCH    打包分支（默认 main）
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

# Git 裸仓库（版本历史权威来源，打包代码必须与之一致）
GIT_BARE_DIR = os.environ.get("AIYICHAT_GIT_BARE_DIR", "<bare-repo-path>")
GIT_BRANCH = os.environ.get("AIYICHAT_GIT_BRANCH", "main")


def run_command(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """执行命令并返回 (returncode, stdout, stderr)。"""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, result.stdout, result.stderr


def pull_from_git(temp_dir: Path) -> Path | None:
    """[1/4] 从 Git 裸仓库导出最新代码到临时目录。

    使用 git archive 导出目标分支的 aiyichat 子树，
    确保打包内容与 Git 仓库完全一致。
    """
    print(f"[1/4] 从 Git 裸仓库拉取最新代码（{GIT_BRANCH}）...")
    archive_path = temp_dir / "aiyichat-latest.tar.gz"
    with archive_path.open("wb") as f:
        result = subprocess.run(
            [
                "git",
                f"--git-dir={GIT_BARE_DIR}",
                "archive",
                "--format=tar.gz",
                GIT_BRANCH,
                "aiyichat",
            ],
            stdout=f,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        print(
            f"错误：git archive 失败\n{result.stderr.decode(errors='replace')}",
            file=sys.stderr,
        )
        return None

    # 解压到临时目录（归档条目带 aiyichat/ 前缀）
    extract_dir = temp_dir / "git_repo"
    extract_dir.mkdir()
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(path=extract_dir, filter="fully_trusted")

    project_root = extract_dir / "aiyichat"
    if not project_root.is_dir():
        print(f"错误：导出内容缺少 aiyichat/ 目录: {project_root}", file=sys.stderr)
        return None

    # 显示导出的最新提交
    returncode, stdout, _ = run_command(
        ["git", f"--git-dir={GIT_BARE_DIR}", "log", "--oneline", "-1", GIT_BRANCH]
    )
    if returncode == 0:
        print(f"最新提交: {stdout.strip()}")
    print(f"代码已导出到: {project_root}")
    return project_root


def generate_deploy_dockerfile(output_path: Path) -> None:
    """生成部署版 Dockerfile（单阶段）。"""
    dockerfile_content = """FROM python:3.11-slim
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8
WORKDIR /app
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \\
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev \\
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt
COPY app/ ./app/
COPY http_redirect.py .
# 预置表情包资源（启动时自动 seed 入库，离线可用）
COPY data/stickers/ ./data/stickers/
# 预置角色种子脚本与定妆照素材（容器内手动执行 seed）
COPY tools/seed_companion_profiles.py ./tools/seed_companion_profiles.py
COPY data/preset_portraits/ ./data/preset_portraits/
# 服务端口可配置（宿主机与容器内一致），默认 HTTPS 443 + HTTP 80
ENV HTTPS_PORT=443 HTTP_PORT=80
EXPOSE 443 80
# HTTPS 主服务（uvicorn）+ HTTP→HTTPS 重定向服务
CMD ["sh", "-c", "python http_redirect.py & exec python -m uvicorn app.main:app --host 0.0.0.0 --port $HTTPS_PORT --ssl-keyfile /app/certs/selfsigned.key --ssl-certfile /app/certs/selfsigned.crt"]
"""
    output_path.write_text(dockerfile_content, encoding="utf-8")


def assemble_package(
    project_root: Path,
    output_dir: Path,
    runtime_dir: Path | None = None,
) -> bool:
    """组装部署包目录结构。

    Args:
        project_root: 代码根目录（Git 导出目录或当前项目根）
        output_dir: 部署包输出目录
        runtime_dir: 运行环境项目根（Git 导出内容缺失时的资源补充来源）
    """
    print("[2/4] 组装部署包（linux）...")

    # 复制 app 源码目录
    shutil.copytree(project_root / "app", output_dir / "app", dirs_exist_ok=True)

    # 复制部署文件（Windows 产物为 EXE 安装包，由 tools/windows_service/ 单独构建）
    files_to_copy = [
        "docker-compose.yml",
        ".env.example",
        "requirements.txt",
        "deploy.sh",
        "http_redirect.py",
        "README.md",
    ]
    for filename in files_to_copy:
        src = project_root / filename
        if src.exists():
            shutil.copy2(src, output_dir / filename)
        else:
            print(f"警告：文件不存在，跳过: {filename}")

    # 生成部署版 Dockerfile
    generate_deploy_dockerfile(output_dir / "Dockerfile")

    # 确保 tools 目录存在（seed 脚本需要）
    (output_dir / "tools").mkdir(exist_ok=True)

    # 复制预置表情包资源（随包内置，启动时自动 seed 入库）
    stickers_src = project_root / "data" / "stickers"
    if not stickers_src.exists() and runtime_dir is not None:
        stickers_src = runtime_dir / "data" / "stickers"
    if stickers_src.exists():
        shutil.copytree(
            stickers_src, output_dir / "data" / "stickers",
            dirs_exist_ok=True,
        )
    else:
        print("警告：预置表情包资源不存在，跳过: data/stickers/")

    # 复制预置角色定妆照资源（随包内置，seed 时写入 media 集合）
    portraits_src = project_root / "data" / "preset_portraits"
    if not portraits_src.exists() and runtime_dir is not None:
        portraits_src = runtime_dir / "data" / "preset_portraits"
    if portraits_src.exists():
        shutil.copytree(
            portraits_src, output_dir / "data" / "preset_portraits",
            dirs_exist_ok=True,
        )
    else:
        print("警告：预置角色定妆照不存在，跳过: data/preset_portraits/")

    # 复制预置角色种子脚本（部署后手动执行 seed）
    seed_script_src = project_root / "tools" / "seed_companion_profiles.py"
    if not seed_script_src.exists() and runtime_dir is not None:
        seed_script_src = runtime_dir / "tools" / "seed_companion_profiles.py"
    if seed_script_src.exists():
        shutil.copy2(seed_script_src, output_dir / "tools" / "seed_companion_profiles.py")
    else:
        print("警告：预置角色种子脚本不存在，跳过: tools/seed_companion_profiles.py")

    # 证书不入包：自签名证书由 deploy.sh / deploy.ps1 在部署时自动生成，
    # 生产环境可在部署前自行准备正式证书放入 certs/ 目录

    print(f"部署包已组装到: {output_dir}")
    return True


def create_tarball(source_dir: Path, output_path: Path) -> bool:
    """打包为 tar.gz。"""
    print("[3/4] 打包为 tar.gz...")

    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"打包完成: {output_path} ({size_mb:.1f} MB)")
    return True


def print_file_list(package_dir: Path) -> None:
    """输出文件清单。"""
    print("[4/4] 文件清单:")
    for item in sorted(package_dir.rglob("*")):
        if item.is_file():
            rel_path = item.relative_to(package_dir)
            size_kb = item.stat().st_size / 1024
            print(f"  {rel_path} ({size_kb:.1f} KB)")


def main() -> int:
    parser = argparse.ArgumentParser(description="AIYIchat 部署包打包脚本")
    parser.add_argument(
        "--from-git",
        action="store_true",
        help="从 Git 裸仓库拉取代码后打包（默认行为）",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="使用当前目录代码打包（跳过 Git 拉取）",
    )
    parser.add_argument(
        "--platform",
        choices=["linux"],
        default="linux",
        help="目标平台（仅支持 linux；Windows 产物为 EXE 安装包，"
        "由 tools/windows_service/ 工具链单独构建）",
    )
    args = parser.parse_args()

    if args.local and args.from_git:
        print("错误：--local 与 --from-git 不能同时使用", file=sys.stderr)
        return 1

    # 脚本位于 tools/deploy_package/ 下，项目根为上三级
    current_root = Path(__file__).resolve().parent.parent.parent
    date_str = datetime.now().strftime("%Y%m%d")
    platforms = [args.platform]

    # 创建临时目录
    with tempfile.TemporaryDirectory(prefix="aiyichat_deploy_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)

        # 1. 确定代码来源：默认从 Git 裸仓库拉取
        project_root: Path
        runtime_dir: Path
        if args.local:
            project_root = current_root
            runtime_dir = current_root
            print(f"使用当前目录代码打包: {project_root}")
        else:
            pulled_root = pull_from_git(temp_dir)
            if pulled_root is None:
                return 1
            project_root = pulled_root
            # Git 导出内容缺失时，从运行环境目录补充资源
            runtime_dir = current_root

        # 校验源码目录存在
        source_app = project_root / "app"
        if not source_app.is_dir():
            print(f"错误：源码目录不存在: {source_app}", file=sys.stderr)
            return 1

        # 2~4. 组装、打包并输出清单
        for platform in platforms:
            package_name = f"aiyichat-deploy-{date_str}-{platform}"
            package_dir = temp_dir / package_name
            package_dir.mkdir()

            if not assemble_package(project_root, package_dir, runtime_dir):
                return 1

            # 打包（输出到运行环境项目根，临时目录退出时会被清理）
            output_path = current_root / f"{package_name}.tar.gz"
            if not create_tarball(package_dir, output_path):
                return 1

            print_file_list(package_dir)
            print(f"\n✓ 部署包已生成: {output_path}")

        return 0


if __name__ == "__main__":
    sys.exit(main())
