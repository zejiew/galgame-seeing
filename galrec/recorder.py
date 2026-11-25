"""总调度：把「检测到剧情开始/结束」翻译成「OBS 开录/停录」。

这一层负责所有副作用：连 OBS、改录制目录、起停录制、文件命名、清理短录像。
检测线程只抛事件，不碰 OBS——这样界面卡不卡都不影响判定。

后端可以是 WebSocket 也可以是热键（见 backend.py），这里只调用统一接口。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import backend as backendmod
from . import obs as obsmod
from . import windows
from .advance import AutoAdvancer
from .config import Config
from .detector import Detector, Phase, Snapshot, StopReason


@dataclass
class Session:
    """一段录制记录。"""
    index: int
    path: str = ""
    started: float = 0.0
    stopped: float = 0.0
    reason: str = ""
    text: str = ""
    duration: float = 0.0        # 实测时长（停录时定死）

    def live_duration(self) -> float:
        if self.duration:
            return self.duration
        return max(0.0, (self.stopped or time.time()) - self.started)


class Controller:
    """界面与检测器之间的中介。所有跨线程状态都在这里收口。"""

    def __init__(self, cfg: Config,
                 log: Optional[Callable[[str, str], None]] = None,
                 on_snapshot: Optional[Callable[[Snapshot], None]] = None,
                 on_sessions: Optional[Callable[[List[Session]], None]] = None):
        self.cfg = cfg
        self._log = log or (lambda msg, level="info": None)
        self._on_snapshot = on_snapshot or (lambda s: None)
        self._on_sessions = on_sessions or (lambda l: None)

        self.backend: Optional[backendmod.BaseBackend] = None
        self.detector = Detector(cfg, on_event=self._on_detect_event,
                                 on_snapshot=self._snapshot)
        self.advancer = AutoAdvancer(cfg, log=self._log,
                                     on_pause=self._on_advance_pause)
        self.detector.attach_advancer(self.advancer)
        self.sessions: List[Session] = []
        self._lock = threading.RLock()
        self._obs_lock = threading.RLock()
        self._files_before: Dict[str, float] = {}
        self._current: Optional[Session] = None
        self._auto_launched = False
        self._run_lock = threading.Lock()
        self._stop_guard = threading.Lock()
        self._stopping = False
        self._pending_stop = False
        self._pending_reason: Optional[StopReason] = None
        self._last_stop_ts = 0.0     # 上次真正停录的时刻（开录用它算冷却间隔）
        self.running = False
        self.on_ui_event = None      # 界面可赋值：(kind, payload) -> None

    # ------------------------------------------------------------------ #
    # 对外：启动 / 停止监控
    # ------------------------------------------------------------------ #
    def start(self, hwnd: int = 0) -> bool:
        if self.running:
            return True
        if not self._ensure_obs():
            return False
        self.detector.cfg = self.cfg
        self.advancer.cfg = self.cfg
        self.detector.start(hwnd=hwnd)
        self.running = True
        if self.cfg.advance.enabled:
            self.advancer.set_enabled(True)
        self._log("监控已启动，正在等待剧情…", "ok")
        return True

    def stop(self) -> None:
        if self.detector.running:
            self.detector.stop()
        if self.backend and self.backend.connected:
            try:
                if self.backend.recording():
                    self._stop_record(StopReason.MANUAL)
                else:
                    # 有段落记录但 OBS 那边已经没在录：可能是 OBS 自己崩过/手动停过，
                    # 状态要对齐，不然界面会一直显示「录制中」
                    with self._lock:
                        if self._current is not None:
                            self._close_session_wo_obs(StopReason.MANUAL)
            except Exception:
                pass
        self.running = False
        self._log("监控已停止", "info")

    # ------------------------------------------------------------------ #
    # 对外：手动操作
    # ------------------------------------------------------------------ #
    def manual_start_record(self) -> None:
        self.detector.force_start()

    def manual_stop_record(self) -> None:
        self.detector.force_stop()

    # ---------------- 自动推进 ---------------- #
    def set_auto_advance(self, on: bool) -> None:
        self.advancer.cfg = self.cfg
        self.advancer.set_enabled(on)
        if on:
            self._log("自动推进已开启（遇到选项会自动暂停）", "warn")
        else:
            self._log("自动推进已关闭", "info")

    def resume_auto_advance(self) -> None:
        self.advancer.resume()

    def pause_auto_advance(self, why: str = "手动") -> None:
        self.advancer.pause(why)

    @property
    def advance_state(self):
        return self.advancer.state

    def _on_advance_pause(self, reason: str, detail: str) -> None:
        self._emit_ui("advance_pause", {"reason": reason, "detail": detail})

    @property
    def obs_connected(self) -> bool:
        return bool(self.backend and self.backend.connected)

    def backend_label(self) -> str:
        if not self.backend:
            return "未连接"
        return self.backend.label

    def test_connection(self) -> obsmod.ObsStatus:
        """给界面「连接测试」用，返回统一状态。"""
        st = obsmod.ObsStatus()
        if self.backend and self.backend.connected:
            st.connected = True
            st.protocol = self.backend.name
            st.recording = self.backend.recording()
            st.duration = self.backend.duration()
            st.record_dir = self.backend.record_dir()
            st.obs_version = getattr(
                getattr(self.backend, "client", None), "obs_version", "") or self.backend.label
            return st
        # 冷探测：WebSocket 走 probe，热键模式直接认为可用
        mode = (self.cfg.obs.backend or "auto").lower()
        if mode in ("auto", "websocket"):
            st = obsmod.probe(self.cfg.obs.host, self.cfg.obs.port,
                              self.cfg.obs.password, self.cfg.obs.protocol)
            if st.connected:
                return st
        if mode in ("auto", "hotkey") and self.cfg.obs.hotkey_start:
            from . import hotkey as _hk
            ok = bool(self.cfg.obs.hotkey_stop) and \
                _hk.parse_accel(self.cfg.obs.hotkey_start) is not None and \
                _hk.parse_accel(self.cfg.obs.hotkey_stop) is not None
            if ok:
                st.connected = True
                st.protocol = "hotkey"
                st.obs_version = f"热键 {self.cfg.obs.hotkey_start}"
                st.record_dir = self.cfg.record_dir()
                return st
        return st

    # ------------------------------------------------------------------ #
    # OBS 连接 / 自动拉起
    # ------------------------------------------------------------------ #
    def _ensure_obs(self) -> bool:
        with self._obs_lock:
            if self.backend and self.backend.connected:
                return True
            be = backendmod.make_backend(self.cfg)
            if be.connected:
                self.backend = be
                self._after_connect()
                return True

            # 连不上：如果配了热键，自动退到热键后端
            if be.name == "websocket" and self.cfg.obs.hotkey_start:
                hkb = backendmod.HotkeyBackend(self.cfg)
                if hkb.connect():
                    self.backend = hkb
                    self._log(f"OBS WebSocket 未就绪，已切换到热键模式"
                              f"（{self.cfg.obs.hotkey_start}）", "warn")
                    self._after_connect()
                    return True

            err = getattr(getattr(be, "client", None), "last_error", "") or \
                "OBS 未连接"
            self._log(err, "error")

            if self.cfg.obs.auto_launch and not self._auto_launched:
                self._auto_launched = True
                exe = self.cfg.resolve_path(self.cfg.obs.exe_path)
                if os.path.isfile(exe):
                    self._log(f"正在启动 OBS：{exe}", "info")
                    if windows.launch_exe(exe):
                        for _ in range(40):
                            time.sleep(1.5)
                            be2 = backendmod.make_backend(self.cfg)
                            if be2.connected:
                                self.backend = be2
                                self._log("OBS 已就绪", "ok")
                                self._after_connect()
                                return True
                        self._log("OBS 启动了但连不上。请检查 "
                                  "工具 → WebSocket 服务器设置（OBS 28+），"
                                  "或在设置页改用「热键」模式。", "error")
                else:
                    self._log(f"找不到 OBS 程序：{exe}", "error")
            return False

    def _after_connect(self) -> None:
        be = self.backend
        if not be:
            return
        self._log(f"已连接 OBS（{be.label}）", "ok")
        # 自动发现的端口写回配置，下次直接命中
        auto_port = getattr(be, "port_auto", 0)
        if auto_port and auto_port != self.cfg.obs.port:
            self._log(f"注意到 OBS 实际监听 {auto_port} 端口，已自动采用"
                      f"（OBS 27 的 WebSocket 插件默认是 4444）", "info")
            self.cfg.obs.port = auto_port
        if isinstance(be, backendmod.WsBackend):
            info = be.prepare()
            if info:
                self._log(info, "info")

    def check_obs(self) -> None:
        """界面点「连接测试」时用：真正做一次连接，不只查缓存。"""
        with self._obs_lock:
            if self.backend and self.backend.connected:
                return
            be = backendmod.make_backend(self.cfg)
            if be.connected:
                self.backend = be
                self._after_connect()
            else:
                err = getattr(getattr(be, "client", None), "last_error", "")
                if err:
                    self._log(err, "error")

    # ------------------------------------------------------------------ #
    # 事件：来自检测线程
    # ------------------------------------------------------------------ #
    def _on_detect_event(self, event: str, payload: dict) -> None:
        if event == "segment_start":
            self._start_record(payload)
        elif event == "segment_stop":
            try:
                reason = StopReason(payload.get("reason", "manual"))
            except ValueError:
                reason = StopReason.MANUAL
            # 停录要等宽限，放到后台线程，别把检测线程堵住。
            # ⚠ 这里**不能**因为「已经有一个停录在跑」就把这次丢掉：
            # 旧版用 _run_lock 非阻塞抢锁，抢不到直接 return，
            # 于是「剧情结束该停录」这件事被静默吞掉，一直录到单段上限。
            self._request_stop(reason)
        elif event == "window":
            self._log(f"已锁定窗口：{payload.get('title')} "
                      f"[{payload.get('process')}] {payload.get('size')}", "info")
        elif event == "window_lost":
            self._log("游戏窗口已关闭", "warn")
        elif event == "region":
            self._log(f"自动识别对话框区域：{payload.get('pixels')}", "info")
        elif event == "choice":
            self._log(f"画面疑似出现选项：{payload.get('lines')}", "warn")
            self._emit_ui("choice", payload)
        elif event == "error":
            self._log(payload.get("message", "未知错误"), "error")
        elif event == "info":
            self._log(payload.get("message", ""), "info")

    def _snapshot(self, snap: Snapshot) -> None:
        self._on_snapshot(snap)

    def _emit_ui(self, kind: str, payload: dict) -> None:
        """把非快照类事件推给界面。"""
        try:
            fn = getattr(self, "on_ui_event", None)
            if callable(fn):
                fn(kind, payload)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 录制起停
    # ------------------------------------------------------------------ #
    def _start_record(self, payload: dict) -> None:
        if not self._run_lock.acquire(blocking=False):
            # 开录抢不到锁（基本只会是上一段还没收拾完）→ 放弃这一次是安全的：
            # 等锁一放开，检测线程的下一次采样还会再触发一次 segment_start。
            self._log("上一段还在收尾，跳过本次开录", "warn")
            return
        try:
            if not (self.backend and self.backend.connected):
                if not self._ensure_obs():
                    self.detector.force_stop()
                    return
            be = self.backend
            # OBS 的录制输出有冷却：刚 StopRecord 之后马上 StartRecord 容易「假成功」。
            # 先等一小会儿能显著降低卡死概率；真卡住了下面还有重试兜底。
            gap = max(0.0, float(getattr(self.cfg.obs, "restart_gap", 3.0) or 0))
            if gap and self._last_stop_ts:
                wait = gap - (time.time() - self._last_stop_ts)
                if 0 < wait:
                    time.sleep(min(wait, gap))
            self._files_before = be.snapshot_files()
            sess = Session(
                index=len(self.sessions) + 1,
                started=time.time(),
                reason=payload.get("reason", ""),
                text=payload.get("text", ""),
            )
            # 开录最多试 3 轮。OBS 的录制输出偶尔会「卡死」：
            # 回 active=true 但 outputDuration 恒为 0、不写数据，等多久都不会自愈。
            # 实测有效的救法是：再发一次 StopRecord（它会回 501），等 2 秒，
            # 输出就恢复可用了。所以卡住不是放弃，而是主动清一下再重试。
            ok, why = False, ""
            for attempt in range(1, 4):
                try:
                    be.start()
                except Exception as e:
                    why = str(e)
                    self._log(f"开录失败：{e}", "error")
                    break
                ok, why = self._verify_recording_started()
                if ok:
                    break
                if attempt < 3:
                    self._log(f"OBS 输出没真正起来（{why}），"
                              f"尝试重置录制输出后重试（{attempt}/2）…", "warn")
                    self._kick_output_reset()
                    time.sleep(2.0)
            if not ok:
                self._log(f"OBS 没有真正开始录制（{why}）", "error")
                self._recover_after_failed_start()
                return
            with self._lock:
                self._current = sess
            self._log(f"● 开始录制（第 {sess.index} 段，触发："
                      f"{_reason_trigger(payload.get('reason',''))}）", "ok")
        finally:
            self._run_lock.release()

    def _kick_output_reset(self) -> None:
        """把卡住的录制输出踢一下：发 StopRecord（多半回 501），再等它释放。

        这是实测出来的：OBS 输出卡在「假的 active + 0 字节」时，
        StartRecord 怎么发都没用，但发一次 StopRecord 之后等两秒就能恢复。
        """
        be = self.backend
        if be is None:
            return
        try:
            be.stop()
        except obsmod.ObsNotRunning:
            pass
        except Exception:
            pass
        try:
            be.client.disconnect()
            be.connect()
        except Exception:
            pass

    def _verify_recording_started(self, timeout: float = 6.0) -> tuple:
        """确认 OBS 真的在写数据。返回 (是否在录, 原因)。"""
        be = self.backend
        if be is None:
            return False, "后端未连接"
        if be.name == "hotkey":
            return True, ""         # 热键后端没法回读状态，只能相信
        try:
            verify = getattr(be, "verify_started", None)
            if callable(verify):
                return verify(timeout)
        except Exception as e:
            return False, str(e)
        # 老后端没有这个方法：退回简单的状态轮询
        end = time.time() + max(0.5, timeout)
        while time.time() < end:
            try:
                if be.recording():
                    return True, ""
            except Exception:
                pass
            time.sleep(0.25)
        return False, "OBS 报录制已开始，但状态回读为「未录制」"

    def _recover_after_failed_start(self) -> None:
        """开录失败后收口：OBS 那边若半开着就先停掉，再把检测器拉回可再试的状态。"""
        try:
            if self.backend and self.backend.connected and self.backend.recording():
                try:
                    self.backend.stop()
                    self._last_stop_ts = time.time()
                except obsmod.ObsNotRunning:
                    pass
                except Exception:
                    pass
        except Exception:
            pass
        with self._lock:
            self._current = None
        # 用 abort_segment 而不是 force_stop：后者会再抛一条 segment_stop，
        # 上层又要发一次 StopRecord——而 OBS 根本没开始录，白白多撞一次 501。
        try:
            self.detector.abort_segment()
        except AttributeError:
            self.detector.force_stop()

    def _request_stop(self, reason: StopReason) -> None:
        """排队一次停录请求。

        与旧版的关键差别：**不丢请求**。
        - 已经有停录在跑 → 只把「更优先的原因」记下来，等它跑完再补一次；
        - 有开录还占着锁 → 等锁放开后照样要停（否则会一直录下去）。
        """
        with self._stop_guard:
            if self._stopping:
                self._pending_stop = True
                if self._pending_reason is None:
                    self._pending_reason = reason
                return
            self._stopping = True
        threading.Thread(target=self._stop_worker, args=(reason,),
                         name="galrec-stop", daemon=True).start()

    def _stop_worker(self, reason: StopReason) -> None:
        cur = reason
        while True:
            try:
                self._stop_record(cur)
            except Exception as e:
                self._log(f"停录过程出错：{e}", "error")
            with self._stop_guard:
                if self._pending_stop and self._pending_reason is not None:
                    cur = self._pending_reason
                    self._pending_stop = False
                    self._pending_reason = None
                    continue
                self._stopping = False
                self._pending_stop = False
                self._pending_reason = None
                return

    def _stop_record(self, reason: StopReason) -> None:
        delay = max(0.0, float(self.cfg.obs.stop_delay or 0))
        if delay:
            time.sleep(delay)
        # 开录可能还占着锁（刚触发就判结束的极端情况）：等它，绝不放弃
        self._run_lock.acquire()
        try:
            be = self.backend
            path = ""
            already_gone = False
            if be is None:
                return
            if not (be.connected):
                self._log("OBS 连接已断开，本段录像可能已丢失", "error")
                self._finalize_session(reason, "", already_gone=True)
                return
            if be.name != "hotkey":
                try:
                    if not be.recording():
                        already_gone = True
                        self._log("OBS 那边已经不在录制（可能被手动停了），"
                                  "直接收尾这一段", "warn")
                except Exception:
                    # 查不到就当「还在录」：多发一次 StopRecord 是幂等的（501 已识别），
                    # 但漏停会让 OBS 一直写文件，直接毁掉这段素材。
                    already_gone = False
            if not already_gone:
                path = self._do_stop_with_retry(reason)
            sess = self._finalize_session(reason, path, already_gone)
            if sess is None:
                return
            # 拿不到路径时，靠目录 diff 兜底（热键后端必然走这条）。
            # ⚠ 但「本来就没录到东西」（already_gone / 501）时不能兜底：
            # 那时目录里根本没有本段的新文件，硬取会抓到上一段刚写完的那个，
            # 造成「这一段显示成上一段的文件」。实测踩过。
            if not sess.path and not already_gone and be.connected:
                # 如果是刚被 _kick_output_reset 重启过连接，录制可能其实还在，
                # 再给 OBS 一点时间把文件落地
                time.sleep(1.2)
                after = be.snapshot_files()
                new = [p for p in after
                       if p not in self._files_before or after[p] != self._files_before.get(p)]
                new = [p for p in new if _looks_like_video(p)]
                if new:
                    sess.path = max(new, key=lambda p: after.get(p, 0))
            self._post_process(sess)
        finally:
            self._run_lock.release()

    def _do_stop_with_retry(self, reason: StopReason) -> str:
        """真正发 StopRecord。

        501（OutputNotRunning）= 本来就没在录，不是失败，立即收尾。
        超时 / 连接断 → 复核状态、必要时重连再试。
        """
        be = self.backend
        attempts = 3
        last_err = ""
        for i in range(attempts):
            try:
                return be.stop()
            except obsmod.ObsNotRunning as e:
                self._log(f"停录时 OBS 报「没在录制」（{e}），按已停处理", "info")
                return ""
            except Exception as e:
                last_err = str(e)
                # 连接断了：重连再试一次，否则这段就白录了
                if not be.connected:
                    self._log("停录时连接已断开，尝试重连…", "warn")
                    try:
                        if self._ensure_obs() and self.backend:
                            be = self.backend
                            if not be.recording():
                                return ""
                    except Exception:
                        pass
                else:
                    try:
                        if not be.recording():
                            return ""     # OBS 其实已经停了，只是响应慢
                    except Exception:
                        pass
                if i + 1 < attempts:
                    self._log(f"停录没响应（{last_err}），第 {i+2} 次重试…", "warn")
                    time.sleep(1.5)
        self._log(f"停录失败：{last_err}", "error")
        return ""

    def _finalize_session(self, reason: StopReason, path: str,
                          already_gone: bool = False):
        """把当前段落的元数据定死，返回 Session（没有则 None）。"""
        with self._lock:
            sess = self._current
            self._current = None
        if sess is None:
            return None
        now = time.time()
        self._last_stop_ts = now
        sess.stopped = now
        sess.reason = reason.value
        dur = max(0.0, now - sess.started)
        # OBS 自己停了（手动/崩溃）：用 OBS 报的时长更可信
        if already_gone and self.backend and self.backend.name != "hotkey":
            try:
                d = self.backend.duration()
                if d and d > 0:
                    dur = d
            except Exception:
                pass
        sess.duration = dur
        sess.path = path or ""
        self._log(f"■ 停止录制（{_reason_text_s(reason)}，时长 {_fmt_sec(dur)}）"
                  + (f" → {os.path.basename(path)}" if path else ""), "ok")
        return sess

    def _close_session_wo_obs(self, reason: StopReason) -> None:
        """OBS 已不在录时的收尾（界面 stop() 用）。"""
        sess = self._finalize_session(reason, "", already_gone=True)
        if sess is not None:
            self._post_process(sess)

    def _post_process(self, sess: Session) -> None:
        # OBS 回完 StopRecord 之后，封装器还在把索引/尾部刷进文件（mp4 尤其明显，
        # 长录像可能要十几秒）。这期间文件是独占打开的：删不掉、更不了名、
        # ffmpeg 也读不了，日志里就是「另一个程序正在使用此文件」。
        # 所以所有后处理之前先统一等它放手，等不到就别硬来。
        released = self._wait_file_released(sess.path, timeout=45.0)

        keep = True
        dur = sess.duration
        if self.cfg.record.delete_short and dur < self.cfg.detect.min_record_seconds:
            if not released:
                # 还在被占用就删不得，留着比删一半强
                self._log("录像文件仍被 OBS 占用，本次跳过删除太短录像", "warn")
            else:
                keep = not self._discard(sess, f"短于 {self.cfg.detect.min_record_seconds:.0f} 秒")

        if keep and self.cfg.record.remux and sess.path:
            newp = self._remux(sess.path)
            if newp:
                sess.path = newp

        if not keep:
            return
        game = self._game_name()
        if game and sess.path and self.cfg.record.filename_template:
            self.rename_output(sess, game)
        with self._lock:
            # 同一条记录别重复入表（重试/重入都可能走到这里）
            if any(s is sess for s in self.sessions):
                return
            self.sessions.append(sess)
        self._on_sessions(list(self.sessions))

    def _game_name(self) -> str:
        d = self.detector
        if d.win and d.win.title:
            t = d.win.title.strip()
            # 去掉常见后缀，「女裝男生想脫下裙子！[攸望漢化組]1 - 窗口标题」之类
            t = re.split(r"\s+-\s+", t)[0]
            return t[:60]
        return self.cfg.detect.window_title or "galgame"

    def _wait_file_released(self, path: str, timeout: float = 8.0) -> bool:
        """等 OBS 真正放手这个文件。

        ⚠ 不能用「能不能打开」来判断。Windows 上 OBS 录制时用的是
        FILE_SHARE_READ|WRITE，所以 `open(path, "rb+")` 往往**能成功**，
        但那个句柄没有 FILE_SHARE_DELETE —— 删除和改名照样会被拒
        （WinError 32：“另一个程序正在使用此文件”）。
        旧版就是拿 open() 当判据，于是永远「以为已经释放」，然后必然改名失败。

        正确判据：真的试着改个名再改回来。改得动 = 拿到了 DELETE 权限 = 已放手。
        """
        if not path or not os.path.isfile(path):
            return False
        end = time.time() + timeout
        probe = path + ".probe"
        while time.time() < end:
            try:
                os.rename(path, probe)
                try:
                    os.rename(probe, path)
                except Exception:
                    # 改回来了才安全；万一失败至少文件没丢，名字是 .probe
                    pass
                return True
            except (PermissionError, OSError):
                time.sleep(0.3)
            except Exception:
                break
        return False

    def _retry_fs(self, fn, path: str, what: str,
                  timeout: float = 30.0) -> bool:
        """对文件操作做退避重试（OBS 收尾写 moov 索引时能占十几秒）。"""
        end = time.time() + timeout
        while True:
            try:
                fn()
                return True
            except (PermissionError, OSError) as e:
                if time.time() >= end:
                    self._log(f"{what}失败（文件仍被占用）：{e}", "warn")
                    return False
                time.sleep(0.5)

    def _discard(self, sess: Session, why: str) -> bool:
        p = sess.path
        if not (p and os.path.isfile(p)):
            return False
        # 调用方已经等过释放了；这里再给一点余量，然后真删
        if not self._wait_file_released(p, timeout=10.0):
            self._log(f"文件仍被占用，跳过删除：{os.path.basename(p)}", "warn")
            return False
        if self._retry_fs(lambda: os.remove(p), p, "删除录像"):
            self._log(f"已丢弃太短的录像（{why}）：{os.path.basename(p)}", "info")
            return True
        return False

    # ------------------------------------------------------------------ #
    # 后处理
    # ------------------------------------------------------------------ #
    def _find_ffmpeg(self) -> str:
        if self.cfg.record.ffmpeg:
            p = self.cfg.resolve_path(self.cfg.record.ffmpeg)
            if os.path.isfile(p):
                return p
        return shutil.which("ffmpeg") or ""

    def _remux(self, path: str) -> str:
        ff = self._find_ffmpeg()
        if not ff:
            self._log("找不到 ffmpeg，跳过转封装", "warn")
            return ""
        base, _ = os.path.splitext(path)
        target = f"{base}_remux.mp4"
        try:
            cmd = [ff, "-y", "-loglevel", "error", "-i", path,
                   "-c", "copy", "-movflags", "+faststart", target]
            subprocess.run(cmd, check=True, timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                try:
                    os.remove(path)
                except Exception:
                    pass
                self._log(f"已转封装：{os.path.basename(target)}", "info")
                return target
        except Exception as e:
            self._log(f"转封装失败：{e}", "warn")
        return ""

    def rename_output(self, sess: Session, game_name: str) -> str:
        if not sess.path or not os.path.isfile(sess.path):
            return ""
        t = time.localtime(sess.started or time.time())
        try:
            name = self.cfg.record.filename_template.format(
                game=_safe(game_name or "galgame"),
                time=time.strftime("%Y%m%d_%H%M%S", t),
                date=time.strftime("%Y-%m-%d", t),
                index=sess.index,
                reason=sess.reason or "",
            )
        except (KeyError, IndexError):
            name = f"{_safe(game_name)}_{time.strftime('%Y%m%d_%H%M%S', t)}"
        name = _safe(name)
        ext = os.path.splitext(sess.path)[1] or ".mkv"
        target = os.path.join(os.path.dirname(sess.path), name + ext)
        if os.path.abspath(target) == os.path.abspath(sess.path):
            return target
        if os.path.exists(target):
            target = os.path.join(os.path.dirname(sess.path),
                                  f"{name}_{int(time.time())}{ext}")

        def _do():
            os.rename(sess.path, target)
            sess.path = target

        if self._retry_fs(_do, sess.path, "重命名", timeout=30.0):
            return target
        return ""


# --------------------------------------------------------------------------- #
_VIDEO_EXT = {".mp4", ".mkv", ".flv", ".mov", ".ts", ".m4v", ".avi", ".webm", ".wmv"}


def _looks_like_video(p: str) -> bool:
    return os.path.splitext(p)[1].lower() in _VIDEO_EXT


def _safe(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip(" .")
    return s[:150] or "recording"


def _fmt_sec(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _reason_text_s(r: StopReason) -> str:
    return {
        StopReason.IDLE: "剧情结束", StopReason.WINDOW: "窗口关闭",
        StopReason.MAXDUR: "达到单段上限", StopReason.MANUAL: "手动停止",
        StopReason.OCCLUDED: "窗口被遮挡", StopReason.MINIMIZED: "窗口被最小化",
        StopReason.TITLE: "回到标题画面", StopReason.TEXT_LOST: "台词消失",
        StopReason.ERROR: "异常",
    }.get(r, str(r))


def _reason_trigger(reason: str) -> str:
    return {"dialogue": "出现新台词", "activity": "画面活动",
            "window": "画面活动", "manual": "手动"}.get(reason, reason or "-")
