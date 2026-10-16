"""
player.py - video preview / playback widgets.

Widgets
-------
`VideoSurface`   : letterboxed frame display
`TransportBar`   : (inside PlayerPanel) timeline + buttons
`ThumbnailWall`  : contact sheet of extracted preview frames
`PlayerPanel`    : ties it together and drives the FFmpeg decode threads

Decoding model
--------------
Two FFmpeg pipes read the same file:

  video -> rawvideo rgb24 -> bounded queue -> presented on a 60 Hz UI timer
  audio -> s16le PCM       -> ring buffer      -> QAudioSink (the device pulls)

The playback clock comes from the audio device's `processedUSecs()` plus the
media offset the current audio pipe was started at, so A/V stay together
without re-encoding. Files without audio (or without QtMultimedia) fall back to
a wall clock limited by how far video decoding has progressed.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import (
    QElapsedTimer, QPoint, QRect, Qt, QTimer, Signal, QObject,
)
from PySide6.QtGui import (
    QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedLayout, QVBoxLayout,
    QWidget,
)

import video_tools as vt
from ui_common import (
    ACCENT, ACCENT_HI, INK, TimeSlider, ToolBtn,
    make_app_icon, paint_film_scissors,
)

try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
    HAVE_AUDIO = True
except Exception:                                     # pragma: no cover
    QAudioFormat = QAudioSink = QMediaDevices = None  # type: ignore
    HAVE_AUDIO = False


# PCM format used for the audio preview pipe
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BYTES_PER_SEC = AUDIO_RATE * AUDIO_CHANNELS * 2


# --------------------------------------------------------------------------- #
# Splash / idle state
# --------------------------------------------------------------------------- #

class SplashArt(QWidget):
    """Blue radial-burst splash, drawn entirely with QPainter."""

    def __init__(self, caption: str = "— The fastest video splitter based on keyframe",
                 title: str = "Video Splitter Studio", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.caption = caption
        self.title = title
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = max(1, self.width()), max(1, self.height())
        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor("#0a5bd8"))
        grad.setColorAt(0.45, QColor("#0a3f9e"))
        grad.setColorAt(1.0, QColor("#04214f"))
        p.fillRect(self.rect(), grad)

        center = QPoint(w // 2, int(h * 0.34))
        radius = int(max(w, h) * 1.15)
        p.save()
        p.translate(center)
        rays = 44
        for i in range(rays):
            p.save()
            p.rotate(i * (360.0 / rays))
            p.setBrush(QColor(255, 255, 255, 26 if i % 2 else 12))
            p.setPen(Qt.NoPen)
            path = QPainterPath()
            path.moveTo(0, 0)
            half = math.radians((360.0 / rays) / 2)
            path.lineTo(radius * math.cos(-half), radius * math.sin(-half))
            path.lineTo(radius * math.cos(half), radius * math.sin(half))
            path.closeSubpath()
            p.drawPath(path)
            p.restore()
        p.restore()

        wave = QPainterPath()
        wave.moveTo(0, h * 0.78)
        wave.cubicTo(w * 0.25, h * 0.66, w * 0.45, h * 0.90, w * 0.70, h * 0.78)
        wave.cubicTo(w * 0.85, h * 0.71, w * 0.95, h * 0.80, w, h * 0.76)
        wave.lineTo(w, h)
        wave.lineTo(0, h)
        wave.closeSubpath()
        wg = QLinearGradient(0, h * 0.70, 0, h)
        wg.setColorAt(0.0, QColor(120, 235, 220, 95))
        wg.setColorAt(1.0, QColor(20, 60, 120, 165))
        p.setPen(Qt.NoPen)
        p.setBrush(wg)
        p.drawPath(wave)

        mark = min(96, int(h * 0.20))
        my = int(h * 0.46)
        paint_film_scissors(p, QRect(int(w * 0.22), my, mark, mark))
        f = QFont(self.font())
        f.setPointSize(max(15, int(h * 0.045)))
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRect(int(w * 0.22) + mark + 14, my, w, mark),
                   Qt.AlignVCenter | Qt.AlignLeft, self.title)

        f2 = QFont(self.font())
        f2.setPointSize(max(9, int(h * 0.023)))
        p.setFont(f2)
        p.setPen(QColor("#d6e8ff"))
        p.drawText(QRect(0, my + mark + 8, w, 28), Qt.AlignHCenter, self.caption)
        p.end()


class EmptyState(QWidget):
    """Idle splash shown before a file is selected."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.art = SplashArt(parent=self)
        self.hint = QLabel("拖入视频文件，或点击上方「添加视频」开始", self)
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(
            "color:#eaf4ff;background:rgba(4,10,20,0.35);border-radius:6px;padding:6px 10px;")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.art.setGeometry(0, 0, self.width(), self.height())
        self.art.lower()
        self.hint.adjustSize()
        self.hint.move(max(0, (self.width() - self.hint.width()) // 2),
                       max(0, self.height() - self.hint.height() - 8))


# --------------------------------------------------------------------------- #
# Thumbnail wall
# --------------------------------------------------------------------------- #

@dataclass
class ThumbJob:
    frames: list[str]
    times: list[float]
    sheet: str | None
    columns: int


class ThumbTile(QFrame):
    THUMB_HEIGHT = 82
    CAPTION_HEIGHT = 20         # the timestamp strip under the picture
    # Below this total height the timestamp strip is dropped (see `set_compact`)
    # and below ~78 px the wall stops producing useful thumbnails at all, so the
    # layout scorer treats it as a hard floor.
    MIN_LEGIBLE_HEIGHT = 78

    # Named `frameClicked` rather than `clicked`: QFrame already owns a
    # `clicked` member and a Signal with the same name would shadow it.
    frameClicked = Signal(int)          # left click -> jump to this frame's time
    contextRequested = Signal(int)      # right click -> menu (save this frame)

    def __init__(self, index: int, time_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.setObjectName("ThumbTile")
        self.setStyleSheet(
            "#ThumbTile{background:#0d1a2b;border:1px solid #1d3350;border-radius:8px;}"
            "#ThumbTile:hover{border-color:#7c5cff;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)
        self.image = QLabel("…")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumHeight(42)
        self.image.setStyleSheet("background:#08111d;border-radius:5px;color:#4d6a8f;")
        self.caption = QLabel(f"#{index + 1:02d}   {time_text}")
        self.caption.setStyleSheet("color:#a9c8ea;font-size:10px;background:transparent;")
        lay.addWidget(self.image, 1)
        lay.addWidget(self.caption, 0)
        self._pm: QPixmap | None = None
        self._pm_path: str | None = None

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            self.contextRequested.emit(self.index)
            return
        if event.button() == Qt.LeftButton:
            self.frameClicked.emit(self.index)
        super().mouseReleaseEvent(event)

    def set_compact(self, compact: bool) -> None:
        """Hide the timestamp caption when the wall is short on space."""
        if compact:
            self.caption.setVisible(False)
        else:
            self.caption.setVisible(True)

    def set_pixmap(self, pm: QPixmap) -> None:
        self._pm = pm
        self._rescale()

    def _rescale(self) -> None:
        if self._pm is None or self._pm.isNull():
            return
        area = self.image.size()
        if area.width() < 8 or area.height() < 8:
            return
        self.image.setPixmap(self._pm.scaled(area, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


class ThumbnailWall(QWidget):
    """Scrollable grid of preview frames ("视频合集预览图")."""

    exportRequested = Signal()
    regenerateRequested = Signal()
    saveOneRequested = Signal(int)          # index of a single tile to save
    tileClicked = Signal(float)
    captureRequested = Signal()             # save every frame as <视频名>_001.png

    TILE_MIN = 140
    TILE_MAX = 340
    GAP = 6
    # Short walls are normal here: the sheet scales its frames down rather than
    # scrolling, so the minimum only has to keep the tiles recognisable.
    AREA_MIN_HEIGHT = 150
    MAX_ROWS = 3
    TILE_MIN_HEIGHT = 78

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # Two rows on purpose: title + readouts + the three small action buttons
        # on the first, the capture parameters on the second. Cramming all of it
        # into one row squeezed the title into the count label and pushed the
        # buttons onto the panel border.
        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        head.setSpacing(8)
        title = QLabel("合集预览图")
        title.setStyleSheet("color:#dce8ff;font-size:12px;font-weight:600;background:transparent;")
        title.setToolTip("视频合集预览图：把视频按间隔抽帧，铺成一张缩略图墙")
        self.info = QLabel("选择视频后自动生成")
        self.info.setStyleSheet("color:#7f9dc0;font-size:11px;background:transparent;")
        # "截取次数" readout, kept next to the frame count so how many shots were
        # taken (and how big the sheet is) is visible at a glance
        self.lbl_captures = QLabel("截取 0 张")
        self.lbl_captures.setStyleSheet(
            "color:#c9b6ff;background:rgba(124,92,255,0.16);border:1px solid #4b3a91;"
            "border-radius:6px;padding:1px 7px;font-size:11px;")

        self.lbl_count = QLabel("张数")
        self.lbl_count.setStyleSheet("color:#9dbde0;font-size:11px;background:transparent;")
        self.sp_count = QSpinBox()
        self.sp_count.setRange(1, 64)
        self.sp_count.setValue(12)
        self.sp_count.setToolTip(
            "要截取的预览图张数。张数越多，合成的合集预览图越大。")
        # Wide enough for "64" plus the up/down buttons: at 56 px the buttons
        # covered the value entirely.
        self.sp_count.setFixedWidth(72)

        self.lbl_step = QLabel("间隔")
        self.lbl_step.setStyleSheet("color:#9dbde0;font-size:11px;background:transparent;")
        self.sp_step = QDoubleSpinBox()
        self.sp_step.setRange(0.0, 86400.0)
        self.sp_step.setValue(0.0)
        self.sp_step.setDecimals(1)
        self.sp_step.setSuffix(" 秒")
        self.sp_step.setToolTip(
            "0 = 自动：在整段视频上从头到尾均匀取样「张数」张。\n"
            "大于 0 = 从 0 秒开始每隔这么多秒截一张，直到视频结尾，"
            "最多不超过「张数」张。")
        self.sp_step.setFixedWidth(92)

        self.lbl_fmt = QLabel("格式")
        self.lbl_fmt.setStyleSheet("color:#9dbde0;font-size:11px;background:transparent;")
        self.cmb_format = QComboBox()
        self.cmb_format.addItems(["PNG（无损）", "JPG（体积小）"])
        self.cmb_format.setToolTip("导出合集预览图的格式")
        self.cmb_format.setFixedWidth(120)

        self.sp_quality = QSpinBox()
        self.sp_quality.setRange(40, 100)
        self.sp_quality.setValue(92)
        self.sp_quality.setToolTip("JPG 质量")
        self.sp_quality.setFixedWidth(72)
        self.sp_quality.setVisible(False)

        self.btn_grid = ToolBtn("⟳", "按当前设置重新生成预览图")
        self.btn_grid.clicked.connect(self.regenerateRequested.emit)
        self.btn_export = ToolBtn("⤓", "把整张合集预览图导出为一张图片")
        self.btn_export.clicked.connect(self.exportRequested.emit)
        self.btn_capture = ToolBtn("📷", "把这一批预览图存成图片文件\n"
                                         "命名：视频文件名_缩略图_001.png\n"
                                         "位置：默认直接放在视频旁（不建文件夹），"
                                         "可在输出设置里改成收进「视频名」文件夹")
        self.btn_capture.clicked.connect(self.captureRequested.emit)

        head.addWidget(title)
        head.addWidget(self.info)
        head.addWidget(self.lbl_captures)
        head.addStretch(1)
        head.addWidget(self.btn_grid)
        head.addWidget(self.btn_export)
        head.addWidget(self.btn_capture)
        outer.addLayout(head)

        params = QHBoxLayout()
        params.setContentsMargins(2, 0, 2, 0)
        params.setSpacing(8)
        for widget in (self.lbl_count, self.sp_count, self.lbl_step, self.sp_step,
                       self.lbl_fmt, self.cmb_format, self.sp_quality):
            params.addWidget(widget)
        params.addStretch(1)
        outer.addLayout(params)

        self.cmb_format.currentIndexChanged.connect(self._on_format_changed)
        self.sp_count.valueChanged.connect(lambda _v: self.regenerateRequested.emit())

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setMinimumHeight(self.AREA_MIN_HEIGHT)
        # The sheet is laid out by hand to fit exactly, so scrolling is off for
        # both axes. That also avoids a resize feedback loop (scrollbar appears
        # -> viewport shrinks -> relayout -> scrollbar disappears ...).
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.area.setStyleSheet("QScrollArea{background:transparent;}")
        self.holder = QWidget()
        self.holder.setStyleSheet("background:transparent;")
        self._grid = QGridLayout(self.holder)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(self.GAP)
        self.area.setWidget(self.holder)
        outer.addWidget(self.area, 1)

        # timeline scrubber sits under the grid so it cannot overlap the tiles
        self.bar = QSlider(Qt.Horizontal)
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(12)
        self.bar.setToolTip("拖动可跳到对应时间点")
        self.bar.setStyleSheet(
            "QSlider::groove:horizontal{height:3px;background:#16283f;border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#7c5cff;border-radius:2px;}"
            "QSlider::handle:horizontal{width:9px;margin:-4px 0;border-radius:4px;"
            "background:#ffffff;}")
        self.bar.hide()
        self.bar.valueChanged.connect(self._on_bar)
        outer.addWidget(self.bar)

        self.tiles: list[ThumbTile] = []
        self.times: list[float] = []
        self._captures = 0              # frames saved as image files this session
        self._cols = 0
        self._relayout_deferred = False
        self._tile_h = ThumbTile.THUMB_HEIGHT + 34
        self._placeholder = QLabel(
            "尚未生成预览图\n\n添加视频后会自动提取关键帧缩略图，\n"
            "也可以点击右上角 ⟳ 重新生成。", self.holder)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color:#5f7ea3;background:transparent;padding:24px;")
        self._grid.addWidget(self._placeholder, 0, 0, 1, 2)

    # ------------------------------------------------------------------ #
    def _show_placeholder(self) -> None:
        self._grid.addWidget(self._placeholder, 0, 0, 1, 2)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        self._placeholder.show()

    def _hide_placeholder(self) -> None:
        self._grid.removeWidget(self._placeholder)
        self._placeholder.hide()

    # ------------------------------------------------------------------ #
    def _on_format_changed(self, index: int) -> None:
        self.sp_quality.setVisible(index == 1)

    def sheet_format(self) -> str:
        return "jpg" if self.cmb_format.currentIndex() == 1 else "png"

    def sheet_quality(self) -> int:
        return int(self.sp_quality.value())

    def frame_count(self) -> int:
        return int(self.sp_count.value())

    def frame_step(self) -> float:
        """0 means "spread evenly over the whole clip"."""
        return float(self.sp_step.value())

    def set_captures(self, count: int) -> None:
        """Number of preview frames saved as files (the "截取次数" readout)."""
        self._captures = max(0, int(count))
        self._update_capture_label()

    def captures(self) -> int:
        return self._captures

    def _update_capture_label(self) -> None:
        self.lbl_captures.setText(f"截取 {self._captures} 张")
        self.lbl_captures.setToolTip(
            f"当前视频已保存 {self._captures} 张预览图" if self._captures
            else "点右侧 📷 把预览图存成图片文件")

    def set_busy(self, busy: bool, text: str = "") -> None:
        if busy:
            self.info.setText(text or "生成中 …")
        self.btn_grid.setEnabled(not busy)
        self.btn_export.setEnabled(not busy and bool(self.tiles))
        self.btn_capture.setEnabled(not busy and bool(self.tiles))

    def set_info(self, text: str) -> None:
        self.info.setText(text)

    def _on_tile_clicked(self, index: int) -> None:
        if 0 <= index < len(self.times):
            self.tileClicked.emit(self.times[index])

    def _on_tile_menu(self, index: int) -> None:
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_save = menu.addAction("保存这一帧为图片 …")
        act_jump = menu.addAction("跳到此时间点")
        chosen = menu.exec(QCursor.pos())
        if chosen is act_save:
            self.saveOneRequested.emit(index)
        elif chosen is act_jump:
            self._on_tile_clicked(index)

    def clear(self) -> None:
        for tile in self.tiles:
            self._grid.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self.tiles = []
        self.times = []
        self._cols = 0
        self._captures = 0
        self._update_capture_label()
        self.bar.hide()
        self._show_placeholder()
        self.btn_export.setEnabled(False)
        self.btn_capture.setEnabled(False)
        self.info.setText("选择视频后自动生成")

    def show_job(self, job: ThumbJob, tile_width: int = 240) -> None:
        for tile in self.tiles:
            self._grid.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self.tiles = []
        self._cols = 0
        self.bar.hide()
        self._hide_placeholder()
        self.times = list(job.times or [])
        for i, frame in enumerate(job.frames):
            t = self.times[i] if i < len(self.times) else 0.0
            tile = ThumbTile(i, vt.human_time(t, with_ms=True), self.holder)
            tile._pm_path = frame
            tile.setCursor(Qt.PointingHandCursor)
            tile.frameClicked.connect(self._on_tile_clicked)
            tile.contextRequested.connect(self._on_tile_menu)
            pm = QPixmap(frame)
            if not pm.isNull():
                tile.set_pixmap(pm)
            tile.show()
            self.tiles.append(tile)
        self._tile_width = tile_width
        self._cols = 0                 # force a relayout
        self._relayout()
        self.btn_export.setEnabled(bool(self.tiles))
        self.btn_capture.setEnabled(bool(self.tiles))
        if self.tiles and not self.bar.isVisible():
            # show() on a child during a synchronous layout pass can crash Qt,
            # so defer it to the next event-loop turn
            QTimer.singleShot(0, self.bar.show)

    # ------------------------------------------------------------------ #
    def _relayout(self) -> None:
        """
        Fit the whole contact sheet into view without scrolling.

        Column count is the only free variable: it fixes the tile width, that
        width fixes the height a tile *wants* (frame aspect ratio + caption),
        and the resulting rows then have to fit the viewport height. So every
        column count is scored on the picture size it allows minus the dead
        space it leaves, and the best one wins. `MAX_ROWS` is only a hint: a
        wall that is much wider than it is tall should use more rows rather
        than shrink every frame.
        """
        if not self.tiles:
            return
        area = self.area.viewport().size()
        if area.width() < 80 or area.height() < 24:
            # The scroll area has not been laid out yet (first show, or the
            # widget is temporarily tiny). Sizing the tiles from this reading
            # would collapse the sheet into one column, so wait one turn.
            if not self._relayout_deferred:
                self._relayout_deferred = True
                QTimer.singleShot(0, self._relayout)
            return
        self._relayout_deferred = False
        avail_w = max(200, area.width() - 8)
        avail_h = max(48, min(area.height(), self.area.height()) - 8)
        count = len(self.tiles)
        gap = self.GAP
        aspect = self._tile_aspect()
        content_w = avail_w - 8
        caption = ThumbTile.CAPTION_HEIGHT

        def tile_width_for(cols: int) -> int:
            # fill the row, but never wider than one viewport row
            return min(self.TILE_MAX, (content_w - (cols - 1) * gap) // cols)

        def layout_for(cols: int) -> tuple[int, int, int, bool]:
            """(tile width, tile height, rows, fits) for this column count."""
            tile_w = tile_width_for(cols)
            want_h = caption + int(tile_w / max(0.2, aspect))
            rows = max(1, (count + cols - 1) // cols)
            room = (avail_h - (rows - 1) * gap) // rows
            fits = room >= want_h
            # squeezed layouts give up height, but never become a bare strip and
            # never drop below the height where the timestamp caption still fits
            tile_h = want_h if fits else max(ThumbTile.MIN_LEGIBLE_HEIGHT, room)
            return tile_w, tile_h, rows, fits

        # Every column count is viable as long as the sheet does not have to
        # grow taller than the viewport. Among those, the winner shows the most
        # picture *and* leaves the least dead space - otherwise a handful of
        # frames would float in a half-empty wall instead of using it.
        best_cols: int | None = None
        best_score = 0.0
        squeezed: tuple[float, int] | None = None
        for candidate in range(1, count + 1):
            tile_w, tile_h, rows, fits = layout_for(candidate)
            total_h = rows * tile_h + (rows - 1) * gap
            value = tile_w * max(1, tile_h - caption)
            if not fits and total_h > avail_h:
                if squeezed is None or value > squeezed[0]:
                    squeezed = (value, candidate)
                continue
            score = value - max(0, avail_h - total_h)
            # Prefer a layout where every tile still carries its timestamp, then
            # one that also gives the picture room to breathe. Without this the
            # scorer happily picks "many small tiles" (hiding every caption) over
            # "fewer, bigger tiles" even when the bigger layout would fit.
            picture = max(1, tile_h - caption)
            if picture >= ThumbTile.MIN_LEGIBLE_HEIGHT:
                score *= 1.25
            if picture >= 120:
                score *= 1.10
            if best_cols is None or score > best_score:
                best_cols, best_score = candidate, score
        if best_cols is None:
            best_cols = squeezed[1] if squeezed else 1
        cols = best_cols
        tile_w, tile_h, rows, _fits = layout_for(cols)

        if cols != self._cols:
            for tile in self.tiles:
                self._grid.removeWidget(tile)
            for i, tile in enumerate(self.tiles):
                r, c = divmod(i, cols)
                self._grid.addWidget(tile, r, c)
            # Reset every column's stretch/minimum first: leftovers from a wider
            # previous layout would otherwise prop the grid open.
            for c in range(self._grid.columnCount()):
                self._grid.setColumnStretch(c, 0)
                self._grid.setColumnMinimumWidth(c, 0)
            # Equal stretch on every column, so the leftover pixels are spread
            # evenly and all tiles keep the *same* width. (Letting only the last
            # column stretch made the final tile in each row visibly wider than
            # its neighbours - the misalignment that showed up in review.)
            for c in range(cols):
                self._grid.setColumnMinimumWidth(c, tile_w)
                self._grid.setColumnStretch(c, 1)
            for r in range(self._grid.rowCount()):
                self._grid.setRowStretch(r, 0)
            self._cols = cols
        else:
            for c in range(self._grid.columnCount()):
                self._grid.setColumnStretch(c, 1 if c < cols else 0)
            for c in range(cols):
                self._grid.setColumnMinimumWidth(c, tile_w)
        for tile in self.tiles:
            tile.setFixedHeight(tile_h)
            tile.setFixedWidth(tile_w)
            tile.set_compact(tile_h < ThumbTile.MIN_LEGIBLE_HEIGHT)
        if self.times:
            spread = (f"{vt.human_time(self.times[0])} – {vt.human_time(self.times[-1])}"
                      if len(self.times) > 1 else vt.human_time(self.times[0]))
            # Short: the long "覆盖 起点 – 终点" line lived here, but with the
            # parameters on their own row the info label has no room to spare, so
            # the covered range moved into the tooltip.
            self.info.setText(f"{len(self.tiles)} 张 · {cols}×{rows}")
            self.info.setToolTip(
                f"共 {len(self.tiles)} 张预览图，排成 {cols} 列 × {rows} 行\n"
                f"覆盖时间：{spread}\n点击缩略图可跳到该时间点；右键可只保存这一帧")
        self._update_capture_label()

    def _tile_aspect(self) -> float:
        """Width/height of a preview frame - drives how wide a tile wants to be."""
        pm = self.tiles[0]._pm if self.tiles else None
        if pm is not None and not pm.isNull() and pm.height() > 0:
            return max(0.4, min(3.2, pm.width() / pm.height()))
        return 16.0 / 9.0

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.tiles:
            self._relayout()
        elif not self._placeholder.isHidden():
            self._show_placeholder()

    def highlight(self, position: float, duration: float) -> None:
        if duration <= 0:
            return
        self.bar.blockSignals(True)
        self.bar.setValue(int(max(0.0, min(1.0, position / duration)) * 1000))
        self.bar.blockSignals(False)

    def _on_bar(self, value: int) -> None:
        if self.times:
            idx = int(value / 1000.0 * (len(self.times) - 1) + 0.5)
            idx = max(0, min(len(self.times) - 1, idx))
            self.tileClicked.emit(self.times[idx])


# --------------------------------------------------------------------------- #
# Audio plumbing
# --------------------------------------------------------------------------- #

class AudioSource(QObject):
    """
    Bounded PCM ring buffer: FFmpeg pipe writes, the audio device pulls.

    The producer decodes far faster than real time, so an unbounded buffer
    would swallow the whole file. Instead the decoder thread blocks on `full()`
    and is woken up by the pump, which keeps decoded-ahead audio to roughly
    `max_bytes` - short enough that seeking still feels instant.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._written = 0
        self._dropped = 0
        self._space = threading.Condition(self._lock)
        self._stopped = False
        self.max_bytes = int(AUDIO_BYTES_PER_SEC * 0.35)   # ~350 ms

    def write(self, data: bytes) -> bool:
        """Append `data`, waiting while the buffer is full. False if stopped."""
        with self._space:
            if self._stopped:
                return False
            # Wait for room first, then append whole. Never trim from the front:
            # the queued samples are the ones about to be played, so dropping
            # them would produce clicks and an audible gap.
            waited = 0.0
            while not self._stopped and len(self._buf) + len(data) > self.max_bytes:
                self._space.wait(0.05)
                waited += 0.05
                if waited > 2.0:                 # device stalled: don't block forever
                    break
            if self._stopped:
                return False
            self._buf.extend(data)
            self._written += len(data)
            return True

    def read(self, max_bytes: int) -> bytes:
        with self._lock:
            if not self._buf:
                return b""
            n = min(max_bytes, len(self._buf))
            n -= n % (AUDIO_CHANNELS * 2)             # whole stereo frames only
            if n <= 0:
                return b""
            out = bytes(self._buf[:n])
            del self._buf[:n]
            self._space.notify_all()
            return out

    def clear(self) -> None:
        with self._space:
            self._buf.clear()
            self._space.notify_all()

    def stop(self) -> None:
        with self._space:
            self._stopped = True
            self._buf.clear()
            self._space.notify_all()

    def reopen(self) -> None:
        with self._space:
            self._stopped = False

    @property
    def buffered_seconds(self) -> float:
        with self._lock:
            return len(self._buf) / AUDIO_BYTES_PER_SEC

    @property
    def dropped_seconds(self) -> float:
        with self._lock:
            return self._dropped / AUDIO_BYTES_PER_SEC


class AudioOutput(QObject):
    """
    Owns the QAudioSink and keeps its device buffer topped up.

    A 1 s device buffer plus a 350 ms decode ring gives enough headroom that
    the hardware never runs dry, which is what keeps `processedUSecs()` - our
    audio clock - advancing smoothly instead of stalling.
    """

    PRELOAD_SECONDS = 0.35
    DEVICE_BUFFER_SECONDS = 1.0

    def __init__(self, source: AudioSource, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.sink = None
        self.device = None
        self.clock_us = 0
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._pump)

    def _pump(self) -> None:
        if self.sink is None:
            return
        try:
            self.clock_us = int(self.sink.processedUSecs())
        except Exception:
            pass
        if self.device is None:
            return
        # keep the device buffer nearly full; the ring buffer caps how far the
        # decoder may run ahead
        for _ in range(4):
            try:
                free = self.sink.bytesFree()
            except Exception:
                return
            if free < AUDIO_BYTES_PER_SEC // 20:      # < 50 ms of room left
                return
            chunk = self.source.read(free)
            if not chunk:
                return
            try:
                written = self.device.write(chunk)
            except Exception:
                return
            if written < len(chunk):
                return

    def start(self) -> bool:
        if not HAVE_AUDIO or self.sink is not None:
            return self.sink is not None
        try:
            fmt = QAudioFormat()
            fmt.setSampleRate(AUDIO_RATE)
            fmt.setChannelCount(AUDIO_CHANNELS)
            fmt.setSampleFormat(QAudioFormat.Int16)
            dev = QMediaDevices.defaultAudioOutput()
            if dev is None or dev.isNull() or not dev.isFormatSupported(fmt):
                return False
            self.sink = QAudioSink(dev, fmt, self)
            self.sink.setBufferSize(int(AUDIO_BYTES_PER_SEC * self.DEVICE_BUFFER_SECONDS))
            self.device = self.sink.start()
            if self.device is None:
                self.sink = None
                return False
            self.clock_us = 0
            # preload so playback starts with sound already queued
            deadline = self.PRELOAD_SECONDS
            waited = 0.0
            while waited < 0.6:
                free = self.sink.bytesFree()
                chunk = self.source.read(free)
                if not chunk:
                    break
                self.device.write(chunk)
                waited += len(chunk) / AUDIO_BYTES_PER_SEC
                if waited >= deadline:
                    break
                time.sleep(0.01)
            self._timer.start()
            return True
        except Exception:
            self.sink = None
            self.device = None
            return False

    def stop(self) -> None:
        self._timer.stop()
        if self.sink is not None:
            try:
                self.sink.stop()
            except Exception:
                pass
        self.sink = None
        self.device = None
        self.clock_us = 0


def find_ffplay() -> str | None:
    """ffplay ships next to ffmpeg in most static builds."""
    found = vt.locate_ffmpeg()
    if not found:
        return None
    candidate = os.path.join(os.path.dirname(found[0]),
                             "ffplay.exe" if os.name == "nt" else "ffplay")
    if os.path.isfile(candidate):
        return candidate
    return shutil.which("ffplay")


class PlayerAudio(QObject):
    """
    Audio output with two interchangeable backends.

    `ffplay` is preferred: it consumes the same PCM pipe and needs no Qt
    multimedia plugin, so audio keeps working even in a stripped PySide6
    ("Essentials") install. When ffplay is unavailable we fall back to
    QAudioSink, and when neither works the preview simply plays silently.

    Playback video timing is anchored to `audio_started_at` - the moment audio
    data actually began flowing - so a slow-starting audio process does not
    push the picture ahead of the sound.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.backend = "none"
        self.ffplay = find_ffplay()
        self.process: subprocess.Popen | None = None
        self.thread: DecodeThread | None = None
        self.source = AudioSource(self)
        self.sink_out = AudioOutput(self.source, self)
        self.audio_started_at: float | None = None
        self.clock_source = "wall"          # wall | qt
        self._chunk_seen = False
        # Guards against a decode thread calling back into a player that is
        # being torn down; without it the callback can touch a garbage
        # collected QObject and crash the process.
        self._stopping = False

        if self.ffplay:
            self.backend = "ffplay"
        elif HAVE_AUDIO:
            self.backend = "qt"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    # ------------------------------------------------------------------ #
    def start(self, src: str, at: float) -> bool:
        self.stop()
        if not self.available:
            return False
        self._stopping = False
        self.audio_started_at = None
        self._chunk_seen = False

        if self.backend == "ffplay":
            found = vt.locate_ffmpeg()
            if not found:
                return False
            args = [
                self.ffplay, "-hide_banner", "-loglevel", "error", "-nostats",
                "-nodisp", "-autoexit", "-volume", "80",
                "-ss", f"{max(0.0, at):.3f}", "-i", src,
                "-vn", "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_RATE),
            ]
            try:
                self.process = vt.popen_hidden(
                    args, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.process = None
                return False
            self.clock_source = "wall"
            # ffplay starts playing almost immediately; anchor now and refine
            # the moment the decoder reports its first real progress later.
            self.audio_started_at = time.perf_counter()
            return True

        if self.backend == "qt":
            found = vt.locate_ffmpeg()
            if not found:
                return False
            args = [
                found[0], "-hide_banner", "-v", "error",
                "-nostdin", "-ss", f"{max(0.0, at):.3f}", "-i", src,
                "-vn", "-sn", "-dn", "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_RATE), "-f", "s16le", "-",
            ]
            self.source.clear()
            self.source.reopen()
            self.thread = DecodeThread(args, self._on_pcm, 32768, name="audio-pcm")
            self.thread.start()
            if not self.sink_out.start():
                self.stop()
                return False
            self.clock_source = "qt"
            self.audio_started_at = time.perf_counter()
            return True
        return False

    def _on_pcm(self, chunk: bytes) -> bool:
        # the decode thread may still be draining its pipe while we tear down
        if self._stopping or self.source is None:
            return False
        if not self._chunk_seen:
            self._chunk_seen = True
            self.audio_started_at = time.perf_counter()
        return self.source.write(chunk)

    # ------------------------------------------------------------------ #
    def elapsed(self) -> float:
        """Seconds of audio played since start(), or 0 when unknown."""
        if self.backend == "qt" and self.sink_out.sink is not None:
            return self.sink_out.clock_us / 1_000_000.0
        if self.audio_started_at is None:
            return 0.0
        return max(0.0, time.perf_counter() - self.audio_started_at)

    def stop(self) -> None:
        self._stopping = True
        if self.thread is not None:
            self.thread.stop()
            self.thread.join(timeout=1.0)
            self.thread = None
        self.sink_out.stop()
        proc = self.process
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        self.process = None
        self.source.stop()
        self.source.reopen()
        self.audio_started_at = None
        self._chunk_seen = False

    def pause(self) -> None:
        """
        Stop making sound without losing the media position.

        The Qt backend can suspend its sink. The ffplay backend is killed
        outright - a preview pause is always followed by either a resume (which
        restarts the pipe at the current position) or a seek, so keeping a
        suspended process around would only leak handles.
        """
        if self.backend == "qt" and self.sink_out.sink is not None:
            try:
                self.sink_out.sink.suspend()
            except Exception:
                pass
            return
        self.stop()


class DecodeThread(threading.Thread):
    """Reads one raw stream out of an FFmpeg pipe, in its own thread."""

    def __init__(self, args: list[str], on_chunk, chunk_size: int,
                 on_done=None, name: str = "decode") -> None:
        super().__init__(daemon=True, name=name)
        self.args = args
        self.on_chunk = on_chunk
        self.chunk_size = chunk_size
        self.on_done = on_done
        # NOTE: not `_stop` - threading.Thread owns that name internally and
        # shadowing it makes join() raise "'Event' object is not callable".
        self._stop_event = threading.Event()
        self.proc: subprocess.Popen | None = None

    def stop(self) -> None:
        self._stop_event.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self) -> None:
        try:
            self.proc = vt.popen_hidden(self.args, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        except Exception:
            if self.on_done:
                self.on_done()
            return
        stdout = self.proc.stdout
        try:
            while not self._stop_event.is_set() and stdout is not None:
                chunk = stdout.read(self.chunk_size)
                if not chunk or self._stop_event.is_set():
                    break
                self.on_chunk(chunk)
        except Exception:
            pass
        finally:
            try:
                if self.proc is not None and self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
            if self.on_done:
                self.on_done()


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #

class VideoSurface(QLabel):
    """
    Letterboxed frame display with mouse zoom / pan.

    Plain clicks and drags still belong to the player (nothing else claims the
    mouse here), so the wheel zooms, a left drag pans while zoomed in, and a
    double click resets the view. The widget keeps painting the *source* frame
    itself (`_pm`) instead of letting QLabel scale it, because zooming needs to
    know the letterboxed size.
    """

    ZOOM_MIN = 0.5
    ZOOM_MAX = 12.0
    ZOOM_STEP = 1.15
    _MAX_OFFSET_SLACK = 0.75        # how far the picture may leave the viewport

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(130)
        self.setStyleSheet("background:#04070d;")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMouseTracking(True)
        self.setToolTip("滚轮缩放 · 放大后按住左键拖动平移 · 双击还原")
        self._pm: QPixmap | None = None
        self._zoom = 1.0
        self._offset = [0.0, 0.0]       # pan from center, device pixels
        self._drag_from: QPoint | None = None
        self._drag_offset = (0.0, 0.0)

    # ------------------------------------------------------------------ #
    # frames
    # ------------------------------------------------------------------ #
    def set_frame_image(self, image: QImage) -> None:
        """New frame during playback: keep whatever zoom the user dialled in."""
        self._pm = QPixmap.fromImage(image)
        self.update()

    def set_source(self, pm: QPixmap | None, reset: bool = True) -> None:
        """Install a new frame as the (unzoomed) baseline."""
        self._pm = pm
        if reset:
            self.reset_view()
        self.update()

    def clear_frame(self) -> None:
        self._pm = None
        self.reset_view()
        self.update()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._offset = [0.0, 0.0]
        self._drag_from = None
        self._sync_cursor()

    @property
    def zoom(self) -> float:
        return self._zoom

    def has_frame(self) -> bool:
        """True once a frame (poster or decoded picture) is on screen."""
        return self._pm is not None and not self._pm.isNull()

    def reset_zoom(self) -> None:
        self.reset_view()
        self.update()

    # ------------------------------------------------------------------ #
    # geometry
    # ------------------------------------------------------------------ #
    def _fit_size(self) -> tuple[int, int]:
        """Size of the picture at zoom 1 (fits inside the widget, aspect kept)."""
        if self._pm is None or self._pm.isNull():
            return (0, 0)
        area = self.size()
        if area.width() < 8 or area.height() < 8:
            return (0, 0)
        scaled = self._pm.size().scaled(area, Qt.KeepAspectRatio)
        return (max(1, scaled.width()), max(1, scaled.height()))

    def _target_rect(self) -> QRect:
        """Where the picture goes, in widget coordinates."""
        base_w, base_h = self._fit_size()
        if base_w <= 0:
            return QRect()
        w = max(1, int(round(base_w * self._zoom)))
        h = max(1, int(round(base_h * self._zoom)))
        x = (self.width() - w) // 2 + int(round(self._offset[0]))
        y = (self.height() - h) // 2 + int(round(self._offset[1]))
        return QRect(x, y, w, h)

    def _clamp_offset(self) -> None:
        """Keep the picture from being dragged completely out of sight."""
        if self._pm is None or self._pm.isNull():
            self._offset = [0.0, 0.0]
            return
        base_w, base_h = self._fit_size()
        slack_x = self.width() * self._MAX_OFFSET_SLACK + base_w * self._zoom * 0.5
        slack_y = self.height() * self._MAX_OFFSET_SLACK + base_h * self._zoom * 0.5
        self._offset[0] = max(-slack_x, min(slack_x, self._offset[0]))
        self._offset[1] = max(-slack_y, min(slack_y, self._offset[1]))

    def _sync_cursor(self) -> None:
        self.setCursor(Qt.OpenHandCursor if self._zoom > 1.001 else Qt.ArrowCursor)

    # ------------------------------------------------------------------ #
    # painting
    # ------------------------------------------------------------------ #
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#04070d"))
        if self._pm is None or self._pm.isNull():
            painter.end()
            return
        rect = self._target_rect()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(rect, self._pm)
        if self._zoom > 1.001:
            self._paint_zoom_badge(painter)
        painter.end()

    def _paint_zoom_badge(self, painter: QPainter) -> None:
        text = f"{self._zoom:.2f}×"
        f = QFont(self.font())
        f.setPointSize(9)
        painter.setFont(f)
        metrics = painter.fontMetrics()
        w = metrics.horizontalAdvance(text) + 14
        box = QRect(8, 8, w, metrics.height() + 6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(8, 17, 29, 190))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(QColor("#cfe4ff"))
        painter.drawText(box, Qt.AlignCenter, text)

    # ------------------------------------------------------------------ #
    # interaction
    # ------------------------------------------------------------------ #
    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._pm is None or self._pm.isNull():
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        old_zoom = self._zoom
        new_zoom = old_zoom * (self.ZOOM_STEP if delta > 0 else 1.0 / self.ZOOM_STEP)
        new_zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, new_zoom))
        if abs(new_zoom - old_zoom) < 1e-6:
            event.accept()
            return
        # keep whatever sits under the cursor pinned while zooming
        center = QPoint(self.width() // 2, self.height() // 2)
        pos = event.position()
        rel_x = pos.x() - center.x() - self._offset[0]
        rel_y = pos.y() - center.y() - self._offset[1]
        ratio = new_zoom / old_zoom
        self._offset[0] += rel_x * (1.0 - ratio)
        self._offset[1] += rel_y * (1.0 - ratio)
        self._zoom = new_zoom
        if new_zoom <= 1.0001:
            self._offset = [0.0, 0.0]
        else:
            self._clamp_offset()
        self._sync_cursor()
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._zoom > 1.001:
            self.reset_view()
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._zoom > 1.001:
            self._drag_from = event.position().toPoint()
            self._drag_offset = (self._offset[0], self._offset[1])
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None:
            delta = event.position().toPoint() - self._drag_from
            self._offset = [self._drag_offset[0] + delta.x(),
                            self._drag_offset[1] + delta.y()]
            self._clamp_offset()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None and event.button() == Qt.LeftButton:
            self._drag_from = None
            self._sync_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._clamp_offset()
        self.update()


class PlayerPanel(QWidget):
    """Preview player driven by FFmpeg pipes (see module docstring)."""

    positionChanged = Signal(float)
    playbackChanged = Signal(bool)
    splitPointRequested = Signal(float)

    VIDEO_QUEUE_LIMIT = 6
    MAX_RENDER_WIDTH = 1280
    """Frames decoded ahead of the clock. Large enough that a decoding hiccup
    never starves the clock, small enough that memory stays modest."""
    LOOKAHEAD_FRAMES = 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info: vt.MediaInfo | None = None

        self._playing = False
        self._pos = 0.0
        self._base_pos = 0.0
        self._audio_base_pos = 0.0
        self._play_start = 0.0
        self._scrubbing = False
        self._resume_after_scrub = False
        self._eof = False

        self._queue: deque[tuple[float, bytes]] = deque()
        self._queue_lock = threading.Lock()
        self._next_pts = 0.0
        self._frame_size = 0
        self._frame_dims = (0, 0)
        self._frame_interval = 1.0 / 25.0

        self._video_thread: DecodeThread | None = None
        self._video_eof = False
        self._last_audio_check = 0.0
        self._audio_requested_at = 0.0
        self._audio_watchdog_done = False
        self._audio_watch = 0.0
        self._last_audio_elapsed = 0.0
        self._abort_decode = False

        self.audio = PlayerAudio(self)

        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.set_info(None)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        host = QWidget()
        host.setStyleSheet("background:#04070d;border-radius:9px;")
        self.stack = QStackedLayout(host)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.empty = EmptyState()
        self.surface = VideoSurface()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.surface)
        root.addWidget(host, 1)

        bar = QFrame()
        bar.setObjectName("TransportBar")
        bar.setStyleSheet(
            "#TransportBar{background:rgba(10,22,38,0.85);border:1px solid #1b2f4a;"
            "border-radius:9px;}")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(10, 7, 10, 8)
        bl.setSpacing(5)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.lbl_pos = QLabel("0:00:00.000")
        self.lbl_pos.setStyleSheet(
            "color:#e2edff;font-family:Consolas,monospace;font-size:11px;")
        self.lbl_dur = QLabel("0:00:00.000")
        self.lbl_dur.setStyleSheet(
            "color:#7f9dc0;font-family:Consolas,monospace;font-size:11px;")
        self.slider = TimeSlider()
        self.slider.setStyleSheet(
            "QSlider::groove:horizontal{height:6px;background:#16283f;border-radius:3px;}"
            "QSlider::sub-page:horizontal{background:%s;border-radius:3px;}"
            "QSlider::handle:horizontal{width:13px;margin:-5px 0;border-radius:6px;"
            "background:#ffffff;border:2px solid %s;}" % (ACCENT, ACCENT_HI))
        row1.addWidget(self.lbl_pos)
        row1.addWidget(self.slider, 1)
        row1.addWidget(self.lbl_dur)
        bl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.btn_start = ToolBtn("⏮", "回到开头 (Home)")
        self.btn_back = ToolBtn("⏪", "后退 5 秒 (←)")
        self.btn_play = ToolBtn("▶", "播放 / 暂停 (空格)")
        self.btn_fwd = ToolBtn("⏩", "前进 5 秒 (→)")
        self.btn_end = ToolBtn("⏭", "跳到结尾 (End)")
        self.btn_mute = ToolBtn("🔊", "静音 / 取消静音 (M)", checkable=True)
        self.btn_mark = ToolBtn("✂", "把播放位置设为拆分点 (S)")
        self.btn_full = ToolBtn("⛶", "全屏预览 (F11)")
        self.lbl_state = QLabel("就绪")
        self.lbl_state.setStyleSheet("color:#8fb2d8;font-size:11px;")
        audio_note = {"ffplay": "", "qt": "", "none": "· 未找到音频输出，仅预览画面"}
        self.lbl_audio = QLabel(audio_note.get(self.audio.backend, ""))
        self.lbl_audio.setStyleSheet("color:#c9a227;font-size:10px;")

        for b in (self.btn_start, self.btn_back, self.btn_play, self.btn_fwd,
                  self.btn_end, self.btn_mute, self.btn_mark, self.btn_full):
            row2.addWidget(b)
        row2.addSpacing(6)
        row2.addWidget(self.lbl_state)
        row2.addWidget(self.lbl_audio)
        row2.addStretch(1)
        bl.addLayout(row2)
        root.addWidget(bar, 0)

        self.btn_play.clicked.connect(self.toggle)
        self.btn_start.clicked.connect(lambda: self.seek(0.0))
        self.btn_end.clicked.connect(lambda: self.seek(self.duration))
        self.btn_back.clicked.connect(lambda: self.seek(self._pos - 5.0))
        self.btn_fwd.clicked.connect(lambda: self.seek(self._pos + 5.0))
        self.btn_mute.toggled.connect(self._on_mute)
        self.btn_mark.clicked.connect(lambda: self.splitPointRequested.emit(self._pos))
        self.btn_full.clicked.connect(self._toggle_fullscreen)
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderReleased.connect(self._on_scrub_end)
        self.slider.sliderMoved.connect(self._on_scrub_move)

        if not self.audio.available:
            self.btn_mute.setEnabled(False)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    @property
    def duration(self) -> float:
        return self.info.duration if self.info else 0.0

    @property
    def position(self) -> float:
        return self._pos

    @property
    def playing(self) -> bool:
        return self._playing

    def set_info(self, info: vt.MediaInfo | None) -> None:
        self.stop()
        self.info = info
        self._pos = 0.0
        self._base_pos = 0.0
        self._audio_base_pos = 0.0
        self.slider.setValue(0)
        self.lbl_pos.setText("0:00:00.000")
        self.lbl_dur.setText(vt.human_time(info.duration, True) if info else "0:00:00.000")
        controls = (self.btn_play, self.btn_back, self.btn_fwd, self.btn_start,
                    self.btn_end, self.btn_mark, self.btn_full)
        if info is None:
            self.stack.setCurrentWidget(self.empty)
            self.surface.clear_frame()
            self.slider.setEnabled(False)
            self.lbl_state.setText("就绪")
            for b in controls:
                b.setEnabled(False)
            return
        for b in controls:
            b.setEnabled(True)
        self.slider.setEnabled(True)
        if info.error:
            self.lbl_state.setText(f"⚠ {info.error}")
        else:
            parts = [info.resolution_text]
            if info.fps:
                parts.append(f"{info.fps:.2f} fps")
            if info.video_codec:
                parts.append(info.video_codec.upper())
            if info.has_audio and info.audio_codec:
                parts.append(info.audio_codec.upper())
            self.lbl_state.setText(" · ".join(parts))

    def load(self, info: vt.MediaInfo, poster: str | None = None) -> None:
        """Select a file. Optional `poster` is a still image shown before play."""
        self.set_info(info)
        if poster and os.path.isfile(poster):
            pm = QPixmap(poster)
            if not pm.isNull():
                self.surface.set_source(pm)
                self.stack.setCurrentWidget(self.surface)

    # ------------------------------------------------------------------ #
    # decode plumbing
    # ------------------------------------------------------------------ #
    def _pipe_base(self, start: float) -> list[str]:
        found = vt.locate_ffmpeg()
        if not found or self.info is None:
            return []
        return [found[0], "-hide_banner", "-v", "error", "-nostdin",
                "-ss", f"{max(0.0, start):.3f}", "-i", self.info.path]

    def _start_video(self, start: float) -> None:
        area = self.surface.size()
        width = min(self.MAX_RENDER_WIDTH, max(320, area.width() or 960))
        height = max(180, min(900, area.height() or 540))
        width -= width % 2
        height -= height % 2
        self._frame_dims = (width, height)
        self._frame_size = width * height * 3
        fps = self.info.fps if self.info and self.info.fps else 25.0
        self._frame_interval = 1.0 / max(1.0, min(120.0, fps))
        with self._queue_lock:
            self._queue.clear()
        self._next_pts = start
        self._video_eof = False
        self._abort_decode = False
        args = self._pipe_base(start) + [
            "-an", "-sn", "-dn",
            "-vf", (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"),
            "-r", f"{max(1.0, min(60.0, fps)):.4f}",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ]
        self._video_thread = DecodeThread(
            args, self._on_video_chunk, self._frame_size,
            on_done=self._on_video_done, name="video-decode")
        self._video_thread.start()

    def _start_audio(self, start: float) -> None:
        if not (self.info and self.info.has_audio and self.audio.available):
            return
        if self.btn_mute.isChecked():
            return
        self._audio_base_pos = start
        self._audio_requested_at = time.perf_counter()
        self._audio_watchdog_done = False
        self._last_audio_check = time.perf_counter()
        self.audio.start(self.info.path, start)

    def _stop_threads(self) -> None:
        self._abort_decode = True
        thread = self._video_thread
        if thread is not None:
            thread.stop()
            self._video_thread = None
        with self._queue_lock:
            self._queue.clear()

    def _on_video_chunk(self, chunk: bytes) -> None:
        size = self._frame_size
        if size <= 0:
            return
        offset = 0
        while offset + size <= len(chunk):
            if self._abort_decode:
                return              # paused/stopped/seeked: drop the rest
            queued = False
            with self._queue_lock:
                if not self._abort_decode and len(self._queue) < self.LOOKAHEAD_FRAMES:
                    pts = self._next_pts
                    self._next_pts += self._frame_interval
                    self._queue.append((pts, chunk[offset:offset + size]))
                    queued = True
            offset += size
            if not queued:
                # Decoder is ahead of the player: throttle instead of buffering
                # the whole file in RAM (a few tens of ms is imperceptible).
                time.sleep(0.01)

    def _on_video_done(self) -> None:
        self._video_eof = True

    # ------------------------------------------------------------------ #
    # clock
    # ------------------------------------------------------------------ #
    def _clock(self) -> float:
        """
        Smooth playback position: wall clock since the last anchor, capped by
        how far video decoding has progressed. `_sync_with_audio()` nudges the
        anchor periodically so the picture follows the sound.
        """
        elapsed = time.perf_counter() - self._play_start
        with self._queue_lock:
            ahead = (self._queue[-1][0] - self._base_pos + self._frame_interval
                     if self._queue else 0.0)
        return self._base_pos + min(elapsed, max(0.0, ahead))

    def _decoded_limit(self) -> float:
        """Never report a position beyond what has actually been decoded."""
        with self._queue_lock:
            limit = (self._queue[-1][0] + self._frame_interval
                     if self._queue else self._base_pos)
        return max(self._base_pos, limit)

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def play(self) -> None:
        if self.info is None or self._playing:
            return
        start = self._pos
        if self.duration and start >= self.duration - 0.03:
            start = 0.0
        self._playing = True
        self._base_pos = start
        self._play_start = time.perf_counter()
        self._start_video(start)
        self._start_audio(start)
        self._abort_decode = False
        self.timer.start()
        self.stack.setCurrentWidget(self.surface)
        self.btn_play.setText("⏸")
        self.playbackChanged.emit(True)

    def pause(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._pos = self._clock()
        self.timer.stop()
        self._stop_threads()
        self.audio.pause()
        self.btn_play.setText("▶")
        self.playbackChanged.emit(False)

    def stop(self) -> None:
        was_playing = self._playing
        self._playing = False
        self.timer.stop()
        self._stop_threads()
        self.audio.stop()
        self.btn_play.setText("▶")
        if was_playing:
            self.playbackChanged.emit(False)

    def seek(self, seconds: float, resume: bool | None = None) -> None:
        if self.info is None:
            return
        target = max(0.0, min(float(seconds), self.duration or 0.0))
        was_playing = self._playing
        if was_playing:
            self._playing = False
            self.timer.stop()
            self._stop_threads()
        self._pos = target
        self._base_pos = target
        self._audio_base_pos = target
        self.audio.stop()
        self.lbl_pos.setText(vt.human_time(target, True))
        if self.duration:
            self.slider.setValue(int(target / self.duration * 1000))
        self.positionChanged.emit(target)
        if was_playing or resume:
            self._playing = False
            self.play()
        else:
            self._show_still(target)

    def _show_still(self, at: float) -> None:
        if self.info is None:
            return
        found = vt.locate_ffmpeg()
        if not found:
            return
        folder = os.path.join(vt.CACHE_DIR, "scrub")
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, f"seek_{int(at * 1000):09d}.jpg")
        if not os.path.isfile(out):
            args = [found[0], "-y", "-v", "error", "-ss", f"{at:.3f}",
                    "-i", self.info.path, "-frames:v", "1",
                    "-vf", "scale=960:-2", "-q:v", "4", out]
            try:
                vt.run_hidden(args, timeout=45)
            except Exception:
                return
        if os.path.isfile(out):
            pm = QPixmap(out)
            if not pm.isNull():
                self.surface.set_source(pm, reset=False)
                self.stack.setCurrentWidget(self.surface)

    def _tick(self) -> None:
        if not self._playing or self.info is None:
            return
        self._check_audio_health()
        self._sync_with_audio()
        self._pos = self._clock()
        self._drain_frames()
        if not self._scrubbing:
            if self.duration:
                self.slider.setValue(int(min(1.0, self._pos / self.duration) * 1000))
            self.lbl_pos.setText(vt.human_time(self._pos, True))
        self.positionChanged.emit(self._pos)

        ended = (self.duration > 0 and self._pos >= self.duration - 0.05)
        starved = self._video_eof and not self._has_frames()
        if ended or (starved and self._pos > 0.2 and not self._decoder_alive()):
            self.pause()
            if not self._scrubbing:
                self.slider.setValue(1000)
            self.lbl_pos.setText(vt.human_time(self.duration, True))
            self._pos = self.duration

    # ------------------------------------------------------------------ #
    def _check_audio_health(self) -> None:
        """
        Drop the audio backend if it stopped making progress, so the preview
        keeps playing silently instead of the clock being held back by a device
        that is not draining its buffer.
        """
        if self._audio_watchdog_done or not self.info or not self.info.has_audio:
            return
        now = time.perf_counter()
        self._audio_watch = getattr(self, "_audio_watch", 0.0)

        if self.audio.audio_started_at is None:
            if self._audio_requested_at and now - self._audio_requested_at > 1.5:
                self._disable_audio("· 音频输出不可用，已切换为静音预览")
            return

        # data is flowing: make sure the device is actually consuming it
        if now - self._audio_watch >= 2.0:
            processed = self.audio.elapsed()
            if processed - self._last_audio_elapsed < 0.5 and processed > 0.5:
                self._disable_audio("· 音频设备未推进，已切换为静音预览")
                return
            self._last_audio_elapsed = processed
            self._audio_watch = now

    def _disable_audio(self, note: str) -> None:
        self._audio_watchdog_done = True
        self.audio.backend = "none"
        self.audio.stop()
        self.btn_mute.blockSignals(True)
        self.btn_mute.setChecked(True)
        self.btn_mute.blockSignals(False)
        self.btn_mute.setText("🔇")
        self.lbl_audio.setText(note)
        self.lbl_audio.setStyleSheet("color:#c9a227;font-size:10px;")
        # playback continues on the wall clock from wherever we are
        self._base_pos = self._pos
        self._play_start = time.perf_counter()

    def _sync_with_audio(self) -> None:
        """
        Keep the picture on the sound, once per second.

        The video clock is a smooth wall clock capped by decoder progress, and
        audio is the reference it is nudged towards: if audio has fallen behind
        we re-anchor the picture to it (never skipping backwards far - the
        queue is capped, so a lagging picture is a lagging decoder), and if the
        wall clock is lagging behind audio we simply let it catch up.
        """
        if self.audio.audio_started_at is None:
            return
        now = time.perf_counter()
        if now - self._last_audio_check < 1.0:
            return
        self._last_audio_check = now
        audio_pos = self._audio_base_pos + self.audio.elapsed()
        drift = self._pos - audio_pos
        if drift > 0.35:
            # picture is ahead of the sound: re-anchor the clock to audio
            self._base_pos = audio_pos
            self._play_start = now
        elif drift < -0.35:
            # picture lags the sound (slow decoder): restart decoding here
            self._start_video(audio_pos)
            self._base_pos = audio_pos
            self._play_start = now

    def _has_frames(self) -> bool:
        with self._queue_lock:
            return bool(self._queue)

    def _decoder_alive(self) -> bool:
        thread = self._video_thread
        return bool(thread is not None and thread.is_alive())

    def _drain_frames(self) -> None:
        frame: bytes | None = None
        with self._queue_lock:
            while self._queue and self._queue[0][0] <= self._pos + self._frame_interval * 0.75:
                frame = self._queue.popleft()[1]
        if frame is None:
            return
        width, height = self._frame_dims
        expected = width * height * 3
        if len(frame) < expected or width <= 0:
            return
        image = QImage(frame[:expected], width, height, width * 3, QImage.Format_RGB888).copy()
        self.surface.set_frame_image(image)

    # ------------------------------------------------------------------ #
    # input helpers
    # ------------------------------------------------------------------ #
    def _on_scrub_start(self) -> None:
        self._scrubbing = True
        self._resume_after_scrub = self._playing
        if self._playing:
            self.pause()

    def _on_scrub_move(self, value: int) -> None:
        if not self.duration:
            return
        self.lbl_pos.setText(vt.human_time(value / 1000.0 * self.duration, True))

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        if not self.duration:
            return
        self.seek(self.slider.value() / 1000.0 * self.duration,
                  resume=self._resume_after_scrub)

    def _on_mute(self, muted: bool) -> None:
        self.btn_mute.setText("🔇" if muted else "🔊")
        if muted:
            self.audio.stop()
        elif self._playing and self.info is not None and self.info.has_audio:
            self._start_audio(self._pos)

    def _toggle_fullscreen(self) -> None:
        window = self.window()
        window.showNormal() if window.isFullScreen() else window.showFullScreen()

    def shutdown(self) -> None:
        self.stop()
