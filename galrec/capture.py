"""屏幕截图与轻量像素比对。

两条通道：

1. ``Grabber`` —— 屏幕像素（ImageGrab）。最通用，但抓的是「屏幕上现在长什么样」，
   窗口一旦被别的窗口盖住就抓到了别人的画面。
2. ``WindowGrabber`` —— 后台抓图（PrintWindow）。直接向窗口要它的内容，
   即使被完全盖住也能拿到自己的画面。很多 galgame 的引擎（EntisGLS、吉里吉里、
   Siglus 等）都吃这套，实测《ハルカナソラ》在完全被遮挡时仍能抓到完整画面。
   但有一类引擎（部分 D3D 独占全屏、Unity 某些配置）PrintWindow 会返回纯黑，
   所以它只作为遮挡时的兜底，抓不到就老实报告失败。

两条通道都配 numpy 做降采样指纹。抓一次 1920x1080 约 30~50ms，够 2 次/秒的轮询。
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageGrab

try:
    import win32con
    import win32gui
    import win32ui
    from ctypes import windll
    _HAS_PW = True
except Exception:  # pragma: no cover - 非 Windows 环境
    _HAS_PW = False

# 指纹尺寸：把区域缩到 32x18 的灰度图，共 576 个像素，比对成本极低
_FP_SIZE = (32, 18)


class Grabber:
    """一个屏幕区域的抓取器，内部缓存滚动指纹。"""

    def __init__(self, bbox: tuple):
        self.bbox = tuple(int(v) for v in bbox)
        self._last: np.ndarray | None = None

    # ---------------- 基础抓取 ---------------- #
    def grab(self) -> Image.Image | None:
        try:
            return ImageGrab.grab(bbox=self.bbox, all_screens=True)
        except Exception:
            try:
                return ImageGrab.grab(bbox=self.bbox)
            except Exception:
                return None

    def grab_array(self) -> np.ndarray | None:
        img = self.grab()
        if img is None:
            return None
        return np.array(img.convert("RGB"))

    # ---------------- 指纹与变化量 ---------------- #
    @staticmethod
    def fingerprint(img: Image.Image) -> np.ndarray:
        g = img.convert("L").resize(_FP_SIZE, Image.BILINEAR)
        return np.asarray(g, dtype=np.float32)

    def update(self) -> tuple[float, np.ndarray | None, Image.Image | None]:
        """抓一帧，返回 (与上帧的差异分, 指纹, 原图)。首帧差异记为 0。"""
        img = self.grab()
        if img is None:
            return 0.0, None, None
        fp = self.fingerprint(img)
        if self._last is None:
            self._last = fp
            return 0.0, fp, img
        diff = float(np.mean(np.abs(fp - self._last)))
        self._last = fp
        return diff, fp, img

    def reset(self) -> None:
        """丢掉上一帧指纹。切抓图通道回来时用——否则第一帧会跟旧通道比，
        假报一次「画面巨变」，把确认计数和静默计时都带偏。"""
        self._last = None

    # ---------------- 判定工具 ---------------- #
    @staticmethod
    def is_blank(img: Image.Image, thresh: float = 4.0) -> bool:
        """画面几乎纯色（全黑/全白）时，说明游戏没在显示内容。"""
        g = np.asarray(img.convert("L").resize((64, 36)), dtype=np.float32)
        return float(np.std(g)) < thresh

    @staticmethod
    def dark_ratio(img: Image.Image, cut: int = 24) -> float:
        g = np.asarray(img.convert("L").resize((64, 36)), dtype=np.uint8)
        return float((g < cut).mean())


class WindowGrabber:
    """后台抓图：直接向窗口要画面（PrintWindow），不受遮挡影响。

    用法与 ``Grabber`` 一致，但坐标是「窗口客户区内的像素」，不是屏幕坐标。
    尺寸变了（换分辨率/切全屏）会自动重建位图。
    """

    #: PrintWindow 的 flag。2 = PW_RENDERFULLCONTENT，
    #: 对 DWM 合成的窗口能拿到完整内容，是遮挡兜底的首选。
    FLAG = 2

    def __init__(self, hwnd: int, sub: tuple | None = None):
        self.hwnd = hwnd
        #: 客户区里的子区域 (l, t, r, b)。给了就只返回这一块，
        #: 这样指纹和屏幕通道的「对话区域」对得上，diff 才有可比性。
        self.sub = tuple(int(v) for v in sub) if sub else None
        self._size = (0, 0)
        self._last = None
        self.last_error = ""

    # ---------------- 基础抓取 ---------------- #
    def grab(self) -> Image.Image | None:
        if not _HAS_PW or not self.hwnd:
            self.last_error = "非 Windows 环境或窗口无效"
            return None
        try:
            if not win32gui.IsWindow(self.hwnd):
                self.last_error = "窗口已销毁"
                return None
            # 客户区尺寸（要返回的画面）与它相对整个窗口的偏移
            cl, ct, cr, cb = win32gui.GetClientRect(self.hwnd)
            cw, ch = cr - cl, cb - ct
            if cw <= 0 or ch <= 0:
                self.last_error = "客户区为空（窗口可能已最小化）"
                return None
            wl, wt, wr, wb = win32gui.GetWindowRect(self.hwnd)
            ww, wh = wr - wl, wb - wt
            if ww <= 0 or wh <= 0:
                self.last_error = "窗口矩形无效"
                return None
            # PrintWindow 从窗口左上角开始画（含标题栏），所以按整个窗口建位图，
            # 画完再裁掉标题栏和边框，才是客户区。
            try:
                sl, st = win32gui.ClientToScreen(self.hwnd, (cl, ct))
                off_x, off_y = sl - wl, st - wt
            except Exception:
                off_x = off_y = 0

            hwnd_dc = win32gui.GetWindowDC(self.hwnd)
            if not hwnd_dc:
                self.last_error = "取不到窗口 DC"
                return None
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            try:
                bmp.CreateCompatibleBitmap(mfc_dc, ww, wh)
                save_dc.SelectObject(bmp)
                windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), self.FLAG)
                info = bmp.GetInfo()
                bits = bmp.GetBitmapBits(True)
                img = Image.frombuffer(
                    "RGB", (info["bmWidth"], info["bmHeight"]),
                    bits, "raw", "BGRX", 0, 1,
                )
            finally:
                # GDI 对象必须显式释放，否则这个线程每秒泄漏几个句柄，
                # 挂一晚上监控会把 GDI 句柄数顶到上限。
                try:
                    win32gui.DeleteObject(bmp.GetHandle())
                except Exception:
                    pass
                try:
                    save_dc.DeleteDC()
                except Exception:
                    pass
                try:
                    mfc_dc.DeleteDC()
                except Exception:
                    pass
                try:
                    win32gui.ReleaseDC(self.hwnd, hwnd_dc)
                except Exception:
                    pass

            if (off_x or off_y) and img.size == (ww, wh):
                img = img.crop((off_x, off_y, off_x + cw, off_y + ch))
            elif img.size != (cw, ch):
                img = img.crop((0, 0, min(cw, img.size[0]), min(ch, img.size[1])))
            if self.sub:
                l, t, r, b = self.sub
                l, t = max(0, l), max(0, t)
                r, b = min(img.size[0], r), min(img.size[1], b)
                if r - l >= 2 and b - t >= 2:
                    img = img.crop((l, t, r, b))
            self._size = img.size
            self.last_error = ""
            return img
        except Exception as e:
            self.last_error = f"抓取失败：{e}"
            return None

    def probe(self) -> bool:
        """先试抓一帧，判断这台机器的 PrintWindow 到底能不能用。

        有些引擎（独占全屏的 D3D、部分 Unity 配置）会返回一块纯黑或纯白，
        与其每帧都白抓，不如第一次就试出来。
        """
        img = self.grab()
        if img is None:
            return False
        return not Grabber.is_blank(img)

    # ---------------- 指纹与变化量 ---------------- #
    def update(self) -> tuple[float, np.ndarray | None, Image.Image | None]:
        """抓一帧，返回 (与上帧的差异分, 指纹, 原图)。首帧差异记为 0。"""
        img = self.grab()
        if img is None:
            return 0.0, None, None
        fp = Grabber.fingerprint(img)
        if self._last is None or self._last.shape != fp.shape:
            self._last = fp
            return 0.0, fp, img
        diff = float(np.mean(np.abs(fp - self._last)))
        self._last = fp
        return diff, fp, img

    def reset(self) -> None:
        self._last = None


def probe_region(bbox: tuple, frames: int = 3, gap: float = 0.25) -> dict:
    """新手引导用：抓几帧看看这个区域是否「有内容、会变化」。"""
    import time
    g = Grabber(bbox)
    diffs, blanks, imgs = [], [], []
    for i in range(max(1, frames)):
        img = g.grab()
        if img is None:
            continue
        imgs.append(img)
        diffs.append(float(np.mean(np.abs(
            np.asarray(img.convert("L").resize(_FP_SIZE), dtype=np.float32)
            - np.asarray(imgs[0].convert("L").resize(_FP_SIZE), dtype=np.float32)
        ))))
        blanks.append(Grabber.is_blank(img))
        if i + 1 < frames:
            time.sleep(gap)
    return {
        "frames": len(imgs),
        "diff_max": max(diffs) if diffs else 0.0,
        "blank": all(blanks) if blanks else True,
        "size": imgs[0].size if imgs else (0, 0),
    }
