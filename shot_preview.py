"""
shot_preview.py - render the CURRENT UI with real content into ./shots.

    python shot_preview.py [width height]

Loads testmedia/sample.mp4 (creating it if needed), fills the queue, the video
player, the contact sheet, the segment table and the log, then writes a full
window screenshot plus cropped panels. Used to review layout changes.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The scripts live either in the project root or in tests/; resolve the
# project root either way so imports and paths keep working.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.pop("QT_QPA_PLATFORM", None)      # a real window makes the sizes exact

import video_tools as vt                      # noqa: E402
from PySide6.QtCore import Qt                 # noqa: E402
from PySide6.QtGui import QPixmap              # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402
import main_window                            # noqa: E402
import player                                 # noqa: E402

SHOTS = os.path.join(HERE, "shots")


def main() -> int:
    size = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (1640, 1000)

    sample = os.path.join(HERE, "testmedia", "sample.mp4")
    if not os.path.isfile(sample):
        found = vt.locate_ffmpeg() or vt.download_ffmpeg()
        os.makedirs(os.path.dirname(sample), exist_ok=True)
        vt.run_hidden([
            found[0], "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=60",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", sample,
        ], timeout=600)
    media = vt.probe(sample)

    app = QApplication(sys.argv)
    win = main_window.MainWindow()
    win.resize(*size)
    win.show()
    app.processEvents()

    win._ffmpeg_ready = True
    win._set_ui_enabled(True)
    win.add_paths([sample])
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if win.current is not None and win.current.info is not None and win.wall.tiles:
            break
        time.sleep(0.1)
    for _ in range(25):
        app.processEvents()
        time.sleep(0.03)

    win._log("FFmpeg 就绪，队列已读取完成")
    win._log("预览图已生成，可在「视频合集预览图」里调整张数与间隔")
    win._log("拆分方式：按目标大小 100 MB；输出到源文件旁的新文件夹")
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)

    os.makedirs(SHOTS, exist_ok=True)
    win.grab().save(os.path.join(SHOTS, "ui_full.png"))
    for name, widget in (("panel_queue", win.list.parentWidget()),
                         ("panel_player", win.player),
                         ("panel_wall", win.wall),
                         ("panel_settings", win._settings_split)):
        widget.grab().save(os.path.join(SHOTS, f"{name}.png"))

    print(f"window      : {win.width()}x{win.height()}")
    print(f"queue       : {win.list.parentWidget().width()} px wide")
    print(f"preview col : {win._preview_split.width()} px")
    print(f"player      : {win.player.width()}x{win.player.height()}")
    print(f"wall        : {win.wall.width()}x{win.wall.height()} "
          f"viewport={win.wall.area.viewport().width()}x{win.wall.area.viewport().height()}")
    print(f"log         : {win.log.width()}x{win.log.height()} (max {win.log.maximumHeight()})")
    print(f"settings col: {win._settings_split.width()} px "
          f"sizes={win._settings_split.sizes()}")
    print(f"tiles       : {len(win.wall.tiles)} in {win.wall._cols} columns")
    print("screenshots :", SHOTS)

    win.player.stop()
    win.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
