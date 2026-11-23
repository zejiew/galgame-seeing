"""OBS 33（内建 obs-websocket 5.x）的端到端测试。

与 e2e_obs.py 的区别：这里测的是 **v5 协议**，也就是用户升级 OBS 后的真实场景。
便携版放在 _obs33/，不动用户现有安装。

跑法： python tests/e2e_obs_v5.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from galrec import obs as obsmod  # noqa: E402

PORTABLE = os.path.join(BASE, "_obs33")
ZIP = os.path.join(BASE, "_dl", "obs3303.zip")
REC_DIR = os.path.join(BASE, "recordings_v5")
PORT = 4455


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def port_open(port: int, timeout: float = 1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def prepare() -> bool:
    exe = os.path.join(PORTABLE, "bin", "64bit", "obs64.exe")
    if not os.path.isfile(exe):
        if not os.path.isfile(ZIP):
            log(f"缺少 OBS 33 压缩包：{ZIP}")
            return False
        log("解压 OBS 33…")
        os.makedirs(PORTABLE, exist_ok=True)
        with zipfile.ZipFile(ZIP) as z:
            z.extractall(PORTABLE)
    open(os.path.join(PORTABLE, "portable_mode.txt"), "w").close()

    # WebSocket：开、不要求鉴权（OBS 33 会自己生成密码，但不开启就不校验）
    cfg_dir = os.path.join(PORTABLE, "config", "obs-studio",
                           "plugin_config", "obs-websocket")
    os.makedirs(cfg_dir, exist_ok=True)
    p = os.path.join(cfg_dir, "config.json")
    cfg = {}
    if os.path.isfile(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.update({"server_enabled": True, "server_port": PORT,
                "auth_required": False, "alerts_enabled": False,
                "first_load": False})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)

    os.makedirs(REC_DIR, exist_ok=True)
    return True


def start_obs() -> subprocess.Popen | None:
    exe = os.path.join(PORTABLE, "bin", "64bit", "obs64.exe")
    if port_open(PORT):
        log("已有 OBS 占用端口，复用")
        return None
    log("启动 OBS 33…")
    try:
        p = subprocess.Popen(
            [exe, "--portable", "--disable-shutdown-check"],
            cwd=os.path.join(PORTABLE, "bin", "64bit"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        log(f"启动失败：{e}")
        return None
    for i in range(40):
        time.sleep(1.5)
        if p.poll() is not None:
            log(f"OBS 提前退出（{p.returncode}）")
            return None
        if port_open(PORT):
            log(f"WebSocket {PORT} 就绪（{i*1.5+1.5:.0f}s）")
            return p
    log("等端口超时")
    return p


def ensure_source(c) -> None:
    """确保场景里有画面源——OBS 没有源时录制不会真正产出文件。"""
    try:
        scenes = c.list_scenes()
        if not scenes:
            return
        scene = scenes[0]
        items = (c.call("GetSceneItemList", {"sceneName": scene}) or {}).get("sceneItems", [])
        if items:
            return
        log("  场景里没有源，自动添加显示器捕获…")
        a = c.call("CreateInput", {
            "sceneName": scene,
            "inputName": "galgame-screen",
            "inputKind": "monitor_capture",
            "inputSettings": {"monitor": 0, "capture_cursor": True},
            "sceneItemEnabled": True,
        }) if callable(getattr(c, "call", None)) else None
        # 新加的源需要几秒建立渲染管线，否则前几秒录不到画面（0 字节文件）
        log("  等待画面源就绪（5 秒）…")
        time.sleep(5.0)
    except Exception as e:
        log(f"  添加源失败（不影响连接测试）：{e}")


def main() -> int:
    if not prepare():
        return 1
    proc = start_obs()
    if not port_open(PORT):
        log("端口始终没开，放弃")
        return 1

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        log(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))

    try:
        # ---------- 1. 连接 ----------
        log("\n[1] v5 连接与协商")
        st = obsmod.probe("127.0.0.1", PORT, "", "auto", timeout=6)
        check("连接成功", st.connected, st.message)
        check("识别为 v5", st.protocol == "v5", f"实际 {st.protocol}")
        check("版本正确", "33" in (st.obs_version or ""), st.obs_version)
        if not st.connected:
            return 1

        c = obsmod.ObsClient("127.0.0.1", PORT, "", "auto", timeout=8)
        check("客户端连上", c.connect(), c.last_error)
        ensure_source(c)

        # ---------- 2. 录制目录 ----------
        log("\n[2] 录制目录")
        check("设置目录", c.set_record_dir(REC_DIR), REC_DIR)
        check("回读一致",
              os.path.normcase(os.path.abspath(c.get_record_dir()))
              == os.path.normcase(os.path.abspath(REC_DIR)),
              c.get_record_dir())

        # ---------- 3. 真实录制 ----------
        log("\n[3] 真实录制 4 秒")
        for f in os.listdir(REC_DIR):
            try:
                os.remove(os.path.join(REC_DIR, f))
            except Exception:
                pass
        before = c.snapshot_files()
        c.start_record()
        # 首帧编码器要预热，前 2 秒可能还没写出数据；多等一会儿再判状态
        time.sleep(3.5)
        rs = c.record_status()
        check("状态为录制中", rs.recording, f"duration={rs.duration:.1f}s")
        time.sleep(3.0)
        path = c.stop_record()
        check("StopRecord 返回路径", bool(path), path)
        time.sleep(2.0)

        after = c.snapshot_files()
        new = [p for p in after if p not in before]
        check("目录里出现新文件", bool(new),
              str([os.path.basename(x) for x in new]))
        if new:
            f = max(new, key=lambda p: after.get(p, 0))
            size = os.path.getsize(f)
            check("文件大小合理", size > 20_000, f"{size/1024:.0f} KB")
            check("返回路径与文件一致",
                  os.path.normcase(os.path.abspath(path))
                  == os.path.normcase(os.path.abspath(f)),
                  f"{path} vs {f}")

        # ---------- 4. 场景 ----------
        log("\n[4] 场景操作")
        scenes = c.list_scenes()
        check("列出场景", len(scenes) > 0, str(scenes))
        if scenes:
            check("切换场景", c.set_scene(scenes[0]), scenes[0])

        c.disconnect()

        # ---------- 5. 本程序 Controller 全链路 ----------
        log("\n[5] Controller 全链路（真 OBS + 假检测）")
        from galrec.backend import WsBackend
        from galrec.config import Config
        from galrec.recorder import Controller

        cfg = Config()
        cfg.obs.port = PORT
        cfg.obs.record_dir = REC_DIR
        cfg.obs.set_record_dir = True
        cfg.obs.stop_delay = 0.5
        cfg.detect.min_record_seconds = 2.0

        ctrl = Controller(cfg, log=lambda m, l="info": log(f"    [{l}] {m}"))
        ok = ctrl.start(hwnd=0)
        check("Controller 启动", ok)
        if ok:
            # 直接触发一次开录 / 停录，验证整条链路
            ctrl.detector.force_start()
            time.sleep(3.0)
            check("Controller 已开录", ctrl.backend.recording(),
                  f"{ctrl.backend.duration():.1f}s")
            ctrl.detector.force_stop()
            time.sleep(3.0)
            check("Controller 已停录并记录",
                  len(ctrl.sessions) >= 1,
                  f"{[(s.duration, s.reason) for s in ctrl.sessions]}")
            if ctrl.sessions:
                s = ctrl.sessions[0]
                check("录像文件存在", bool(s.path) and os.path.isfile(s.path),
                      s.path)
            ctrl.stop()

        # ---------- 6. 错误密码 ----------
        log("\n[6] 认证行为")
        # OBS 33 未开鉴权时，任何密码都能连（auth_required=false）
        pwd_cfg = os.path.join(PORTABLE, "config", "obs-studio",
                               "plugin_config", "obs-websocket", "config.json")
        cfgd = json.load(open(pwd_cfg, encoding="utf-8"))
        cfgd["auth_required"] = True
        cfgd["server_password"] = "test1234"
        json.dump(cfgd, open(pwd_cfg, "w", encoding="utf-8"), indent=4)
        log("  （已改成需要密码，重启 OBS 才能生效——此项仅记录，不重启）")

    finally:
        log("\n关闭 OBS…")
        try:
            c3 = obsmod.ObsClient("127.0.0.1", PORT, "", "auto", timeout=3)
            if c3.connect():
                try:
                    c3.call("Quit")
                except Exception:
                    pass
                c3.disconnect()
        except Exception:
            pass
        time.sleep(2)
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    print("\n" + "=" * 56)
    ok_all = all(ok for _, ok in results)
    for n, ok in results:
        print(f"  {'✓' if ok else '✗'} {n}")
    print("=" * 56)
    print("OBS 33 (v5) 端到端通过 ✔" if ok_all else "有失败项 ✘")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
