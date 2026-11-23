"""本地 OCR 与对话框区域自动探测。

用 rapidocr_onnxruntime（PP-OCRv4 onnx，纯 CPU、完全离线、无需联网）。
首次初始化约 3~4 秒（加载模型），之后单次识别 0.2~1 秒，取决于区域大小。

识别结果的比较不看原始字符串：OCR 对同一行字偶尔会多认/漏认一两个字，
直接比字符串会把「同一句话」误判成「新台词」。所以统一做归一化 + 相似度。
"""
from __future__ import annotations

import difflib
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_ENGINE_FAILED: Optional[str] = None


@dataclass
class Line:
    text: str
    conf: float
    box: tuple          # (x0, y0, x1, y1)
    idx: int = 0

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self) -> int:
        return max(1, self.box[3] - self.box[1])


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
def engine_available() -> Tuple[bool, str]:
    """能否加载 OCR。返回 (可用, 说明)。"""
    global _ENGINE_FAILED
    if _ENGINE is not None:
        return True, "已加载"
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception as e:
        _ENGINE_FAILED = f"缺少 rapidocr_onnxruntime：{e}"
        return False, _ENGINE_FAILED
    if _ENGINE_FAILED:
        return False, _ENGINE_FAILED
    return True, "未加载（首次识别时初始化）"


def get_engine():
    """线程安全地懒加载 OCR 引擎。"""
    global _ENGINE, _ENGINE_FAILED
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
            # 不传参数，用库自带默认配置：它内部会挑可用 provider。
            # 之前试过显式指定 use_cuda 系列参数，但不同版本的参数名不一致，
            # 传错会被 **kwargs 吞掉而不报错，反而更难排查。
            _ENGINE = RapidOCR()
        except Exception as e:
            _ENGINE_FAILED = f"OCR 初始化失败：{e}"
            raise
    return _ENGINE


def warmup() -> Tuple[bool, str]:
    """提前加载模型（界面启动时后台调一次，避免第一次识别卡 4 秒）。"""
    try:
        get_engine()
        return True, "OCR 就绪"
    except Exception as e:
        return False, str(e)


# --------------------------------------------------------------------------- #
# 识别
# --------------------------------------------------------------------------- #
def recognize(img: "Image.Image | np.ndarray", min_conf: float = 0.4) -> List[Line]:
    """识别一张图，返回按阅读顺序排序的行。"""
    if isinstance(img, Image.Image):
        arr = np.array(img.convert("RGB"))
    else:
        arr = img
    if arr is None or arr.size == 0:
        return []
    try:
        eng = get_engine()
    except Exception:
        return []
    try:
        res, _ = eng(arr)
    except Exception:
        return []
    if not res:
        return []
    lines: List[Line] = []
    for i, item in enumerate(res):
        try:
            box, text, conf = item[0], item[1], float(item[2])
        except Exception:
            continue
        if conf < min_conf or not str(text).strip():
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        lines.append(Line(
            text=str(text).strip(), conf=conf,
            box=(min(xs), min(ys), max(xs), max(ys)), idx=i,
        ))
    lines.sort(key=lambda l: (round(l.cy / max(1, l.height) * 0.5), l.cx))
    return lines


# --------------------------------------------------------------------------- #
# 文本归一化与比较
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(
    r"[\s\u3000·•…—\-—–_.,，。、;；:：!！?？\"'“”‘’()（）\[\]【】<>《》~～|/\\]+"
)


