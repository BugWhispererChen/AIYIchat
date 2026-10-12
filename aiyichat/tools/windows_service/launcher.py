"""爱易聊 AIYIchat Windows 服务面板（CustomTkinter GUI）。

承载 FastAPI Web 服务（uvicorn 工作线程），面板实时显示：
- 服务状态 / MongoDB 连接状态 / 运行时长 / 访问地址
- 资源占用（进程与系统的 CPU、内存，psutil 周期采集）
- 滚动运行日志（同时写入 logs/service.log，按大小轮转）

目录约定（冻结模式与源码模式一致，均以 BASE_DIR 为根）：
    BASE_DIR/app/          应用包
    BASE_DIR/certs/        SSL 自签名证书（缺失时自动生成）
    BASE_DIR/ffmpeg/bin/   捆绑 ffmpeg（自动加入 PATH）
    BASE_DIR/.env          环境配置（pydantic-settings 读取）
    BASE_DIR/logs/         运行日志

关闭窗口时最小化到系统托盘（pystray）后台运行，托盘菜单可还原/退出。
"""

import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

# ── 路径定位：冻结模式以 exe 所在目录为根，源码模式以 aiyichat 根目录为根 ──
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parents[2]

# 应用包与 .env 均相对 BASE_DIR 定位
os.chdir(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# windowed 模式（PyInstaller runw）无控制台，stdout/stderr 为 None，
# 部分库（如 uvicorn 日志格式化）会因 isatty() 崩溃，统一兜底到空设备
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull

# 捆绑 ffmpeg 加入 PATH（audio_utils 通过子进程调用）
_FFMPEG_BIN = BASE_DIR / "ffmpeg" / "bin"
if _FFMPEG_BIN.is_dir():
    os.environ["PATH"] = str(_FFMPEG_BIN) + os.pathsep + os.environ.get("PATH", "")

_CERT_DIR = BASE_DIR / "certs"
_CERT_KEY = _CERT_DIR / "selfsigned.key"
_CERT_CRT = _CERT_DIR / "selfsigned.crt"
_LOG_DIR = BASE_DIR / "logs"
_DEFAULT_HTTPS_PORT = 8443  # Windows 默认避开 443（需管理员权限）


# ─────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────

def _read_env_var(name: str, default: str) -> str:
    """从 .env 读取变量（简易解析，不引入额外依赖；找不到返回默认值）。"""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return default


def _get_https_port() -> int:
    """读取 HTTPS 端口：环境变量 > .env > 默认 8443。"""
    raw = os.environ.get("HTTPS_PORT") or _read_env_var("HTTPS_PORT", "")
    try:
        return int(raw) if raw else _DEFAULT_HTTPS_PORT
    except ValueError:
        return _DEFAULT_HTTPS_PORT


def _get_mongo_port() -> int:
    """从 .env 的 MONGODB_URI 解析端口（默认 27017）。"""
    uri = os.environ.get("MONGODB_URI") or _read_env_var(
        "MONGODB_URI", "mongodb://localhost:27017"
    )
    try:
        host_part = uri.split("//", 1)[1].split("/", 1)[0]
        if ":" in host_part:
            return int(host_part.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        pass
    return 27017


def _get_lan_ip() -> str | None:
    """探测本机局域网 IP（UDP 连接外部地址，不实际发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _ensure_certs() -> None:
    """确保证书存在，缺失时生成自签名证书（有效期 10 年）。

    SAN 包含 localhost / 127.0.0.1 及探测到的局域网 IP，
    避免浏览器通过 IP 访问时证书域名不匹配。
    """
    if _CERT_KEY.exists() and _CERT_CRT.exists():
        return
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)

    # 构建 SAN 列表：localhost + 127.0.0.1 + 局域网 IP（如可探测）
    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    lan_ip = _get_lan_ip()
    if lan_ip and lan_ip != "127.0.0.1":
        san_entries.append(x509.IPAddress(ipaddress.IPv4Address(lan_ip)))

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    _CERT_KEY.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    _CERT_CRT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _check_mongo(port: int, timeout: float = 1.0) -> bool:
    """TCP 探测 MongoDB 端口（不依赖驱动握手）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _format_duration(seconds: float) -> str:
    """秒数格式化为 'X天 Y小时 Z分钟'。"""
    s = max(int(seconds), 0)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}天 {hours}小时 {minutes}分钟"
    if hours > 0:
        return f"{hours}小时 {minutes}分钟"
    return f"{minutes}分钟"


def _icon_path() -> Path | None:
    """程序图标路径：冻结模式在 _internal/assets，源码模式在 windows_service/assets。"""
    if getattr(sys, "frozen", False):
        internal = Path(getattr(sys, "_MEIPASS", "")) / "assets" / "app_icon.ico"
        if internal.exists():
            return internal
    src = Path(__file__).resolve().parent / "assets" / "app_icon.ico"
    return src if src.exists() else None


def _load_tray_image():
    """加载托盘图标 PIL.Image（缺失时绘制简易蓝底气泡占位图）。"""
    from PIL import Image

    path = _icon_path()
    if path is not None:
        try:
            return Image.open(path)
        except OSError:
            pass
    # 占位图标：蓝底 + 白色气泡，保证托盘始终可用
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    try:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(47, 128, 237, 255))
        draw.rounded_rectangle([14, 16, 50, 42], radius=8, fill=(255, 255, 255, 255))
        draw.polygon([(20, 42), (30, 42), (22, 52)], fill=(255, 255, 255, 255))
    except Exception:  # noqa: BLE001 绘制失败时仅用纯色底
        pass
    return img


