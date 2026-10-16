"""
layout_probe.py - geometry check for the contact-sheet wall.

The wall must always fit inside its viewport (no scrolling) while keeping the
preview frames as large as possible, for any viewport size and any frame count.
This drives `ThumbnailWall._relayout()` directly with synthetic pixmaps, so it
runs in a second and does not need FFmpeg.

    python layout_probe.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# The scripts live either in the project root or in tests/; resolve the
# project root either way so imports and paths keep working.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize            # noqa: E402
from PySide6.QtGui import QColor, QPixmap    # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import player                                # noqa: E402

VIEWPORTS = [
    (1040, 350, "默认窗口的墙"),
    (1040, 196, "矮墙"),
    (1400, 420, "宽而高"),
    (760, 300, "窄"),
    (1560, 200, "很宽很矮"),
    (420, 180, "很窄很矮"),
    (1000, 700, "很高"),
]
COUNTS = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64)


def main() -> int:
    QApplication.instance() or QApplication([])
    host = QWidget()
    wall = player.ThumbnailWall(host)
    host.show()

    pixmaps = []
    for _ in range(max(COUNTS)):
        pm = QPixmap(320, 180)
        pm.fill(QColor("#123456"))
        pixmaps.append(pm)

    failures = 0
    for width, height, label in VIEWPORTS:
        print(f"\n=== viewport {width}x{height} ({label}) ===")
        host.resize(QSize(width, height))
        wall.setGeometry(0, 0, width, height)
        wall.holder.setGeometry(0, 0, 10, 10)
        QApplication.processEvents()

        for count in COUNTS:
            for tile in wall.tiles:
                wall._grid.removeWidget(tile)
                tile.setParent(None)
                tile.deleteLater()
            wall.tiles = []
            wall._cols = 0
            for index in range(count):
                tile = player.ThumbTile(index, "0:00:00.000", wall.holder)
                tile.set_pixmap(pixmaps[index])
                tile.show()
                wall.tiles.append(tile)
            wall.times = [float(i) for i in range(count)]
            wall._relayout()
            QApplication.processEvents()

            cols = wall._cols
            rows = (count + cols - 1) // cols
            tile = wall.tiles[0]
            # what the grid asks for, versus what the viewport can show
            need_w = cols * tile.width() + (cols - 1) * wall.GAP
            need_h = rows * tile.height() + (rows - 1) * wall.GAP
            vw = wall.area.viewport().width()
            vh = wall.area.viewport().height()
            over_w, over_h = need_w > vw + 1, need_h > vh + 1
            if over_w or over_h:
                failures += 1
            cover = (need_w * need_h) / max(1, vw * vh) * 100.0
            caption = "caption" if tile.height() >= player.ThumbTile.MIN_LEGIBLE_HEIGHT \
                else "no-caption"
            print(f"  n={count:3d} -> {cols:2d}x{rows:<2d} tile={tile.width():3d}x"
                  f"{tile.height():3d} need={need_w:4d}x{need_h:4d} "
                  f"viewport={vw}x{vh} fill={cover:5.1f}% {caption:10s} "
                  f"{'OVERFLOW' if over_w or over_h else 'OK'}"
                  f"{' width' if over_w else ''}{' height' if over_h else ''}")

    wall.hide()
    print(f"\noverflow cases: {failures}")
    print("RESULT:", "PASS" if failures == 0 else "FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
