"""首次启动的依赖安装器：无控制台窗口，自绘进度窗。

要点
----
* 只用标准库 + Win32 API —— 此时第三方依赖还没装，不能用任何库。
* 分层窗口（UpdateLayeredWindow）绘制，能做出抗锯齿圆角与半透明卡片，
  窗口类带 CS_DROPSHADOW，阴影交给系统画。
* pip 在后台线程执行，主线程只画界面。
* 依赖齐全时不显示任何窗口，直接启动主程序。
* 调试用：`python bootstrap.py --preview` 只放演示进度，不真的装依赖。
"""

from __future__ import annotations

import ctypes
import importlib.util
import math
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REQUIREMENTS = BASE_DIR / "requirements.txt"
APP_ENTRY = BASE_DIR / "desktop.py"
REQUIRED = ["fastapi", "uvicorn", "requests", "imageio_ffmpeg", "webview"]

# ---------------------------------------------------------------- 布局（逻辑像素）

CARD_W, CARD_H = 480, 222
RADIUS = 15
PAD = 28

# COLORREF = 0x00BBGGRR
C_BG_TOP = (30, 34, 47)
C_BG_BOTTOM = (17, 20, 28)
C_TITLE = 0x00F8F1EE
C_SUB = 0x00B5A096
C_STATUS = 0x00A6928A
C_ERR = 0x007A7AFF
C_PINK = 0x009972FB
C_TRACK = 0x00392B26
C_FOOTER = 0x006E6A62
C_CLOSE = 0x00989A9E
C_BORDER = 0x002D2F33

# ---------------------------------------------------------------- 句柄类型

HGDIOBJ = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
HPEN = ctypes.c_void_p
HFONT = ctypes.c_void_p
HDC = ctypes.c_void_p
HWND = ctypes.c_void_p
HICON = ctypes.c_void_p
HCURSOR = ctypes.c_void_p
HBITMAP = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wintypes.WORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, wintypes.HMENU, HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = HWND
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [HWND]
user32.SetTimer.argtypes = [HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.c_void_p, HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.SendMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.ReleaseCapture.argtypes = []
user32.LoadCursorW.argtypes = [HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = HCURSOR
user32.LoadImageW.argtypes = [HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = HICON
user32.UpdateLayeredWindow.argtypes = [
    HWND, HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]

gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateDIBSection.argtypes = [HDC, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, HBITMAP, wintypes.DWORD]
gdi32.CreateDIBSection.restype = HBITMAP
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = HBRUSH
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.CreatePen.restype = HPEN
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
gdi32.CreateFontW.restype = HFONT
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.SetTextColor.argtypes = [HDC, wintypes.DWORD]
gdi32.SetBkMode.argtypes = [HDC, ctypes.c_int]
user32.DrawTextW.argtypes = [HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int
gdi32.RoundRect.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.MoveToEx.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.LineTo.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.GdiFlush.argtypes = []
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = HGDIOBJ
gdi32.GetDeviceCaps.argtypes = [HDC, ctypes.c_int]
user32.GetDC.argtypes = [HWND]
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [HWND, HDC]

# ---------------------------------------------------------------- 常量

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
CS_DROPSHADOW = 0x00020000

WM_PAINT, WM_TIMER, WM_DESTROY = 0x000F, 0x0113, 0x0002
WM_LBUTTONDOWN, WM_KEYDOWN, WM_NCLBUTTONDOWN = 0x0201, 0x0100, 0x00A1
WM_SETICON = 0x0080
HTCAPTION, VK_ESCAPE = 2, 0x1B
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
DT_LEFT, DT_RIGHT = 0x0000, 0x0002
DT_TOP, DT_SINGLELINE, DT_NOPREFIX = 0x0000, 0x0020, 0x0800
DT_END_ELLIPSIS = 0x00008000
TRANSPARENT = 1
NULL_BRUSH = 5


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE), ("hIcon", HICON), ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT),
    ]


class TRIVERTEX(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG), ("y", wintypes.LONG),
        ("Red", wintypes.USHORT), ("Green", wintypes.USHORT),
        ("Blue", wintypes.USHORT), ("Alpha", wintypes.USHORT),
    ]


