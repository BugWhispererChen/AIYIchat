"""系统资源监控工具模块。

使用 psutil 采集 CPU、内存、磁盘、运行时间、网络连接等系统指标，提供结构化数据。
磁盘路径平台自适应：POSIX 取根分区，Windows 取应用所在盘符。
运行时间为服务进程自身的 uptime（非系统开机时长），
避免 Windows 桌面部署时显示与预期不符的系统开机时长。
"""

import time
from dataclasses import dataclass
from pathlib import Path

import psutil


def _disk_root() -> str:
    """磁盘统计的目标分区：Windows 为应用所在盘符，POSIX 为根分区。"""
    return Path.cwd().anchor or "/"


def _service_uptime_seconds() -> float:
    """服务进程运行时长（秒）：当前进程创建至今，非系统开机时长。"""
    try:
        return time.time() - psutil.Process().create_time()
    except (psutil.Error, OSError):
        return 0.0


@dataclass
class SystemMetrics:
    """系统资源指标数据类。"""

    cpu_percent: float
    memory_percent: float
    memory_used_gb: float
    memory_total_gb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    uptime_seconds: float = 0.0
    net_connections: int = 0

    @property
    def uptime_display(self) -> str:
        """格式化运行时间显示，如 '3天 5小时'。"""
        seconds = int(self.uptime_seconds)
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        if days > 0:
            return f"{days}天 {hours}小时"
        elif hours > 0:
            return f"{hours}小时 {minutes}分钟"
        else:
            return f"{minutes}分钟"


def get_all_metrics() -> SystemMetrics:
    """获取所有系统资源指标的综合数据。

    Returns:
        SystemMetrics: 包含 CPU、内存、磁盘、运行时间、网络连接的结构化数据。
    """
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(_disk_root())

    # 服务进程运行时长（秒）：进程创建至今，非系统开机时长
    uptime = _service_uptime_seconds()

    # 网络连接数（需处理权限不足的情况）
    try:
        net_conns = len(psutil.net_connections())
    except (psutil.AccessDenied, PermissionError, OSError):
        net_conns = 0

    return SystemMetrics(
        cpu_percent=cpu,
        memory_percent=mem.percent,
        memory_used_gb=round(mem.used / (1024 ** 3), 1),
        memory_total_gb=round(mem.total / (1024 ** 3), 1),
        disk_percent=disk.percent,
        disk_used_gb=round(disk.used / (1024 ** 3), 1),
        disk_total_gb=round(disk.total / (1024 ** 3), 1),
        uptime_seconds=uptime,
        net_connections=net_conns,
    )
