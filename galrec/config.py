"""配置管理。

用一个 YAML 文件保存全部设置，GUI 与命令行共用。读取时对缺字段、多字段、
类型不符都做宽容处理——配置是给人手改的，不该因为少写一行就崩。
"""
from __future__ import annotations

import copy
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

import yaml

DEFAULT_CONFIG_NAME = "config.yaml"


# --------------------------------------------------------------------------- #
# 三段配置
# --------------------------------------------------------------------------- #
@dataclass
class ObsConfig:
    """OBS 连接与录制参数。"""
    host: str = "127.0.0.1"
    port: int = 4455
    password: str = ""
    protocol: str = "auto"            # auto | v5 | v4
    backend: str = "auto"             # auto | websocket | hotkey
    hotkey_start: str = ""            # 热键后端：开始录制，如 ctrl+alt+f9
    hotkey_stop: str = ""             # 热键后端：停止录制，如 ctrl+alt+f10
    auto_launch: bool = True          # 未检测到 OBS 时自动拉起
    exe_path: str = r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"
    record_dir: str = "recordings"    # 相对本程序目录，或绝对路径
    set_record_dir: bool = True       # 连接成功后把 OBS 录制目录改到 record_dir
    scene: str = ""                   # 非空则切到该场景
    stop_delay: float = 2.0           # 判定结束后再录几秒，防止切掉尾巴


@dataclass
class DetectConfig:
    """画面检测参数。"""
    window_title: str = ""            # 窗口标题包含的子串（空=按下面的进程名）
    process_name: str = ""            # 例如 game.exe；两者都空则用界面里选中的窗口
    use_foreground: bool = False      # 直接拿当前前台窗口当游戏窗口

    poll_interval: float = 0.6        # 采样间隔（秒）
    change_threshold: float = 2.0     # 画面变化阈值（越大越迟钝）
    idle_timeout: float = 120.0       # 多久没有新台词就判定剧情结束（秒）
    start_grace: float = 20.0         # 开录后的宽限期，期间不判结束（片头/加载）
    min_record_seconds: float = 15.0  # 短于这个时长的录制会后处理掉
    max_record_seconds: float = 14400  # 安全上限，防止忘关录一整天

    confirm_ticks: int = 2            # 连续几次采样有变化才认为剧情开始
    settle_frames: int = 2            # 画面静止几帧后 OCR（抓「一句话说完」）
    text_lost_timeout: float = 0.0    # 对话框消失多久判结束；0=不启用
    segmented: bool = True            # 停录后剧情再动时另起一段（否则只录一段）
    require_dialogue: bool = True     # 开始录制要求画面里确实有台词文字
    detect_occlusion: bool = True     # 检测游戏窗口是否被别的窗口挡住
    occlusion_timeout: float = 180.0  # 被挡住多久算「剧情中断」（0=不因遮挡停）
    occluded_fallback: bool = True    # 被挡住时改从窗口后台抓图，照常读台词/计时
    minimized_timeout: float = 30.0   # 窗口最小化多久就停录（最小化必然录到黑屏）
    choice_check: bool = True         # 顺便检测选项界面（自动推进要用）
    choice_check_interval: float = 0.0  # 0=只在需要时查

    region_rel: Optional[list] = None  # [x, y, w, h] 相对游戏窗口的比例(0~1)
    auto_region: bool = True           # 未指定区域时自动探测对话框
    auto_region_retry: float = 20.0    # 自动探测失败后多少秒再试（探测要跑 OCR，别太频繁）

    ocr: bool = True                  # 关掉则只靠像素变化判断（省 CPU）
    ocr_min_conf: float = 0.55
    ocr_interval: float = 1.2         # OCR 最小间隔（秒）
    dialogue_min_chars: int = 4       # 少于这么多字认为不是台词


