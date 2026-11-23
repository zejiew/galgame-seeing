"""天蓝色渐变 + 二次元风格的界面主题。

这个模块集中放三样东西，界面相关的东西只从这里取，改风格只改这一个文件：

1. **调色板 / 字体**：所有颜色、字号常量。
2. **GradientCanvas**：天空蓝渐变背景（PIL 生成，带光斑与飘落樱瓣）。
   窗口拉大拉小时防抖重绘；机器上没有 PIL 时退回「分条画线」，效果差一点但不会崩。
3. **样式与小手件**：``install_styles()`` 装配 ttk 外观，
   ``Card`` / ``PillButton`` / ``PillLabel`` / ``StatTile`` 是几个自绘的小部件，
   用来做出圆角胶囊、卡片这类 tk 原生做不出的形状。

配色思路：上面浅、下面深的天空蓝，白色卡片浮在渐变上，
点缀樱粉（操作重点）与薄荷绿（成功态），文字用深海军蓝而不是纯黑。
"""
from __future__ import annotations

import math
import os
import random
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

try:                       # PIL 是本程序本来就有的依赖，但缺失时也要能跑
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except Exception:          # pragma: no cover
    Image = ImageDraw = ImageTk = None  # type: ignore
    HAS_PIL = False

# 图片资源目录：源码运行时是 galrec/assets，
# 打包后 PyInstaller 会把整个包放到 _MEIPASS/galrec/ 下。
def _assets_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "assets")
    if os.path.isdir(cand):
        return cand
    base = getattr(sys, "_MEIPASS", "")
    if base:
        cand = os.path.join(base, "galrec", "assets")
        if os.path.isdir(cand):
            return cand
    return cand


ASSETS = _assets_dir()
LOGO_FILE = os.path.join(ASSETS, "logo.jpg")


# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #
SKY_TOP = "#F2FBFF"        # 渐变顶：接近白的浅蓝
SKY_MID = "#BFE4FA"
SKY_LOW = "#77BCE9"        # 渐变底：天蓝
SKY_DEEP = "#4E9CD2"

CARD_BG = "#FFFFFF"        # 卡片底
CARD_SOFT = "#F4FBFF"      # 卡片内的浅色块（数字格、输入框底）
BORDER = "#BFE2F7"         # 卡片描边
BORDER_SOFT = "#E2F1FB"

TEXT = "#1E3A50"           # 主文字（深海军蓝，不用纯黑）
TEXT_SOFT = "#5C7E96"      # 次要文字
TEXT_MUTED = "#8FA9BA"     # 提示文字

ACCENT = "#3FA9E0"         # 主天蓝
ACCENT_HOVER = "#5CBBEC"
ACCENT_DARK = "#2C8BC4"
ACCENT_SOFT = "#D6EEFC"

SAKURA = "#FF8FBF"         # 樱粉：强调/运行态
SAKURA_DARK = "#E56E9F"
SAKURA_SOFT = "#FFE3EF"

MINT = "#3FBFA4"           # 薄荷：成功
MINT_SOFT = "#DFF7F1"
AMBER = "#E39A34"          # 等待/提醒
AMBER_SOFT = "#FDF0DC"
RED = "#E2607A"            # 错误
RED_SOFT = "#FCE4EA"

LOG_BG = "#122B40"         # 日志框：深色，像游戏里的控制台
LOG_FG = "#CFE9FA"

# 日志级别配色
LOG_COLORS = {
    "info": "#BFDCF0",
    "ok": "#63E3B6",
    "warn": "#FFC978",
    "error": "#FF8FA3",
    "dim": "#7C99AE",
}

# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #
FAMILY = "Microsoft YaHei UI"
FONT_UI = (FAMILY, 10)
FONT_SMALL = (FAMILY, 9)
FONT_BOLD = (FAMILY, 10, "bold")
FONT_TITLE = (FAMILY, 17, "bold")
FONT_SUB = (FAMILY, 9)
FONT_CARD_TITLE = (FAMILY, 10, "bold")
FONT_STAT = (FAMILY, 15, "bold")
FONT_MONO = ("Consolas", 10)
FONT_DIALOGUE = (FAMILY, 12)

# 小装饰符号：都用常见字符，避免缺字变豆腐块
ICON_SPARK = "✦"
ICON_SAKURA = "❀"
ICON_DOT = "●"


def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a: str, b: str, t: float) -> tuple:
    ca, cb = _rgb(a), _rgb(b)
    t = max(0.0, min(1.0, t))
    return tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


def _hex(c) -> str:
    return "#%02x%02x%02x" % (int(c[0]), int(c[1]), int(c[2]))


