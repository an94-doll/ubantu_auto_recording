from __future__ import annotations

import queue
import re
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import AppConfig, ConfigStore
from .metadata import CsvLedger, iso_time
from .models import Camera, CameraInfo, Frame
from .recording import Recording


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:80].encode("utf-8")[:180].decode("utf-8", errors="ignore") or "采集"


def saved_camera_settings(info: CameraInfo) -> dict:
    result = {}
    for key in ("exposure_us", "gain_db", "exposure_auto", "gain_auto", "balance_auto"):
        value = getattr(info, key)
        mode = {"exposure_us": info.exposure_auto, "gain_db": info.gain_auto}.get(key)
        if mode not in (None, "Off"):
            continue
        if value is not None:
            result[key] = value
    if info.balance_ratios and info.balance_auto in (None, "Off"):
        result["balance_ratios"] = dict(info.balance_ratios)
    return result


class Controller:
    """One camera owner and one encoder owner, with a bounded frame handoff."""

    def __init__(self, config: AppConfig, store: ConfigStore,
                 camera_factory: Callable[[], Camera], encoder_factory: Callable | None = None,
                 persist: bool = True):
        self.config = config
        self.store = store
        self.camera_factory = camera_factory
        if encoder_factory is None:
            from .encoder import Encoder
            encoder_factory = Encoder
        self.encoder_factory = encoder_factory
        self.persist = persist
        self._commands: queue.Queue = queue.Queue()
        self._quit = threading.Event()
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._view = dict(state="connecting", camera_info=None, message="正在连接相机…", error="",
                          captured_fps=0.0, received=0, written=0, dropped=0, queue=0,
                          disk_free_gb=None, duration_s=0.0, output_file="", encoder="")
        self._thread = threading.Thread(target=self._run, name="camera-owner", daemon=True)
        self._camera: Camera | None = None
        self._info: CameraInfo | None = None
        self._recording: Recording | None = None
        self._ledger: CsvLedger | None = None
        self._identity = ""
        self._active = False
        self._reason = "normal"
        self._end_time = ""
        self._next_connect = 0.0
        self._last_frame_at = 0.0

    def _update(self, **values) -> None:
        with self._lock:
            self._view.update(values)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._view)

    def preview(self) -> Frame | None:
        with self._lock:
            return self._latest

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def shutdown(self) -> None:
        self._quit.set()

    def request_start(self, output_dir: str, name: str, codec: str, quality: int) -> None:
        self._commands.put(("start", (output_dir, name, codec, quality)))

    def request_stop(self) -> None:
        self._commands.put(("stop", ()))

    def request_apply(self, settings: dict) -> None:
        self._commands.put(("apply", (settings,)))

    def request_restore(self) -> None:
        self._commands.put(("restore", ()))

    def _save(self, config: AppConfig) -> None:
        if self.persist:
            self.store.save(config)
        self.config = config

    def _connect(self) -> None:
        camera = self.camera_factory()
        try:
            info = camera.open()
            if self.config.camera_settings:
                info = camera.apply(self.config.camera_settings)
            self._save(replace(self.config, camera_serial=info.serial,
                               camera_settings=saved_camera_settings(info)))
            self._camera = camera
            self._info = info
            self._last_frame_at = time.monotonic()
            self._update(state="preview", camera_info=info.to_dict(), message="预览中 · 可以开始录制", error="")
        except Exception:
            camera.close()
            raise

    def _close_camera(self) -> None:
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.close()
            except Exception as exc:
                self._update(error=f"关闭相机失败：{exc}")
        with self._lock:
            self._latest = None
        self._update(camera_info=None, captured_fps=0.0)

    def _disk_free(self, directory: str | Path) -> float:
        return shutil.disk_usage(directory).free / 1_000_000_000

    def _start_recording(self, directory: str, name: str, codec: str, quality: int) -> None:
        if self._camera is None or self._recording is not None:
            raise ValueError("请等待相机就绪或当前录制收尾结束")
        if not directory.strip():
            raise ValueError("请选择保存目录")
        config = replace(self.config, output_dir=str(Path(directory).expanduser().resolve()),
                         codec=codec, quality=quality)
        config.validate()
        path = Path(config.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        free = self._disk_free(path)
        if free < config.min_free_gb:
            raise ValueError(f"磁盘剩余 {free:.2f} GB，低于保留值 {config.min_free_gb:g} GB")
        # Read effective values at the recording boundary, never replace them with UI setpoints.
        info = self._camera.snapshot()
        ledger = CsvLedger(path)
        ledger._rows()  # Refuse incompatible existing data before opening a video.
        filename = f"{datetime.now().astimezone():%Y%m%d_%H%M%S_%f}_{safe_name(name)}.mkv"
        self._save(config)
        self._recording = Recording(path / filename, info, codec, quality,
                                    config.queue_frames, self.encoder_factory)
        self._ledger = ledger
        self._identity = ""
        self._active = False
        self._reason = "normal"
        self._end_time = ""
        self._update(state="saving", message="正在准备 H.265 编码器…", error="", output_file=str(path / filename),
                     received=0, written=0, dropped=0, queue=0, duration_s=0.0, disk_free_gb=free)

    def _stop_recording(self, reason: str = "normal", error: str = "") -> None:
        rec = self._recording
        if rec is None:
            return
        if not rec.stop_event.is_set():
            # This timestamp precedes encoder drain; saving time is not capture time.
            self._end_time = iso_time(datetime.now().astimezone())
            self._reason = reason
            self._active = False
            if error:
                rec.error = error
            rec.stop()
            self._update(state="saving", message="正在保存 · 等待编码队列和文件收尾", error=error or rec.error)

    def _poll_recording(self) -> None:
        rec = self._recording
        if rec is None:
            return
        if rec.ready.is_set() and not self._identity and not rec.stop_event.is_set() and not rec.error:
            assert self._ledger is not None
            try:
                self._identity = self._ledger.begin(rec.path.name, rec.info, rec.codec, rec.quality)
                identity, ledger = self._identity, self._ledger
                rec.on_first_frame = lambda f: ledger.update(
                    identity, start_time=iso_time(f.wall_time), timestamp_source=f.timestamp_source)
                self._active = True
                message = f"录制中 · {rec.codec}"
                if rec.notes:
                    message += f" · 自动选择说明：{rec.notes}"
                self._update(state="recording", message=message, encoder=rec.codec)
            except Exception as exc:
                self._stop_recording("metadata_error", f"建立 CSV 失败：{exc}")
        if rec.error and not rec.stop_event.is_set():
            self._stop_recording(rec.failure_kind or "encoding_error", rec.error)
        first = rec.first
        duration = 0.0
        if first is not None:
            duration = max(0.0, (rec.last.timestamp_ns - first.timestamp_ns) / 1e9) if rec.last else 0.0
        self._update(received=rec.received, written=rec.written, dropped=rec.dropped,
                     queue=rec.queue.qsize(), duration_s=duration)
        if not rec.done.is_set():
            return
        if not self._end_time:
            self._end_time = iso_time(datetime.now().astimezone())
        status = self._reason
        if rec.error and status == "normal":
            status = rec.failure_kind or "encoding_error"
        if not first and status == "normal":
            status = "empty"
        error = rec.error
        try:
            if self._identity:
                assert self._ledger is not None
                self._ledger.update(self._identity,
                                    start_time=iso_time(first.wall_time) if first else "",
                                    timestamp_source=first.timestamp_source if first else "",
                                    end_time=self._end_time,
                                    received_frames=rec.received, encoded_frames=rec.written,
                                    dropped_frames=rec.dropped,
                                    file_size_bytes=rec.path.stat().st_size if rec.path.exists() else 0,
                                    status=status, error=error)
        except Exception as exc:
            error = f"{error}; CSV 收尾失败：{exc}".strip("; ")
        self._recording = None
        self._active = False
        self._ledger = None
        message = f"保存完成：{rec.path.name}" if not error and status == "normal" else f"本次采集结束（{status}）：{error}"
        self._update(state="preview" if self._camera else "error", message=message, error=error)

    def _command(self, name: str, args: tuple) -> None:
        if name == "stop":
            self._stop_recording()
        elif name == "start":
            self._start_recording(*args)
        elif name in {"apply", "restore"}:
            if self._camera is None or self._recording is not None:
                raise ValueError("只能在相机已连接且未录制时修改参数")
            settings = args[0] if name == "apply" else self.config.camera_settings
            try:
                info = self._camera.apply(settings)
            except Exception:
                # A multi-node SDK write can fail after earlier nodes have changed.
                # Show readback, but retain the last successfully saved settings.
                try:
                    info = self._camera.snapshot()
                    self._info = info
                    self._update(camera_info=info.to_dict())
                except Exception:
                    pass
                raise
            self._info = info
            # Update actual readback even if saving to disk subsequently fails.
            self._update(camera_info=info.to_dict())
            if name == "apply":
                self._save(replace(self.config, camera_serial=info.serial,
                                   camera_settings=saved_camera_settings(info)))
            self._update(message="参数已应用并保存" if name == "apply" else "已恢复上次保存的参数", error="")

    def _run(self) -> None:
        fps_started = time.monotonic()
        fps_frames = 0
        last_disk_check = 0.0
        try:
            while True:
                if self._quit.is_set():
                    self._stop_recording("application_exit")
                    if self._recording is None:
                        break
                else:
                    try:
                        name, args = self._commands.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        try:
                            self._command(name, args)
                        except Exception as exc:
                            self._update(error=str(exc), message=str(exc))
                self._poll_recording()
                now = time.monotonic()
                if self._camera is None and now >= self._next_connect and not self._quit.is_set() and self._recording is None:
                    self._update(state="connecting", message="等待相机 · 正在重试连接…")
                    try:
                        self._connect()
                    except Exception as exc:
                        self._next_connect = time.monotonic() + 3.0
                        self._update(state="error", error=str(exc), message=f"等待相机：{exc}")
                if self._camera is not None and not self._quit.is_set():
                    try:
                        frame = self._camera.read(100)
                        if frame is not None:
                            self._last_frame_at = time.monotonic()
                            with self._lock:
                                self._latest = frame
                            fps_frames += 1
                            if self._active and self._recording is not None:
                                if not self._recording.submit(frame):
                                    self._stop_recording(self._recording.failure_kind or "frame_loss", self._recording.error)
                        elif time.monotonic() - self._last_frame_at > 3.0:
                            raise RuntimeError("连续 3 秒未收到相机图像，请检查 USB 连接、供电和触发模式")
                    except Exception as exc:
                        self._stop_recording("camera_disconnected", str(exc))
                        self._close_camera()
                        self._next_connect = time.monotonic() + 3.0
                        self._update(error=str(exc), message=f"相机异常：{exc}")
                        if self._recording is None:
                            self._update(state="error")
                else:
                    time.sleep(0.02)
                if now - fps_started >= 1.0:
                    self._update(captured_fps=fps_frames / (now - fps_started))
                    fps_frames = 0
                    fps_started = now
                if self._recording is not None and now - last_disk_check >= 1.0:
                    last_disk_check = now
                    try:
                        free = self._disk_free(self._recording.path.parent)
                        self._update(disk_free_gb=free)
                        if free < self.config.min_free_gb:
                            self._stop_recording("disk_low", f"磁盘剩余 {free:.2f} GB，低于保留值")
                    except OSError as exc:
                        self._stop_recording("disk_error", str(exc))
        except Exception as exc:
            self._update(state="error", message="采集线程异常", error=str(exc))
            self._stop_recording("internal_error", str(exc))
            if self._recording:
                self._recording.done.wait()
                self._poll_recording()
        finally:
            self._close_camera()
            self._update(state="closed")
