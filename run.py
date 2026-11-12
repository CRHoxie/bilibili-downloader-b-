"""启动入口：拉起本地服务并自动打开浏览器。"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def pick_port(start: int = DEFAULT_PORT) -> int:
    """端口被占用时顺延，避免启动失败。"""
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    return start


def main() -> None:
    # 控制台编码不一致时不要让中文日志直接抛异常
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    port = pick_port()
    url = f"http://{HOST}:{port}"
    print("=" * 56)
    print("  B 站视频下载器已启动")
    print(f"  访问地址: {url}")
    print("  关闭此窗口即可停止服务")
    print("=" * 56)

    def open_browser() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            print(f"（自动打开浏览器失败，请手动访问 {url}）")

    threading.Timer(1.2, open_browser).start()
    uvicorn.run("app:app", host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
