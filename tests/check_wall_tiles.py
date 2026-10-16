"""Temporary check: does the wall keep every tile it was given?"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap            # noqa: E402
from PySide6.QtWidgets import QApplication           # noqa: E402

import player                                        # noqa: E402

app = QApplication.instance() or QApplication([])

host = player.ThumbnailWall()
host.resize(556, 300)
host.show()
app.processEvents()

pm = QPixmap(320, 180)
pm.fill(QColor("#123456"))

for count in (6, 12, 24, 64):
    host.show_job(player.ThumbJob(frames=[pm] * count,
                                  times=[float(i) for i in range(count)],
                                  sheet=None, columns=4))
    for _ in range(6):
        app.processEvents()
    real = len(host.tiles)
    print(f"  frames={count:3d} -> tiles={real:3d} cols={host._cols:2d} "
          f"viewport={host.area.viewport().width()}x{host.area.viewport().height()} "
          f"{'OK' if real == count else 'LOST TILES'}")

host.hide()
print("done")
raise SystemExit(0)
