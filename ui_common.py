"""
ui_common.py - shared Qt bits with no dependency on the rest of the app.

Kept separate so `player.py` and `main_window.py` can both use them without
importing each other.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QIcon, QLinearGradient, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QSlider, QToolButton, QVBoxLayout, QWidget,
)

ACCENT = "#7c5cff"
ACCENT_HI = "#9b83ff"
INK = "#e8eefc"

# --------------------------------------------------------------------------- #
# Big, high-contrast dialog skin
#
# The stock QMessageBox on this dark theme came out small and washed out (the
# information text sat on a light rectangle, the buttons were tiny). Everything
# here is one shared stylesheet plus a hand-built dialog so every popup in the
# app looks the same, reads at 14 px, and can be dismissed from the keyboard.
# --------------------------------------------------------------------------- #
DIALOG_QSS = """
QDialog#VssDialog { background:#0b1a2b; }
QLabel#VssTitle {
  color:#ffffff; font-size:17px; font-weight:700; background:transparent;
}
QLabel#VssBody {
  color:#e8f1ff; font-size:14px; background:transparent; line-height:170%;
}
QLabel#VssIcon {
  color:#ffffff; font-size:26px; font-weight:700;
  background:@icon_bg; border-radius:24px;
}
QPushButton#VssBtn {
  background:#1b3252; border:1px solid #35608f; border-radius:8px;
  color:#ffffff; font-size:14px; font-weight:600; padding:8px 20px; min-width:96px;
  min-height:22px;
}
QPushButton#VssBtn:hover { background:#26456b; border-color:#63b3ed; }
QPushButton#VssBtn:pressed { background:#152840; }
QPushButton#VssBtn:focus { border:2px solid #9b83ff; }
QPushButton#VssOk {
  background:@accent; border:1px solid @accent_hi; border-radius:8px;
  color:#ffffff; font-size:14px; font-weight:700; padding:8px 26px; min-width:96px;
  min-height:22px;
}
QPushButton#VssOk:hover { background:@accent_hi; }
QPushButton#VssOk:pressed { background:#6547d8; }
QPushButton#VssOk:focus { border:2px solid #ffffff; }
QFrame#VssSep { background:#1d3350; max-height:1px; border:0; }
"""


def _qss(template: str, **colors: str) -> str:
    """
    Fill a stylesheet's `@name` placeholders.

    Two rules learned the hard way:
      * plain `str.replace`, not `template % {...}` - these sheets are CSS and
        CSS uses `%` (`line-height:170%`), so `%`-formatting raises
        "unsupported format character" and takes the whole dialog down;
      * longest name first, otherwise `@accent` eats the `@accent_hi` prefix
        and leaves a broken `#7c5cff_hi` colour behind.
    """
    out = template
    for key in sorted(colors, key=len, reverse=True):
        out = out.replace(f"@{key}", colors[key])
    return out


ICON_STYLE = {
    "info": ("i", "#2f7de1"),
    "question": ("?", "#c9a227"),
    "warning": ("!", "#d9822b"),
    "critical": ("✕", "#c0392b"),
    "ok": ("✓", "#2f9e6f"),
}


def _body_html(text: str) -> str:
    """Keep authored line breaks and make paths readable in the body label."""
    escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return escaped.replace("\n", "<br>")


class MessageDialog(QDialog):
    """
    Modal popup with a big icon, 14 px body text and full-width, obvious buttons.

    `buttons` is a list of (label, role); the role is what `choice()` returns,
    and the button whose role is "accept" is painted in the accent colour and
    made the default, so Enter confirms and Esc cancels. Any other role simply
    closes the dialog with that role, which lets callers offer a third action
    (e.g. "打开文件夹" / "打开图片" / "关闭").
    """

    def __init__(self, parent: QWidget | None, title: str, text: str,
                 kind: str = "info",
                 buttons: list[tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("VssDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setStyleSheet(_qss(
            DIALOG_QSS,
            accent=ACCENT,
            accent_hi=ACCENT_HI,
            icon_bg=ICON_STYLE.get(kind, ICON_STYLE["info"])[1],
        ))
        self._choice = "reject"

        buttons = buttons or [("确定", "accept")]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)
        glyph, _colour = ICON_STYLE.get(kind, ICON_STYLE["info"])
        badge = QLabel(glyph)
        badge.setObjectName("VssIcon")
        badge.setFixedSize(48, 48)
        badge.setAlignment(Qt.AlignCenter)
        row.addWidget(badge, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(8)
        head = QLabel(title)
        head.setObjectName("VssTitle")
        head.setWordWrap(True)
        body = QLabel(_body_html(text))
        body.setObjectName("VssBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setMinimumWidth(360)
        text_box.addWidget(head)
        text_box.addWidget(body)
        row.addLayout(text_box, 1)
        outer.addLayout(row)

        sep = QFrame()
        sep.setObjectName("VssSep")
        sep.setFrameShape(QFrame.HLine)
        outer.addWidget(sep)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addStretch(1)
        for label, role in buttons:
            btn = QPushButton(label)
            btn.setObjectName("VssOk" if role == "accept" else "VssBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setAutoDefault(role == "accept")
            btn.setDefault(role == "accept")
            btn.clicked.connect(lambda _=False, r=role: self._pick(r))
            bar.addWidget(btn)
        outer.addLayout(bar)

        self.setMinimumWidth(470)
        if parent is not None:
            self.adjustSize()

    def _pick(self, role: str) -> None:
        self._choice = role
        if role in ("accept", "reject"):
            self.accept() if role == "accept" else self.reject()
        else:
            self.done(0)

    def choice(self) -> str:
        return self._choice


# --------------------------------------------------------------------------- #
# Unattended mode
#
# Self-check scripts (smoke_test.py, verify_app.py) drive the real window with
# nobody at the keyboard. A modal popup there does not "show and wait" - it
# hangs the run for good. VSS_NO_DIALOG=1 makes every popup a no-op that
# returns the confirm role, so those runs can cover the real code paths.
# --------------------------------------------------------------------------- #
import os as _os

QUIET = bool(_os.environ.get("VSS_NO_DIALOG"))


def set_quiet(quiet: bool) -> None:
    """Turn popups off (or back on) at runtime."""
    global QUIET
    QUIET = bool(quiet)


def message_box(parent: QWidget | None, title: str, text: str,
                kind: str = "info",
                buttons: list[tuple[str, str]] | None = None) -> str:
    """
    Show the shared dialog and return the chosen role ("accept"/"reject").

    When `set_quiet(True)` is active (the VSS_NO_DIALOG env flag, set by the
    automated self-checks) nothing is shown and the accept role is returned.
    A modal dialog in an unattended run would block forever on a machine that
    cannot click it - that is exactly how the smoke test hung once.
    """
    buttons = buttons or [("确定", "accept")]
    if QUIET:
        for _label, role in buttons:
            if role in ("accept", "folder"):
                return role
        return buttons[-1][1]
    dialog = MessageDialog(parent, title, text, kind, buttons)
    dialog.exec()
    return dialog.choice()


def ask_yes_no(parent: QWidget | None, title: str, text: str,
               kind: str = "question", yes: str = "确定", no: str = "取消") -> bool:
    return message_box(parent, title, text, kind,
                       [(no, "reject"), (yes, "accept")]) == "accept"

def notify(parent: QWidget | None, title: str, text: str,
           kind: str = "info") -> None:
    message_box(parent, title, text, kind, [("好的", "accept")])


# --------------------------------------------------------------------------- #
# Hover notes: a deliberately loud replacement for the tiny native tooltip.
# --------------------------------------------------------------------------- #
HOVER_QSS = """
QFrame#HoverNote {
  background:#0d2137; border:2px solid @accent; border-radius:9px;
}
QLabel#HoverText { color:#ffffff; font-size:13px; font-weight:600; background:transparent; }
"""


class HoverNote(QFrame):
    """Frameless, high-contrast bubble that explains what a small button does."""

    _instance: "HoverNote | None" = None
    _timer: QTimer | None = None

    def __init__(self) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setObjectName("HoverNote")
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(_qss(HOVER_QSS, accent=ACCENT_HI))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        self.label = QLabel("")
        self.label.setObjectName("HoverText")
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(220)
        self.label.setMaximumWidth(430)
        lay.addWidget(self.label)

    @classmethod
    def _bubble(cls) -> "HoverNote":
        if cls._instance is None:
            cls._instance = HoverNote()
        if cls._timer is None:
            cls._timer = QTimer()
            cls._timer.setSingleShot(True)
            cls._timer.timeout.connect(HoverNote.dismiss)
        return cls._instance

    @classmethod
    def show_for(cls, widget: QWidget, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        bubble = cls._bubble()
        bubble.label.setText(text)
        bubble.adjustSize()
        pos = widget.mapToGlobal(QPoint(0, widget.height() + 8))
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            if pos.x() + bubble.width() > area.right():
                pos.setX(max(area.left(), area.right() - bubble.width() - 4))
            if pos.y() + bubble.height() > area.bottom():
                above = widget.mapToGlobal(QPoint(0, -bubble.height() - 8))
                if above.y() >= area.top():
                    pos.setY(above.y())
                else:
                    pos.setY(max(area.top(), area.bottom() - bubble.height() - 4))
        bubble.move(pos)
        bubble.show()
        bubble.raise_()
        cls._timer.start(9000)

    @classmethod
    def dismiss(cls) -> None:
        if cls._instance is not None:
            cls._instance.hide()
        if cls._timer is not None:
            cls._timer.stop()


class HoverNoteFilter(QObject):
    """
    Drop-in event filter: hover a widget with a tooltip -> loud bubble appears.

    The native tooltip is swallowed so only one explanation is ever on screen.
    """

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Enter:
            text = obj.toolTip() if hasattr(obj, "toolTip") else ""
            if text:
                HoverNote.show_for(obj, text)
        elif kind in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Hide, QEvent.FocusOut):
            HoverNote.dismiss()
        elif kind == QEvent.ToolTip:
            return True
        return False


_FILTER = HoverNoteFilter()


def loud_tooltip(widget: QWidget, text: str | None = None) -> QWidget:
    """Give a widget the loud hover note (its current tooltip text by default)."""
    if text is not None:
        widget.setToolTip(text)
    if not widget.toolTip():
        return widget
    widget.installEventFilter(_FILTER)
    widget.setAttribute(Qt.WA_Hover, True)
    return widget


def paint_film_scissors(painter: QPainter, rect: QRect, accent: str = ACCENT) -> None:
    """Draw the app mark: a film frame with scissors cutting through it."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    side = min(rect.width(), rect.height())
    x = rect.x() + (rect.width() - side) / 2
    y = rect.y() + (rect.height() - side) / 2

    film = QRect(int(x + side * 0.06), int(y + side * 0.16),
                 int(side * 0.72), int(side * 0.68))
    grad = QLinearGradient(film.topLeft(), film.bottomRight())
    grad.setColorAt(0.0, QColor("#123a63"))
    grad.setColorAt(1.0, QColor("#0a1b2e"))
    painter.setBrush(grad)
    painter.setPen(QPen(QColor("#8fb6e8"), max(1.0, side * 0.018)))
    painter.drawRoundedRect(film, side * 0.05, side * 0.05)

    painter.setBrush(QColor("#cfe4ff"))
    painter.setPen(Qt.NoPen)
    hole_w, hole_h = side * 0.055, side * 0.075
    for i in range(4):
        hy = film.top() + film.height() * (0.09 + i * 0.24)
        painter.drawRoundedRect(int(film.left() + side * 0.05), int(hy),
                                int(hole_w), int(hole_h), 2, 2)
        painter.drawRoundedRect(int(film.right() - side * 0.05 - hole_w), int(hy),
                                int(hole_w), int(hole_h), 2, 2)

    painter.setBrush(QColor(accent))
    painter.setPen(QPen(QColor(accent), max(1.2, side * 0.02)))
    blade = side * 0.62
    cx, cy = x + side * 0.72, y + side * 0.40
    painter.drawLine(int(cx - blade * 0.55), int(cy + blade * 0.62),
                     int(cx + blade * 0.30), int(cy - blade * 0.30))
    painter.drawLine(int(cx - blade * 0.55), int(cy - blade * 0.05),
                     int(cx + blade * 0.30), int(cy + blade * 0.32))

    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor("#2f7de1"), max(1.2, side * 0.028)))
    painter.drawEllipse(QPoint(int(x + side * 0.80), int(y + side * 0.74)),
                        int(side * 0.10), int(side * 0.10))
    painter.drawEllipse(QPoint(int(x + side * 0.94), int(y + side * 0.66)),
                        int(side * 0.09), int(side * 0.09))
    painter.restore()


