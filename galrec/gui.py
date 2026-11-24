"""tkinter 图形界面。

设计原则：一屏看完全部信息——上边一条标题栏，左边状态与台词，右边设置，
卡片式分区，背景是天蓝渐变 + 二次元点缀（樱瓣、光斑）。
配色与样式集中在 ``ui_theme``，改风格只动那一个文件。

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
from . import ui_theme as T
from .config import DEFAULT_CONFIG_NAME, Config
from .detector import Phase, Snapshot
from .recorder import Controller, Session
from .selector import RegionMask, RegionSelector

APP_TITLE = "Galgame 自动录制助手"
APP_SUBTITLE = "剧情自动录制 · 由 OBS 驱动的 galgame 助手"

# 兼容旧名字（其它模块/测试可能引用）
FONT_UI = T.FONT_UI
FONT_BOLD = T.FONT_BOLD
FONT_MONO = T.FONT_MONO
FONT_TITLE = T.FONT_TITLE
FONT_DIALOGUE = T.FONT_DIALOGUE

# 阶段 → (胶囊文字, 胶囊底色, 文字色)
PHASE_STYLE = {
    Phase.IDLE: ("待机", "#E7F2F9", T.TEXT_SOFT),
    Phase.ARMED: ("监控中 · 等待剧情", T.ACCENT, "#FFFFFF"),
    Phase.RECORDING: ("● 录制中", T.SAKURA, "#FFFFFF"),
    Phase.COOLDOWN: ("等待下一段剧情", T.AMBER, "#FFFFFF"),
}
# 兼容旧引用
PHASE_TEXT = {k: (v[0], v[1]) for k, v in PHASE_STYLE.items()}


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
        self._phase = Phase.IDLE
        self._pulse_on = False

        self.title(f"{APP_TITLE}  v{__version__}")
        # 任务栏 / 标题栏图标：用 logo 那张图
        try:
            icon = T.app_icon_image(64)
            if icon is not None and hasattr(T, "ImageTk"):
                self._icon_photo = T.ImageTk.PhotoImage(icon)
                self.iconphoto(True, self._icon_photo)
        except Exception:
            pass
        # 按屏幕大小定初始尺寸，别一开就顶满 1920×1080 的边
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1200, max(980, sw - 200))
        h = min(940, max(700, sh - 70))
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 4)}")
        self.configure(bg=T.SKY_MID)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_style()
        self._build_ui()
        # 状态栏测完最小尺寸再锁住，避免窗口能拖到把卡片挤扁
        self.update_idletasks()
        self.minsize(max(980, self.winfo_reqwidth() - 40),
                     max(660, min(760, self.winfo_reqheight())))
        self.after(100, self._drain)
        self.after(200, self._boot)
        self.after(700, self._pulse)

    # ------------------------------------------------------------------ #
    # 外观
    # ------------------------------------------------------------------ #
    def _build_style(self):
        # 整套 ttk 样式（含自绘勾选框）交给 ui_theme，图片对象要在这里保活
        self._theme_kept = T.install_styles(self)
        self._images = self._theme_kept.get("images", [])

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        # 背景画布：既画渐变，也当布局容器（子部件浮在画布上，卡片间隙露出渐变）
        self.bg = T.GradientCanvas(self)
        self.bg.pack(fill="both", expand=True)
        self.bg.columnconfigure(0, weight=5, minsize=460)
        self.bg.columnconfigure(1, weight=4, minsize=440)
        for r in range(8):
            self.bg.rowconfigure(r, weight=0)
        # 台词框给固定高度，多出来的空间全给日志与录制表
        self.bg.rowconfigure(4, weight=5, minsize=120)
        self.bg.rowconfigure(5, weight=4, minsize=120)

        self._build_header()
        self._build_left()
        self._build_right()

        self._log(f"配置：{self.cfg_path}", "info")
        self._refresh_windows(quiet=True)
        self._load_cfg_to_ui()

    # -- 顶部标题栏 -- #
    def _build_header(self):
        # 横幅本身就是一块渐变画布：logo、标题都直接画上去。
        # （子 Label 有自己的纯色底，压在渐变上会显出一个方块，所以不用 Label）
        bar = T.Banner(self.bg, height=86, radius=16,
                       c1="#B7E5FA", c2="#3E96D1")
        bar.configure(bg=T.SKY_LOW)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew",
                 padx=14, pady=(12, 8))
        self.banner = bar

        # 标志：圆形头像（有 logo.jpg 就用它，没有退回花瓣字符）
        # 直接画在横幅画布上——套一层 Canvas 的话，它那块纯色底会在
        # 渐变上留下一个方块，圆形头像的四个角就藏不住了。
        logo_size = 56
        logo_photo = T.logo_photo(logo_size, ring="#FFFFFF", ring_w=3)
        if logo_photo is not None:
            self._images.append(logo_photo)
            bar.create_image(16, (86 - logo_size) // 2, anchor="nw",
                             image=logo_photo)
        else:
            bar.create_oval(16, 15, 16 + logo_size, 15 + logo_size,
                            fill="#FFFFFF", outline="")
            bar.create_text(16 + logo_size / 2, 15 + logo_size / 2 + 1,
                            text=T.ICON_SAKURA, fill=T.SAKURA,
                            font=(T.FAMILY, 20, "bold"))

        # 标题 / 副标题：两行之间留足空隙，不再贴在一起
        tx = 16 + logo_size + 18
        bar.create_text(tx, 31, anchor="w", text=APP_TITLE, fill="#FFFFFF",
                        font=(T.FAMILY, 17, "bold"))
        bar.create_text(tx + 1, 65, anchor="w", text=APP_SUBTITLE,
                        fill="#EAF7FF", font=(T.FAMILY, 9))

        # 右侧一块白色圆角面板，控件都放面板里（同色底，看不出边）
        panel_w, panel_h = 470, 54
        self._panel_photo = T.rrect_photo(panel_w, panel_h, panel_h // 2,
                                          "#FFFFFF")
        if self._panel_photo is not None:
            self._images.append(self._panel_photo)
            bar.create_image(-999, 0, anchor="nw", image=self._panel_photo,
                             tags="panel")
        else:
            bar.create_rectangle(-999, 0, -999 + panel_w, panel_h,
                                 fill="#FFFFFF", outline="", tags="panel")

        holder = tk.Frame(bar, bg="#FFFFFF")
        holder.place(relx=1.0, x=-12, y=16, anchor="ne",
                     width=panel_w, height=panel_h)
        bar.bind("<Configure>", lambda e: self._place_banner_panel(
            bar, holder, panel_w, panel_h), add="+")
        self._banner_panel = (holder, panel_w, panel_h)

        inner = tk.Frame(holder, bg="#FFFFFF")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        self.pill_phase = T.PillLabel(inner, "待机", bg="#FFFFFF",
                                      fill="#E7F2F9", fg=T.TEXT_SOFT,
                                      height=30, min_width=112)
        self.pill_phase.pack(side="left", padx=(6, 0))

        self.lbl_obs = tk.Label(inner, text=f"{T.ICON_DOT} OBS 未连接",
                                bg="#FFFFFF", fg=T.RED,
                                font=T.FONT_SMALL, anchor="w")
        self.lbl_obs.pack(side="left", padx=(14, 10))

        self.btn_run = T.PillButton(inner, "▶  开始监控",
                                    command=self._toggle_run, kind="primary",
                                    bg="#FFFFFF", height=38, min_width=140)
        self.btn_run.pack(side="left")

        self.btn_conn = tk.Canvas(inner, width=96, height=32, bg="#FFFFFF",
                                  highlightthickness=0, bd=0, cursor="hand2")
        self.btn_conn.pack(side="left", padx=(8, 6))
        self._draw_ghost_btn(self.btn_conn, "连接测试", 96, 32, hover=False)
        self.btn_conn.bind("<Enter>", lambda e: self._draw_ghost_btn(
            self.btn_conn, "连接测试", 96, 32, hover=True))
        self.btn_conn.bind("<Leave>", lambda e: self._draw_ghost_btn(
            self.btn_conn, "连接测试", 96, 32, hover=False))
        self.btn_conn.bind("<Button-1>", lambda e: self._test_obs())

    def _place_banner_panel(self, bar, holder, w, h):
        """让白色圆角面板跟着横幅右边一起移动。"""
        try:
            bw = bar.winfo_width()
            y = max(0, (bar.winfo_height() - h) // 2)
            holder.place_configure(x=-12, y=y, width=w, height=h, anchor="ne")
            item = bar.find_withtag("panel")
            if item:
                bar.coords(item[0], bw - 12 - w, y)
                bar.itemconfigure(item[0], anchor="nw")
        except Exception:
            pass

    def _draw_ghost_btn(self, cv: tk.Canvas, text: str, w: int, h: int,
                        hover: bool = False):
        """浅色描边按钮：白底 + 天蓝描边 + 天蓝字。"""
        cv.delete("all")
        bg = "#E4F5FE" if hover else "#F2FBFF"
        photo = T.rrect_photo(w, h, h // 2, bg,
                              outline=T.ACCENT if hover else T.BORDER)
        if photo is not None:
            self._ghost_photos = getattr(self, "_ghost_photos", [])
            self._ghost_photos.append(photo)
            cv.create_image(0, 0, anchor="nw", image=photo)
        cv.create_text(w / 2, h / 2, text=text, fill=T.ACCENT_DARK,
                       font=T.FONT_SMALL)

    # -- 左列：状态 -- #
    def _build_left(self):
        pad = dict(padx=(14, 7), sticky="nsew")

        # ===== 游戏窗口 ===== #
        card = T.Card(self.bg, "游戏窗口", icon=T.ICON_SPARK,
                      subtitle="选中要录的那个游戏")
        card.grid(row=1, column=0, pady=(0, 8), **pad)
        body = card.body
        body.columnconfigure(0, weight=1)

        self.cmb_win = ttk.Combobox(body, state="readonly", height=12)
        self.cmb_win.grid(row=0, column=0, sticky="ew")
        self.cmb_win.bind("<<ComboboxSelected>>", lambda e: self._on_pick_window())

        btns = tk.Frame(body, bg=T.CARD_BG)
        btns.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        ttk.Button(btns, text="↻  刷新列表", style="Ghost.TButton",
                   command=self._refresh_windows).pack(side="left")
        ttk.Button(btns, text="自动识别对话框", style="Ghost.TButton",
                   command=self._auto_region).pack(side="left", padx=6)
        ttk.Button(btns, text="手动框选区域", style="Ghost.TButton",
                   command=self._manual_region).pack(side="left")

        self.lbl_region = tk.Label(body, text="区域：自动", bg=T.CARD_BG,
                                   fg=T.TEXT_MUTED, font=T.FONT_SMALL, anchor="w")
        self.lbl_region.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # ===== 实时状态 ===== #
        card = T.Card(self.bg, "实时状态", icon="◈", accent=T.MINT,
                      subtitle="每秒刷新")
        card.grid(row=2, column=0, pady=(0, 6), **pad)
        grid = card.body
        self.stat_vars = {}
        tiles = [
            ("画面变化", "diff", "0.00", T.ACCENT_DARK),
            ("静默时长", "idle", "0s", T.ACCENT_DARK),
            ("本段时长", "elapsed", "0:00", T.SAKURA_DARK),
            ("已完成段数", "segments", "0", T.MINT),
        ]
        for i, (cap, key, init, color) in enumerate(tiles):
            grid.columnconfigure(i, weight=1, uniform="stat")
            tile = T.StatTile(grid, cap, init, accent=color)
            tile.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else 6, 0))
            self.stat_vars[key] = tile.var

        # ===== 当前台词 ===== #
        card = T.Card(self.bg, "当前台词", icon="❝", accent=T.SAKURA,
                      subtitle="OCR 实时读取")
        card.grid(row=3, column=0, pady=(0, 8), **pad)
        box = tk.Frame(card.body, bg=T.CARD_SOFT, highlightthickness=1,
                       highlightbackground=T.BORDER_SOFT, bd=0)
        box.pack(fill="both", expand=True)
        self.txt_line = T.AccentText(box, height=3, width=40,
                                     font=T.FONT_DIALOGUE,
                                     fg=T.TEXT, bg=T.CARD_SOFT)
        self.txt_line.pack(fill="both", expand=True, padx=4, pady=2)
        self.txt_line.configure(state="disabled")

        # ===== 运行日志 ===== #
        card = T.Card(self.bg, "运行日志", icon="▤",
                      subtitle="最近 2000 行")
        card.grid(row=4, column=0, pady=(0, 8), **pad)
        wrap = tk.Frame(card.body, bg=T.LOG_BG, highlightthickness=0, bd=0)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(wrap, height=5, width=40, wrap="word",
                               font=("Consolas", 9),
                               bg=T.LOG_BG, fg=T.LOG_FG, relief="flat", bd=0,
                               highlightthickness=0, padx=10, pady=6,
                               insertbackground=T.LOG_FG,
                               selectbackground=T.ACCENT_DARK,
                               selectforeground="#FFFFFF")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, style="Sky.Vertical.TScrollbar",
                           command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)
        for tag, color in T.LOG_COLORS.items():
            self.txt_log.tag_configure(tag, foreground=color)

        # ===== 本次录制 ===== #
        card = T.Card(self.bg, "本次录制", icon="◉", accent=T.AMBER,
                      subtitle="双击打开所在文件夹")
        card.grid(row=5, column=0, pady=(0, 14), **pad)
        twrap = tk.Frame(card.body, bg=T.CARD_BG)
        twrap.pack(fill="both", expand=True)
        twrap.columnconfigure(0, weight=1)
        twrap.rowconfigure(0, weight=1)
        cols = ("n", "dur", "reason", "file")
        self.tree = ttk.Treeview(twrap, columns=cols, show="headings", height=3)
        for c, t, w in (("n", "#", 40), ("dur", "时长", 68),
                        ("reason", "停止原因", 96), ("file", "文件", 260)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "file" else "center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(twrap, style="Sky.Vertical.TScrollbar",
                            command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.bind("<Double-1>", self._open_session)

    # -- 右列：设置 -- #
    def _build_right(self):
        panel = tk.Frame(self.bg, bg=T.CARD_BG, highlightthickness=1,
                         highlightbackground=T.BORDER, bd=0)
        panel.grid(row=1, column=1, rowspan=5, sticky="nsew",
                   padx=(7, 14), pady=(0, 14))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        head = tk.Frame(panel, bg=T.CARD_BG)
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        head.columnconfigure(1, weight=1)
        bar = tk.Frame(head, bg=T.ACCENT, width=4, height=15)
        bar.grid(row=0, column=0, sticky="w", padx=(0, 8))
        bar.grid_propagate(False)
        tk.Label(head, text=f"{T.ICON_SPARK}  设置", bg=T.CARD_BG, fg=T.TEXT,
                 font=T.FONT_CARD_TITLE).grid(row=0, column=1, sticky="w")

        self.nb = ttk.Notebook(panel)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        self._build_tab_obs()
        self._build_tab_detect()
        self._build_tab_record()
        self._build_tab_advance()

        bar2 = tk.Frame(panel, bg=T.CARD_BG)
        bar2.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))
        ttk.Button(bar2, text="恢复默认", style="Soft.TButton",
                   command=self._reset_cfg).pack(side="right", padx=(8, 0))
        ttk.Button(bar2, text="保存设置", style="Primary.TButton",
                   command=self._save_cfg).pack(side="right")

    # ------------------------------------------------------------------ #
    # 设置页：OBS
    # ------------------------------------------------------------------ #
    def _build_tab_obs(self):
        f = self._tab_body("  OBS  ")
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
        r = self._hint(f, r, "websocket 功能全；hotkey 不用装插件（OBS 27 走这条）")

        r = self._row_entry(f, r, "地址", self.v_host, width=18)
        r = self._row_entry(f, r, "端口", self.v_port, width=18)
        r = self._row_entry(f, r, "密码", self.v_pw, width=18, show="*")
        r = self._row_combo(f, r, "协议", self.v_proto, ["auto", "v5", "v4"])
        r = self._hint(f, r, "OBS 28+ 用 v5（内建）；OBS 27 装 obs-websocket 4.x "
                             "插件后用 v4；auto 自动试。")

        r = self._row_entry(f, r, "热键·开始", self.v_hk_start, width=18)
        r = self._row_entry(f, r, "热键·停止", self.v_hk_stop, width=18)
        r = self._hint(f, r, "例：ctrl+alt+f9。要先去 OBS 设置 → 热键，把"
                             "「开始录制/停止录制」设成同样的组合。")
        r = self._sep(f, r)

        r = self._row_entry(f, r, "OBS 程序", self.v_exe, width=30)
        ttk.Button(f, text="…", width=3, style="Soft.TButton",
                   command=lambda: self._pick_file(self.v_exe,
                                                   [("OBS", "obs64.exe"),
                                                    ("程序", "*.exe")])
                   ).grid(row=r - 1, column=2, sticky="w", padx=(6, 0))

        r = self._row_entry(f, r, "场景名", self.v_scene, width=18)
        self._note(f, r - 1, "留空用当前场景", padx=(6, 0))

        r = self._row_entry(f, r, "录制目录", self.v_obs_dir, width=30)
        ttk.Button(f, text="…", width=3, style="Soft.TButton",
                   command=lambda: self._pick_dir(self.v_obs_dir)).grid(
            row=r - 1, column=2, sticky="w", padx=(6, 0))

        r = self._row_entry(f, r, "停录宽限(秒)", self.v_stopdelay, width=10)
        self._note(f, r - 1, "结束后多录几秒", padx=(6, 0))

        r += 1
        ttk.Checkbutton(f, text="连接时把 OBS 录制目录改到上面这个",
                        style="Sky.TCheckbutton",
                        variable=self.v_setdir).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1
        ttk.Checkbutton(f, text="OBS 没开就自动启动它",
                        style="Sky.TCheckbutton",
                        variable=self.v_autolaunch).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1

        r = self._sep(f, r)
        self.lbl_obs_detail = tk.Label(f, text="", bg=T.CARD_BG, fg=T.TEXT_MUTED,
                                       font=T.FONT_SMALL, justify="left",
                                       anchor="w", wraplength=430)
        self.lbl_obs_detail.grid(row=r, column=0, columnspan=3, sticky="w")

    # ------------------------------------------------------------------ #
    # 设置页：检测
    # ------------------------------------------------------------------ #
    def _build_tab_detect(self):
        f = self._tab_body("  检测  ")
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
        self.v_occ_fallback = tk.BooleanVar()

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
            self._note(f, r - 1, hint)

        r = self._sep(f, r)

        for var, text in [
            (self.v_ocr, "用 OCR 读台词（推荐，判断最准）"),
            (self.v_auto_region, "自动识别对话框位置"),
            (self.v_require_dlg, "必须有台词才算剧情开始"),
            (self.v_segmented, "停录后剧情再动时另起一段"),
            (self.v_detect_occ, "检测窗口是否被其它窗口挡住"),
            (self.v_occ_fallback, "被挡住时从窗口后台抓图（多数 galgame 支持）"),
        ]:
            ttk.Checkbutton(f, text=text, style="Sky.TCheckbutton",
                            variable=var).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1

        r = self._sep(f, r)
        tk.Label(f, text="OCR 状态", bg=T.CARD_BG, fg=T.TEXT_SOFT,
                 font=T.FONT_BOLD).grid(row=r, column=0, sticky="nw")
        self.lbl_ocr = tk.Label(f, text="检查中…", bg=T.CARD_BG, fg=T.TEXT_MUTED,
                                font=T.FONT_SMALL, justify="left", anchor="w",
                                wraplength=330)
        self.lbl_ocr.grid(row=r, column=1, columnspan=2, sticky="w"); r += 1

        ttk.Button(f, text=f"{T.ICON_SPARK}  在这块区域上试识别一次",
                   style="Ghost.TButton", command=self._test_ocr).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ------------------------------------------------------------------ #
    # 设置页：录制
    # ------------------------------------------------------------------ #
    def _build_tab_record(self):
        f = self._tab_body("  录制  ")
        f.columnconfigure(1, weight=1)
        r = 0

        self.v_start_mode = tk.StringVar()
        self.v_stop_mode = tk.StringVar()
        self.v_tpl = tk.StringVar()
        self.v_remux = tk.BooleanVar()
        self.v_delshort = tk.BooleanVar()
        self.v_ffmpeg = tk.StringVar()

        # 单选框整组独占一行：塞在第三列里时，滚动区宽度不够会把最后一个选项切掉
        tk.Label(f, text="开始条件", bg=T.CARD_BG, fg=T.TEXT_SOFT,
                 font=T.FONT_UI).grid(row=r, column=0, columnspan=3, sticky="w",
                                      pady=(2, 0)); r += 1
        box = tk.Frame(f, bg=T.CARD_BG)
        box.grid(row=r, column=0, columnspan=3, sticky="w", pady=(1, 4))
        for val, text in (("on_dialogue", "出现新台词时"),
                          ("on_window", "画面一动就开"),
                          ("manual", "完全手动")):
            ttk.Radiobutton(box, text=text, value=val, style="Sky.TRadiobutton",
                            variable=self.v_start_mode).pack(side="left",
                                                             padx=(0, 14))
        r += 1

        tk.Label(f, text="停止条件", bg=T.CARD_BG, fg=T.TEXT_SOFT,
                 font=T.FONT_UI).grid(row=r, column=0, columnspan=3, sticky="w",
                                      pady=(2, 0)); r += 1
        box2 = tk.Frame(f, bg=T.CARD_BG)
        box2.grid(row=r, column=0, columnspan=3, sticky="w", pady=(1, 4))
        for val, text in (("on_end", "剧情结束自动停"), ("manual", "只手动停")):
            ttk.Radiobutton(box2, text=text, value=val, style="Sky.TRadiobutton",
                            variable=self.v_stop_mode).pack(side="left",
                                                            padx=(0, 14))
        r += 1

        r = self._row_entry(f, r, "文件名模板", self.v_tpl, width=26)
        self._note(f, r - 1, "可用 {game} {time} {date} {index}")

        r = self._row_entry(f, r, "ffmpeg", self.v_ffmpeg, width=26)
        ttk.Button(f, text="…", width=3, style="Soft.TButton",
                   command=lambda: self._pick_file(
                       self.v_ffmpeg, [("ffmpeg", "ffmpeg.exe"),
                                       ("程序", "*.exe")])
                   ).grid(row=r - 1, column=2, sticky="w", padx=(6, 0))

        r += 1
        ttk.Checkbutton(f, text="录制完用 ffmpeg 转封装为 mp4",
                        style="Sky.TCheckbutton",
                        variable=self.v_remux).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1
        ttk.Checkbutton(f, text="自动删除太短的录像", style="Sky.TCheckbutton",
                        variable=self.v_delshort).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1

        r = self._sep(f, r)
        tk.Label(f, text="小工具", bg=T.CARD_BG, fg=T.TEXT_SOFT,
                 font=T.FONT_CARD_TITLE).grid(row=r, column=0, columnspan=3,
                                              sticky="w"); r += 1
        tools = tk.Frame(f, bg=T.CARD_BG)
        tools.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(tools, text="打开录制目录", style="Ghost.TButton",
                   command=self._open_rec_dir).pack(side="left")
        ttk.Button(tools, text="打开 OBS", style="Ghost.TButton",
                   command=self._launch_obs).pack(side="left", padx=8)

    # ------------------------------------------------------------------ #
    # 设置页：自动推进
    # ------------------------------------------------------------------ #
    def _build_tab_advance(self):
        f = self._tab_body("  自动推进  ")
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
                        style="Sky.TCheckbutton",
                        variable=self.v_adv_on).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 6)); r += 1

        warn = tk.Frame(f, bg=T.AMBER_SOFT, highlightthickness=0, bd=0)
        warn.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 10)); r += 1
        tk.Label(warn, text="⚠  这是可选功能，默认关闭。开关打开后才会真的按键。",
                 bg=T.AMBER_SOFT, fg="#A9741F", font=T.FONT_SMALL,
                 justify="left", anchor="w", wraplength=420,
                 padx=10, pady=8).pack(fill="x")

        r = self._row_combo(f, r, "方式", self.v_adv_mode,
                            ["key", "mouse", "both"])
        self._note(f, r - 1, "key=按键盘；mouse=点鼠标；both=都发")

        r = self._row_combo(f, r, "按键", self.v_adv_key,
                            ["space", "enter", "ctrl", "z", "x"])
        self._note(f, r - 1, "多数 galgame 按 space 或 enter 推进")

        r = self._row_entry(f, r, "台词后等待(秒)", self.v_adv_delay, width=10)
        self._note(f, r - 1, "这句显示完多久才按，别打断打字机")

        r = self._row_entry(f, r, "最小间隔(秒)", self.v_adv_interval, width=10)
        self._note(f, r - 1, "两次按键之间至少隔多久")

        r = self._sep(f, r)

        ttk.Checkbutton(f, text="遇到选项自动停手（强烈建议开着）",
                        style="Sky.TCheckbutton",
                        variable=self.v_adv_choice).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1
        ttk.Checkbutton(f, text="连续按了没反应就停（防卡死在选项/小游戏）",
                        style="Sky.TCheckbutton",
                        variable=self.v_adv_stuck).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=3); r += 1

        r = self._row_entry(f, r, "卡住判定次数", self.v_adv_stuck_n, width=10)
        self._note(f, r - 1, "按这么多次台词还没变就停")

        r = self._sep(f, r)

        self.lbl_adv = tk.Label(f, text="状态：未启用", bg=T.CARD_BG,
                                fg=T.TEXT_MUTED, font=T.FONT_SMALL,
                                justify="left", anchor="w", wraplength=430)
        self.lbl_adv.grid(row=r, column=0, columnspan=3, sticky="w"); r += 1

        box = tk.Frame(f, bg=T.CARD_BG)
        box.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(box, text="立即暂停推进", style="Ghost.TButton",
                   command=self._pause_advance).pack(side="left")
        ttk.Button(box, text="恢复推进", style="Ghost.TButton",
                   command=self._resume_advance).pack(side="left", padx=8)
        ttk.Button(box, text="试按一次", style="Ghost.TButton",
                   command=self._test_advance).pack(side="left")
        r += 1

        tk.Label(f, text="提示：按键只发给前台窗口。切出去看视频/聊天时程序不会按键，"
                         "切回游戏后自动继续。",
                 bg=T.CARD_BG, fg=T.TEXT_MUTED, font=T.FONT_SMALL,
                 justify="left", anchor="w", wraplength=430).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(12, 0))

    # ------------------------------------------------------------------ #
    # 通用行构造
    # ------------------------------------------------------------------ #
    def _tab_body(self, title: str) -> tk.Frame:
        """新建一个设置页并返回可放内容的容器（外层带滚动）。

        标签列定宽；输入列吃掉剩余宽度；行尾提示列按内容自动定宽
        （提示自己的 wraplength 已经把长度压住了，不要再硬锁列宽）。
        """
        holder = ttk.Frame(self.nb, padding=0)
        self.nb.add(holder, text=title)
        sc = T.ScrollFrame(holder, bg=T.CARD_BG)
        sc.pack(fill="both", expand=True)
        body = tk.Frame(sc.inner, bg=T.CARD_BG, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=92)
        body.columnconfigure(1, weight=1, minsize=150)
        body.columnconfigure(2, weight=0)
        return body

    def _row_entry(self, parent, r, label, var, width=20, show=""):
        tk.Label(parent, text=label, bg=T.CARD_BG, fg=T.TEXT_SOFT,
                 font=T.FONT_UI).grid(row=r, column=0, sticky="w", pady=4,
                                      padx=(0, 10))
        e = ttk.Entry(parent, textvariable=var, width=width, show=show)
        e.grid(row=r, column=1, sticky="ew")
        return r + 1

    def _row_combo(self, parent, r, label, var, values):
        tk.Label(parent, text=label, bg=T.CARD_BG, fg=T.TEXT_SOFT,
                 font=T.FONT_UI).grid(row=r, column=0, sticky="w", pady=4,
                                      padx=(0, 10))
        c = ttk.Combobox(parent, textvariable=var, values=values, width=17,
                         state="readonly")
        c.grid(row=r, column=1, sticky="w")
        return r + 1

    def _note(self, parent, r, text, padx=(8, 0)):
        """行尾的灰色小提示。

        放进设置页的第三列时，长句会被滚动区的可视宽度裁掉（滚动条让
        可用宽度少十几像素），所以这里改成「窄列 + 自身换行」，
        宽度只在必要时撑开，不跟输入框抢空间。
        """
        tk.Label(parent, text=text, bg=T.CARD_BG, fg=T.TEXT_MUTED,
                 font=T.FONT_SMALL, justify="left", anchor="w",
                 wraplength=176).grid(row=r, column=2, sticky="w",
                                      padx=(padx[0], 6))

    def _hint(self, parent, r, text):
        tk.Label(parent, text=text, bg=T.CARD_BG, fg=T.TEXT_MUTED,
                 font=T.FONT_SMALL, justify="left", anchor="w",
                 wraplength=440).grid(row=r, column=0, columnspan=3, sticky="w",
                                      pady=(0, 10))
        return r + 1

    def _sep(self, parent, r, pady=8):
        tk.Frame(parent, bg=T.BORDER_SOFT, height=1).grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=pady)
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
        self._set_phase(ph)
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

    def _set_phase(self, ph: Phase):
        self._phase = ph
        text, fill, fg = PHASE_STYLE.get(ph, ("未知", "#9FB6C6", "#FFFFFF"))
        try:
            self.pill_phase.set(text, fill=fill, fg=fg)
        except Exception:
            pass

    def _pulse(self):
        """录制中让胶囊轻微呼吸，一眼就能看出还在录。"""
        try:
            if self._phase == Phase.RECORDING:
                self._pulse_on = not self._pulse_on
                self.pill_phase.set("● 录制中",
                                    fill=T.SAKURA if self._pulse_on else "#FFB3D2",
                                    fg="#FFFFFF")
        except Exception:
            pass
        self.after(620, self._pulse)

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
            self.lbl_obs.configure(text=f"{T.ICON_DOT} OBS 已连接", fg=T.MINT)
            extra = f"方式：{st.protocol}    版本：{st.obs_version or '未知'}\n"
            extra += f"录制目录：{st.record_dir or '(未知)'}"
            if st.recording:
                extra += f"\n正在录制：{_fmt_mmss(st.duration)}"
            self.lbl_obs_detail.configure(text=extra, fg=T.TEXT_SOFT)
        else:
            self.lbl_obs.configure(text=f"{T.ICON_DOT} OBS 未连接", fg=T.RED)
            self.lbl_obs_detail.configure(
                text=st.message or "点「连接测试」查看原因\n"
                                   "OBS 27 需装 obs-websocket 插件，或改用热键模式",
                fg=T.TEXT_MUTED)

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
            self.btn_run.set_text("▶  开始监控")
            self.btn_run.set_kind("primary")
            self._set_phase(Phase.IDLE)
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
        self.btn_run.set_text("■  停止监控")
        self.btn_run.set_kind("stop")

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
            color = T.TEXT_MUTED
        elif st.paused:
            txt = f"状态：已暂停（{st.pause_reason}）  已推进 {st.actions} 次"
            color = T.AMBER
        else:
            txt = f"状态：运行中  已推进 {st.actions} 次"
            color = T.MINT
        try:
            self.lbl_adv.configure(text=txt, fg=color)
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
        self.v_occ_fallback.set(c.detect.occluded_fallback)

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
        c.detect.occluded_fallback = bool(self.v_occ_fallback.get())

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
        "occluded": "被遮挡", "minimized": "最小化", "error": "异常",
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