class GRADIENT_RECT(ctypes.Structure):
    _fields_ = [("UpperLeft", wintypes.ULONG), ("LowerRight", wintypes.ULONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# ---------------------------------------------------------------- 状态

STATE = {
    "percent": 0.0,
    "status": "检查运行环境…",
    "footer": "安装完成后会自动启动，无需操作",
    "error": "",
    "finished": False,
}
LOCK = threading.Lock()


def set_state(**kw) -> None:
    with LOCK:
        STATE.update(kw)


def get_state() -> dict:
    with LOCK:
        return dict(STATE)


# ---------------------------------------------------------------- 工具

def dpi_scale() -> float:
    try:
        return user32.GetDpiForSystem() / 96
    except Exception:
        try:
            hdc = user32.GetDC(None)
            dpi = gdi32.GetDeviceCaps(hdc, 88) or 96
            user32.ReleaseDC(None, hdc)
            return dpi / 96
        except Exception:
            return 1.0


def corner_alpha(px: int, py: int, r: int) -> float:
    """圆角内某像素的覆盖率（4×4 超采样），px/py 是相对角落的局部坐标。"""
    hits = 0
    for sy in range(4):
        for sx in range(4):
            x = px + (sx + 0.5) / 4
            y = py + (sy + 0.5) / 4
            if (x - r) ** 2 + (y - r) ** 2 <= r * r:
                hits += 1
    return hits / 16


class BootWindow:
    def __init__(self) -> None:
        self.scale = dpi_scale()
        self.w = int(CARD_W * self.scale)
        self.h = int(CARD_H * self.scale)
        self.hwnd: HWND | None = None
        self.mem_dc: HDC | None = None
        self.dib: HBITMAP | None = None
        self.bits = ctypes.c_void_p()
        self.bg = b""
        self.close_rect = (0, 0, 0, 0)
        self._proc = WNDPROC(self._wndproc)

    # --- 消息处理
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg in (WM_PAINT, WM_TIMER):
            self.render()
            return 0
        if msg == WM_LBUTTONDOWN:
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            x0, y0, x1, y1 = self.close_rect
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.close()
            else:
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
            return 0
        if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
            self.close()
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def close(self) -> None:
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)

    # --- 创建窗口
    def create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls = "BiliBootWindow"

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_DROPSHADOW
        wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        wc.lpszClassName = cls
        user32.RegisterClassExW(ctypes.byref(wc))

        x = (user32.GetSystemMetrics(0) - self.w) // 2
        y = (user32.GetSystemMetrics(1) - self.h) // 3
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            cls, "正在准备运行环境", WS_POPUP, x, y, self.w, self.h,
            None, None, hinst, None,
        )
        if not self.hwnd:
            raise RuntimeError("无法创建安装窗口")

        ico = BASE_DIR / "assets" / "app.ico"
        if ico.exists():
            hicon = user32.LoadImageW(None, str(ico), 1, 32, 32, 0x0010)
            if hicon:
                user32.SendMessageW(self.hwnd, WM_SETICON, 1, hicon)
                user32.SendMessageW(self.hwnd, WM_SETICON, 0, hicon)

        # 32 位 DIB，直接写像素
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.w
        bmi.bmiHeader.biHeight = -self.h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        self.dib = gdi32.CreateDIBSection(
            None, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0
        )
        self.mem_dc = gdi32.CreateCompatibleDC(None)
        gdi32.SelectObject(self.mem_dc, self.dib)

        self._build_background()
        user32.ShowWindow(self.hwnd, 5)
        user32.SetTimer(self.hwnd, 1, 100, None)

    # --- 预渲染背景（渐变 + 抗锯齿圆角 + 细边），只做一次
    def _build_background(self) -> None:
        """预渲染卡片背景：竖向渐变 + 抗锯齿圆角（纯 Python 生成，只做一次）。"""
        w, h = self.w, self.h
        r = int(RADIUS * self.scale)
        top, bottom = C_BG_TOP, C_BG_BOTTOM

        buf = bytearray(w * h * 4)
        for y in range(h):
            t = y / max(1, h - 1)
            rr = int(top[0] + (bottom[0] - top[0]) * t)
            gg = int(top[1] + (bottom[1] - top[1]) * t)
            bb = int(top[2] + (bottom[2] - top[2]) * t)
            # BGRA，alpha 先给满
            buf[y * w * 4:(y + 1) * w * 4] = bytes((bb, gg, rr, 255)) * w

        # 四角按覆盖率预乘 alpha，得到平滑圆角
        for y in range(r):
            for x in range(r):
                cov = corner_alpha(x, y, r)
                if cov >= 0.998:
                    continue
                for cx, cy in ((x, y), (w - 1 - x, y), (x, h - 1 - y), (w - 1 - x, h - 1 - y)):
                    i = (cy * w + cx) * 4
                    buf[i] = int(buf[i] * cov)
                    buf[i + 1] = int(buf[i + 1] * cov)
                    buf[i + 2] = int(buf[i + 2] * cov)
                    buf[i + 3] = int(255 * cov)

        self.bg = bytes(buf)

    # --- 每帧绘制
    def render(self) -> None:
        if not self.mem_dc or not self.hwnd:
            return
        w, h, s = self.w, self.h, self.scale

        ctypes.memmove(self.bits, self.bg, len(self.bg))
        state = get_state()

        # 进度槽
        tx, ty = int(PAD * s), int(104 * s)
        tw = w - tx * 2
        th = max(6, int(7 * s))

        def bar(x0, y0, x1, y1, color, radius_px):
            brush = gdi32.CreateSolidBrush(color)
            pen = gdi32.CreatePen(0, 1, color)
            old_b = gdi32.SelectObject(self.mem_dc, brush)
            old_p = gdi32.SelectObject(self.mem_dc, pen)
            gdi32.RoundRect(self.mem_dc, x0, y0, x1, y1, radius_px, radius_px)
            gdi32.SelectObject(self.mem_dc, old_p)
            gdi32.SelectObject(self.mem_dc, old_b)
            gdi32.DeleteObject(pen)
            gdi32.DeleteObject(brush)

        bar(tx, ty, tx + tw, ty + th, C_TRACK, th)
        pct = max(0.0, min(100.0, state["percent"]))
        if pct > 0.2:
            fw = max(th + 2, int(tw * pct / 100))
            bar(tx, ty, tx + min(tw, fw), ty + th, C_PINK, th)

        # 文本
        gdi32.SetBkMode(self.mem_dc, TRANSPARENT)

        def text(value: str, x: int, y: int, px: float, color: int, weight: int = 400,
                 width: int | None = None, right: bool = False) -> None:
            font = gdi32.CreateFontW(
                -int(px * s), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI"
            )
            old = gdi32.SelectObject(self.mem_dc, font)
            gdi32.SetTextColor(self.mem_dc, color)
            rect = wintypes.RECT(x, y, x + (width if width else (w - x - int(PAD * s))), y + int(px * s * 1.9))
            user32.DrawTextW(
                self.mem_dc, value, -1, ctypes.byref(rect),
                (DT_RIGHT if right else DT_LEFT) | DT_TOP | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS,
            )
            gdi32.SelectObject(self.mem_dc, old)
            gdi32.DeleteObject(font)

        text("正在准备运行环境", int(PAD * s), int(28 * s), 15.5, C_TITLE, 600)
        text("首次启动需要下载依赖组件，请稍候", int(PAD * s), int(57 * s), 11.5, C_SUB)

        if state["error"]:
            text(state["error"][:60], int(PAD * s), int(122 * s), 11.5, C_ERR,
                 width=int((CARD_W - PAD * 2 - 70) * s))
        else:
            text(state["status"][:40], int(PAD * s), int(122 * s), 11.5, C_STATUS,
                 width=int((CARD_W - PAD * 2 - 70) * s))

        text(f"{int(pct)}%", int((CARD_W - PAD - 58) * s), int(115 * s), 19, C_PINK, 700,
             width=int(58 * s), right=True)
        text(state["footer"], int(PAD * s), int(181 * s), 10.5, C_FOOTER)

        # 右上角关闭按钮
        cx, cy, half = int((CARD_W - 30) * s), int(30 * s), int(5 * s)
        pen = gdi32.CreatePen(0, max(1, int(1.6 * s)), C_CLOSE)
        old = gdi32.SelectObject(self.mem_dc, pen)
        gdi32.MoveToEx(self.mem_dc, cx - half, cy - half, None)
        gdi32.LineTo(self.mem_dc, cx + half, cy + half)
        gdi32.MoveToEx(self.mem_dc, cx + half, cy - half, None)
        gdi32.LineTo(self.mem_dc, cx - half, cy + half)
        gdi32.SelectObject(self.mem_dc, old)
        gdi32.DeleteObject(pen)
        self.close_rect = (cx - int(14 * s), cy - int(14 * s), cx + int(14 * s), cy + int(14 * s))

        # GDI 绘制会清零 alpha，把内容区域补回不透明（先 flush 再读像素）
        gdi32.GdiFlush()
        self._restore_alpha()

        pt_dst = wintypes.POINT(int((user32.GetSystemMetrics(0) - w) // 2), int((user32.GetSystemMetrics(1) - h) // 3))
        size = wintypes.SIZE(w, h)
        pt_src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(pt_dst), ctypes.byref(size),
            self.mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA,
        )

    def _restore_alpha(self) -> None:
        """只修复内容区域（避开四角，保留圆角抗锯齿）。"""
        w, h = self.w, self.h
        r = int(RADIUS * self.scale)
        size = w * h * 4
        buf = bytearray(ctypes.string_at(self.bits, size))

        # 中间整行直接整行置 255（按字节切片，速度是 C 级）
        for y in range(r, h - r):
            base = y * w * 4
            buf[base + 3: base + w * 4: 4] = b"\xff" * w
        # 上下两个圆角带：只处理非圆角列
        for y in list(range(r)) + list(range(h - r, h)):
            base = y * w * 4
            for x in range(r, w - r):
                buf[base + x * 4 + 3] = 255

        ctypes.memmove(self.bits, bytes(buf), size)

    def run(self) -> None:
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------- 依赖安装

def deps_ready() -> bool:
    for name in REQUIRED:
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except Exception:
            return False
    return True


def requirement_count() -> int:
    try:
        lines = [
            ln.strip()
            for ln in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        return max(1, len(lines))
    except Exception:
        return 6


def install_deps() -> None:
    total = requirement_count()
    set_state(percent=3, status="正在连接软件源…")

    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), "--disable-pip-version-check"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=0x08000000,  # CREATE_NO_WINDOW，绝不弹控制台
        cwd=str(BASE_DIR),
    )

    seen = 0
    installing = False
    last_error = ""
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        low = line.lower()

        if low.startswith("collecting "):
            seen += 1
            pkg = line.split(" ", 1)[1].split(" from ")[0].strip()
            # 渐近曲线：先快后慢，不会卡在 99%
            pct = 8 + 72 * (1 - math.exp(-seen / (total * 1.6)))
            set_state(percent=pct, status=f"正在下载 {pkg}")
        elif low.startswith("downloading "):
            set_state(status=f"正在下载 {line.split()[1][:30]}")
        elif low.startswith("installing collected packages"):
            installing = True
            set_state(percent=max(84.0, get_state()["percent"]), status="正在安装组件…")
        elif low.startswith("successfully installed"):
            set_state(percent=98, status="安装完成，正在校验…")
        elif low.startswith("error") or "no matching distribution" in low:
            last_error = line[:70]

        if installing:
            cur = get_state()["percent"]
            if cur < 96:
                set_state(percent=min(96.0, cur + 0.6))

    code = proc.wait()
    if code != 0 or not deps_ready():
        set_state(
            percent=0,
            error=last_error or "依赖安装失败，请检查网络后重新运行",
            status="",
            footer="按 Esc 或点右上角关闭，然后重新运行",
        )
        return

    set_state(percent=100, status="准备完成，正在启动…", finished=True, footer="马上就好")


