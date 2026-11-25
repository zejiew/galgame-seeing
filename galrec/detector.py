"""剧情推进状态机 —— 整个程序的核心。

它在后台线程里按固定间隔看游戏窗口：

    没在录 → 等「剧情开始」（出现台词 / 画面在动）
    在录   → 盯「剧情有没有推进」，连续 idle_timeout 秒没有新台词就判定结束

判定用两路信号加权：

1. **像素变化**：对话框附近画面在变（打字机效果、立绘、CG 切换）。
2. **台词文本**：OCR 出当前台词，和上一句做相似度比较。相似度高 = 还是同一句
   （打字机在逐字显示），相似度低 = 换嘴了，这才是真正的「剧情推进」。

只靠像素会栽在动态背景 / Live2D 呼吸动画上（永远在动 → 永不结束）；
只靠文本会栽在纯过场 CG（一句台词都没有 → 永远不开始）。所以两条腿走路。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from PIL import Image

from . import capture, ocr, windows
from .advance import AutoAdvancer
from .advance import looks_like_choice_screen as adv_choice_check
from .config import Config


class Phase(str, Enum):
    IDLE = "idle"          # 待机：没找到窗口 / 手动模式未启动
    ARMED = "armed"        # 已就绪：盯着画面，等剧情开始
    RECORDING = "recording"
    COOLDOWN = "cooldown"  # 刚停录，等剧情再次起势才允许重开


class StopReason(str, Enum):
    IDLE = "idle"              # 长时间没有新台词
    WINDOW = "window_closed"
    MAXDUR = "max_duration"
    MANUAL = "manual"
    OCCLUDED = "occluded"      # 窗口被遮住太久
    MINIMIZED = "minimized"    # 窗口被最小化（仅在 stop_on_minimized 打开时）
    TITLE = "title"            # 画面回到标题/主菜单
    TEXT_LOST = "text_lost"    # 台词框长时间一个可读的字都没有
    ERROR = "error"


@dataclass
class Snapshot:
    """给界面看的一帧状态。"""
    phase: str = Phase.IDLE.value
    window: str = ""
    region: tuple = ()
    diff: float = 0.0
    text: str = ""
    text_age: float = 0.0      # 当前这句台词停留了多久
    idle: float = 0.0          # 距上次剧情推进多久
    elapsed: float = 0.0       # 本段已录多久
    segments: int = 0
    note: str = ""
    ts: float = field(default_factory=time.time)


class Detector:
    """后台采样线程 + 状态机。事件通过回调抛给上层。"""

    def __init__(
        self,
        cfg: Config,
        on_event: Optional[Callable[[str, dict], None]] = None,
        on_snapshot: Optional[Callable[[Snapshot], None]] = None,
    ):
        self.cfg = cfg
        self._on_event = on_event or (lambda e, p: None)
        self._on_snapshot = on_snapshot or (lambda s: None)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self.phase = Phase.IDLE
        self.hwnd = 0
        self.win: Optional[windows.WindowInfo] = None
        self._grabber: Optional[capture.Grabber] = None
        self._wgrabber: Optional[capture.WindowGrabber] = None  # 遮挡时的后台抓图通道
        self._win_grab_ok: Optional[bool] = None   # None=还没试过
        self._win_grab_tried = 0.0                 # 上次探测后台抓图的时间
        self._region_rel = None      # (x0,y0,x1,y1) 相对客户区的比例
        self._auto_rel = None        # 自动探测出来的区域

        # 自动推进（可选）
        self.advancer: Optional["AutoAdvancer"] = None
        self._choice_lines: list = []
        self._last_advance_ts = 0.0

        # 观测状态
        self._last_text = ""
        self._burst_best = ""        # 当前这句「见过最完整的形态」（对付打字机）
        self._last_text_ts = 0.0
        self._last_progress_ts = 0.0
        self._last_change_ts = 0.0
        self._last_ocr_ts = 0.0
        self._seg_start_ts = 0.0
        self._phase_ts = time.time()
        self._segment_start_text = ""
        self._last_stop_text = ""
        self._last_stop_ts = 0.0
        self._change_streak = 0
        self._no_text_ticks = 0
        self._segments = 0
        self._blank_streak = 0
        self._occluded_since = 0.0
        self._minimized_since = 0.0
        self._minimized_warned = False
        self._last_tick_ts = 0.0
        self._last_dialogue_ts = 0.0
        self._seg_wall_start = 0.0
        self._title_hits = 0
        self._auto_try_ts = 0.0
        self._idle = 0.0
        self._last_snap = Snapshot()
        self.latest_frame: Optional[Image.Image] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self, hwnd: int = 0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.hwnd = hwnd or self.hwnd
        self.phase = Phase.ARMED if hwnd else Phase.IDLE
        self._phase_ts = time.time()
        self._thread = threading.Thread(target=self._run, name="galrec-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        self.phase = Phase.IDLE

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ #
    # 外部指令
    # ------------------------------------------------------------------ #
    def bind_window(self, hwnd: int) -> bool:
        w = windows.find_window(hwnd=hwnd)
        if not w:
            return False
        with self._lock:
            self.hwnd = hwnd
            self.win = w
            self._grabber = None
            self._wgrabber = None
            self._win_grab_ok = None   # 换窗口了，后台抓图能力要重新探测
            self._win_grab_tried = 0.0
            self._auto_rel = None
            self._region_rel = self._cfg_rel()
        return True

    def attach_advancer(self, advancer) -> None:
        """挂上自动推进器（None 表示不推进）。"""
        self.advancer = advancer

    @property
    def choice_lines(self) -> list:
        """最近一次判定为「选项」的文字（界面显示/测试用）。"""
        return list(self._choice_lines)

    def force_start(self) -> None:
        """手动开录。"""
        if self.phase in (Phase.ARMED, Phase.COOLDOWN, Phase.IDLE):
            self._begin_segment(reason="manual")

    def force_stop(self) -> None:
        if self.phase is Phase.RECORDING:
            self._end_segment(StopReason.MANUAL)

    def abort_segment(self) -> None:
        """开录失败时用：把段落状态退回「等剧情开始」，**不**抛 segment_stop。

        不能走 force_stop()：那会抛一条 segment_stop，上层又去发一次 StopRecord，
        而 OBS 压根没开始录 → 白白撞一次 501，日志里还会多出一条「停录」。
        """
        if self.phase is not Phase.RECORDING:
            return
        self.phase = Phase.ARMED if self.hwnd else Phase.IDLE
        self._phase_ts = time.time()
        self._seg_start_ts = 0.0
        self._seg_wall_start = 0.0
        self._last_stop_ts = time.time()
        self._emit("phase", {"phase": self.phase.value, "note": "开录失败，已退回待命"})

    def pause_armed(self) -> None:
        """暂停监控（不移除窗口）。"""
        if self.phase is not Phase.RECORDING:
            self.phase = Phase.IDLE

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception as e:  # 任何异常都不能让监控线程死掉
                self._emit("error", {"message": f"检测循环异常：{e}"})
            spent = time.time() - t0
            wait = max(0.05, float(self.cfg.detect.poll_interval) - spent)
            self._stop.wait(wait)

    def _tick(self) -> None:
        d = self.cfg.detect

        # 1) 找到并确认窗口
        if not self.hwnd or not windows.is_alive(self.hwnd):
            if self.hwnd and self.phase is Phase.RECORDING:
                self._end_segment(StopReason.WINDOW)
                self._emit("window_lost", {})
            if not self._locate_window():
                self._set_phase(Phase.IDLE, note="未找到游戏窗口")
                self._snap(note="未找到游戏窗口")
                return
        if self.win is None or self._grabber is None:
            self.win = windows.find_window(hwnd=self.hwnd)
            if not self.win:
                self._set_phase(Phase.IDLE, note="窗口已关闭")
                return

        # 2) 最小化处理。
        #    ⚠ 这里**不能**默认就停录：OBS 的窗口采集 / 游戏采集在窗口最小化后
        #    往往照样能正常输出画面（DWM 还在合成），替用户把这段掐掉是错的。
        #    所以默认只在「最小化期间读不到台词」时冻结计时，录制继续。
        #    真遇到某些引擎最小化后确实只录到黑屏，可以在设置里打开
        #    stop_on_minimized 恢复旧行为。
        now0 = time.time()
        if self.win and windows.is_iconic(self.win.hwnd):
            if not self._minimized_since:
                self._minimized_since = now0
            self._emit_once_minimized()
            d2 = self.cfg.detect
            if d2.stop_on_minimized and self.phase is Phase.RECORDING and \
                    now0 - self._minimized_since >= d2.minimized_timeout:
                self._end_segment(StopReason.MINIMIZED)
                self._snap(note="游戏窗口最小化超时，已停录")
                return
            # 先试试走后门抓图：最小化时 PrintWindow 对一部分引擎仍然有效
            img_min = None
            if d2.occluded_fallback:
                if self._win_grab_ok is not True and \
                        (self._win_grab_ok is None or
                         now0 - self._win_grab_tried >= 60.0):
                    self._win_grab_tried = now0
                    self._win_grab_ok = capture.WindowGrabber(self.win.hwnd).probe()
                if self._win_grab_ok:
                    try:
                        img_min = capture.WindowGrabber(self.win.hwnd).grab()
                    except Exception:
                        img_min = None
            if img_min is not None and not capture.Grabber.is_blank(img_min):
                # 读得到画面：和正常流程一样读台词、照常计时
                text = ""
                if self.cfg.detect.ocr:
                    text = self._read_text(img_min)
                has_dlg = len(ocr.normalize(text)) >= self.cfg.detect.dialogue_min_chars
                new_line = self._merge_line(text, now0) if has_dlg else False
                if has_dlg:
                    self._no_text_ticks = 0
                self.latest_frame = img_min
                if self.phase in (Phase.ARMED, Phase.COOLDOWN):
                    self._maybe_begin(False, has_dlg, new_line, img_min, "最小化·后台抓图")
                elif self.phase is Phase.RECORDING:
                    self._check_end(False, new_line, now0, text, occluded=False)
                self._snap(text=text or self._last_text,
                           note="游戏窗口最小化，仍在后台抓图检测（录制不受影响）")
                return
            # 抓不到画面：冻结计时，但**不停录**
            self._shift_clock(max(0.0, now0 - (self._last_tick_ts or now0)))
            self._last_tick_ts = now0
            # 冻结的是「剧情推进」计时，不是安全上限——录太久照旧要收
            if self.phase is Phase.RECORDING and d2.max_record_seconds > 0 and \
                    now0 - (self._seg_wall_start or now0) >= d2.max_record_seconds:
                self._end_segment(StopReason.MAXDUR)
                return
            self._snap(note="游戏窗口已最小化，暂停检测计时（录制继续）")
            return
        self._minimized_since = 0.0
        self._minimized_warned = False
        self._last_tick_ts = now0

        # 3) 确定采样区域
        bbox, region_desc = self._resolve_bbox()
        if not bbox:
            self._snap(note="窗口无有效客户区")
            return
        if self._grabber is None or self._grabber.bbox != bbox:
            self._grabber = capture.Grabber(bbox)

        # 3.5) 遮挡检查必须放在抓帧之前。
        #      窗口被别的窗口盖住时，屏幕截图拿到的是「盖在上面的那个窗口」，
        #      不是游戏画面。先判断有没有被挡，再决定这一帧从哪条通道取：
        #        · 没被挡  → 屏幕截图（最快，也不会漏掉独占全屏的情况）
        #        · 被挡住  → 后门抓图（PrintWindow），直接向窗口要它自己的画面
        occluded = False
        if d.detect_occlusion:
            occluded = not windows.is_region_on_top(self.win.hwnd, bbox)
        now_occl = time.time()

        # 3.6) 被遮挡时尝试走后门。能不能走通因引擎而异，第一次探测一次就记住。
        #      探测失败不等于永远失败：有些游戏只在独占全屏时抓不到，
        #      切成窗口模式就好了，所以隔一阵再试一次。
        use_window_grab = False
        if occluded and d.occluded_fallback:
            if self._win_grab_ok is not True:
                if self._win_grab_ok is None or \
                        now_occl - self._win_grab_tried >= 60.0:
                    self._win_grab_tried = now_occl
                    self._win_grab_ok = capture.WindowGrabber(self.win.hwnd).probe()
                    if self._win_grab_ok:
                        self._emit("info", {"message": (
                            "窗口被遮挡，已改从窗口后台抓图（PrintWindow）"
                            "——照常读台词、照常计时")})
                    else:
                        self._emit("info", {"message": (
                            "窗口被遮挡，这个游戏暂时不支持后台抓图，"
                            "先冻结计时等它露出来（过一会儿会再试）")})
            use_window_grab = bool(self._win_grab_ok)

        if use_window_grab:
            # 子区域用「相对客户区」的坐标：屏幕 bbox 减掉客户区原点
            cl = windows.client_rect(self.win.hwnd)
            sub = (bbox[0] - cl[0], bbox[1] - cl[1],
                   bbox[2] - cl[0], bbox[3] - cl[1])
            if self._wgrabber is None or self._wgrabber.hwnd != self.win.hwnd \
                    or self._wgrabber.sub != sub:
                self._wgrabber = capture.WindowGrabber(self.win.hwnd, sub=sub)
            diff, _fp, img = self._wgrabber.update()
            # 两条通道的指纹不能混着比，切回来时重置，否则第一帧会假报「变化很大」
            if self._grabber is not None:
                self._grabber.reset()
            if img is not None:
                # 后门抓到了真画面，那就等于「看得见」：不冻结计时，也不跳 OCR
                occluded = False
            else:
                use_window_grab = False
        if not use_window_grab:
            diff, _fp, img = self._grabber.update()
            if self._wgrabber is not None:
                self._wgrabber.reset()

        if img is not None:
            self.latest_frame = img
        now = time.time()
        changed = diff >= d.change_threshold

        # 遮挡计时的维护：真正「看不见」才算遮挡
        if occluded:
            if not self._occluded_since:
                self._occluded_since = now
        else:
            if self._occluded_since:
                # 遮挡结束：把遮挡的时长从「静默计时」里扣掉。
                # 不然切走两分钟回来，程序会认为剧情已停两分钟，立刻停录。
                self._shift_clock(now - self._occluded_since)
            self._occluded_since = 0.0

        blank = False
        # 被遮挡且没走后门时，img 是别人窗口的画面，判黑白毫无意义，跳过
        if img is not None and (self.phase is Phase.RECORDING or changed) \
                and (use_window_grab or not occluded):
            blank = capture.Grabber.is_blank(img)
            self._blank_streak = self._blank_streak + 1 if blank else 0

        # 4) 按需 OCR。
        #    「被遮挡」且没走到后台抓图时完全不读——那张图是别人窗口的画面，
        #    读进来就是外面的文字。走到后台抓图就照常读，那已经是游戏自己的画面。
        text = ""
        has_dialogue = False
        new_line = False
        can_read = (not occluded) or use_window_grab
        if d.ocr and img is not None and can_read and \
                now - self._last_ocr_ts >= d.ocr_interval:
            if changed or now - self._last_ocr_ts >= max(3.0, d.ocr_interval * 3):
                self._last_ocr_ts = now
                text = self._read_text(img)
                has_dialogue = len(ocr.normalize(text)) >= d.dialogue_min_chars
                if has_dialogue:
                    self._no_text_ticks = 0
                    new_line = self._merge_line(text, now)
                else:
                    self._no_text_ticks += 1
                    if self.phase is Phase.RECORDING:
                        self._maybe_auto_region(threshold=d.ocr_interval * 2)

        if changed and not occluded:
            self._last_change_ts = now
            self._change_streak += 1
        else:
            self._change_streak = 0

        # 5) 状态迁移
        if self.phase in (Phase.ARMED, Phase.COOLDOWN):
            self._maybe_begin(changed and not occluded, has_dialogue, new_line,
                              img, region_desc)
        elif self.phase is Phase.RECORDING:
            self._check_end(changed and not occluded, new_line, now, text,
                            occluded=occluded)

        # 6) 自动推进（与录制状态无关，只看剧情有没有停住民等输入）
        if self.advancer is not None and not occluded:
            self._maybe_advance(now, img, text, bbox)

        note = "被遮挡" if occluded else region_desc
        if occluded and self._occluded_since:
            note = f"游戏窗口被遮挡 {int(now - self._occluded_since)}s"
        elif use_window_grab:
            note = f"窗口被遮挡·后台抓图中（{region_desc}）"
        if self.win and windows.is_iconic(self.win.hwnd):
            note = "游戏窗口已最小化（录制继续，检测计时暂停）"
        self._snap(diff=diff, text=text or self._last_text,
                   region=bbox, note=note)

    # ------------------------------------------------------------------ #
    # 自动推进
    # ------------------------------------------------------------------ #
    def _emit_once_minimized(self) -> None:
        """最小化只提示一次，别每 0.6 秒刷一条日志。"""
        if not self._minimized_warned:
            self._minimized_warned = True
            if self.cfg.detect.stop_on_minimized:
                msg = ("游戏窗口已最小化——已设置「最小化就停录」，"
                       f"超过 {self.cfg.detect.minimized_timeout:.0f} 秒会停止录制")
            else:
                msg = ("游戏窗口已最小化。录制**不会**因此停止"
                       "（OBS 的窗口/游戏采集通常能照常录到画面）")
            self._emit("info", {"message": msg})

    def _maybe_advance(self, now: float, img, text: str, bbox: tuple) -> None:
        adv = self.advancer
        if adv is None or not adv.active:
            return
        d = self.cfg.detect
        if now - self._last_advance_ts < max(0.4, self.cfg.advance.min_interval):
            return

        # 台词已经稳定多久（从最后一次「有新内容」算起）
        stable = now - max(self._last_progress_ts, self._last_text_ts)

        # 顺便看一眼是不是选项界面：看整窗，而不是只看对话框
        choice_lines = []
        if self.cfg.advance.stop_on_choice and img is not None \
                and self._should_check_choice(now):
            choice_lines = self._detect_choices(self.win.hwnd if self.win else 0)
        self._choice_lines = choice_lines

        ok = adv.maybe_advance(
            hwnd=self.hwnd, text=text or self._last_text,
            text_stable=stable, choice_lines=choice_lines,
            dialogue_bbox=bbox,
        )
        if ok:
            self._last_advance_ts = now
            # 按完就当「有新进展」，避免立刻又判定结束
            self._last_progress_ts = max(self._last_progress_ts, now)
            self._emit("advance", {"count": adv.state.actions})

    def _should_check_choice(self, now: float) -> bool:
        """选项检测要跑整窗 OCR（贵），限制频率。"""
        if not self.cfg.detect.choice_check:
            return False
        last = getattr(self, "_choice_check_ts", 0.0)
        gap = self.cfg.detect.choice_check_interval or \
            max(1.5, self.cfg.advance.min_interval)
        if now - last < gap:
            return False
        self._choice_check_ts = now
        return True

    def _detect_choices(self, hwnd: int) -> list:
        """在整窗截图里找「选项列表」。"""
        try:
            if not hwnd:
                return []
            cl = windows.client_rect(hwnd)
            img = capture.Grabber(cl).grab()
            if img is None:
                return []
            lines = ocr.recognize(img, min_conf=max(0.5, self.cfg.detect.ocr_min_conf))
            if not lines:
                return []
            # 对话框区域要排除，那里面的是台词
            dbox = None
            if self._grabber:
                gb = self._grabber.bbox
                dbox = (gb[0] - cl[0], gb[1] - cl[1], gb[2] - cl[0], gb[3] - cl[1])
            is_choice, texts = adv_choice_check(lines, img.size, dbox)
            if is_choice:
                self._emit("choice", {"lines": texts})
            return texts if is_choice else []
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # 台词合并（对付打字机）
    # ------------------------------------------------------------------ #
    def _shift_clock(self, seconds: float) -> None:
        """把所有「时间戳」往前推，等价于把这段时间从计时里抹掉。

        ⚠ 只推 seconds，绝不能多推（旧版多推了 0.5 秒）：
        遮挡抖动时会一秒一次地反复进出这个分支，每次都多推 0.5 秒，
        累计几分钟后 `_last_progress_ts` 就被推到未来去了，
        `idle = now - _last_progress_ts` 永远小于阈值——于是**剧情早就结束
        （甚至已经回到标题）却永远不停录**。
        """
        if seconds <= 0:
            return
        for attr in ("_last_progress_ts", "_last_text_ts", "_seg_start_ts",
                     "_last_change_ts", "_last_dialogue_ts"):
            v = getattr(self, attr, 0.0)
            if v:
                setattr(self, attr, v + seconds)

    def _merge_line(self, text: str, now: float) -> bool:
        """判断这次读到的是「新的一句」还是「同一句的更多字」。

        打字机效果会让同一句不断变长：`你这家伙` → `你这家伙，真是让` → `……啊。`
        若把每次变长都算新台词，一句 20 字的台词会被数成十几次「推进」，
        导致「剧情早已结束，却因为还在增长而不停录」。

        这里维护 `_burst_best`：同一句里只保留最完整（最长）的形态。
        真正算新台词的条件是：相似度低，或者「短到像是换了一句」。
        """
        cur = ocr.normalize(text)
        best = ocr.normalize(self._burst_best)
        self._last_dialogue_ts = now      # 「最后一次确实读到台词」的时刻
        if not self._last_text:
            self._burst_best = text
            self._last_text = text
            self._last_text_ts = now
            self._last_progress_ts = now
            return True

        sim = ocr.similarity(text, self._burst_best or self._last_text)

        # 同一句在生长：当前比见过的更长，且是包含关系 → 只是打字机，不算新句
        grew = len(cur) > len(best) and (best in cur or sim >= 0.55)
        # 明显是另一句
        switched = sim < 0.5 and not grew
        # 同一句但变短（打字机重开 / OCR 抖了一下）——不算新句
        shrunk_same = sim >= 0.6

        if switched:
            self._burst_best = text
            self._last_text = text
            self._last_text_ts = now
            self._last_progress_ts = now
            self._emit("line", {"text": text})
            return True

        if grew:
            # 打字机在走：更新最完整形态，并且算作「剧情仍在推进」
            self._burst_best = text
            self._last_text = text
            self._last_text_ts = now
            self._last_progress_ts = now
            self._emit("line_progress", {"text": text})
            return False

        if shrunk_same:
            self._last_text = text
            # 同一句没有实质推进，不刷新 progress 时间戳
            return False

        # 落在中间地带：当成同一句，避免抖动被误判为新台词
        self._last_text = text
        return False

    # ------------------------------------------------------------------ #
    # 开始判定
    # ------------------------------------------------------------------ #
    def _maybe_begin(self, changed: bool, has_dialogue: bool,
                     new_line: bool, img, region_desc: str) -> None:
        d = self.cfg.detect
        mode = self.cfg.record.start_mode
        if mode == "manual":
            return
        if mode == "on_window":
            if self._change_streak >= max(1, d.confirm_ticks):
                self._begin_segment(reason="window")
            return
        # on_dialogue
        if d.require_dialogue:
            if not (has_dialogue and new_line):
                return
            # 刚停录的那句话不能立刻触发重开，否则会在原地反复起停
            if self._last_stop_text and ocr.similarity(self._last_stop_text, self._last_text) > 0.85:
                return
            # 停录后的冷却期：回标题画面时 UI 一抖就重新开录，会切出一堆废片
            cd = float(self.cfg.detect.cooldown_seconds or 0)
            if cd > 0 and time.time() - (self._last_stop_ts or 0) < cd:
                return
            self._begin_segment(reason="dialogue")
        else:
            if changed and self._change_streak >= max(1, d.confirm_ticks):
                cd = float(self.cfg.detect.cooldown_seconds or 0)
                if cd > 0 and time.time() - (self._last_stop_ts or 0) < cd:
                    return
                self._begin_segment(reason="activity")

    # ------------------------------------------------------------------ #
    # 结束判定
    # ------------------------------------------------------------------ #
    def _check_end(self, changed: bool, new_line: bool, now: float, text: str,
                   occluded: bool = False) -> None:
        d = self.cfg.detect
        seg_elapsed = now - (self._seg_wall_start or self._seg_start_ts)

        # 保护：最长录制时长
        if d.max_record_seconds > 0 and seg_elapsed >= d.max_record_seconds:
            self._end_segment(StopReason.MAXDUR)
            return

        # 保护：窗口画面全黑太久。
        # 注意这条只看「录到的是不是纯色画面」，和「最小化」是两件事：
        # 最小化在 _tick 里已经单独处理（默认不停录）。
        if self._blank_streak >= 30:
            self._end_segment(StopReason.WINDOW)
            return

        # 被遮挡时冻结计时：这不是剧情停滞，是我们看不见
        if occluded:
            if d.occlusion_timeout > 0 and self._occluded_since and \
                    now - self._occluded_since >= d.occlusion_timeout:
                self._end_segment(StopReason.OCCLUDED)
            return

        # 宽限期内不判结束
        if seg_elapsed < d.start_grace:
            return

        # 「只手动停」：除了硬性安全阀（单段上限/窗口没了/画面全黑），
        # 一律不许自动停。这个选项以前在界面上有、代码里压根没读，等于假的。
        manual_only = (self.cfg.record.stop_mode == "manual")

        # 结束判定只看「有没有新台词」。
        # 像素变化不能作为推进信号——动态背景、Live2D 呼吸、UI 闪烁一直在动，
        # 一旦把「画面在动」当成剧情推进，就永远不会停录。像素只用来决定要不要 OCR。
        idle = now - self._last_progress_ts
        # 防御：时间戳被推到未来（历史上的 _shift_clock 重复加 eps 就会这样）
        # 会让 idle 变成负数、永远到不了阈值。发现就当成「刚刚才推进过」，
        # 让计时从现在重新开始，而不是一直卡着不停录。
        if idle < -1.0:
            self._last_progress_ts = now
            self._last_text_ts = min(self._last_text_ts or now, now)
            self._seg_start_ts = min(self._seg_start_ts, now)
            self._emit("info", {"message": "检测到计时时钟漂移（时间戳超出当前时刻），"
                                           "已自动校正"})
            idle = 0.0
        self._idle = max(0.0, idle)

        # 台词框长时间一个字都读不到：多半是回到了标题/主菜单/纯 CG 过场。
        # 这条比 idle_timeout 敏感得多，解决「剧情结束、甚至回到标题了还在录」。
        if not manual_only and self.cfg.record.auto_stop_on_title and \
                self._last_dialogue_ts and d.text_lost_timeout > 0 and \
                now - self._last_dialogue_ts >= d.text_lost_timeout:
            self._end_segment(StopReason.TEXT_LOST)
            return

        # 回到标题/主菜单：不必等 idle 超时，直接停。
        # 只在「冷静了一会儿」之后才跑整窗 OCR，避免录制中每帧都花 1 秒。
        if not manual_only and self.cfg.record.auto_stop_on_title:
            probe_after = min(max(20.0, d.idle_timeout * 0.35),
                              max(20.0, d.idle_timeout - 10.0)) \
                if d.idle_timeout > 0 else 20.0
            if self._idle >= probe_after and self._title_check_due(now):
                if self._looks_like_title_screen():
                    self._end_segment(StopReason.TITLE)
                    return

        if manual_only:
            return
        if d.idle_timeout > 0 and self._idle >= d.idle_timeout:
            self._end_segment(StopReason.IDLE)

    # ------------------------------------------------------------------ #
    # 标题 / 主菜单识别
    # ------------------------------------------------------------------ #
    #: 标题画面常见的菜单词（中/日/英三套，galgame 基本跑不出这些）
    _TITLE_WORDS = (
        "开始游戏", "新游戏", "新的游戏", "继续游戏", "继续", "载入", "读取存档",
        "加载存档", "存档", "退出游戏", "退出", "设置", "设定", "配置", "画廊",
        "鉴赏", "回想", "额外内容", "特典", "安装",
        "NEW GAME", "NEWGAME", "START", "CONTINUE", "LOAD", "SAVE",
        "CONFIG", "SETTINGS", "OPTIONS", "EXTRA", "GALLERY", "EXIT", "QUIT",
        "はじめから", "つづきから", "ろーど", "ロード", "コンフィグ",
        "せってい", "おまけ", "ギャラリー", "しゅうりょう", "タイトルへ",
    )

    def _title_check_due(self, now: float) -> bool:
        """整窗 OCR 很贵（0.5~1.5 秒），限制频率。"""
        last = getattr(self, "_title_check_ts", 0.0)
        if now - last < 4.0:
            return False
        self._title_check_ts = now
        return True

    def _looks_like_title_screen(self) -> bool:
        """判断当前画面是不是标题/主菜单。

        依据：整窗里出现 ≥2 个「菜单词」，且这些词都比较短（菜单项不会是一长句），
        并且对话框区域里没有台词。三条同时成立才判——宁可漏判也不能误判，
        误判会在剧情中途把录像掐断。
        """
        if self._occluded_since or not self.win:
            return False
        try:
            cl = windows.client_rect(self.win.hwnd)
            img = capture.Grabber(cl).grab()
            if img is None or capture.Grabber.is_blank(img):
                return False
            lines = ocr.recognize(img, min_conf=max(0.5, self.cfg.detect.ocr_min_conf))
            if not lines:
                return False
            hits = set()
            for l in lines:
                core = ocr.normalize(l.text).upper()
                if not core or len(core) > 24:
                    continue        # 菜单项都很短
                for w in self._TITLE_WORDS:
                    if ocr.normalize(w).upper() and ocr.normalize(w).upper() in core:
                        hits.add(w)
            # 对话框里还有台词 → 是在剧情里，不是标题
            if self._grabber is not None:
                gb = self._grabber.bbox
                sub = (gb[0] - cl[0], gb[1] - cl[1], gb[2] - cl[0], gb[3] - cl[1])
                if sub[2] - sub[0] > 4 and sub[3] - sub[1] > 4:
                    box = img.crop(sub)
                    dlg = ocr.dialogue_text(
                        ocr.recognize(box, min_conf=self.cfg.detect.ocr_min_conf),
                        min_conf=self.cfg.detect.ocr_min_conf)
                    if len(ocr.normalize(dlg)) >= self.cfg.detect.dialogue_min_chars:
                        return False
            return len(hits) >= 2
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 段落起停
    # ------------------------------------------------------------------ #
    def _begin_segment(self, reason: str) -> None:
        now = time.time()
        self._seg_start_ts = now
        self._seg_wall_start = now      # 真实墙钟起点（不被 _shift_clock 影响，单段上限用）
        self._minimized_since = 0.0
        self._minimized_warned = False
        self._last_progress_ts = now
        self._last_change_ts = now
        self._last_dialogue_ts = now
        self._last_ocr_ts = 0.0
        self._blank_streak = 0
        self._occluded_since = 0.0
        self._burst_best = ""
        self._segment_start_text = self._last_text
        self._segments += 1
        self._set_phase(Phase.RECORDING)
        self._emit("segment_start", {
            "reason": reason, "index": self._segments,
            "text": self._last_text,
        })

    def _end_segment(self, reason: StopReason) -> None:
        now = time.time()
        if self.phase is not Phase.RECORDING:
            # 已经不是录制中了：重复的结束事件不该再发一遍（会触发重复 StopRecord → 501）
            return
        dur = now - self._seg_start_ts
        self._last_stop_text = self._last_text
        self._last_stop_ts = now
        self._set_phase(Phase.COOLDOWN if self.cfg.detect.segmented else Phase.IDLE)
        self._emit("segment_stop", {
            "reason": reason.value, "duration": round(dur, 1),
            "index": self._segments, "text": self._last_text,
        })
        if reason in (StopReason.WINDOW, StopReason.MAXDUR):
            self._set_phase(Phase.IDLE, note=reason.value)

    def _set_phase(self, ph: Phase, note: str = "") -> None:
        if self.phase is not ph:
            self.phase = ph
            self._phase_ts = time.time()
            self._emit("phase", {"phase": ph.value, "note": note})

    # ------------------------------------------------------------------ #
    # 区域解析
    # ------------------------------------------------------------------ #
    def _cfg_rel(self):
        r = self.cfg.detect.region_rel
        if isinstance(r, (list, tuple)) and len(r) == 4:
            try:
                x0, y0, w, h = [float(v) for v in r]
                return (x0, y0, x0 + w, y0 + h)
            except Exception:
                return None
        return None

    def _resolve_bbox(self):
        """返回 (绝对像素 bbox, 描述文字)。"""
        if not self.win:
            return None, ""
        cl = windows.client_rect(self.win.hwnd) if windows.is_alive(self.win.hwnd) else self.win.rect
        # 有些游戏窗口客户区取不到，退化为整窗
        if cl[2] - cl[0] < 100 or cl[3] - cl[1] < 100:
            cl = self.win.rect
        cw, ch = cl[2] - cl[0], cl[3] - cl[1]
        if cw <= 0 or ch <= 0:
            return None, ""

        src = "自动"
        rel = self._region_rel
        if rel is None:
            rel = self._cfg_rel()
            if rel is not None:
                src = "指定"
        if rel is None:
            rel = self._auto_rel
        if rel is None and self.cfg.detect.auto_region:
            # 自动探测要跑一次整窗 OCR（1~3 秒），绝不能每个采样周期都试。
            # 只在「画面刚变过」时试——没有变化的窗口里必然也找不到台词，
            # 白跑一次 OCR 只会拖慢检测线程。
            now = time.time()
            occluded_now = bool(self._occluded_since)
            recent_change = (now - self._last_change_ts) <= 8.0
            due = now - self._auto_try_ts >= max(3.0, self.cfg.detect.auto_region_retry)
            if not occluded_now and recent_change and due:
                self._auto_try_ts = now
                rel = self._detect_auto_region(cl, quiet=True)
                self._auto_rel = rel
                if rel is not None:
                    src = "自动"
        if rel is None:
            # 兜底：取窗口下半部分（绝大多数 galgame 台词都在下方）
            rel = (0.02, 0.62, 0.98, 0.99)
            src = "默认"

        x0 = int(cl[0] + rel[0] * cw)
        y0 = int(cl[1] + rel[1] * ch)
        x1 = int(cl[0] + rel[2] * cw)
        y1 = int(cl[1] + rel[3] * ch)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(x1, cl[2]), min(y1, cl[3])
        if x1 - x0 < 20 or y1 - y0 < 12:
            return None, "区域无效"
        return (x0, y0, x1, y1), src

    def _detect_auto_region(self, cl: tuple, quiet: bool = False):
        """在整窗截图里自动找对话框位置，转成比例坐标。"""
        try:
            img = capture.Grabber(cl).grab()
            if img is None:
                return None
            box = ocr.detect_dialogue_region(img)
            if not box:
                if not quiet:
                    self._emit("info", {"message": "没找到对话框文字，暂用窗口下半部分"})
                return None
            cw, ch = cl[2] - cl[0], cl[3] - cl[1]
            rel = (box[0] / cw, box[1] / ch, box[2] / cw, box[3] / ch)
            self._emit("region", {"relative": [round(v, 3) for v in rel],
                                  "pixels": list(box), "size": [cw, ch]})
            return rel
        except Exception:
            return None

    def _maybe_auto_region(self, threshold: float) -> None:
        """录制中连续认不出字 → 可能界面变了（换场景/换对话框皮肤），重探一次。"""
        if not self.cfg.detect.auto_region or self._region_rel is not None:
            return
        if self._no_text_ticks < 8:
            return
        self._no_text_ticks = 0
        if self._occluded_since:
            return
        now = time.time()
        if now - self._auto_try_ts < max(5.0, self.cfg.detect.auto_region_retry):
            return
        self._auto_try_ts = now
        if self.win:
            cl = windows.client_rect(self.win.hwnd)
            rel = self._detect_auto_region(cl, quiet=True)
            if rel:
                self._auto_rel = rel
                self._grabber = None

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _read_text(self, img: Image.Image) -> str:
        if img is None:
            return ""
        lines = ocr.recognize(img, min_conf=self.cfg.detect.ocr_min_conf)
        return ocr.dialogue_text(lines, min_conf=self.cfg.detect.ocr_min_conf)

    def _locate_window(self) -> bool:
        d = self.cfg.detect
        if self.cfg.record.start_mode == "manual" and not self.hwnd:
            return False
        w = None
        if d.window_title or d.process_name:
            w = windows.find_window(title_sub=d.window_title, process=d.process_name)
        if not w and d.use_foreground:
            w = windows.get_foreground()
        if w:
            self.hwnd = w.hwnd
            self.win = w
            self._grabber = None
            self._wgrabber = None
            self._win_grab_ok = None
            self._win_grab_tried = 0.0
            self._auto_rel = None
            self._emit("window", {"hwnd": w.hwnd, "title": w.title,
                                  "process": w.process, "size": list(w.size)})
            return True
        return False

    def _snap(self, diff: float = None, text: str = "", region=None, note: str = "") -> None:
        now = time.time()
        s = Snapshot(
            phase=self.phase.value,
            window=self.win.label() if self.win else "",
            region=tuple(region) if region else (),
            diff=float(diff if diff is not None else self._last_snap.diff),
            text=text,
            text_age=(now - self._last_text_ts) if self._last_text_ts else 0.0,
            idle=(now - self._last_progress_ts) if self._last_progress_ts else 0.0,
            elapsed=(now - self._seg_start_ts) if self.phase is Phase.RECORDING else 0.0,
            segments=self._segments,
            note=note,
        )
        self._last_snap = s
        self._on_snapshot(s)

    def _emit(self, event: str, payload: dict) -> None:
        try:
            self._on_event(event, payload)
        except Exception:
            pass

    # 供界面查询
    @property
    def snapshot(self) -> Snapshot:
        return self._last_snap
