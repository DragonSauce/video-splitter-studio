"""Temporary check: every dialog kind builds and no @placeholder survives."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication    # noqa: E402

import ui_common                              # noqa: E402

app = QApplication.instance() or QApplication([])

bad = 0
for kind in ("info", "question", "warning", "critical", "ok"):
    dlg = ui_common.MessageDialog(None, "title " + kind, "line 1\nline 2", kind,
                                  [("close", "reject"), ("ok", "accept")])
    sheet = dlg.styleSheet()
    left = [tok for tok in ("@accent", "@icon_bg", "@accent_hi") if tok in sheet]
    if left:
        bad += 1
    print(f"  {kind:9s} ok | leftover placeholders: {left or 'none'}")

print("  HoverNote accent replaced:",
      "#9b83ff" in ui_common._qss(ui_common.HOVER_QSS, accent=ui_common.ACCENT_HI))
print("  all dialog kinds construct:", bad == 0)
print("RESULT:", "PASS" if bad == 0 else "FAIL")
raise SystemExit(0 if bad == 0 else 1)
