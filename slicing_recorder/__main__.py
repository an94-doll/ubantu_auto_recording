from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="海康 USB 单相机 H.265 数据采集")
    parser.add_argument("--demo", action="store_true", help="使用合成相机，不需要 MVS 或真实硬件")
    parser.add_argument("--config", type=Path, help="配置文件；默认 ~/.config/slicing-recorder/config.json")
    parser.add_argument("--serial", help="指定 USB 相机序列号，成功连接后保存")
    parser.add_argument("--sdk-path", help="MVS 的 MvImport 目录，成功连接后保存")
    parser.add_argument("--demo-width", type=int, default=640)
    parser.add_argument("--demo-height", type=int, default=480)
    parser.add_argument("--demo-fps", type=float, default=30.0)
    args = parser.parse_args(argv)

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .camera import DemoCamera, HikCamera
    from .config import ConfigStore, default_config_path
    from .controller import Controller
    from .ui import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Slicing Recorder")
    app.setOrganizationName("Slicing")
    # One application instance per user, even with different --config paths.
    lock_dir = default_config_path().parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / "application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.warning(None, "程序已运行", "已有采集程序在运行，请使用现有窗口。")
        return 1
    path = args.config or default_config_path().with_name("demo.json" if args.demo else "config.json")
    store = ConfigStore(path)
    try:
        config = store.load()
        if args.serial is not None:
            config.camera_serial = args.serial
        if args.sdk_path is not None:
            config.sdk_path = args.sdk_path
        if args.demo:
            # Demo recordings are visibly separated from real acquisition by default.
            if not path.exists():
                config.output_dir = str(Path.home() / "Videos" / "slicing-demo")
            factory = lambda: DemoCamera(args.demo_width, args.demo_height, args.demo_fps)
        else:
            factory = lambda: HikCamera(controller.config.camera_serial, controller.config.sdk_path)
        controller = Controller(config, store, factory)
        window = MainWindow(controller, config)
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        return 1
    controller.start()
    window.show()
    # Keep Python signal handling active while the native Qt event loop runs.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    signal.signal(signal.SIGTERM, lambda *_: window.close())
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(200)
    result = app.exec()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
