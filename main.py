"""命令行入口。

  python main.py                 打开图形界面
  python main.py cli             纯命令行监控（无界面，适合挂后台）
  python main.py selfcheck       环境自检
  python main.py probe           探测 OBS 连接

命令行模式用 Ctrl+C 停止，结束时自动停录。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def app_dir() -> str:
    """程序所在目录。

    PyInstaller 打包后 __file__ 指向 _internal 里的临时位置，配置若跟着它走，
    用户会在 _internal 里翻出 config.yaml，重装或换机器就丢了。
    所以打包后固定用 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE = app_dir()
if BASE not in sys.path:
    sys.path.insert(0, BASE)

DEFAULT_CFG = os.path.join(BASE, "config.yaml")


# --------------------------------------------------------------------------- #
def cmd_gui(args) -> int:
    from galrec import gui
    gui.run(args.config)
    return 0


def cmd_selfcheck(args) -> int:
    from galrec import capture, obs as obsmod, ocr, windows

    # 打包成 exe 后没有控制台，自检结果要能在图形界面里看到，
    # 所以额外把内容写到 exe 同目录的 selfcheck.txt
    report = []
    ok_all = True

    def out(s=""):
        report.append(s)
        try:
            print(s, flush=True)
        except Exception:
            pass

    def item(name, ok, detail=""):
        nonlocal ok_all
        ok_all = ok_all and ok
        out(f" [{'✓' if ok else '✗'}] {name}" + (f" — {detail}" if detail else ""))

    out("=" * 60)
    out(" Galgame 自动录制助手 · 环境自检")
    out("=" * 60)
    out(f" 程序目录    : {BASE}")
    out(f" Python      : {sys.version.split()[0]}  ({sys.executable})")
    out(f" 打包运行    : {bool(getattr(sys, 'frozen', False))}")

    try:
        import websocket  # noqa
        item("websocket-client", True, websocket.__version__)
    except Exception as e:
        item("websocket-client", False, str(e))

    try:
        from PIL import ImageGrab  # noqa
        item("Pillow 截图", True)
    except Exception as e:
        item("Pillow 截图", False, str(e))

    try:
        import numpy  # noqa
        item("numpy", True, numpy.__version__)
    except Exception as e:
        item("numpy", False, str(e))

    ok, msg = ocr.engine_available()
    item("OCR（rapidocr）", ok, msg)
    if ok:
        t = time.time()
        ok2, msg2 = ocr.warmup()
        item("OCR 模型加载", ok2, f"{msg2}，{time.time()-t:.1f}s")

    try:
        import tkinter  # noqa
        item("tkinter（图形界面）", True)
    except Exception as e:
        item("tkinter（图形界面）", False, str(e))

    wins = windows.list_windows()
    item("窗口枚举", len(wins) > 0, f"{len(wins)} 个候选窗口")
    for w in wins[:6]:
        out(f"        · {w.label()}")

    from galrec.config import Config
    cfg = Config.load(args.config)
    st = obsmod.probe(cfg.obs.host, cfg.obs.port, cfg.obs.password, cfg.obs.protocol)
    item("OBS 连接", st.connected, st.obs_version or st.message)
    if st.connected:
        out(f"        协议 {st.protocol}  端口 {st.port}  录制目录 {st.record_dir}")

    exe = cfg.resolve_path(cfg.obs.exe_path)
    item("OBS 程序路径", os.path.isfile(exe), exe)

    d = cfg.record_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        item("录制目录可写", True, d)
    except Exception as e:
        item("录制目录可写", False, f"{d} — {e}")

    out("=" * 60)
    out(" 结论：" + ("全部就绪，可以开始录制。" if ok_all else "有项目未通过，见上方 ✗ 行。"))
    out("=" * 60)

    # 无控制台时（打包成 exe）把报告落盘，方便排查
    if getattr(sys, "frozen", False):
        try:
            path = os.path.join(BASE, "selfcheck.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(report))
            try:
                os.startfile(path)
            except Exception:
                pass
        except Exception:
            pass
    return 0 if ok_all else 1


def cmd_probe(args) -> int:
    from galrec import obs as obsmod
    from galrec.config import Config
    cfg = Config.load(args.config)
    st = obsmod.probe(cfg.obs.host, cfg.obs.port, cfg.obs.password, cfg.obs.protocol)
    if st.connected:
        print(f"已连接：{st.protocol} / {st.obs_version}")
        print(f"录制目录：{st.record_dir}")
        print(f"正在录制：{'是 ' + str(round(st.duration, 1)) + 's' if st.recording else '否'}")
        return 0
    print(f"连接失败：{st.message}")
    print("\n排查：")
    print(" 1. OBS 是否在运行？")
    print(" 2. OBS 27 需要安装 obs-websocket 4.x 插件；OBS 28+ 已内建（工具 → WebSocket 服务器设置）")
    print(" 3. 端口默认 4455，密码要与 OBS 里设置的一致")
    print(" 4. 防火墙是否拦了本地回环（一般不会）")
    return 1


def cmd_cli(args) -> int:
    """无界面长跑模式。"""
    from galrec import obs as obsmod, windows
    from galrec.config import Config
    from galrec.recorder import Controller

    windows.enable_dpi_awareness()
    cfg = Config.load(args.config)

    def log(msg: str, level: str = "info"):
        ts = time.strftime("%H:%M:%S")
        mark = {"ok": "✓", "warn": "!", "error": "✗"}.get(level, "·")
        print(f"[{ts}] {mark} {msg}", flush=True)

    def snap(s):
        pass

    ctrl = Controller(cfg, log=log, on_snapshot=snap)
    hwnd = 0
    if args.title or args.process:
        w = windows.find_window(title_sub=args.title, process=args.process)
        if w:
            hwnd = w.hwnd
            log(f"锁定窗口：{w.label()}", "ok")
        else:
            log(f"没找到窗口（title={args.title!r} process={args.process!r}）", "error")
            return 2
    elif cfg.detect.window_title or cfg.detect.process_name:
        w = windows.find_window(title_sub=cfg.detect.window_title,
                                process=cfg.detect.process_name)
        if w:
            hwnd = w.hwnd
            log(f"锁定窗口：{w.label()}", "ok")

    if not ctrl.start(hwnd=hwnd):
        log("启动失败（OBS 未连接？）", "error")
        return 1

    stop_flag = {"v": False}

    def on_sig(_s, _f):
        stop_flag["v"] = True
    try:
        signal.signal(signal.SIGINT, on_sig)
    except Exception:
        pass

    log("监控中…（Ctrl+C 退出）")
    last_line = ""
    try:
        while not stop_flag["v"]:
            time.sleep(1.0)
            s = ctrl.detector.snapshot
            if s.phase != "recording" and s.phase != "armed":
                pass
            if s.text and s.text != last_line:
                last_line = s.text
                print(f"        台词：{last_line[:60]}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        log("正在停止…")
        ctrl.stop()
        time.sleep(0.5)
        for s in ctrl.sessions:
            log(f"第 {s.index} 段 {s.duration:.0f}s → {s.path}")
    return 0


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Galgame 剧情自动录制（OBS）")
    ap.add_argument("command", nargs="?", default="gui",
                    choices=["gui", "cli", "selfcheck", "probe"],
                    help="默认 gui")
    ap.add_argument("-c", "--config", default=DEFAULT_CFG, help="配置文件路径")
    ap.add_argument("--title", default="", help="cli 模式：窗口标题包含")
    ap.add_argument("--process", default="", help="cli 模式：进程名，如 game.exe")
    args = ap.parse_args(argv)
    return {
        "gui": cmd_gui, "cli": cmd_cli,
        "selfcheck": cmd_selfcheck, "probe": cmd_probe,
    }[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
