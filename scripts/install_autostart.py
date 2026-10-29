"""Install a desktop-login launcher; does not enable passwordless login."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def exec_quote(value: str) -> str:
    # Desktop Entry Exec quoting, not shell quoting; literal % must be doubled.
    if "\n" in value or "\r" in value:
        raise ValueError("启动路径不能包含换行")
    value = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    # String-value escaping is applied before Exec argument unquoting.
    value = value.replace("\\", "\\\\")
    return '"' + value + '"'


def desktop_entry(root: Path, mvs_root: Path, config: Path | None = None) -> str:
    command = ["/usr/bin/env", f"MVS_ROOT={mvs_root}", "/bin/bash", str(root / "scripts" / "run.sh")]
    if config is not None:
        command.extend(["--config", str(config)])
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Slicing Recorder",
        "Comment=Hikrobot USB camera acquisition", "Terminal=false",
        "Exec=" + " ".join(exec_quote(arg) for arg in command),
        "X-GNOME-Autostart-enabled=true", "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="移除本程序的桌面自启动项")
    parser.add_argument("--dry-run", action="store_true", help="仅打印启动项，不写入用户配置")
    parser.add_argument("--mvs-root", type=Path, default=Path("/opt/MVS"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "linux" and not args.dry_run:
        parser.error("桌面自启动安装仅用于 Linux，请在 Ubuntu 工控机执行")
    root = Path(__file__).resolve().parents[1]
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    target = base / "autostart" / "slicing-recorder.desktop"
    if args.remove:
        if args.dry_run:
            print(f"Would remove: {target}")
        else:
            target.unlink(missing_ok=True)
            print(f"已移除：{target}")
        return 0
    content = desktop_entry(root, args.mvs_root.resolve(), args.config.resolve() if args.config else None)
    if args.dry_run:
        print(content)
        return 0
    if not (root / ".venv" / "bin" / "python").is_file():
        parser.error("请先在项目中建立 .venv 并安装 requirements.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"已安装：{target}\n下次登录桌面后自动打开程序；不会自动录制。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
