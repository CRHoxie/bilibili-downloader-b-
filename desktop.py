"""B 站视频下载器 —— 桌面窗口版入口。

后端仍然是 app.py 里的本地服务（跑在一条后台线程里），
界面由 WebView2 内核渲染在一个无系统边框的窗口里，
所以看起来是一个独立应用，而不是浏览器里的一张网页。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import uvicorn
import webview

BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WINDOW_TITLE = "B 站视频下载器"


# ------------------------------------------------------------------ 工具

def pick_port(start: int = DEFAULT_PORT) -> int:
    """端口被占用时顺延，避免多开或残留进程导致启动失败。"""
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    return start


def wait_for_server(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.35)
            if s.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.12)
    return False


_SCALE: float | None = None


def dpi_scale() -> float:
    """系统缩放比例（125% → 1.25）。结果缓存，避免反复查询拿到不一致的值。"""
    global _SCALE
    if _SCALE:
        return _SCALE

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    dpi = 0
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        dpi = 0

    if dpi < 72:  # 老系统或调用失败时退回 GDI 查询
        try:
            user32 = ctypes.windll.user32
            hdc = user32.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)) or 96
            user32.ReleaseDC(0, hdc)
        except Exception:
            dpi = 96

    _SCALE = dpi / 96
    return _SCALE


def work_area() -> tuple[int, int, int, int]:
    """可用工作区（逻辑像素），已扣掉任务栏。"""
    try:
        # 必须先确保进程已 DPI 感知，否则拿到的坐标是系统虚拟化后的值，
        # 再除以缩放就会算小一圈（窗口会退到最小尺寸）
        scale = dpi_scale()
        rect = wintypes.RECT()
        # SPI_GETWORKAREA
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        return (
            int(rect.left / scale),
            int(rect.top / scale),
            int((rect.right - rect.left) / scale),
            int((rect.bottom - rect.top) / scale),
        )
    except Exception:
        return (0, 0, 1200, 800)


def window_size() -> tuple[int, int]:
    """按当前屏幕的可用工作区算出合适的窗口尺寸（逻辑像素）。

    注意：窗口带系统投影边框，实际内容区会比这个值各小 8~20px。
    """
    _, _, avail_w, avail_h = work_area()
    width = max(920, min(1220, avail_w - 40))
    height = max(640, min(900, avail_h - 12))
    return width, height


def resource_path(*parts: str) -> Path:
    """打包后资源在解包目录，源码运行时在项目目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")).joinpath(*parts)
    return BASE_DIR.joinpath(*parts)


