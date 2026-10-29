"""One durable CSV row per recording; incomplete rows survive application crashes."""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import atomic_text
from .models import CameraInfo

FIELDS = [
    "recording_id", "video_file", "requested_at", "start_time", "end_time",
    "camera_model", "camera_serial", "width", "height", "pixel_format",
    "target_fps", "camera_fps_readback", "exposure_us", "exposure_auto", "gain_db", "gain_auto",
    "white_balance", "encoder", "quality", "output_pixel_format", "timestamp_source",
    "received_frames", "encoded_frames", "dropped_frames", "file_size_bytes",
    "status", "error",
]


def iso_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


class CsvLedger:
    def __init__(self, directory: Path):
        self.path = directory / "recordings.csv"

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != FIELDS:
                raise ValueError(f"CSV 列结构不匹配，未修改原文件：{self.path}")
            return list(reader)

    def _write(self, rows: list[dict[str, Any]]) -> None:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        atomic_text(self.path, buffer.getvalue(), encoding="utf-8-sig")

    def begin(self, filename: str, info: CameraInfo, encoder: str, quality: int) -> str:
        rows = self._rows()
        identity = uuid.uuid4().hex
        row = dict.fromkeys(FIELDS, "")
        row.update(
            recording_id=identity, video_file=filename,
            requested_at=iso_time(datetime.now().astimezone()),
            camera_model=info.model, camera_serial=info.serial,
            width=info.width, height=info.height, pixel_format=info.pixel_format,
            target_fps=info.ranges.get("fps", (info.fps, info.fps))[1],
            camera_fps_readback=info.fps, exposure_us=info.exposure_us,
            exposure_auto=info.exposure_auto, gain_db=info.gain_db, gain_auto=info.gain_auto,
            white_balance=json.dumps({"mode": info.balance_auto, "ratios": info.balance_ratios}, ensure_ascii=False),
            encoder=encoder, quality=quality, output_pixel_format="yuv420p", status="recording",
        )
        rows.append(row)
        self._write(rows)
        return identity

    def update(self, identity: str, **values: Any) -> None:
        rows = self._rows()
        for row in rows:
            if row["recording_id"] == identity:
                row.update(values)
                self._write(rows)
                return
        raise ValueError(f"找不到采集记录：{identity}")