@dataclass
class AdvanceConfig:
    """自动推进剧情（可选，默认关闭）。"""
    enabled: bool = False             # 总开关
    mode: str = "key"                 # key | mouse | both
    key: str = "space"                # key 模式：发哪个键
    click_pos: Optional[list] = None  # mouse 模式：[x, y] 绝对屏幕坐标
    delay_after_text: float = 2.5     # 台词稳定多久后才按（别打断打字机）
    min_interval: float = 1.2         # 两次按键的最小间隔
    stop_on_choice: bool = True       # 检测到选项就停手（强烈建议开启）
    choice_confirm: int = 2           # 连续几次认定选项才停（防误判）
    stop_when_stuck: bool = True      # 连续多次按了没变化就停
    stuck_limit: int = 8              # 按几次没变化算卡住
    auto_resume_text_change: bool = True  # 台词变了就自动恢复（选项过去了）


@dataclass
class RecordConfig:
    """录制行为。"""
    start_mode: str = "on_dialogue"   # on_dialogue | on_window | manual
    stop_mode: str = "on_end"         # on_end | manual
    pause_when_unfocused: bool = False  # 游戏窗口失去焦点时视为暂停
    unfocus_grace: float = 90.0       # 切走后多久算暂停（秒）
    filename_template: str = "{game}_{time}"
    delete_short: bool = True         # 删除短于 min_record_seconds 的录像
    remux: bool = False               # 录制完用 ffmpeg 转封装（mkv->mp4 等）
    ffmpeg: str = ""                  # 留空=自动在 PATH 里找


@dataclass
class Config:
    obs: ObsConfig = field(default_factory=ObsConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    record: RecordConfig = field(default_factory=RecordConfig)
    advance: AdvanceConfig = field(default_factory=AdvanceConfig)
    base_dir: str = field(default="", init=False, repr=False)

    # ---------------- 路径 ---------------- #
    def record_dir(self) -> str:
        d = os.path.expandvars(os.path.expanduser(self.obs.record_dir))
        if not os.path.isabs(d):
            d = os.path.join(self.base_dir or os.getcwd(), d)
        return os.path.normpath(d)

    def resolve_path(self, p: str) -> str:
        if not p:
            return ""
        p = os.path.expandvars(os.path.expanduser(p))
        if not os.path.isabs(p):
            p = os.path.join(self.base_dir or os.getcwd(), p)
        return os.path.normpath(p)

    # ---------------- 序列化 ---------------- #
    def to_dict(self) -> dict:
        d = {
            "obs": asdict(self.obs),
            "detect": asdict(self.detect),
            "record": asdict(self.record),
            "advance": asdict(self.advance),
        }
        return d

    @classmethod
    def from_dict(cls, data: Any) -> "Config":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        for section, obj in (
            ("obs", cfg.obs), ("detect", cfg.detect),
            ("record", cfg.record), ("advance", cfg.advance),
        ):
            raw = data.get(section)
            if isinstance(raw, dict):
                _apply(obj, raw)
        return cfg

    @classmethod
    def load(cls, path: str) -> "Config":
        if not os.path.isfile(path):
            cfg = cls()
            cfg.base_dir = os.path.dirname(os.path.abspath(path))
            return cfg
        with open(path, "r", encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls.from_dict(data)
        cfg.base_dir = os.path.dirname(os.path.abspath(path))
        return cfg

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                self.to_dict(), f, allow_unicode=True,
                sort_keys=False, default_flow_style=False,
            )

    def clone(self) -> "Config":
        c = Config.from_dict(copy.deepcopy(self.to_dict()))
        c.base_dir = self.base_dir
        return c


def _apply(obj: Any, raw: dict) -> None:
    """把字典里的值塞进 dataclass 实例，类型能掰就掰，掰不动就忽略。"""
    known = {f.name: f for f in fields(obj)}
    for k, v in raw.items():
        if k not in known or v is None:
            continue
        target_type = known[k].type
        try:
            if target_type in ("int", int):
                setattr(obj, k, int(v))
            elif target_type in ("float", float):
                setattr(obj, k, float(v))
            elif target_type in ("bool", bool):
                setattr(obj, k, bool(v))
            elif target_type in ("str", str):
                setattr(obj, k, str(v))
            else:
                setattr(obj, k, v)
        except (TypeError, ValueError):
            continue
