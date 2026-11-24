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
        self._pending_stop = False
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
            # 停录要等宽限，放到后台线程，别把检测线程堵住
            self._pending_stop = True
            threading.Thread(target=self._stop_record, args=(reason,),
                             daemon=True).start()
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
            self._log("上一个操作还没完成，跳过本次开录", "warn")
            return
        try:
            if not (self.backend and self.backend.connected):
                if not self._ensure_obs():
                    self.detector.force_stop()
                    return
            be = self.backend
            self._files_before = be.snapshot_files()
            sess = Session(
                index=len(self.sessions) + 1,
                started=time.time(),
                reason=payload.get("reason", ""),
                text=payload.get("text", ""),
            )
            with self._lock:
                self._current = sess
            try:
                be.start()
            except Exception as e:
                self._log(f"开录失败：{e}", "error")
                with self._lock:
                    self._current = None
                self.detector.force_stop()
                return
            self._log(f"● 开始录制（第 {sess.index} 段，触发："
                      f"{_reason_trigger(payload.get('reason',''))}）", "ok")
        finally:
            self._run_lock.release()

    def _stop_record(self, reason: StopReason) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            delay = max(0.0, float(self.cfg.obs.stop_delay or 0))
            if delay:
                time.sleep(delay)
            be = self.backend
            path = ""
            if be:
                try:
                    path = be.stop()
                except Exception as e:
                    self._log(f"停录失败：{e}", "error")
            with self._lock:
                sess = self._current
                self._current = None
                if sess is None:
                    return
                sess.stopped = time.time()
                sess.reason = reason.value
                sess.duration = max(0.0, sess.stopped - sess.started)

            # 拿不到路径时，靠目录 diff 兜底（热键后端必然走这条）
            if not path and be:
                time.sleep(0.9)
                after = be.snapshot_files()
                new = [p for p in after
                       if p not in self._files_before or after[p] != self._files_before.get(p)]
                new = [p for p in new if _looks_like_video(p)]
                if new:
                    path = max(new, key=lambda p: after.get(p, 0))
            sess.path = path or ""

            dur = sess.duration
            self._log(f"■ 停止录制（{_reason_text_s(reason)}，时长 {_fmt_sec(dur)}）"
                      + (f" → {os.path.basename(path)}" if path else ""), "ok")

            keep = True
            if self.cfg.record.delete_short and dur < self.cfg.detect.min_record_seconds:
                keep = not self._discard(sess, f"短于 {self.cfg.detect.min_record_seconds:.0f} 秒")

            if keep and self.cfg.record.remux and sess.path:
                newp = self._remux(sess.path)
                if newp:
                    sess.path = newp

            if keep:
                # OBS 停录后文件句柄不是立刻释放的（封装器还在写尾部/写索引），
                # 立刻改名或转封装会撞 "另一个程序正在使用此文件"。先等它放手。
                self._wait_file_released(sess.path)
                game = self._game_name()
                if game and sess.path and self.cfg.record.filename_template:
                    self.rename_output(sess, game)
                with self._lock:
                    self.sessions.append(sess)
                self._on_sessions(list(self.sessions))
        finally:
            self._pending_stop = False
            self._run_lock.release()

    def _game_name(self) -> str:
        d = self.detector
        if d.win and d.win.title:
            t = d.win.title.strip()
            # 去掉常见后缀，「女裝男生想脫下裙子！[攸望漢化組]1 - 窗口标题」之类
            t = re.split(r"\s+-\s+", t)[0]
            return t[:60]
        return self.cfg.detect.window_title or "galgame"

    def _wait_file_released(self, path: str, timeout: float = 8.0) -> bool:
        """等 OBS 真正放手这个文件（能独占打开 → 说明写完了）。"""
        if not path or not os.path.isfile(path):
            return False
        end = time.time() + timeout
        while time.time() < end:
            try:
                with open(path, "rb+"):
                    return True
            except (PermissionError, OSError):
                time.sleep(0.25)
            except Exception:
                break
        return False

    def _discard(self, sess: Session, why: str) -> bool:
        p = sess.path
        if p and os.path.isfile(p):
            self._wait_file_released(p, timeout=5.0)
            try:
                os.remove(p)
                self._log(f"已丢弃太短的录像（{why}）：{os.path.basename(p)}", "info")
                return True
            except Exception as e:
                self._log(f"删除录像失败：{e}", "warn")
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
        try:
            if os.path.exists(target):
                target = os.path.join(os.path.dirname(sess.path),
                                      f"{name}_{int(time.time())}{ext}")
            os.rename(sess.path, target)
            sess.path = target
            return target
        except Exception as e:
            self._log(f"重命名失败：{e}", "warn")
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
        StopReason.ERROR: "异常",
    }.get(r, str(r))


def _reason_trigger(reason: str) -> str:
    return {"dialogue": "出现新台词", "activity": "画面活动",
            "window": "画面活动", "manual": "手动"}.get(reason, reason or "-")
