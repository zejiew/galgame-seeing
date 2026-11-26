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
# 标题 / 主菜单识别
# --------------------------------------------------------------------------- #
#: 整行等于这些词 → 就是菜单项。都是 galgame 主菜单的标准写法。
#: （normalize 会去掉空格，所以 "NEW GAME" 与 "NEWGAME" 都写进来）
MENU_WORDS = (
    "开始游戏", "新游戏", "新的游戏", "继续游戏", "开始", "继续",
    "载入", "读取", "读取存档", "载入存档", "加载存档", "存档",
    "退出游戏", "退出", "设置", "设定", "配置", "画廊", "鉴赏", "回想",
    "额外内容", "特典", "安装",
    "NEW GAME", "NEWGAME", "START", "STARTGAME", "CONTINUE", "LOAD",
    "LOADGAME", "SAVE", "CONFIG", "SETTINGS", "OPTIONS", "EXTRA",
    "GALLERY", "EXIT", "QUIT", "ENDFROMTITLE",
    "はじめから", "つづきから", "ろーど", "ロード", "コンフィグ",
    "せってい", "おまけ", "ギャラリー", "しゅうりょう", "タイトルへ",
)

#: 这些词后面常跟版本号 / 副标题 / 星号，允许「以它开头」也算菜单项。
MENU_WORDS_PREFIX = (
    "开始游戏", "新游戏", "新的游戏", "继续游戏", "载入存档", "加载存档",
    "读取存档", "额外内容", "NEW GAME", "NEWGAME", "CONTINUE",
    "GALLERY", "SETTINGS", "OPTIONS", "EXTRA",
    "はじめから", "つづきから", "ギャラリー", "コンフィグ", "タイトルへ",
)


def _menu_norm(text: str) -> str:
    return normalize(text).upper()


#: 预先算好的归一化词表，避免每帧都重算
_MENU_EXACT = {_menu_norm(w) for w in MENU_WORDS if _menu_norm(w)}
_MENU_PREFIX = tuple(_menu_norm(w) for w in MENU_WORDS_PREFIX if _menu_norm(w))


def menu_kind(text: str) -> int:
    """这一行有多像「主菜单项」。

    返回 2=很像（整行就是菜单词，且词够长够有辨识度）、
         1=有点像（整行是 2~3 字短标签，或以菜单词开头）、
         0=不像。

    ⚠ 中文台词里「继续」「回想」这种词很容易出现（「……继续。」「回想起那天」），
    所以 2~3 字的短标签只算「弱」证据，单行不足以定性，必须多行凑一起才算菜单。
    """
    core = _menu_norm(text)
    if not core or len(core) > 16:
        return 0
    if core in _MENU_EXACT:
        return 2 if len(core) >= 4 else 1
    for cw in _MENU_PREFIX:
        if len(core) <= len(cw) + 3 and core.startswith(cw):
            return 1
    return 0


def looks_like_menu_lines(lines: Sequence[Line], max_len: int = 16) -> bool:
    """这一批识别结果是不是主菜单/标题画面上的菜单项。

    对话区域里如果读到的是菜单（有些作品的菜单就压在画面下半部分），
    就不能把它当台词——否则光标一动就是「新的一句」，会同时造成
    「回主页面后永远不停录」和「冷却一到又在主页面开录」。

    ⚠ 关键限定：**必须竖着排成多行**。
    galgame 剧情界面的底部工具栏（「自动 / 跳过 / 存档 / 设置 / 隐藏」）
    也是一排短词，但它们在同一行上。只有「两个以上不同的行」才算菜单，
    否则会把工具栏认成主菜单，在剧情中途把录像掐断。
    """
    cand = []
    for l in lines:
        core = _menu_norm(getattr(l, "text", "") or "")
        if not core or len(core) > max_len:
            continue
        k = menu_kind(l.text)
        if k:
            cand.append((k, l))

    if cand:
        # 行数：按纵向中心分堆，间距超过半个行高就算另一行
        rows = _row_centers([l for _k, l in cand])
        n_rows = len(rows)
        strong = sum(1 for k, _l in cand if k == 2)
        weak = sum(1 for k, _l in cand if k == 1)
        if n_rows < 2:
            # 单行：只有当整行就是一条又长又确定的菜单词（如「NEW GAME」大按钮）
            if strong >= 1 and any(
                    len(_menu_norm(l.text)) >= 4 for k, l in cand if k == 2):
                return True
        elif strong >= 2:
            return True
        elif strong >= 1 and weak >= 1:
            return True
        elif weak >= 2 and len(cand) <= 4:
            return True

    # 词表没命中也别放过：竖排 3 行以上短标签同样是菜单
    return looks_like_stacked_short_lines(lines, max_chars=max_len)


def looks_like_menu_text(text: str) -> bool:
    """单段文字（已经拼好的台词）是不是菜单项。"""
    return menu_kind(text) == 2


def _row_centers(lines: Sequence[Line]) -> list:
    rows: list = []
    for l in sorted(lines, key=lambda x: x.cy):
        h = max(8, getattr(l, "height", 12))
        if rows and abs(l.cy - rows[-1]) <= h * 0.5:
            continue
        rows.append(l.cy)
    return rows


def looks_like_stacked_short_lines(lines: Sequence[Line],
                                   max_chars: int = 8,
                                   min_rows: int = 3) -> bool:
    """兜底判据：竖着排了 3 行以上、每行都很短 → 基本是菜单/按钮，不是台词。

    有些作品的菜单项是自制词（「アーカイブ」「おまけモード」），词表命中不了。
    但结构上仍然骗不过：对话框里的台词通常 1~3 行且至少有一行比较长；
    主菜单是 3 行以上、每行都是两三个词的短按钮。

    ⚠ 故意设得保守（≥3 行 + 每行 ≤8 字）。两行短台词（「嗯。」「好的。」）
    很常见，不能因此把剧情中途的录像掐断。
    """
    good = [l for l in lines if (l.text or "").strip()]
    if len(good) < min_rows:
        return False
    for l in good:
        core = normalize(l.text)
        if len(core) < 1 or len(core) > max_chars:
            return False
        # 台词会带句读；菜单项基本不带
        if any(ch in (l.text or "") for ch in "，。！？、…"):
            return False
    return len(_row_centers(good)) >= min_rows


def menu_word_hits(lines: Sequence[Line], max_len: int = 24) -> set:
    """整窗用：这一屏命中过哪些菜单词。"""
    return menu_evidence(lines, max_len=max_len)[0]


def menu_evidence(lines: Sequence[Line], max_len: int = 24) -> tuple:
    """整窗用：返回 (命中的菜单词集合, 这些命中的行数)。

    行数用来排除「剧情界面底部工具栏」——那是**同一行**上的
    「自动 / 跳过 / 存档 / 设置」，命中两三个词很正常，
    但主菜单的菜单项是**竖着排成多行**的。
    """
    hits = set()
    hit_lines = []
    for l in lines:
        core = _menu_norm(getattr(l, "text", "") or "")
        if not core or len(core) > max_len:
            continue
        for w in MENU_WORDS:
            cw = _menu_norm(w)
            if cw and cw in core:
                hits.add(w)
                hit_lines.append(l)
                break
    return hits, len(_row_centers(hit_lines)) if hit_lines else 0


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
