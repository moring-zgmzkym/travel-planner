"""应用窗口启动（2026-09-19，需求 4）：服务起来后弹出独立应用窗口，而非浏览器标签。

实现：探测 Edge（Windows 几乎必有，PDF 渲染同源依赖）→ `msedge --app=<url>`
打开无地址栏/无标签页的独立窗口，自动复用 static/manifest.webmanifest 的应用名与图标；
Edge 缺失回退系统默认浏览器。零新依赖。
环境变量 TRIPMATE_NO_WINDOW=1 禁用（自动化测试/冒烟脚本场景）。"""

import logging
import os
import shutil
import socket
import subprocess
import threading
import webbrowser

from .config import ServerConfig

logger = logging.getLogger("tripmate.app_window")

_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_WINDOW_WAIT_S = 15.0     # 服务端口就绪等待上限：超时静默放弃，绝不影响服务本身


def _display_url() -> str:
    """展示用 URL：HOST=0.0.0.0（监听全部网卡）不能作为访问地址，归一化为 127.0.0.1。"""
    host = (ServerConfig.HOST or "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{ServerConfig.PORT}"


def _wait_port(host: str, port: int, timeout_s: float) -> bool:
    """轮询端口就绪（复用 ws_status_smoke 的 wait_port 模式）。"""
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _find_edge() -> str | None:
    for p in _EDGE_PATHS:
        if os.path.exists(p):
            return p
    return shutil.which("msedge")


def open_app_window() -> None:
    """等端口就绪 → Edge --app 独立窗口（回退默认浏览器）。任何失败只记日志。"""
    url = _display_url()
    host = url.split("//", 1)[1].split(":", 1)[0]
    try:
        if not _wait_port(host, ServerConfig.PORT, _WINDOW_WAIT_S):
            logger.warning("服务端口 %s:%s 未就绪，跳过应用窗口弹出", host, ServerConfig.PORT)
            return
        edge = _find_edge()
        if edge:
            subprocess.Popen([edge, f"--app={url}"], close_fds=True)
            logger.info("应用窗口已打开（Edge --app）：%s", url)
        else:
            webbrowser.open(url)
            logger.info("未找到 Edge，已用系统默认浏览器打开：%s", url)
    except Exception as e:  # noqa: BLE001 — 窗口弹出失败绝不影响服务主流程
        logger.warning("应用窗口打开失败（%s: %s），可用浏览器访问 %s", type(e).__name__, e, url)


def open_app_window_async() -> None:
    """供启动入口调用：daemon 线程触发（uvicorn 启动失败时线程不悬挂进程）。"""
    if os.environ.get("TRIPMATE_NO_WINDOW", "").strip() in ("1", "true", "True"):
        logger.info("TRIPMATE_NO_WINDOW 已设置：跳过应用窗口弹出")
        return
    t = threading.Timer(0.5, open_app_window)
    t.daemon = True
    t.start()
