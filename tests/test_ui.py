import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import csv
from datetime import datetime
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

import numpy as np
from PySide6.QtWidgets import QApplication

from slicing_recorder.models import CameraInfo, Frame
from slicing_recorder.ui import MainWindow


class FakeController:
    def __init__(self):
        self.info = CameraInfo(
            "MV-CT032G-104UC01-PRO", "TEST001", 2048, 1536, "BayerGR8", 104.8,
            exposure_us=5000, gain_db=0, exposure_auto="Off", gain_auto="Off",
            balance_auto="Off", balance_ratios={"Red": 1000, "Green": 1000, "Blue": 1200},
            ranges={"exposure_us": (10, 100000), "gain_db": (0, 24),
                    "balance_red": (1, 4095), "balance_green": (1, 4095), "balance_blue": (1, 4095)},
            modes={"exposure_auto": ["Off", "Continuous"], "gain_auto": ["Off", "Continuous"],
                   "balance_auto": ["Off", "Once", "Continuous"]},
        ).to_dict()
        self.data = dict(state="preview", camera_info=self.info, captured_fps=104.6,
                         disk_free_gb=352.5, message="相机已连接，可以开始录制。", error="")
        self.calls = []
        self.alive = True
        self.frame = None

    def snapshot(self):
        return self.data.copy()

    def preview(self):
        return self.frame

    def request_start(self, *args):
        self.calls.append(("start", args))

    def request_stop(self):
        self.calls.append(("stop",))

    def request_apply(self, settings):
        self.calls.append(("apply", settings))

    def request_restore(self):
        self.calls.append(("restore",))

    def shutdown(self):
        self.calls.append(("shutdown",))

    def is_alive(self):
        return self.alive


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.controller = FakeController()
        self.config = SimpleNamespace(output_dir="/tmp/采集", codec="auto", quality=23, preview_fps=25)
        self.window = MainWindow(self.controller, self.config)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.controller.alive = False
        self.window.close()
        self.app.processEvents()

    def test_start_stop_and_recording_locks(self):
        self.window.name_edit.setText("实验001")
        self.window.start_button.click()
        self.assertEqual(self.controller.calls[-1], ("start", ("/tmp/采集", "实验001", "auto", 23)))
        self.controller.data.update(state="recording", received=200, written=180, queue=20,
                                    duration_s=75, output_file="/tmp/采集/test.mkv", encoder="libx265")
        self.window.refresh()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertFalse(self.window.output_edit.isEnabled())
        self.assertFalse(self.window.codec_combo.isEnabled())
        self.assertEqual(self.window.stats["duration"].text(), "00:01:15")
        self.assertEqual(self.window.stats["written"].text(), "180")
        self.window.stop_button.click()
        self.assertEqual(self.controller.calls[-1], ("stop",))
        self.controller.data["state"] = "saving"
        self.window.refresh()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())
        self.assertIn("正在保存", self.window.status_label.text())

    def test_apply_only_manual_supported_parameters(self):
        self.window._number_controls["exposure_us"].setValue(1200)
        self.window._mode_controls["gain_auto"].setCurrentIndex(1)
        self.window.apply_button.click()
        _, settings = self.controller.calls[-1]
        self.assertEqual(settings["exposure_us"], 1200)
        self.assertEqual(settings["gain_auto"], "Continuous")
        self.assertNotIn("gain_db", settings)
        self.assertEqual(settings["balance_ratios"]["Blue"], 1200)
        self.window.restore_button.click()
        self.assertEqual(self.controller.calls[-1], ("restore",))

    def test_unsupported_parameters_are_not_sent(self):
        info = self.controller.info.copy()
        info["ranges"] = {"exposure_us": (10, 100000)}
        info["modes"] = {}
        self.controller.data["camera_info"] = info
        self.window.refresh()
        self.window.apply_button.click()
        self.assertEqual(self.controller.calls[-1], ("apply", {"exposure_us": 5000.0}))
        self.assertFalse(self.window._mode_controls["gain_auto"].isVisible())

    def test_live_refresh_preserves_unsaved_edits(self):
        self.window._number_controls["exposure_us"].setValue(3333)
        self.window.refresh()
        self.assertEqual(self.window._number_controls["exposure_us"].value(), 3333)

    def test_restore_discards_local_edits_when_readback_is_unchanged(self):
        self.window._number_controls["exposure_us"].setValue(3333)
        self.window.restore_button.click()
        self.window.refresh()
        self.assertEqual(self.window._number_controls["exposure_us"].value(), 5000)

    def test_reconnect_marks_last_frame_and_recovers_specification(self):
        self.controller.data["state"] = "connecting"
        self.window.refresh()
        self.assertIn("保留最后画面", self.window.spec_label.text())
        self.controller.data["state"] = "preview"
        self.window.refresh()
        self.assertIn("设备回读", self.window.spec_label.text())
        self.assertNotIn("保留最后画面", self.window.spec_label.text())

    def test_preview_and_original_scale(self):
        self.controller.frame = Frame(np.full((480, 640, 3), 100, dtype=np.uint8), 1, 100,
                                      datetime.now().astimezone())
        self.window.refresh()
        self.assertFalse(self.window.preview_view.label.pixmap().isNull())
        self.window.fit_check.setChecked(False)
        self.assertEqual(self.window.preview_view.label.pixmap().width(), 640)

    def test_error_and_graceful_close(self):
        self.controller.data.update(state="error", error="磁盘空间不足，录制已停止。")
        self.window.refresh()
        self.assertIn("磁盘空间不足", self.window.message_label.text())
        self.assertFalse(self.window.start_button.isEnabled())
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.controller.calls[-1], ("shutdown",))
        self.assertIn("等待视频收尾", self.window.status_label.text())
        self.controller.alive = False
        self.window.refresh()
        self.assertFalse(self.window.isVisible())