# --------------------------------------------------------------------------- #
# 背景图
# --------------------------------------------------------------------------- #
def render_background(w: int, h: int, seed: int = 20240917):
    """生成 w×h 的天空渐变背景（RGB），带柔光光斑与飘落的花瓣。"""
    w, h = max(2, int(w)), max(2, int(h))

    # 渐变大头用小图算，再放大——直接逐像素画 1000×800 会卡顿
    # 上 55% 大部分落在浅蓝，越往下越快过渡到天蓝，底部才够「蓝」。
    sw, sh = 40, 64
    base = Image.new("RGB", (sw, sh))
    px = base.load()
    for y in range(sh):
        ty = y / (sh - 1)
        for x in range(sw):
            tx = x / (sw - 1)
            # 横向也掺一点，形成斜向的明暗，比纯竖向更有空气感
            t = max(0.0, min(1.0, ty ** 0.82 * 0.94 + tx * 0.06))
            if t < 0.5:
                c = _mix(SKY_TOP, SKY_MID, t / 0.5)
            else:
                c = _mix(SKY_MID, SKY_LOW, (t - 0.5) / 0.5)
            px[x, y] = c
    img = base.resize((w, h), Image.BICUBIC).convert("RGBA")

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    rnd = random.Random(seed)

    # 大块柔光（虚化的光斑），营造动画背景那种通透感
    for _ in range(9):
        r = rnd.randint(int(min(w, h) * 0.12), int(min(w, h) * 0.38))
        cx = rnd.randint(-r // 3, w)
        cy = rnd.randint(-r // 3, int(h * 0.9))
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=(255, 255, 255, rnd.randint(14, 26)))

    # 顶部一条淡淡的云带：几团重叠的扁椭圆，模拟动画里的云
    for i in range(5):
        cy = int(h * (0.04 + 0.05 * i))
        cx = int(w * (0.18 + 0.17 * i))
        rw = int(w * (0.20 + 0.04 * (i % 2)))
        rh = max(6, int(h * 0.055))
        d.ellipse([cx - rw, cy - rh, cx + rw, cy + rh],
                  fill=(255, 255, 255, 42 - i * 5))

    # 小光点（星屑）
    for _ in range(52):
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        s = rnd.choice((1, 1, 2, 2, 3))
        a = rnd.randint(60, 150)
        d.ellipse([x - s, y - s, x + s, y + s], fill=(255, 255, 255, a))

    # 樱花瓣：主要飘在四角与边缘，不挡中间内容
    for _ in range(22):
        side = rnd.random()
        if side < 0.5:
            cx, cy = rnd.randint(0, w), rnd.randint(0, int(h * 0.45))
        elif side < 0.7:
            cx, cy = rnd.randint(0, w), rnd.randint(int(h * 0.6), h)
        else:
            cx = rnd.choice((rnd.randint(0, int(w * 0.26)),
                             rnd.randint(int(w * 0.74), w)))
            cy = rnd.randint(0, h)
        size = rnd.uniform(6, 15)
        ang = rnd.uniform(0, math.pi * 2)
        alpha = rnd.randint(46, 88)
        pts = _petal_points(cx, cy, size, ang)
        d.polygon(pts, fill=(255, 158, 196, alpha))

    return Image.alpha_composite(img, overlay).convert("RGB")


def _petal_points(cx: float, cy: float, size: float, ang: float):
    pts = []
    steps = 26
    for i in range(steps):
        a = 2 * math.pi * i / steps
        # 一头稍尖一头圆的心形轮廓，缩放下就是花瓣
        r = size * (0.62 + 0.38 * math.cos(a))
        x = r * math.cos(a) * 0.72
        y = r * math.sin(a)
        xr = x * math.cos(ang) - y * math.sin(ang)
        yr = x * math.sin(ang) + y * math.cos(ang)
        pts.append((cx + xr, cy + yr))
    return pts


class GradientCanvas(tk.Canvas):
    """铺满窗口的渐变背景，同时充当布局容器（卡片直接 grid 在它上面）。

    Canvas 上被 grid/place 的子部件会浮在画布内容之上，于是卡片之间的空隙
    露出的是渐变，而不是一片纯色——这就是不用再套一层 Frame 的原因。
    """

    def __init__(self, master, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("bg", SKY_MID)
        super().__init__(master, **kw)
        self._photo = None
        self._img_id = self.create_image(0, 0, anchor="nw")
        self._size = (0, 0)
        self._job = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, evt):
        if (evt.width, evt.height) == self._size:
            return
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.after(60, lambda: self._redraw(evt.width, evt.height))

    def _redraw(self, w, h):
        self._job = None
        w, h = max(2, int(w)), max(2, int(h))
        if (w, h) == self._size:
            return
        self._size = (w, h)
        if HAS_PIL:
            try:
                img = render_background(w, h)
                self._photo = ImageTk.PhotoImage(img)
                self.itemconfigure(self._img_id, image=self._photo)
                return
            except Exception:
                pass
        # 没有 PIL：用横向色条近似渐变
        self.delete("bg")
        steps = 90
        for i in range(steps):
            t0 = i / steps
            col = _hex(_mix(SKY_TOP, SKY_LOW, t0))
            y0 = int(h * t0)
            y1 = int(h * (i + 1) / steps) + 1
            self.create_rectangle(0, y0, w, y1, outline="", fill=col, tags="bg")
        self.itemconfigure(self._img_id, image="")
        self.tag_lower("bg")


# --------------------------------------------------------------------------- #
# 绘制小工具
# --------------------------------------------------------------------------- #
def rrect_photo(w: int, h: int, radius: int, fill, outline=None, width: int = 1):
    """圆角矩形图片（4 倍超采样再缩小，边缘才不毛糙）。返回 PhotoImage 或 None。"""
    if not HAS_PIL:
        return None
    w, h = max(2, int(w)), max(2, int(h))
    s = 4
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = _rgba(fill)
    outline = _rgba(outline) if outline else None
    d.rounded_rectangle([0, 0, w * s - 1, h * s - 1], radius=radius * s,
                        fill=fill, outline=outline,
                        width=int(width * s) if outline else 0)
    return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))


