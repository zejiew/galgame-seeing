"""galrec —— Galgame 剧情自动录制（OBS）工具包。

模块划分
--------
config     配置读写（config.yaml）
windows    Win32 窗口枚举 / 定位 / DPI
capture    屏幕区域抓图 + 轻量像素比对
ocr        本地 OCR（rapidocr onnxruntime），对话框区域自动探测
detector   剧情推进状态机（核心：判断「剧情在走」还是「剧情结束了」）
obs        obs-websocket 客户端（协议 v5 / v4 自动协商）
recorder   总调度：检测 + 控制 OBS 录制 + 文件命名
gui        tkinter 图形界面
"""

__version__ = "1.0.0"
