"""tkinter 图形界面。

设计原则：一屏看完全部信息——左边状态与台词，右边设置，底部日志。
线程模型：检测器跑在后台线程，所有 UI 更新通过队列回到主线程，避免 Tk 跨线程崩溃。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import __version__, capture, ocr, obs as obsmod, windows
from .config import DEFAULT_CONFIG_NAME, Config
from .detector import Phase, Snapshot
from .recorder import Controller, Session
from .selector import RegionMask, RegionSelector

APP_TITLE = "Galgame 自动录制助手"
FONT_UI = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_MONO = ("Consolas", 9)
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_DIALOGUE = ("Microsoft YaHei UI", 11)

PHASE_TEXT = {
    Phase.IDLE: ("待机", "#888888"),
    Phase.ARMED: ("监控中 · 等待剧情", "#2f81f7"),
    Phase.RECORDING: ("● 录制中", "#e5484d"),
    Phase.COOLDOWN: ("等待下一段剧情", "#d29922"),
}


class App(tk.Tk):
    def __init__(self, cfg_path: str):
        super().__init__()
        self.cfg_path = cfg_path
        self.cfg = Config.load(cfg_path)
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self.ctrl = Controller(
            self.cfg,
            log=lambda m, lv="info": self._q.put(("log", (m, lv))),
            on_snapshot=lambda s: self._q.put(("snap", s)),
            on_sessions=lambda s: self._q.put(("sessions", s)),
        )
        self.ctrl.on_ui_event = lambda k, p: self._q.put((k, p))
        self._banner_shown = False
        self._win_map = {}

        self.title(f"{APP_TITLE}  v{__version__}")
        self.geometry("1180x800")
        self.minsize(1040, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_style()
        self._build_ui()
        self.after(100, self._drain)
        self.after(200, self._boot)

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #
    def _build_style(self):
        style = ttk.Style(self)
        # clam 主题下 Checkbutton 的勾选标记会被画成「×」，
        # 看起来像「未勾选」，容易误读。默认用 vista（Win10/11 原生外观），
        # 只有它不可用时才退回 clam。
        for theme in ("vista", "winnative", "clam"):
            try:
                if theme in style.theme_names():
                    style.theme_use(theme)
                    break
            except Exception:
                continue
        style.configure(".", font=FONT_UI)
        style.configure("Hint.TLabel", foreground="#6b7280",
                        font=("Microsoft YaHei UI", 8))
        style.configure("Title.TLabel", font=FONT_TITLE)
        style.configure("TLabelframe.Label", font=FONT_BOLD)
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", font=FONT_BOLD)
        style.configure("TNotebook.Tab", padding=(14, 6))

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=5, minsize=480)
        root.columnconfigure(1, weight=4, minsize=470)
        root.rowconfigure(1, weight=1)

        # ============ 顶部：控制条 ============ #
        top = ttk.Frame(root)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)

        self.btn_run = ttk.Button(top, text="▶  开始监控", style="Accent.TButton",
                                  command=self._toggle_run, width=14)
        self.btn_run.grid(row=0, column=0, sticky="w")

        self.lbl_phase = ttk.Label(top, text="待机", style="Title.TLabel")
        self.lbl_phase.grid(row=0, column=1, sticky="w", padx=12)

        self.lbl_obs = ttk.Label(top, text="OBS 未连接", style="TLabel")
        self.lbl_obs.grid(row=0, column=2, sticky="e", padx=(0, 8))

        ttk.Button(top, text="连接测试", command=self._test_obs,
                   width=9).grid(row=0, column=3, sticky="e")

        # ============ 左：状态 ============ #
        left = ttk.Frame(root)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=2)
        left.rowconfigure(4, weight=1)

        # -- 窗口 -- #
        wf = ttk.LabelFrame(left, text="游戏窗口", padding=8)
        wf.grid(row=0, column=0, sticky="ew")
        wf.columnconfigure(0, weight=1)
        self.cmb_win = ttk.Combobox(wf, state="readonly", height=12)
        self.cmb_win.grid(row=0, column=0, sticky="ew")
        self.cmb_win.bind("<<ComboboxSelected>>", lambda e: self._on_pick_window())
        btns = ttk.Frame(wf)
        btns.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="刷新列表", command=self._refresh_windows,
                   width=10).pack(side="left")
        ttk.Button(btns, text="自动识别对话框", command=self._auto_region,
                   width=16).pack(side="left", padx=6)
        ttk.Button(btns, text="手动框选区域", command=self._manual_region,
                   width=13).pack(side="left")
        self.lbl_region = ttk.Label(wf, text="区域：自动", style="Hint.TLabel")
        self.lbl_region.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # -- 实时状态 -- #
        sf = ttk.LabelFrame(left, text="实时状态", padding=8)
        sf.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        sf.columnconfigure(1, weight=1)
        self.stat_vars = {}
        for i, (k, label, init) in enumerate([
            ("diff", "画面变化", "0.00"),
            ("idle", "静默时长", "0s"),
            ("elapsed", "本段时长", "0:00"),
            ("segments", "已完成段数", "0"),
        ]):
            ttk.Label(sf, text=label + "：").grid(row=i, column=0, sticky="w")
            v = tk.StringVar(value=init)
            ttk.Label(sf, textvariable=v, font=FONT_MONO).grid(
                row=i, column=1, sticky="w")
            self.stat_vars[k] = v

        # -- 当前台词 -- #
        df = ttk.LabelFrame(left, text="当前台词（OCR）", padding=8)
        df.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        df.columnconfigure(0, weight=1)
        self.txt_line = tk.Text(df, height=3, wrap="word", font=FONT_DIALOGUE,
                                bg="#ffffff", relief="flat", padx=8, pady=6,
                                foreground="#1f2937")
        self.txt_line.grid(row=0, column=0, sticky="ew")
        self.txt_line.configure(state="disabled")

        # -- 日志 -- #
        lf = ttk.LabelFrame(left, text="运行日志", padding=6)
        lf.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(lf, height=9, wrap="word", font=FONT_MONO,
                               bg="#0f172a", fg="#cbd5e1", relief="flat",
                               padx=8, pady=6)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)
        for tag, color in (("info", "#cbd5e1"), ("ok", "#4ade80"),
                           ("warn", "#fbbf24"), ("error", "#f87171")):
            self.txt_log.tag_configure(tag, foreground=color)

        # -- 录制记录 -- #
        rf = ttk.LabelFrame(left, text="本次录制", padding=6)
        rf.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        rf.columnconfigure(0, weight=1)
        rf.rowconfigure(0, weight=1)
        cols = ("n", "dur", "reason", "file")
        self.tree = ttk.Treeview(rf, columns=cols, show="headings", height=4)
        for c, t, w in (("n", "#", 34), ("dur", "时长", 60),
                        ("reason", "停止原因", 92), ("file", "文件", 240)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "file" else "center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._open_session)

        # ============ 右：设置 ============ #
        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.nb = ttk.Notebook(right)
        self.nb.grid(row=0, column=0, sticky="nsew")
        self._build_tab_obs()
        self._build_tab_detect()
        self._build_tab_record()
        self._build_tab_advance()

        bar = ttk.Frame(right)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="保存设置", command=self._save_cfg).pack(side="right")
        ttk.Button(bar, text="恢复默认", command=self._reset_cfg).pack(side="right", padx=6)

        self._log(f"配置：{self.cfg_path}", "info")
        self._refresh_windows(quiet=True)
        self._load_cfg_to_ui()

    # ------------------------------------------------------------------ #
    # 设置页：OBS
    # ------------------------------------------------------------------ #
    def _build_tab_obs(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  OBS  ")
        f.columnconfigure(1, weight=1)
        r = 0

        self.v_host = tk.StringVar()
        self.v_port = tk.StringVar()
        self.v_pw = tk.StringVar()
        self.v_proto = tk.StringVar()
        self.v_backend = tk.StringVar()
        self.v_hk_start = tk.StringVar()
        self.v_hk_stop = tk.StringVar()
        self.v_exe = tk.StringVar()
        self.v_scene = tk.StringVar()
        self.v_obs_dir = tk.StringVar()
        self.v_stopdelay = tk.StringVar()
        self.v_setdir = tk.BooleanVar()
        self.v_autolaunch = tk.BooleanVar()

        r = self._row_combo(f, r, "控制方式", self.v_backend,
                            ["auto", "websocket", "hotkey"])
        ttk.Label(f, text="websocket 功能全；hotkey 不用装插件（OBS 27 走这条）",
                  style="Hint.TLabel", wraplength=430).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 8)); r += 1

        r = self._row_entry(f, r, "地址", self.v_host, width=18)
        r = self._row_entry(f, r, "端口", self.v_port, width=18)
        r = self._row_entry(f, r, "密码", self.v_pw, width=18, show="*")
        r = self._row_combo(f, r, "协议", self.v_proto, ["auto", "v5", "v4"])
        ttk.Label(f, text="OBS 28+ 用 v5（内建）；OBS 27 装 obs-websocket 4.x 插件后用 v4；auto 自动试。",
                  style="Hint.TLabel", wraplength=430).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 8)); r += 1

        r = self._row_entry(f, r, "热键·开始", self.v_hk_start, width=18)
        r = self._row_entry(f, r, "热键·停止", self.v_hk_stop, width=18)
        ttk.Label(f, text="例：ctrl+alt+f9。要先去 OBS 设置 → 热键，把「开始录制/停止录制」设成同样的组合。",
                  style="Hint.TLabel", wraplength=430).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 8)); r += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=6); r += 1

        r = self._row_entry(f, r, "OBS 程序", self.v_exe, width=30)
        ttk.Button(f, text="…", width=3,
                   command=lambda: self._pick_file(self.v_exe,
                                                   [("OBS", "obs64.exe"),
                                                    ("程序", "*.exe")])
                   ).grid(row=r - 1, column=2, sticky="w", padx=(4, 0))

        r = self._row_entry(f, r, "场景名", self.v_scene, width=18)
        ttk.Label(f, text="留空用当前场景", style="Hint.TLabel").grid(
            row=r - 1, column=2, sticky="w", padx=(4, 0))

        r = self._row_entry(f, r, "录制目录", self.v_obs_dir, width=30)
        ttk.Button(f, text="…", width=3,
                   command=lambda: self._pick_dir(self.v_obs_dir)).grid(
            row=r - 1, column=2, sticky="w", padx=(4, 0))

        r = self._row_entry(f, r, "停录宽限(秒)", self.v_stopdelay, width=10)
        ttk.Label(f, text="结束后多录几秒", style="Hint.TLabel").grid(
            row=r - 1, column=2, sticky="w", padx=(4, 0))

        r += 1
        ttk.Checkbutton(f, text="连接时把 OBS 录制目录改到上面这个",
                        variable=self.v_setdir).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        ttk.Checkbutton(f, text="OBS 没开就自动启动它",
                        variable=self.v_autolaunch).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        self.lbl_obs_detail = ttk.Label(f, text="", style="Hint.TLabel",
                                        wraplength=430, justify="left")
        self.lbl_obs_detail.grid(row=r, column=0, columnspan=3, sticky="w")

    # ------------------------------------------------------------------ #
    # 设置页：检测
    # ------------------------------------------------------------------ #
    def _build_tab_detect(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  检测  ")
        f.columnconfigure(1, weight=1)
        r = 0

        self.v_interval = tk.StringVar()
        self.v_thresh = tk.StringVar()
        self.v_idle = tk.StringVar()
        self.v_grace = tk.StringVar()
        self.v_minrec = tk.StringVar()
        self.v_maxrec = tk.StringVar()
        self.v_occlusion = tk.StringVar()
        self.v_ocr = tk.BooleanVar()
        self.v_auto_region = tk.BooleanVar()
        self.v_require_dlg = tk.BooleanVar()
        self.v_segmented = tk.BooleanVar()
        self.v_detect_occ = tk.BooleanVar()

        rows = [
            ("采样间隔(秒)", self.v_interval, "0.3~2，越小越灵敏也越费 CPU"),
            ("变化阈值", self.v_thresh, "0.5~10，画面没动却一直触发就调大"),
            ("结束判定(秒)", self.v_idle, "多久没有新台词算剧情结束"),
            ("开录宽限(秒)", self.v_grace, "刚开录时不判结束（片头/加载）"),
            ("最短保留(秒)", self.v_minrec, "比这短的录像自动删掉"),
            ("单段上限(秒)", self.v_maxrec, "防止忘关录一整天，0=不限"),
            ("遮挡超时(秒)", self.v_occlusion, "窗口被挡住多久算中断，0=不因此停"),
        ]
        for label, var, hint in rows:
            r = self._row_entry(f, r, label, var, width=10)
            ttk.Label(f, text=hint, style="Hint.TLabel").grid(
                row=r - 1, column=2, sticky="w", padx=(6, 0))

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1

        for var, text in [
            (self.v_ocr, "用 OCR 读台词（推荐，判断最准）"),
            (self.v_auto_region, "自动识别对话框位置"),
            (self.v_require_dlg, "必须有台词才算剧情开始"),
            (self.v_segmented, "停录后剧情再动时另起一段"),
            (self.v_detect_occ, "检测窗口是否被其它窗口挡住"),
        ]:
            ttk.Checkbutton(f, text=text, variable=var).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        ttk.Label(f, text="OCR 状态：").grid(row=r, column=0, sticky="w")
        self.lbl_ocr = ttk.Label(f, text="检查中…", style="Hint.TLabel",
                                 wraplength=330, justify="left")
        self.lbl_ocr.grid(row=r, column=1, columnspan=2, sticky="w"); r += 1

        ttk.Button(f, text="在这块区域上试识别一次",
                   command=self._test_ocr).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------ #
    # 设置页：录制
    # ------------------------------------------------------------------ #
    def _build_tab_record(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  录制  ")
        f.columnconfigure(1, weight=1)
        r = 0

        self.v_start_mode = tk.StringVar()
        self.v_stop_mode = tk.StringVar()
        self.v_tpl = tk.StringVar()
        self.v_remux = tk.BooleanVar()
        self.v_delshort = tk.BooleanVar()
        self.v_ffmpeg = tk.StringVar()

        ttk.Label(f, text="开始条件").grid(row=r, column=0, sticky="w", pady=4)
        box = ttk.Frame(f)
        box.grid(row=r, column=1, columnspan=2, sticky="w")
        for val, text in (("on_dialogue", "出现新台词时"),
                          ("on_window", "画面一动就开"),
                          ("manual", "完全手动")):
            ttk.Radiobutton(box, text=text, value=val,
                            variable=self.v_start_mode).pack(side="left", padx=(0, 10))
        r += 1

        ttk.Label(f, text="停止条件").grid(row=r, column=0, sticky="w", pady=4)
        box2 = ttk.Frame(f)
        box2.grid(row=r, column=1, columnspan=2, sticky="w")
        for val, text in (("on_end", "剧情结束自动停"), ("manual", "只手动停")):
            ttk.Radiobutton(box2, text=text, value=val,
                            variable=self.v_stop_mode).pack(side="left", padx=(0, 10))
        r += 1

        r = self._row_entry(f, r, "文件名模板", self.v_tpl, width=26)
        ttk.Label(f, text="可用 {game} {time} {date} {index}",
                  style="Hint.TLabel").grid(row=r - 1, column=2, sticky="w",
                                            padx=(6, 0))

        r = self._row_entry(f, r, "ffmpeg", self.v_ffmpeg, width=26)
        ttk.Button(f, text="…", width=3,
                   command=lambda: self._pick_file(
                       self.v_ffmpeg, [("ffmpeg", "ffmpeg.exe"),
                                       ("程序", "*.exe")])
                   ).grid(row=r - 1, column=2, sticky="w", padx=(4, 0))

        r += 1
        ttk.Checkbutton(f, text="录制完用 ffmpeg 转封装为 mp4",
                        variable=self.v_remux).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        ttk.Checkbutton(f, text="自动删除太短的录像",
                        variable=self.v_delshort).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1
        ttk.Label(f, text="小工具", style=FONT_BOLD and "TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Button(f, text="打开录制目录", command=self._open_rec_dir).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        ttk.Button(f, text="打开 OBS", command=self._launch_obs).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1

    # ------------------------------------------------------------------ #
    # 设置页：自动推进
    # ------------------------------------------------------------------ #
    def _build_tab_advance(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="  自动推进  ")
        f.columnconfigure(1, weight=1)
        r = 0

        self.v_adv_on = tk.BooleanVar()
        self.v_adv_mode = tk.StringVar()
        self.v_adv_key = tk.StringVar()
        self.v_adv_delay = tk.StringVar()
        self.v_adv_interval = tk.StringVar()
        self.v_adv_choice = tk.BooleanVar()
        self.v_adv_stuck = tk.BooleanVar()
        self.v_adv_stuck_n = tk.StringVar()

        ttk.Checkbutton(f, text="启用自动推进（替你按键推进剧情）",
                        variable=self.v_adv_on).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 6)); r += 1

        ttk.Label(f, text="⚠ 这是可选功能，默认关闭。开关打开后才会真的按键。",
                  style="Hint.TLabel", foreground="#b45309",
                  wraplength=440, justify="left").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 8)); r += 1

        r = self._row_combo(f, r, "方式", self.v_adv_mode,
                            ["key", "mouse", "both"])
        ttk.Label(f, text="key=按键盘；mouse=点鼠标；both=都发",
                  style="Hint.TLabel").grid(row=r - 1, column=2, sticky="w",
                                            padx=(6, 0))

        r = self._row_combo(f, r, "按键", self.v_adv_key,
                            ["space", "enter", "ctrl", "z", "x"])
        ttk.Label(f, text="多数 galgame 按 space 或 enter 推进",
                  style="Hint.TLabel").grid(row=r - 1, column=2, sticky="w",
                                            padx=(6, 0))

        r = self._row_entry(f, r, "台词后等待(秒)", self.v_adv_delay, width=10)
        ttk.Label(f, text="这句显示完多久才按，别打断打字机",
                  style="Hint.TLabel").grid(row=r - 1, column=2, sticky="w",
                                            padx=(6, 0))

        r = self._row_entry(f, r, "最小间隔(秒)", self.v_adv_interval, width=10)
        ttk.Label(f, text="两次按键之间至少隔多久",
                  style="Hint.TLabel").grid(row=r - 1, column=2, sticky="w",
                                            padx=(6, 0))

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1

        ttk.Checkbutton(f, text="遇到选项自动停手（强烈建议开着）",
                        variable=self.v_adv_choice).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1
        ttk.Checkbutton(f, text="连续按了没反应就停（防卡死在选项/小游戏）",
                        variable=self.v_adv_stuck).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=2); r += 1

        r = self._row_entry(f, r, "卡住判定次数", self.v_adv_stuck_n, width=10)
        ttk.Label(f, text="按这么多次台词还没变就停",
                  style="Hint.TLabel").grid(row=r - 1, column=2, sticky="w",
                                            padx=(6, 0))

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1

        self.lbl_adv = ttk.Label(f, text="状态：未启用", style="Hint.TLabel",
                                 wraplength=440, justify="left")
        self.lbl_adv.grid(row=r, column=0, columnspan=3, sticky="w"); r += 1

        box = ttk.Frame(f)
        box.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(box, text="立即暂停推进", width=14,
                   command=self._pause_advance).pack(side="left")
        ttk.Button(box, text="恢复推进", width=10,
                   command=self._resume_advance).pack(side="left", padx=6)
        ttk.Button(box, text="试按一次", width=10,
                   command=self._test_advance).pack(side="left")
        r += 1

        ttk.Label(f, text="提示：按键只发给前台窗口。切出去看视频/聊天时程序不会按键，"
                          "切回游戏后自动继续。",
                  style="Hint.TLabel", wraplength=440, justify="left").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ------------------------------------------------------------------ #
    # 通用行构造
    # ------------------------------------------------------------------ #
    def _row_entry(self, parent, r, label, var, width=20, show=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=3,
                                           padx=(0, 8))
        e = ttk.Entry(parent, textvariable=var, width=width, show=show)
        e.grid(row=r, column=1, sticky="ew")
        return r + 1

    def _row_combo(self, parent, r, label, var, values):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=3,
                                           padx=(0, 8))
        c = ttk.Combobox(parent, textvariable=var, values=values, width=17,
                         state="readonly")
        c.grid(row=r, column=1, sticky="w")
        return r + 1

    def _pick_file(self, var, types):
        p = filedialog.askopenfilename(filetypes=types + [("所有文件", "*.*")])
        if p:
            var.set(p)

    def _pick_dir(self, var):
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    # ------------------------------------------------------------------ #
    # 启动流程
    # ------------------------------------------------------------------ #
    def _boot(self):
        ok, msg = ocr.engine_available()
        self.lbl_ocr.configure(text=msg)
        threading.Thread(target=self._warm_ocr, daemon=True).start()
        self._log("界面已就绪。先选中游戏窗口 → 开始监控。", "info")
        if not self._banner_shown:
            self._banner_shown = True
            threading.Thread(target=self._obs_hint, daemon=True).start()

    def _warm_ocr(self):
        ok, msg = ocr.warmup()
        self._q.put(("ocr", (ok, msg)))

    def _obs_hint(self):
        st = self.ctrl.test_connection()
        self._q.put(("obs", st))

    # ------------------------------------------------------------------ #
    # 队列泵
    # ------------------------------------------------------------------ #
    def _drain(self):
        try:
            for _ in range(80):
                kind, payload = self._q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        except Exception:
            pass
        self.after(120, self._drain)

    def _handle(self, kind: str, payload):
        if kind == "log":
            msg, lv = payload
            self._log(msg, lv)
        elif kind == "snap":
            self._apply_snap(payload)
        elif kind == "sessions":
            self._apply_sessions(payload)
        elif kind == "ocr":
            ok, msg = payload
            self.lbl_ocr.configure(text=("✅ " if ok else "⚠ ") + msg)
        elif kind == "obs":
            self._apply_obs(payload)
        elif kind == "windows":
            self._apply_windows(payload)
        elif kind == "dialogue_region":
            self._apply_region(payload)
        elif kind == "advance_pause":
            self._log(f"自动推进暂停：{payload.get('reason')}", "warn")
            self._refresh_adv_label()
        elif kind == "choice":
            self._refresh_adv_label()
        elif kind == "msg":
            title, text, is_err = payload
            (messagebox.showerror if is_err else messagebox.showinfo)(title, text)

    # ------------------------------------------------------------------ #
    # 状态刷新
    # ------------------------------------------------------------------ #
    def _apply_snap(self, s: Snapshot):
        try:
            ph = Phase(s.phase)
        except ValueError:
            ph = Phase.IDLE
        text, color = PHASE_TEXT.get(ph, ("未知", "#888"))
        self.lbl_phase.configure(text=text, foreground=color)
        self.stat_vars["diff"].set(f"{s.diff:.2f}")
        self.stat_vars["idle"].set(_fmt_dur(s.idle))
        self.stat_vars["elapsed"].set(_fmt_mmss(s.elapsed))
        self.stat_vars["segments"].set(str(s.segments))
        if s.text:
            self._set_dialogue(s.text)
        if s.region:
            x0, y0, x1, y1 = s.region
            self.lbl_region.configure(
                text=f"区域：{s.note or '自动'}  ({x0},{y0}) {x1-x0}×{y1-y0}")
        self._refresh_adv_label()

    def _set_dialogue(self, text: str):
        self.txt_line.configure(state="normal")
        self.txt_line.delete("1.0", "end")
        self.txt_line.insert("1.0", text)
        self.txt_line.configure(state="disabled")

    def _apply_sessions(self, sessions):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for s in sessions:
            self.tree.insert("", "end", values=(
                s.index, _fmt_mmss(s.live_duration()), _reason_text(s.reason),
                os.path.basename(s.path) if s.path else "(未取到路径)",
            ), tags=(s.path,))

    def _apply_obs(self, st: obsmod.ObsStatus):
        if st.connected:
            self.lbl_obs.configure(text="OBS 已连接", foreground="#16a34a")
            extra = f"方式：{st.protocol}\n版本：{st.obs_version or '未知'}"
            extra += f"\n录制目录：{st.record_dir or '(未知)'}"
            if st.recording:
                extra += f"\n正在录制：{_fmt_mmss(st.duration)}"
            self.lbl_obs_detail.configure(text=extra)
        else:
            self.lbl_obs.configure(text="OBS 未连接", foreground="#b91c1c")
            self.lbl_obs_detail.configure(
                text=st.message or "点「连接测试」查看原因\n"
                                   "OBS 27 需装 obs-websocket 插件，或改用热键模式")

    def _apply_windows(self, wins):
        self._win_map = {}
        labels = []
        for w in wins:
            self._win_map[w.label()] = w
            labels.append(w.label())
        self.cmb_win.configure(values=labels)
        cur = self.cfg.detect.window_title
        picked = ""
        for lab, w in self._win_map.items():
            if cur and (cur.lower() in w.title.lower()
                        or cur.lower() == w.process.lower()):
                picked = lab
                break
        if picked:
            self.cmb_win.set(picked)
        elif labels and self.cmb_win.get() not in labels:
            self.cmb_win.set(labels[0])

    def _apply_region(self, payload):
        rel = payload.get("relative")
        if rel:
            self.cfg.detect.region_rel = list(rel)
            self.lbl_region.configure(
                text=f"区域：自动探测 ({payload.get('pixels')})  "
                     f"比例 {[round(v, 2) for v in rel]}")

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def _toggle_run(self):
        if self.ctrl.running:
            self.ctrl.stop()
            self.btn_run.configure(text="▶  开始监控")
            self.lbl_phase.configure(text="待机", foreground="#888")
            return
        self._collect_ui()
        hwnd = self._selected_hwnd()
        if not hwnd and self.cfg.record.start_mode != "manual":
            if not messagebox.askyesno(
                    "没有选中窗口",
                    "还没有选中游戏窗口。\n\n要继续吗？"
                    "（也可以先去游戏里，再回来点刷新列表）"):
                return
        if self.cfg.advance.enabled:
            if not messagebox.askyesno(
                    "确认开启自动推进",
                    "「自动推进」已启用，程序会替你按键推进剧情。\n\n"
                    "· 只在游戏窗口在前台时按键\n"
                    "· 检测到选项会自动暂停\n"
                    "· 可以随时在「自动推进」页暂停\n\n"
                    "确定开始吗？"):
                return
        if not self.ctrl.start(hwnd=hwnd):
            messagebox.showerror(
                "启动失败",
                "无法连接 OBS，也没配上热键。\n\n"
                "· OBS 28+ 默认已开 WebSocket（端口 4455）\n"
                "· OBS 27 需装 obs-websocket 4.x 插件，"
                "或在设置页把「控制方式」改成 hotkey\n"
                "· 检查地址/端口/密码是否与 OBS 里一致")
            return
        self.btn_run.configure(text="■  停止监控")

    def _test_obs(self):
        self._collect_ui()
        self._log("正在测试 OBS 连接…", "info")

        def run():
            self.ctrl.check_obs()
            st = self.ctrl.test_connection()
            self._q.put(("obs", st))
        threading.Thread(target=run, daemon=True).start()

    def _refresh_windows(self, quiet: bool = False):
        def run():
            wins = windows.list_windows()
            skip = ("explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe",
                    "python.exe", "pythonw.exe", "obs64.exe", "code.exe",
                    "dsh next.exe", "word.exe", "winword.exe")
            real = [w for w in wins if w.process.lower() not in skip]
            self._q.put(("windows", real or wins))
            if not quiet:
                self._q.put(("log", (f"找到 {len(real or wins)} 个窗口", "info")))
        threading.Thread(target=run, daemon=True).start()

    def _on_pick_window(self):
        lab = self.cmb_win.get()
        w = self._win_map.get(lab)
        if not w:
            return
        self.cfg.detect.window_title = w.title
        self.cfg.detect.process_name = ""
        self._log(f"已选择窗口：{w.title} [{w.process}]", "info")
        if self.ctrl.detector.running:
            self.ctrl.detector.bind_window(w.hwnd)

    def _selected_hwnd(self) -> int:
        w = self._win_map.get(self.cmb_win.get())
        return w.hwnd if w else 0

    def _auto_region(self):
        hwnd = self._selected_hwnd()
        if not hwnd:
            messagebox.showinfo("提示", "先在上面选一个游戏窗口")
            return
        self._log("正在自动识别对话框区域（约 2~4 秒）…", "info")

        def run():
            try:
                cl = windows.client_rect(hwnd)
                img = capture.Grabber(cl).grab()
                if img is None:
                    self._q.put(("log", ("截图失败，窗口可能已关闭", "error")))
                    return
                box = ocr.detect_dialogue_region(img)
                cw, ch = cl[2] - cl[0], cl[3] - cl[1]
                if not box:
                    self._q.put(("log", ("没找到成行的台词文字（此刻可能没有台词）。"
                                         "可点「手动框选区域」自己画。", "warn")))
                    return
                rel = [round(box[0] / cw, 3), round(box[1] / ch, 3),
                       round((box[2] - box[0]) / cw, 3), round((box[3] - box[1]) / ch, 3)]
                self._q.put(("dialogue_region", {"relative": rel, "pixels": list(box)}))
                self._q.put(("log", (f"对话框区域：{list(box)}", "ok")))
            except Exception as e:
                self._q.put(("log", (f"自动识别失败：{e}", "error")))
        threading.Thread(target=run, daemon=True).start()

    def _manual_region(self):
        hwnd = self._selected_hwnd()
        self.withdraw()
        self.after(220, lambda: self._do_region_select(hwnd))

    def _do_region_select(self, hwnd: int):
        try:
            sel = RegionSelector(self)
            box = sel.select()
        finally:
            self.deiconify()
            self.lift()
        if not box:
            self._log("已取消框选", "info")
            return
        if hwnd:
            try:
                cl = windows.client_rect(hwnd)
                cw, ch = cl[2] - cl[0], cl[3] - cl[1]
                rel = [round((box[0] - cl[0]) / cw, 3),
                       round((box[1] - cl[1]) / ch, 3),
                       round((box[2] - box[0]) / cw, 3),
                       round((box[3] - box[1]) / ch, 3)]
                self.cfg.detect.region_rel = rel
                self.lbl_region.configure(text=f"区域：手动 {list(box)}")
                self._log(f"已设定区域：{list(box)} 比例 {rel}", "ok")
                RegionMask(self, box)
                return
            except Exception:
                pass
        self.lbl_region.configure(text=f"区域：绝对坐标 {list(box)}")
        self._log(f"已记录绝对区域 {list(box)}（先选窗口才能换算比例）", "warn")

    def _test_ocr(self):
        hwnd = self._selected_hwnd()
        if not hwnd:
            messagebox.showinfo("提示", "先选一个游戏窗口")
            return
        self._collect_ui()
        self._log("正在试识别…", "info")

        def run():
            try:
                cl = windows.client_rect(hwnd)
                cw, ch = cl[2] - cl[0], cl[3] - cl[1]
                rel = self.cfg.detect.region_rel
                if rel and len(rel) == 4:
                    x0 = int(cl[0] + rel[0] * cw)
                    y0 = int(cl[1] + rel[1] * ch)
                    x1 = int(x0 + rel[2] * cw)
                    y1 = int(y0 + rel[3] * ch)
                    bbox = (x0, y0, x1, y1)
                else:
                    bbox = (cl[0] + int(cw * 0.02), cl[1] + int(ch * 0.62),
                            cl[0] + int(cw * 0.98), cl[1] + int(ch * 0.99))
                img = capture.Grabber(bbox).grab()
                if img is None:
                    self._q.put(("msg", ("识别失败", "截图失败", True)))
                    return
                lines = ocr.recognize(img, min_conf=self.cfg.detect.ocr_min_conf)
                txt = ocr.dialogue_text(lines, min_conf=self.cfg.detect.ocr_min_conf)
                if txt:
                    self._q.put(("log", (f"识别到：{txt}", "ok")))
                    self._q.put(("msg", ("识别结果", f"区域 {list(bbox)}\n\n{txt}", False)))
                else:
                    self._q.put(("log", ("没识别到台词文字。区域可能不对，"
                                         "或此刻没有台词。", "warn")))
                    self._q.put(("msg", ("识别结果",
                                         f"区域 {list(bbox)}\n\n没识别到台词文字。\n"
                                         "试试「自动识别对话框」或重新框选。", False)))
            except Exception as e:
                self._q.put(("log", (f"试识别出错：{e}", "error")))
        threading.Thread(target=run, daemon=True).start()

    def _pause_advance(self):
        self._collect_ui()
        self.ctrl.pause_auto_advance("手动暂停")
        self._refresh_adv_label()

    def _resume_advance(self):
        self._collect_ui()
        self.ctrl.resume_auto_advance()
        self._refresh_adv_label()

    def _test_advance(self):
        """按当前设置真的发一次输入，方便确认按键对游戏有效。"""
        self._collect_ui()
        hwnd = self._selected_hwnd()
        if not hwnd:
            messagebox.showinfo("提示", "先选一个游戏窗口")
            return
        if not messagebox.askyesno(
                "试按一次",
                "将向游戏窗口发一次推进按键。\n\n"
                "如果游戏里正停在选项上，可能会替你做选择。\n"
                "确定继续吗？"):
            return
        from . import advance as advmod
        d = self.cfg.advance
        if not windows.bring_to_front(hwnd):
            self._log("无法把游戏窗口切到前台，按键可能不生效", "warn")
        time.sleep(0.5)
        ok = False
        if d.mode in ("key", "both"):
            vk = advmod.ADVANCE_KEYS.get((d.key or "space").lower(), 0x20)
            ok = advmod.send_key(vk) or ok
        if d.mode in ("mouse", "both") and d.click_pos:
            ok = advmod.click_at(int(d.click_pos[0]), int(d.click_pos[1])) or ok
        self._log("已发送一次推进输入" if ok else "发送失败", "ok" if ok else "error")

    def _refresh_adv_label(self):
        st = self.ctrl.advance_state
        if not st.enabled:
            txt = "状态：未启用"
        elif st.paused:
            txt = f"状态：已暂停（{st.pause_reason}）  已推进 {st.actions} 次"
        else:
            txt = f"状态：运行中  已推进 {st.actions} 次"
        try:
            self.lbl_adv.configure(text=txt)
        except Exception:
            pass

    def _open_rec_dir(self):
        self._collect_ui()
        d = self.cfg.record_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except Exception as e:
            self._log(f"打不开目录：{e}", "error")

    def _launch_obs(self):
        exe = self.cfg.resolve_path(self.cfg.obs.exe_path)
        if os.path.isfile(exe):
            windows.launch_exe(exe)
            self._log(f"正在启动 OBS：{exe}", "info")
        else:
            self._log(f"找不到 OBS：{exe}", "error")

    def _open_session(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        tags = self.tree.item(sel[0], "tags")
        if tags and isinstance(tags, (list, tuple)) and tags[0] \
                and os.path.isfile(tags[0]):
            try:
                os.startfile(os.path.dirname(tags[0]))
            except Exception as e:
                self._log(f"打开失败：{e}", "error")

    # ------------------------------------------------------------------ #
    # 配置绑定
    # ------------------------------------------------------------------ #
    def _load_cfg_to_ui(self):
        c = self.cfg
        self.v_host.set(c.obs.host)
        self.v_port.set(str(c.obs.port))
        self.v_pw.set(c.obs.password)
        self.v_proto.set(c.obs.protocol)
        self.v_backend.set(c.obs.backend)
        self.v_hk_start.set(c.obs.hotkey_start)
        self.v_hk_stop.set(c.obs.hotkey_stop)
        self.v_exe.set(c.obs.exe_path)
        self.v_scene.set(c.obs.scene)
        self.v_obs_dir.set(c.obs.record_dir)
        self.v_stopdelay.set(str(c.obs.stop_delay))
        self.v_setdir.set(c.obs.set_record_dir)
        self.v_autolaunch.set(c.obs.auto_launch)

        self.v_interval.set(str(c.detect.poll_interval))
        self.v_thresh.set(str(c.detect.change_threshold))
        self.v_idle.set(str(c.detect.idle_timeout))
        self.v_grace.set(str(c.detect.start_grace))
        self.v_minrec.set(str(c.detect.min_record_seconds))
        self.v_maxrec.set(str(c.detect.max_record_seconds))
        self.v_occlusion.set(str(c.detect.occlusion_timeout))
        self.v_ocr.set(c.detect.ocr)
        self.v_auto_region.set(c.detect.auto_region)
        self.v_require_dlg.set(c.detect.require_dialogue)
        self.v_segmented.set(c.detect.segmented)
        self.v_detect_occ.set(c.detect.detect_occlusion)

        self.v_start_mode.set(c.record.start_mode)
        self.v_stop_mode.set(c.record.stop_mode)
        self.v_tpl.set(c.record.filename_template)
        self.v_remux.set(c.record.remux)
        self.v_delshort.set(c.record.delete_short)
        self.v_ffmpeg.set(c.record.ffmpeg or "")

        self.v_adv_on.set(c.advance.enabled)
        self.v_adv_mode.set(c.advance.mode)
        self.v_adv_key.set(c.advance.key)
        self.v_adv_delay.set(str(c.advance.delay_after_text))
        self.v_adv_interval.set(str(c.advance.min_interval))
        self.v_adv_choice.set(c.advance.stop_on_choice)
        self.v_adv_stuck.set(c.advance.stop_when_stuck)
        self.v_adv_stuck_n.set(str(c.advance.stuck_limit))

    def _collect_ui(self):
        c = self.cfg

        def num(var, default, lo=None, hi=None, cast=float):
            try:
                v = cast(var.get())
            except Exception:
                v = default
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v

        c.obs.host = self.v_host.get().strip() or "127.0.0.1"
        c.obs.port = int(num(self.v_port, 4455, 1, 65535))
        c.obs.password = self.v_pw.get()
        c.obs.protocol = self.v_proto.get() or "auto"
        c.obs.backend = self.v_backend.get() or "auto"
        c.obs.hotkey_start = self.v_hk_start.get().strip()
        c.obs.hotkey_stop = self.v_hk_stop.get().strip()
        c.obs.exe_path = self.v_exe.get().strip()
        c.obs.scene = self.v_scene.get().strip()
        c.obs.record_dir = self.v_obs_dir.get().strip() or "recordings"
        c.obs.stop_delay = num(self.v_stopdelay, 2.0, 0, 120)
        c.obs.set_record_dir = bool(self.v_setdir.get())
        c.obs.auto_launch = bool(self.v_autolaunch.get())

        c.detect.poll_interval = num(self.v_interval, 0.6, 0.15, 10)
        c.detect.change_threshold = num(self.v_thresh, 2.0, 0.1, 100)
        c.detect.idle_timeout = num(self.v_idle, 120.0, 5, 86400)
        c.detect.start_grace = num(self.v_grace, 20.0, 0, 3600)
        c.detect.min_record_seconds = num(self.v_minrec, 15.0, 0, 86400)
        c.detect.max_record_seconds = num(self.v_maxrec, 14400.0, 0, 864000)
        c.detect.occlusion_timeout = num(self.v_occlusion, 180.0, 0, 86400)
        c.detect.ocr = bool(self.v_ocr.get())
        c.detect.auto_region = bool(self.v_auto_region.get())
        c.detect.require_dialogue = bool(self.v_require_dlg.get())
        c.detect.segmented = bool(self.v_segmented.get())
        c.detect.detect_occlusion = bool(self.v_detect_occ.get())

        c.record.start_mode = self.v_start_mode.get() or "on_dialogue"
        c.record.stop_mode = self.v_stop_mode.get() or "on_end"
        c.record.filename_template = self.v_tpl.get() or "{game}_{time}"
        c.record.remux = bool(self.v_remux.get())
        c.record.delete_short = bool(self.v_delshort.get())
        c.record.ffmpeg = self.v_ffmpeg.get().strip()

        c.advance.enabled = bool(self.v_adv_on.get())
        c.advance.mode = self.v_adv_mode.get() or "key"
        c.advance.key = self.v_adv_key.get() or "space"
        c.advance.delay_after_text = num(self.v_adv_delay, 2.5, 0.5, 120)
        c.advance.min_interval = num(self.v_adv_interval, 1.2, 0.3, 60)
        c.advance.stop_on_choice = bool(self.v_adv_choice.get())
        c.advance.stop_when_stuck = bool(self.v_adv_stuck.get())
        c.advance.stuck_limit = int(num(self.v_adv_stuck_n, 8, 2, 100))

    def _save_cfg(self):
        self._collect_ui()
        try:
            self.cfg.save(self.cfg_path)
            self._log(f"设置已保存：{self.cfg_path}", "ok")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _reset_cfg(self):
        if not messagebox.askyesno("恢复默认", "确定把所有设置恢复成默认值吗？"):
            return
        base = os.path.dirname(os.path.abspath(self.cfg_path))
        self.cfg = Config()
        self.cfg.base_dir = base
        self._load_cfg_to_ui()
        self._log("已恢复默认设置（记得点保存）", "info")

    # ------------------------------------------------------------------ #
    def _log(self, msg: str, level: str = "info"):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"[{ts}] {msg}\n", level)
        self.txt_log.see("end")
        try:
            if int(self.txt_log.index("end-1c").split(".")[0]) > 2000:
                self.txt_log.delete("1.0", "400.0")
        except Exception:
            pass

    def _on_close(self):
        try:
            if self.ctrl.running:
                self.ctrl.stop()
            self._collect_ui()
            self.cfg.save(self.cfg_path)
        except Exception:
            pass
        self.destroy()


def _fmt_mmss(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def _fmt_dur(sec: float) -> str:
    sec = max(0, int(sec or 0))
    if sec < 60:
        return f"{sec}s"
    return f"{sec // 60}m{sec % 60:02d}s"


def _reason_text(reason: str) -> str:
    return {
        "idle": "剧情结束", "window_closed": "窗口关闭",
        "max_duration": "达上限", "manual": "手动",
        "occluded": "被遮挡", "error": "异常",
    }.get(reason, reason or "-")


def app_dir() -> str:
    """程序所在目录（打包后是 exe 目录，不是 _internal）。"""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cfg_path: Optional[str] = None) -> None:
    windows.enable_dpi_awareness()
    path = cfg_path or os.path.join(app_dir(), DEFAULT_CONFIG_NAME)
    app = App(path)
    app.mainloop()
