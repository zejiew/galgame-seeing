"""OBS 27 便携版 + obs-websocket 4.9.1 的端到端测试。

这个脚本做三件事：
1. 在独立目录里准备一份「便携版 OBS」（不动用户现有 OBS 安装与配置）
2. 启动它，等 WebSocket 起来
3. 用本程序的 ObsClient 连上去，真实执行 开始录制 → 停 → 检查文件

跑法： python tests/e2e_obs.py [--keep]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from galrec import obs as obsmod  # noqa: E402

OBS_SRC = r"C:\Program Files\obs-studio"
PORTABLE = os.path.join(BASE, "_obsportable")
PLUGIN_ZIP = os.path.join(BASE, "_dl", "obs-websocket-4.9.1.zip")
REC_DIR = os.path.join(BASE, "recordings")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def port_open(port: int, timeout: float = 2.0) -> bool:
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
    """准备便携版 OBS（幂等，已存在就复用）。"""
    exe = os.path.join(PORTABLE, "bin", "64bit", "obs64.exe")
    if not os.path.isfile(exe):
        log("复制 OBS 到便携目录…")
        if os.path.isdir(PORTABLE):
            shutil.rmtree(PORTABLE, ignore_errors=True)
        shutil.copytree(OBS_SRC, PORTABLE)
        open(os.path.join(PORTABLE, "portable_mode.txt"), "w").close()

    # 插件
    dll = os.path.join(PORTABLE, "obs-plugins", "64bit", "obs-websocket.dll")
    if not os.path.isfile(dll):
        if not os.path.isfile(PLUGIN_ZIP):
            log(f"缺少插件包：{PLUGIN_ZIP}")
            return False
        log("安装 obs-websocket 插件…")
        import zipfile
        tmp = os.path.join(PORTABLE, "_z")
        with zipfile.ZipFile(PLUGIN_ZIP) as z:
            z.extractall(tmp)
        shutil.copy2(os.path.join(tmp, "obs-plugins", "64bit", "obs-websocket.dll"),
                     os.path.join(PORTABLE, "obs-plugins", "64bit"))
        data_src = os.path.join(tmp, "data", "obs-plugins", "obs-websocket")
        data_dst = os.path.join(PORTABLE, "data", "obs-plugins", "obs-websocket")
        if os.path.isdir(data_src):
            shutil.copytree(data_src, data_dst, dirs_exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)

    # WebSocket 配置：启用、无密码
    cfg_dir = os.path.join(PORTABLE, "config", "obs-studio",
                           "plugin_config", "obs-websocket")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {"server_enabled": True, "server_port": 4455,
           "auth_required": False, "server_password": ""}
    with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)

    os.makedirs(REC_DIR, exist_ok=True)
    return True


def start_obs() -> subprocess.Popen | None:
    exe = os.path.join(PORTABLE, "bin", "64bit", "obs64.exe")
    log("启动便携版 OBS…")
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
    # obs-websocket 4.x（OBS 27 插件）默认监听 4444，5.x 才是 4455，两个都试
    for i in range(40):
        time.sleep(1.5)
        if p.poll() is not None:
            log(f"OBS 提前退出，返回码 {p.returncode}")
            return None
        for port in (4455, 4444):
            if port_open(port, 0.8):
                log(f"WebSocket 端口 {port} 已就绪（{i*1.5+1.5:.0f}s）")
                return p
        if i % 5 == 4:
            log(f"  等待中… {int(i*1.5+1.5)}s")
    log("等待 WebSocket 超时")
    return p


def main() -> int:
    keep = "--keep" in sys.argv
    if not prepare():
        return 1
    proc = start_obs()
    if not proc:
        return 1

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        log(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))

    try:
        # ---- 1. 自动协议探测（本程序模拟「OBS 27 + v4 插件」场景）----
        log("\n[1] 自动协议协商（含端口自动发现）")
        # 故意填 4455（错的），验证程序能自己找到 4444
        st = obsmod.probe("127.0.0.1", 4455, "", "auto", timeout=6)
        check("连接成功", st.connected, st.obs_version or st.message)
        check("识别为 v4", st.protocol == "v4", f"实际 {st.protocol}")
        check("自动发现端口 4444", st.port == 4444, f"实际 {st.port}")
        if not st.connected:
            return 1

        c = obsmod.ObsClient("127.0.0.1", 4444, "", "auto", timeout=8)
        if not c.connect():
            log(f"连接失败：{c.last_error}")
            return 1
        check("客户端协商", c.negotiated == "v4", c.negotiated)

        # ---- 2. 录制目录 ----
        log("\n[2] 录制目录")
        old = c.get_record_dir()
        log(f"  原目录：{old}")
        check("设置新目录", c.set_record_dir(REC_DIR), REC_DIR)
        check("回读一致", os.path.normcase(os.path.abspath(c.get_record_dir()))
              == os.path.normcase(os.path.abspath(REC_DIR)), c.get_record_dir())

        # ---- 3. 录制 ----
        log("\n[3] 开始录制 → 停 3 秒 → 停止")
        before = c.snapshot_files()
        for f in list(before):
            try:
                os.remove(f)
            except Exception:
                pass
        c.start_record()
        time.sleep(1.0)
        rs = c.record_status()
        check("录制中", rs.recording, f"duration={rs.duration:.1f}s")
        time.sleep(3.0)
        c.stop_record()
        time.sleep(2.5)

        after = c.snapshot_files()
        new = obsmod.ObsClient.new_files({
            p: 0 for p in before}, after)
        check("产生了录像文件", len(new) > 0, f"{[os.path.basename(x) for x in new]}")
        if new:
            f = max(new, key=lambda p: after.get(p, 0))
            size = os.path.getsize(f)
            check("文件大小合理", size > 10_000, f"{size/1024:.0f} KB  {os.path.basename(f)}")

        # ---- 4. 场景 ----
        log("\n[4] 场景列表")
        scenes = c.list_scenes()
        check("能列出场景", len(scenes) > 0, str(scenes))

        # ---- 5. 本程序完整链路（FakeBackend 换成真 OBS）----
        log("\n[5] 走本程序的 Controller 完整链路")
        from galrec.backend import WsBackend
        from galrec.config import Config

        cfg = Config()
        cfg.obs.port = 4455          # 故意填错，验证后端能自动发现 4444
        cfg.obs.record_dir = REC_DIR
        cfg.obs.set_record_dir = True
        be = WsBackend(cfg)
        check("后端连接", be.connect(), be.label)
        check("后端自动发现端口", be.port_auto == 4444, f"port_auto={be.port_auto}")
        be.prepare()
        be.start()
        time.sleep(2.0)
        check("后端录制中", be.recording(), f"duration={be.duration():.1f}s")
        path = be.stop()
        check("后端停止并拿到路径", bool(path), path or "(空)")

        c.disconnect()
    finally:
        if not keep:
            log("\n关闭 OBS…")
            try:
                c2 = obsmod.ObsClient("127.0.0.1", 4455, "", "auto", timeout=3)
                if c2.connect():
                    try:
                        c2.call("Quit")
                    except Exception:
                        pass
                    c2.disconnect()
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
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print("=" * 56)
    print("端到端通过 ✔" if ok_all else "有失败项 ✘")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
