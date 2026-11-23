"""屏幕截图与轻量像素比对。

只用 Pillow 的 ImageGrab（PrintWindow 对 D3D 游戏经常黑屏，实测不可靠），
配合 numpy 做降采样指纹。抓一次 1920x1080 约 30~50ms，够 2 次/秒的轮询。
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageGrab

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
