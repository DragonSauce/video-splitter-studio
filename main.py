"""
main.py - entry point for Video Splitter Studio.

Boot order
----------
1. make `vendor/` importable (deps installed there by fetch_deps.py)
2. make sure a Python runtime with tkinter/Qt can run us
3. import PySide6 - if it is missing, offer to download it right here
4. hand over to main_window.MainWindow

Run it with:  run.bat        (or)  python main.py
"""

from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
FROZEN = bool(getattr(sys, "frozen", False))
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Qt's platform plugin path must be reachable when running from a source tree
_plugins = os.path.join(VENDOR, "PySide6", "plugins")
if os.path.isdir(_plugins):
    os.environ.setdefault("QT_PLUGIN_PATH", _plugins)
    os.environ.setdefault(
        "QT_QPA_PLATFORM_PLUGIN_PATH", os.path.join(_plugins, "platforms"))

if FROZEN:
    # PyInstaller unpacks the Qt runtime next to the executable's _internal
    # folder; video_tools must write ffmpeg/cache outside that temporary tree,
    # so point them at the folder that holds the .exe.
    _base = os.path.dirname(os.path.abspath(sys.executable))
    os.environ.setdefault("VSS_BASE_DIR", _base)


def _fatal(title: str, message: str) -> int:
    """Show the error in a GUI box when possible, otherwise print it."""
    sys.stderr.write(f"{title}\n{message}\n")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass
    return 1


def _install_pyside6_interactive() -> bool:
    """Offer to download PySide6 into ./vendor using fetch_deps.py."""
    import subprocess
    if FROZEN:
        return False                   # a frozen build bundles Qt already
    fetcher = os.path.join(HERE, "fetch_deps.py")
    if not os.path.isfile(fetcher):
        return False
    try:
        import ctypes
        choice = ctypes.windll.user32.MessageBoxW(
            None,
            "缺少界面库 PySide6。\n\n是否现在自动下载到程序目录（约 80 MB）？\n"
            "下载完成后程序会自动启动。",
            "Video Splitter Studio - 首次运行",
            0x04 | 0x40,          # MB_YESNO | MB_ICONINFORMATION
        )
    except Exception:
        choice = 6
    if choice != 6:               # IDYES
        return False
    try:
        subprocess.run([sys.executable, fetcher], cwd=HERE, check=True)
    except Exception:
        return False
    if VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    return os.path.isdir(os.path.join(VENDOR, "PySide6"))


def _selftest() -> int:
    """
    `--selftest`: verify the environment without opening the GUI.

    Everything is written to stdout *and* to selftest.log next to the
    executable, which makes it usable from a double-clicked frozen build that
    has no console of its own.
    """
    import platform
    import traceback

    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    say(f"Video Splitter Studio - selftest")
    say(f"frozen      : {FROZEN}")
    say(f"python      : {sys.version.split()[0]} ({platform.machine()})")
    say(f"base dir    : {os.environ.get('VSS_BASE_DIR', HERE)}")
    ok = True

    try:
        from PySide6.QtCore import qVersion
        say(f"PySide6     : {qVersion()}")
    except Exception as exc:                            # noqa: BLE001
        ok = False
        say(f"PySide6     : FAILED - {exc}")

    try:
        import video_tools as vt
        say(f"bin dir     : {vt.BIN_DIR}")
        say(f"cache dir   : {vt.CACHE_DIR}")
        found = vt.locate_ffmpeg()
        if found:
            say(f"ffmpeg      : {found[0]}")
            say(f"ffprobe     : {found[1]}")
        else:
            say("ffmpeg      : not found (it will be downloaded on first run)")
    except Exception:                                   # noqa: BLE001
        ok = False
        say("video_tools : FAILED")
        say(traceback.format_exc())

    try:
        import video_tools as vt
        from PIL import Image  # noqa: F401
        say("Pillow      : ok")
        try:
            from PySide6.QtMultimedia import QAudioSink  # noqa: F401
            say("QtMultimedia: ok")
        except Exception:                               # noqa: BLE001
            say("QtMultimedia: unavailable (ffplay fallback, or silent preview)")
    except Exception as exc:                            # noqa: BLE001
        ok = False
        say(f"Pillow      : FAILED - {exc}")

    for module in ("ui_common", "player", "main_window"):
        try:
            __import__(module)
            say(f"module {module:<8}: ok")
        except Exception as exc:                        # noqa: BLE001
            ok = False
            say(f"module {module:<8}: FAILED - {exc}")
            say(traceback.format_exc())

    log_path = os.path.join(os.path.dirname(os.path.abspath(sys.executable))
                            if FROZEN else HERE, "selftest.log")
    try:
        import io as _io
        with _io.open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        say(f"log written : {log_path}")
    except Exception as exc:                            # noqa: BLE001
        say(f"log write failed: {exc}")

    say("RESULT      : " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 3


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    if "--version" in sys.argv:
        print("Video Splitter Studio 1.0")
        return 0

    try:
        import PySide6  # noqa: F401
    except ImportError:
        if not _install_pyside6_interactive():
            return _fatal(
                "缺少 PySide6",
                "无法启动：缺少界面库 PySide6。\n\n"
                "请在命令行运行：\n"
                f'  "{sys.executable}" "{os.path.join(HERE, "fetch_deps.py")}"\n\n'
                "或安装到当前 Python：\n"
                "  python -m pip install PySide6-Essentials")

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from ui_common import make_app_icon
        from main_window import MainWindow
    except Exception:
        return _fatal("启动失败", "导入界面模块时出错：\n\n" + traceback.format_exc())

    # Qt 6 always scales for high-DPI displays, so the old attributes are gone.

    app = QApplication(sys.argv)
    app.setApplicationName("Video Splitter Studio")
    app.setApplicationDisplayName("Video Splitter Studio")
    app.setOrganizationName("VideoSplitterStudio")
    app.setWindowIcon(make_app_icon())

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
