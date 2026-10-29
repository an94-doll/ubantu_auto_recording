"""Qt front end. The controller owns acquisition and recording threads."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)


STATE_LABELS = {
    "connecting": "等待相机",
    "preview": "预览中 · 可以录制",
    "recording": "正在录制",
    "saving": "正在保存 · 请稍候",
    "error": "需要处理",
    "closed": "已关闭",
}

STYLESHEET = """
QMainWindow, QWidget#central { background: #f1f4f7; color: #172b3b; }
QWidget { font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; font-size: 13px; }
QFrame#header { background: #162f40; border-radius: 9px; }
QLabel#title { color: #ffffff; font-size: 23px; font-weight: 600; }
QLabel#subtitle { color: #a9c4d2; font-size: 11px; letter-spacing: 1px; }
QLabel#cameraLabel { color: #cfe0e9; }
QLabel#statusLabel { border-radius: 6px; padding: 9px 14px; font-weight: 600; }
QGroupBox { background: #ffffff; border: 1px solid #d8e1e7; border-radius: 8px;
    margin-top: 9px; padding: 17px 14px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; }
QLabel#muted { color: #617887; font-size: 12px; }
QLabel#messageLabel { padding: 8px 11px; border-radius: 5px; background: #e7eef3; color: #405d6f; }
QLabel#outputFileLabel { color: #536f80; font-size: 12px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #ffffff; color: #172b3b;
    border: 1px solid #c7d4dc; border-radius: 4px; padding: 6px; min-height: 19px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #008c83; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: #eff3f5; color: #8294a1; border-color: #dfe6ea; }
