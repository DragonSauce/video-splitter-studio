"""Temporary check: queue right-click menu wiring and right-click selection."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["VSS_NO_DIALOG"] = "1"

from PySide6.QtCore import QEvent, QPointF, QSize, Qt   # noqa: E402
from PySide6.QtGui import QMouseEvent                    # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

import main_window                                       # noqa: E402

app = QApplication.instance() or QApplication([])
win = main_window.MainWindow()
win.resize(1640, 1000)
win.show()
for _ in range(45):
    app.processEvents()

print("右键菜单策略 CustomContextMenu:",
      win.list.contextMenuPolicy() == Qt.CustomContextMenu)

sample = os.path.join(ROOT, "testmedia", "sample.mp4")
second = os.path.join(ROOT, "testmedia", "sample2.mp4")
win.items = []
win.list.clear()
for path in (sample, second):
    item = main_window.QueueItem(path=path)
    win.items.append(item)
    row = QListWidgetItem()
    row.setData(Qt.UserRole, item)
    row.setSizeHint(QSize(0, main_window.QueueDelegate.ROW_HEIGHT))
    win.list.addItem(row)
app.processEvents()

# --- right-click selects the row under the cursor -------------------------- #
rect = win.list.visualItemRect(win.list.item(1))
pt = rect.center()
press = QMouseEvent(QEvent.MouseButtonPress, QPointF(pt), QPointF(pt),
                    Qt.RightButton, Qt.RightButton, Qt.NoModifier)
win.eventFilter(win.list, press)
app.processEvents()
rows = [i.row() for i in win.list.selectedIndexes()]
print(f"右键点第 2 行 -> 选中 {rows} | currentRow {win.list.currentRow()} "
      f"{'OK' if rows == [1] else 'FAIL'}")

# --- the menu itself (built, not shown) ------------------------------------ #
win.current = win.items[1]
menu = win._build_queue_menu()
entries = ["<分隔线>" if a.isSeparator() else
           f"{a.text()}{'' if a.isEnabled() else '  [禁用]'}" for a in menu.actions()]
print("右键菜单内容:")
for entry in entries:
    print("   ", entry)

text = " | ".join(entries)
checks = {
    "添加视频": "添加视频" in text,
    "从队列移除": "从队列移除" in text,
    "上移/下移": "上移" in text and "下移" in text,
    "清空队列": "清空队列" in text,
    "打开源文件夹": "打开源文件所在文件夹" in text,
}
for name, ok in checks.items():
    print(f"   含 {name}: {ok}")

# --- empty-space right click must leave the selection alone ---------------- #
win.list.setCurrentRow(0)
app.processEvents()
below = QPointF(10.0, float(win.list.viewport().height() - 4))
press2 = QMouseEvent(QEvent.MouseButtonPress, below, below,
                     Qt.RightButton, Qt.RightButton, Qt.NoModifier)
win.eventFilter(win.list, press2)
app.processEvents()
print("空白处右键后 currentRow:", win.list.currentRow(), "(应仍为 0)")

# --- remove / clear actually work ------------------------------------------ #
win.list.setCurrentRow(1)
app.processEvents()
win.remove_selected()
print(f"移除一项后: items={len(win.items)} list={win.list.count()}")
win.clear_queue()
print(f"清空后: items={len(win.items)} list={win.list.count()}")

print("RESULT:", "PASS" if all(checks.values()) and rows == [1] else "FAIL")
win.player.shutdown()
raise SystemExit(0)
