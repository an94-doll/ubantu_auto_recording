from __future__ import annotations

import csv
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import numpy as np

from slicing_recorder.camera import DemoCamera
from slicing_recorder.config import AppConfig, ConfigStore
from slicing_recorder.controller import Controller, safe_name, saved_camera_settings
from slicing_recorder.metadata import CsvLedger, FIELDS
from slicing_recorder.models import CameraInfo, Frame
from slicing_recorder.recording import Recording
from scripts.install_autostart import desktop_entry, exec_quote


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.01)
    raise AssertionError("状态等待超时")


def frame(number: int) -> Frame:
    return Frame(np.zeros((48, 64, 3), dtype=np.uint8), number, number * 1_000_000,
                 datetime.now(timezone.utc))


class FakeEncoder:
    def __init__(self, path, info, codec, quality):
        self.codec = "libx265"
        self.fallback_reasons = []
        self.file = Path(path).open("xb")

    def write(self, image):
        self.file.write(b"frame")

    def close(self):
        self.file.close()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = ConfigStore(self.directory / "config.json")
        self.config = AppConfig(output_dir=str(self.directory), min_free_gb=0)
        self.controller = None

    def tearDown(self):
        if self.controller:
            self.controller.shutdown()
            self.controller._thread.join(timeout=5)
            self.assertFalse(self.controller.is_alive())
        self.temp.cleanup()

    def start_controller(self, camera_factory=None, encoder_factory=FakeEncoder):
        self.controller = Controller(self.config, self.store,
                                     camera_factory or (lambda: DemoCamera(64, 48, 100)), encoder_factory)
        self.controller.start()
        wait_for(lambda: self.controller.snapshot()["state"] == "preview")
        return self.controller

    def start_recording(self, controller):
        controller.request_start(str(self.directory), "实验/001", "libx265", 23)
        wait_for(lambda: controller.snapshot()["state"] == "recording")
        wait_for(lambda: controller.snapshot()["written"] >= 3)

    def rows(self):
        with (self.directory / "recordings.csv").open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))

    def test_config_roundtrip_and_unsupported_key(self):
        self.store.save(self.config)
        self.assertEqual(self.store.load(), self.config)
        self.store.path.write_text('{"surprise":1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.load()

    def test_recording_one_row_per_session_and_drain(self):
        ctl = self.start_controller()
        self.start_recording(ctl)
        self.assertEqual(self.rows()[0]["status"], "recording")
        ctl.request_stop()
        wait_for(lambda: ctl.snapshot()["state"] == "preview")
        row = self.rows()[0]
        self.assertEqual(row["status"], "normal")
        self.assertEqual(row["received_frames"], row["encoded_frames"])
        self.assertEqual(row["dropped_frames"], "0")
        self.assertTrue(row["start_time"])
        self.assertTrue(row["end_time"])
        self.assertEqual(int(row["file_size_bytes"]), (self.directory / row["video_file"]).stat().st_size)
        self.start_recording(ctl)
        ctl.request_stop()
        wait_for(lambda: ctl.snapshot()["state"] == "preview")
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(list(self.directory.glob("*.mkv"))), 2)
        self.assertTrue((self.directory / "recordings.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_capture_end_excludes_encoder_close_delay(self):
        close_started = threading.Event()
        release = threading.Event()

        class SlowClose(FakeEncoder):
            def close(self):
                close_started.set()
                release.wait(3)
                super().close()

        ctl = self.start_controller(encoder_factory=SlowClose)
        try:
            self.start_recording(ctl)
            ctl.request_stop()
            self.assertTrue(close_started.wait(2))
            self.assertEqual(ctl.snapshot()["state"], "saving")
            time.sleep(.1)
            before_release = datetime.now().astimezone()
        finally:
            release.set()
        wait_for(lambda: ctl.snapshot()["state"] == "preview")
        end = datetime.fromisoformat(self.rows()[0]["end_time"])
        self.assertLess(end, before_release)

    def test_parameter_readback_persist_and_restore(self):
        ctl = self.start_controller()
        ctl.request_apply({"exposure_us": 1234})
        wait_for(lambda: self.store.load().camera_settings.get("exposure_us") == 1234)
        self.assertEqual(ctl.snapshot()["camera_info"]["exposure_us"], 1234)
        self.assertEqual(self.store.load().camera_serial, "DEMO-001")
        ctl.request_apply({"exposure_auto": "Continuous"})
        wait_for(lambda: self.store.load().camera_settings.get("exposure_auto") == "Continuous")
        self.assertNotIn("exposure_us", self.store.load().camera_settings)
        ctl.request_restore()
        wait_for(lambda: ctl.snapshot()["message"] == "已恢复上次保存的参数")

    def test_parameters_locked_during_recording(self):
        ctl = self.start_controller()
        self.start_recording(ctl)
        before = self.store.load().camera_settings.copy()
        ctl.request_apply({"exposure_us": 50})
        wait_for(lambda: "只能" in ctl.snapshot()["error"])
        self.assertEqual(before, self.store.load().camera_settings)

    def test_low_disk_prevents_start(self):
        ctl = self.start_controller()
        ctl.config = replace(ctl.config, min_free_gb=1e12)
        ctl.request_start(str(self.directory), "test", "libx265", 23)
        wait_for(lambda: "磁盘" in ctl.snapshot()["error"])
        self.assertFalse((self.directory / "recordings.csv").exists())

    def test_low_disk_during_capture_stops_with_status(self):
        ctl = self.start_controller()
        self.start_recording(ctl)
        ctl.config = replace(ctl.config, min_free_gb=1e12)
        wait_for(lambda: ctl.snapshot()["state"] == "preview")
        self.assertEqual(self.rows()[0]["status"], "disk_low")

    def test_disconnection_stops_and_records_failure(self):
        disconnect = threading.Event()

        class DisconnectingCamera(DemoCamera):
            def read(self, timeout_ms=250):
                if disconnect.is_set():
                    raise RuntimeError("USB disconnected")
                return super().read(timeout_ms)

        ctl = self.start_controller(lambda: DisconnectingCamera(64, 48, 100))
        self.start_recording(ctl)
        disconnect.set()
        wait_for(lambda: (self.rows()[0]["status"] == "camera_disconnected"))
        self.assertIn("USB disconnected", self.rows()[0]["error"])
        disconnect.clear()
        ctl._next_connect = 0
        wait_for(lambda: ctl.snapshot()["state"] == "preview")
        self.assertIsNone(ctl._recording)
        self.assertEqual(ctl.snapshot()["error"], "")
        self.assertEqual(len(self.rows()), 1)

    def test_encoder_failure_updates_csv_instead_of_success(self):
        class FailingEncoder(FakeEncoder):
            def write(self, image):
                raise RuntimeError("injected disk write failure")

        ctl = self.start_controller(encoder_factory=FailingEncoder)
        ctl.request_start(str(self.directory), "test", "libx265", 23)
        wait_for(lambda: "injected disk write failure" in ctl.snapshot()["error"])
        wait_for(lambda: ctl.snapshot()["state"] == "preview")
        row = self.rows()[0]
        self.assertEqual(row["status"], "encoding_error")
        self.assertGreater(int(row["dropped_frames"]), 0)

    def test_incompatible_csv_not_overwritten(self):
        (self.directory / "recordings.csv").write_text("other,data\n1,2\n", encoding="utf-8")
        ctl = self.start_controller()
        ctl.request_start(str(self.directory), "test", "libx265", 23)
        wait_for(lambda: "列结构不匹配" in ctl.snapshot()["error"])
        self.assertEqual((self.directory / "recordings.csv").read_text(), "other,data\n1,2\n")
        self.assertFalse(list(self.directory.glob("*.mkv")))

    def test_csv_incomplete_row_and_readback_semantics(self):
        info = CameraInfo("model", "serial", 64, 48, "BayerGR8", 50)
        info.ranges["fps"] = (1, 100)
        ledger = CsvLedger(self.directory)
        identity = ledger.begin("file.mkv", info, "libx265", 23)
        row = ledger._rows()[0]
        self.assertEqual(list(row), FIELDS)
        self.assertEqual(row["target_fps"], "100")
        self.assertEqual(row["camera_fps_readback"], "50")
        self.assertEqual(row["end_time"], "")
        self.assertEqual(row["file_size_bytes"], "")
        ledger.update(identity, status="normal")
        self.assertEqual(len(ledger._rows()), 1)


class RecordingTests(unittest.TestCase):
    def test_encoder_failure_accounts_all_discarded_frames(self):
        entered, release = threading.Event(), threading.Event()

        class Failing(FakeEncoder):
            def write(self, image):
                entered.set()
                release.wait(2)
                raise RuntimeError("write failed")

        with tempfile.TemporaryDirectory() as directory:
            rec = Recording(Path(directory) / "a.mkv", CameraInfo("m", "s", 64, 48, "RGB8", 100),
                            "libx265", 23, 3, Failing)
            self.assertTrue(rec.ready.wait(2))
            self.assertTrue(rec.submit(frame(1)))
            self.assertTrue(entered.wait(2))
            self.assertTrue(rec.submit(frame(2)))
            self.assertTrue(rec.submit(frame(3)))
            release.set()
            self.assertTrue(rec.done.wait(2))
            self.assertEqual(rec.received, 3)
            self.assertEqual(rec.written, 0)
            self.assertEqual(rec.dropped, 3)
            self.assertFalse(rec.submit(frame(4)))
            self.assertEqual(rec.dropped, 4)

    def test_queue_overflow_is_explicit_and_bounded(self):
        entered, release = threading.Event(), threading.Event()

        class Slow(FakeEncoder):
            def write(self, image):
                entered.set()
                release.wait(2)
                super().write(image)

        with tempfile.TemporaryDirectory() as directory:
            rec = Recording(Path(directory) / "a.mkv", CameraInfo("m", "s", 64, 48, "RGB8", 100),
                            "libx265", 23, 1, Slow)
            self.assertTrue(rec.ready.wait(2))
            self.assertTrue(rec.submit(frame(1)))
            self.assertTrue(entered.wait(2))
            self.assertTrue(rec.submit(frame(2)))
            self.assertFalse(rec.submit(frame(3)))
            self.assertIn("队列已满", rec.error)
            self.assertEqual(rec.queue.qsize(), 1)
            rec.stop()
            release.set()
            self.assertTrue(rec.done.wait(2))
            self.assertEqual(rec.written, 2)
            self.assertEqual(rec.dropped, 1)

    def test_camera_gap_stops_and_counts_missing_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = Recording(Path(directory) / "a.mkv", CameraInfo("m", "s", 64, 48, "RGB8", 100),
                            "libx265", 23, 4, FakeEncoder)
            self.assertTrue(rec.ready.wait(2))
            self.assertTrue(rec.submit(frame(1)))
            self.assertFalse(rec.submit(frame(4)))
            self.assertEqual(rec.dropped, 3)  # Missing 2,3; rejected current 4.
            rec.stop()
            self.assertTrue(rec.done.wait(2))


class LauncherTests(unittest.TestCase):
    def test_filename_stays_inside_directory_and_within_utf8_limit(self):
        self.assertNotIn("/", safe_name("../trial/a"))
        self.assertLessEqual(len(safe_name("实验" * 100).encode("utf-8")), 180)

    def test_autostart_does_not_use_shell_command_string(self):
        text = desktop_entry(PurePosixPath("/home/one two/采集"), PurePosixPath("/opt/MVS"), PurePosixPath("/home/u/a%b.json"))
        self.assertIn('"/bin/bash" "/home/one two/采集/scripts/run.sh"', text)
        self.assertIn("a%%b.json", text)
        self.assertNotIn("sh -c", text)
        self.assertNotIn("--demo", text)
        with self.assertRaises(ValueError):
            exec_quote("/home/bad\npath")


if __name__ == "__main__":
    unittest.main()
