import ctypes as ct
from datetime import datetime
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np

from slicing_recorder.camera import DemoCamera, HikCamera, _load_sdk
from slicing_recorder.models import CameraError


class UsbInfo(ct.Structure):
    _fields_ = [("chSerialNumber", ct.c_char * 32), ("chModelName", ct.c_char * 64)]


class Special(ct.Structure):
    _fields_ = [("stUsb3VInfo", UsbInfo)]


class Device(ct.Structure):
    _fields_ = [("SpecialInfo", Special)]


class FakeCamera:
    devices = []
    subsampling = False

    @staticmethod
    def MV_CC_EnumDevices(kind, output):
        output.nDeviceNum = len(FakeCamera.devices)
        output.pDeviceInfo = [ct.pointer(device) for device in FakeCamera.devices]
        return 0

    def __init__(self):
        self.ints = {"Width": [2, 2, 8, 2], "Height": [2, 2, 6, 2],
                     "OffsetX": [2, 0, 6, 1], "OffsetY": [2, 0, 4, 1],
                     "BalanceRatio": [1024, 1, 4095, 1]}
        self.floats = {"AcquisitionFrameRate": [30.0, 1.0, 105.0], "ExposureTime": [5000.0, 20.0, 1000000.0],
                       "Gain": [0.0, 0.0, 24.0]}
        self.enums = {"TriggerMode": [1, [0, 1]], "AcquisitionMode": [0, [0, 1, 2]],
                      "PixelFormat": [10, [10, 11]], "ExposureAuto": [0, [0, 1, 2]],
                      "GainAuto": [0, [0, 2]], "BalanceWhiteAuto": [0, [0, 1, 2]],
                      "BalanceRatioSelector": [0, [0, 1, 2]]}
        self.writes = []
        self.events = []
        self.native_grabbing = False
        self.free_count = 0
        self.cleanup = []
        self.convert_ret = self.get_ret = self.set_ret = self.free_ret = 0
        self.start_ret = self.stop_ret = 0
        self.bad_length = False
        self.connected = True
        self._raw = (ct.c_ubyte * 48)()
        if self.subsampling:
            for node in ("BinningHorizontal", "BinningVertical", "DecimationHorizontal", "DecimationVertical"):
                self.ints[node] = [2, 1, 4, 1]

    def MV_CC_CreateHandle(self, device): return 0
    def MV_CC_OpenDevice(self, access, key): return 0
    def MV_CC_StartGrabbing(self):
        self.events.append("start")
        if self.start_ret == 0:
            self.native_grabbing = True
        return self.start_ret

    def MV_CC_StopGrabbing(self):
        self.cleanup.append("stop")
        self.events.append("stop")
        if self.stop_ret == 0:
            self.native_grabbing = False
        return self.stop_ret

    def MV_CC_CloseDevice(self):
        self.cleanup.append("close")
        return 0

    def MV_CC_DestroyHandle(self):
        self.cleanup.append("destroy")
        return 0

    def MV_CC_GetIntValueEx(self, node, value):
        if node not in self.ints:
            return -1
        value.nCurValue, value.nMin, value.nMax, value.nInc = self.ints[node]
        if self.subsampling and node in ("Width", "Height"):
            axis = "Horizontal" if node == "Width" else "Vertical"
            factor = self.ints["Binning" + axis][0] * self.ints["Decimation" + axis][0]
            value.nMax //= factor
        return 0

    def MV_CC_SetIntValueEx(self, node, value):
        self.writes.append((node, value))
        if self.set_ret:
            return self.set_ret
        self.ints[node][0] = value
        return 0

    def MV_CC_GetFloatValue(self, node, value):
        if node not in self.floats:
            return -1
        value.fCurValue, value.fMin, value.fMax = self.floats[node]
        return 0

    def MV_CC_SetFloatValue(self, node, value):
        self.writes.append((node, value))
        if self.set_ret:
            return self.set_ret
        self.floats[node][0] = round(value, 1)
        return 0

    def MV_CC_GetEnumValue(self, node, value):
        if node not in self.enums:
            return -1
        value.nCurValue, value.nSupportedValue = self.enums[node]
        value.nSupportedNum = len(value.nSupportedValue)
        return 0

    def MV_CC_SetEnumValue(self, node, value):
        self.writes.append((node, value))
        self.events.append((node, value))
        if node == "BalanceRatioSelector" and self.native_grabbing:
            return 0x80000106  # This camera locks selector writes while streaming.
        if self.set_ret:
            return self.set_ret
        self.enums[node][0] = value
        return 0

    def MV_CC_GetBoolValue(self, node, value):
        value.value = True
        return 0

    def MV_CC_SetBoolValue(self, node, value): return self.set_ret
    def MV_CC_IsDeviceConnected(self): return self.connected

    def MV_CC_GetImageBuffer(self, out, timeout):
        if self.get_ret:
            return self.get_ret
        out.pBufAddr = ct.cast(self._raw, ct.POINTER(ct.c_ubyte))
        out.stFrameInfo = NS(nWidth=8, nHeight=6, enPixelType=10, nFrameLen=48,
                             nDevTimeStampHigh=0, nDevTimeStampLow=5000, nFrameNum=42,
                             nLostPacket=0)
        return 0

    def MV_CC_ConvertPixelType(self, parameters):
        if self.convert_ret:
            return self.convert_ret
        ct.memset(parameters.pDstBuffer, 17, parameters.nDstBufferSize)
        parameters.nDstLen = parameters.nDstBufferSize - int(self.bad_length)
        return 0

    def MV_CC_FreeImageBuffer(self, out):
        self.free_count += 1
        ct.memset(self._raw, 99, len(self._raw))
        return self.free_ret