def normalize(text: str) -> str:
    """去掉标点空白与视觉等价字符，只留下可比对的骨架。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _PUNCT_RE.sub("", t)
    return t


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def join_lines(lines: Sequence[Line], min_conf: float = 0.4) -> str:
    """把多行拼成一段台词文本（按阅读顺序，行间不留空格）。"""
    parts = [l.text for l in lines if l.conf >= min_conf and l.text.strip()]
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 台词过滤
# --------------------------------------------------------------------------- #
# 这些是「文字很多且带 UI 特征」的界面，说明我们读到的多半不是游戏，而是
# 盖在上面的 IDE / 浏览器 / 日志窗口。命中就当没读到字。
_UI_WORDS = (
    "思考", "写入", "运行命令", "import ", "def ", "class ", "return ",
    "http://", "https://", "www.", "菜单", "登录", "注册",
    "token", "error:", "warning:", "traceback", "null", "undefined",
    "Galgame剧情",
)

# 出现即判定为代码/日志，不需要凑够两个
_CODE_MARKERS = (
    "def ", "class ", "import ", "return ", "print(", "self.", "()",
    "#!/", "<?xml", "<div", "{" , "}", "==", "!=", "=>", "::",
)


def looks_like_dialogue_line(text: str, max_len: int = 120) -> bool:
    """单行是不是「像台词」：长度合理、不是代码/URL、不像 UI 文案。"""
    t = (text or "").strip()
    if not t:
        return False
    core = normalize(t)
    if len(core) < 2:
        return False
    if len(core) > max_len:
        return False          # 台词不会有几百字
    low = t.lower()
    if "://" in low or low.startswith("www."):
        return False
    if low.count("\\") >= 2 or low.count("/") >= 4:
        return False          # 路径
    if t.startswith("#") or t.startswith("//") or t.startswith("/*"):
        return False          # 注释
    # 代码特征：命中两个即判定为代码（一个可能只是台词里带了括号）
    code_hits = sum(1 for m in _CODE_MARKERS if m in low)
    if code_hits >= 2:
        return False
    hits = sum(1 for w in _UI_WORDS if w in low)
    if hits >= 2:
        return False
    if hits >= 1 and code_hits >= 1:
        return False
    return True


def filter_dialogue(lines: Sequence[Line], min_conf: float = 0.4,
                    max_lines: int = 4) -> List[Line]:
    """从识别结果里挑出真正的台词行。

    galgame 的对话框通常只有 1~3 行字；如果一次认出十几行、且行高参差，
    基本可以断定抓到的是别的窗口。这里用「行数上限 + 单行合理性」过滤。
    """
    cand = [l for l in lines if l.conf >= min_conf and l.text.strip()]
    if not cand:
        return []
    good = [l for l in cand if looks_like_dialogue_line(l.text)]
    if not good:
        return []
    # 行高差异过大 → 是排版复杂的界面，不是对话框
    hs = [l.height for l in good]
    if hs and max(hs) > min(hs) * 3.2:
        return []
    if len(good) > max_lines:
        # 只保留最靠下、最像台词的那几行（对话框在最下面）
        good = sorted(good, key=lambda l: (l.cy, -l.cx))[-max_lines:]
        good = sorted(good, key=lambda l: l.cy)
    return good


def dialogue_text(lines: Sequence[Line], min_conf: float = 0.4) -> str:
    """过滤后拼接台词。"""
    return join_lines(filter_dialogue(lines, min_conf), min_conf)


# --------------------------------------------------------------------------- #
# 对话框区域自动探测
# --------------------------------------------------------------------------- #
def detect_dialogue_region(
    img: "Image.Image | np.ndarray",
    bottom_ratio: float = 0.55,
    min_lines: int = 1,
) -> Optional[Tuple[int, int, int, int]]:
    """在窗口截图里找出「台词文字」所在的矩形。

    做法：只在画面下半部分跑 OCR 检测，再用 dialogue 过滤器剔掉代码/UI 文案，
    挑出成行的文字块，取它们的外接框，再外扩一圈作为动态区域。
    找不到就返回 None（调用方退化为整窗比对）。
    """
    if isinstance(img, Image.Image):
        pil = img.convert("RGB")
    else:
        pil = Image.fromarray(img).convert("RGB")
    W, H = pil.size
    if W < 40 or H < 40:
        return None
    y0 = int(H * (1.0 - bottom_ratio))
    crop = pil.crop((0, y0, W, H))
    lines = recognize(crop, min_conf=0.45)
    if len(lines) < min_lines:
        return None

    # 先用「像不像台词」过滤掉 UI / 代码 / 路径
    if not filter_dialogue(lines, min_conf=0.45, max_lines=4):
        return None

    cand = []
    for l in lines:
        w = l.box[2] - l.box[0]
        if w < W * 0.12:
            continue
        if l.height < max(10, H * 0.012):
            continue
        if not looks_like_dialogue_line(l.text):
            continue
        cand.append(l)
    if not cand:
        return None

    # 以最「靠下且最长」的那一行作为锚，收拢它附近的行
    anchor = max(cand, key=lambda l: (l.cy * 0.6 + (l.box[2] - l.box[0]) * 0.4))
    band = anchor.height * 2.2
    group = [l for l in cand if abs(l.cy - anchor.cy) <= band * 1.6]
    if not group:
        group = [anchor]

    x0 = min(l.box[0] for l in group)
    y_top = min(l.box[1] for l in group)
    x1 = max(l.box[2] for l in group)
    y_bot = max(l.box[3] for l in group)

    pad_x = max(20, int(W * 0.04))
    pad_y = max(12, int(anchor.height * 0.9))
    x0 = max(0, int(x0) - pad_x)
    x1 = min(W, int(x1) + pad_x)
    y_top = max(y0, int(y_top) - pad_y + y0) - y0  # 折回裁剪坐标
    y_bot = min(H - y0, int(y_bot) + pad_y)

    # 区域太小说明没找准，宁可不返回
    if (x1 - x0) < W * 0.2 or (y_bot - y_top) < max(8, H * 0.02):
        return None
    return (x0, y0 + y_top, x1, y0 + y_bot)


def region_is_sane(bbox: tuple, size: tuple) -> bool:
    """检查用户给的区域是否合理。"""
    try:
        x0, y0, x1, y1 = [int(v) for v in bbox]
        W, H = size
        if x1 <= x0 or y1 <= y0:
            return False
        if x0 < 0 or y0 < 0 or x1 > W + 2 or y1 > H + 2:
            return False
        return (x1 - x0) >= 40 and (y1 - y0) >= 16
    except Exception:
        return False
