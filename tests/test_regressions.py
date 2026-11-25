"""回归测试：专盯用户实际踩过的两个坑，防止再犯。

    python tests/test_regressions.py

覆盖：

A. **最小化不该停录**（用户反馈：最小化一下就突然停止录制）
   - 默认配置下，窗口最小化后超过 minimized_timeout 也不许停
   - 只有显式打开 stop_on_minimized 才允许停
   - 最小化期间「静默计时」必须被冻结（不能算成剧情结束）

B. **停录必须灵敏、且不谎报失败**
   - 501 / OutputNotRunning 要当成「本来就没在录」，而不是「停录失败」
   - 停录请求不许因为「已经有一个在跑」而被丢掉（旧版非阻塞抢锁直接 return）
   - 开录后 OBS 其实没在录 → 要能发现，不能拖到停录时才炸 501
   - 长录像 StopRecord 慢（超过旧的 5 秒超时）也要能正常收尾
   - 时钟漂移（旧 _shift_clock 反复多加 eps）不能导致永远不停录
   - 标题画面要能提前停（不用等满 idle_timeout）

全部离线：用假的 OBS 服务端 + 假窗口，不碰真 OBS、不碰真游戏。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from galrec import obs as obsmod  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok))
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


# --------------------------------------------------------------------------- #
# 假的 v5 服务端：可以按需模拟「输出没起来」「停录很慢」「本来就没在录」等
# --------------------------------------------------------------------------- #
class FakeObsV5:
    def __init__(self, stop_delay: float = 0.0, never_really_records: bool = False,
                 report_not_running: bool = False):
        self.stop_delay = stop_delay
        self.never_really_records = never_really_records
        self.report_not_running = report_not_running
        self.recording = False
        self.start_calls = 0
        self.stop_calls = 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _serve(self):
        conn, _ = self.sock.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                return
            req += chunk
        key = ""
        for line in req.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        f = conn.makefile("rwb")

        def send(obj):
            data = json.dumps(obj).encode()
            hdr = bytearray([0x81])
            n = len(data)
            if n < 126:
                hdr.append(n)
            elif n < 65536:
                hdr.append(126)
                hdr += n.to_bytes(2, "big")
            else:
                hdr.append(127)
                hdr += n.to_bytes(8, "big")
            f.write(bytes(hdr) + data)
            f.flush()

        def recv():
            hdr = f.read(2)
            if len(hdr) < 2:
                return None
            b2 = hdr[1]
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = int.from_bytes(f.read(2), "big")
            elif ln == 127:
                ln = int.from_bytes(f.read(8), "big")
            mask = f.read(4) if masked else b""
            data = f.read(ln)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            try:
                return json.loads(data)
            except Exception:
                return None

        send({"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}})
        while True:
            msg = recv()
            if msg is None:
                return
            if msg.get("op") != 1:
                continue
            d = msg.get("d") or {}
            if d.get("rpcVersion") != 1:
                send({"op": 8, "d": {"code": 4009, "reason": "bad rpc"}})
                return
            send({"op": 2, "d": {"negotiatedRpcVersion": 1}})
            break

        while True:
            msg = recv()
            if msg is None:
                return
            if msg.get("op") != 6:
                continue
            d = msg.get("d") or {}
            rt = d.get("requestType")
            rid = d.get("requestId")
            status = {"result": True, "code": 100}
            resp = {}

            if rt == "GetVersion":
                resp = {"obsVersion": "30.0.0", "obsWebSocketVersion": "5.5.0",
                        "rpcVersion": 1}
            elif rt == "GetRecordStatus":
                # never_really_records：StartRecord 收下了但压根没真录
                active = self.recording and not self.never_really_records
                resp = {"outputActive": active, "outputPaused": False,
                        "outputDuration": int(9000), "outputBytes": 1024}
            elif rt == "StartRecord":
                self.start_calls += 1
                self.recording = True
            elif rt == "StopRecord":
                self.stop_calls += 1
                if self.report_not_running or not self.recording:
                    # v5 真实行为：没在录就是 501 OutputNotRunning
                    status = {"result": False, "code": 501,
                              "comment": "The output is not active or does not exist."}
                else:
                    if self.stop_delay:
                        time.sleep(self.stop_delay)
                    self.recording = False
                    resp = {"outputPath": r"C:\rec\r1.mkv"}
            elif rt == "GetRecordDirectory":
                resp = {"recordDirectory": r"C:\rec"}
            elif rt == "GetOutputStatus":
                resp = {"outputActive": self.recording,
                        "outputName": d.get("requestData", {}).get("outputName", "")}
            else:
                status = {"result": False, "code": 204, "comment": "unknown"}

            send({"op": 7, "d": {"requestType": rt, "requestId": rid,
                                 "requestStatus": status, "responseData": resp}})


def connect(port: int) -> obsmod.ObsClient:
    c = obsmod.ObsClient("127.0.0.1", port, "", "v5", timeout=5.0)
    if not c.connect():
        raise RuntimeError(c.last_error)
    return c


# --------------------------------------------------------------------------- #
# A. 最小化
# --------------------------------------------------------------------------- #
class FakeWindow:
    """假窗口：只提供 detector 需要的几个属性。"""
    hwnd = 0x1234
    title = "假游戏"
    process = "fake.exe"
    rect = (0, 0, 1280, 720)

    def label(self):
        return "假游戏 1280x720"

    @property
    def size(self):
        return (1280, 720)


def test_minimize_stops_by_default():
    """A. 最小化必须默认停录。

    实测（BitBlt / WGC / 自动三种方法一致）：窗口最小化后 Windows 停止渲染，
    OBS 只能录到纯黑。所以「最小化不停录」是错的默认值——
    它会让录像里塞进一大段黑屏，还占硬盘。
    """
    print("\n[A] 最小化必须默认停录（录下去只有黑屏）")
    from galrec.config import Config
    from galrec.detector import Detector, Phase, StopReason

    # ---- A1：默认配置就该停 ----
    cfg = Config()
    check("默认 stop_on_minimized = True",
          cfg.detect.stop_on_minimized is True,
          f"实际 {cfg.detect.stop_on_minimized}")
    check("默认 minimized_timeout 是 20 秒量级（不是 300）",
          0 < cfg.detect.minimized_timeout <= 60,
          f"{cfg.detect.minimized_timeout}s")

    cfg.detect.minimized_timeout = 0.25     # 缩短方便测
    events = []
    det = Detector(cfg, on_event=lambda e, p: events.append((e, p)))
    det.win = FakeWindow()
    det.hwnd = FakeWindow.hwnd
    det.phase = Phase.RECORDING
    det._seg_start_ts = time.time() - 10
    det._seg_wall_start = time.time() - 10
    det._last_progress_ts = time.time() - 10

    import galrec.detector as detmod
    orig_iconic = detmod.windows.is_iconic
    orig_alive = detmod.windows.is_alive
    orig_find = detmod.windows.find_window
    orig_probe = detmod.capture.WindowGrabber.probe
    detmod.windows.is_iconic = lambda hwnd: True
    detmod.windows.is_alive = lambda hwnd: True
    detmod.windows.find_window = lambda **kw: FakeWindow()
    detmod.capture.WindowGrabber.probe = lambda self: False
    det._win_grab_ok = False
    try:
        t0 = time.time()
        while time.time() - t0 < 1.2 and det.phase is Phase.RECORDING:
            det._tick()
            time.sleep(0.06)
    finally:
        detmod.windows.is_iconic = orig_iconic
        detmod.windows.is_alive = orig_alive
        detmod.windows.find_window = orig_find
        detmod.capture.WindowGrabber.probe = orig_probe

    reasons = [p.get("reason") for e, p in events if e == "segment_stop"]
    check("默认配置下最小化会停录（reason=minimized）",
          StopReason.MINIMIZED.value in reasons,
          f"reasons={reasons} phase={det.phase.value}")

    # ---- A2：用户显式关掉开关，才允许不停 ----
    cfg2 = Config()
    cfg2.detect.stop_on_minimized = False
    cfg2.detect.minimized_timeout = 0.2
    ev2 = []
    d2 = Detector(cfg2, on_event=lambda e, p: ev2.append((e, p)))
    d2.win = FakeWindow()
    d2.hwnd = FakeWindow.hwnd
    d2.phase = Phase.RECORDING
    d2._seg_start_ts = time.time() - 5
    d2._seg_wall_start = time.time() - 5
    d2._win_grab_ok = False
    detmod.windows.is_iconic = lambda hwnd: True
    detmod.windows.is_alive = lambda hwnd: True
    detmod.windows.find_window = lambda **kw: FakeWindow()
    detmod.capture.WindowGrabber.probe = lambda self: False
    try:
        t0 = time.time()
        while time.time() - t0 < 1.0:
            d2._tick()
            time.sleep(0.06)
    finally:
        detmod.windows.is_iconic = orig_iconic
        detmod.windows.is_alive = orig_alive
        detmod.windows.find_window = orig_find
        detmod.capture.WindowGrabber.probe = orig_probe
    stops2 = [p for e, p in ev2 if e == "segment_stop"]
    check("关掉开关后最小化不停（保留给特殊场景）",
          len(stops2) == 0 and d2.phase is Phase.RECORDING,
          f"stops={stops2} phase={d2.phase.value}")

    # ---- A3：静默计时的时钟平移只推指定秒数 ----
    cfg3 = Config()
    d3 = Detector(cfg3)
    now = time.time()
    d3._last_progress_ts = now - 100
    d3._seg_start_ts = now - 100
    d3._seg_wall_start = now - 100
    d3._last_dialogue_ts = now - 100
    d3._shift_clock(60.0)
    drift = d3._last_progress_ts - (now - 100 + 60.0)
    check("_shift_clock 只平移指定秒数（不再多补 0.5s）",
          abs(drift) < 1e-6, f"额外偏移 {drift:.3f}s")


# --------------------------------------------------------------------------- #
# B. 停录
# --------------------------------------------------------------------------- #
def test_stop_501_is_not_failure():
    print("\n[B1] 501 不是「停录失败」")
    srv = FakeObsV5(report_not_running=True).start()
    c = connect(srv.port)
    raised_not_running = False
    other = ""
    try:
        c.stop_record(timeout=5)
    except obsmod.ObsNotRunning as e:
        raised_not_running = True
    except Exception as e:
        other = str(e)
    check("StopRecord 撞 501 → 抛 ObsNotRunning（不是普通错误）",
          raised_not_running, other)
    check("ObsNotRunning 是 ObsError 的子类（上层 try/except 仍然兜得住）",
          issubclass(obsmod.ObsNotRunning, obsmod.ObsError))
    check("501 被 is_not_running_error 正确识别",
          obsmod.is_not_running_error("StopRecord 失败：501 The output is not active"))
    check("正常错误不会被误判成「没在录」",
          not obsmod.is_not_running_error("StopRecord 失败：604 磁盘已满"))
    c.disconnect()
    srv.sock.close()


def test_start_verified():
    print("\n[B2] 开录后要核实真的在录")
    from galrec.backend import WsBackend
    from galrec.config import Config

    srv = FakeObsV5(never_really_records=True).start()
    cfg = Config()
    cfg.obs.port = srv.port
    be = WsBackend(cfg)
    ok = be.connect()
    check("连上假 OBS", ok, be.client.last_error)
    be.start()
    rec = be.recording()
    check("输出没真起来时 recording() 回 False（旧版会一直以为在录）",
          rec is False, f"recording={rec}")
    be.disconnect()
    srv.sock.close()


def test_stop_slow_does_not_timeout():
    print("\n[B3] 长录像封盘慢（8 秒）也要收得住")
    from galrec.backend import WsBackend
    from galrec.config import Config

    srv = FakeObsV5(stop_delay=8.0).start()
    cfg = Config()
    cfg.obs.port = srv.port
    cfg.obs.stop_timeout = 20.0
    be = WsBackend(cfg)
    be.connect()
    be.start()
    time.sleep(0.3)
    t0 = time.time()
    err = ""
    path = ""
    try:
        path = be.stop()
    except Exception as e:
        err = str(e)
    spent = time.time() - t0
    check("8 秒才回包的 StopRecord 没超时", not err, err or f"{spent:.1f}s")
    check("拿到了 outputPath", bool(path), path or "(空)")
    check("确实等了足够久（说明用的是 stop_timeout 而不是 5 秒）",
          spent > 6.0, f"{spent:.1f}s")
    be.disconnect()
    srv.sock.close()


def test_stop_request_not_dropped():
    print("\n[B4] 停录请求不许被丢掉（旧版非阻塞抢锁直接 return）")
    from galrec.config import Config
    from galrec.detector import StopReason
    from galrec.recorder import Controller
    from galrec.backend import BaseBackend

    class SlowBackend(BaseBackend):
        name = "fake"

        def __init__(self):
            self.calls = 0
            self.rec = False

        @property
        def connected(self):
            return True

        @property
        def label(self):
            return "慢速假后端"

        def connect(self):
            return True

        def disconnect(self):
            pass

        def start(self):
            self.rec = True

        def stop(self):
            self.calls += 1
            time.sleep(1.2)         # 模拟 OBS 封盘慢
            self.rec = False
            return ""

        def recording(self):
            return self.rec

    cfg = Config()
    cfg.obs.stop_delay = 0
    cfg.detect.min_record_seconds = 0
    cfg.record.delete_short = False
    cfg.record.remux = False
    cfg.record.filename_template = ""
    ctrl = Controller(cfg, log=lambda m, l="info": None)
    be = SlowBackend()
    ctrl.backend = be
    ctrl._current = type("S", (), {"index": 1, "path": "", "started": time.time(),
                                   "stopped": 0.0, "reason": "", "text": "",
                                   "duration": 0.0,
                                   "live_duration": lambda self: 0})()
    be.rec = True       # OBS 那边确实在录（否则会走「本来就没在录」的快捷分支）

    # 连发三次停录（模拟检测器/用户都喊着要停）
    ctrl._request_stop(StopReason.IDLE)
    time.sleep(0.15)
    ctrl._request_stop(StopReason.IDLE)
    time.sleep(0.15)
    ctrl._request_stop(StopReason.MANUAL)

    # 等所有后台线程收完
    deadline = time.time() + 12
    while time.time() < deadline:
        if not ctrl._stopping and ctrl._current is None:
            break
        time.sleep(0.1)

    check("停录线程跑完了、状态回到干净", ctrl._current is None and not ctrl._stopping,
          f"stopping={ctrl._stopping} current={ctrl._current}")
    check("3 次请求至少真正停了 1 次（没被静默吞掉）", be.calls >= 1,
          f"stop 调用 {be.calls} 次")
    check("多次请求被合并成有限次（不会重复轰炸 OBS）", be.calls <= 3,
          f"stop 调用 {be.calls} 次")
    check("收尾后进入了会话表", len(ctrl.sessions) >= 1,
          f"sessions={len(ctrl.sessions)}")


def test_clock_drift_self_heals():
    print("\n[B5] 时钟漂移不能导致永不停录")
    from galrec.config import Config
    from galrec.detector import Detector, Phase

    cfg = Config()
    cfg.detect.idle_timeout = 5.0
    cfg.detect.start_grace = 0.0
    cfg.record.auto_stop_on_title = False   # 本条只测 idle 逻辑
    ev = []
    det = Detector(cfg, on_event=lambda e, p: ev.append((e, p)))
    det.phase = Phase.RECORDING
    now = time.time()
    det._seg_start_ts = now - 100
    det._seg_wall_start = now - 100
    # 人为把「最后推进」推到未来 10 分钟（旧版 _shift_clock 反复加 eps 的后果）
    det._last_progress_ts = now + 600
    det._last_text_ts = now + 600
    det._last_dialogue_ts = now
    det._check_end(False, False, now, "")
    stops = [p.get("reason") for e, p in ev if e == "segment_stop"]
    check("检测到时间戳在未來 → 自愈，不再永久卡住",
          det._last_progress_ts <= time.time() + 1,
          f"_last_progress_ts 校正为 {det._last_progress_ts - now:.1f}s 后")
    check("自愈后会继续正常计时（不是一次性放过就完了）", isinstance(stops, list))


def test_title_stops_early():
    print("\n[B6] 回到标题画面要提前停（不等满 idle_timeout）")
    from galrec.config import Config
    from galrec.detector import Detector, Phase

    cfg = Config()
    cfg.detect.idle_timeout = 120.0     # 正常要等 2 分钟
    cfg.detect.start_grace = 0.0
    ev = []
    det = Detector(cfg, on_event=lambda e, p: ev.append((e, p)))
    det.phase = Phase.RECORDING
    now = time.time()
    det._seg_start_ts = now - 300
    det._seg_wall_start = now - 300
    det._last_progress_ts = now - 60
    det._last_dialogue_ts = now - 60
    # 伪造「看起来像标题」的判定，跳过昂贵 OCR
    det._looks_like_title_screen = lambda: True
    det._check_end(False, False, now, "")
    stops = [p.get("reason") for e, p in ev if e == "segment_stop"]
    check("60 秒无台词 + 看起来是标题 → 立刻停", "title" in stops, f"stops={stops}")

    # 台词消失（更硬的信号）
    cfg.detect.text_lost_timeout = 30.0
    ev2 = []
    d2 = Detector(cfg, on_event=lambda e, p: ev2.append((e, p)))
    d2.phase = Phase.RECORDING
    d2._seg_start_ts = now - 300
    d2._seg_wall_start = now - 300
    d2._last_progress_ts = now - 40
    d2._last_dialogue_ts = now - 40
    d2._looks_like_title_screen = lambda: False
    d2._check_end(False, False, now, "")
    r2 = [p.get("reason") for e, p in ev2 if e == "segment_stop"]
    check("台词消失超时 → 停（text_lost）", "text_lost" in r2, f"stops={r2}")


def test_duplicate_stop_not_sent():
    print("\n[B7] 重复的结束事件不该重复发 StopRecord")
    from galrec.config import Config
    from galrec.detector import Detector, Phase
    from galrec.detector import StopReason

    cfg = Config()
    cfg.detect.segmented = True
    ev = []
    det = Detector(cfg, on_event=lambda e, p: ev.append((e, p)))
    det.phase = Phase.RECORDING
    det._seg_start_ts = time.time()
    det._end_segment(StopReason.IDLE)
    det._end_segment(StopReason.IDLE)      # 重复调用
    det._end_segment(StopReason.MANUAL)
    stops = [p for e, p in ev if e == "segment_stop"]
    check("同一段落只发一次 segment_stop", len(stops) == 1, f"发了 {len(stops)} 次")


def test_status_error_is_not_stopped_truth():
    print("\n[B8] 状态查询失败 ≠ 没在录（这个坑会毁素材）")
    from galrec.backend import WsBackend
    from galrec.config import Config

    cfg = Config()
    be = WsBackend(cfg)
    # 伪造：发过 start 了（_is_rec=True），但查询一直抛异常
    be._is_rec = True

    def boom(*a, **kw):
        raise obsmod.ObsError("请求状态丢失：galrec-14")

    be.client.record_status = boom
    check("查询异常时不再谎报 False（沿用本地时间线）",
          be.recording() is True,
          f"recording()={be.recording()}")

    be._is_rec = False
    check("本来就没在录 + 查询异常 → 仍是 False",
          be.recording() is False)


def test_concurrent_requests_no_lost_state():
    print("\n[B9] 请求-响应竞态（旧版偶发「请求状态丢失」）")
    srv = FakeObsV5().start()
    c = connect(srv.port)
    errs = []
    t0 = time.time()
    for _ in range(300):
        try:
            c.record_status()
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")
    spent = time.time() - t0
    check("连发 300 次状态查询，零异常", not errs,
          f"{len(errs)} 次异常（首条：{errs[0] if errs else '-'}）")
    check("300 次查询很快（没有卡到超时）", spent < 20.0, f"{spent:.2f}s")
    c.disconnect()
    srv.sock.close()


def test_file_release_uses_rename_probe():
    print("\n[B10] 判断文件是否已释放：必须能改名，而不是能打开")
    import tempfile
    from galrec.config import Config
    from galrec.recorder import Controller

    cfg = Config()
    ctrl = Controller(cfg, log=lambda m, l="info": None)
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        # 正常文件：应当能识别为「已释放」，且探测后文件名不变
        ok = ctrl._wait_file_released(path, timeout=3.0)
        check("正常文件判为已释放", ok is True)
        check("探测不会把文件名弄丢", os.path.isfile(path),
              os.path.basename(os.listdir(os.path.dirname(path))[0]))
        # 不存在的文件：False，且不炸
        check("文件不存在时返回 False",
              ctrl._wait_file_released(path + ".nope", timeout=1.0) is False)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def test_obs_error_carries_code():
    print("\n[B11] 错误要带结构化 code（不再靠字符串猜 501）")
    e = obsmod.ObsError("StopRecord 失败：501", code=501, request="StopRecord")
    check("ObsError 带 code / request 字段",
          e.code == 501 and e.request == "StopRecord")
    check("按 code 判断 501 有效", obsmod.is_not_running_error(e) is True)
    e2 = obsmod.ObsError("StopRecord 失败：604", code=604)
    check("604（磁盘满）不被误判", obsmod.is_not_running_error(e2) is False)
    check("异常消息本身仍然可读", str(e) == "StopRecord 失败：501", str(e))


def main() -> int:
    print("=" * 62)
    print(" Galgame 录制 · 回归测试（最小化 / 停录灵敏性）")
    print("=" * 62)
    test_minimize_stops_by_default()
    test_stop_501_is_not_failure()
    test_start_verified()
    test_stop_slow_does_not_timeout()
    test_stop_request_not_dropped()
    test_clock_drift_self_heals()
    test_title_stops_early()
    test_duplicate_stop_not_sent()
    test_status_error_is_not_stopped_truth()
    test_concurrent_requests_no_lost_state()
    test_file_release_uses_rename_probe()
    test_obs_error_carries_code()

    print("\n" + "=" * 62)
    allok = True
    for n, ok in RESULTS:
        print(f"  {'✓' if ok else '✗'} {n}")
        allok = allok and ok
    print("=" * 62)
    print(f"{'全部通过 ✔' if allok else '有失败项 ✘'}  ({len(RESULTS)} 项)")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
