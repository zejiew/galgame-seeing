"""自动推进 + 选项检测的离线测试。

用一个假游戏窗口模拟：台词播完 → 停住等输入 → 喂一次按键 → 下一句。
再插入一段「选项界面」，验证程序能不能认出来并停手。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from galrec import capture, ocr, windows  # noqa: E402
from galrec.advance import looks_like_choice_screen  # noqa: E402


class FakeGame:
    LINES = [
        "早上好，今天也是好天气呢。",
        "你这家伙，真是让人伤脑筋啊。",
        "要不我们一起去天台看看吧？",
    ]
    CHOICES = ["跟她一起去天台", "回教室继续自习", "岔开话题"]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("假Galgame-推进测试")
        self.root.geometry("900x600+150+150")
        self.canvas = tk.Canvas(self.root, bg="#0d1420", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.idx = 0
        self.chars = 0
        self.show_choice = False
        self.done = False
        self._tick()

    def _tick(self):
        c = self.canvas
        W, H = max(1, c.winfo_width()), max(1, c.winfo_height())
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill="#0d1420", outline="")
        c.create_oval(W * 0.4, H * 0.03, W * 0.6, H * 0.6,
                      fill="#27405c", outline="")

        if self.show_choice:
            # 选项：三行居中，竖排
            y0 = H * 0.3
            for i, t in enumerate(self.CHOICES):
                y = y0 + i * H * 0.13
                c.create_rectangle(W * 0.25, y - H * 0.05, W * 0.75, y + H * 0.05,
                                   fill="#182436", outline="#4a6b8a")
                c.create_text(W * 0.5, y, text=t, fill="#dbe7f5",
                              font=("Microsoft YaHei UI", 15))
        else:
            bx0, by0, bx1, by1 = W * 0.06, H * 0.68, W * 0.94, H * 0.94
            c.create_rectangle(bx0, by0, bx1, by1, fill="#0a1018", outline="#3d5470")
            if not self.done:
                txt = self.LINES[self.idx][: self.chars]
            else:
                txt = "（剧情结束）"
            c.create_text(bx0 + 22, by0 + 24, anchor="nw", text=txt,
                          fill="#e8eef7", font=("Microsoft YaHei UI", 18),
                          width=int(bx1 - bx0 - 44))
        self.root.after(80, self._tick)

    def run(self):
        self.root.mainloop()


def pump(game, seconds: float) -> None:
    """让 Tk 真正跑起来多久——只 sleep 不会触发重绘，截到的是旧画面。"""
    end = time.time() + seconds
    while time.time() < end:
        game.root.update()
        time.sleep(0.03)


def main():
    windows.enable_dpi_awareness()
    game = FakeGame()
    game.root.update()
    time.sleep(0.8)
    game.root.update()

    w = windows.find_window(title_sub="假Galgame-推进测试")
    if not w:
        print("!! 没找到假窗口")
        return 1
    print(f"假窗口：{w.label()}")
    cl = windows.client_rect(w.hwnd)
    print(f"客户区：{cl}")

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))

    # ---------- 1. 选项检测（纯函数层）---------- #
    print("\n[1] 选项界面识别")
    game.show_choice = True
    pump(game, 1.2)          # 必须让 Tk 自己重绘，光 sleep 是看不到新画面的
    img = capture.Grabber(cl).grab()
    lines = ocr.recognize(img, min_conf=0.5)
    print(f"  识别到 {len(lines)} 行：{[l.text for l in lines]}")
    # 对话框区域（选项时不存在，传 None）
    is_choice, texts = looks_like_choice_screen(lines, img.size, None)
    check("识别为选项界面", is_choice, f"{texts}")
    check("识别出 3 个选项", len(texts) == 3, str(texts))

    # 台词画面不应被判成选项
    game.show_choice = False
    game.done = False
    game.chars = 99
    game.idx = 0
    pump(game, 1.2)
    img2 = capture.Grabber(cl).grab()
    lines2 = ocr.recognize(img2, min_conf=0.5)
    is_choice2, texts2 = looks_like_choice_screen(lines2, img2.size, None)
    check("台词画面不误判为选项", not is_choice2, f"{[l.text for l in lines2]}")

    # ---------- 2. 自动推进（真发按键）---------- #
    print("\n[2] 自动推进：发按键 → 假游戏前进")
    from galrec.advance import AutoAdvancer, send_key
    from galrec.config import Config

    cfg = Config()
    cfg.advance.enabled = True
    cfg.advance.mode = "key"
    cfg.advance.key = "space"
    cfg.advance.delay_after_text = 0.5
    cfg.advance.min_interval = 0.5
    cfg.advance.stop_on_choice = False   # 这一步单独测推进

    logs = []
    adv = AutoAdvancer(cfg, log=lambda m, l="info": logs.append(m))
    adv.set_enabled(True)

    windows.bring_to_front(w.hwnd)
    pump(game, 0.8)

    # 让假游戏进入「台词播完、等输入」状态
    game.done = False
    game.idx = 0
    game.chars = len(game.LINES[0])
    pump(game, 0.5)

    # 假游戏监听空格键推进
    advanced = {"n": 0}

    def on_space(_e=None):
        if not game.done and not game.show_choice:
            game.idx += 1
            game.chars = 0
            advanced["n"] += 1
            if game.idx >= len(game.LINES):
                game.done = True
                game.idx = len(game.LINES) - 1
            else:
                # 模拟打字机
                def type_out():
                    for k in range(1, len(game.LINES[game.idx]) + 1):
                        game.chars = k
                        time.sleep(0.08)
                threading.Thread(target=type_out, daemon=True).start()
    game.root.bind("<space>", on_space)
    game.root.focus_force()

    ok_any = False
    for i in range(8):
        got = adv.maybe_advance(w.hwnd, game.LINES[game.idx], text_stable=2.0,
                                choice_lines=[], dialogue_bbox=None)
        if got:
            ok_any = True
        pump(game, 0.7)

    check("发送了推进按键", ok_any, f"{adv.state.actions} 次")
    check("假游戏收到了按键", advanced["n"] > 0, f"前进 {advanced['n']} 次")

    # ---------- 3. 选项时停手 ---------- #
    print("\n[3] 出现选项时自动停手")
    cfg.advance.stop_on_choice = True
    cfg.advance.choice_confirm = 2
    adv2 = AutoAdvancer(cfg, log=lambda m, l="info": logs.append(m))
    adv2.set_enabled(True)

    game.show_choice = True
    pump(game, 0.8)

    paused = {"hit": False}
    adv2._on_pause = lambda r, d: paused.__setitem__("hit", True)

    for i in range(4):
        adv2.maybe_advance(w.hwnd, "任意台词", text_stable=3.0,
                           choice_lines=["跟她一起去天台", "回教室继续自习"],
                           dialogue_bbox=None)
        time.sleep(0.2)

    check("遇到选项后暂停", adv2.state.paused, adv2.state.pause_reason)
    check("暂停回调触发", paused["hit"])

    # ---------- 4. 前台保护 ---------- #
    print("\n[4] 前台不是游戏时不按键")
    game.show_choice = False
    pump(game, 0.6)
    cfg.advance.stop_on_choice = False
    adv3 = AutoAdvancer(cfg, log=lambda m, l="info": logs.append(m))
    adv3.set_enabled(True)

    before = adv3.state.actions
    # 故意把焦点给别的窗口（用桌面）
    other = windows.list_windows()
    other_hwnd = 0
    for x in other:
        if "假Galgame" not in x.title:
            other_hwnd = x.hwnd
            break
    if other_hwnd:
        windows.bring_to_front(other_hwnd)
        pump(game, 0.6)
        adv3.maybe_advance(w.hwnd, "测试", text_stable=3.0,
                           choice_lines=[], dialogue_bbox=None)
        check("非前台时不发送", adv3.state.actions == before,
              f"actions={adv3.state.actions}")
    else:
        print("  （跳过：找不到其它窗口）")

    # ---------- 5. 卡住检测 ---------- #
    print("\n[5] 连续按没变化就停")
    windows.bring_to_front(w.hwnd)
    pump(game, 0.5)
    cfg.advance.stop_when_stuck = True
    cfg.advance.stuck_limit = 3
    adv4 = AutoAdvancer(cfg, log=lambda m, l="info": logs.append(m))
    adv4.set_enabled(True)
    for i in range(6):
        adv4.maybe_advance(w.hwnd, "同一句话", text_stable=3.0,
                           choice_lines=[], dialogue_bbox=None)
        time.sleep(0.4)
    check("卡住后自动暂停", adv4.state.paused, adv4.state.pause_reason)

    game.root.quit()

    print("\n" + "=" * 52)
    ok_all = all(ok for _, ok in results)
    for n, ok in results:
        print(f"  {'✓' if ok else '✗'} {n}")
    print("=" * 52)
    print("全部通过 ✔" if ok_all else "有失败项 ✘")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
