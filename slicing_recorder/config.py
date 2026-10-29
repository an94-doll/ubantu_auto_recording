from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "slicing-recorder" / "config.json"


@dataclass
class AppConfig:
    camera_serial: str = ""
    sdk_path: str = ""
    output_dir: str = str(Path.home() / "Videos" / "slicing")
    codec: str = "auto"
    quality: int = 23
    queue_frames: int = 32
    min_free_gb: float = 2.0
    preview_fps: int = 25
    camera_settings: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.codec not in {"auto", "libx265", "hevc_nvenc", "hevc_qsv", "hevc_vaapi"}:
            raise ValueError("不支持的 H.265 编码器")
        if not isinstance(self.quality, int) or not 0 <= self.quality <= 51:
            raise ValueError("quality 必须为 0–51 的整数")
        if not isinstance(self.queue_frames, int) or not 1 <= self.queue_frames <= 256:
            raise ValueError("queue_frames 必须为 1–256 的整数")
        if not math.isfinite(self.min_free_gb) or self.min_free_gb < 0:
            raise ValueError("min_free_gb 必须为非负有限数")
        if not isinstance(self.preview_fps, int) or not 1 <= self.preview_fps <= 60:
            raise ValueError("preview_fps 必须为 1–60 的整数")
        if not isinstance(self.camera_settings, dict):
            raise ValueError("camera_settings 必须为对象")


class ConfigStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_config_path()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件顶层必须为对象")
        unknown = set(data) - {f.name for f in fields(AppConfig)}
        if unknown:
            raise ValueError(f"未知配置项：{', '.join(sorted(unknown))}")
        config = AppConfig(**data)
        config.validate()
        return config

    def save(self, config: AppConfig) -> None:
        config.validate()
        atomic_text(self.path, json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n")


def atomic_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
