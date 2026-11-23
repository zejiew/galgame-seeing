"""Win32 窗口工具：枚举、定位、前台切换、DPI。

坐标系说明：全部对外返回「物理像素」屏幕坐标。程序启动时把进程设为
Per-Monitor DPI Aware，否则在 125%/150% 缩放下 GetWindowRect 与截屏会对不上。
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import List, Optional

try:
    import win32api
    import win32con
    import win32gui
    import win32process
    _HAS_WIN32 = True
except Exception:  # pragma: no cover - 非 Windows 环境
    _HAS_WIN32 = False


def enable_dpi_awareness() -> None:
    """把当前进程标记为 DPI Aware，必须在建窗口/截图之前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def make_dpi_aware_deep() -> None:
    """更彻底：连子进程（OBS、游戏）也按每显示器 DPI 处理。

    SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2) 对当前进程生效；
    对已经启动的其他进程无效，所以只在必要时用。
    """
    try:
        ctx = ctypes.c_void_p(-4)  # PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 窗口信息
# --------------------------------------------------------------------------- #
@dataclass
class WindowInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    process: str
    rect: tuple          # (l, t, r, b) 物理像素
    visible: bool

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def size(self) -> tuple:
        return (self.width, self.height)

    @property
    def bbox(self) -> tuple:
        """(left, top, right, bottom)，直接喂给 ImageGrab.grab。"""
        return self.rect

    def label(self) -> str:
        p = f" [{self.process}]" if self.process else ""
        return f"{self.title}{p}  {self.width}x{self.height}"


# 这些窗口没有录制价值，直接过滤
_SKIP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow",
    "ApplicationFrameWindow", "DV2ControlHost", "MsgrIMEWindowClass",
    "SysShadow", "Button", "IME", "Chrome_ImF",
}
_SKIP_TITLES = {"", "Program Manager", "默认 IME 窗口", "Microsoft Text Input Application"}


def _proc_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return ""


def list_windows(only_visible: bool = True, min_size: int = 200) -> List[WindowInfo]:
    """枚举候选窗口（已过滤系统窗口），按面积从大到小排。"""
    if not _HAS_WIN32:
        return []
    out: List[WindowInfo] = []

    def _cb(hwnd, _):
        try:
            if only_visible and not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            if title in _SKIP_TITLES or cls in _SKIP_CLASSES:
                return True
            if not win32gui.IsWindowEnabled(hwnd):
                return True
            # 无标题栏的工具窗口通常是悬浮层，不要
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            if not (style & win32con.WS_CAPTION) and not (style & win32con.WS_THICKFRAME):
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] < min_size or rect[3] - rect[1] < min_size:
                return True
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            out.append(WindowInfo(
                hwnd=hwnd, title=title, cls=cls, pid=pid,
                process=_proc_name(pid), rect=tuple(rect), visible=True,
            ))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    out.sort(key=lambda w: w.width * w.height, reverse=True)
    return out


def find_window(title_sub: str = "", process: str = "", hwnd: int = 0) -> Optional[WindowInfo]:
    """按 hwnd > 进程名 > 标题子串 的顺序找窗口。"""
    wins = list_windows()
    if hwnd:
        for w in wins:
            if w.hwnd == hwnd:
                return w
        return None
    if process:
        p = process.lower()
        cands = [w for w in wins if w.process.lower() == p]
        if not cands:
            cands = [w for w in wins if p in w.process.lower()]
        if cands:
            return cands[0]
    if title_sub:
        t = title_sub.lower()
        cands = [w for w in wins if t in w.title.lower()]
        if cands:
            return cands[0]
    return None


def get_foreground() -> Optional[WindowInfo]:
    if not _HAS_WIN32:
        return None
    hwnd = win32gui.GetForegroundWindow()
    for w in list_windows():
        if w.hwnd == hwnd:
            return w
    return None


def client_rect(hwnd: int) -> tuple:
    """窗口客户区在屏幕上的物理像素矩形 (l, t, r, b)。"""
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (l, t))
        ex, ey = win32gui.ClientToScreen(hwnd, (r, b))
        return (sx, sy, ex, ey)
    except Exception:
        return tuple(win32gui.GetWindowRect(hwnd))


def bring_to_front(hwnd: int) -> bool:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        # SetForegroundWindow 会被前台锁挡住，用 AttachThreadInput 绕一下
        try:
            fg = win32gui.GetForegroundWindow()
            cur = win32api.GetCurrentThreadId()
            tgt = win32process.GetWindowThreadProcessId(fg)[0]
            win32process.AttachThreadInput(cur, tgt, True)
            win32gui.SetForegroundWindow(hwnd)
            win32process.AttachThreadInput(cur, tgt, False)
            return True
        except Exception:
            return False


def is_alive(hwnd: int) -> bool:
    try:
        return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def screen_size() -> tuple:
    if not _HAS_WIN32:
        return (1920, 1080)
    return (win32gui.GetSystemMetrics(0), win32gui.GetSystemMetrics(1))


def root_of(hwnd: int) -> int:
    """取窗口根（顶层）窗口。"""
    try:
        r = win32gui.GetAncestor(hwnd, 2)  # GA_ROOT
        return r or hwnd
    except Exception:
        return hwnd


def is_region_on_top(hwnd: int, bbox: tuple, samples: int = 5) -> bool:
    """检查这个屏幕区域此刻是否真的属于 hwnd（没被别的窗口盖住）。

    截图抓的是屏幕像素，窗口一旦被挡住，抓到的就是盖在它上面的另一个窗口。
    对 galgame 来说这很致命：挡住它的若是浏览器/聊天窗口，OCR 会不断读到
    外面的文字，程序会误以为剧情一直在推进，永远不停录。

    做法：在区域内取几个点，用 WindowFromPoint 看每个点最上层是哪个窗口的根，
    只要多数点属于 hwnd 就算没被遮挡。
    """
    if not _HAS_WIN32:
        return True
    try:
        target = root_of(hwnd)
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            return False
        pts = [
            ((x0 + x1) // 2, (y0 + y1) // 2),
            (x0 + (x1 - x0) // 4, y0 + (y1 - y0) // 4),
            (x0 + (x1 - x0) * 3 // 4, y0 + (y1 - y0) // 4),
            (x0 + (x1 - x0) // 4, y0 + (y1 - y0) * 3 // 4),
            (x0 + (x1 - x0) * 3 // 4, y0 + (y1 - y0) * 3 // 4),
        ][:max(1, samples)]
        hit = 0
        for px, py in pts:
            try:
                h = win32gui.WindowFromPoint((px, py))
            except Exception:
                continue
            if h and root_of(h) == target:
                hit += 1
        return hit >= max(1, len(pts) // 2 + 1)
    except Exception:
        return True


def launch_exe(exe_path: str, workdir: str = "") -> bool:
    """后台拉起一个程序（如 OBS），不抢占焦点。"""
    import subprocess
    if not exe_path or not os.path.isfile(exe_path):
        return False
    wd = workdir or os.path.dirname(exe_path)
    try:
        subprocess.Popen(
            [exe_path], cwd=wd,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True
    except Exception:
        return False
