"""自动推进剧情（可选功能）。

很多 galgame 有「自动播放」按钮，但不少作品没有，或者需要手动点掉很长的
共通线。这个模块负责在「剧情已经停住、等玩家点下一步」的时候替你按一下。

三条安全规则（重要，否则会帮倒忙）：

1. **只在游戏窗口在前台时才发送**。按键是发给前台窗口的，如果此刻前台是
   浏览器或聊天窗，一按就会干别的事。所以发送前必须核对前台窗口。
2. **遇到选项就停手**。选项是玩家该拿主意的地方，替你点会毁掉存档。
   两种方式识别：画面中部出现 2~4 行短文字（选项样式），或者连点多次文字
   一直不变（卡在选项上）。
3. **随时可关**。开关关掉后只做检测和录制，一下都不点。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

user32 = ctypes.windll.user32


# --------------------------------------------------------------------------- #
# 输入注入
# --------------------------------------------------------------------------- #
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


# 常用推进键
ADVANCE_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D,
    "ctrl": 0x11, "z": 0x5A, "x": 0x58, "esc": 0x1B,
}


def send_key(vk: int, hold: float = 0.04) -> bool:
    """按下并释放一个键。"""
    try:
        for up in (False, True):
            inp = _INPUT(type=INPUT_KEYBOARD)
            inp.ki = _KEYBDINPUT(wVk=vk, wScan=0,
                                 dwFlags=KEYEVENTF_KEYUP if up else 0,
                                 time=0,
                                 dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
            if not up:
                time.sleep(max(0.01, hold))
        return True
    except Exception:
        return False


def click_at(x: int, y: int) -> bool:
    """在屏幕坐标点一下左键。"""
    try:
        old = wt.POINT()
        user32.GetCursorPos(ctypes.byref(old))
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
            inp = _INPUT(type=INPUT_MOUSE)
            inp.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag,
                                 time=0,
                                 dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
            time.sleep(0.02)
        # 把鼠标放回去，别把用户的光标弄丢
        try:
            user32.SetCursorPos(old.x, old.y)
        except Exception:
            pass
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 选项识别
# --------------------------------------------------------------------------- #
def looks_like_choice_screen(lines, img_size: Tuple[int, int],
                            dialogue_bbox: Optional[tuple] = None,
                            ) -> Tuple[bool, list]:
    """判断画面是不是「选项界面」。

    galgame 的选项通常：2~4 行、每行都短、竖直排成一列、位于画面中部，
    而且**不在**对话框区域里。台词则相反：1~3 行、在下方对话框内。

    返回 (是否像选项, 候选文字列表)。
    """
    W, H = img_size
    if not lines:
        return False, []
    cand = []
    for l in lines:
        text = (l.text or "").strip()
        if len(text) < 1 or len(text) > 40:
            continue
        cy = l.cy / max(1, H)
        # 选项一般在画面 15%~75% 高度
        if not (0.12 <= cy <= 0.78):
            continue
        # 如果在对话框区域内，那是台词不是选项
        if dialogue_bbox:
            x0, y0, x1, y1 = dialogue_bbox
            if (l.box[0] >= x0 - 10 and l.box[2] <= x1 + 10
                    and l.box[1] >= y0 - 10 and l.box[3] <= y1 + 10):
                continue
        cand.append(l)

    if len(cand) < 2:
        return False, []
    # 竖直排布：相邻两行中心距相近
    cand.sort(key=lambda l: l.cy)
    gaps = [cand[i + 1].cy - cand[i].cy for i in range(len(cand) - 1)]
    if gaps:
        avg = sum(gaps) / len(gaps)
        if avg <= 0 or avg > H * 0.2:
            return False, []
        if max(gaps) > avg * 2.2:
            return False, []
    # 行高要接近（选项框里的字大小一致）
    hs = [l.height for l in cand]
    if hs and max(hs) > min(hs) * 2.4:
        return False, []
    return True, [l.text for l in cand]


# --------------------------------------------------------------------------- #
# 推进器
# --------------------------------------------------------------------------- #
@dataclass
class AdvanceState:
    enabled: bool = False
    paused: bool = False
    pause_reason: str = ""
    last_action: float = 0.0
    actions: int = 0


class AutoAdvancer:
    """在检测器线程之外独立跑，避免按键卡住采样。"""

    def __init__(self, cfg, log: Optional[Callable[[str, str], None]] = None,
                 on_pause: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self._log = log or (lambda m, l="info": None)
        self._on_pause = on_pause or (lambda reason, detail: None)
        self.state = AdvanceState()
        self._lock = threading.RLock()
        self._last_text = ""
        self._same_text_count = 0
        self._choices_hits = 0

    # ---------------- 对外 ---------------- #
    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.state.enabled = bool(on)
            if on:
                self.state.paused = False
                self.state.pause_reason = ""
                self._same_text_count = 0
                self._choices_hits = 0

    def resume(self) -> None:
        with self._lock:
            self.state.paused = False
            self.state.pause_reason = ""
            self._same_text_count = 0
            self._choices_hits = 0
        self._log("自动推进已恢复", "ok")

    def pause(self, reason: str, detail: str = "") -> None:
        with self._lock:
            if not self.state.paused:
                self.state.paused = True
                self.state.pause_reason = reason
        self._log(f"自动推进暂停：{reason}" + (f"（{detail}）" if detail else ""), "warn")
        try:
            self._on_pause(reason, detail)
        except Exception:
            pass

    @property
    def active(self) -> bool:
        s = self.state
        return s.enabled and not s.paused

    # ---------------- 主判断 ---------------- #
    def maybe_advance(self, hwnd: int, text: str, text_stable: float,
                      choice_lines: Optional[list] = None,
                      dialogue_bbox: Optional[tuple] = None) -> bool:
        """如果该推进就发一次输入。返回是否真的按了。

        text_stable: 当前这句台词已经稳定多久（秒）。只有稳定够久才按，
                     否则打字机还在显示就按，会把整句跳掉。
        """
        with self._lock:
            if not self.state.enabled or self.state.paused:
                return False

        d = self.cfg.advance

        # 1) 选项优先：发现选项就立刻停手
        if d.stop_on_choice and choice_lines:
            self._choices_hits += 1
            if self._choices_hits >= d.choice_confirm:
                self.pause("检测到选项",
                           "、".join(str(x)[:14] for x in choice_lines[:4]))
                return False
        else:
            self._choices_hits = 0

        # 2) 台词得稳定够久（说明已经播完，在等玩家）
        if text and text_stable < d.delay_after_text:
            return False
        if not text and text_stable < d.delay_after_text * 1.5:
            return False

        # 3) 同上一次相比有没有变——连点多次不变，说明卡住了
        norm = (text or "").strip()
        if norm and norm == self._last_text:
            self._same_text_count += 1
            if d.stop_when_stuck and self._same_text_count >= d.stuck_limit:
                self.pause("疑似卡住",
                           f"连续 {self._same_text_count} 次推进后台词没变，"
                           f"可能是选项/小游戏/等待输入")
                return False
        else:
            self._same_text_count = 0
            self._last_text = norm

        # 4) 前台必须还是游戏窗口
        if not self._is_foreground(hwnd):
            return False

        # 5) 发送
        ok = self._emit(hwnd)
        if ok:
            with self._lock:
                self.state.actions += 1
                self.state.last_action = time.time()
        return ok

    def _is_foreground(self, hwnd: int) -> bool:
        """前台窗口必须是游戏（或其子窗口），否则绝不发送。"""
        if not hwnd:
            return False
        try:
            fg = user32.GetForegroundWindow()
            if not fg:
                return False
            if fg == hwnd:
                return True
            # 有些游戏的主窗口是子窗口，比较根窗口
            return user32.GetAncestor(fg, 2) == user32.GetAncestor(hwnd, 2)
        except Exception:
            return False

    def _emit(self, hwnd: int) -> bool:
        d = self.cfg.advance
        mode = (d.mode or "key").lower()
        sent = False
        if mode in ("key", "both"):
            vk = ADVANCE_KEYS.get((d.key or "space").lower(), 0x20)
            sent = send_key(vk) or sent
        if mode in ("mouse", "both") and d.click_pos:
            try:
                x, y = d.click_pos
                sent = click_at(int(x), int(y)) or sent
            except Exception:
                pass
        if sent:
            self._log(f"自动推进（第 {self.state.actions + 1} 次）", "info")
        return sent
