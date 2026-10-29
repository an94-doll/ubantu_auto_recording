"""Official MVS USB adapter and a deterministic, hardware-free demo camera.

The vendor's Python declarations come from the installed MVS SDK.  No SDK ABI
structures are duplicated here.  All methods belong to the acquisition thread.
"""
from __future__ import annotations

import ctypes as ct
import importlib
import math
import os
from pathlib import Path
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import numpy as np

from .models import CameraError, CameraInfo, Frame


def _load_sdk(sdk_path: str = "") -> SimpleNamespace:
    configured = sdk_path or os.environ.get("MVS_PYTHON_PATH", "")
    candidates = ([configured] if configured else
                  ["/opt/MVS/Samples/64/Python/MvImport", "/opt/MVS/Samples/Python/MvImport"])
    for candidate in filter(None, candidates):
        path = Path(candidate).expanduser()
        if (path / "MvImport").is_dir():
            path /= "MvImport"
        if (path / "MvCameraControl_class.py").is_file():
            sys.path.insert(0, str(path.resolve()))
            break
    else:
        detail = f"配置路径 {configured} 下" if configured else ""
        raise CameraError(f"{detail}找不到 MVS 的 MvImport，请设置 --sdk-path 或 MVS_PYTHON_PATH。")
    exports: dict[str, Any] = {}
    try:
        for name in ("CameraParams_header", "CameraParams_const", "MvErrorDefine_const",
                     "PixelType_header", "MvCameraControl_class"):
            exports.update(vars(importlib.import_module(name)))
    except (ImportError, OSError) as exc:
        raise CameraError(f"MVS 加载失败：{exc}；请检查 SDK 架构和动态库搜索路径。") from exc
    return SimpleNamespace(**exports)


def _text(value: Any) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("utf-8", errors="replace")


