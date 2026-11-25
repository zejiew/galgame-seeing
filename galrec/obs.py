"""OBS WebSocket 客户端（同时支持协议 v5 与 v4）。

为什么不用 obsws-python：本机装不了外部包，且 v5/v4 的差异其实就是二十行
协议差异，自己写反而更可控。

- **v5**：OBS 28+ 内建 obs-websocket 5.x。Hello(op1) → Identify(op2) →
  请求走 Op 6，响应走 Op 7。
- **v4**：OBS 27 + obs-websocket 4.x 插件。请求是 {"request-type": ...}
  的裸 JSON，先 GetAuthRequired 再 Auth。

对外统一成一套方法名，上层不用关心版本。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import websocket  # websocket-client
    _HAS_WS = True
except Exception:  # pragma: no cover
    _HAS_WS = False


class ObsError(Exception):
    def __init__(self, message: str = "", code: Optional[int] = None,
                 request: str = ""):
        super().__init__(message)
        #: obs-websocket 的 RequestStatus 码（v5）；v4 为 -1；连接类错误为 None
        self.code = code
        #: 出错的请求名，如 StartRecord / StopRecord
        self.request = request


class ObsNotRunning(ObsError):
    """OBS 那边根本没在录（v5 叫 OutputNotRunning，code 501）。

    这不是「失败」，是「无需停」。上层要能单独识别出来，
    否则每次正常停录都会被当成「停录失败：501」吓用户一跳。
    """


#: 表示「目标输出本来就没在跑」的错误码/文案。v5 是 5xx 系列，
#: 其中 501 = OutputNotRunning；v4 是文字错误。
_NOT_RUNNING_CODES = {500, 501, 502, 503, 504, 505, 506}
_NOT_RUNNING_WORDS = ("not running", "not active", "not started",
                      "not recording", "outputnotrunning", "outputnotpaused")


def is_not_running_error(err) -> bool:
    """判断一条错误（对象或文本）是不是「本来就没在录」。"""
    code = getattr(err, "code", None)
    if isinstance(code, int) and code in _NOT_RUNNING_CODES:
        return True
    text = str(err)
    low = text.lower()
    if any(w in low for w in _NOT_RUNNING_WORDS):
        return True
    # 形如 "StopRecord 失败：501" / "code 501"
    for c in _NOT_RUNNING_CODES:
        if re.search(rf"\b{c}\b", low):
            return True
    return False


@dataclass
class ObsStatus:
    connected: bool = False
    protocol: str = ""          # v5 / v4
    obs_version: str = ""
    recording: bool = False
    recording_paused: bool = False
    duration: float = 0.0
    bytes: int = 0
    record_dir: str = ""
    port: int = 0               # 实际连上的端口
    message: str = ""
    output_active: bool = False  # 采集输出是否在跑（不在跑就录不出东西）


def _b64_sha256(s: str) -> str:
    return base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode("ascii")


class ObsClient:
    """线程安全的 OBS 客户端；内部一个收包线程 + 请求-响应配对。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 4455,
                 password: str = "", protocol: str = "auto",
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.protocol = protocol        # auto / v5 / v4
        self.timeout = timeout

        self._ws: Optional["websocket.WebSocket"] = None
        self._thread: Optional[threading.Thread] = None
        self._alive = threading.Event()
        self._lock = threading.RLock()
        self._req_id = 0
        self._responses: Dict[str, Any] = {}
        self._completed: Dict[str, Any] = {}
        self._events: List[dict] = []

        self.negotiated = ""            # 实际连通后的协议版本
        self.obs_version = ""
        self.last_error = ""
        self.on_event: Optional[Callable[[str, dict], None]] = None

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return bool(self._ws and self._alive.is_set())

    def connect(self, timeout: Optional[float] = None) -> bool:
        if not _HAS_WS:
            self.last_error = "缺少 websocket-client 库"
            return False
        self.disconnect()
        url = f"ws://{self.host}:{self.port}"
        try:
            ws = websocket.WebSocket()
            ws.connect(url, timeout=timeout or self.timeout,
                       suppress_origin=True)
        except Exception as e:
            self.last_error = f"连接 OBS 失败（{url}）：{e}"
            return False

        self._ws = ws
        self._alive.set()

        # 区分 v5 / v4：v5 连上立刻发 Hello，v4 一声不吭。
        # 但也不能死等——v4 会一直沉默到超时。折中：短超时（1.2s）等 Hello，
        # 等不到就发一条 GetVersion 探路，能拿到响应就是 v4。
        first: dict = {}
        got_hello = False
        try:
            ws.settimeout(1.2)
            raw = ws.recv()
            if raw:
                first = json.loads(raw)
                got_hello = isinstance(first, dict) and first.get("op") == 0
        except Exception:
            got_hello = False

        if got_hello:
            ok = self._handshake_v5(first.get("d") or {})
            self.negotiated = "v5"
        else:
            ok = self._handshake_v4(first)
            if ok:
                self.negotiated = "v4"
        if not ok:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None
            self._alive.clear()
            return False

        try:
            ws.settimeout(None)
        except Exception:
            pass

        if self.negotiated == "v5":
            self.obs_version = str(
                (first.get("d") or {}).get("obsWebSocketVersion") or "")
        else:
            v = getattr(self, "_v4_version", {}) or {}
            obs_v = v.get("obs-studio-version") or v.get("obsVersion") or ""
            ws_v = v.get("obs-websocket-version") or "4.x"
            self.obs_version = f"OBS {obs_v} / WS {ws_v}".strip(" /")

        self._thread = threading.Thread(target=self._recv_loop, name="obs-ws", daemon=True)
        self._thread.start()

        # 确认能干活：拿一次版本信息
        try:
            v = self.call("GetVersion")
            if isinstance(v, dict):
                obs_v = v.get("obsVersion") or v.get("obs-studio-version") or ""
                ws_v = v.get("obsWebSocketVersion") or v.get("obs-websocket-version") or ""
                if obs_v or ws_v:
                    self.obs_version = f"OBS {obs_v} / WS {ws_v}".strip(" /")
        except ObsError:
            pass
        return True

    def _handshake_v5(self, hello: dict) -> bool:
        """v5 握手。

        协议里的 OpCode 容易记混，官方定义是：
            0 = Hello（服务端发）
            1 = Identify（客户端发）   ← 我们要发的这个
            2 = Identified（服务端答） ← 等这个
        把 Identify 当成 2 发出去，服务端会判成「未认证就想发请求」，
        直接以 4007 non-Identify message 断开。
        """
        auth = hello.get("authentication")
        # 用服务端的 rpcVersion 作为基准；超出我们支持的就退回 1
        server_rpc = int(hello.get("rpcVersion", 1) or 1)
        client_rpc = min(server_rpc, 1)
        ident: Dict[str, Any] = {"rpcVersion": client_rpc}
        if auth:
            if not self.password:
                self.last_error = "OBS 设置了密码，但配置里没填"
                return False
            challenge = auth.get("challenge", "")
            salt = auth.get("salt", "")
            ident["authentication"] = _b64_sha256(
                _b64_sha256(self.password + salt) + challenge)
        # 事件订阅掩码：General(1<<0) | Config(1<<1) | Scenes(1<<2) |
        # Outputs(1<<3) 等。只要录制状态和场景变化就够，别收全量。
        ident["eventSubscriptions"] = (1 << 0) | (1 << 2) | (1 << 3)
        try:
            self._ws.send(json.dumps({"op": 1, "d": ident}))
            resp = json.loads(self._ws.recv())
        except Exception as e:
            self.last_error = f"Identify 失败：{e}"
            return False
        if not isinstance(resp, dict):
            self.last_error = f"Identify 响应无法解析：{resp}"
            return False
        op = resp.get("op")
        body = resp.get("d") or {}
        if op == 2:                     # Identified
            self._rpc_version = body.get("negotiatedRpcVersion", client_rpc)
            return True
        if op == 8:                     # Close
            self.last_error = _close_reason(body.get("code", ""),
                                            body.get("reason", ""))
            return False
        self.last_error = (f"认证失败：{body.get('comment') or str(resp)[:200]}")
        return False

    def _handshake_v4(self, first: dict) -> bool:
        """v4 握手：服务端连上后不主动说话，由客户端发 GetAuthRequired 探路。"""
        try:
            resp = self._v4_send_direct({"request-type": "GetAuthRequired",
                                         "message-id": "auth-probe"})
        except Exception as e:
            self.last_error = (f"对端不像 obs-websocket v4：{e}"
                               f"（OBS 28+ 请把协议设为 auto 或 v5）")
            return False
        if not isinstance(resp, dict):
            self.last_error = "对端响应格式不认识（可能不是 obs-websocket）"
            return False
        if resp.get("status") == "error":
            self.last_error = f"v4 握手被拒：{resp.get('error')}"
            return False
        if resp.get("authRequired"):
            if not self.password:
                self.last_error = "OBS 设置了密码，但配置里没填"
                return False
            salt = resp.get("salt", "")
            challenge = resp.get("challenge", "")
            secret = _b64_sha256(self.password + salt)
            auth = _b64_sha256(secret + challenge)
            try:
                r2 = self._v4_send_direct({"request-type": "Auth", "auth": auth,
                                           "message-id": "auth"})
            except Exception as e:
                self.last_error = f"v4 认证失败：{e}"
                return False
            if r2.get("status") == "error":
                self.last_error = f"v4 认证被拒：{r2.get('error')}"
                return False
        # 用 GetVersion 确认真的能用，顺便拿到版本号
        try:
            self._v4_version = self._v4_send_direct(
                {"request-type": "GetVersion", "message-id": "ver"})
        except Exception:
            self._v4_version = {}
        return True

    def _v4_send_direct(self, payload: dict) -> dict:
        """v4 的同步收发：发一条，等到同 message-id 的响应（忽略事件与旧响应）。"""
        self._ws.send(json.dumps(payload))
        want = payload.get("message-id")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                self._ws.settimeout(max(0.5, deadline - time.time()))
                raw = self._ws.recv()
            except Exception as e:
                raise ObsError(f"等待响应超时：{e}")
            if not raw:
                raise ObsError("连接被关闭")
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            # 事件消息没有 message-id，跳过
            if msg.get("message-id") != want:
                continue
            return msg
        raise ObsError("等待响应超时")

    def disconnect(self) -> None:
        self._alive.clear()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        self.negotiated = ""
        with self._lock:
            self._responses.clear()

    # ------------------------------------------------------------------ #
    # 收包
    # ------------------------------------------------------------------ #
    def _recv_loop(self) -> None:
        ws = self._ws
        while self._alive.is_set() and ws is self._ws:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            try:
                self._dispatch(msg)
            except Exception:
                pass
        self._alive.clear()
        # 唤醒所有等待中的请求，并把错误留在旁路表里，
        # 这样 _wait 取到的是一条像样的「连接已断开」，而不是「请求状态丢失」
        with self._lock:
            for rid, slot in self._responses.items():
                slot["error"] = "连接已断开（OBS 关掉了 WebSocket）"
                slot["event"].set()
                self._completed[rid] = {"result": None,
                                        "error": slot["error"]}
            self._responses.clear()

    def _dispatch(self, msg: dict) -> None:
        # ---- v5 ---- #
        if "op" in msg:
            op = msg["op"]
            d = msg.get("d") or {}
            if op == 7:      # RequestResponse
                rid = d.get("requestId")
                self._complete(rid, result=d)
            elif op == 9:    # RequestBatchResponse
                rid = d.get("requestId")
                if rid:
                    self._complete(rid, result=d)
            elif op == 5:    # Event
                et = d.get("eventType", "")
                if self.on_event:
                    try:
                        self.on_event(et, d.get("eventData") or {})
                    except Exception:
                        pass
            return
        # ---- v4 ---- #
        mid = msg.get("message-id")
        if mid:
            self._complete(mid, result=msg)
            return
        if msg.get("update-type") and self.on_event:
            try:
                self.on_event(msg["update-type"], msg)
            except Exception:
                pass

    def _complete(self, rid: str, result=None, error: str = "") -> None:
        """把响应交给等待者。

        ⚠ 这里**只能**填好 slot 并 set 事件，**不能** pop。
        旧版在这里 pop，于是出现一个竞态：
        收包线程比 `_wait` 先跑到时，slot 已被 pop 掉，
        `_wait` 再查 `_responses` 得到 None，只好报「请求状态丢失」——
        而这个异常会被上层 `recording()` 吞成 False，造成「明明在录却以为没录」，
        于是跳过 StopRecord，OBS 继续写文件 → 文件被占用 → 停录失败。
        slot 的生命周期由 `_wait` 负责回收。
        """
        if not rid:
            return
        with self._lock:
            slot = self._responses.get(rid)
            if slot is None:
                # 响应比注册还快：先记下来，_wait 会来取
                self._completed[rid] = {"result": result, "error": error}
                if len(self._completed) > 64:   # 防止异常情况下无限增长
                    for k in list(self._completed)[:32]:
                        self._completed.pop(k, None)
                return
            slot["result"] = result
            slot["error"] = error
        slot["event"].set()

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def call(self, request_type: str, params: Optional[dict] = None,
             timeout: Optional[float] = None) -> Any:
        """发一条请求并等响应。失败抛 ObsError。"""
        if not self.connected:
            raise ObsError("未连接 OBS")
        params = params or {}
        to = timeout or self.timeout
        if self.negotiated == "v5":
            return self._call_v5(request_type, params, to)
        return self._call_v4(request_type, params, to)

    def _new_id(self) -> str:
        with self._lock:
            self._req_id += 1
            return f"galrec-{self._req_id}"

    def _wait(self, rid: str, to: float) -> dict:
        """等某条请求的响应。

        竞态的三种时序都要活着出去：
        1. 正常：先注册 slot，后收到响应；
        2. 响应比 `_wait` 跑得还快：slot 里已经被 `_complete` 填好、事件已 set，
           这里直接拿到（所以 `_complete` 绝不能 pop，见那里的注释）；
        3. 极端情况 slot 还没注册上（理论上 v5 不会，但别赌）：
           给一小段宽限期轮询，仍没有才报错。
        """
        deadline = time.time() + to
        grace = time.time() + min(1.0, max(0.05, to / 4))
        while True:
            with self._lock:
                slot = self._responses.get(rid)
            if slot is not None:
                break
            with self._lock:
                done = self._completed.get(rid)
            if done is not None:
                with self._lock:
                    self._completed.pop(rid, None)
                if done.get("error"):
                    raise ObsError(done["error"])
                return done.get("result") or {}
            if time.time() >= grace:
                raise ObsError(f"请求状态丢失：{rid}")
            time.sleep(0.005)
        if not slot["event"].wait(max(0.01, deadline - time.time())):
            with self._lock:
                self._responses.pop(rid, None)
            raise ObsError(f"请求超时：{rid}")
        with self._lock:
            self._responses.pop(rid, None)
        if slot.get("error"):
            raise ObsError(slot["error"])
        return slot.get("result") or {}

    def _call_v5(self, rt: str, params: dict, to: float) -> Any:
        rid = self._new_id()
        with self._lock:
            self._responses[rid] = {"event": threading.Event()}
        payload = {"op": 6, "d": {"requestType": rt, "requestId": rid,
                                  "requestData": params}}
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            with self._lock:
                self._responses.pop(rid, None)
            raise ObsError(f"发送失败：{e}")
        res = self._wait(rid, to)
        st = res.get("requestStatus") or {}
        if not st.get("result"):
            # 错误码一定要带上：401/501 这些数字本身就是判断依据
            # （501 = OutputNotRunning，即「本来就没在录」）
            code = st.get("code")
            comment = st.get("comment") or ""
            detail = f"{code}" + (f" {comment}" if comment else "") if code is not None \
                else (comment or "未知错误")
            err = ObsError(f"{rt} 失败：{detail}")
            # 带着结构化字段走，上层判断不用再去匹配字符串
            err.code = code          # type: ignore[attr-defined]
            err.request = rt         # type: ignore[attr-defined]
            raise err
        return res.get("responseData") or {}

    def _call_v4(self, rt: str, params: dict, to: float) -> Any:
        rid = self._new_id()
        with self._lock:
            self._responses[rid] = {"event": threading.Event()}
        payload = {"request-type": rt, "message-id": rid}
        payload.update(params)
        try:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            with self._lock:
                self._responses.pop(rid, None)
            raise ObsError(f"发送失败：{e}")
        res = self._wait(rid, to)
        if res.get("status") == "error":
            err = ObsError(f"{rt} 失败：{res.get('error')}")
            err.code = -1            # type: ignore[attr-defined]
            err.request = rt         # type: ignore[attr-defined]
            raise err
        return res

    # ------------------------------------------------------------------ #
    # 业务封装
    # ------------------------------------------------------------------ #
    def get_version(self) -> dict:
        try:
            return self.call("GetVersion") or {}
        except ObsError:
            return {}

    def start_record(self) -> bool:
        """开始录制。已经在录就直接返回 True（幂等）。"""
        st = self.record_status()
        if st.recording:
            return True
        if self.negotiated == "v5":
            try:
                self.call("StartRecord")
            except ObsError as e:
                if "output running" in str(e).lower() or "500" in str(e):
                    return True      # 并发/状态滞后：其实已经在录了
                raise
        else:
            try:
                self.call("StartRecording")
            except ObsError as e:
                if "already" in str(e).lower():
                    return True
                raise
        return True

    def stop_record(self, timeout: Optional[float] = None) -> str:
        """停止录制，返回输出文件路径（拿不到则空串）。

        三种情况要分开处理：

        1. 本来就没在录（v5 是 `OutputNotRunning` / code 501）→ 不是错误，
           抛 ObsNotRunning 让上层安静跳过。旧版把 501 当异常抛，
           用户看到的就是「停录失败：501」，其实根本没什么可停的。
        2. 停录超时（长录像要封盘、写索引，几秒到几十秒）→ 抛 ObsError，
           但**不要**把这当成「还在录」，上层会自己复核状态。
        3. 成功 → 返回 outputPath。
        """
        to = timeout or self.timeout
        if self.negotiated == "v5":
            try:
                data = self.call("StopRecord", timeout=to) or {}
                return str(data.get("outputPath") or "")
            except ObsError as e:
                if is_not_running_error(str(e)):
                    raise ObsNotRunning(str(e))
                raise
        try:
            self.call("StopRecording", timeout=to)
        except ObsError as e:
            if is_not_running_error(str(e)):
                raise ObsNotRunning(str(e))
            raise
        return ""

    def record_status(self) -> ObsStatus:
        if self.negotiated == "v5":
            d = self.call("GetRecordStatus")
            rec = bool(d.get("outputActive"))
            return ObsStatus(
                connected=True, protocol="v5",
                recording=rec,
                output_active=rec,
                recording_paused=bool(d.get("outputPaused")),
                duration=float(d.get("outputDuration") or 0) / 1000.0,
                bytes=int(d.get("outputBytes") or 0),
            )
        d = self.call("GetRecordingStatus")
        rec = bool(d.get("isRecording"))
        return ObsStatus(
            connected=True, protocol="v4",
            recording=rec,
            output_active=rec,
            recording_paused=bool(d.get("isRecordingPaused")),
            duration=_tc_to_sec(d.get("recordTimecode", "")),
            bytes=int(d.get("recordBytes") or 0),
        )

    def output_ready(self) -> bool:
        """采集输出是否在跑。

        注意：v5 的 GetOutputStatus 需要的是**输出名**，录制输出在不同 OBS 版本里
        叫 `record` / `OBS_WEBSOCKET_OUTPUT_RECORD`，查不到会回 600。
        所以这里只当「附加信息」，不拿它当开录成败的判据——
        真正的判据是 `wait_recording_live()` 里「时长/字节有没有在涨」。
        """
        try:
            if self.negotiated == "v5":
                for name in ("record", "OBS_WEBSOCKET_OUTPUT_RECORD"):
                    try:
                        d = self.call("GetOutputStatus",
                                      {"outputName": name}) or {}
                        return bool(d.get("outputActive"))
                    except ObsError:
                        continue
                return True
            return True
        except ObsError:
            return True

    def wait_recording_live(self, timeout: float = 6.0,
                            settle: float = 1.0) -> Tuple[bool, str]:
        """确认 StartRecord 之后 OBS **真的**在写数据。

        OBS 有个很容易踩的坑（实测 OBS 33 / obs-websocket 5.7）：
        刚停录 1~2 秒内再 StartRecord，OBS 会返回成功、`outputActive` 也一度是 true，
        但 `outputDuration` 永远是 0、一个字节都不写，随后 StopRecord 直接给 501，
        这一整段等于白录。

        所以不能只看 outputActive，要同时满足：
        - outputActive 为 true；
        - 连续 `settle` 秒内 outputDuration 或 outputBytes 在**增长**
          （说明编码器真的在出数据）。

        返回 (是否真在录, 没真录的原因)。
        """
        end = time.time() + max(1.0, timeout)
        last_dur = 0.0
        last_bytes = 0
        last_progress = 0.0
        started = False
        last_err = ""
        while time.time() < end:
            try:
                data = self.call("GetRecordStatus") or {}
            except ObsError as e:
                last_err = str(e)
                time.sleep(0.25)
                continue
            active = bool(data.get("outputActive"))
            dur = float(data.get("outputDuration") or 0) / 1000.0
            nbytes = int(data.get("outputBytes") or 0)
            last_err = ""
            if not active:
                started = False
                last_dur, last_bytes = 0.0, 0
                last_progress = 0.0
                time.sleep(0.25)
                continue
            if not started:
                started = True
                last_progress = time.time()
            if dur > last_dur or nbytes != last_bytes:
                # 有数据在动：这就是活的录制
                if time.time() - last_progress >= settle and (dur > 0 or nbytes > 0):
                    return True, ""
            else:
                last_progress = time.time()
            last_dur = max(last_dur, dur)
            last_bytes = nbytes
            time.sleep(0.25)
        if last_err:
            return False, last_err
        return False, ("OBS 报了「正在录制」但不产生任何数据"
                       "（常见于刚停录后立刻重开；已自动回避）")

    def get_record_dir(self) -> str:
        rt = "GetRecordDirectory" if self.negotiated == "v5" else "GetRecordingFolder"
        try:
            d = self.call(rt)
        except ObsError:
            return ""
        return str(d.get("recordDirectory") or d.get("rec-folder") or "")

    def set_record_dir(self, path: str) -> bool:
        os.makedirs(path, exist_ok=True)
        rt = "SetRecordDirectory" if self.negotiated == "v5" else "SetRecordingFolder"
        key = "recordDirectory" if self.negotiated == "v5" else "rec-folder"
        try:
            self.call(rt, {key: path})
            return True
        except ObsError:
            return False

    def set_scene(self, scene: str) -> bool:
        if not scene:
            return False
        rt = "SetCurrentProgramScene" if self.negotiated == "v5" else "SetCurrentScene"
        key = "sceneName" if self.negotiated == "v5" else "scene-name"
        try:
            self.call(rt, {key: scene})
            return True
        except ObsError:
            return False

    def list_scenes(self) -> List[str]:
        try:
            if self.negotiated == "v5":
                d = self.call("GetSceneList")
                return [s.get("sceneName", "") for s in d.get("scenes", [])]
            d = self.call("GetSceneList")
            return [s.get("name", "") for s in d.get("scenes", [])]
        except ObsError:
            return []

    def snapshot_files(self) -> Dict[str, float]:
        """记录录制目录里现有文件 → 用来事后找出新产生的录像。"""
        d = self.get_record_dir()
        out: Dict[str, float] = {}
        if not d or not os.path.isdir(d):
            return out
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    out[p] = os.path.getmtime(p)
        except Exception:
            pass
        return out

    @staticmethod
    def new_files(before: Dict[str, float], after: Dict[str, float]) -> List[str]:
        return [p for p in after if p not in before or after[p] != before.get(p)]