def launch_app() -> None:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            exe = candidate
    subprocess.Popen(
        [exe, str(APP_ENTRY)],
        cwd=str(BASE_DIR),
        creationflags=0x08000000 | 0x00000008,
        close_fds=True,
    )


def preview_progress() -> None:
    """--preview：走一遍假进度，用来检查界面。"""
    demo = [
        (4, "正在连接软件源…"), (18, "正在下载 fastapi"), (34, "正在下载 uvicorn"),
        (52, "正在下载 pydantic"), (70, "正在下载 imageio-ffmpeg"),
        (86, "正在安装组件…"), (96, "安装完成，正在校验…"), (100, "准备完成，正在启动…"),
    ]
    for pct, status in demo:
        time.sleep(0.75)
        set_state(percent=pct, status=status)
    time.sleep(3)


def main() -> None:
    preview = "--preview" in sys.argv

    if not preview and deps_ready():
        launch_app()
        return

    win = BootWindow()
    win.create()

    def worker() -> None:
        try:
            if preview:
                preview_progress()
                set_state(finished=True)
            else:
                install_deps()
        except Exception as exc:  # noqa: BLE001
            set_state(error=f"安装出错：{exc}"[:70], status="", percent=0)

    threading.Thread(target=worker, daemon=True).start()

    def watcher() -> None:
        while True:
            time.sleep(0.3)
            if get_state()["finished"]:
                if not preview:
                    time.sleep(0.4)
                    launch_app()
                win.close()
                return

    threading.Thread(target=watcher, daemon=True).start()
    win.run()


if __name__ == "__main__":
    main()