def fake_sdk():
    return NS(MvCamera=FakeCamera, MV_USB_DEVICE=4, MV_ACCESS_Exclusive=1,
              MV_CC_DEVICE_INFO_LIST=NS, MV_CC_DEVICE_INFO=Device,
              MVCC_INTVALUE_EX=NS, MVCC_FLOATVALUE=NS, MVCC_ENUMVALUE=NS,
              MV_FRAME_OUT=NS, MV_CC_PIXEL_CONVERT_PARAM=NS,
              PixelType_Gvsp_BayerGR8=10, PixelType_Gvsp_RGB8_Packed=11,
              MV_E_NODATA=0x80000007)


class HikCameraTests(unittest.TestCase):
    def setUp(self):
        device = Device()
        device.SpecialInfo.stUsb3VInfo.chSerialNumber = b"USB123"
        device.SpecialInfo.stUsb3VInfo.chModelName = b"MV-CT032G-104UC01-PRO"
        FakeCamera.devices = [device]
        FakeCamera.subsampling = False
        self.loader = patch("slicing_recorder.camera._load_sdk", return_value=fake_sdk())
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.camera = HikCamera()
        self.addCleanup(self.camera.close)

    def test_full_resolution_max_rate_and_readback(self):
        info = self.camera.open()
        self.assertEqual((info.width, info.height, info.fps), (8, 6, 105.0))
        self.assertEqual(info.pixel_format, "BayerGR8")
        self.assertEqual(info.model, "MV-CT032G-104UC01-PRO")
        writes = self.camera._cam.writes
        self.assertLess(writes.index(("OffsetX", 0)), writes.index(("Width", 8)))
        info = self.camera.apply({"exposure_us": 1234.56})
        self.assertEqual(info.exposure_us, 1234.6)
        self.assertEqual(info.modes["gain_auto"], ["Off", "Continuous"])

    def test_owned_rgb_memory_and_buffer_release(self):
        self.camera.open()
        frame = self.camera.read()
        self.assertEqual(frame.rgb.shape, (6, 8, 3))
        self.assertTrue(frame.rgb.flags.c_contiguous)
        self.assertTrue(np.all(frame.rgb == 17))
        self.assertEqual(frame.sequence, 42)
        self.assertIsNotNone(frame.wall_time.tzinfo)
        self.assertEqual(frame.timestamp_source, "host_monotonic")
        self.assertEqual(self.camera._cam.free_count, 1)

    def test_buffer_released_after_conversion_failure(self):
        self.camera.open()
        self.camera._cam.convert_ret = 0x80000001
        with self.assertRaisesRegex(CameraError, "80000001"):
            self.camera.read()
        self.assertEqual(self.camera._cam.free_count, 1)

    def test_bad_conversion_length_is_error(self):
        self.camera.open()
        self.camera._cam.bad_length = True
        with self.assertRaisesRegex(CameraError, "长度"):
            self.camera.read()
        self.assertEqual(self.camera._cam.free_count, 1)

    def test_no_data_timeout_and_disconnection(self):
        self.camera.open()
        self.camera._cam.get_ret = 0x80000007
        self.assertIsNone(self.camera.read())
        self.assertEqual(self.camera._cam.free_count, 0)
        self.camera._cam.connected = False
        with self.assertRaisesRegex(CameraError, "断开"):
            self.camera.read()

    def test_device_ticks_only_when_frequency_is_known(self):
        self.camera.open()
        self.camera._tick_hz = 1_000_000
        frame = self.camera.read()
        self.assertEqual((frame.timestamp_ns, frame.timestamp_source), (5_000_000, "camera_device"))

    def test_manual_parameters_reject_automatic_mode(self):
        self.camera.open()
        with self.assertRaisesRegex(CameraError, "关闭自动"):
            self.camera.apply({"exposure_auto": "Continuous", "exposure_us": 3000.0})

    def test_parameter_failure_has_return_code(self):
        self.camera.open()
        self.camera._cam.set_ret = 0x80000106
        with self.assertRaisesRegex(CameraError, "80000106"):
            self.camera.apply({"exposure_us": 3000.0})
        self.assertEqual(self.camera._cam.floats["ExposureTime"][0], 5000.0)

    def test_cleanup_is_idempotent(self):
        self.camera.open()
        native = self.camera._cam
        self.camera.close()
        self.camera.close()
        self.assertEqual(native.cleanup, ["stop", "close", "destroy"])

    def test_open_snapshot_does_not_pause_before_initial_start(self):
        self.camera.open()
        native = self.camera._cam
        self.assertEqual(native.events.count("start"), 1)
        self.assertEqual(native.events.count("stop"), 0)

    def test_preview_snapshot_pauses_selector_access_and_resumes(self):
        self.camera.open()
        native = self.camera._cam
        native.events.clear()
        info = self.camera.snapshot()
        self.assertEqual(native.events[0], "stop")
        self.assertEqual(native.events[-1], "start")
        self.assertEqual(info.balance_ratios["Red"], 1024.0)
        self.assertTrue(self.camera._grabbing)
        self.assertIsNotNone(self.camera.read())

    def test_nested_apply_snapshots_pause_only_once(self):
        self.camera.open()
        native = self.camera._cam
        native.events.clear()
        info = self.camera.apply({"exposure_us": 1234.5, "balance_ratios": {"Red": 1200.0}})
        self.assertEqual(native.events.count("stop"), 1)
        self.assertEqual(native.events.count("start"), 1)
        self.assertEqual(info.exposure_us, 1234.5)
        self.assertEqual(info.balance_ratios["Red"], 1200.0)

    def test_parameter_error_still_restores_stream(self):
        self.camera.open()
        native = self.camera._cam
        native.set_ret = 0x80000106
        with self.assertRaisesRegex(CameraError, "80000106"):
            self.camera.apply({"exposure_us": 3000.0})
        self.assertTrue(self.camera._grabbing)
        self.assertTrue(native.native_grabbing)
        self.assertEqual(native.events[-1], "start")

    def test_restart_error_disconnects_camera_and_is_not_hidden(self):
        self.camera.open()
        native = self.camera._cam
        native.start_ret = 0x80000003
        with self.assertRaisesRegex(CameraError, "恢复相机取流.*80000003"):
            self.camera.snapshot()
        self.assertFalse(self.camera._grabbing)
        self.assertFalse(self.camera._opened)
        self.assertIsNone(self.camera._cam)
        self.assertEqual(native.cleanup[-2:], ["close", "destroy"])
        with self.assertRaisesRegex(CameraError, "未连接"):
            self.camera.snapshot()

    def test_parameter_and_restart_errors_are_both_reported(self):
        self.camera.open()
        native = self.camera._cam
        native.set_ret = 0x80000106
        native.start_ret = 0x80000003
        with self.assertRaises(CameraError) as caught:
            self.camera.apply({"exposure_us": 3000.0})
        self.assertIn("80000106", str(caught.exception))
        self.assertIn("80000003", str(caught.exception))
        self.assertIsNone(self.camera._cam)

    def test_pause_failure_disconnects_without_reading_parameters(self):
        self.camera.open()
        native = self.camera._cam
        native.stop_ret = 0x80000004
        native.events.clear()
        with self.assertRaisesRegex(CameraError, "暂停取流.*80000004"):
            self.camera.snapshot()
        self.assertTrue(all(not isinstance(event, tuple) for event in native.events))
        self.assertIsNone(self.camera._cam)

    def test_full_frame_clears_saved_subsampling_before_reading_max_size(self):
        FakeCamera.subsampling = True
        info = self.camera.open()
        native = self.camera._cam
        self.assertEqual((info.width, info.height), (8, 6))
        for node in ("BinningHorizontal", "BinningVertical", "DecimationHorizontal", "DecimationVertical"):
            self.assertEqual(native.ints[node][0], 1)
            self.assertLess(native.writes.index((node, 1)), native.writes.index(("Width", 8)))

    def test_multiple_devices_require_explicit_serial(self):
        FakeCamera.devices.append(FakeCamera.devices[0])
        with self.assertRaisesRegex(CameraError, "多台"):
            self.camera.open()

    def test_missing_serial_does_not_select_another_camera(self):
        self.camera.serial = "MISSING"
        with self.assertRaisesRegex(CameraError, "MISSING"):
            self.camera.open()


