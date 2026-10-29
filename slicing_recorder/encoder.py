"""H.265/MKV encoding with capture timestamps, through FFmpeg's PyAV bindings.

An Encoder belongs to exactly one worker thread, including construction and close.
"""
from __future__ import annotations

import io
import math
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import av
import numpy as np

from .models import CameraInfo, Frame

TIME_BASE = Fraction(1, 1_000_000)
CODECS = ("auto", "hevc_nvenc", "hevc_qsv", "hevc_vaapi", "libx265")
AUTO_CODECS = CODECS[1:]


class EncoderError(RuntimeError):
    pass


def _configure(container, info: CameraInfo, codec: str, quality: int):
    rate = Fraction(str(info.fps)).limit_denominator(100_000)
    kwargs = {}
    if codec == "hevc_vaapi":
        # PyAV versions/builds without encoding HWAccel support fail the real
        # probe below; they never appear as working hardware encoders.
        from av.codec.hwaccel import HWAccel

        kwargs["hwaccel"] = HWAccel("vaapi", allow_software_fallback=False)
    stream = container.add_stream(codec, rate=rate, **kwargs)
    stream.width = info.width
    stream.height = info.height
    stream.pix_fmt = "nv12" if codec in ("hevc_qsv", "hevc_vaapi") else "yuv420p"
    ctx = stream.codec_context
    ctx.time_base = TIME_BASE
    stream.time_base = TIME_BASE
    ctx.thread_count = 2
    ctx.gop_size = max(1, round(info.fps * 2))
    ctx.max_b_frames = 0
    if codec == "libx265":
        stream.options = {
            "preset": "ultrafast", "tune": "zerolatency", "crf": str(quality),
            "x265-params": "pools=2:frame-threads=1:log-level=error",
        }
    elif codec == "hevc_nvenc":
        stream.options = {"preset": "p1", "rc": "vbr", "cq": str(quality), "b": "0"}
    elif codec == "hevc_qsv":
        stream.options = {"preset": "veryfast", "global_quality": str(quality)}
    else:
        stream.options = {"rc_mode": "CQP", "qp": str(quality)}
    return stream


def _probe(info: CameraInfo, codec: str, quality: int) -> None:
    """Open, encode, mux and flush at the requested dimensions, in memory."""
    sink = io.BytesIO()
    container = av.open(sink, mode="w", format="matroska")
    try:
        stream = _configure(container, info, codec, quality)
        dummy = av.VideoFrame.from_ndarray(
            np.zeros((info.height, info.width, 3), dtype=np.uint8), format="rgb24"
        )
        dummy.pts = 0
        dummy.time_base = TIME_BASE
        for packet in stream.encode(dummy):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()


class Encoder:
    """A single non-overwriting H.265/MKV file. No frame dropping or CFR rewrite."""

    def __init__(
        self, path: Path, info: CameraInfo, codec: str = "auto", quality: int = 23
    ):
        if codec not in CODECS:
            raise EncoderError(f"不支持的编码器：{codec}；仅支持 H.265")
        if info.width < 16 or info.height < 16 or info.width % 2 or info.height % 2:
            raise EncoderError("H.265 YUV420 要求宽高为不小于 16 的偶数；不会自动缩放")
        if not math.isfinite(info.fps) or info.fps <= 0:
            raise EncoderError("相机实际帧率必须为正数")
        if not isinstance(quality, int) or not 0 <= quality <= 51:
            raise EncoderError("质量参数必须为 0–51 的整数")
        self.path = Path(path)
        self.info = info
        self.quality = quality
        self.codec = ""
        self.fallback_reasons: list[str] = []
        self._container = None
        self._stream = None
        self._file: BinaryIO | None = None
        self._closed = False
        self._origin_ns: int | None = None
        self._last_pts = -1
        self._timestamp_source: str | None = None
        self.frames_written = 0
        for candidate in AUTO_CODECS if codec == "auto" else (codec,):
            try:
                _probe(info, candidate, quality)
            except Exception as exc:
                self.fallback_reasons.append(f"{candidate}: {exc}")
            else:
                self.codec = candidate
                break
        if not self.codec:
            raise EncoderError("没有可用的 H.265 编码器：" + "; ".join(self.fallback_reasons))
        try:
            self._file = self.path.open("xb")
            self._container = av.open(self._file, mode="w", format="matroska")
            self._stream = _configure(self._container, info, self.codec, quality)
            # Fail before advertising recording readiness, not on the first frame.
            self._container.start_encoding()
        except Exception as exc:
            self._dispose()
            raise EncoderError(f"无法创建录制文件 {self.path}: {exc}") from exc

    @property
    def output_pixel_format(self) -> str:
        return "yuv420p"  # NV12 is the same 8-bit 4:2:0 sampling, semi-planar.

    def write(self, frame: Frame) -> None:
        if self._closed:
            raise EncoderError("编码器已经关闭")
        if frame.rgb.dtype != np.uint8 or frame.rgb.shape != (self.info.height, self.info.width, 3):
            raise EncoderError("帧尺寸或格式变化；必须为相机原分辨率 RGB24")
        if not frame.rgb.flags.c_contiguous:
            raise EncoderError("RGB 帧内存必须连续")
        if self._timestamp_source is not None and frame.timestamp_source != self._timestamp_source:
            raise EncoderError("采集时钟源在录制期间发生变化")
        origin_ns = frame.timestamp_ns if self._origin_ns is None else self._origin_ns
        pts = (frame.timestamp_ns - origin_ns) // 1000
        if pts <= self._last_pts:
            raise EncoderError("采集时间戳未严格递增（编码时基为微秒）")
        try:
            video_frame = av.VideoFrame.from_ndarray(frame.rgb, format="rgb24")
            video_frame.pts = pts
            video_frame.time_base = TIME_BASE
            for packet in self._stream.encode(video_frame):
                self._container.mux(packet)
        except Exception as exc:
            raise EncoderError(f"H.265 编码或写盘失败：{exc}") from exc
        self._origin_ns = origin_ns
        self._timestamp_source = frame.timestamp_source
        self._last_pts = pts
        self.frames_written += 1

    def _dispose(self) -> None:
        container, file = self._container, self._file
        self._container = None
        self._stream = None
        self._file = None
        self._closed = True
        try:
            if container is not None:
                container.close()
        finally:
            if file is not None:
                file.close()

    def close(self) -> None:
        if self._closed:
            return
        error: Exception | None = None
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        except Exception as exc:
            error = exc
        try:
            self._dispose()
        except Exception as exc:
            if error is None:
                error = exc
        if error is not None:
            raise EncoderError(f"视频收尾失败：{error}") from error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