QPushButton { background: #f7fafb; border: 1px solid #bdcdd7; border-radius: 5px;
    padding: 8px 13px; color: #29485c; font-weight: 500; }
QPushButton:hover { background: #e9f1f5; border-color: #849fad; }
QPushButton:disabled { background: #eef2f4; color: #96a5ae; border-color: #dbe3e8; }
QPushButton#startButton { background: #007f78; color: #ffffff; border-color: #007f78;
    font-size: 16px; font-weight: 600; padding: 12px 23px; }
QPushButton#startButton:hover { background: #006961; }
QPushButton#startButton:disabled { background: #bacdcd; color: #f3f7f7; border-color: #bacdcd; }
QPushButton#stopButton { background: #fff3f0; color: #a43d2c; border-color: #dba99e;
    font-size: 15px; padding: 12px 19px; }
QPushButton#stopButton:disabled { background: #f0f2f4; color: #9ba8b0; border-color: #dbe3e8; }
QScrollArea#previewArea, QLabel#previewLabel { background: #10222e; border: none; color: #94aebd; }
QScrollArea#parameterArea { border: none; background: transparent; }
QLabel#statValue { color: #163b4e; font-size: 20px; font-weight: 600; }
QLabel#statCaption { color: #627b8a; font-size: 11px; }
QCheckBox { color: #486879; spacing: 6px; }
QToolTip { color: #172b3b; background: #ffffff; border: 1px solid #b7cad5; }
"""


class PreviewView(QScrollArea):
    """Keep only the latest displayed image; never queue preview frames."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("previewArea")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidgetResizable(True)
        self.label = QLabel("等待相机连接…")
        self.label.setObjectName("previewLabel")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMinimumSize(1, 1)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setWidget(self.label)
        self._image: QPixmap | None = None
        self._key: tuple[int, int, int] | None = None
        self._fit = True

    def set_fit(self, fit: bool) -> None:
        self._fit = fit
        self.setWidgetResizable(fit)
        self._render()

    def show_frame(self, frame: Any) -> None:
        key = (frame.sequence, frame.timestamp_ns, id(frame.rgb))
        if key == self._key:
            return
        rgb = frame.rgb
        height, width, _ = rgb.shape
        # Copy at the UI boundary: QImage must not outlive the NumPy owner.
        image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
        self._image = QPixmap.fromImage(image)
        self._key = key
        self._render()

    def _render(self) -> None:
        if self._image is None:
            return
        if self._fit:
            size = self.viewport().size()
            pixmap = self._image.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            self.label.setPixmap(pixmap)
        else:
            self.label.setPixmap(self._image)
            self.label.resize(self._image.size())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._render()


class MainWindow(QMainWindow):
    def __init__(self, controller: Any, config: Any) -> None:
        super().__init__()
        self.controller = controller
        self._closing = False
        self._closed = False
        self._info: dict[str, Any] | None = None
        self._state = "connecting"
        self._specification = "全分辨率 · 设备最高帧率"
        self._last_status_style = ""
        self._number_controls: dict[str, QDoubleSpinBox] = {}
        self._mode_controls: dict[str, QComboBox] = {}
        self._parameter_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self.setWindowTitle("海康相机采集台")
        self.setMinimumSize(1080, 720)
        self.resize(1360, 880)
        self.setStyleSheet(STYLESHEET)
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(12)
        self._build_header(layout)
        body = QHBoxLayout()
        body.setSpacing(13)
        self._build_preview(body)
        self._build_parameters(body)
        layout.addLayout(body, 1)
        self._build_recording(layout, config)
        self._build_status(layout)
        self.timer = QTimer(self)
        self.timer.setInterval(max(20, int(1000 / max(1, config.preview_fps))))
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _build_header(self, layout: QVBoxLayout) -> None:
        header = QFrame()
        header.setObjectName("header")
        row = QHBoxLayout(header)
        row.setContentsMargins(19, 14, 19, 14)
        text = QVBoxLayout()
        title = QLabel("相机采集台")
        title.setObjectName("title")
        subtitle = QLabel("HIKROBOT  /  USB INDUSTRIAL CAMERA")
        subtitle.setObjectName("subtitle")
        text.addWidget(title)
        text.addWidget(subtitle)
        row.addLayout(text)
        row.addSpacing(25)
        self.camera_label = QLabel("正在查找 USB 相机")
        self.camera_label.setObjectName("cameraLabel")
        self.camera_label.setWordWrap(True)
        row.addWidget(self.camera_label, 1)
        self.status_label = QLabel("等待相机")
        self.status_label.setObjectName("statusLabel")
        row.addWidget(self.status_label)
        layout.addWidget(header)

    def _build_preview(self, layout: QHBoxLayout) -> None:
        group = QGroupBox("实时预览")
        preview_layout = QVBoxLayout(group)
        toolbar = QHBoxLayout()
        self.spec_label = QLabel("全分辨率 · 设备最高帧率")
        self.spec_label.setObjectName("muted")
        toolbar.addWidget(self.spec_label, 1)
        self.fit_check = QCheckBox("适应窗口")
        self.fit_check.setChecked(True)
        self.fit_check.setToolTip("取消勾选后，以原始像素比例显示，可滚动查看局部。")
        self.fit_check.setObjectName("fitCheck")
        toolbar.addWidget(self.fit_check)
        preview_layout.addLayout(toolbar)
        self.preview_view = PreviewView()
        preview_layout.addWidget(self.preview_view, 1)
        self.fit_check.toggled.connect(self.preview_view.set_fit)
        layout.addWidget(group, 1)

    def _build_parameters(self, layout: QHBoxLayout) -> None:
        group = QGroupBox("相机参数")
        group.setMinimumWidth(315)
        group.setMaximumWidth(355)
        box = QVBoxLayout(group)
        self.parameter_hint = QLabel("连接后读取设备支持的参数与范围。")
        self.parameter_hint.setObjectName("muted")
        self.parameter_hint.setWordWrap(True)
        box.addWidget(self.parameter_hint)
        scroll = QScrollArea()
        scroll.setObjectName("parameterArea")
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setStyleSheet("background: #ffffff;")
        self.parameter_form = QFormLayout(content)
        self.parameter_form.setContentsMargins(0, 8, 3, 5)
        self.parameter_form.setSpacing(9)
        self.parameter_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for key, title in (
            ("exposure_auto", "曝光模式"), ("exposure_us", "曝光 / μs"),
            ("gain_auto", "增益模式"), ("gain_db", "增益 / dB"),
            ("balance_auto", "白平衡模式"), ("balance_red", "白平衡 R"),
            ("balance_green", "白平衡 G"), ("balance_blue", "白平衡 B"),
        ):
            label = QLabel(title)
            if key.endswith("_auto"):
                control = QComboBox()
                control.currentIndexChanged.connect(self._lock_parameters)
                self._mode_controls[key] = control
            else:
                control = QDoubleSpinBox()
                control.setDecimals(1 if key == "exposure_us" else 2)
                control.setKeyboardTracking(False)
                self._number_controls[key] = control
            control.setObjectName(key)
            self.parameter_form.addRow(label, control)
            self._parameter_rows[key] = (label, control)
            label.hide()
            control.hide()
        scroll.setWidget(content)
        box.addWidget(scroll, 1)
        self.apply_button = QPushButton("应用并保存")
        self.apply_button.setObjectName("applyButton")
        self.apply_button.clicked.connect(self._apply)
        self.restore_button = QPushButton("恢复上次设置")
        self.restore_button.setObjectName("restoreButton")
        self.restore_button.clicked.connect(self._restore)
        box.addWidget(self.apply_button)
        box.addWidget(self.restore_button)
        layout.addWidget(group)

    def _build_recording(self, layout: QVBoxLayout, config: Any) -> None:
        group = QGroupBox("录制")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        grid.addWidget(QLabel("保存目录"), 0, 0)
        self.output_edit = QLineEdit(config.output_dir)
        self.output_edit.setObjectName("outputEdit")
        self.output_edit.setPlaceholderText("选择视频和 recordings.csv 的保存目录")
        grid.addWidget(self.output_edit, 0, 1, 1, 4)
        self.browse_button = QPushButton("选择目录")
        self.browse_button.setObjectName("browseButton")
        self.browse_button.clicked.connect(self._browse)
        grid.addWidget(self.browse_button, 0, 5)
        grid.addWidget(QLabel("采集名称"), 1, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("nameEdit")
        self.name_edit.setPlaceholderText("实验编号（可选）")
        self.name_edit.setMaxLength(80)
        grid.addWidget(self.name_edit, 1, 1)
        self.codec_combo = QComboBox()
        self.codec_combo.setObjectName("codecCombo")
        for label, key in (
            ("自动选择 H.265", "auto"), ("H.265 · 软件 x265", "libx265"),
            ("H.265 · NVIDIA", "hevc_nvenc"), ("H.265 · Intel QSV", "hevc_qsv"),
        ):
            self.codec_combo.addItem(label, key)
        self.codec_combo.setCurrentIndex(max(0, self.codec_combo.findData(config.codec)))
        self.codec_combo.setToolTip("自动模式实际探测硬件编码器；不可用时使用软件 H.265，不切换为 H.264。")
        grid.addWidget(self.codec_combo, 1, 2)
        grid.addWidget(QLabel("质量值"), 1, 3)
        self.quality_spin = QSpinBox()
        self.quality_spin.setObjectName("qualitySpin")
        self.quality_spin.setRange(0, 51)
        self.quality_spin.setValue(config.quality)
        self.quality_spin.setToolTip("CRF / CQ：数值越小，画质越高，文件通常越大。不同编码器不能直接按数值比较画质。")
        grid.addWidget(self.quality_spin, 1, 4)
        self.start_button = QPushButton("开始录制")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        grid.addWidget(self.start_button, 1, 5, 2, 1)
        self.stop_button = QPushButton("停止并保存")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.clicked.connect(self.controller.request_stop)
        grid.addWidget(self.stop_button, 1, 6, 2, 1)
        self.output_file_label = QLabel("H.265 + MKV · 每次一个视频 · 采集信息汇总到 recordings.csv")
        self.output_file_label.setObjectName("outputFileLabel")
        self.output_file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.output_file_label.setWordWrap(True)
        grid.addWidget(self.output_file_label, 2, 0, 1, 5)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(2, 1)
        layout.addWidget(group)

    def _build_status(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(26)
        self.stats: dict[str, QLabel] = {}
        for key, caption, initial in (
            ("duration", "录制时长", "00:00:00"), ("fps", "实际采集 / fps", "—"),
            ("received", "接收帧", "0"), ("written", "已编码帧", "0"),
            ("dropped", "可检测丢帧", "0"), ("queue", "待编码帧", "0"),
            ("disk", "磁盘可用 / GB", "—"),
        ):
            box = QVBoxLayout()
            box.setSpacing(2)
            value = QLabel(initial)
            value.setObjectName("statValue")
            value.setProperty("stat", key)
            caption_label = QLabel(caption)
            caption_label.setObjectName("statCaption")
            box.addWidget(value)
            box.addWidget(caption_label)
            row.addLayout(box, 1)
            self.stats[key] = value
        layout.addLayout(row)
        self.message_label = QLabel("连接相机后即可预览。开机只打开页面，不自动录制。")
        self.message_label.setObjectName("messageLabel")
        self.message_label.setWordWrap(True)
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message_label.setToolTip(self.message_label.text())
        self.message_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.message_label.customContextMenuRequested.connect(self._message_menu)
        layout.addWidget(self.message_label)
        self.message_label.ensurePolished()
        self.message_label.setMaximumHeight(self.message_label.fontMetrics().lineSpacing() * 3 + 18)

    def _message_menu(self, position: Any) -> None:
        menu = QMenu(self)
        menu.addAction("复制完整提示", self._copy_message)
        menu.exec(self.message_label.mapToGlobal(position))

    def _copy_message(self) -> None:
        QApplication.clipboard().setText(self.message_label.text())

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择采集保存目录", self.output_edit.text())
        if directory:
            self.output_edit.setText(directory)

    def _start(self) -> None:
        directory = self.output_edit.text().strip()
        if not directory:
            self._show_message("请先选择保存目录。", True)
            self.output_edit.setFocus()
            return
        self.controller.request_start(
            directory, self.name_edit.text().strip(),
            self.codec_combo.currentData(), self.quality_spin.value(),
        )

    def _apply(self) -> None:
        settings: dict[str, Any] = {}
        for key, control in self._mode_controls.items():
            if key in (self._info or {}).get("modes", {}):
                settings[key] = control.currentData()
        ratios: dict[str, float] = {}
        for key, control in self._number_controls.items():
            if control.isEnabled():
                if key.startswith("balance_"):
                    ratios[key.removeprefix("balance_").capitalize()] = control.value()
                else:
                    settings[key] = control.value()
        if ratios:
            settings["balance_ratios"] = ratios
        self.controller.request_apply(settings)

    def _restore(self) -> None:
        self.controller.request_restore()
        # Rehydrate local edits even when restoring leaves device readback unchanged.
        # If device values change, the controller's subsequent readback updates them again.
        self._info = None

    def _update_info(self, info: dict[str, Any]) -> None:
        self._info = info
        self.camera_label.setText(f"{info['model']}\n序列号  {info['serial']}")
        specification = f"{info['width']} × {info['height']}  ·  {info['pixel_format']}  ·  设备回读 {info['fps']:.2f} fps"
        fps_range = info.get("ranges", {}).get("fps")
        if fps_range:
            specification += f"  ·  设置上限 {fps_range[1]:.2f} fps"
        self.spec_label.setText(specification)
        self.spec_label.setWordWrap(True)
        self._specification = specification
        self.parameter_hint.setText("录制期间锁定参数。应用后读回并保存实际生效值。")
        ranges = info.get("ranges", {})
        modes = info.get("modes", {})
        for key, control in self._mode_controls.items():
            supported = bool(modes.get(key))
            for widget in self._parameter_rows[key]:
                widget.setVisible(supported)
            control.blockSignals(True)
            control.clear()
            for mode in modes.get(key, []):
                names = {"Off": "手动 / 关闭自动", "Once": "单次自动", "Continuous": "连续自动"}
                control.addItem(names.get(mode, mode), mode)
            control.setCurrentIndex(max(0, control.findData(info.get(key))))
            control.blockSignals(False)
        for key, control in self._number_controls.items():
            supported = key in ranges
            for widget in self._parameter_rows[key]:
                widget.setVisible(supported)
            if not supported:
                continue
            low, high = ranges[key]
            control.setRange(low, high)
            control.setSingleStep(10 if key == "exposure_us" else 1 if key.startswith("balance_") else 0.1)
            if key.startswith("balance_"):
                value = info.get("balance_ratios", {}).get(key.removeprefix("balance_").capitalize())
            else:
                value = info.get(key)
            if value is not None:
                control.setValue(float(value))
        self._lock_parameters()

    def _lock_parameters(self, *_: Any) -> None:
        editable = self._state == "preview" and self._info is not None and not self._closing
        info = self._info or {}
        for key, control in self._mode_controls.items():
            control.setEnabled(editable and bool(info.get("modes", {}).get(key)))
        for key, control in self._number_controls.items():
            mode_key = "balance_auto" if key.startswith("balance_") else "exposure_auto" if key == "exposure_us" else "gain_auto"
            mode = self._mode_controls[mode_key]
            automatic = bool(mode.count()) and mode.currentData() != "Off"
            control.setEnabled(editable and key in info.get("ranges", {}) and not automatic)
        supported = bool(info.get("ranges") or info.get("modes"))
        self.apply_button.setEnabled(editable and supported)
        self.restore_button.setEnabled(editable)

    def _show_message(self, text: str, error: bool = False) -> None:
        self.message_label.setText(text)
        self.message_label.setToolTip(text)
        if error:
            self.message_label.setStyleSheet("background: #fff0e9; color: #9b3824; border: 1px solid #e9b9a5;")
        else:
            self.message_label.setStyleSheet("")

    def refresh(self) -> None:
        data = self.controller.snapshot()
        self._state = data.get("state", "connecting")
        info = data.get("camera_info")
        if info and info != self._info:
            self._update_info(info)
        ready = self._state == "preview" and self._info is not None and not self._closing
        self.spec_label.setText(
            self._specification if self._state in {"preview", "recording", "saving"}
            else "相机未在实时预览 · 保留最后画面" if self._info else "全分辨率 · 等待设备连接"
        )
        self.start_button.setEnabled(ready)
        self.stop_button.setEnabled(self._state == "recording" and not self._closing)
        for control in (
            self.output_edit, self.browse_button, self.name_edit, self.codec_combo, self.quality_spin,
        ):
            control.setEnabled(self._state not in {"recording", "saving"} and not self._closing)
        self._lock_parameters()
        label = "正在关闭 · 等待视频收尾" if self._closing else STATE_LABELS.get(self._state, self._state)
        self.status_label.setText(label)
        colors = {
            "preview": ("#d3efe9", "#0b655b"), "recording": ("#ffe0d9", "#9e3523"),
            "saving": ("#ffedc9", "#805818"), "error": ("#ffe0d9", "#9e3523"),
        }
        background, color = colors.get(self._state, ("#dce7ed", "#405d6f"))
        style = f"background: {background}; color: {color};"
        if style != self._last_status_style:
            self.status_label.setStyleSheet(style)
            self._last_status_style = style
        duration = max(0, int(data.get("duration_s", 0)))
        self.stats["duration"].setText(f"{duration // 3600:02d}:{duration % 3600 // 60:02d}:{duration % 60:02d}")
        self.stats["fps"].setText(f"{data.get('captured_fps', 0):.1f}")
        for key in ("received", "written", "dropped", "queue"):
            self.stats[key].setText(f"{data.get(key, 0):,}")
        disk = data.get("disk_free_gb")
        self.stats["disk"].setText("—" if disk is None else f"{disk:.1f}")
        if data.get("output_file"):
            encoder = data.get("encoder") or "H.265"
            self.output_file_label.setText(f"{encoder}  ·  {data['output_file']}")
        error = str(data.get("error") or "")
        message = str(data.get("message") or "")
        if self._closing:
            self._show_message("正在停止采集并关闭视频文件，请勿关闭电源。")
        elif error:
            self._show_message(error, True)
        elif message:
            self._show_message(message)
        frame = self.controller.preview()
        if frame is not None:
            self.preview_view.show_frame(frame)
        if self._closing and not self.controller.is_alive():
            self._closed = True
            self.timer.stop()
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed or not self.controller.is_alive():
            self.timer.stop()
            event.accept()
            return
        if not self._closing:
            self._closing = True
            self.controller.shutdown()
            self.refresh()
        event.ignore()