class LiveUIIntegrationTests(unittest.TestCase):
    """Exercise Qt buttons through the real controller, camera and HEVC writer."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_record_save_restore_and_close(self):
        import av
        from slicing_recorder.camera import DemoCamera
        from slicing_recorder.config import AppConfig, ConfigStore
        from slicing_recorder.controller import Controller

        workspace = Path(__file__).resolve().parents[1]
        artifact_root = workspace / "test-artifacts"
        artifact_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ui-e2e-", dir=artifact_root) as temporary:
            root = Path(temporary).resolve()
            self.assertTrue(root.is_relative_to(artifact_root.resolve()))
            output = root / "recordings"
            config = AppConfig(output_dir=str(output), codec="libx265", min_free_gb=0.01)
            store = ConfigStore(root / "config.json")
            controller = Controller(config, store, lambda: DemoCamera(640, 480, 30))
            window = MainWindow(controller, config)
            controller.start()
            window.show()

            def wait_for(predicate, timeout=15):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    self.app.processEvents()
                    window.refresh()
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail(f"UI integration timeout: {controller.snapshot()}")

            def screenshot(name):
                destination = os.environ.get("SLICING_UI_SCREENSHOT_DIR")
                if destination:
                    directory = Path(destination)
                    directory.mkdir(parents=True, exist_ok=True)
                    self.assertTrue(window.grab().save(str(directory / name)))

            try:
                wait_for(lambda: controller.snapshot()["state"] == "preview" and controller.preview() is not None)
                self.assertTrue(window.start_button.isEnabled())
                self.assertIn("DEMO", window.camera_label.text())
                window._number_controls["exposure_us"].setValue(1234)
                window.apply_button.click()
                wait_for(lambda: store.path.exists() and store.load().camera_settings.get("exposure_us") == 1234)
                self.assertEqual(controller.snapshot()["camera_info"]["exposure_us"], 1234)

                window._number_controls["exposure_us"].setValue(4321)
                window.restore_button.click()
                wait_for(lambda: controller.snapshot()["message"] == "已恢复上次保存的参数")
                self.assertEqual(window._number_controls["exposure_us"].value(), 1234)
                screenshot("ui-demo-preview.png")

                window.name_edit.setText("界面集成测试")
                window.start_button.click()
                wait_for(lambda: controller.snapshot()["state"] == "recording")
                self.assertFalse(window.apply_button.isEnabled())
                wait_for(lambda: controller.snapshot()["written"] >= 1)
                ledger_path = output / "recordings.csv"
                with ledger_path.open(encoding="utf-8-sig", newline="") as source:
                    initial = list(csv.DictReader(source))
                self.assertEqual(len(initial), 1)
                self.assertEqual(initial[0]["end_time"], "")
                wait_for(lambda: controller.snapshot()["written"] >= 35)
                screenshot("ui-demo-recording.png")

                window.stop_button.click()
                # A tiny demo may finish drain before the next event-loop poll.
                wait_for(lambda: controller.snapshot()["state"] in {"saving", "preview"})
                if controller.snapshot()["state"] == "saving":
                    self.assertFalse(window.start_button.isEnabled())
                wait_for(lambda: controller.snapshot()["state"] == "preview")
                state = controller.snapshot()
                self.assertEqual(state["error"], "")
                self.assertEqual(state["received"], state["written"])
                self.assertEqual(state["dropped"], 0)
                videos = list(output.glob("*.mkv"))
                self.assertEqual(len(videos), 1)
                with ledger_path.open(encoding="utf-8-sig", newline="") as source:
                    rows = list(csv.DictReader(source))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["video_file"], initial[0]["video_file"])
                self.assertTrue(rows[0]["start_time"])
                self.assertTrue(rows[0]["end_time"])
                self.assertEqual(rows[0]["status"], "normal")
                self.assertEqual(int(rows[0]["file_size_bytes"]), videos[0].stat().st_size)
                with av.open(str(videos[0])) as encoded:
                    self.assertEqual(encoded.streams.video[0].codec_context.name, "hevc")
                    self.assertEqual(sum(1 for _ in encoded.decode(video=0)), state["written"])
                screenshot("ui-demo-saved.png")
                window.close()
                wait_for(lambda: not controller.is_alive())
                self.assertFalse(window.isVisible())
            finally:
                controller.shutdown()
                deadline = time.monotonic() + 15
                while controller.is_alive() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                window.close()
                self.app.processEvents()
                self.assertFalse(controller.is_alive(), "采集线程未完成收尾")


if __name__ == "__main__":
    unittest.main()