def make_app_icon(size: int = 128) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#0d47a1"))
    grad.setColorAt(1.0, QColor("#0b2748"))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, size, size, size * 0.18, size * 0.18)
    paint_film_scissors(p, QRect(0, 0, size, size))
    p.end()
    return QIcon(pm)


class ToolBtn(QToolButton):
    """Flat transport / toolbar button with a loud hover explanation."""

    def __init__(self, glyph: str, tooltip: str, checkable: bool = False,
                 parent: QWidget | None = None, width: int = 34) -> None:
        super().__init__(parent)
        self.setText(glyph)
        self.setToolTip(tooltip)
        self.setCheckable(checkable)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(width, 30)
        self.setStyleSheet(
            "QToolButton{color:%s;background:rgba(255,255,255,0.06);"
            "border:1px solid rgba(255,255,255,0.10);border-radius:7px;font-size:14px;}"
            "QToolButton:hover{background:rgba(124,92,255,0.35);border-color:%s;}"
            "QToolButton:checked{background:%s;border-color:%s;}"
            "QToolButton:disabled{color:#63708a;background:rgba(255,255,255,0.03);}"
            % (INK, ACCENT_HI, ACCENT, ACCENT_HI)
        )
        loud_tooltip(self)


class TimeSlider(QSlider):
    """Horizontal slider that jumps straight to the clicked position."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 1000)
        self.setCursor(Qt.PointingHandCursor)

    def _value_from_x(self, x: int) -> int:
        span = max(1, self.width())
        ratio = min(1.0, max(0.0, x / span))
        return int(self.minimum() + ratio * (self.maximum() - self.minimum()))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.setValue(self._value_from_x(int(event.position().x())))
            self.sliderMoved.emit(self.value())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton:
            self.setValue(self._value_from_x(int(event.position().x())))
            self.sliderMoved.emit(self.value())
            event.accept()
            return
        super().mouseMoveEvent(event)


class ElidedLabel(QLabel):
    """
    One-line label that elides instead of forcing the layout wider.

    A plain QLabel reports its full text as its size hint, so a long sample
    path inside a narrow panel makes Qt stretch (or overlap) everything around
    it. This keeps whatever width the layout gives it and elides the middle,
    with the full text in the tooltip.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._full)

    def full_text(self) -> str:
        return self._full

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(self._full, Qt.ElideMiddle, max(10, self.width()))
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(), int(self.alignment()), elided)
        painter.end()

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = self.fontMetrics()
        return QSize(min(360, metrics.horizontalAdvance(self._full)),
                     metrics.height() + 2)


class DropZoneHint(QWidget):
    """Dashed 'drop files here' hint."""

    pathsDropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, text: str = "") -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setCursor(Qt.PointingHandCursor)
        self._hot = False
        self._text = text or "拖入视频文件\n或点击这里选择文件"

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hot = True
            self.update()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._hot = False
        self.update()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._hot = False
        self.update()
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.pathsDropped.emit(paths)
        event.acceptProposedAction()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        pen = QPen(QColor("#63b3ed" if self._hot else "#3f6d9e"), 2, Qt.DashLine)
        pen.setDashPattern([6, 4])
        painter.setPen(pen)
        painter.setBrush(QColor("#0b2340" if not self._hot else "#123f70"))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(QColor("#dbe9ff"))
        f = QFont(self.font())
        f.setPointSize(12)
        painter.setFont(f)
        painter.drawText(rect, Qt.AlignCenter, self._text)
        painter.end()
