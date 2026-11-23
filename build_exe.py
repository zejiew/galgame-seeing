"""PyInstaller 打包脚本。

用法：
    python build_exe.py            打包（目录模式，启动快）
    python build_exe.py --onefile  打包成单文件（体积大、启动慢，但只有一个 exe）

产物在 dist/Galgame自动录制/ 下：Galgame自动录制.exe

打包要点：
- rapidocr 的 onnx 模型是数据文件，必须显式带上，否则运行时报「找不到模型」
- tkinter 由 PyInstaller 自动处理，不用管
- 窗口程序用 --noconsole，避免弹出黑色命令行窗口
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
# exe 用英文名：一是避免中文名在 PyInstaller 的 spec 里被 GBK 读成乱码，
# 二是中文名的新 exe 更容易被杀软拦下。窗标题仍然是中文，用户看不出区别。
APP_NAME = "GalgameAutoRec"
ENTRY = os.path.join(BASE, "main.py")


def find_rapidocr_data() -> list:
    """收集 rapidocr 需要的数据文件（模型 + 配置）。"""
    try:
        import rapidocr_onnxruntime
    except Exception as e:
        print(f"!! 找不到 rapidocr：{e}")
        return []
    root = os.path.dirname(rapidocr_onnxruntime.__file__)
    pairs = []
    # 整个包目录一起带过去，最省事也最不容易漏
    pairs.append((root, "rapidocr_onnxruntime"))
    return pairs


# onnxruntime 里这些是 GPU/特殊后端，本程序只用 CPU，打包时删掉能省两百多 MB。
_ORT_DROP = (
    "onnxruntime_providers_cuda.dll",
    "onnxruntime_providers_tensorrt.dll",
    "onnxruntime_providers_shared.dll",
    "onnxruntime_providers_dml.dll",
    "onnxruntime_providers_openvino.dll",
)

# cv2 内部带了 4 个版本的 ffmpeg 视频读写后端（每个 25~30 MB），
# 本程序只把 numpy 数组交给 rapidocr，从不读写视频文件，这些可以删。
# 注意：cv2 本身必须保留——rapidocr 内部 import cv2，删掉整包 OCR 会直接崩。
_CV2_DROP_PREFIX = ("opencv_videoio_ffmpeg",)


def slim_down(dist_dir: str) -> None:
    """打包后删掉用不到的大块内容，压缩体积。"""
    internal = os.path.join(dist_dir, "_internal")
    if not os.path.isdir(internal):
        return
    removed = 0
    capi = os.path.join(internal, "onnxruntime", "capi")
    if os.path.isdir(capi):
        for name in _ORT_DROP:
            p = os.path.join(capi, name)
            if os.path.isfile(p):
                try:
                    removed += os.path.getsize(p)
                    os.remove(p)
                except Exception:
                    pass
    cv2dir = os.path.join(internal, "cv2")
    if os.path.isdir(cv2dir):
        for name in os.listdir(cv2dir):
            if name.startswith(_CV2_DROP_PREFIX):
                p = os.path.join(cv2dir, name)
                try:
                    removed += os.path.getsize(p)
                    os.remove(p)
                except Exception:
                    pass
    if removed:
        print(f"  精简：删除 {removed/1024/1024:.0f} MB 用不到的运行库")


def build(onefile: bool = False) -> int:
    print("=" * 60)
    print(f" 打包 {APP_NAME}")
    print("=" * 60)

    if not os.path.isfile(ENTRY):
        print(f"!! 找不到入口：{ENTRY}")
        return 1

    # 清掉上次产物
    for d in ("build", "dist"):
        p = os.path.join(BASE, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    spec = os.path.join(BASE, f"{APP_NAME}.spec")
    if os.path.isfile(spec):
        os.remove(spec)

    # 只把我们真正用到的部分打进去。
    # 不能用 --collect-all onnxruntime：那会把 onnxruntime_gpu 的 CUDA 运行库、
    # 以及环境里装着的 paddle / torch / PySide6 等一堆无关包全拖进来，
    # 产物会从几十 MB 涨到 3 GB。
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--noconsole",
        "--paths", BASE,
        # 项目自己的包
        "--hidden-import", "galrec",
        "--hidden-import", "galrec.gui",
        "--hidden-import", "galrec.ui_theme",
        "--hidden-import", "galrec.recorder",
        "--hidden-import", "galrec.detector",
        "--hidden-import", "galrec.advance",
        "--hidden-import", "galrec.obs",
        "--hidden-import", "galrec.backend",
        "--hidden-import", "galrec.hotkey",
        "--hidden-import", "galrec.selector",
        # 第三方
        "--hidden-import", "websocket",
        "--hidden-import", "PIL.ImageGrab",
        "--hidden-import", "numpy",
        "--hidden-import", "rapidocr_onnxruntime",
        "--hidden-import", "onnxruntime",
        "--hidden-import", "cv2",
        # rapidocr 的 onnx 模型是数据文件，必须显式带上
        "--collect-data", "rapidocr_onnxruntime",
        # 明确排除这些：环境里装着、但本程序完全用不到，留着只会让体积爆炸
        "--exclude-module", "paddle",
        "--exclude-module", "paddleocr",
        "--exclude-module", "torch",
        "--exclude-module", "torchvision",
        "--exclude-module", "torchaudio",
        "--exclude-module", "PySide6",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "pyarrow",
        "--exclude-module", "imageio_ffmpeg",
        "--exclude-module", "av",
        "--exclude-module", "onnxruntime_gpu",
        "--exclude-module", "tkinter.test",
        "--exclude-module", "test",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc_data",
    ]

    if onefile:
        args.append("--onefile")
    else:
        args.append("--onedir")

    # 界面用的图片资源（logo 等）：随包带上，主题模块按 galrec/assets 去找
    assets_dir = os.path.join(BASE, "galrec", "assets")
    if os.path.isdir(assets_dir):
        args += ["--add-data", f"{assets_dir}{os.pathsep}galrec/assets"]

    # 图标（有就用）
    ico = os.path.join(BASE, "app.ico")
    if os.path.isfile(ico):
        args += ["--icon", ico]

    args.append(ENTRY)

    print("执行：")
    print(" ", " ".join(args[:8]), "...")
    print()
    r = subprocess.run(args, cwd=BASE)
    if r.returncode != 0:
        print(f"\n!! 打包失败，返回码 {r.returncode}")
        return r.returncode

    # 附带一份说明和默认配置
    if onefile:
        out = os.path.join(BASE, "dist", f"{APP_NAME}.exe")
    else:
        out = os.path.join(BASE, "dist", APP_NAME, f"{APP_NAME}.exe")
        slim_down(os.path.join(BASE, "dist", APP_NAME))
    print()
    if os.path.isfile(out):
        size = os.path.getsize(out) / 1024 / 1024
        print(f"✓ 打包完成：{out}  ({size:.1f} MB)")
        # 把 readme 拷到产物旁边
        readme = os.path.join(BASE, "README.md")
        if os.path.isfile(readme) and not onefile:
            shutil.copy2(readme, os.path.join(os.path.dirname(out), "使用说明.md"))
        print("\n注意：exe 首次运行会在同目录生成 config.yaml 和 recordings/。")
        return 0
    print("!! 没找到产物，请检查上面的输出")
    return 1


if __name__ == "__main__":
    sys.exit(build("--onefile" in sys.argv))
