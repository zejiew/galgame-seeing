"""诊断：在模拟窗口上持续采样，打印每次 OCR 读数与相似度，定位「迟迟不停录」的原因。"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from galrec import capture, ocr, windows  # noqa: E402
from tests.simulate import FakeGame  # noqa: E402


def main():
    windows.enable_dpi_awareness()
    game = FakeGame()
    game.root.update()
    time.sleep(0.8)
    game.root.update()

    w = windows.find_window(title_sub="假Galgame")
    print("window:", w.label())
    cl = windows.client_rect(w.hwnd)
    print("client:", cl)

    rel = (0.05, 0.66, 0.95, 0.96)
    cw, ch = cl[2] - cl[0], cl[3] - cl[1]
    bbox = (int(cl[0] + rel[0] * cw), int(cl[1] + rel[1] * ch),
            int(cl[0] + rel[2] * cw), int(cl[1] + rel[3] * ch))
    print("bbox:", bbox, "size", (bbox[2] - bbox[0], bbox[3] - bbox[1]))

    g = capture.Grabber(bbox)
    prev = ""
    t0 = time.time()

    def pump():
        nonlocal prev
        for _ in range(90):
            time.sleep(0.4)
            img = g.grab()
            if img is None:
                continue
            lines = ocr.recognize(img, min_conf=0.55)
            txt = ocr.join_lines(lines, min_conf=0.55)
            sim = ocr.similarity(txt, prev) if prev else 0.0
            print(f"[{time.time()-t0:5.1f}] n={len(lines)} confs={[round(l.conf,2) for l in lines]} "
                  f"sim={sim:.2f} txt={txt[:40]!r}", flush=True)
            prev = txt

    threading.Thread(target=pump, daemon=True).start()
    game.root.after(500, lambda: game.start_script(lambda m: print("GAME:", m, flush=True)))
    game.root.after(75000, game.root.quit)
    game.run()


if __name__ == "__main__":
    main()