def apply_window_icon(hwnd: int) -> None:
    """给窗口和任务栏换上应用图标（WinForms 窗口不会自动继承 exe 图标）。"""
    ico = resource_path("assets", "app.ico")
    if not ico.exists():
        return
    try:
        user32 = ctypes.windll.user32
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        for size, which in ((32, 1), (16, 0)):
            handle = user32.LoadImageW(0, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if handle:
                user32.SendMessageW(hwnd, WM_SETICON, which, handle)
                # 同时改窗口类图标，任务栏/Alt-Tab 才会跟着变
                user32.SetClassLongPtrW(hwnd, -14 if which == 1 else -34, handle)
    except Exception:
        pass


def post_start(attempts: int = 25, interval: float = 0.2) -> None:
    """给无边框窗口补回边缘拖拽缩放。

    pywebview 的 frameless 窗口默认没有 WS_THICKFRAME，
    用户没法用鼠标拉边缘改大小；补上这个样式位就能恢复，
    且不会出现系统边框（视觉上仍是纯自绘窗口）。
    """
    if sys.platform != "win32":
        return

    user32 = ctypes.windll.user32
    hwnd = None
    # 回调可能在窗口真正创建之前就跑了，这里等一下
    for _ in range(attempts):
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            break
        time.sleep(interval)
    if not hwnd:
        return

    apply_window_icon(hwnd)

    try:
        gwl_style = -16
        ws_thickframe = 0x00040000
        style = user32.GetWindowLongW(hwnd, gwl_style)
        if not style & ws_thickframe:
            user32.SetWindowLongW(hwnd, gwl_style, style | ws_thickframe)
            # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0020)
    except Exception:
        pass


def fatal(message: str) -> None:
    """pythonw 下没有控制台，用系统弹窗兜底提示。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, WINDOW_TITLE, 0x10)
    except Exception:
        print(message)


# ------------------------------------------------------------------ 窗口桥接

def _save_dialog_type() -> int:
    """pywebview 5 用 webview.SAVE_DIALOG，6 起改为 FileDialog.SAVE。"""
    enum = getattr(webview, "FileDialog", None)
    return enum.SAVE if enum is not None else webview.SAVE_DIALOG


def iter_cookie_pairs(cookies: Any):
    """把 pywebview 的 cookie 返回值统一拍平成 (name, value)。

    pywebview 6 的 get_cookies() 返回的是一个列表，每项是 SimpleCookie
    对象（dict 子类，键才是 cookie 名，值是 Morsel）；
    旧版本可能是带 name/value 的字典或对象，这里都兼容。
    """
    for item in cookies or []:
        if isinstance(item, dict):
            if "name" in item and "value" in item:
                yield item.get("name"), item.get("value")
            else:
                for name, morsel in item.items():
                    yield name, getattr(morsel, "value", morsel)
        else:
            yield getattr(item, "name", None), getattr(item, "value", None)


class WindowApi:
    """暴露给前端 JS 的窗口能力（最小化 / 最大化 / 关闭 / 另存为）。"""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._maximized = False
        self._restore_rect: tuple[int, int, int, int] = (0, 0, 1200, 800)

    # --- 窗口控制

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize(self) -> bool:
        if not self._window:
            return False

        if self._maximized:
            x, y, w, h = self._restore_rect
            self._window.resize(w, h)
            self._window.move(x, y)
            self._maximized = False
        else:
            # 手动按工作区铺满：系统自带的 maximize 对无边框窗口会盖住任务栏
            self._restore_rect = (
                self._window.x,
                self._window.y,
                self._window.width,
                self._window.height,
            )
            wx, wy, ww, wh = work_area()
            self._window.move(wx, wy)
            self._window.resize(ww, wh)
            self._maximized = True
        return self._maximized

    def close(self) -> None:
        if self._window:
            self._window.destroy()

    # --- 文件

    def save_task(self, task_id: str) -> dict[str, Any]:
        """把已完成的任务另存到用户指定位置（原生保存对话框）。"""
        import app as backend  # 与 uvicorn 线程共用同一个模块实例

        task = backend.manager.get(task_id)
        if not task:
            return {"ok": False, "detail": "任务不存在"}
        if task.status != "done" or not task.filepath.exists():
            return {"ok": False, "detail": "文件尚未就绪"}

        target = self._window.create_file_dialog(
            _save_dialog_type(),
            directory=str(backend.DOWNLOAD_DIR),
            save_filename=task.filepath.name,
        )
        if not target:
            return {"ok": False, "canceled": True}

        dest = target[0] if isinstance(target, (list, tuple)) else str(target)
        try:
            shutil.copy2(task.filepath, dest)
        except Exception as exc:
            return {"ok": False, "detail": f"保存失败：{exc}"}
        return {"ok": True, "path": dest}

    def reveal_task(self, task_id: str) -> dict[str, Any]:
        """在资源管理器里定位到具体文件。"""
        import app as backend

        task = backend.manager.get(task_id)
        if not task or not task.filepath.exists():
            return {"ok": False, "detail": "文件不存在"}
        try:
            subprocess.Popen(["explorer", f"/select,{task.filepath}"])
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True}

    # --- 登录

    def login_bilibili(self) -> dict[str, Any]:
        """打开 B 站登录窗口，登录成功后自动抓取 SESSDATA 并保存。

        登录页在应用内的独立窗口里打开，用户扫码或账号登录后，
        主线程轮询这个窗口的 Cookie，一旦出现 SESSDATA 就落盘并关窗。
        """
        import app as backend

        if backend.client.is_logged_in():
            return {"ok": True, "already": True}

        # 登录页是桌面版布局：窗口太小会被压成手机版，既看不到二维码旁的说明，
        # 也没有「密码登录 / 短信登录」这一栏，所以这里给足尺寸
        _, _, avail_w, avail_h = work_area()
        login_w = max(880, min(1220, avail_w - 40))
        login_h = max(560, min(660, avail_h - 20))

        state = {"closed": False}
        login_win = webview.create_window(
            "登录 B 站 · 登录成功后本窗口自动关闭",
            "https://passport.bilibili.com/login",
            width=login_w,
            height=login_h,
            min_size=(880, 560),
            resizable=True,
            background_color="#ffffff",
            text_select=True,
        )
        if login_win is None:
            return {"ok": False, "detail": "登录窗口创建失败"}

        login_win.events.closed += lambda: state.update(closed=True)

        # 二维码偶尔会因为网络抖动加载失败，页面上只留一句「点击刷新」。
        # 这里定时探一下，发现失效状态就替用户点一次刷新，免得他以为登录坏了。
        nudge_js = (
            "(function(){try{"
            "var t=document.body?document.body.innerText:'';"
            "if(t.indexOf('点击刷新')>=0||t.indexOf('二维码失效')>=0){"
            "var n=[].slice.call(document.querySelectorAll('div,span,a,button,p'))"
            ".filter(function(e){return e.children.length===0&&"
            "/点击刷新|二维码失效/.test(e.textContent||'');});"
            "if(n.length){n[0].click();return 'clicked';}}"
            "return '';}catch(e){return '';}})()"
        )

        deadline = time.time() + 300  # 最多等 5 分钟
        sessdata = ""
        tick = 0
        while time.time() < deadline and not state["closed"]:
            time.sleep(0.7)
            tick += 1
            if tick in (9, 18, 27):  # 约 6s / 12s / 18s 时各试一次
                try:
                    login_win.evaluate_js(nudge_js)
                except Exception:
                    pass
            try:
                cookies = login_win.get_cookies() or []
            except Exception:
                continue
            for name, value in iter_cookie_pairs(cookies):
                if name == "SESSDATA" and value:
                    sessdata = str(value).strip()
                    break
            if sessdata:
                break

        if not sessdata:
            try:
                if not state["closed"]:
                    login_win.destroy()
            except Exception:
                pass
            return {
                "ok": False,
                "canceled": state["closed"],
                "detail": "没有检测到登录状态，可以再试一次",
            }

        backend.config["sessdata"] = sessdata
        backend.client.set_sessdata(sessdata)
        backend.save_config(backend.config)
        logged = backend.client.is_logged_in()

        try:
            if not state["closed"]:
                login_win.destroy()
        except Exception:
            pass

        if self._window:
            try:
                self._window.evaluate_js(
                    "window.onLoginStateChanged && window.onLoginStateChanged()"
                )
            except Exception:
                pass

        return {
            "ok": logged,
            "detail": "" if logged else "Cookie 已保存，但服务端校验未通过，可能已被风控",
        }


# ------------------------------------------------------------------ 启动

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    port = pick_port()
    url = f"http://{HOST}:{port}/?desktop=1"

    # 直接传 ASGI 应用对象，不靠 "app:app" 字符串导入：
    # 打包成 exe 后字符串导入依赖运行时导入机制，容易踩坑
    import app as backend_mod

    server = uvicorn.Server(
        uvicorn.Config(backend_mod.app, host=HOST, port=port, log_level="warning", access_log=False)
    )
    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()

    if not wait_for_server(port):
        raise RuntimeError("本地服务启动失败，请检查是否有安全软件拦截了本机端口。")

    api = WindowApi()
    win_w, win_h = window_size()

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=win_w,
        height=win_h,
        min_size=(900, 620),
        frameless=True,   # 去掉系统标题栏，用自绘的玻璃标题栏
        shadow=True,      # 保留系统圆角与投影，同时保留边缘拖拽改变大小
        background_color="#06070b",
        text_select=True,
        js_api=api,
    )
    api._window = window

    state = {"closing": False}

    def on_closed() -> None:
        state["closing"] = True
        server.should_exit = True

    window.events.closed += on_closed

    def watchdog() -> None:
        """后台服务挂掉时关窗，避免留下一个能点但一直报错的界面。"""
        server_thread.join()
        if state["closing"]:
            return
        fatal("后台服务意外停止，程序将关闭。\n请重新启动；若反复出现，请检查安全软件是否拦截了本机端口。")
        try:
            window.destroy()
        except Exception:
            os._exit(1)

    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()

    webview.start(post_start)
    # 后台线程里还有 uvicorn 与下载线程，直接退出避免进程卡在残留线程上
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        fatal(f"启动失败：{exc}\n\n请确认已安装依赖：pip install -r requirements.txt")
        sys.exit(1)