class HikCamera:
    def __init__(self, serial: str = "", sdk_path: str = ""):
        self.serial = serial
        self.sdk_path = sdk_path
        self._sdk = None
        self._cam = None
        self._created = self._opened = self._grabbing = self._initialized = False
        self._model = ""
        self._tick_hz: int | None = None
        self._fps_node = "AcquisitionFrameRate"

    @staticmethod
    def _check(ret: int, operation: str) -> None:
        if ret != 0:
            raise CameraError(f"{operation} 失败，MVS=0x{ret & 0xffffffff:08X}")

    def _int(self, node: str, required: bool = False):
        sdk = self._sdk
        extended = hasattr(self._cam, "MV_CC_GetIntValueEx")
        value = (sdk.MVCC_INTVALUE_EX if extended else sdk.MVCC_INTVALUE)()
        getter = self._cam.MV_CC_GetIntValueEx if extended else self._cam.MV_CC_GetIntValue
        ret = getter(node, value)
        if required:
            self._check(ret, f"读取 {node}")
        return value if ret == 0 else None

    def _float(self, node: str, required: bool = False):
        value = self._sdk.MVCC_FLOATVALUE()
        ret = self._cam.MV_CC_GetFloatValue(node, value)
        if required:
            self._check(ret, f"读取 {node}")
        return value if ret == 0 else None

    def _enum(self, node: str, required: bool = False):
        value = self._sdk.MVCC_ENUMVALUE()
        ret = self._cam.MV_CC_GetEnumValue(node, value)
        if required:
            self._check(ret, f"读取 {node}")
        return value if ret == 0 else None

    def _set_int(self, node: str, value: int) -> None:
        setter = getattr(self._cam, "MV_CC_SetIntValueEx", None) or self._cam.MV_CC_SetIntValue
        self._check(setter(node, int(value)), f"设置 {node}")

    def _set_enum(self, node: str, value: int) -> None:
        self._check(self._cam.MV_CC_SetEnumValue(node, int(value)), f"设置 {node}")

    @staticmethod
    def _supported(value) -> list[int]:
        return [int(value.nSupportedValue[i]) for i in range(value.nSupportedNum)]

    def _set_symbol(self, node: str, symbol: str, options: dict[str, int]) -> None:
        available = self._enum(node, True)
        if symbol not in options or options[symbol] not in self._supported(available):
            raise CameraError(f"{node} 不支持 {symbol}")
        self._set_enum(node, options[symbol])

    def _auto_options(self, field: str) -> dict[str, int]:
        # GenICam standard exposure/gain and white-balance enum values differ.
        prefix = {"exposure_auto": "MV_EXPOSURE_AUTO_", "gain_auto": "MV_GAIN_MODE_",
                  "balance_auto": "MV_BALANCEWHITE_AUTO_"}[field]
        suffix = {"Off": "OFF", "Once": "ONCE", "Continuous": "CONTINUOUS"}
        defaults = {"Off": 0, "Once": 2 if field == "balance_auto" else 1,
                    "Continuous": 1 if field == "balance_auto" else 2}
        return {name: int(getattr(self._sdk, prefix + part, defaults[name]))
                for name, part in suffix.items()}

    def open(self) -> CameraInfo:
        if self._cam is not None:
            raise CameraError("相机已经打开")
        self._sdk = _load_sdk(self.sdk_path)
        sdk = self._sdk
        try:
            if hasattr(sdk.MvCamera, "MV_CC_Initialize"):
                self._check(sdk.MvCamera.MV_CC_Initialize(), "初始化 MVS")
                self._initialized = True
            devices = sdk.MV_CC_DEVICE_INFO_LIST()
            self._check(sdk.MvCamera.MV_CC_EnumDevices(sdk.MV_USB_DEVICE, devices), "枚举 USB 相机")
            selected = []
            for i in range(devices.nDeviceNum):
                device = ct.cast(devices.pDeviceInfo[i], ct.POINTER(sdk.MV_CC_DEVICE_INFO)).contents
                usb = device.SpecialInfo.stUsb3VInfo
                serial = _text(usb.chSerialNumber)
                if not self.serial or serial == self.serial:
                    selected.append((device, serial, _text(usb.chModelName)))
            if not selected:
                raise CameraError(f"等待 USB 相机{('：' + self.serial) if self.serial else ''}")
            if len(selected) != 1:
                raise CameraError("发现多台 USB 相机，请用 --serial 指定序列号。")
            device, self.serial, self._model = selected[0]
            self._cam = sdk.MvCamera()
            self._check(self._cam.MV_CC_CreateHandle(device), "创建相机句柄")
            self._created = True
            self._check(self._cam.MV_CC_OpenDevice(getattr(sdk, "MV_ACCESS_Exclusive", 1), 0), "打开相机")
            self._opened = True
            self._configure_full_frame()
            tick = self._int("TimestampTickFrequency")
            self._tick_hz = int(tick.nCurValue) if tick is not None and tick.nCurValue > 0 else None
            info = self.snapshot()
            self._check(self._cam.MV_CC_StartGrabbing(), "开始取流")
            self._grabbing = True
            return info
        except Exception as exc:
            try:
                self.close()
            except CameraError as cleanup:
                raise CameraError(f"{exc}；清理失败：{cleanup}") from exc
            raise

    def _configure_full_frame(self) -> None:
        self._set_enum("TriggerMode", getattr(self._sdk, "MV_TRIGGER_MODE_OFF", 0))
        acquisition = self._enum("AcquisitionMode")
        if acquisition is not None:
            self._set_enum("AcquisitionMode", getattr(self._sdk, "MV_ACQ_MODE_CONTINUOUS", 2))
        for node in ("OffsetX", "OffsetY"):
            current = self._int(node)
            if current is not None:
                self._set_int(node, current.nMin)
        # A previous MVS session may have left sensor subsampling enabled.
        # Restore the least subsampling before querying the full image bounds.
        for node in ("BinningHorizontal", "BinningVertical", "DecimationHorizontal", "DecimationVertical"):
            value = self._int(node)
            if value is not None:
                increment = max(1, int(value.nInc))
                minimum = int(value.nMin)
                target = 1 if minimum <= 1 <= int(value.nMax) and (1 - minimum) % increment == 0 else minimum
                if int(value.nCurValue) != target:
                    self._set_int(node, target)
        for node in ("Width", "Height"):
            value = self._int(node, True)
            increment = max(1, int(value.nInc))
            maximum = int(value.nMin) + ((int(value.nMax) - int(value.nMin)) // increment) * increment
            self._set_int(node, maximum)
        pixels = self._enum("PixelFormat", True)
        supported = self._supported(pixels)
        names = ("PixelType_Gvsp_BayerGR8", "PixelType_Gvsp_BayerRG8",
                 "PixelType_Gvsp_BayerGB8", "PixelType_Gvsp_BayerBG8",
                 "PixelType_Gvsp_RGB8_Packed", "PixelType_Gvsp_Mono8")
        candidates = [getattr(self._sdk, name) for name in names if hasattr(self._sdk, name)]
        # Preserve an already selected 8-bit Bayer layout when possible.
        bayer = [getattr(self._sdk, name) for name in names[:4] if hasattr(self._sdk, name)]
        selected = int(pixels.nCurValue) if int(pixels.nCurValue) in bayer else None
        if selected is None:
            selected = next((int(p) for p in candidates if int(p) in supported), None)
        if selected is None:
            raise CameraError("相机未提供受支持的 Bayer8 / RGB8 / Mono8 像素格式。")
        self._set_enum("PixelFormat", selected)
        enabled = ct.c_bool()
        if self._cam.MV_CC_GetBoolValue("AcquisitionFrameRateEnable", enabled) == 0:
            self._check(self._cam.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True), "启用帧率控制")
        self._fps_node = "AcquisitionFrameRate"
        rate = self._float(self._fps_node)
        if rate is None:
            self._fps_node = "AcquisitionFrameRateAbs"
            rate = self._float(self._fps_node, True)
        self._check(self._cam.MV_CC_SetFloatValue(self._fps_node, float(rate.fMax)), "设置最高帧率")

    @contextmanager
    def _parameter_access(self):
        """Pause preview once for nodes that lock while acquisition is active.

        Public snapshot/apply are called only before recording or in preview.
        Nested snapshots inside apply do not restart the stream prematurely.
        A failed restart closes the device so it cannot advertise a live camera.
        """
        if not self._opened:
            raise CameraError("相机未连接")
        resume = self._grabbing
        if resume:
            try:
                self._check(self._cam.MV_CC_StopGrabbing(), "暂停取流以访问相机参数")
            except Exception as exc:
                message = str(exc)
                try:
                    self.close()
                except Exception as cleanup:
                    message += f"；关闭失败：{cleanup}"
                raise CameraError(message) from exc
            self._grabbing = False
        operation_error = None
        try:
            yield
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            if resume:
                try:
                    self._check(self._cam.MV_CC_StartGrabbing(), "恢复相机取流")
                    self._grabbing = True
                except Exception as restart:
                    message = f"{operation_error}；{restart}" if operation_error else str(restart)
                    try:
                        self.close()
                    except Exception as cleanup:
                        message += f"；关闭失败：{cleanup}"
                    raise CameraError(message) from (operation_error or restart)

    def snapshot(self) -> CameraInfo:
        with self._parameter_access():
            return self._snapshot()

    def _snapshot(self) -> CameraInfo:
        width, height = self._int("Width", True), self._int("Height", True)
        pixel = self._enum("PixelFormat", True)
        names = {int(value): name.removeprefix("PixelType_Gvsp_")
                 for name, value in vars(self._sdk).items()
                 if name.startswith("PixelType_Gvsp_") and isinstance(value, int)}
        requested = self._float(self._fps_node, True)
        actual = self._float("ResultingFrameRate") or self._float("ResultingFrameRateAbs") or requested
        info = CameraInfo(self._model, self.serial, int(width.nCurValue), int(height.nCurValue),
                          names.get(int(pixel.nCurValue), f"0x{int(pixel.nCurValue):08X}"), float(actual.fCurValue))
        info.ranges["fps"] = (float(requested.fMin), float(requested.fMax))
        for field, node in (("exposure_us", "ExposureTime"), ("gain_db", "Gain")):
            value = self._float(node)
            if value is not None:
                setattr(info, field, float(value.fCurValue))
                info.ranges[field] = (float(value.fMin), float(value.fMax))
        for field, node in (("exposure_auto", "ExposureAuto"), ("gain_auto", "GainAuto"),
                            ("balance_auto", "BalanceWhiteAuto")):
            value = self._enum(node)
            if value is not None:
                options = self._auto_options(field)
                setattr(info, field, next((k for k, v in options.items() if v == value.nCurValue),
                                          str(value.nCurValue)))
                info.modes[field] = [k for k, v in options.items() if v in self._supported(value)]
        selector = self._enum("BalanceRatioSelector")
        if selector is not None:
            try:
                for channel, enum in (("Red", 0), ("Green", 1), ("Blue", 2)):
                    if enum not in self._supported(selector):
                        continue
                    self._set_enum("BalanceRatioSelector", enum)
                    value = self._int("BalanceRatio")
                    if value is not None:
                        info.balance_ratios[channel] = float(value.nCurValue)
                        info.ranges["balance_" + channel.lower()] = (float(value.nMin), float(value.nMax))
            finally:
                self._set_enum("BalanceRatioSelector", selector.nCurValue)
        return info

    def apply(self, settings: dict[str, Any]) -> CameraInfo:
        with self._parameter_access():
            return self._apply(settings)

    def _apply(self, settings: dict[str, Any]) -> CameraInfo:
        allowed = {"exposure_us", "gain_db", "exposure_auto", "gain_auto", "balance_auto", "balance_ratios"}
        unknown = set(settings) - allowed
        if unknown:
            raise CameraError("未知相机参数：" + ", ".join(sorted(unknown)))
        before = self.snapshot()
        for field in ("exposure_auto", "gain_auto", "balance_auto"):
            if field in settings and settings[field] not in before.modes.get(field, []):
                raise CameraError(f"相机不支持 {field}={settings[field]}")
        for field, mode in (("exposure_us", "exposure_auto"), ("gain_db", "gain_auto")):
            if field in settings:
                value = float(settings[field])
                limits = before.ranges.get(field)
                if limits is None or not math.isfinite(value) or not limits[0] <= value <= limits[1]:
                    raise CameraError(f"{field} 超出相机范围 {limits}")
                if settings.get(mode, getattr(before, mode)) not in (None, "Off"):
                    raise CameraError(f"手动设置 {field} 前请关闭自动模式")
        ratios = settings.get("balance_ratios", {})
        if ratios and settings.get("balance_auto", before.balance_auto) not in (None, "Off"):
            raise CameraError("手动设置白平衡比例前请关闭自动白平衡")
        for channel, value in ratios.items():
            limits = before.ranges.get("balance_" + channel.lower())
            if channel not in before.balance_ratios or limits is None or not math.isfinite(float(value)) or not limits[0] <= float(value) <= limits[1]:
                raise CameraError(f"白平衡 {channel} 超出相机范围 {limits}")
        for field, node in (("exposure_auto", "ExposureAuto"), ("gain_auto", "GainAuto"),
                            ("balance_auto", "BalanceWhiteAuto")):
            if field in settings:
                self._set_symbol(node, settings[field], self._auto_options(field))
        for field, node in (("exposure_us", "ExposureTime"), ("gain_db", "Gain")):
            if field in settings:
                self._check(self._cam.MV_CC_SetFloatValue(node, float(settings[field])), f"设置 {node}")
        if ratios:
            selector = self._enum("BalanceRatioSelector", True)
            try:
                for channel, value in ratios.items():
                    self._set_enum("BalanceRatioSelector", {"Red": 0, "Green": 1, "Blue": 2}[channel])
                    self._set_int("BalanceRatio", round(float(value)))
            finally:
                self._set_enum("BalanceRatioSelector", selector.nCurValue)
        return self.snapshot()

    def read(self, timeout_ms: int = 250) -> Frame | None:
        if not self._grabbing:
            raise CameraError("相机尚未开始取流")
        out = self._sdk.MV_FRAME_OUT()
        ret = self._cam.MV_CC_GetImageBuffer(out, timeout_ms)
        if ret != 0:
            if (ret & 0xffffffff) == (getattr(self._sdk, "MV_E_NODATA", 0x80000007) & 0xffffffff):
                if hasattr(self._cam, "MV_CC_IsDeviceConnected") and not self._cam.MV_CC_IsDeviceConnected():
                    raise CameraError("USB 相机已断开")
                return None
            self._check(ret, "获取图像")
        host_ns, wall = time.perf_counter_ns(), datetime.now().astimezone()
        try:
            info = out.stFrameInfo
            width, height = int(info.nWidth), int(info.nHeight)
            if width <= 0 or height <= 0 or info.nFrameLen <= 0 or not out.pBufAddr:
                raise CameraError("MVS 返回空帧或无效尺寸")
            if getattr(info, "nLostPacket", 0):
                raise CameraError("MVS 返回不完整帧（nLostPacket 非零）")
            rgb = np.empty((height, width, 3), dtype=np.uint8)
            converter = getattr(self._cam, "MV_CC_ConvertPixelTypeEx", None)
            parameter_type = getattr(self._sdk, "MV_CC_PIXEL_CONVERT_PARAM_EX", None)
            if converter is None or parameter_type is None:
                converter = self._cam.MV_CC_ConvertPixelType
                parameter_type = self._sdk.MV_CC_PIXEL_CONVERT_PARAM
            params = parameter_type()
            params.nWidth, params.nHeight = width, height
            params.enSrcPixelType, params.pSrcData = info.enPixelType, out.pBufAddr
            params.nSrcDataLen = info.nFrameLen
            params.enDstPixelType = self._sdk.PixelType_Gvsp_RGB8_Packed
            params.pDstBuffer = rgb.ctypes.data_as(ct.POINTER(ct.c_ubyte))
            params.nDstBufferSize = rgb.nbytes
            self._check(converter(params), "转换 RGB 图像")
            if int(params.nDstLen) != rgb.nbytes:
                raise CameraError("MVS RGB 转换输出长度与实际分辨率不一致")
            ticks = (int(info.nDevTimeStampHigh) << 32) | int(info.nDevTimeStampLow)
            device_time = self._tick_hz is not None
            timestamp = ticks * 1_000_000_000 // self._tick_hz if device_time else host_ns
            return Frame(rgb, int(info.nFrameNum), timestamp, wall,
                         "camera_device" if device_time else "host_monotonic")
        finally:
            self._check(self._cam.MV_CC_FreeImageBuffer(out), "释放 MVS 图像缓冲")

    def close(self) -> None:
        errors = []
        if self._cam is not None:
            for active, method in ((self._grabbing, "MV_CC_StopGrabbing"),
                                   (self._opened, "MV_CC_CloseDevice"),
                                   (self._created, "MV_CC_DestroyHandle")):
                if active:
                    try:
                        self._check(getattr(self._cam, method)(), method)
                    except Exception as exc:
                        errors.append(str(exc))
        self._cam = None
        self._created = self._opened = self._grabbing = False
        if self._initialized:
            try:
                self._check(self._sdk.MvCamera.MV_CC_Finalize(), "结束 MVS")
            except Exception as exc:
                errors.append(str(exc))
            self._initialized = False
        if errors:
            raise CameraError("；".join(errors))


