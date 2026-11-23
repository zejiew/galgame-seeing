"""全屏框选：在桌面上拉一个矩形，返回物理像素坐标。

tkinter 的 Toplevel 可以做半透明全屏遮罩。多显示器时用虚拟桌面尺寸覆盖全部，
坐标统一转成物理像素（与 ImageGrab / GetWindowRect 对齐）。
"""
from __future__ import annotations

import tkinter as tk
from typing import Optional, Tuple


class RegionSelector:
    def __init__(self, master: tk.Misc, hint: str = "拖动鼠标框选对话框区域，Esc 取消"):
        self.master = master
        self.hint = hint
        self.result: Optional[Tuple[int, int, int, int]] = None
        self._x0 = self._y0 = 0
        self._rect = None
        self._dim = None

    def select(self) -> Optional[Tuple[int, int, int, int]]:
        # 虚拟桌面（多屏）
        top = tk.Toplevel(self.master)
        top.attributes("-topmost", True)
        top.overrideredirect(True)
        vx = top.winfo_vrootx()
        vy = top.winfo_vrooty()
        vw = top.winfo_vrootwidth()
        vh = top.winfo_vrootheight()
        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        top.configure(bg="black")
        try:
            top.attributes("-alpha", 0.28)
        except Exception:
            pass
        top.focus_force()
        top.grab_set()

        canvas = tk.Canvas(top, bg="black", highlightthickness=0,
                           width=vw, height=vh, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            24, 24, anchor="nw", fill="#7ef", font=("Microsoft YaHei UI", 14),
            text=self.hint + "\n（框选后自动使用；点击一次也可取一个点附近区域）",
        )

        state = {"start": None, "rect": None}

        def on_press(e):
            state["start"] = (e.x_root, e.y_root)
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x_root - vx, e.y_root - vy, e.x_root - vx, e.y_root - vy,
                outline="#39ff88", width=2, fill="#39ff88", stipple="gray12",
            )

        def on_drag(e):
            if not state["start"] or not state["rect"]:
                return
            x0, y0 = state["start"]
            canvas.coords(
                state["rect"],
                x0 - vx, y0 - vy, e.x_root - vx, e.y_root - vy,
            )

        def on_release(e):
            if not state["start"]:
                top.destroy()
                return
            x0, y0 = state["start"]
            x1, y1 = e.x_root, e.y_root
            left, right = sorted((x0, x1))
            top_, bottom = sorted((y0, y1))
            if right - left < 20 or bottom - top_ < 10:
                # 单击：以点击点为中心取 600x120
                left = max(vx, x0 - 300)
                top_ = max(vy, y0 - 60)
                right, bottom = left + 600, top_ + 120
            self.result = (int(left), int(top_), int(right), int(bottom))
            top.destroy()

        def on_key(e):
            if e.keysym == "Escape":
                top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_key)
        self.master.wait_window(top)
        return self.result


class RegionMask(tk.Toplevel):
    """在屏幕上把已选区域描一圈，方便确认选得对不对（2 秒自动消失）。"""

    def __init__(self, master: tk.Misc, bbox: Tuple[int, int, int, int]):
        super().__init__(master)
        x0, y0, x1, y1 = bbox
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{max(1, x1 - x0)}x{max(1, y1 - y0)}+{x0}+{y0}")
        try:
            self.attributes("-alpha", 0.25)
        except Exception:
            pass
        self.configure(bg="#39ff88")
        self.after(1600, self.destroy)
