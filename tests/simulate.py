"""端到端离线测试：不碰真 OBS，用假后端验证「检测 → 开录 → 停录」全链路。

跑法： python tests/simulate.py
它会开一个假的「galgame 窗口」，把台词一句句往外吐，中间夹长静默，
观察检测器是否在正确的时机开录/停录。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from galrec import capture, windows  # noqa: E402
from galrec import backend as backendmod  # noqa: E402
from galrec.config import Config  # noqa: E402
from galrec.detector import Detector, Phase  # noqa: E402

FONT_PATHS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def _font(size: int):
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


class FakeGame:
    """一个真窗口，画面由我们自己画，用来喂检测器。"""

    LINES = [
        "早上好，今天也是好天气呢。",
        "你这家伙，真是让人伤脑筋啊。",
        "要不我们一起去天台看看吧？",
        "……其实我一直在等你这句话。",
        "那么，走吧。",
    ]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("假Galgame - 模拟测试窗口")
        self.root.geometry("960x600+100+100")
        self.canvas = tk.Canvas(self.root, bg="#101828", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.line_idx = 0
        self.char_idx = 0
        self.talking = True
        self._closure = None
        self._tick()

    def _tick(self):
        c = self.canvas
        W = max(1, c.winfo_width())
        H = max(1, c.winfo_height())
        c.delete("all")
        # 背景 + 立绘方块（让画面有变化但不动）
        c.create_rectangle(0, 0, W, H, fill="#101828", outline="")
        c.create_oval(W * 0.35, H * 0.05, W * 0.65, H * 0.7,
                      fill="#2b3a55", outline="")
        # 对话框
        bx0, by0 = W * 0.06, H * 0.66
        bx1, by1 = W * 0.94, H * 0.95
        c.create_rectangle(bx0, by0, bx1, by1, fill="#0b1220", outline="#3d5470")
        if self.talking:
            full = self.LINES[self.line_idx % len(self.LINES)]
            shown = full[: self.char_idx]
            c.create_text(bx0 + 24, by0 + 26, anchor="nw", text=shown,
                          fill="#e5e7eb", font=("Microsoft YaHei UI", 20),
                          width=int(bx1 - bx0 - 48))
        self.root.after(90, self._tick)

    def start_script(self, on_log):
        """编排：说话 → 停顿 → 继续 …最后长静默，再插一段「被遮挡」。

        被遮挡这一段是专门用来验证：当浏览器盖在游戏上时，OCR 会读到浏览器的
        文字，程序必须识别出「看不见」并冻结计时，而不是把它当成剧情还在走。
        """
        def run():
            for i in range(len(self.LINES)):
                self.talking = True
                self.char_idx = 0
                self.line_idx = i
                full = self.LINES[i]
                for k in range(1, len(full) + 1):
                    self.char_idx = k
                    time.sleep(0.13)
                on_log(f"[假游戏] 说完第 {i+1} 句")
                time.sleep(2.0)   # 玩家读完按下一句的间隔
            on_log("[假游戏] 剧情结束，进入长时间静默")
            self.talking = False
            self.char_idx = 0
        threading.Thread(target=run, daemon=True).start()

    def cover_with(self, minutes: int = 40):
        """用一个看起来像浏览器的窗口盖住游戏窗口。"""
        top = tk.Toplevel(self.root)
        top.title("假浏览器 - 用来干扰检测")
        top.geometry("1000x700+80+80")
        top.attributes("-topmost", True)
        txt = tk.Text(top, font=("Consolas", 10), bg="#ffffff", fg="#111111")
        txt.pack(fill="both", expand=True)
        lines = [
            "Galgame剧情 运行命令 · Import-test new modules 13分钟",
            "写入 galrec/recorder.py 17KB 14分钟",
            "思考 Now the client side ... import os, sys, time",
            "def main():  print('hello')  https://example.com/a/b/c",
        ]
        for i in range(minutes):
            txt.insert("end", "\n".join(lines) + "\n")
        txt.configure(state="disabled")
        self.cover = top
        return top

    def uncover(self):
        try:
            self.cover.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


class FakeBackend(backendmod.BaseBackend):
    name = "fake"

    def __init__(self):
        self._rec = False
        self._t0 = 0.0
        self.started = 0

    @property
    def connected(self):
        return True

    @property
    def label(self):
        return "模拟后端"

    def connect(self):
        return True

    def disconnect(self):
        pass

    def start(self):
        self._rec = True
        self._t0 = time.time()
        self.started += 1

    def stop(self):
        self._rec = False
        return ""

    def recording(self):
        return self._rec

    def duration(self):
        return time.time() - self._t0 if self._rec else 0.0


def main():
    windows.enable_dpi_awareness()
    logs = []
    t0 = time.time()

    def log(msg, level="info"):
        line = f"[{time.time()-t0:6.1f}s] {msg}"
        logs.append(line)
        print(line, flush=True)

    game = FakeGame()
    game.root.update()
    time.sleep(0.8)
    game.root.update()

    hwnd = windows.find_window(title_sub="假Galgame")
    if not hwnd:
        print("!! 没找到假窗口")
        return 1
    log(f"假窗口 hwnd={hwnd.hwnd} rect={hwnd.rect}", "info")

    cfg = Config()
    cfg.detect.window_title = "假Galgame"
    cfg.detect.idle_timeout = 12.0      # 测试用，缩短
    cfg.detect.start_grace = 4.0
    cfg.detect.poll_interval = 0.45
    cfg.detect.ocr = True
    cfg.detect.ocr_interval = 0.8
    cfg.detect.change_threshold = 2.0
    cfg.detect.require_dialogue = True
    cfg.detect.auto_region = True
    cfg.detect.detect_occlusion = True
    cfg.record.start_mode = "on_dialogue"
    cfg.obs.stop_delay = 0.5

    backend = FakeBackend()

    from galrec.recorder import Controller

    ctrl = Controller(cfg, log=log)
    ctrl.backend = backend
    orig_start = ctrl._start_record
    orig_stop = ctrl._stop_record

    def wrap_start(payload):
        log(f"===> 开录触发（{payload}）", "ok")
        orig_start(payload)

    def wrap_stop(reason):
        log(f"<=== 停录触发（{reason}）", "ok")
        orig_stop(reason)

    ctrl._start_record = wrap_start
    ctrl._stop_record = wrap_stop
    ctrl.detector._on_event = ctrl._on_detect_event

    ctrl.detector.start(hwnd=hwnd.hwnd)
    game.root.after(600, lambda: game.start_script(lambda m: log(m)))

    # 25 秒后盖上一个「浏览器」，模拟用户切去干别的
    def cover():
        log("[干扰] 用假浏览器盖住游戏窗口", "warn")
        game.cover_with(60)

    def uncover():
        log("[干扰] 移开假浏览器", "warn")
        game.uncover()

    game.root.after(25000, cover)
    game.root.after(45000, uncover)

    def watch():
        last_phase = None
        for _ in range(170):
            time.sleep(0.5)
            s = ctrl.detector.snapshot
            if s.phase != last_phase:
                log(f"    phase -> {s.phase}  (diff={s.diff:.2f} idle={s.idle:.0f}s) {s.note}", "info")
                last_phase = s.phase
    threading.Thread(target=watch, daemon=True).start()

    def finish():
        log("---- 测试结束 ----")
        d = ctrl.detector
        print("\n统计：")
        print(f"  开录次数      : {backend.started}")
        print(f"  完成段数      : {len(ctrl.sessions)}")
        for s in ctrl.sessions:
            print(f"    #{s.index} {s.duration:.0f}s  {s.reason}")
        print(f"  自动识别区域  : {d._auto_rel}")
        ok = backend.started >= 1 and len(ctrl.sessions) >= 1
        print(f"  结果          : {'通过 ✔' if ok else '失败 ✘'}")
        ctrl.detector.stop()
        game.root.quit()

    game.root.after(88000, finish)
    game.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