class DemoTests(unittest.TestCase):
    def test_demo_owns_frames_and_applies_settings(self):
        camera = DemoCamera(32, 24, 1000)
        camera.open()
        first = camera.read()
        saved = first.rgb.copy()
        second = camera.read()
        self.assertEqual(second.sequence, first.sequence + 1)
        np.testing.assert_array_equal(first.rgb, saved)
        self.assertGreater(second.timestamp_ns, first.timestamp_ns)
        self.assertEqual(camera.apply({"gain_db": 2.0}).gain_db, 2.0)
        self.assertEqual(camera.snapshot().model, "DEMO（非真实相机）")
        camera.close()
        with self.assertRaises(CameraError):
            camera.read()

    def test_invalid_settings_are_not_applied(self):
        camera = DemoCamera()
        camera.open()
        with self.assertRaises(CameraError):
            camera.apply({"gain_db": 100.0})
        self.assertEqual(camera.snapshot().gain_db, 0.0)


class LoaderTests(unittest.TestCase):
    def test_explicit_missing_path_cannot_select_another_sdk(self):
        with patch("slicing_recorder.camera.Path.is_file", return_value=False) as exists:
            with self.assertRaisesRegex(CameraError, "missing-sdk"):
                _load_sdk("missing-sdk")
        self.assertEqual(exists.call_count, 1)

    def test_configured_environment_path_cannot_select_another_sdk(self):
        with patch.dict("os.environ", {"MVS_PYTHON_PATH": "missing-environment-sdk"}):
            with patch("slicing_recorder.camera.Path.is_file", return_value=False) as exists:
                with self.assertRaisesRegex(CameraError, "missing-environment-sdk"):
                    _load_sdk()
        self.assertEqual(exists.call_count, 1)


if __name__ == "__main__":
    unittest.main()