_MUTEX_NAME = "AIYIchat_ServicePanel_Mutex"
_mutex_handle: object | None = None  # 持有句柄防止被 GC 释放


def _acquire_single_instance() -> bool:
    """Windows 命名互斥体单实例保护；已有面板实例运行时返回 False。"""
    global _mutex_handle
    import ctypes

    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    error_already_exists = 183
    return kernel32.GetLastError() != error_already_exists


def _show_message(title: str, text: str) -> None:
    """无依赖的 Windows 消息框（3 秒自动关闭，避免残留进程）。"""
    import ctypes

    # MessageBoxTimeoutW：带自动关闭的消息框，防止第二实例阻塞挂起
    ctypes.windll.user32.MessageBoxTimeoutW(0, text, title, 0x30, 0, 3000)


class _GuiLogHandler(logging.Handler):
    """将日志转发到 GUI 文本框的 Handler（线程安全：经 after 调度到主线程）。"""

    def __init__(self, app: "ServicePanelApp") -> None:
        super().__init__()
        self._app = app
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 日志格式化异常不应中断服务
            return
        try:
            self._app.root.after(0, self._app.append_log, msg)
        except Exception:  # noqa: BLE001 GUI 已销毁时静默忽略
            pass


# ─────────────────────────────────────────────────────────
# 主界面
# ─────────────────────────────────────────────────────────

import customtkinter as ctk  # noqa: E402


