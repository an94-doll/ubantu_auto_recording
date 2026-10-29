from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Callable

from .models import CameraInfo, Frame


class Recording:
    """Encoder thread owns the encoder; capture never waits on an overfull queue."""

    def __init__(self, path: Path, info: CameraInfo, codec: str, quality: int,
                 capacity: int, encoder_factory: Callable):
        self.path = path
        self.info = info
        self.codec_request = codec
        self.quality = quality
        self.codec = ""
        self.notes = ""
        self.queue: queue.Queue[Frame] = queue.Queue(maxsize=capacity)
        self.encoder_factory = encoder_factory
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.done = threading.Event()
        self.error = ""
        self.failure_kind = ""
        self.received = 0
        self.written = 0
        self._capture_dropped = 0
        self._writer_dropped = 0
        self.first: Frame | None = None
        self.last: Frame | None = None
        self.last_sequence: int | None = None
        self.on_first_frame: Callable[[Frame], None] | None = None
        self.thread = threading.Thread(target=self._run, name="h265-writer", daemon=True)
        self.thread.start()

    def submit(self, frame: Frame) -> bool:
        self.received += 1
        if self.stop_event.is_set() or self.done.is_set() or self.error:
            self._capture_dropped += 1
            return False
        if self.last_sequence is not None:
            delta = (frame.sequence - self.last_sequence) & 0xFFFFFFFF
            if delta == 0 or delta > 0x80000000:
                self._capture_dropped += 1
                self.error = "相机帧号重复或倒退"
                self.failure_kind = "frame_loss"
                return False
            if delta > 1:
                self._capture_dropped += delta
                self.error = f"检测到相机丢帧：缺少 {delta - 1} 帧"
                self.failure_kind = "frame_loss"
                return False
        self.last_sequence = frame.sequence
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            self._capture_dropped += 1
            self.error = "编码队列已满；本次采集结束，请检查编码吞吐或磁盘速度"
            self.failure_kind = "frame_loss"
            return False
        if self.first is None:
            self.first = frame
        self.last = frame
        return True

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def dropped(self) -> int:
        return self._capture_dropped + self._writer_dropped

    def _run(self) -> None:
        encoder = None
        inflight = False
        try:
            encoder = self.encoder_factory(self.path, self.info, self.codec_request, self.quality)
            self.codec = encoder.codec
            self.notes = "; ".join(getattr(encoder, "fallback_reasons", []))
            self.ready.set()
            started = False
            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    frame = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                inflight = True
                if not started:
                    if self.on_first_frame is not None:
                        try:
                            self.on_first_frame(frame)
                        except Exception:
                            self.failure_kind = "metadata_error"
                            raise
                    started = True
                encoder.write(frame)
                self.written += 1
                inflight = False
        except Exception as exc:
            if not self.failure_kind:
                self.failure_kind = "encoding_error"
            self.error = str(exc)
            if inflight:
                self._writer_dropped += 1
        finally:
            if encoder is not None:
                try:
                    encoder.close()
                except Exception as exc:
                    if not self.failure_kind:
                        self.failure_kind = "encoding_error"
                    self.error = f"{self.error}; 收尾失败：{exc}".strip("; ")
            # Release queued full-resolution images even after a codec failure.
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                    self._writer_dropped += 1
                except queue.Empty:
                    break
            self.ready.set()
            self.done.set()
