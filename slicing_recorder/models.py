from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

import numpy as np


@dataclass
class CameraInfo:
    model: str
    serial: str
    width: int
    height: int
    pixel_format: str
    fps: float
    exposure_us: float | None = None
    gain_db: float | None = None
    exposure_auto: str | None = None
    gain_auto: str | None = None
    balance_auto: str | None = None
    balance_ratios: dict[str, float] = field(default_factory=dict)
    # Only supported, readable/writable controls are advertised.
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    modes: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Frame:
    # Owned contiguous RGB24 memory, independent of the SDK buffer lifetime.
    rgb: np.ndarray
    sequence: int
    timestamp_ns: int
    wall_time: datetime
    timestamp_source: str = "host_monotonic"


class CameraError(RuntimeError):
    pass


class Camera(Protocol):
    def open(self) -> CameraInfo: ...
    def read(self, timeout_ms: int = 250) -> Frame | None: ...
    def apply(self, settings: dict[str, Any]) -> CameraInfo: ...
    def snapshot(self) -> CameraInfo: ...
    def close(self) -> None: ...