def _rgba(color):
    if isinstance(color, str):
        c = _rgb(color)
        return (c[0], c[1], c[2], 255)
    if len(color) == 3:
        return (color[0], color[1], color[2], 255)
    return color


def gradient_photo(w: int, h: int, radius: int, c1: str, c2: str,
                   horizontal: bool = False):
    """双色渐变的圆角块，按钮上用来做一点光泽。"""
    if not HAS_PIL:
        return None
    w, h = max(2, int(w)), max(2, int(h))
    s = 3
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    if horizontal:
        grad = Image.new("RGB", (w * s, 1))
        px = grad.load()
        for x in range(w * s):
            t = x / max(1, w * s - 1)
            px[x, 0] = _mix(c1, c2, t)
        grad = grad.resize((w * s, h * s))
    else:
        grad = Image.new("RGB", (1, h * s))
        px = grad.load()
        for y in range(h * s):
            t = y / max(1, h * s - 1)
            px[0, y] = _mix(c1, c2, t)
        grad = grad.resize((w * s, h * s))
    mask = Image.new("L", (w * s, h * s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w * s - 1, h * s - 1], radius=radius * s, fill=255)
    img.paste(grad, (0, 0), mask)
    return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))


# --------------------------------------------------------------------------- #
# ttk 样式
# --------------------------------------------------------------------------- #
def install_styles(root) -> dict:
    """把整套外观装上。返回需要常驻引用的图片对象（Tk 不帮你保活）。"""
    kept: dict = {"images": []}
    style = ttk.Style(root)
    # clam 才能改颜色（vista 是系统原生，很多选项会被忽略）。
    # clam 的勾选标记默认画成「×」，所以下面自己贴图片。
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(
        ".", background=CARD_BG, foreground=TEXT, font=FONT_UI,
        bordercolor=BORDER, darkcolor=BORDER, lightcolor="#FFFFFF",
        troughcolor=ACCENT_SOFT, focuscolor=ACCENT, selectbackground=ACCENT,
        selectforeground="#FFFFFF", fieldbackground=CARD_BG,
    )

    # ---------------- 标签 ---------------- #
    style.configure("TLabel", background=CARD_BG, foreground=TEXT)
    style.configure("CardTitle.TLabel", background=CARD_BG, foreground=TEXT,
                    font=FONT_CARD_TITLE)
    style.configure("Hint.TLabel", background=CARD_BG, foreground=TEXT_MUTED,
                    font=FONT_SMALL)
    style.configure("Field.TLabel", background=CARD_BG, foreground=TEXT_SOFT)
    style.configure("Title.TLabel", background=CARD_BG, foreground=TEXT,
                    font=FONT_TITLE)
    style.configure("Sub.TLabel", background=CARD_BG, foreground=TEXT_MUTED,
                    font=FONT_SUB)
    style.configure("Stat.TLabel", background=CARD_SOFT, foreground=ACCENT_DARK,
                    font=FONT_STAT)
    style.configure("StatCap.TLabel", background=CARD_SOFT,
                    foreground=TEXT_MUTED, font=FONT_SMALL)
    style.configure("Good.TLabel", background=CARD_BG, foreground=MINT)
    style.configure("Bad.TLabel", background=CARD_BG, foreground=RED)
    style.configure("Warn.TLabel", background=CARD_BG, foreground=AMBER)

    # ---------------- 按钮 ---------------- #
    style.configure("TButton", background="#EFF8FF", foreground=TEXT,
                    bordercolor=BORDER, lightcolor="#F7FCFF",
                    darkcolor="#DCEEFA", relief="flat", padding=(12, 6),
                    font=FONT_UI)
    style.map("TButton",
              background=[("pressed", "#D8EDFB"), ("active", "#E2F3FE")],
              bordercolor=[("active", ACCENT)], foreground=[("disabled", "#A9C4D4")])

    style.configure("Primary.TButton", background=ACCENT, foreground="#FFFFFF",
                    bordercolor=ACCENT_DARK, lightcolor=ACCENT,
                    darkcolor=ACCENT_DARK, font=FONT_BOLD, padding=(16, 7))
    style.map("Primary.TButton",
              background=[("pressed", ACCENT_DARK), ("active", ACCENT_HOVER)],
              bordercolor=[("active", ACCENT_DARK)],
              foreground=[("disabled", "#F2FAFE")])

    style.configure("Ghost.TButton", background=CARD_BG, foreground=ACCENT_DARK,
                    bordercolor=BORDER, lightcolor=CARD_BG, darkcolor=CARD_BG,
                    relief="solid", borderwidth=1, padding=(14, 6))
    style.map("Ghost.TButton",
              background=[("active", "#EAF7FF"), ("pressed", ACCENT_SOFT)],
              bordercolor=[("active", ACCENT)])

    style.configure("Soft.TButton", background=CARD_SOFT, foreground=TEXT_SOFT,
                    bordercolor=BORDER_SOFT, lightcolor=CARD_SOFT,
                    darkcolor=CARD_SOFT, padding=(12, 6))
    style.map("Soft.TButton",
              background=[("active", ACCENT_SOFT)],
              foreground=[("active", ACCENT_DARK)])

    style.configure("Stop.TButton", background=SAKURA, foreground="#FFFFFF",
                    bordercolor=SAKURA_DARK, lightcolor=SAKURA,
                    darkcolor=SAKURA_DARK, font=FONT_BOLD, padding=(16, 7))
    style.map("Stop.TButton",
              background=[("pressed", SAKURA_DARK), ("active", "#FFA8CD")])

    # ---------------- 勾选框 / 单选框 ---------------- #
    _install_indicators(style, kept)

    # ---------------- 输入类 ---------------- #
    style.configure("TEntry", fieldbackground="#FFFFFF", foreground=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    insertcolor=ACCENT_DARK, padding=(7, 5), relief="flat")
    style.map("TEntry",
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])

    style.configure("TCombobox", fieldbackground="#FFFFFF", background="#FFFFFF",
                    foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, arrowcolor=ACCENT_DARK,
                    padding=(7, 4), relief="flat")
    style.map("TCombobox",
              fieldbackground=[("readonly", "#FFFFFF")],
              foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", "#FFFFFF")],
              selectforeground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT), ("active", ACCENT)],
              arrowcolor=[("active", ACCENT_DARK)])
    # 下拉列表是原生 Listbox，只能通过 option 改色
    root.option_add("*TCombobox*Listbox.background", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.font", FONT_UI)

    # ---------------- 选项卡 ---------------- #
    style.configure("TNotebook", background=CARD_BG, borderwidth=0,
                    tabmargins=(2, 6, 2, 0))
    style.configure("TNotebook.Tab", background="#EDF7FE", foreground=TEXT_SOFT,
                    bordercolor=BORDER_SOFT, lightcolor="#EDF7FE",
                    darkcolor="#EDF7FE", padding=(16, 7), font=FONT_UI)
    style.map("TNotebook.Tab",
              background=[("selected", CARD_BG), ("active", "#E3F3FE")],
              foreground=[("selected", ACCENT_DARK)],
              font=[("selected", FONT_BOLD)],
              lightcolor=[("selected", CARD_BG)],
              darkcolor=[("selected", CARD_BG)],
              expand=[("selected", (0, 0, 0, 2))])

    # ---------------- 表格 ---------------- #
    style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF",
                    foreground=TEXT, bordercolor=BORDER, rowheight=26,
                    font=FONT_SMALL, lightcolor="#FFFFFF", darkcolor="#FFFFFF",
                    borderwidth=0)
    style.configure("Treeview.Heading", background="#EAF6FE",
                    foreground=TEXT_SOFT, font=FONT_SMALL, relief="flat",
                    bordercolor=BORDER_SOFT, padding=(4, 5))
    style.map("Treeview.Heading", background=[("active", ACCENT_SOFT)])
    style.map("Treeview",
              background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", ACCENT_DARK)])

    # ---------------- 滚动条 ---------------- #
    for orient in ("Vertical", "Horizontal"):
        name = f"Sky.{orient}.TScrollbar"
        style.configure(name, background="#CFE9F9", troughcolor="#F1FAFF",
                        bordercolor="#F1FAFF", lightcolor="#CFE9F9",
                        darkcolor="#CFE9F9", arrowcolor=ACCENT_DARK,
                        gripcount=0, arrowsize=12, relief="flat")
        style.map(name, background=[("active", "#AFDCF4")])
    try:
        root.option_add("*Scrollbar.width", 10)
    except Exception:
        pass

    # ---------------- 分隔线 ---------------- #
    style.configure("TSeparator", background=BORDER_SOFT)

    return kept


def _install_indicators(style: ttk.Style, kept: dict) -> None:
    """自己画勾选/单选标记。

    clam 主题的 Checkbutton 默认显示成「×」，看着像没勾上，很容易误读，
    所以这里用 PIL 画两个小图标替换掉。
    """
    def circlish(size, kind, on):
        if not HAS_PIL:
            return None
        s = 5
        img = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if kind == "check":
            if on:
                d.rounded_rectangle([1 * s, 1 * s, (size - 1) * s - 1, (size - 1) * s - 1],
                                    radius=4 * s, fill=_rgba(ACCENT))
                d.line([(4 * s, 8 * s), (7 * s, 11.4 * s), (12 * s, 5 * s)],
                       fill=(255, 255, 255, 255), width=int(2.1 * s), joint="curve")
            else:
                d.rounded_rectangle([1 * s, 1 * s, (size - 1) * s - 1, (size - 1) * s - 1],
                                    radius=4 * s, fill=(255, 255, 255, 255),
                                    outline=_rgba(BORDER), width=s)
        else:
            c = size / 2
            pad = 1.4
            d.ellipse([pad * s, pad * s, (size - pad) * s - 1, (size - pad) * s - 1],
                      fill=(255, 255, 255, 255),
                      outline=_rgba(ACCENT if on else BORDER), width=int(1.6 * s))
            if on:
                d.ellipse([c * s - 3 * s, c * s - 3 * s,
                           c * s + 3 * s, c * s + 3 * s], fill=_rgba(ACCENT))
        return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))

    if not HAS_PIL:
        return

    ck_off, ck_on = circlish(17, "check", False), circlish(17, "check", True)
    rd_off, rd_on = circlish(17, "radio", False), circlish(17, "radio", True)
    if not all((ck_off, ck_on, rd_off, rd_on)):
        return
    kept["images"] += [ck_off, ck_on, rd_off, rd_on]

    try:
        style.element_create("Sky.Check.indicator", "image", ck_off,
                             ("selected", ck_on), ("disabled", ck_off), border=0)
        style.layout("Sky.TCheckbutton", [
            ("Sky.Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Checkbutton.label", {"side": "left", "sticky": "w"})]}),
        ])
        style.element_create("Sky.Radio.indicator", "image", rd_off,
                             ("selected", rd_on), ("disabled", rd_off), border=0)
        style.layout("Sky.TRadiobutton", [
            ("Sky.Radio.indicator", {"side": "left", "sticky": ""}),
            ("Radiobutton.padding", {"sticky": "nswe", "children": [
                ("Radiobutton.label", {"side": "left", "sticky": "w"})]}),
        ])
    except Exception:
        return

    style.configure("Sky.TCheckbutton", background=CARD_BG, foreground=TEXT,
                    focuscolor=CARD_BG, padding=(0, 3))
    style.map("Sky.TCheckbutton",
              background=[("active", CARD_BG)],
              foreground=[("disabled", TEXT_MUTED)])
    style.configure("Sky.TRadiobutton", background=CARD_BG, foreground=TEXT,
                    focuscolor=CARD_BG, padding=(0, 3))
    style.map("Sky.TRadiobutton",
              background=[("active", CARD_BG)],
              foreground=[("disabled", TEXT_MUTED)])


