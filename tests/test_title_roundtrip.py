"""回主页面往返测试（真窗口 + 真 OCR）。

    python tests/test_title_roundtrip.py

复现用户报的 bug 场景：

    剧情中 → 点「返回标题」→ 主菜单（光标在菜单项间移动）→ 点「开始游戏」
    → 进入新剧情

验证三件事：

1. 回主菜单后**必须停录**（不能等到 idle 超时，50 秒内就要停）；
2. 停在主菜单期间**不许再开录**（光标来回移 40 秒，一段都不许起）；
3. 点「开始游戏」进入新剧情后**必须能重新开录**（不能因为旧台词撞相似度卡死）。

用假窗口画菜单和对话框，喂真检测器；后端是假的，不碰真 OBS。
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

from galrec import backend as backendmod  # noqa: E402
from galrec import windows  # noqa: E402
from galrec.config import Config  # noqa: E402
from galrec.detector import Detector, Phase  # noqa: E402

RESULT = []


def check(name, ok, detail=""):
    RESULT.append((name, ok))
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


FONT_PATHS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def _font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


class FakeGame:
    """一个真 tk 窗口，画面内容我们说了算：剧情 / 主菜单 两种。"""

    MENU = ["开始游戏", "继续游戏", "载入存档", "画廊", "设置"]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("假Galgame回主页测试 - window")
        self.root.geometry("960x600+80+80")
        self.canvas = tk.Canvas(self.root, bg="#0b1220", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.mode = "story"          # story | menu
        self.line = "早上好，今天也是好天气呢。"
        self.menu_idx = 0            # 光标在哪个菜单项（模拟鼠标移动）
        self._tick()

    def _tick(self):
        c = self.canvas
        W = max(1, c.winfo_width())
        H = max(1, c.winfo_height())
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill="#0b1220", outline="")
        if self.mode == "story":
            c.create_rectangle(W * 0.06, H * 0.66, W * 0.94, H * 0.95,
                               fill="#16233a", outline="#3d5470")
            c.create_text(W * 0.09, H * 0.70, anchor="nw", text=self.line,
                          fill="#e5e7eb", font=("Microsoft YaHei UI", 20),
                          width=int(W * 0.82))
        else:
            # 主菜单：竖排菜单项，光标那一项高亮（文字颜色变，内容不变）
            f = ("Microsoft YaHei UI", 22)
            for i, t in enumerate(self.MENU):
                y = H * 0.28 + i * 62
                hot = (i == self.menu_idx)
                c.create_text(W * 0.5, y, text=t, font=f,
                              fill="#FFD166" if hot else "#9FB6C6")
        self.root.after(120, self._tick)

    def set_mode(self, m):
        self.mode = m

    def run(self):
        self.root.mainloop()


class FakeBackend(backendmod.BaseBackend):
    name = "fake"

    def __init__(self):
        self._rec = False
        self._t0 = 0.0
        self.starts = 0
        self.stops = 0

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
        self.starts += 1

    def stop(self):
        self._rec = False
        self.stops += 1
        return ""

    def recording(self):
        return self._rec

    def duration(self):
        return time.time() - self._t0 if self._rec else 0.0


def main():
    windows.enable_dpi_awareness()
    t0 = time.time()
    log_lines = []

    def log(msg, level="info"):
        line = f"[{time.time()-t0:6.1f}s] {msg}"
        log_lines.append(line)
        print(line, flush=True)

    game = FakeGame()
    game.root.update()
    time.sleep(0.8)
    game.root.update()

    hwnd = windows.find_window(title_sub="假Galgame回主页测试")
    if not hwnd:
        print("!! 没找到假窗口")
        return 1
    log(f"假窗口 hwnd={hwnd.hwnd} rect={hwnd.rect}")

    cfg = Config()
    cfg.detect.window_title = "假Galgame回主页测试"
    cfg.detect.idle_timeout = 120.0     # 关键：故意设大，证明不是靠它停的
    cfg.detect.start_grace = 3.0
    cfg.detect.poll_interval = 0.45
    cfg.detect.ocr_interval = 0.8
    cfg.detect.cooldown_seconds = 3.0
    cfg.detect.region_rel = None
    cfg.detect.auto_region = False      # 固定用默认下半屏，避免测试里探测抖动
    cfg.record.start_mode = "on_dialogue"
    cfg.record.auto_stop_on_title = True
    cfg.obs.stop_delay = 0.3
    cfg.obs.restart_gap = 1.0
    cfg.record.delete_short = False
    cfg.record.remux = False
    cfg.record.filename_template = ""

    be = FakeBackend()
    events = []

    from galrec.recorder import Controller
    ctrl = Controller(cfg, log=log)
    ctrl.backend = be
    # 串接事件记录（detector 是我们真正要观察的）
    orig_ev = ctrl._on_detect_event

    def spy(ev, payload):
        events.append((time.time() - t0, ev, payload))
        orig_ev(ev, payload)
    ctrl._on_detect_event = spy
    ctrl.detector._on_event = spy

    ctrl.detector.start(hwnd=hwnd.hwnd)

    phase_log = []

    def watch():
        last = None
        end = time.time() + 100
        while time.time() < end:
            p = ctrl.detector.phase.value
            if p != last:
                phase_log.append((time.time() - t0, p))
                log(f"    phase -> {p}")
                last = p
            time.sleep(0.2)
    threading.Thread(target=watch, daemon=True).start()

    # 时间线
    def to_menu():
        log(">>> 切到主菜单画面（模拟点「返回标题」）")
        game.set_mode("menu")

    def move_cursor():
        log(">>> 模拟鼠标在菜单项之间移动（每 1.5 秒换一个）")
        st = {"i": 0}

        def step():
            if not game.winfo_exists() if hasattr(game, "winfo_exists") else False:
                return
            if game.mode != "menu":
                return
            game.menu_idx = st["i"] % len(game.MENU)
            st["i"] += 1
            t = getattr(game, "root", None)
            if t is not None:
                try:
                    t.after(1500, step)
                except Exception:
                    pass
        game.root.after(1500, step)

    def back_to_story():
        log(">>> 点「开始游戏」，进入新剧情")
        game.set_mode("story")
        game.line = "早上好，今天也是好天气呢。"   # 故意用和上次同一句，测相似度陷阱

    game.root.after(9000, to_menu)
    game.root.after(11000, move_cursor)
    game.root.after(42000, back_to_story)

    def finish():
        log("---- 测试结束 ----")
        d = ctrl.detector
        starts = [e for e in events if e[1] == "segment_start"]
        stops = [e for e in events if e[1] == "segment_stop"]
        stop_reasons = [e[2].get("reason") for e in stops]
        print("\n统计：")
        print(f"  开录次数     : {be.starts}")
        print(f"  停录次数     : {be.stops}")
        print(f"  停录原因     : {stop_reasons}")
        print(f"  阶段变化     : {phase_log}")

        # 断 1：回主菜单后要停（reason 应为 title）
        title_stops = [s for s in stops if s[2].get("reason") == "title"]
        check("回主菜单 → 停录（reason=title）", len(title_stops) >= 1,
              f"reasons={stop_reasons}")

        # 断 2：主菜单期间不许开录。
        # 主菜单阶段 = 从 9s 到 42s；这期间任何 segment_start 都算 bug。
        menu_starts = [e for e in starts if 10.0 <= e[0] <= 41.0]
        check("停在主菜单 30 秒期间没有乱开录", len(menu_starts) == 0,
              f"多开出 {len(menu_starts)} 段：{[round(e[0],1) for e in menu_starts]}")

        # 断 3：进入新剧情后要能重新开录
        late_starts = [e for e in starts if e[0] > 42.0]
        check("进入新剧情后能重新开录", len(late_starts) >= 1,
              f"开录时刻={[round(e[0],1) for e in starts]}")

        ctrl.detector.stop()
        try:
            game.root.quit()
        except Exception:
            pass

    game.root.after(72000, finish)
    game.run()

    print("\n" + "=" * 62)
    allok = all(ok for _n, ok in RESULT)
    for n, ok in RESULT:
        print(f"  {'✓' if ok else '✗'} {n}")
    print("=" * 62)
    print(f"{'通过 ✔' if allok else '失败 ✘'}  ({len(RESULT)} 项)")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
