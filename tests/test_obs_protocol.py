"""OBS WebSocket 协议握手 + 录制控制的离线单元测试。

用本地假服务端模拟 v5 / v4 两种协议，验证客户端能正确握手、发请求、解析响应。
不需要装 OBS，也不需要 obs-websocket 插件。
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


def _b64sha(s: str) -> str:
    return base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()


class FakeObsV5:
    """最小的 v5 服务端：Hello → Identify → 处理 GetVersion/StartRecord 等。"""

    def __init__(self, password: str = ""):
        self.password = password
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.recording = False
        self.stopped_path = ""
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _serve(self):
        conn, _ = self.sock.accept()
        # 先完成 HTTP Upgrade 握手，否则 websocket-client 连不上
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
        self._ws_recv_loop(conn)

    def _ws_recv_loop(self, conn):
        # 极简 WebSocket 帧处理（只处理文本帧、不掩码响应）
        def recv_frame():
            hdr = conn.recv(2)
            if len(hdr) < 2:
                return None
            b1, b2 = hdr[0], hdr[1]
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = int.from_bytes(conn.recv(2), "big")
            elif ln == 127:
                ln = int.from_bytes(conn.recv(8), "big")
            mask = conn.recv(4) if masked else b""
            data = b""
            while len(data) < ln:
                chunk = conn.recv(ln - len(data))
                if not chunk:
                    return None
                data += chunk
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            return opcode, data

        def send_frame(payload: str):
            data = payload.encode()
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
            conn.sendall(bytes(hdr) + data)

        salt = "salt123"
        challenge = "chal456"
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.0.1", "rpcVersion": 1}}
        if self.password:
            hello["d"]["authentication"] = {"challenge": challenge, "salt": salt}
        send_frame(json.dumps(hello))

        # 等 Identify（客户端发 op=1）
        while True:
            fr = recv_frame()
            if not fr:
                return
            opcode, data = fr
            if opcode == 8:
                return
            msg = json.loads(data)
            if msg.get("op") != 1:
                continue
            d = msg.get("d") or {}
            if self.password:
                expect = _b64sha(_b64sha(self.password + salt) + challenge)
                if d.get("authentication") != expect:
                    # 真实的 obs-websocket v5 认证失败会回 Close(op=8, code=4009)
                    send_frame(json.dumps({"op": 8, "d": {"code": 4009,
                                                          "reason": "Authentication failed."}}))
                    print("  [fake v5] 认证失败（认证码不符），已回 Close 4009")
                    conn.close()
                    return
            send_frame(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
            break

        # 处理请求
        while True:
            fr = recv_frame()
            if not fr:
                return
            opcode, data = fr
            if opcode == 8:
                return
            msg = json.loads(data)
            if msg.get("op") != 6:
                continue
            d = msg.get("d") or {}
            rt = d.get("requestType")
            rid = d.get("requestId")
            status = {"result": True, "code": 100}
            resp = {}
            if rt == "GetVersion":
                resp = {"obsVersion": "27.2.4", "obsWebSocketVersion": "5.0.1",
                        "rpcVersion": 1}
            elif rt == "GetRecordStatus":
                resp = {"outputActive": self.recording, "outputPaused": False,
                        "outputDuration": 1234, "outputBytes": 4096}
            elif rt == "StartRecord":
                self.recording = True
            elif rt == "StopRecord":
                self.recording = False
                self.stopped_path = r"C:\rec\test.mkv"
                resp = {"outputPath": self.stopped_path}
            elif rt == "GetRecordDirectory":
                resp = {"recordDirectory": r"C:\rec"}
            elif rt == "SetRecordDirectory":
                resp = {}
            elif rt == "GetSceneList":
                resp = {"scenes": [{"sceneName": "Main"}, {"sceneName": "Alt"}],
                        "currentProgramSceneName": "Main"}
            elif rt == "SetCurrentProgramScene":
                resp = {}
            else:
                status = {"result": False, "code": 204, "comment": "unknown"}
            send_frame(json.dumps({
                "op": 7,
                "d": {"requestType": rt, "requestId": rid,
                      "requestStatus": status, "responseData": resp},
            }))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def test_v5(password: str = "") -> bool:
    print(f"\n=== 测试 v5（密码={password!r}）===")
    srv = FakeObsV5(password).start()
    c = obsmod.ObsClient("127.0.0.1", srv.port, password, "v5", timeout=4)
    ok = True
    if not c.connect():
        print("  ✗ 连接失败:", c.last_error)
        return False
    print("  ✓ 握手成功，协商协议:", c.negotiated)
    if c.negotiated != "v5":
        print("  ✗ 协议识别错误")
        ok = False
    v = c.get_version()
    print("  ✓ GetVersion:", v.get("obsVersion"))
    if v.get("obsVersion") != "27.2.4":
        ok = False
    c.start_record()
    st = c.record_status()
    print(f"  ✓ 开始录制 → recording={st.recording}")
    if not st.recording:
        ok = False
    path = c.stop_record()
    print(f"  ✓ 停止录制 → path={path}")
    if path != srv.stopped_path:
        print("  ✗ 停止返回的路径不对")
        ok = False
    scenes = c.list_scenes()
    print("  ✓ 场景列表:", scenes)
    if "Main" not in scenes:
        ok = False
    print("  ✓ 录制目录:", c.get_record_dir())
    c.disconnect()
    srv.close()
    print("  结果:", "通过 ✔" if ok else "失败 ✘")
    return ok


class FakeObsV4:
    """最小的 v4 服务端：裸 JSON 请求/响应 + Auth。"""

    def __init__(self, password: str = ""):
        self.password = password
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.recording = False
        self.authenticated = not bool(password)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
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
            return json.loads(data)

        salt, challenge = "vsalt", "vchal"
        send({"obs-websocket-version": "4.9.1",
              "obs-studio-version": "27.2.4",
              "authRequired": bool(self.password),
              "salt": salt if self.password else "",
              "challenge": challenge if self.password else "",
              "available-requests": "GetAuthRequired,Auth,GetVersion,StartRecording"})

        while True:
            msg = recv()
            if msg is None:
                return
            mid = msg.get("message-id")
            rt = msg.get("request-type")
            if rt == "GetAuthRequired":
                send({"message-id": mid, "status": "ok",
                      "authRequired": bool(self.password),
                      "salt": salt, "challenge": challenge})
                continue
            if rt == "Auth":
                secret = _b64sha(self.password + salt)
                want = _b64sha(secret + challenge)
                if msg.get("auth") == want:
                    self.authenticated = True
                    send({"message-id": mid, "status": "ok"})
                else:
                    send({"message-id": mid, "status": "error", "error": "bad auth"})
                continue
            if rt == "GetVersion":
                send({"message-id": mid, "status": "ok",
                      "obs-websocket-version": "4.9.1",
                      "obs-studio-version": "27.2.4"})
            elif rt == "GetRecordingStatus":
                send({"message-id": mid, "status": "ok",
                      "isRecording": self.recording,
                      "isRecordingPaused": False,
                      "recordTimecode": "00:00:12.500",
                      "recordBytes": 4096})
            elif rt == "StartRecording":
                self.recording = True
                send({"message-id": mid, "status": "ok"})
            elif rt == "StopRecording":
                self.recording = False
                send({"message-id": mid, "status": "ok"})
            elif rt == "GetRecordingFolder":
                send({"message-id": mid, "status": "ok", "rec-folder": r"C:\rec"})
            elif rt == "GetSceneList":
                send({"message-id": mid, "status": "ok",
                      "current-scene": "Main",
                      "scenes": [{"name": "Main"}, {"name": "Alt"}]})
            else:
                send({"message-id": mid, "status": "ok"})


def test_v4(password: str = "") -> bool:
    print(f"\n=== 测试 v4（密码={password!r}）===")
    srv = FakeObsV4(password)
    time.sleep(0.1)
    c = obsmod.ObsClient("127.0.0.1", srv.port, password, "v4", timeout=4)
    ok = True
    if not c.connect():
        print("  ✗ 连接失败:", c.last_error)
        return False
    print("  ✓ 握手成功，协商协议:", c.negotiated)
    if c.negotiated != "v4":
        print("  ✗ 协议识别错误")
        ok = False
    v = c.get_version()
    print("  ✓ GetVersion:", v.get("obs-studio-version") or v.get("obsVersion"))
    c.start_record()
    st = c.record_status()
    print(f"  ✓ 开始录制 → recording={st.recording}, duration={st.duration}")
    if not st.recording or abs(st.duration - 12.5) > 0.01:
        print("  ✗ 状态解析错误")
        ok = False
    c.stop_record()
    print("  ✓ 停止录制")
    print("  ✓ 场景列表:", c.list_scenes())
    print("  ✓ 录制目录:", c.get_record_dir())
    c.disconnect()
    print("  结果:", "通过 ✔" if ok else "失败 ✘")
    return ok


def test_wrong_password() -> bool:
    print("\n=== 测试错误密码必须被拒绝 ===")
    srv = FakeObsV5("correct").start()
    c = obsmod.ObsClient("127.0.0.1", srv.port, "wrong", "v5", timeout=4)
    ok_conn = c.connect()
    c.disconnect()
    srv.close()
    if ok_conn:
        print("  ✗ 错误密码竟然连上了")
        return False
    print("  ✓ 正确拒绝:", c.last_error)
    return True


def test_no_obs() -> bool:
    print("\n=== 测试端口不通时的错误信息 ===")
    c = obsmod.ObsClient("127.0.0.1", 1, "", "auto", timeout=2)
    ok = c.connect()
    c.disconnect()
    if ok:
        print("  ✗ 不应该连上")
        return False
    print("  ✓ 报错正常:", c.last_error[:80])
    return True


def test_hotkey_parse() -> bool:
    print("\n=== 测试热键解析 ===")
    from galrec import hotkey as hk
    cases = [
        ("ctrl+alt+f9", True), ("Ctrl+Shift+F10", True),
        ("f12", True), ("0x77", True), ("badkey", False), ("", False),
    ]
    ok = True
    for src, want in cases:
        got = hk.parse_accel(src) is not None
        ok = ok and (got == want)
        print(f"  {'✓' if got == want else '✗'} {src!r} → {got} (期望 {want})")
    return ok


def test_dialogue_filter() -> bool:
    print("\n=== 测试台词过滤（拦截 UI / 代码） ===")
    from galrec.ocr import looks_like_dialogue_line as f
    cases = [
        ("你这家伙，真是让人伤脑筋啊。", True),
        ("那么，走吧。", True),
        ("# 这个函数用于计算总价", False),      # 有 2 个 UI 词？测 length
        ("https://example.com/a/b/c/d", False),
        ("def main(): print('x')", False),
        ("C:\\Users\\a\\b\\c\\d", False),
        ("x", False),
        ("思考" * 80, False),
    ]
    ok = True
    for text, want in cases:
        got = f(text)
        flag = "✓" if got == want else "✗"
        ok = ok and (got == want)
        print(f"  {flag} {want!s:5} {text[:40]!r} → {got}")
    return ok


def main():
    results = []
    results.append(("v5 无密码", test_v5("")))
    results.append(("v5 有密码", test_v5("s3cret")))
    results.append(("v4 无密码", test_v4("")))
    results.append(("v4 有密码", test_v4("s3cret")))
    results.append(("错误密码被拒", test_wrong_password()))
    results.append(("端口不通", test_no_obs()))
    results.append(("热键解析", test_hotkey_parse()))
    results.append(("台词过滤", test_dialogue_filter()))

    print("\n" + "=" * 50)
    allok = True
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
        allok = allok and ok
    print("=" * 50)
    print("全部通过 ✔" if allok else "有失败项 ✘")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
