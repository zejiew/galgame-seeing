"""向系统发送按键（SendInput），用于驱动 OBS 的全局输出热键。

这条路用于「OBS 没有 WebSocket 插件」的情况：OBS 的「开始/停止录制」热键是
全局热键，注册后无论焦点在哪都能触发，所以模拟按键即可。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from typing import List, Optional

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): 0x41 + (c - 65) for c in range(65, 91)},
    **{str(d): 0x30 + d for d in range(10)},
}
VK_EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E}


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def parse_accel(accel: str) -> Optional[List[int]]:
    """把 "ctrl+alt+f9" 解析成虚拟键码列表；无法解析返回 None。"""
    if not accel:
        return None
    parts = [p.strip().lower() for p in str(accel).replace(" ", "").split("+") if p.strip()]
    out: List[int] = []
    for p in parts:
        vk = VK.get(p)
        if vk is None:
            # 允许直接写十六进制键码，如 0x77
            try:
                vk = int(p, 16) if p.startswith("0x") else int(p)
            except Exception:
                return None
            if not (0 < vk < 256):
                return None
        out.append(vk)
    return out or None


def _send(vk: int, up: bool) -> None:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in VK_EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                         dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def send_hotkey(accel: str, hold: float = 0.05) -> bool:
    """按下并释放一组组合键。"""
    keys = parse_accel(accel)
    if not keys:
        return False
    try:
        for vk in keys:
            _send(vk, up=False)
            time.sleep(0.01)
        time.sleep(max(0.01, hold))
        for vk in reversed(keys):
            _send(vk, up=True)
            time.sleep(0.01)
        return True
    except Exception:
        return False


def list_obs_hotkeys_hint() -> str:
    return (
        "在 OBS 里打开 设置 → 热键，给这两项各设一个组合键：\n"
        "  · 开始录制\n"
        "  · 停止录制\n"
        "然后把同样的组合键填到本程序的设置里（例如 ctrl+alt+f9 / ctrl+alt+f10）。\n"
        "OBS 的热键是全局的，程序在后台发按键就能生效。"
    )