class DemoCamera:
    def __init__(self, width: int = 640, height: int = 480, fps: float = 30.0):
        if width < 2 or height < 2 or fps <= 0:
            raise ValueError("演示相机尺寸和帧率必须为正数")
        self.width, self.height, self.fps = width, height, fps
        self._opened = False
        self._sequence = 0
        self._next = 0.0
        self._settings: dict[str, Any] = dict(exposure_us=5000.0, gain_db=0.0,
                                            exposure_auto="Off", gain_auto="Off", balance_auto="Off",
                                            balance_ratios={"Red": 1024.0, "Green": 1024.0, "Blue": 1024.0})
        x = np.arange(width, dtype=np.uint16)[None, :]
        y = np.arange(height, dtype=np.uint16)[:, None]
        self._base = np.empty((height, width, 3), dtype=np.uint8)
        self._base[..., 0] = x % 256
        self._base[..., 1] = y % 256
        self._base[..., 2] = (x // 4 + y // 4) % 256

    def open(self) -> CameraInfo:
        self._opened, self._next, self._sequence = True, time.perf_counter(), 0
        return self.snapshot()

    def snapshot(self) -> CameraInfo:
        if not self._opened:
            raise CameraError("演示相机未连接")
        info = CameraInfo("DEMO（非真实相机）", "DEMO-001", self.width, self.height, "RGB8_Packed", self.fps,
                          **{k: (v.copy() if isinstance(v, dict) else v) for k, v in self._settings.items()})
        info.ranges = {"exposure_us": (50.0, 1000000.0), "gain_db": (0.0, 24.0), "fps": (self.fps, self.fps),
                       "balance_red": (1.0, 4095.0), "balance_green": (1.0, 4095.0), "balance_blue": (1.0, 4095.0)}
        info.modes = {name: ["Off", "Once", "Continuous"] for name in ("exposure_auto", "gain_auto", "balance_auto")}
        return info

    def apply(self, settings: dict[str, Any]) -> CameraInfo:
        info = self.snapshot()
        for key, value in settings.items():
            if key in info.modes:
                if value not in info.modes[key]:
                    raise CameraError(f"无效模式：{key}={value}")
            elif key in ("exposure_us", "gain_db"):
                lo, hi = info.ranges[key]
                if not math.isfinite(float(value)) or not lo <= float(value) <= hi:
                    raise CameraError(f"{key} 超出范围")
            elif key == "balance_ratios":
                if any(c not in info.balance_ratios or not math.isfinite(float(v)) or not 1 <= float(v) <= 4095 for c, v in value.items()):
                    raise CameraError("无效白平衡比例")
            else:
                raise CameraError(f"未知相机参数：{key}")
        for key, value in settings.items():
            if key == "balance_ratios":
                self._settings[key].update(value)
            else:
                self._settings[key] = value
        return self.snapshot()

    def read(self, timeout_ms: int = 250) -> Frame | None:
        if not self._opened:
            raise CameraError("演示相机未连接")
        delay = max(0.0, self._next - time.perf_counter())
        if delay > timeout_ms / 1000:
            time.sleep(timeout_ms / 1000)
            return None
        if delay:
            time.sleep(delay)
        timestamp = time.perf_counter_ns()
        self._next += 1 / self.fps
        self._sequence += 1
        rgb = self._base.copy()
        position = (self._sequence * 7) % self.width
        rgb[:, position:position + min(16, self.width)] = 255
        return Frame(rgb, self._sequence, timestamp, datetime.now().astimezone())

    def close(self) -> None:
        self._opened = False
