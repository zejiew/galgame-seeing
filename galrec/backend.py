"""录制后端抽象层。

两种后端：

- **websocket**：通过 obs-websocket（v5/v4）控制 OBS。功能最全，能拿文件名、
  能读录制时长、能改录制目录。
- **hotkey**：给 OBS 设「开始/停止录制」全局热键，程序模拟按键触发。
  不需要装任何插件，OBS 27 开箱即用；代价是拿不到文件名（只能靠扫描录制
  目录里的新文件来补，本模块已经实现了这个兜底）。

上层（Controller）只认这套接口，不关心用的是哪个后端。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import hotkey as hk
from . import obs as obsmod
from .config import Config


class BackendError(Exception):
    pass


class BaseBackend:
    name = "base"
    label = "未连接"

    def connect(self) -> bool:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> str:
        raise NotImplementedError

    def recording(self) -> bool:
        return False

    def duration(self) -> float:
        return 0.0

    def record_dir(self) -> str:
        return ""

    def snapshot_files(self) -> Dict[str, float]:
        d = self.record_dir()
        out: Dict[str, float] = {}
        if not d or not os.path.isdir(d):
            return out
        try:
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    out[p] = os.path.getmtime(p)
        except Exception:
            pass
        return out


# --------------------------------------------------------------------------- #
class WsBackend(BaseBackend):
    name = "websocket"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = obsmod.ObsClient(
            cfg.obs.host, cfg.obs.port, cfg.obs.password, cfg.obs.protocol
        )
        self._t0 = 0.0
        self._is_rec = False
        self._last_dur = 0.0       # 最后一次读到的正数时长（停录后兜底用）
        self._last_status_ok = True  # 上次状态查询是否成功
        self._started_ts = 0.0       # 本段开录时刻（算「刚停录多久」用）
        self.port_auto = 0          # 自动发现的端口（非用户填写时）

    @property
    def connected(self) -> bool:
        return self.client.connected

    @property
    def label(self) -> str:
        if self.connected:
            p = self.client.port
            tag = f"（自动发现端口 {p}）" if self.port_auto == p else ""
            return f"WebSocket {self.client.negotiated} :{p}{tag}"
        return "WebSocket 未连接"

    def connect(self) -> bool:
        self.client.host = self.cfg.obs.host
        self.client.password = self.cfg.obs.password
        # 端口自适应：obs-websocket 5.x（OBS 28+）默认 4455，
        # 4.x（OBS 27 插件）默认 4444。用户填的端口优先，但填错了也要能自己找回来。
        want = int(self.cfg.obs.port or 4455)
        cands = [want] + [p for p in (4455, 4444, 4456) if p != want]
        for p in cands:
            self.client.port = p
            if self.client.connect():
                if p != want:
                    self.port_auto = p
                return True
        # 全都连不上：复位到用户填的端口，让报错信息对应用户的输入
        self.client.port = want
        return False

    def disconnect(self) -> None:
        self.client.disconnect()

    def start(self) -> None:
        self.client.start_record()
        self._t0 = time.time()
        self._is_rec = True
        self._last_dur = 0.0
        self._started_ts = time.time()
        try:
            d = self.client.record_status().duration
            if d > 0:
                self._t0 = time.time() - d
        except Exception:
            pass

    def verify_started(self, timeout: float = 6.0) -> tuple:
        """确认真的在录（见 ObsClient.wait_recording_live 的说明）。"""
        return self.client.wait_recording_live(timeout=timeout)

    def seconds_since_start(self) -> float:
        return (time.time() - self._started_ts) if self._started_ts else 1e9

    def stop(self) -> str:
        p = self.client.stop_record(timeout=max(5.0, float(self.cfg.obs.stop_timeout or 20.0)))
        self._is_rec = False
        return p

    def recording(self) -> bool:
        """是否在录。

        ⚠ 关键：**查询失败不等于「没在录」**。
        旧版这里 `except ObsError: return False`，一旦赶上偶发的查询异常，
        上层就会以为「已经不在录了」→ 跳过 StopRecord → OBS 继续写文件
        → 文件被占用 → 删不掉、改不了名 → 报「停录失败」。
        现在失败时沿用上一次已知状态，并回退到我们的本地时间线判断。
        """
        try:
            st = self.client.record_status()
            self._is_rec = st.recording
            self._last_status_ok = True
            return st.recording
        except obsmod.ObsError:
            self._last_status_ok = False
            # 本地时间线：我们发过 start、还没发 stop，且距离开录还没超过单段上限，
            # 那就按「仍在录」处理，宁可多发一次 StopRecord（幂等，不伤文件），
            # 也不能漏停。
            return bool(self._is_rec)

    def duration(self) -> float:
        try:
            d = self.client.record_status().duration
            # OBS 停录之后 outputDuration 会归零，但停录那一刻读到的才是准的，
            # 所以这里把最后一次读到的正数记住，供收尾时兜底。
            if d > 0:
                self._last_dur = d
                return d
            return self._last_dur or (time.time() - self._t0 if self._is_rec else 0.0)
        except obsmod.ObsError:
            return self._last_dur or (time.time() - self._t0 if self._is_rec else 0.0)

    def record_dir(self) -> str:
        try:
            return self.client.get_record_dir()
        except obsmod.ObsError:
            return ""

    def prepare(self) -> str:
        """连接后对齐场景与录制目录，返回给用户看的说明。"""
        msgs = []
        if self.cfg.obs.scene:
            ok = self.client.set_scene(self.cfg.obs.scene)
            msgs.append(("已切到场景 " + self.cfg.obs.scene) if ok
                        else ("切换场景失败：" + self.cfg.obs.scene))
        if self.cfg.obs.set_record_dir:
            want = self.cfg.record_dir()
            os.makedirs(want, exist_ok=True)
            cur = self.record_dir()
            try:
                same = os.path.normcase(os.path.abspath(cur)) == os.path.normcase(os.path.abspath(want))
            except Exception:
                same = False
            if not same:
                if self.client.set_record_dir(want):
                    msgs.append("录制目录 → " + want)
                else:
                    msgs.append(f"无法设置录制目录，沿用 OBS 的：{cur}")
        return "；".join(msgs)


# --------------------------------------------------------------------------- #
class HotkeyBackend(BaseBackend):
    name = "hotkey"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._t0 = 0.0
        self._is_rec = False
        self._last_err = ""

    @property
    def connected(self) -> bool:
        return bool(self.cfg.obs.hotkey_start and self.cfg.obs.hotkey_stop) and \
            hk.parse_accel(self.cfg.obs.hotkey_start) is not None

    @property
    def label(self) -> str:
        return f"热键 {self.cfg.obs.hotkey_start} / {self.cfg.obs.hotkey_stop}"

    def connect(self) -> bool:
        ok1 = hk.parse_accel(self.cfg.obs.hotkey_start) is not None
        ok2 = hk.parse_accel(self.cfg.obs.hotkey_stop) is not None
        if not (ok1 and ok2):
            self._last_err = "热键格式不对，示例：ctrl+alt+f9"
            return False
        return True

    def disconnect(self) -> None:
        self._is_rec = False

    def start(self) -> None:
        if not hk.send_hotkey(self.cfg.obs.hotkey_start):
            raise BackendError(f"发送热键失败：{self.cfg.obs.hotkey_start}")
        self._t0 = time.time()
        self._is_rec = True

    def stop(self) -> str:
        if not hk.send_hotkey(self.cfg.obs.hotkey_stop):
            raise BackendError(f"发送热键失败：{self.cfg.obs.hotkey_stop}")
        self._is_rec = False
        return ""

    def toggle(self) -> str:
        """按一下停止热键没生效时的兜底：再发一次（OBS 的录制热键是 toggle）。"""
        return self.stop()

    def recording(self) -> bool:
        return self._is_rec

    def duration(self) -> float:
        return time.time() - self._t0 if self._is_rec else 0.0

    def record_dir(self) -> str:
        d = self.cfg.obs.record_dir
        return self.cfg.record_dir()

    def prepare(self) -> str:
        return ""


# --------------------------------------------------------------------------- #
def make_backend(cfg: Config) -> BaseBackend:
    mode = (cfg.obs.backend or "auto").lower()
    if mode == "hotkey":
        return HotkeyBackend(cfg)
    if mode == "websocket":
        return WsBackend(cfg)
    # auto：先试 WebSocket，连不上且配了热键就退到热键
    ws = WsBackend(cfg)
    if ws.connect():
        return ws
    hkb = HotkeyBackend(cfg)
    if hkb.cfg.obs.hotkey_start and hkb.connect():
        return hkb
    return ws  # 返回失败的 ws，让上层显示它的错误