def _tc_to_sec(tc: str) -> float:
    try:
        parts = str(tc).split(":")
        parts = [p for p in parts if p != ""]
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except Exception:
        pass
    return 0.0


# obs-websocket v5 的 Close 码
_CLOSE_CODES = {
    4004: "OBS 未启用 WebSocket 服务器（也可能是端口填错）",
    4005: "已连接但握手超时",
    4008: "认证失败：密码不正确",
    4009: "认证失败：密码不正确",
    4010: "认证方式不受支持（请更新 OBS 或插件）",
    4011: "认证失败：服务端要求加密连接",
}


def _close_reason(code, reason: str = "") -> str:
    try:
        c = int(code)
    except Exception:
        return f"OBS 关闭了连接：{reason or code}"
    tip = _CLOSE_CODES.get(c)
    if tip:
        return tip + (f"（{reason}）" if reason else "")
    return f"OBS 关闭了连接：{code} {reason}".strip()


def probe(host: str, port: int, password: str = "", protocol: str = "auto",
          timeout: float = 3.0, auto_port: bool = True) -> ObsStatus:
    """快速探测 OBS 是否在线（界面「测试连接」用）。

    auto_port=True 时，若给定端口不通，会再试 4455 / 4444 / 4456。
    原因是 obs-websocket 4.x（OBS 27 插件）默认监听 **4444**，
    而 5.x（OBS 28+ 内建）默认 **4455**——用户装完插件后往往不知道要改端口。
    """
    cands = [port]
    if auto_port:
        cands = [port] + [p for p in (4455, 4444, 4456) if p != port]
    last_err = ""
    for p in cands:
        c = ObsClient(host, p, password, protocol, timeout)
        if c.connect(timeout=timeout):
            st = ObsStatus(connected=True, protocol=c.negotiated,
                           obs_version=c.obs_version, port=p)
            try:
                rs = c.record_status()
                st.recording = rs.recording
                st.duration = rs.duration
            except ObsError:
                pass
            st.record_dir = c.get_record_dir()
            c.disconnect()
            return st
        last_err = c.last_error
    st = ObsStatus()
    st.message = last_err
    return st


def find_open_port(host: str = "127.0.0.1",
                   ports=(4455, 4444, 4456), timeout: float = 1.0) -> int:
    """在候选端口里找第一个能连上的（用来给自动连接定端口）。"""
    import socket as _s
    for p in ports:
        s = _s.socket()
        s.settimeout(timeout)
        try:
            s.connect((host, p))
            return p
        except Exception:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return 0