# --------------------------------------------------------------------------- #
# 小手件
# --------------------------------------------------------------------------- #
class Banner(tk.Canvas):
    """顶部渐变横幅：天蓝横向渐变 + 圆角，子部件直接往上放。

    横幅是整套配色里最大的一块纯渐变区域——卡片都是白的，
    所以「天蓝渐变」这个主色调主要靠这里的面积和左侧露出的背景来表达。
    """

    def __init__(self, master, height: int = 76, radius: int = 16,
                 c1: str = "#A6DDF7", c2: str = "#3E9CD6", **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("bg", SKY_LOW)
        super().__init__(master, height=height, width=10, **kw)
        self._h = height
        self._r = radius
        self._c1, self._c2 = c1, c2
        self._photo = None
        self._img = self.create_image(0, 0, anchor="nw")
        self._size = (0, 0)
        self._job = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, evt):
        if (evt.width, evt.height) == self._size:
            return
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
        w, h = evt.width, self._h
        self._job = self.after(50, lambda: self._redraw(w, h))

    def _redraw(self, w, h):
        self._job = None
        w, h = max(2, int(w)), max(2, int(self._h))
        if (w, h) == self._size:
            return
        self._size = (w, h)
        photo = gradient_photo(w, h, self._r, self._c1, self._c2,
                               horizontal=True)
        if photo is not None:
            self._photo = photo
            self.itemconfigure(self._img, image=photo)
            self.tag_lower(self._img)
        else:
            self.delete("g")
            self.create_rectangle(0, 0, w, h, fill=self._c2, outline="",
                                  tags="g")
            self.tag_lower("g")
        # 横幅上的樱瓣装饰：纯矢量画，不占图片；只在标题与右侧面板之间
        self.delete("deco")
        x0, x1 = int(w * 0.30), max(int(w * 0.31), w - 520)
        if x1 > x0 + 20:
            rnd = random.Random(7)
            for i in range(8):
                cx = rnd.randint(x0, x1)
                cy = rnd.randint(6, max(7, h - 10))
                s = rnd.uniform(3.5, 7.5)
                ang = rnd.uniform(0, math.pi)
                pts = []
                for p in _petal_points(cx, cy, s, ang):
                    pts.extend(p)
                self.create_polygon(pts, fill="#FFFFFF", outline="",
                                    stipple="gray25", tags="deco")