class ServicePanelApp(ctk.CTk):
    """服务面板主窗口。"""

    _STATUS_COLORS = {
        "未启动": "#888888",
        "启动中": "#e6a23c",
        "运行中": "#2ecc71",
        "停止中": "#e6a23c",
        "已停止": "#888888",
        "异常": "#e74c3c",
    }

    def __init__(self) -> None:
        super().__init__()
        self.title("爱易聊 AIYIchat 服务面板")
        self.geometry("760x620")
        self.minsize(680, 560)
        icon = _icon_path()
        if icon is not None:
            try:
                self.iconbitmap(str(icon))
            except Exception:  # noqa: BLE001 图标加载失败不影响主流程
                pass

        self._server: object | None = None
        self._server_thread: threading.Thread | None = None
        self._started_at_mono: float | None = None  # monotonic 计时，防时钟回拨
        self._error_msg: str = ""
        self._mongo_ok: bool = False
        self._proc: psutil.Process | None = None  # 惰性初始化
        self._psutil_broken_notified: bool = False  # psutil 不完整时仅提示一次
        self._tray_icon: object | None = None
        self._tray_image = None  # 持有引用防止被 GC
        self._quit_requested: bool = False

        self._build_ui()
        self._setup_file_logging()
        logging.getLogger().addHandler(_GuiLogHandler(self))

        # 周期任务
        self.after(500, self._tick_metrics)
        self.after(1000, self._tick_status)
        # 面板启动后自动拉起服务（无需手动点击）
        self.after(1000, self._start_service)
        # 关闭窗口 = 最小化到托盘后台运行（真正退出走托盘菜单/退出按钮）
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

    # ── UI 构建 ──

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        status_frame = ctk.CTkFrame(self)
        status_frame.pack(fill="x", **pad)
        status_frame.grid_columnconfigure(1, weight=1)

        self.status_label = ctk.CTkLabel(
            status_frame, text="服务状态：未启动",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.status_label.grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 2))

        self.mongo_label = ctk.CTkLabel(status_frame, text="MongoDB：检测中…")
        self.mongo_label.grid(row=1, column=0, sticky="w", padx=10)
        self.uptime_label = ctk.CTkLabel(status_frame, text="运行时长：-")
        self.uptime_label.grid(row=1, column=1, sticky="w", padx=10)
        self.url_label = ctk.CTkLabel(status_frame, text="", text_color="#3498db")
        self.url_label.grid(row=1, column=2, sticky="e", padx=10)

        metrics_frame = ctk.CTkFrame(self)
        metrics_frame.pack(fill="x", **pad)
        for i in range(4):
            metrics_frame.grid_columnconfigure(i, weight=1)
        self.proc_cpu_label = self._metric_label(metrics_frame, 0, "进程 CPU", "-")
        self.proc_mem_label = self._metric_label(metrics_frame, 1, "进程内存", "-")
        self.sys_cpu_label = self._metric_label(metrics_frame, 2, "系统 CPU", "-")
        self.sys_mem_label = self._metric_label(metrics_frame, 3, "系统内存", "-")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ctk.CTkButton(
            btn_frame, text="启动服务", width=110, command=self._start_service
        )
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ctk.CTkButton(
            btn_frame, text="停止服务", width=110, state="disabled",
            command=self._stop_service,
        )
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = ctk.CTkButton(
            btn_frame, text="打开管理后台", width=110, command=self._open_admin
        )
        self.open_btn.pack(side="left", padx=8)
        ctk.CTkButton(
            btn_frame, text="退出", width=90, fg_color="#c0392b",
            hover_color="#96281b", command=self._real_exit,
        ).pack(side="right")

        log_frame = ctk.CTkFrame(self)
        log_frame.pack(fill="both", expand=True, **pad)
        ctk.CTkLabel(log_frame, text="运行日志").pack(anchor="w", padx=10, pady=(6, 0))
        self.log_box = ctk.CTkTextbox(log_frame, wrap="none", font=("Consolas", 12))
        self.log_box.pack(fill="both", expand=True, padx=10, pady=6)
        self.log_box.configure(state="disabled")

    def _metric_label(self, parent: ctk.CTkFrame, col: int, title: str, value: str) -> ctk.CTkLabel:
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=0, column=col, sticky="nsew", padx=6, pady=6)
        ctk.CTkLabel(cell, text=title, font=ctk.CTkFont(size=12)).pack()
        val = ctk.CTkLabel(cell, text=value, font=ctk.CTkFont(size=16, weight="bold"))
        val.pack()
        return val

    def _setup_file_logging(self) -> None:
        """日志文件：logs/service.log，单文件 5MB x 5 轮转。"""
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            _LOG_DIR / "service.log", maxBytes=5 * 1024 * 1024,
            backupCount=5, encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)

    # ── 日志与状态刷新 ──

    def append_log(self, msg: str) -> None:
        """向日志文本框追加一行（主线程调用）。"""
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        # 限制行数，避免长时间运行内存膨胀
        if int(self.log_box.index("end-1c").split(".")[0]) > 2000:
            self.log_box.delete("1.0", "500.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        color = self._STATUS_COLORS.get(text, "#888888")
        self.status_label.configure(text=f"服务状态：{text}", text_color=color)

    def _tick_status(self) -> None:
        """每秒刷新状态区：运行时长、MongoDB 探测、状态机同步。"""
        # MongoDB 探测（独立守护线程，避免阻塞主线程）
        port = _get_mongo_port()

        def _probe() -> None:
            self._mongo_ok = _check_mongo(port)

        threading.Thread(target=_probe, daemon=True).start()
        self.mongo_label.configure(
            text=f"MongoDB：{'已连接' if self._mongo_ok else '未连接'}",
            text_color="#2ecc71" if self._mongo_ok else "#e74c3c",
        )

        if self._started_at_mono is not None:
            self.uptime_label.configure(
                text="运行时长：" + _format_duration(
                    time.monotonic() - self._started_at_mono
                )
            )

        # uvicorn 线程退出后的状态同步
        if self._server is not None and getattr(self._server, "should_exit", False):
            pass  # 停止流程进行中
        elif self._server_thread is not None and not self._server_thread.is_alive():
            if self._error_msg:
                self._set_status("异常")
                self.append_log(f"[面板] 服务异常退出：{self._error_msg}")
            else:
                self._set_status("已停止")
            self._server, self._server_thread = None, None
            self._started_at_mono = None
            self.uptime_label.configure(text="运行时长：-")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.after(1000, self._tick_status)

    def _tick_metrics(self) -> None:
        """每 2 秒刷新资源占用。"""
        try:
            # 某些打包/运行环境下 psutil 可能被裁剪或导入到错误模块，
            # 表现为缺少 Process 等核心 API；此时跳过资源采集，避免刷屏。
            if not hasattr(psutil, "Process"):
                if not self._psutil_broken_notified:
                    self._psutil_broken_notified = True
                    self.append_log(
                        "[面板] 资源采集不可用：psutil 缺少 Process（可能打包缺失/运行环境异常），已跳过资源占用显示"
                    )
                return

            if self._proc is None:
                self._proc = psutil.Process(os.getpid())
            proc_cpu = self._proc.cpu_percent(interval=None)
            proc_mem = self._proc.memory_info().rss / (1024 * 1024)
            sys_cpu = psutil.cpu_percent(interval=None)
            sys_mem = psutil.virtual_memory().percent
            self.proc_cpu_label.configure(text=f"{proc_cpu:.1f}%")
            self.proc_mem_label.configure(text=f"{proc_mem:.0f} MB")
            self.sys_cpu_label.configure(text=f"{sys_cpu:.1f}%")
            self.sys_mem_label.configure(text=f"{sys_mem:.1f}%")
        except Exception as e:  # noqa: BLE001 采集失败不影响主流程
            self.append_log(f"[面板] 资源采集失败：{e}")
        finally:
            self.after(2000, self._tick_metrics)

    # ── 服务控制 ──

    def _start_service(self) -> None:
        if self._server_thread is not None and self._server_thread.is_alive():
            return
        self._error_msg = ""
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("启动中")
        self.url_label.configure(
            text=f"https://localhost:{_get_https_port()}/admin/login"
        )
        threading.Thread(target=self._run_server, daemon=True).start()

    def _run_server(self) -> None:
        """工作线程：以编程方式启动 uvicorn（阻塞直至服务停止）。"""
        import uvicorn

        port = _get_https_port()
        try:
            _ensure_certs()
            config = uvicorn.Config(
                "app.main:app",
                host="0.0.0.0",  # noqa: S104 桌面部署场景需局域网可访问
                port=port,
                ssl_keyfile=str(_CERT_KEY),
                ssl_certfile=str(_CERT_CRT),
                # 不套用 uvicorn 默认日志配置（其 stderr 格式化在无控制台时不可用），
                # 日志统一走根 logging（文件 + 面板日志区）
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            self._started_at_mono = time.monotonic()
            self._server.run()
        except Exception as e:  # noqa: BLE001 启动失败统一反馈到面板
            self._error_msg = str(e)
            logging.getLogger(__name__).exception("服务启动失败")
        finally:
            self._started_at_mono = None

    def _stop_service(self) -> None:
        server = self._server
        if server is None:
            return
        self._set_status("停止中")
        self.stop_btn.configure(state="disabled")
        self.append_log("[面板] 正在停止服务…")
        setattr(server, "should_exit", True)

    def _open_admin(self) -> None:
        port = _get_https_port()
        webbrowser.open(f"https://localhost:{port}/admin/login")

    # ── 托盘与退出 ──

    def _ensure_tray(self) -> None:
        """创建托盘图标（仅一次；pystray.run 需独占线程）。"""
        if self._tray_icon is not None:
            return
        import pystray

        self._tray_image = _load_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示面板", self._tray_show, default=True),
            pystray.MenuItem("打开管理后台", self._tray_open_admin),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_exit),
        )
        self._tray_icon = pystray.Icon(
            "aiyichat_service", self._tray_image, "爱易聊服务面板", menu
        )
        threading.Thread(
            target=self._tray_icon.run,  # type: ignore[union-attr]
            daemon=True, name="tray-icon",
        ).start()

    def _hide_to_tray(self) -> None:
        """关闭窗口 → 最小化到托盘，服务继续后台运行。"""
        try:
            self._ensure_tray()
            self.withdraw()
            self.append_log("[面板] 已最小化到托盘，服务继续后台运行")
        except Exception as e:  # noqa: BLE001 托盘不可用时退化为普通最小化
            self.append_log(f"[面板] 托盘创建失败，退化为最小化：{e}")
            self.iconify()

    def _tray_show(self, _icon: object = None, _item: object = None) -> None:
        """托盘菜单：还原面板（pystray 回调，调度回主线程）。"""
        self.after(0, self._show_from_tray)

    def _show_from_tray(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_open_admin(self, _icon: object = None, _item: object = None) -> None:
        self.after(0, self._open_admin)

    def _tray_exit(self, _icon: object = None, _item: object = None) -> None:
        self.after(0, self._real_exit)

    def _real_exit(self) -> None:
        """真正退出：先优雅停止服务，再销毁托盘与窗口。"""
        server = self._server
        if server is not None and self._server_thread is not None \
                and self._server_thread.is_alive():
            if not self._quit_requested:
                self._quit_requested = True
                setattr(server, "should_exit", True)
                self._set_status("停止中")
                self.append_log("[面板] 退出前正在停止服务…")
            self.after(300, self._real_exit)
            return
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 托盘停止失败不阻塞退出
                pass
            self._tray_icon = None
        self.destroy()


def main() -> None:
    """程序入口：单实例保护后启动面板。"""
    if not _acquire_single_instance():
        _show_message(
            "爱易聊服务面板",
            "服务面板已在运行，请勿重复启动。\n"
            "若确认无窗口运行，请在任务管理器中结束残留的 "
            "AIYIchatService 进程后重试。",
        )
        sys.exit(0)
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = ServicePanelApp()
    app.mainloop()


if __name__ == "__main__":
    main()