def logo_photo(size: int, ring: str | None = "#FFFFFF", ring_w: int = 3):
    """把 logo.jpg 裁成圆形头像（带白描边），返回 PhotoImage 或 None。

    原图是正方形，直接缩进圆形空位会露出四个角，所以用圆形蒙版裁。
    """
    if not HAS_PIL or not os.path.isfile(LOGO_FILE):
        return None
    try:
        size = max(8, int(size))
        s = 3
        big = size * s
        img = Image.open(LOGO_FILE).convert("RGBA")
        # 居中裁成正方形，避免原图不是 1:1 时被拉扁
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2,
                        (w - side) // 2 + side, (h - side) // 2 + side))
        img = img.resize((big, big), Image.LANCZOS)

        mask = Image.new("L", (big, big), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse([0, 0, big - 1, big - 1], fill=255)
        out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)

        if ring:
            od = ImageDraw.Draw(out)
            if ring_w > 0:
                rw = max(1, int(ring_w * s))
                od.ellipse([rw // 2, rw // 2, big - 1 - rw // 2, big - 1 - rw // 2],
                           outline=_rgba(ring), width=rw)
        return ImageTk.PhotoImage(out.resize((size, size), Image.LANCZOS))
    except Exception:
        # 没有 Tk 根窗口时 ImageTk 会失败——那是测试/导入场景，不是错误
        return None


def app_icon_image(size: int = 64):
    """窗口/任务栏图标（PIL Image 对象），可用 root.iconphoto 设置。"""
    if not HAS_PIL or not os.path.isfile(LOGO_FILE):
        return None
    try:
        img = Image.open(LOGO_FILE).convert("RGB")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2,
                        (w - side) // 2 + side, (h - side) // 2 + side))
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def Card(master, title: str = "", icon: str = ICON_SPARK,
         accent: str = ACCENT, subtitle: str = "",
         pad: tuple = (14, 12), shadow: bool = True):
    """白色卡片：左侧色条标题 + 淡阴影。

    用法：``card = Card(parent, "游戏窗口"); card.body`` 里放内容。
    """
    outer = tk.Frame(master, bg=CARD_BG, bd=0, highlightthickness=0)
    # 阴影：卡片下面垫一条浅蓝细边，模拟投影（tk 没有真阴影）
    if shadow:
        tk.Frame(outer, bg="#D3E9F7", height=2).pack(side="bottom", fill="x")
    card = _CardInner(outer, title, icon, accent, subtitle, pad)
    card.pack(fill="both", expand=True)
    outer.body = card.body
    outer.head_hint = card.head_hint
    outer.set_hint = card.set_hint
    outer.accent = accent
    return outer


class _CardInner(tk.Frame):
    """卡片的实体部分（圆角做不到，靠细边框 + 色条做出层次）。"""

    def __init__(self, master, title: str = "", icon: str = ICON_SPARK,
                 accent: str = ACCENT, subtitle: str = "",
                 pad: tuple = (14, 12)):
        super().__init__(master, bg=CARD_BG, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER,
                         bd=0)
        self.accent = accent
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        if title:
            head = tk.Frame(self, bg=CARD_BG)
            head.grid(row=0, column=0, sticky="ew", padx=pad[0], pady=(9, 0))
            head.columnconfigure(2, weight=1)
            bar = tk.Frame(head, bg=accent, width=4, height=15)
            bar.grid(row=0, column=0, sticky="w", padx=(0, 8))
            bar.grid_propagate(False)
            tk.Label(head, text=f"{icon}  {title}", bg=CARD_BG, fg=TEXT,
                     font=FONT_CARD_TITLE).grid(row=0, column=1, sticky="w")
            self.head_hint = tk.Label(head, text=subtitle, bg=CARD_BG,
                                      fg=TEXT_MUTED, font=FONT_SMALL)
            self.head_hint.grid(row=0, column=2, sticky="e", padx=(8, 0))
            tk.Frame(self, bg=BORDER_SOFT, height=1).grid(
                row=0, column=0, sticky="sew", padx=pad[0], pady=(30, 0))
            top_pad = 6
        else:
            self.head_hint = None
            top_pad = pad[1]

        self.body = tk.Frame(self, bg=CARD_BG)
        self.body.grid(row=1, column=0, sticky="nsew",
                       padx=pad[0], pady=(top_pad, pad[1]))

    def set_hint(self, text: str):
        if self.head_hint is not None:
            self.head_hint.configure(text=text)


class StatTile(tk.Frame):
    """数字格：大号数值 + 小号说明，2×2 摆在「实时状态」卡片里。"""

    def __init__(self, master, caption: str, initial: str = "-",
                 accent: str = ACCENT_DARK, bg: str = CARD_SOFT):
        super().__init__(master, bg=bg, highlightthickness=1,
                         highlightbackground=BORDER_SOFT, bd=0)
        self.var = tk.StringVar(value=initial)
        self._lbl_val = tk.Label(self, textvariable=self.var, bg=bg, fg=accent,
                                 font=FONT_STAT, anchor="w")
        self._lbl_val.pack(fill="x", padx=10, pady=(5, 0))
        tk.Label(self, text=caption, bg=bg, fg=TEXT_MUTED, font=FONT_SMALL,
                 anchor="w").pack(fill="x", padx=10, pady=(0, 5))

    def set(self, value: str):
        self.var.set(value)

    def set_color(self, color: str):
        self._lbl_val.configure(fg=color)


class PillButton(tk.Canvas):
    """胶囊按钮（自绘）。主操作按钮用它，圆角比 ttk 按钮好看得多。"""

    def __init__(self, master, text: str, command=None, kind: str = "primary",
                 bg: str = CARD_BG, height: int = 38, min_width: int = 132,
                 font=FONT_BOLD):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=height, width=min_width, cursor="hand2")
        self._text = text
        self._command = command
        self._font = tkfont.Font(font=font)
        self._height = height
        self._radius = height // 2
        self._min_w = min_width
        self._kind = kind
        self._enabled = True
        self._hover = False
        self._photo_bg = None
        self._photo_off = None
        self._apply_kind(kind)
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- 配色方案 -- #
    def _apply_kind(self, kind: str):
        self._kind = kind
        if kind == "primary":
            self._c1, self._c2, self._fg = "#59B9EA", "#2F93CE", "#FFFFFF"
        elif kind == "stop":
            self._c1, self._c2, self._fg = "#FF9EC6", "#E06B9C", "#FFFFFF"
        elif kind == "ghost":
            self._c1 = self._c2 = CARD_BG
            self._fg = ACCENT_DARK
        else:
            self._c1 = self._c2 = ACCENT
            self._fg = "#FFFFFF"

    def set_kind(self, kind: str):
        if kind != self._kind:
            self._apply_kind(kind)
            self._redraw()

    def set_text(self, text: str):
        if text != self._text:
            self._text = text
            self._redraw()

    def set_enabled(self, on: bool):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._redraw()

    # -- 绘制 -- #
    def _measure(self) -> int:
        return max(self._min_w, self._font.measure(self._text) + 44)

    def _redraw(self):
        w = self._measure()
        h = self._height
        if int(self.winfo_width()) != w:
            self.configure(width=w)
        self.delete("all")
        c1, c2 = self._c1, self._c2
        if not self._enabled:
            c1 = c2 = "#C9DFEE"
        elif self._hover:
            c1, c2 = _hex(_mix(c1, "#FFFFFF", 0.16)), _hex(_mix(c2, "#FFFFFF", 0.16))

        photo = gradient_photo(w, h, self._radius, c1, c2)
        if photo is not None:
            self._photo_bg = photo
            self.create_image(0, 0, anchor="nw", image=photo)
        else:
            self.create_rectangle(0, 0, w, h, fill=c2, outline="")
        if self._kind == "ghost":
            self.create_rectangle(1, 1, w - 1, h - 1, outline=BORDER, width=1)
        # 顶部一条高光，做出软软的立体感
        if self._enabled and self._kind != "ghost":
            self.create_line(self._radius, 2, w - self._radius, 2,
                             fill="#FFFFFF", stipple="gray50")
        fg = self._fg if self._enabled else "#F0F8FD"
        self.create_text(w / 2, h / 2 + 0.5, text=self._text, fill=fg,
                         font=self._font)

    # -- 交互 -- #
    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True
            self._redraw()

    def _on_leave(self, _e):
        if self._hover:
            self._hover = False
            self._redraw()

    def _on_press(self, _e):
        if self._enabled:
            self.move("all", 0, 1)

    def _on_release(self, e):
        if not self._enabled:
            return
        self.move("all", 0, -1)
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            if callable(self._command):
                self._command()


class PillLabel(tk.Canvas):
    """状态胶囊：一段文字 + 底色（待机灰、监控蓝、录制粉…）。"""

    def __init__(self, master, text: str = "", bg: str = CARD_BG,
                 height: int = 30, fill: str = TEXT_MUTED,
                 fg: str = "#FFFFFF", font=FONT_BOLD, min_width: int = 96):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=height, width=min_width)
        self._text = text
        self._fill = fill
        self._fg = fg
        self._font = tkfont.Font(font=font)
        self._height = height
        self._min_w = min_width
        self._photo = None
        self._redraw()

    def set(self, text: str, fill: str | None = None, fg: str | None = None):
        changed = False
        if text != self._text:
            self._text = text
            changed = True
        if fill and fill != self._fill:
            self._fill = fill
            changed = True
        if fg and fg != self._fg:
            self._fg = fg
            changed = True
        if changed:
            self._redraw()

    def _redraw(self):
        w = max(self._min_w, self._font.measure(self._text) + 34)
        h = self._height
        self.configure(width=w)
        self.delete("all")
        photo = rrect_photo(w, h, h // 2, self._fill)
        if photo is not None:
            self._photo = photo
            self.create_image(0, 0, anchor="nw", image=photo)
        else:
            self.create_rectangle(0, 0, w, h, fill=self._fill, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg,
                         font=self._font)


class AccentText(tk.Text):
    """圆角浅底的多行文本（台词框用），带内边距。"""

    def __init__(self, master, height=3, font=FONT_DIALOGUE, fg=TEXT,
                 bg="#FFFFFF", **kw):
        super().__init__(master, height=height, font=font, fg=fg, bg=bg,
                         relief="flat", bd=0, highlightthickness=0,
                         padx=12, pady=10, wrap="word", spacing1=2,
                         spacing3=2, selectbackground=ACCENT_SOFT,
                         selectforeground=TEXT, **kw)


class ScrollFrame(tk.Frame):
    """可滚动的容器，内容放进 ``.inner``。

    设置页字段多，小屏或 125% 缩放时一页放不下，直接塞进 Notebook 会把
    整个窗口的下半部分挤扁。套一层画布后高度可控，内容高时才出滚动条。
    """

    def __init__(self, master, bg: str = CARD_BG, height: int = 300):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                height=height, width=10)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb = ttk.Scrollbar(self, orient="vertical",
                                style="Sky.Vertical.TScrollbar",
                                command=self.canvas.yview)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)
        self.canvas.configure(yscrollcommand=self._on_scroll)

    # -- 尺寸联动 -- #
    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, evt):
        self.canvas.itemconfigure(self._win, width=max(1, evt.width))

    def _on_scroll(self, first, last):
        # 内容装得下就不显示滚动条，免得右边缘多一条灰杠
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self.sb.winfo_ismapped():
                self.sb.pack_forget()
        else:
            if not self.sb.winfo_ismapped():
                self.sb.pack(side="right", fill="y")
        self.sb.set(first, last)

    # -- 滚轮 -- #
    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, evt):
        if self.sb.winfo_ismapped():
            self.canvas.yview_scroll(int(-evt.delta / 120), "units")

