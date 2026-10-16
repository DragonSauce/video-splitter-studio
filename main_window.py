"""
main_window.py - the application shell.

Layout
------
┌ menu bar ────────────────────────────────────────────────────────────────┐
│ ┌ queue (left) ────────┐ ┌ player (top right) ────────────────────────┐ │
│ │ drop zone / list     │ │ video surface + transport                  │ │
│ │                      │ ├────────────────────────────────────────────┤ │
│ │                      │ │ split settings + segment table + previews  │ │
│ └──────────────────────┘ └────────────────────────────────────────────┘ │
│ status bar / progress / log                                              │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from PySide6.QtCore import (
    QEvent, QObject, QRect, QSize, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen,
    QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStackedLayout, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QTextEdit, QToolButton, QVBoxLayout, QWidget, QPlainTextEdit,
)

import video_tools as vt
from player import HAVE_AUDIO, ThumbJob, ThumbnailWall, PlayerPanel
from ui_common import (
    ACCENT, ACCENT_HI, DropZoneHint, ElidedLabel, TimeSlider, ToolBtn,
    ask_yes_no, loud_tooltip, make_app_icon, message_box, notify,
)

APP_TITLE = "Video Splitter Studio"
DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Videos", "SplitOutput")

# --------------------------------------------------------------------------- #
# Layout knobs - everything a layout tweak needs lives here, in one place.
# --------------------------------------------------------------------------- #
LAYOUT = {
    # Height budget, preview first: the video frame is the point of the window,
    # so it takes the biggest slice and the contact sheet, settings block and
    # log all give way before it does.
    "window": (1640, 1000),
    "window_min": (1280, 860),
    # horizontal split: file queue | preview column | split settings column
    "queue_width": 330,
    "workspace_width": 820,
    "settings_width": 480,
    "queue_min": 300,
    "settings_min": 400,
    # vertical split inside the workspace: player | contact sheet | log.
    # The player keeps the lion's share (stretch 5 below), and the log strip is
    # collapsible to zero so a short window can hand everything to the picture.
    "player_height": 560,
    "wall_height": 300,
    "log_height": 40,
    "player_min": 300,
    # High enough that ThumbTile does not fall into "compact" mode, which hides
    # the per-frame timestamp under every thumbnail.
    "wall_min": 200,
    "log_min": 0,
    # outer split: (player + sheet + log) | split settings + segment table.
    # The settings block only needs ~400 px, so it gets little more than that.
    "top_height": 820,
    "bottom_height": 420,
    "split_min": 300,
    "split_height": 450,         # settings block inside the right-hand column
    "table_height": 460,         # segment table inside the right-hand column
    "seg_min": 220,
    # inner widgets
    "surface_min": 170,          # video surface: keeps the picture from collapsing
    "table_min": 96,
    "log_max": 62,               # <= 3 lines of log output
    "thumb_tile_min": 140,       # ThumbnailWall.TILE_MIN
    "thumb_tile_max": 340,       # ThumbnailWall.TILE_MAX
    "thumb_rows": 3,             # ThumbnailWall.MAX_ROWS
    "queue_row": 62,             # QueueDelegate.ROW_HEIGHT
    "body_font_px": 12,
}

# Compact action buttons (queue move / remove). Small on purpose: they are
# secondary to the video itself and each one explains itself on hover.
COMPACT_BTN_QSS = """
QPushButton {
  background:#16283f; border:1px solid #26456b; border-radius:6px;
  color:#dbe7f7; font-size:11px; padding:1px 8px; min-height:0;
}
QPushButton:hover { background:#1f3a5c; border-color:#63b3ed; }
QPushButton:pressed { background:#12233a; }
QPushButton:disabled { color:#5c6d85; background:#101c2c; border-color:#1b2f4a; }
"""
COMPACT_DANGER_QSS = COMPACT_BTN_QSS + """
QPushButton:hover { background:#5c2020; border-color:#a04040; }
"""


def compact_button(text: str, tip: str, danger: bool = False,
                   parent: QWidget | None = None) -> QPushButton:
    """Small secondary button with a loud hover note."""
    btn = QPushButton(text, parent)
    btn.setFixedHeight(24)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(COMPACT_DANGER_QSS if danger else COMPACT_BTN_QSS)
    btn.setToolTip(tip)
    loud_tooltip(btn)
    return btn


def _size(key: str) -> tuple[int, int]:
    return LAYOUT[key]


STYLE = """
QWidget { color:#dbe7f7; font-size:12px; }
QMainWindow, #Root { background:#08111d; }
QMenuBar { background:#0b1a2b; color:#cfe0f5; }
QMenuBar::item:selected { background:#1c3554; }
QMenu { background:#0e2036; color:#dbe7f7; border:1px solid #1d3350; }
QMenu::item:selected { background:#26456b; }
QGroupBox {
  background:rgba(12,26,44,0.75); border:1px solid #1b2f4a; border-radius:9px;
  margin-top:12px; padding:10px 10px 8px 10px; font-weight:600; color:#bcd6f5;
}
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 5px; }
QTableWidget, QListWidget, QPlainTextEdit, QTextEdit {
  background:#0a1727; border:1px solid #1b2f4a; border-radius:8px;
  gridline-color:#152a44; selection-background-color:#26456b;
  selection-color:#ffffff; alternate-background-color:#0c1e33;
}
QHeaderView::section {
  background:#102341; color:#9dbde0; border:0; border-right:1px solid #1b2f4a;
  padding:5px 6px; font-weight:600;
}
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
  background:#0d1e33; border:1px solid #24405f; border-radius:6px;
  padding:4px 7px; color:#dbe7f7; min-height:20px;
}
QComboBox::drop-down { border:0; width:18px; }
QComboBox QAbstractItemView {
  background:#0d1e33; border:1px solid #24405f; selection-background-color:#26456b;
}
QPushButton {
  background:#16283f; border:1px solid #26456b; border-radius:7px;
  padding:6px 12px; color:#dbe7f7; min-height:18px;
}
QPushButton:hover { background:#1f3a5c; border-color:#3b6ea8; }
QPushButton:pressed { background:#12233a; }
QPushButton:disabled { color:#5c6d85; background:#101c2c; border-color:#1b2f4a; }
QPushButton#Primary {
  background:%s; border:1px solid %s; color:#ffffff; font-weight:600;
}
QPushButton#Primary:hover { background:%s; }
QPushButton#Danger:hover { background:#5c2020; border-color:#a04040; }
QProgressBar {
  background:#0d1e33; border:1px solid #24405f; border-radius:6px;
  text-align:center; color:#dbe7f7; height:16px;
}
QProgressBar::chunk { background:%s; border-radius:5px; }
QCheckBox::indicator, QRadioButton::indicator { width:14px; height:14px; }
QCheckBox::indicator:unchecked { background:#0d1e33; border:1px solid #3b6ea8; border-radius:3px; }
QCheckBox::indicator:checked { background:%s; border:1px solid %s; border-radius:3px; }
QSplitter::handle { background:#0d1e33; }
QScrollBar:vertical { background:#0a1727; width:11px; margin:0; }
QScrollBar::handle:vertical { background:#26456b; border-radius:5px; min-height:24px; }
QScrollBar::handle:vertical:hover { background:#35608f; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
QScrollBar:horizontal { background:#0a1727; height:11px; margin:0; }
QScrollBar::handle:horizontal { background:#26456b; border-radius:5px; min-width:24px; }
QStatusBar { background:#0b1a2b; color:#9dbde0; }
QToolTip { background:#0e2036; color:#dbe7f7; border:1px solid #26456b; padding:4px; }
""" % (ACCENT, ACCENT_HI, ACCENT_HI, ACCENT, ACCENT, ACCENT_HI)


# --------------------------------------------------------------------------- #
# Background workers
# --------------------------------------------------------------------------- #

class FfmpegBootstrap(QThread):
    """Download FFmpeg on first run without freezing the UI."""

    progressed = Signal(str, float)
    finished_ok = Signal(str, str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            path = vt.download_ffmpeg(
                progress=lambda msg, frac: self.progressed.emit(msg, frac))
            found = vt.locate_ffmpeg(refresh=True)
            if not found:
                self.failed.emit("下载完成后仍找不到 FFmpeg")
                return
            self.finished_ok.emit(found[0], found[1])
        except Exception as exc:                     # noqa: BLE001
            self.failed.emit(str(exc))


class ProbeWorker(QThread):
    """Probe a batch of files (used by the queue)."""

    one = Signal(object)          # vt.MediaInfo
    done = Signal()

    def __init__(self, paths: list[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.paths = list(paths)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        for path in self.paths:
            if self._cancel:
                break
            self.one.emit(vt.probe(path))
        self.done.emit()


class ThumbWorker(QThread):
    """Extract preview frames + compose the contact sheet."""

    progressed = Signal(str, float)
    finished_ok = Signal(object)     # ThumbJob
    failed = Signal(str)

    def __init__(self, path: str, duration: float, count: int, columns: int,
                 tile_height: int, parent: QObject | None = None,
                 times: list[float] | None = None,
                 sheet_format: str = "png", quality: int = 92,
                 labels: bool = True) -> None:
        super().__init__(parent)
        self.path, self.duration = path, duration
        self.count, self.columns, self.tile_height = count, columns, tile_height
        self.times = times
        self.sheet_format, self.quality, self.labels = sheet_format, quality, labels
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            data = vt.generate_thumbnails(
                self.path, self.duration, count=self.count, columns=self.columns,
                tile_height=self.tile_height, times=self.times,
                sheet_format=self.sheet_format, sheet_quality=self.quality,
                include_timeline_labels=self.labels,
                progress=lambda m, f: self.progressed.emit(m, f),
                cancel=lambda: self._cancel,
            )
            self.finished_ok.emit(ThumbJob(
                frames=data["frames"], times=data["times"],
                sheet=data["sheet"], columns=data["columns"]))
        except Exception as exc:                     # noqa: BLE001
            self.failed.emit(str(exc))


class SplitWorker(QThread):
    """Runs the whole batch: every queued file, every segment, in order."""

    progressed = Signal(str, float)
    file_done = Signal(str, bool, str)      # output path, ok, message
    all_done = Signal(int, int)             # ok count, fail count

    def __init__(self, jobs: list["QueueItem"], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self._cancel = False
        self.ok = 0
        self.fail = 0
        self.outputs: list[str] = []

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total_jobs = max(1, len(self.jobs))
        for job_index, item in enumerate(self.jobs):
            if self._cancel:
                break
            info = item.info
            if info is None or info.error:
                self.fail += 1
                self.file_done.emit("", False, f"{os.path.basename(item.path)}：{info.error if info else '未读取到信息'}")
                continue
            segments = item.segments
            if not segments:
                self.fail += 1
                self.file_done.emit("", False, f"{os.path.basename(item.path)}：没有可用的拆分片段")
                continue

            out_dir = vt.build_output_dir(item.path, item.settings.output_dir,
                                          item.settings.subfolder,
                                          mode=item.settings.out_mode())
            base = vt.safe_filename(os.path.splitext(os.path.basename(item.path))[0])
            fmt = item.settings.container
            opts = vt.CutOptions(
                reencode=(fmt == "reencode"),
                video_codec=item.settings.video_codec,
                preset=item.settings.preset,
                crf=item.settings.crf,
                audio_codec=item.settings.audio_codec,
                audio_bitrate=item.settings.audio_bitrate,
            )
            ext = ".mp4" if fmt in ("mp4", "reencode") else (".mkv" if fmt == "mkv" else
                                                             os.path.splitext(item.path)[1])

            def on_file(path: str, _ji=job_index) -> None:
                self.outputs.append(path)

            def report(msg: str, frac: float, _ji=job_index) -> None:
                overall = (_ji + max(0.0, min(1.0, frac))) / total_jobs
                self.progressed.emit(msg, overall)

            results = vt.cut_all(
                item.path, segments, out_dir, base, ext=ext, opts=opts,
                progress=report, cancel=lambda: self._cancel, on_file=on_file,
                start_index=item.settings.start_index,
                pad=item.settings.pad,
                smart=item.settings.smart and fmt != "reencode",
                reencode_opts=vt.CutOptions(
                    reencode=True,
                    video_codec=item.settings.video_codec,
                    preset=item.settings.preset,
                    crf=item.settings.crf,
                    audio_codec=item.settings.audio_codec,
                    audio_bitrate=item.settings.audio_bitrate,
                ))

            for _seg, out_path, err in results:
                if err:
                    self.fail += 1
                    self.file_done.emit(out_path, False, err)
                else:
                    self.ok += 1
                    self.file_done.emit(out_path, True, "")

        self.all_done.emit(self.ok, self.fail)


class KeyframeWorker(QThread):
    """Load keyframe timestamps (needed by the keyframe-exact mode)."""

    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            keys = vt.list_keyframes(self.path)
            if not self._cancel:
                self.finished_ok.emit(keys)
        except Exception as exc:                     # noqa: BLE001
            self.failed.emit(str(exc))


class PreviewSaveWorker(QThread):
    """Copy preview frames out as `<视频名>_缩略图.png` (see vt.PREVIEW_NUMBERING)."""

    progressed = Signal(str, float)
    finished_ok = Signal(list, str)      # [(path, time)], folder
    failed = Signal(str)

    def __init__(self, src: str, frames: list[str], times: list[float],
                 out_dir: str, parent: QObject | None = None,
                 pad: int = 3, start_index: int = 1,
                 numbered: bool = vt.PREVIEW_NUMBERING) -> None:
        super().__init__(parent)
        self.src = src
        self.frames = list(frames)
        self.times = list(times)
        self.out_dir = out_dir
        self.pad = max(1, int(pad))
        self.start_index = max(1, int(start_index))
        self.numbered = bool(numbered)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            saved: list[tuple[str, float]] = []
            total = max(1, len(self.frames))
            # A single frame gets the plain `a_缩略图.png`; several frames cannot
            # all share that name, so they keep numbers instead of turning into
            # " (2)", " (3)" copies of each other.
            use_numbers = self.numbered or total > 1
            start = self.start_index if use_numbers else 1
            for index, frame in enumerate(self.frames):
                if self._cancel:
                    break
                self.progressed.emit(f"保存预览图 {index + 1}/{total}", index / total)
                if not os.path.isfile(frame):
                    continue
                out = os.path.join(self.out_dir, vt.preview_frame_name(
                    self.src, start + index, self.pad, numbered=use_numbers))
                out = vt.unique_path(out)
                shutil.copyfile(frame, out)
                saved.append((out, self.times[index] if index < len(self.times) else 0.0))
            self.finished_ok.emit(saved, self.out_dir)
        except Exception as exc:                     # noqa: BLE001
            self.failed.emit(str(exc))


# --------------------------------------------------------------------------- #
# Queue model + list rendering
# --------------------------------------------------------------------------- #

@dataclass
class SplitSettings:
    mode: str = vt.MODE_BY_SIZE
    segment_seconds: float = 60.0
    part_count: int = 2
    target_mb: float = 100.0
    container: str = "mp4"          # mp4 | mkv | reencode
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 20
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    output_dir: str = DEFAULT_OUTPUT
    subfolder: bool = True
    save_beside: bool = True        # write next to the source video instead
    preview_beside: bool = True     # preview frames straight beside the video
                                    # (no folder); set False for the 视频名 folder
    start_index: int = 1            # first output file number
    pad: int = 3                    # digits in the auto-generated number
    keep_source: bool = True        # never touched; shown for reassurance
    on_conflict: str = "rename"     # rename | overwrite | skip
    smart: bool = True              # re-encode segments that miss a keyframe

    def out_mode(self) -> str:
        return "beside" if self.save_beside else "root"


@dataclass
class QueueItem:
    path: str
    info: vt.MediaInfo | None = None
    settings: SplitSettings = field(default_factory=SplitSettings)
    segments: list[vt.SplitSegment] = field(default_factory=list)
    keyframes: list[float] | None = None
    status: str = "等待"
    poster: str | None = None
    preview_count: int = 0          # frames saved as image files for this video

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


class QueueDelegate(QStyledItemDelegate):
    """Row renderer: preview still + name + source folder + metadata + status."""

    ROW_HEIGHT = 78

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        item: QueueItem | None = index.data(Qt.UserRole)
        if item is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        source_dir = index.data(Qt.UserRole + 3) or os.path.dirname(item.path)

        bg = QColor("#1b2f4a" if selected else ("#122438" if hovered else "#0c1c2e"))
        painter.setPen(QPen(QColor("#3f6ea8" if selected else "#1b2f4a"), 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 8, 8)

        current = index.data(Qt.UserRole + 1)
        if current:
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)

        # --- preview thumbnail ---
        thumb = QRect(rect.left() + 7, rect.top() + 7, 84, rect.height() - 14)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#08111d"))
        painter.drawRoundedRect(thumb, 5, 5)
        pm: QPixmap | None = index.data(Qt.UserRole + 2)
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(thumb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap(
                thumb.left() + (thumb.width() - scaled.width()) // 2,
                thumb.top() + (thumb.height() - scaled.height()) // 2, scaled)
        else:
            painter.setPen(QColor("#3d5a7c"))
            painter.drawText(thumb, Qt.AlignCenter, "▶")

        # --- texts ---
        text_left = thumb.right() + 9
        right_edge = rect.right() - 6
        text_w = right_edge - text_left
        if text_w < 20:
            painter.restore()
            return
        info = item.info
        painter.setPen(QColor("#eaf2ff" if not selected else "#ffffff"))
        f = QFont(option.font)
        f.setPointSizeF(max(9.0, f.pointSizeF()))
        f.setBold(True)
        painter.setFont(f)
        name_rect = QRect(text_left, rect.top() + 5, text_w, 17)
        metrics = QFontMetrics(f)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         metrics.elidedText(item.name, Qt.ElideMiddle, name_rect.width()))

        # --- source folder badge: makes a mixed queue obvious at a glance ---
        folder_rect = QRect(text_left, rect.top() + 23, text_w, 17)
        self._paint_folder_badge(painter, folder_rect, source_dir, option.font)

        f2 = QFont(option.font)
        f2.setPointSizeF(max(8.0, f2.pointSizeF() - 0.5))
        painter.setFont(f2)
        painter.setPen(QColor("#93b4d8"))
        if info and not info.error:
            detail = (f"{vt.human_time(info.duration)}  ·  {info.resolution_text}  ·  "
                      f"{info.size_text}  ·  {len(item.segments)} 段")
        elif info and info.error:
            detail = f"⚠ {info.error}"
        else:
            detail = "读取中 …"
        detail_rect = QRect(text_left, rect.top() + 42, text_w, 16)
        painter.drawText(detail_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(f2).elidedText(detail, Qt.ElideRight, detail_rect.width()))

        if item.segments:
            est = sum(s.estimated_bytes for s in item.segments)
            extra = f"预计输出 ≈ {vt.human_size(est)}  ·  {item.status}"
        else:
            extra = item.status
        painter.setPen(QColor("#6f93ba"))
        f3 = QFont(option.font)
        f3.setPointSizeF(max(7.5, f3.pointSizeF() - 1.5))
        painter.setFont(f3)
        painter.drawText(QRect(text_left, rect.top() + 58, text_w, 14),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(f3).elidedText(extra, Qt.ElideRight, text_w))
        painter.restore()

    @staticmethod
    def _paint_folder_badge(painter: QPainter, rect: QRect, folder: str,
                            base_font: QFont) -> None:
        """A small '📁 父文件夹\\' chip in front of the row's metadata."""
        name = os.path.basename(folder.rstrip("\\/")) or folder
        parent = os.path.basename(os.path.dirname(folder.rstrip("\\/")))
        label = f"{parent}\\{name}" if parent else name
        f = QFont(base_font)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.0))
        painter.setFont(f)
        metrics = QFontMetrics(f)
        text = metrics.elidedText(label, Qt.ElideLeft, max(40, rect.width() - 16))
        chip = QRect(rect.left(), rect.top(),
                     min(rect.width(), metrics.horizontalAdvance(text) + 10), rect.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#173356"))
        painter.drawRoundedRect(chip, 5, 5)
        painter.setPen(QColor("#8fc0f0"))
        painter.drawText(chip.adjusted(5, 0, -4, 0),
                         Qt.AlignLeft | Qt.AlignVCenter, "📁 " + text)


# --------------------------------------------------------------------------- #
# Split settings panel
# --------------------------------------------------------------------------- #

class SplitPanel(QWidget):
    """Mode picker + parameters + segment preview + export controls."""

    planChanged = Signal()
    startRequested = Signal()
    cancelRequested = Signal()
    applyToAllRequested = Signal()
    previewPointRequested = Signal(float)
    openOutputRequested = Signal()
    previewModeChanged = Signal()        # 预览图文件夹 / 直接放视频旁 切换

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        # The queue's selected item, so the path hints can show real folders.
        self._current_item: QueueItem | None = None

        # ---------------- mode ---------------- #
        mode_box = QGroupBox("拆分方式")
        grid = QGridLayout(mode_box)
        grid.setContentsMargins(10, 6, 10, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)

        self.rb_size = QRadioButton("按目标大小（MB）自动分段")
        self.rb_time = QRadioButton("按时长（每段秒数）")
        self.rb_count = QRadioButton("平均分成 N 段")
        self.rb_keyframe = QRadioButton("按关键帧拆分（最精确，适合无损剪切）")
        self.rb_custom = QRadioButton("自定义区间（用播放器 ✂ 记录拆分点）")
        self.rb_size.setChecked(True)
        for i, rb in enumerate((self.rb_size, self.rb_time, self.rb_count,
                                self.rb_keyframe, self.rb_custom)):
            grid.addWidget(rb, i // 2, i % 2)

        params = QHBoxLayout()
        params.setSpacing(8)
        self.lbl_size = QLabel("每段大小")
        self.sp_size = QDoubleSpinBox()
        self.sp_size.setRange(1, 102400)
        self.sp_size.setValue(100)
        self.sp_size.setSuffix(" MB")
        self.sp_size.setDecimals(0)
        self.lbl_time = QLabel("每段时长")
        self.sp_time = QDoubleSpinBox()
        self.sp_time.setRange(1, 86400)
        self.sp_time.setValue(60)
        self.sp_time.setSuffix(" 秒")
        self.lbl_count = QLabel("段数")
        self.sp_count = QSpinBox()
        self.sp_count.setRange(2, 9999)
        self.sp_count.setValue(2)
        self.chk_sub = QCheckBox("每段单独输出到子文件夹")
        self.chk_sub.setChecked(True)
        for w in (self.lbl_size, self.sp_size, self.lbl_time, self.sp_time,
                  self.lbl_count, self.sp_count):
            params.addWidget(w)
        params.addStretch(1)
        params.addWidget(self.chk_sub)
        grid.addLayout(params, 3, 0, 1, 2)

        # ---------------- output + codec ---------------- #
        # Laid out as a 4-column grid: the wide cells hold path / format text
        # while the narrow ones hold buttons and spin boxes. Keeping every row
        # short is what lets the whole settings block fit above the segment
        # table without Qt squeezing the rows into each other.
        out_box = QGroupBox("输出设置")
        og = QGridLayout(out_box)
        og.setContentsMargins(10, 6, 10, 8)
        og.setHorizontalSpacing(6)
        og.setVerticalSpacing(4)
        # The output path is the one field people keep an eye on, so it gets the
        # loud treatment: strong border, bold text, full paths on hover.
        self.ed_output = QLineEdit(DEFAULT_OUTPUT)
        self.ed_output.setMinimumHeight(26)
        self.ed_output.setStyleSheet(
            "QLineEdit{background:#0f2440;border:1px solid #4a86c8;border-radius:7px;"
            "padding:2px 8px;color:#ffffff;font-size:12px;font-weight:600;"
            "selection-background-color:#35608f;min-height:22px;}"
            "QLineEdit:hover{border-color:#7c5cff;background:#122b4c;}"
            "QLineEdit:focus{border:2px solid #9b83ff;}"
            "QLineEdit:disabled{color:#93a9c4;background:#0c1b2e;border-color:#24405f;}")
        self.cmb_container = QComboBox()
        self.cmb_container.addItems([
            "不重新编码（MP4 快速无损切）", "不重新编码（MKV 无损）", "重新编码（精确到帧）",
        ])
        self.cmb_container.setToolTip(
            "无损切分不重新编码，速度极快；由于必须从关键帧开始，实际长度可能与设定略有偏差。\n"
            "重新编码可以精确到帧，但速度慢且画质有损。")

        self.rb_beside = QRadioButton("输出到源文件旁的新文件夹（名称 = 视频文件名）")
        self.rb_beside.setChecked(True)
        self.rb_beside.setToolTip(
            "C:\\视频\\我的片子.mp4\n"
            "  → C:\\视频\\我的片子\\拆分片段\\我的片子_001.mp4\n"
            "  → C:\\视频\\我的片子\\我的片子_缩略图_001.png（预览图）\n\n"
            "源文件在哪，结果就在哪，不受下面的目录设置影响。")
        self.rb_root = QRadioButton("输出到下面指定的目录")
        self.rb_root.setToolTip(
            "写到指定目录，并按视频文件名建一层子文件夹：\n"
            "  目录\\我的片子\\我的片子_001.mp4")
        og.addWidget(self.rb_beside, 0, 0, 1, 4)
        og.addWidget(self.rb_root, 1, 0, 1, 4)

        self.btn_output = compact_button(
            "选择目录…", "选择目录\n挑一个用来存放拆分片段的文件夹（仅在「输出到指定目录」时生效）。")
        self.btn_open = compact_button(
            "打开目录", "打开目录\n在资源管理器里打开当前的输出目录。")
        og.addWidget(self.ed_output, 2, 0, 1, 2)
        og.addWidget(self.btn_output, 2, 2)
        og.addWidget(self.btn_open, 2, 3)

        # Preview frames: straight next to the video (default) or collected in a
        # folder named after it. The old layout defaulted to the folder, so a
        # 📷 click looked like it ignored the "输出到源文件旁" choice above.
        self.rb_prev_beside = QRadioButton("预览图直接放视频旁（不建文件夹）")
        self.rb_prev_folder = QRadioButton("预览图收进「视频名」文件夹")
        self.rb_prev_beside.setChecked(True)
        self.rb_prev_beside.setToolTip(
            "不建任何文件夹，预览图直接落在源视频旁边：\n"
            "  C:\\视频\\我的片子_缩略图_001.png\n\n"
            "点「视频合集预览图」那一行的 📷 就存成这个位置；\n"
            "同一个视频再点一次会接着编号（_004、_005…），不会覆盖旧图。")
        self.rb_prev_folder.setToolTip(
            "预览图收在源视频旁的一个文件夹里：\n"
            "  C:\\视频\\我的片子\\我的片子_缩略图_001.png\n\n"
            "同一个视频截很多次也不会把源文件夹弄乱。")
        prev_row = QHBoxLayout()
        prev_row.setSpacing(14)
        prev_row.addWidget(QLabel("预览图存到"))
        prev_row.addWidget(self.rb_prev_beside)
        prev_row.addWidget(self.rb_prev_folder)
        prev_row.addStretch(1)
        og.addLayout(prev_row, 3, 0, 1, 4)

        og.addWidget(QLabel("格式"), 4, 0)
        og.addWidget(self.cmb_container, 4, 1, 1, 3)

        self.lbl_out_hint = ElidedLabel()
        self.lbl_out_hint.setStyleSheet("color:#9dbde0;font-size:11px;")
        og.addWidget(self.lbl_out_hint, 5, 0, 1, 4)

        # the two reassurance checkboxes share the naming row: a full extra row
        # each would push the settings block past the space the panel has
        self.chk_keep = QCheckBox("保留源文件")
        self.chk_keep.setChecked(True)
        self.chk_keep.setToolTip(
            "本程序始终只读取源文件，拆分结果写入输出目录；\n"
            "勾选仅作为确认，取消勾选也不会删除原文件。")
        self.chk_keep.setEnabled(False)
        self.chk_smart = QCheckBox("关键帧不齐时自动重新编码")
        self.chk_smart.setChecked(True)
        self.chk_smart.setToolTip(
            "无损切分只能从关键帧开始。若某段的起点正好落在两个关键帧之间，\n"
            "FFmpeg 会顺延到下一个关键帧，导致该段变短甚至生成空文件。\n"
            "勾选后这些片段会自动改用重新编码，长度就准了（速度慢一些）。")

        # Naming on one row, the two reassurance checkboxes on the next: packing
        # all four into a single row made the right-hand end collide with the
        # panel border in the 580 px settings column (measured, not guessed).
        naming = QHBoxLayout()
        naming.setSpacing(8)
        naming.addWidget(QLabel("序号起始"))
        self.sp_start = QSpinBox()
        self.sp_start.setRange(1, 999999)
        self.sp_start.setValue(1)
        self.sp_start.setToolTip("输出文件名的起始序号，例如起于 1 得到 名称_001.mp4")
        self.sp_start.setFixedWidth(90)
        naming.addWidget(self.sp_start)
        naming.addSpacing(8)
        naming.addWidget(QLabel("位数"))
        self.sp_pad = QSpinBox()
        self.sp_pad.setRange(1, 8)
        self.sp_pad.setValue(3)
        self.sp_pad.setToolTip("序号补零位数，例如 3 位得到 名称_001.mp4")
        self.sp_pad.setFixedWidth(64)
        naming.addWidget(self.sp_pad)
        naming.addStretch(1)
        og.addLayout(naming, 6, 0, 1, 4)

        reassure = QHBoxLayout()
        reassure.setSpacing(18)
        reassure.addWidget(self.chk_keep)
        reassure.addWidget(self.chk_smart)
        reassure.addStretch(1)
        og.addLayout(reassure, 7, 0, 1, 4)
        og.setColumnStretch(1, 1)

        # The mode box and the output box live in one container so the right-hand
        # column can stack it above the segment table with a draggable divider.
        self.settings_box = QWidget(self)
        settings_col = QVBoxLayout(self.settings_box)
        settings_col.setContentsMargins(0, 0, 0, 0)
        settings_col.setSpacing(6)
        settings_col.addWidget(mode_box)
        settings_col.addWidget(out_box)
        root.addWidget(self.settings_box)

        # ---------------- segment table ---------------- #
        seg_box = QGroupBox("拆分预览 / 输出队列")
        self.seg_box = seg_box
        sg = QVBoxLayout(seg_box)
        sg.setContentsMargins(10, 6, 10, 8)
        sg.setSpacing(6)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["#", "区间", "时长", "预计大小", "输出文件"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setMinimumHeight(96)
        sg.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_apply_all = compact_button(
            "应用到整个队列",
            "应用到整个队列\n把当前的拆分设置复制给队列里的每一个视频。")
        self.btn_preview = QPushButton("生成分段预览")
        self.btn_preview.setObjectName("Primary")
        self.btn_preview.setFixedHeight(26)
        self.btn_preview.setToolTip("生成分段预览\n按当前设置算出每一段，先看结果再决定是否拆分。")
        self.btn_start = QPushButton("开始拆分")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setFixedHeight(26)
        self.btn_start.setToolTip(
            "开始拆分\n对队列里所有已读取完成的视频依次执行拆分。\n"
            "无损模式直接复制流，速度接近磁盘上限。")
        self.btn_cancel = compact_button(
            "停止", "停止\n当前片段完成后停止本次批量拆分。", danger=True)
        self.btn_cancel.setEnabled(False)
        self.lbl_summary = QLabel("—")
        self.lbl_summary.setStyleSheet("color:#8fb2d8;")
        actions.addWidget(self.btn_apply_all)
        actions.addWidget(self.btn_preview)
        actions.addStretch(1)
        actions.addWidget(self.lbl_summary)
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_start)
        sg.addLayout(actions)
        root.addWidget(seg_box, 1)

        # ---------------- wiring ---------------- #
        for rb in (self.rb_size, self.rb_time, self.rb_count, self.rb_keyframe,
                   self.rb_custom):
            rb.toggled.connect(self._sync_mode)
        self.sp_size.valueChanged.connect(self.planChanged.emit)
        self.sp_time.valueChanged.connect(self.planChanged.emit)
        self.sp_count.valueChanged.connect(self.planChanged.emit)
        self.sp_start.valueChanged.connect(self.planChanged.emit)
        self.sp_pad.valueChanged.connect(self.planChanged.emit)
        self.cmb_container.currentIndexChanged.connect(self.planChanged.emit)
        self.btn_output.clicked.connect(self._pick_output)
        self.btn_open.clicked.connect(self.openOutputRequested.emit)
        self.btn_preview.clicked.connect(self.planChanged.emit)
        self.btn_start.clicked.connect(self.startRequested.emit)
        self.btn_cancel.clicked.connect(self.cancelRequested.emit)
        self.btn_apply_all.clicked.connect(self.applyToAllRequested.emit)
        self.table.cellDoubleClicked.connect(self._on_cell)
        self.rb_beside.toggled.connect(self._sync_out_mode)
        self.rb_prev_beside.toggled.connect(self._preview_mode_changed)
        self.chk_sub.toggled.connect(self._update_out_hint)
        self.ed_output.textChanged.connect(self._update_out_hint)
        self.sp_pad.valueChanged.connect(self._update_out_hint)
        self._sync_mode()
        self._sync_out_mode()

    def _preview_mode_changed(self) -> None:
        self._update_out_hint()
        self.previewModeChanged.emit()

    def capture_tooltip(self) -> str:
        """Tooltip for the preview window's 📷 button (depends on this setting)."""
        sample = vt.preview_frame_sample("我的片子.mp4", max(1, int(self.sp_pad.value())))
        where = ("源文件夹\\" + sample if self.preview_beside()
                 else f"源文件夹\\我的片子\\{sample}")
        return ("把这一批预览图存成图片文件\n"
                f"命名：{sample}\n"
                f"位置：{where}")

    # ------------------------------------------------------------------ #
    def _sync_mode(self) -> None:
        mode = self.mode()
        self.lbl_size.setVisible(mode == vt.MODE_BY_SIZE)
        self.sp_size.setVisible(mode == vt.MODE_BY_SIZE)
        self.lbl_time.setVisible(mode == vt.MODE_BY_DURATION)
        self.sp_time.setVisible(mode == vt.MODE_BY_DURATION)
        self.lbl_count.setVisible(mode == vt.MODE_BY_COUNT)
        self.sp_count.setVisible(mode == vt.MODE_BY_COUNT)
        self.planChanged.emit()

    def mode(self) -> str:
        if self.rb_time.isChecked():
            return vt.MODE_BY_DURATION
        if self.rb_count.isChecked():
            return vt.MODE_BY_COUNT
        if self.rb_keyframe.isChecked():
            return vt.MODE_BY_KEYFRAME
        if self.rb_custom.isChecked():
            return vt.MODE_CUSTOM
        return vt.MODE_BY_SIZE

    def settings(self) -> SplitSettings:
        container = ["mp4", "mkv", "reencode"][max(0, self.cmb_container.currentIndex())]
        return SplitSettings(
            mode=self.mode(),
            segment_seconds=float(self.sp_time.value()),
            part_count=int(self.sp_count.value()),
            target_mb=float(self.sp_size.value()),
            container=container,
            output_dir=self.ed_output.text().strip() or DEFAULT_OUTPUT,
            subfolder=self.chk_sub.isChecked(),
            save_beside=self.rb_beside.isChecked(),
            preview_beside=self.rb_prev_beside.isChecked(),
            start_index=int(self.sp_start.value()),
            pad=int(self.sp_pad.value()),
            keep_source=True,
            smart=self.chk_smart.isChecked(),
        )

    def load_settings(self, s: SplitSettings) -> None:
        mapping = {
            vt.MODE_BY_SIZE: self.rb_size, vt.MODE_BY_DURATION: self.rb_time,
            vt.MODE_BY_COUNT: self.rb_count, vt.MODE_BY_KEYFRAME: self.rb_keyframe,
            vt.MODE_CUSTOM: self.rb_custom,
        }
        button = mapping.get(s.mode, self.rb_size)
        if not button.isChecked():
            button.setChecked(True)
        for widget, value in ((self.sp_time, s.segment_seconds),
                              (self.sp_count, s.part_count),
                              (self.sp_size, s.target_mb),
                              (self.sp_start, s.start_index),
                              (self.sp_pad, s.pad)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.cmb_container.blockSignals(True)
        self.cmb_container.setCurrentIndex(
            {"mp4": 0, "mkv": 1, "reencode": 2}.get(s.container, 0))
        self.cmb_container.blockSignals(False)
        self.ed_output.setText(s.output_dir)
        self.chk_sub.setChecked(s.subfolder)
        self.rb_beside.setChecked(bool(s.save_beside))
        self.rb_root.setChecked(not s.save_beside)
        self.rb_prev_beside.setChecked(bool(s.preview_beside))
        self.rb_prev_folder.setChecked(not s.preview_beside)
        self._sync_out_mode()

    def preview_beside(self) -> bool:
        """True -> preview frames land straight beside the video (no folder)."""
        return self.rb_prev_beside.isChecked()

    def set_current_item(self, item: "QueueItem | None") -> None:
        """Called when the queue selection changes: refresh the path tooltips."""
        self._current_item = item
        self._update_out_hint()

    # ------------------------------------------------------------------ #
    def _sync_out_mode(self) -> None:
        """Beside-mode ignores the folder field, so grey it out and say why."""
        beside = self.rb_beside.isChecked()
        self.ed_output.setEnabled(not beside)
        self.btn_output.setEnabled(not beside)
        self._update_out_hint()

    def _update_out_hint(self) -> None:
        """
        Keep the loud path box readable: it shows the effective folder, and its
        tooltip lists every folder that will actually be written for the video
        currently selected in the queue.

        Deliberately free of side effects (no folder creation) because it runs on
        every keystroke in the path field.
        """
        sample = vt.preview_frame_sample("我的片子.mp4", max(1, int(self.sp_pad.value())))
        pad = max(1, int(self.sp_pad.value()))
        start = max(1, int(self.sp_start.value()))
        if self.rb_beside.isChecked():
            sub = f"{vt.OUTPUT_AUTO_SUBFOLDER}\\" if self.chk_sub.isChecked() else ""
            where = f"源文件夹\\我的片子\\{sub}"
            self.lbl_out_hint.setText(
                f"拆分片段实际写到：{where}我的片子_{start:0{pad}d}.mp4")
        else:
            root = self.ed_output.text().strip() or DEFAULT_OUTPUT
            sub = "我的片子\\" if self.chk_sub.isChecked() else ""
            where = os.path.join(root, sub)
            self.lbl_out_hint.setText(
                f"拆分片段实际写到：{where}我的片子_{start:0{pad}d}.mp4")

        preview_where = ("源文件夹\\" + sample if self.preview_beside()
                         else f"源文件夹\\我的片子\\{sample}")
        lines = [
            "输出路径说明",
            "",
            f"拆分片段：{where}",
            f"预览图　：{preview_where}",
        ]
        item = self._current_item
        if item is not None:
            src = item.path
            base = vt.safe_filename(os.path.splitext(os.path.basename(src))[0])
            src_dir = os.path.dirname(os.path.abspath(src))
            if self.rb_beside.isChecked():
                out = os.path.join(src_dir, base)
                if self.chk_sub.isChecked():
                    out = os.path.join(out, vt.OUTPUT_AUTO_SUBFOLDER)
            else:
                root = self.ed_output.text().strip() or DEFAULT_OUTPUT
                out = os.path.join(root, base) if self.chk_sub.isChecked() else root
            prev = (src_dir if self.preview_beside()
                    else os.path.join(src_dir, base))
            lines += [
                "",
                "当前选中视频的实际路径：",
                f"源文件　　：{src}",
                f"拆分片段　：{out}",
                f"预览图　　：{prev}",
            ]
        text = "\n".join(lines)
        self.ed_output.setToolTip(text)
        self.lbl_out_hint.setToolTip(text)

    def output_sample(self, src: str | None = None) -> str:
        """Human-readable output folder for the current settings (queue preview)."""
        settings = self.settings()
        source = src or os.path.join("C:\\", "视频", "我的片子.mp4")
        folder = vt.build_output_dir(source, settings.output_dir, settings.subfolder,
                                      mode=settings.out_mode())
        base = vt.safe_filename(os.path.splitext(os.path.basename(source))[0])
        ext = (".mp4" if settings.container in ("mp4", "reencode")
               else ".mkv" if settings.container == "mkv"
               else os.path.splitext(source)[1] or ".mp4")
        return os.path.join(folder, f"{base}_{settings.start_index:0{max(1, settings.pad)}d}{ext}")

    def show_segments(self, segments: list[vt.SplitSegment], info=None,
                      settings: SplitSettings | None = None) -> None:
        self.table.setRowCount(0)
        total_bytes = 0
        for seg in segments:
            row = self.table.rowCount()
            self.table.insertRow(row)
            est = seg.estimated_bytes
            total_bytes += est
            out_name = ""
            if info is not None:
                start_index = settings.start_index if settings else 1
                pad = settings.pad if settings else 3
                container = settings.container if settings else "mp4"
                ext = (".mp4" if container in ("mp4", "reencode")
                       else ".mkv" if container == "mkv"
                       else (os.path.splitext(info.path)[1] or ".mp4"))
                base = vt.safe_filename(os.path.splitext(os.path.basename(info.path))[0])
                out_name = f"{base}_{start_index + seg.index - 1:0{max(1, pad)}d}{ext}"
            cells = [
                f"{seg.index}",
                seg.range_text,
                vt.human_time(seg.duration, True),
                vt.human_size(est) if est else "—",
                out_name,
            ]
            for col, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if col in (0, 2, 3):
                    cell.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, cell)
        if segments:
            self.lbl_summary.setText(
                f"{len(segments)} 段 · 共 {vt.human_time(sum(s.duration for s in segments))} "
                f"· 预计 ≈ {vt.human_size(total_bytes) if total_bytes else '—'}")
        else:
            self.lbl_summary.setText("没有可用的分段（请检查时长/参数）")

    def set_busy(self, busy: bool) -> None:
        self.btn_start.setEnabled(not busy)
        self.btn_preview.setEnabled(not busy)
        self.btn_apply_all.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)

    def _pick_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", self.ed_output.text())
        if folder:
            self.ed_output.setText(folder)

    def _on_cell(self, row: int, _col: int) -> None:
        item = self.table.item(row, 1)
        if item is None:
            return
        text = item.text()
        if "→" in text:
            start = vt.parse_time(text.split("→")[0])
            self.previewPointRequested.emit(start)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(make_app_icon())
        self.setMinimumSize(*_size("window_min"))
        self.resize(*_size("window"))
        self.setStyleSheet(STYLE)
        # Anywhere in the window is a valid drop target (see dragEnterEvent).
        self.setAcceptDrops(True)

        self.items: list[QueueItem] = []
        self.current: QueueItem | None = None
        self._posters: dict[str, QPixmap] = {}
        self._probe_worker: ProbeWorker | None = None
        self._thumb_worker: ThumbWorker | None = None
        self._split_worker: SplitWorker | None = None
        self._kf_worker: KeyframeWorker | None = None
        self._preview_worker: PreviewSaveWorker | None = None
        self._boot: FfmpegBootstrap | None = None
        self._ffmpeg_ready = False
        self._last_output_dirs: list[str] = []
        self._balancing = False          # re-entrancy guard for the height balancer
        self._preview_flat = True        # where the last 📷 run wrote its frames

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        # No header strip: the app name lives in the window title bar and the
        # two actions it used to hold are in the 文件 menu (添加视频 / 清空队列).
        self._make_log()                 # built here: it lives in the preview column
        self._make_status_row()          # status + progress, under the preview
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_queue_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.addWidget(self._build_settings_column())
        # queue | preview | settings — only the preview grows with the window.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([LAYOUT["queue_width"], LAYOUT["workspace_width"],
                           LAYOUT["settings_width"]])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setCollapsible(2, False)
        self._main_split = splitter
        outer.addWidget(splitter, 1)
        self._build_menu()
        self._build_shortcuts()
        self._sync_capture_tooltip()
        self._set_ui_enabled(False)
        QTimer.singleShot(0, self._balance_preview_height)
        QTimer.singleShot(60, self._ensure_ffmpeg)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._balance_preview_height()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _style_ffmpeg(self, state: str) -> None:
        """
        FFmpeg readout in the menu-bar corner.

        Painted as a pill (accent border + translucent fill) so it reads as a
        status chip rather than loose text - the same shape the old "＋ 添加视频"
        button had, and 11 px like every other piece of chrome on that row.
        """
        colours = {"ready": "#7ef0b0", "busy": "#ffd479", "fail": "#ff9b9b"}
        self.lbl_ffmpeg.setStyleSheet(
            "color:%s;background:rgba(124,92,255,0.22);border:1px solid %s;"
            "border-radius:7px;font-size:11px;font-weight:600;padding:2px 9px;"
            % (colours.get(state, colours["busy"]), ACCENT_HI))

    def _build_queue_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(LAYOUT["queue_min"])
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("视频队列")
        title.setStyleSheet("font-size:13px;font-weight:700;color:#eaf2ff;")
        self.lbl_queue = QLabel("0 个文件")
        self.lbl_queue.setStyleSheet("color:#7f9dc0;")
        head.addWidget(title)
        head.addWidget(self.lbl_queue)
        head.addStretch(1)
        lay.addLayout(head)

        # drop zone (visible while empty)
        self.drop = DropZoneHint()
        self.drop.pathsDropped.connect(self.add_paths)
        self.drop.clicked.connect(self.add_files_dialog)
        lay.addWidget(self.drop)
        self.drop.hide()

        self.list = QListWidget()
        self.list.setItemDelegate(QueueDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setSpacing(1)
        self.list.setAcceptDrops(True)
        self.list.viewport().setAcceptDrops(True)
        self.list.setDragDropMode(QAbstractItemView.DropOnly)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._queue_context_menu)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.installEventFilter(self)
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_up = compact_button(
            "↑ 上移", "上移\n把选中的视频在队列里往前排，先拆分。")
        self.btn_down = compact_button(
            "↓ 下移", "下移\n把选中的视频在队列里往后排。")
        self.btn_remove = compact_button(
            "移除", "移除\n把选中的视频从队列删掉（磁盘上的文件不动）。",
            danger=True)
        self.btn_up.clicked.connect(lambda: self._move_selection(-1))
        self.btn_down.clicked.connect(lambda: self._move_selection(1))
        self.btn_remove.clicked.connect(self.remove_selected)
        row.addWidget(self.btn_up)
        row.addWidget(self.btn_down)
        row.addStretch(1)
        row.addWidget(self.btn_remove)
        lay.addLayout(row)
        return panel

    def _build_preview_panel(self) -> QWidget:
        """
        Middle column: the video frame, the contact sheet and the log strip.

        This column now spans the full height of the window (the split settings
        used to sit under it and stole most of the vertical space), so the
        picture is the largest thing on screen.
        """
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.player = PlayerPanel()
        self.player.splitPointRequested.connect(self._on_split_point)
        self.wall = ThumbnailWall()
        self.wall.exportRequested.connect(self.export_sheet)
        self.wall.regenerateRequested.connect(self.generate_thumbs)
        self.wall.saveOneRequested.connect(self.save_single_frame)
        self.wall.captureRequested.connect(self.save_preview_frames)
        self.wall.tileClicked.connect(lambda t: self.player.seek(t))

        col = QSplitter(Qt.Vertical)
        col.addWidget(self.player)
        col.addWidget(self.wall)
        col.addWidget(self.log)          # log strip lives at the bottom of the column
        # The preview frame is the star: it gets the stretch, the sheet gives way
        # next, and the log strip (collapsible to zero) goes last.
        col.setStretchFactor(0, 5)
        col.setStretchFactor(1, 2)
        col.setStretchFactor(2, 0)
        col.setSizes([LAYOUT["player_height"], LAYOUT["wall_height"],
                      LAYOUT["log_height"]])
        col.setCollapsible(0, False)
        col.setCollapsible(1, False)
        col.setCollapsible(2, True)
        self.player.setMinimumHeight(LAYOUT["player_min"])
        self.wall.setMinimumHeight(LAYOUT["wall_min"])
        self._preview_split = col
        lay.addWidget(col, 1)
        lay.addWidget(self._status_box)
        return panel

    def _build_settings_column(self) -> QWidget:
        """
        Right-hand column: the split settings on top of the segment table.

        A splitter rather than a scroll area: with the preview next to it the
        column is now wide (400+ px), which is what the settings block needed
        all along, and a splitter lets the user trade space between the two
        halves instead of hiding one of them behind a scrollbar.
        """
        self.split_panel = SplitPanel()
        self.split_panel.setMinimumHeight(LAYOUT["split_min"])
        self.split_panel.planChanged.connect(self.refresh_plan)
        self.split_panel.startRequested.connect(self.start_split)
        self.split_panel.cancelRequested.connect(self.cancel_split)
        self.split_panel.applyToAllRequested.connect(self.apply_to_all)
        self.split_panel.previewPointRequested.connect(self.player.seek)
        self.split_panel.openOutputRequested.connect(self.open_output_dir)
        self.split_panel.previewModeChanged.connect(self._sync_capture_tooltip)

        column = QSplitter(Qt.Vertical)
        column.addWidget(self.split_panel.settings_box)
        column.addWidget(self.split_panel.seg_box)
        column.setStretchFactor(0, 1)
        column.setStretchFactor(1, 1)
        column.setSizes([LAYOUT["split_height"], LAYOUT["table_height"]])
        column.setCollapsible(0, False)
        column.setCollapsible(1, False)
        self.split_panel.seg_box.setMinimumHeight(LAYOUT["seg_min"])
        self._settings_split = column
        return column

    def _balance_preview_height(self) -> None:
        """
        Give the preview column the height it deserves.

        With the settings in their own column the preview now owns the full
        height, so this only has to keep the contact sheet and the log strip
        from eating into the video frame when the window gets short.
        """
        col = getattr(self, "_preview_split", None)
        if col is None or self._balancing or col.count() < 3:
            return
        self._balancing = True
        try:
            free = col.height() - 12
            log_h = min(LAYOUT["log_height"], max(0, free - LAYOUT["player_min"]
                                                 - LAYOUT["wall_min"]))
            rest = free - log_h
            # The wall keeps enough height to show each frame's timestamp: below
            # ~78 px per tile the caption is dropped, which loses the "when in
            # the video is this" readout people actually look at.
            wall_h = max(LAYOUT["wall_min"], int(rest * 0.36))
            player_h = max(LAYOUT["player_min"], rest - wall_h)
            col.setSizes([player_h, wall_h, log_h])
        finally:
            self._balancing = False

    def _style_progress(self) -> None:
        """
        Paint the percentage where it stays readable.

        The label sits centred, so it is white while the filled part reaches
        past the middle and light blue before that - otherwise the digits
        disappear into the accent chunk (or into the dark groove) at some point
        during every run.
        """
        span = self.progress.maximum() - self.progress.minimum()
        ratio = (self.progress.value() / span) if span > 0 else 0.0
        text = "#ffffff" if ratio >= 0.5 else "#cfe0f5"
        self.progress.setStyleSheet(
            "QProgressBar{background:#0d1e33;border:1px solid #35608f;"
            "border-radius:8px;color:%s;font-size:11px;font-weight:700;"
            "text-align:center;}"
            "QProgressBar::chunk{background:%s;border-radius:7px;}" % (text, ACCENT))

    def _make_status_row(self) -> None:
        """
        One compact status line for the whole window: free-text status on the
        left, the single progress bar on the right.

        It sits at the bottom of the preview column. (There used to be a second
        progress bar in a separate footer box - one is enough.)
        """
        self._status_box = QFrame()
        self._status_box.setObjectName("StatusRow")
        self._status_box.setStyleSheet(
            "#StatusRow{background:#0b1a2b;border:1px solid #1b2f4a;border-radius:8px;}")
        lay = QHBoxLayout(self._status_box)
        lay.setContentsMargins(10, 3, 10, 3)
        lay.setSpacing(8)

        self.lbl_status = QLabel("准备就绪")
        self.lbl_status.setStyleSheet("color:#cfe0f5;font-size:11px;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        # Show "45%" inside the bar: the bar alone gives no sense of how far a
        # 12 GB split has got. (An indeterminate bar, range 0/0, shows nothing.)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setAlignment(Qt.AlignCenter)
        self.progress.setToolTip(
            "总进度\n"
            "拆分 / 预览图保存的整体进度，条上显示百分比。\n"
            "FFmpeg 正在准备时显示为来回滚动的忙碌状态。")
        loud_tooltip(self.progress)
        self.progress.setFixedWidth(260)
        self.progress.setFixedHeight(16)
        self._style_progress()
        self.progress.valueChanged.connect(lambda _v: self._style_progress())
        lay.addWidget(self.lbl_status, 1)
        lay.addWidget(self.progress)

    def _make_log(self) -> None:
        """
        The run log lives in the preview column (not in the status strip): it is
        a collapsible strip that shows <= 3 lines, so a short window can give
        every pixel to the video frame. The status strip keeps the progress bar.
        """
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(LAYOUT["log_min"])
        self.log.setMaximumHeight(LAYOUT["log_max"])
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.log.setToolTip("运行日志（最多 3 行，超出可滚动；"
                           "拖动上方分隔条可以把它缩到最小）")
        loud_tooltip(self.log)
        self.log.setStyleSheet(
            "background:#081422;border:1px solid #16283f;color:#8fb2d8;"
            "font-family:Consolas,monospace;font-size:11px;")

    def _build_menu(self) -> None:
        bar = self.menuBar()
        bar.setStyleSheet("QMenuBar{background:#0b1a2b;color:#cfe0f5;}")

        # FFmpeg state sits centred on this row instead of taking space in the
        # header: `addWidget` on the menu bar puts it between the menus, and the
        # spacers on both sides keep it in the middle.
        self.lbl_ffmpeg = QLabel("FFmpeg 检测中 …")
        self.lbl_ffmpeg.setAlignment(Qt.AlignCenter)
        self._style_ffmpeg("busy")
        self.lbl_ffmpeg.setToolTip(
            "FFmpeg 状态\n"
            "就绪：可以开始读取、预览、拆分视频。\n"
            "检测中：程序正在自动寻找或下载 FFmpeg。\n"
            "获取失败：请手动安装，见「帮助 → 关于 / 使用说明」。")
        loud_tooltip(self.lbl_ffmpeg)

        m_file = bar.addMenu("文件(&F)")
        # These two used to be buttons in the header strip. The strip is gone,
        # so the queue actions live here where they are reachable by keyboard.
        act_add = QAction("添加视频…", self)
        act_add.setShortcut(QKeySequence("Ctrl+O"))
        act_add.setToolTip(
            "添加视频\n"
            "打开文件选择框，可以一次选多个视频；\n"
            "也可以直接把文件 / 文件夹拖进窗口任意位置。\n"
            "视频大小不限，小于 2GB 的短片同样可以直接拖入。")
        act_add.triggered.connect(self.add_files_dialog)
        act_clear = QAction("清空队列", self)
        act_clear.setToolTip(
            "清空队列\n"
            "把队列里的所有视频移除（不会删除任何文件），\n"
            "已完成的拆分结果保持在磁盘上。")
        act_clear.triggered.connect(self.clear_queue)
        act_out = QAction("选择输出目录…", self)
        act_out.triggered.connect(self.split_panel._pick_output)
        act_open = QAction("打开输出目录", self)
        act_open.triggered.connect(self.open_output_dir)
        act_quit = QAction("退出", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        for a in (act_add, act_clear, act_out, act_open):
            m_file.addAction(a)
        m_file.addSeparator()
        m_file.addAction(act_quit)

        m_play = bar.addMenu("播放(&P)")
        for label, key, fn in (
            ("播放 / 暂停", "Space", self.player.toggle),
            ("后退 5 秒", "Left", lambda: self.player.seek(self.player.position - 5)),
            ("前进 5 秒", "Right", lambda: self.player.seek(self.player.position + 5)),
            ("记录拆分点", "S", lambda: self._on_split_point(self.player.position)),
            ("全屏", "F11", self.player._toggle_fullscreen),
        ):
            act = QAction(label, self)
            act.setShortcut(QKeySequence(key))
            act.triggered.connect(fn)
            m_play.addAction(act)

        m_help = bar.addMenu("帮助(&H)")
        act_about = QAction("关于 / 使用说明", self)
        act_about.triggered.connect(self.show_about)
        act_ff = QAction("重新检测 FFmpeg", self)
        act_ff.triggered.connect(lambda: self._ensure_ffmpeg(force=True))
        m_help.addAction(act_ff)
        m_help.addAction(act_about)

        # FFmpeg state lives on the menu bar row rather than in the header: it is
        # a status readout, not an action, so it gets the bar's corner and a
        # 10 px font. (QMenuBar has no addWidget - the corner widget is the only
        # hook Qt offers here.)
        bar.setCornerWidget(self.lbl_ffmpeg, Qt.TopRightCorner)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("Delete"), self.list, activated=self.remove_selected)
        QShortcut(QKeySequence("Ctrl+A"), self.list,
                  activated=lambda: self.list.selectAll())

    # ------------------------------------------------------------------ #
    # FFmpeg bootstrap
    # ------------------------------------------------------------------ #
    def _ensure_ffmpeg(self, force: bool = False) -> None:
        found = vt.locate_ffmpeg(refresh=force)
        if found:
            self._ffmpeg_ready = True
            self.lbl_ffmpeg.setText("FFmpeg：就绪")
            self._style_ffmpeg("ready")
            self._log(f"已找到 FFmpeg：{found[0]}")
            self._set_ui_enabled(True)
            return

        self.lbl_ffmpeg.setText("FFmpeg：正在自动获取 …")
        self._style_ffmpeg("busy")
        self._log("未检测到 FFmpeg，开始自动下载静态版本到程序目录 …")
        self.progress.setRange(0, 0)
        self._boot = FfmpegBootstrap(self)
        self._boot.progressed.connect(self._on_boot_progress)
        self._boot.finished_ok.connect(self._on_boot_ok)
        self._boot.failed.connect(self._on_boot_failed)
        self._boot.start()

    def _on_boot_progress(self, msg: str, frac: float) -> None:
        self.lbl_status.setText(msg)
        if frac < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(frac * 1000))

    def _on_boot_ok(self, ffmpeg: str, ffprobe: str) -> None:
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self._ffmpeg_ready = True
        self.lbl_ffmpeg.setText("FFmpeg：已就绪（自动下载）")
        self._style_ffmpeg("ready")
        self.lbl_status.setText("FFmpeg 准备完成")
        self._log(f"FFmpeg 下载完成：{ffmpeg}")
        self._set_ui_enabled(True)

    def _on_boot_failed(self, message: str) -> None:
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.lbl_ffmpeg.setText("FFmpeg：获取失败")
        self._style_ffmpeg("fail")
        self._log(f"FFmpeg 自动获取失败：{message}")
        message_box(
            self, "需要 FFmpeg",
            "自动下载 FFmpeg 失败。\n\n请手动安装后重试：\n"
            "1. 访问 https://www.gyan.dev/ffmpeg/builds/ 下载 release-essentials 压缩包\n"
            "2. 解压得到 ffmpeg.exe / ffprobe.exe\n"
            "3. 把这两个文件放到下面的目录中：\n"
            f"{vt.BIN_DIR}\n\n"
            "或者把它们加入系统 PATH，然后点击「帮助 → 重新检测 FFmpeg」。",
            "warning", [("知道了", "accept")])

    # ------------------------------------------------------------------ #
    # queue handling
    # ------------------------------------------------------------------ #
    def _set_ui_enabled(self, enabled: bool) -> None:
        for widget in (self.list, self.split_panel):
            widget.setEnabled(enabled)

    def add_files_dialog(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(vt.VIDEO_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择视频文件", "", f"视频文件 ({patterns});;所有文件 (*.*)")
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths: list[str]) -> None:
        """
        Add files to the queue from a dialog or a drop.

        No size filter, ever: the queue takes a 5 MB clip and a 40 GB remux
        alike (the old builds and the packing tool both cared about size, the
        app itself has no reason to). A file whose extension is not on the
        video list still gets probed, and anything FFmpeg cannot open is
        reported in its queue row instead of being silently dropped.
        """
        if not self._ffmpeg_ready:
            notify(self, "请稍候", "FFmpeg 还在准备中，请稍后再添加视频。", "info")
            return
        found: list[str] = []
        for path in paths:
            if os.path.isdir(path):
                try:
                    entries = sorted(os.listdir(path))
                except OSError as exc:
                    self._log(f"无法读取文件夹 {path}：{exc}")
                    continue
                for entry in entries:
                    full = os.path.join(path, entry)
                    if (os.path.isfile(full)
                            and os.path.splitext(entry)[1].lower() in vt.VIDEO_EXTENSIONS):
                        found.append(full)
            elif os.path.isfile(path):
                found.append(path)
        existing = {os.path.normcase(os.path.abspath(i.path)) for i in self.items}
        new_paths = [p for p in found
                     if os.path.normcase(os.path.abspath(p)) not in existing]
        if not new_paths:
            if found:
                self._log("这些文件已经在队列里了，已跳过重复项")
            return

        settings = self.split_panel.settings()
        for path in new_paths:
            item = QueueItem(path=path, settings=SplitSettings(**vars(settings)))
            self.items.append(item)
            row = QListWidgetItem()
            row.setData(Qt.UserRole, item)
            self._decorate_row(row, item)
            row.setSizeHint(QSize(0, QueueDelegate.ROW_HEIGHT))
            self.list.addItem(row)
        self._log(f"已添加 {len(new_paths)} 个文件，开始读取信息 …")
        self._update_queue_header()
        if self.list.currentRow() < 0 and self.items:
            self.list.setCurrentRow(0)

        self._probe_worker = ProbeWorker(new_paths, self)
        self._probe_worker.one.connect(self._on_probed)
        self._probe_worker.done.connect(self._update_queue_header)
        self._probe_worker.start()

    def _decorate_row(self, row: QListWidgetItem, item: QueueItem) -> None:
        """Source folder label + tooltip so a mixed queue stays readable."""
        row.setData(Qt.UserRole + 3, os.path.dirname(item.path))
        row.setToolTip(self._row_tooltip(item))

    def _row_tooltip(self, item: QueueItem) -> str:
        src_dir = os.path.dirname(item.path)
        preview_dir = vt.build_preview_dir(item.path)
        out_dir = vt.build_output_dir(item.path, item.settings.output_dir,
                                      item.settings.subfolder,
                                      mode=item.settings.out_mode())
        return (f"文件：{item.path}\n"
                f"来源文件夹：{src_dir}\n\n"
                f"拆分片段 → {out_dir}\n"
                f"预览图 → {preview_dir}\\{vt.preview_frame_sample(item.name)}")

    def _refresh_row_tooltips(self) -> None:
        for i in range(self.list.count()):
            row = self.list.item(i)
            item = row.data(Qt.UserRole) if row else None
            if item is not None:
                self._decorate_row(row, item)

    def _on_probed(self, info: vt.MediaInfo) -> None:
        for row in range(self.list.count()):
            item: QueueItem = self.list.item(row).data(Qt.UserRole)
            if item and os.path.normcase(item.path) == os.path.normcase(info.path):
                item.info = info
                if info.error:
                    item.status = f"⚠ {info.error}"
                else:
                    item.status = "待拆分"
                self._make_poster(item, row)
                self.list.update(self.list.indexFromItem(self.list.item(row)))
                if item is self.current:
                    self.player.load(item.info, item.poster)
                    self.refresh_plan()
                    self.generate_thumbs()
                break

    def _make_poster(self, item: QueueItem, row: int) -> None:
        if not (item.info and item.info.duration > 0):
            return
        found = vt.locate_ffmpeg()
        if not found:
            return
        folder = os.path.join(vt.CACHE_DIR, "posters")
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, f"{vt.hashlib_key(item.path)}.jpg")
        if not os.path.isfile(out):
            at = min(max(1.0, item.info.duration * 0.08), max(0.0, item.info.duration - 0.5))
            vt.run_hidden([
                found[0], "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", item.path,
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", out,
            ], timeout=60)
        item.poster = out if os.path.isfile(out) else None
        pm = QPixmap(out) if item.poster else QPixmap()
        lst_item = self.list.item(row)
        if lst_item is not None:
            lst_item.setData(Qt.UserRole + 2, pm)

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self.items):
            return
        self.current = self.items[row]
        self.split_panel.load_settings(self.current.settings)
        self.split_panel.set_current_item(self.current)
        self.player.load(self.current.info, self.current.poster)
        self.wall.set_captures(self.current.preview_count)
        self.refresh_plan()
        if self.current.info and not self.current.info.error:
            if self.current.segments:
                self.split_panel.show_segments(self.current.segments, self.current.info,
                                               self.split_panel.settings())
            self.generate_thumbs()

    def _build_queue_menu(self) -> QMenu:
        """
        Build the queue's right-click menu (without showing it).

        Split out from `_queue_context_menu` so the entries can be asserted in
        the self-check without opening a modal popup - `QMenu.exec()` blocks and
        an unattended run would hang on it.
        """
        selected = len(self.list.selectedIndexes())
        act_add = QAction("添加视频…", self)
        act_add.setShortcut(QKeySequence("Ctrl+O"))
        act_add.setToolTip("添加视频\n打开文件选择框；也可以把文件直接拖进队列。")
        act_add.triggered.connect(self.add_files_dialog)

        act_remove = QAction(
            f"从队列移除（{selected} 个）" if selected > 1 else "从队列移除", self)
        act_remove.setToolTip("从队列移除\n只从列表里去掉，磁盘上的视频不会被删除。")
        act_remove.setEnabled(selected > 0)
        act_remove.triggered.connect(self.remove_selected)

        act_up = QAction("上移", self)
        act_up.setEnabled(selected > 0)
        act_up.triggered.connect(lambda: self._move_selection(-1))
        act_down = QAction("下移", self)
        act_down.setEnabled(selected > 0)
        act_down.triggered.connect(lambda: self._move_selection(1))

        act_reveal = QAction("打开源文件所在文件夹", self)
        act_reveal.setEnabled(self.current is not None)
        act_reveal.triggered.connect(self._open_current_folder)

        act_clear = QAction(f"清空队列（{len(self.items)} 个）", self)
        act_clear.setToolTip("清空队列\n移除全部视频（不删除任何文件）。")
        act_clear.setEnabled(bool(self.items))
        act_clear.triggered.connect(self.clear_queue)

        menu = QMenu(self.list)
        # A QMenu is its own top-level window, so it does not inherit the main
        # window's sheet - without this it pops up in the system light theme and
        # looks nothing like the rest of the app.
        menu.setStyleSheet(
            "QMenu{background:#0e2036;color:#dbe7f7;border:1px solid #26456b;"
            "border-radius:8px;padding:5px;}"
            "QMenu::item{padding:6px 24px 6px 12px;border-radius:6px;}"
            "QMenu::item:selected{background:#26456b;color:#ffffff;}"
            "QMenu::item:disabled{color:#6b7d95;}"
            "QMenu::separator{height:1px;background:#1d3350;margin:5px 8px;}")
        menu.addAction(act_add)
        menu.addSeparator()
        menu.addAction(act_remove)
        menu.addAction(act_up)
        menu.addAction(act_down)
        menu.addSeparator()
        menu.addAction(act_reveal)
        menu.addSeparator()
        menu.addAction(act_clear)
        return menu

    def _queue_context_menu(self, pos) -> None:
        """
        Right-click menu for the queue.

        The bottom row of buttons covers the same actions, but the queue is the
        thing people point at, so 添加 / 移除 / 清空 hang off it directly too.
        Clicking an unselected row selects it first (see `eventFilter`), which is
        what you expect before acting on it.
        """
        menu = self._build_queue_menu()
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _open_current_folder(self) -> None:
        """Reveal the selected queue item's source folder."""
        if self.current is None:
            return
        if not vt.open_in_explorer(self.current.path):
            self._log("无法自动打开源文件夹（系统拒绝访问）")

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.list.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            self.list.takeItem(row)
            del self.items[row]
        self.current = None
        if self.items:
            self.list.setCurrentRow(min(rows[-1], len(self.items) - 1))
        else:
            self.split_panel.set_current_item(None)
            self.player.set_info(None)
            self.wall.clear()
            self.split_panel.show_segments([])
        self._update_queue_header()

    def clear_queue(self) -> None:
        if not self.items:
            return
        if not ask_yes_no(self, "清空队列",
                          f"确定要清空整个视频队列吗？\n\n"
                          f"队列里有 {len(self.items)} 个视频会被移除。\n"
                          f"只是从列表里移除，磁盘上的视频和已拆分的片段都不会删除。",
                          kind="warning", yes="清空", no="取消"):
            return
        self.list.clear()
        self.items.clear()
        self.current = None
        self.split_panel.set_current_item(None)
        self.player.set_info(None)
        self.wall.clear()
        self.split_panel.show_segments([])
        self._update_queue_header()

    def _move_selection(self, delta: int) -> None:
        rows = sorted({i.row() for i in self.list.selectedIndexes()})
        if not rows:
            return
        if delta < 0 and rows[0] == 0:
            return
        if delta > 0 and rows[-1] == self.list.count() - 1:
            return
        order = rows if delta > 0 else list(reversed(rows))
        for row in order:
            item = self.list.takeItem(row)
            entry = self.items.pop(row)
            self.list.insertItem(row + delta, item)
            self.items.insert(row + delta, entry)
        self.list.clearSelection()
        for row in rows:
            self.list.item(row + delta).setSelected(True)
        self.list.setCurrentRow(rows[0] + delta)

    def _update_queue_header(self) -> None:
        total = len(self.items)
        ready = sum(1 for i in self.items if i.info and not i.info.error)
        self.lbl_queue.setText(f"{total} 个文件 · {ready} 个已就绪")
        self.drop.setVisible(total == 0)

    # ------------------------------------------------------------------ #
    # plan + thumbnails
    # ------------------------------------------------------------------ #
    def _on_split_point(self, position: float) -> None:
        if self.current is None:
            return
        self.split_panel.rb_custom.setChecked(True)
        ranges = getattr(self, "_custom_ranges", None)
        if not ranges or getattr(self, "_custom_owner", None) is not self.current:
            ranges = [(0.0, position)] if position > 0.1 else []
            self._custom_owner = self.current
        else:
            start = ranges[-1][1]
            if position > start + 0.1:
                ranges.append((start, position))
        self._custom_ranges = ranges
        if self.current.info and self.current.info.duration > 0:
            last_end = ranges[-1][1] if ranges else 0.0
            if last_end < self.current.info.duration - 0.1:
                ranges.append((last_end, self.current.info.duration))
        self.current.segments = vt.plan_split(self.current.info, vt.MODE_CUSTOM,
                                              custom_ranges=ranges) \
            if self.current.info else []
        self.split_panel.show_segments(self.current.segments, self.current.info,
                                       self.split_panel.settings())
        self._update_row(self.current)
        self._log(f"记录拆分点 {vt.human_time(position, True)}（共 {len(ranges)} 个区间）")

    def refresh_plan(self) -> None:
        """Recompute the segment list for the current item from the panel."""
        if self.current is None or self.current.info is None:
            self.split_panel.show_segments([])
            return
        settings = self.split_panel.settings()
        settings.mode = self.split_panel.mode()
        self.current.settings = SplitSettings(**vars(settings))

        info = self.current.info
        mode = settings.mode
        if mode == vt.MODE_CUSTOM:
            segments = vt.plan_split(info, mode, custom_ranges=getattr(self, "_custom_ranges", []))
        elif mode == vt.MODE_BY_KEYFRAME:
            segments = self.current.segments if self.current.keyframes else []
            if not segments:
                self._load_keyframes()
                return
        else:
            segments = vt.plan_split(
                info, mode,
                segment_seconds=settings.segment_seconds,
                part_count=settings.part_count,
                target_mb=settings.target_mb)
        self.current.segments = segments
        self.split_panel.show_segments(segments, info, settings)
        self._update_row(self.current)

    def _load_keyframes(self) -> None:
        if self.current is None or self.current.info is None:
            return
        if self._kf_worker is not None and self._kf_worker.isRunning():
            return
        item = self.current
        self.lbl_status.setText("正在读取关键帧 …")
        self.progress.setRange(0, 0)
        self._kf_worker = KeyframeWorker(item.path, self)
        self._kf_worker.finished_ok.connect(lambda keys, it=item: self._on_keyframes(it, keys))
        self._kf_worker.failed.connect(lambda msg: self._log(f"关键帧读取失败：{msg}"))
        self._kf_worker.start()

    def _on_keyframes(self, item: QueueItem, keys: list[float]) -> None:
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        item.keyframes = keys
        self.lbl_status.setText(f"关键帧：{len(keys)} 个")
        self._log(f"{item.name}：读取到 {len(keys)} 个关键帧")
        if item is self.current:
            item.segments = vt.plan_split(item.info, vt.MODE_BY_KEYFRAME, keyframes=keys)
            self.split_panel.show_segments(item.segments, item.info, item.settings)
            self._update_row(item)

    def _thumbnail_times(self, duration: float) -> list[float] | None:
        """
        Capture points for the contact sheet.

        A step of 0 means "spread `count` frames evenly over the clip"; any
        positive step means "one frame every N seconds, from the head to the
        tail of the clip, up to `count` frames".
        """
        step = self.wall.frame_step()
        count = self.wall.frame_count()
        if step <= 0:
            return None
        return vt.capture_times(duration, step, count)

    def generate_thumbs(self, force: bool = False) -> None:
        if self.current is None or self.current.info is None:
            return
        info = self.current.info
        if info.error or info.duration <= 0:
            return
        if self._thumb_worker is not None and self._thumb_worker.isRunning():
            self._thumb_worker.cancel()
        self.wall.set_busy(True)
        self._thumb_worker = ThumbWorker(
            info.path, info.duration,
            count=self.wall.frame_count(), columns=4, tile_height=220,
            parent=self, times=self._thumbnail_times(info.duration),
            sheet_format=self.wall.sheet_format(), quality=self.wall.sheet_quality(),
        )
        self._thumb_worker.progressed.connect(
            lambda msg, frac: self.wall.set_busy(True, msg))
        self._thumb_worker.finished_ok.connect(self._on_thumbs)
        self._thumb_worker.failed.connect(lambda msg: (
            self.wall.set_busy(False), self._log(f"预览图生成失败：{msg}")))
        self._thumb_worker.start()

    def _on_thumbs(self, job: ThumbJob) -> None:
        self.wall.set_busy(False)
        self.wall.show_job(job)
        if self.current is not None:
            self.current.status = "待拆分"
            self._update_row(self.current)

    def save_preview_frames(self) -> None:
        """
        Save every preview frame as `<视频文件名>_缩略图_001.png …`.

        Two layouts, chosen in 输出设置:
          · 独立文件夹（默认）：源文件夹\\视频名\\视频名_缩略图_001.png
          · 直接放视频旁：源文件夹\\视频名_缩略图_001.png（不建文件夹）
        """
        if self.current is None or not self.wall.tiles:
            notify(self, "还没有预览图", "请先选择视频并生成预览图。", "info")
            return
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._log("预览图正在保存中，请稍候 …")
            return
        info = self.current.info
        src = info.path if info else self.current.path
        frames = [t._pm_path for t in self.wall.tiles if getattr(t, "_pm_path", None)]
        if not frames:
            self._log("保存失败：预览帧缓存已失效，请点 ⟳ 重新生成")
            return

        settings = self.split_panel.settings()
        flat = settings.preview_beside
        folder = vt.build_preview_dir(src, flat=flat)
        # Names are plain (`视频名_缩略图.png`) unless numbering is switched on;
        # the index is only consulted in that mode.
        start = vt.next_preview_index(src, flat=flat) if vt.PREVIEW_NUMBERING else 1
        self.wall.set_busy(True, "保存预览图 …")
        self._preview_worker = PreviewSaveWorker(
            src, frames, list(self.wall.times), folder, self,
            pad=max(1, settings.pad), start_index=start)
        self._preview_flat = flat
        self._preview_worker.progressed.connect(
            lambda msg, frac: self.wall.set_busy(True, msg))
        self._preview_worker.finished_ok.connect(self._on_previews_saved)
        self._preview_worker.failed.connect(self._on_previews_failed)
        self._preview_worker.start()

    def _on_previews_saved(self, saved: list, folder: str) -> None:
        self.wall.set_busy(False)
        self.wall.set_captures(self.wall.captures() + len(saved))
        if self.current is not None:
            self.current.preview_count = self.wall.captures()
        self.lbl_status.setText(f"已保存 {len(saved)} 张预览图：{folder}")
        self._log(f"预览图已保存 {len(saved)} 张 → {folder}")
        if not saved:
            notify(self, "没有可保存的预览图", "预览帧文件已失效，请重新生成。", "warning")
            return
        first = saved[0][0]
        last = saved[-1][0]
        flat = getattr(self, "_preview_flat", False)
        if flat:
            where = (f"位置：与视频同一个文件夹\n{os.path.dirname(first)}\n\n"
                     f"（没有新建任何文件夹）")
        else:
            where = f"文件夹：\n{os.path.dirname(first)}"
        choice = message_box(
            self, "预览图已保存",
            f"已保存 {len(saved)} 张预览图到视频旁。\n\n"
            f"{where}\n\n"
            f"文件名：\n{os.path.basename(first)}  …  {os.path.basename(last)}",
            "ok",
            [("关闭", "reject"),
             ("打开文件夹", "folder"),
             ("打开第一张图片", "accept")])
        if choice == "folder":
            if not vt.open_in_explorer(first):
                self._log("无法自动打开文件夹（系统拒绝访问），已保存的文件不受影响")
        elif choice == "accept":
            if not vt.open_file(first):
                self._log("无法自动打开图片，已保存的文件不受影响")
                vt.open_in_explorer(first)

    def _on_previews_failed(self, message: str) -> None:
        self.wall.set_busy(False)
        self._log(f"保存预览图失败：{message}")
        notify(self, "保存失败", message, "critical")

    def export_sheet(self) -> None:
        """Compose the current frames into one sheet and write it as PNG/JPG."""
        if self.current is None or not self.wall.tiles:
            notify(self, "还没有预览图", "请先选择视频并生成预览图。", "info")
            return
        info = self.current.info
        fmt = self.wall.sheet_format()
        ext = ".jpg" if fmt == "jpg" else ".png"
        base = vt.safe_filename(os.path.splitext(os.path.basename(info.path))[0])
        # default next to the preview frames: the video's own folder, or straight
        # beside the video when "直接放视频旁" is selected
        folder = vt.build_preview_dir(info.path,
                                      flat=self.split_panel.preview_beside())
        default = os.path.join(folder, f"{base}_缩略图合集{ext}")
        os.makedirs(os.path.dirname(default), exist_ok=True)
        filters = ("JPEG 图片 (*.jpg *.jpeg);;PNG 图片 (*.png)" if fmt == "jpg"
                   else "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg)")
        path, chosen_filter = QFileDialog.getSaveFileName(
            self, "导出合集预览图", default, filters)
        if not path:
            return
        if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg"):
            path += ".jpg" if ("JPEG" in (chosen_filter or "")) else ext

        frames = [t._pm_path for t in self.wall.tiles if getattr(t, "_pm_path", None)]
        if not frames:
            self._log("导出失败：预览帧缓存已失效，请点 ⟳ 重新生成")
            return
        try:
            vt.compose_sheet(frames, self.wall.times, path,
                             columns=self.wall._cols or 4,
                             quality=self.wall.sheet_quality())
        except Exception as exc:                     # noqa: BLE001
            self._log(f"导出失败：{exc}")
            notify(self, "导出失败", str(exc), "critical")
            return
        self.lbl_status.setText(f"预览图已导出：{path}")
        self._log(f"合集预览图已导出（{fmt.upper()}）：{path}")
        choice = message_box(
            self, "导出完成",
            f"合集预览图已保存：\n{path}",
            "ok",
            [("关闭", "reject"), ("打开所在文件夹", "folder")])
        if choice == "folder":
            vt.open_in_explorer(path)

    def save_single_frame(self, index: int) -> None:
        """Right-click a thumbnail -> save just that frame."""
        if not (0 <= index < len(self.wall.tiles)):
            return
        tile = self.wall.tiles[index]
        src = getattr(tile, "_pm_path", None)
        if not src or not os.path.isfile(src):
            self._log("该帧缓存已失效，请重新生成预览图")
            return
        info = self.current.info if self.current else None
        base = vt.safe_filename(os.path.splitext(os.path.basename(info.path))[0]) if info else "frame"
        at = self.wall.times[index] if index < len(self.wall.times) else 0.0
        stamp = vt.human_time(at, True).replace(":", "-").replace(".", "_")
        folder = vt.build_preview_dir(
            info.path, flat=self.split_panel.preview_beside()) if info else (
            self.split_panel.ed_output.text().strip() or DEFAULT_OUTPUT)
        default = os.path.join(folder,
                               f"{base}_缩略图{index + 1:02d}_{stamp}.png")
        os.makedirs(os.path.dirname(default), exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "保存这一帧", default, "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg)")
        if not path:
            return
        if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg"):
            path += ".png"
        try:
            vt.compose_sheet([src], [at], path, columns=1,
                             include_timeline_labels=False,
                             quality=self.wall.sheet_quality())
        except Exception as exc:                     # noqa: BLE001
            self._log(f"保存失败：{exc}")
            return
        self._log(f"已保存单帧：{path}")
        self.lbl_status.setText(f"单帧已保存：{path}")

    # ------------------------------------------------------------------ #
    # splitting
    # ------------------------------------------------------------------ #
    def apply_to_all(self) -> None:
        settings = self.split_panel.settings()
        for item in self.items:
            item.settings = SplitSettings(**vars(settings))
        self._log(f"已把当前拆分设置应用到队列中的 {len(self.items)} 个文件")
        self.lbl_status.setText("设置已应用到整个队列")

    def start_split(self) -> None:
        if self._split_worker is not None and self._split_worker.isRunning():
            return
        ready = [i for i in self.items if i.info and not i.info.error]
        if not ready:
            notify(self, "没有可拆分的文件",
                   "请先添加视频并等待读取完成。\n\n"
                   "· 把视频拖进窗口任意位置，或点右上角「＋ 添加视频」\n"
                   "· 队列里每行显示「待拆分」就说明已经读取好了\n"
                   "· 视频大小没有限制，几 MB 的短片和几十 GB 的长片都可以",
                   "warning")
            return
        self.apply_to_all_to_items()
        for item in ready:
            if not item.segments:
                if self.current is item:
                    self.refresh_plan()
                if not item.segments:
                    info = item.info
                    mode = item.settings.mode
                    if mode == vt.MODE_BY_KEYFRAME:
                        self._log(f"{item.name}：关键帧尚未读取，已跳过")
                        continue
                    item.segments = vt.plan_split(
                        info, mode,
                        segment_seconds=item.settings.segment_seconds,
                        part_count=item.settings.part_count,
                        target_mb=item.settings.target_mb)

        jobs = [i for i in ready if i.segments]
        if not jobs:
            notify(self, "没有可拆分的片段",
                   "请先点「生成分段预览」，确认为每一段算出的区间后再开始拆分。",
                   "warning")
            return

        total_segments = sum(len(i.segments) for i in jobs)
        est = sum(s.estimated_bytes for i in jobs for s in i.segments)
        if not ask_yes_no(
                self, "开始拆分",
                f"即将依次处理 {len(jobs)} 个文件，共 {total_segments} 个片段。\n"
                f"预计输出体积 ≈ {vt.human_size(est)}\n\n"
                f"{self._describe_outputs(jobs)}",
                kind="question", yes="开始拆分", no="再等等"):
            return

        self.log.clear()
        self._log(f"开始批量拆分：{len(jobs)} 个文件 / {total_segments} 个片段")
        self._last_output_dirs = []
        self.split_panel.set_busy(True)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self._split_worker = SplitWorker(jobs, self)
        self._split_worker.progressed.connect(self._on_split_progress)
        self._split_worker.file_done.connect(self._on_file_done)
        self._split_worker.all_done.connect(self._on_split_done)
        self._split_worker.start()

    def _describe_outputs(self, jobs: list["QueueItem"], limit: int = 3) -> str:
        """Where the segments will land, one line per folder (usually one)."""
        dirs: list[str] = []
        for item in jobs:
            folder = vt.build_output_dir(item.path, item.settings.output_dir,
                                          item.settings.subfolder,
                                          mode=item.settings.out_mode())
            if folder not in dirs:
                dirs.append(folder)
        head = "输出目录：" if len(dirs) == 1 else f"输出目录（{len(dirs)} 个）："
        shown = dirs[:limit]
        text = head + "\n" + "\n".join(shown)
        if len(dirs) > len(shown):
            text += f"\n… 另外 {len(dirs) - len(shown)} 个"
        return text

    def apply_to_all_to_items(self) -> None:
        """Give the current panel settings to every queued item before a run."""
        if self.current is None:
            return
        settings = self.split_panel.settings()
        self.current.settings = SplitSettings(**vars(settings))

    def _on_split_progress(self, msg: str, frac: float) -> None:
        self.lbl_status.setText(msg)
        self.progress.setValue(int(max(0.0, min(1.0, frac)) * 1000))

    def _on_file_done(self, path: str, ok: bool, message: str) -> None:
        if ok:
            self._log(f"✓ {os.path.basename(path)}")
            folder = os.path.dirname(path)
            if folder and folder not in self._last_output_dirs:
                self._last_output_dirs.append(folder)
        else:
            self._log(f"✗ 失败：{message}")

    def _on_split_done(self, ok: int, fail: int) -> None:
        self.split_panel.set_busy(False)
        self.lbl_status.setText(f"完成：成功 {ok} 个片段，失败 {fail} 个")
        self._log(f"批量拆分结束：成功 {ok}，失败 {fail}")
        dirs = self._last_output_dirs or [self.split_panel.ed_output.text().strip()
                                          or DEFAULT_OUTPUT]
        where = dirs[0] if len(dirs) == 1 else f"{dirs[0]}\n（共 {len(dirs)} 个输出目录）"
        choice = message_box(
            self, "拆分完成",
            f"成功 {ok} 个片段，失败 {fail} 个。\n\n"
            f"输出位置：\n{where}",
            "ok" if not fail else "warning",
            [("关闭", "reject"), ("打开输出文件夹", "accept")])
        if choice == "accept":
            vt.open_in_explorer(dirs[0])

    def cancel_split(self) -> None:
        if self._split_worker is not None and self._split_worker.isRunning():
            self._split_worker.cancel()
            self._log("正在停止 …（当前片段完成后退出）")
            self.lbl_status.setText("正在停止 …")

    def open_output_dir(self) -> None:
        folder = self.split_panel.ed_output.text().strip() or DEFAULT_OUTPUT
        os.makedirs(folder, exist_ok=True)
        vt.open_in_explorer(folder)

    # ------------------------------------------------------------------ #
    # misc
    # ------------------------------------------------------------------ #
    def _update_row(self, item: QueueItem) -> None:
        for row in range(self.list.count()):
            list_item = self.list.item(row)
            if list_item and list_item.data(Qt.UserRole) is item:
                list_item.setData(Qt.UserRole + 1, bool(item is self.current))
                self.list.update(self.list.indexFromItem(list_item))
                return

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.list and event.type() in (QEvent.DragEnter, QEvent.Drop):
            if event.mimeData().hasUrls():
                if event.type() == QEvent.DragEnter:
                    event.acceptProposedAction()
                else:
                    paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
                    self.add_paths(paths)
                    event.acceptProposedAction()
                return True
        elif (obj is self.list and event.type() == QEvent.MouseButtonPress
              and event.button() == Qt.RightButton):
            # A right-click on a row acts on *that* row: select it before the
            # context menu opens. Clicking empty space keeps the selection, so
            # "添加视频" still works with nothing under the cursor.
            item = self.list.itemAt(event.position().toPoint())
            if item is not None and not item.isSelected():
                self.list.clearSelection()
                item.setSelected(True)
                self.list.setCurrentItem(item)
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ #
    # drag & drop: the whole window is a drop target
    #
    # Dropping used to work only over the queue list. Anything dropped on the
    # video preview, the contact sheet or the settings panel was simply
    # ignored, which read as "this file cannot be added". Now the window itself
    # accepts the drop and forwards the paths, whatever the size of the video.
    # ------------------------------------------------------------------ #
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drop_hint(True)
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hint(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_drop_hint(False)
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        event.acceptProposedAction()
        if paths:
            self.add_paths(paths)

    def _set_drop_hint(self, active: bool) -> None:
        self.lbl_status.setText("松手即可加入队列 …" if active else "准备就绪")

    def _sync_capture_tooltip(self) -> None:
        """The 📷 button explains where the files will actually land."""
        self.wall.btn_capture.setToolTip(self.split_panel.capture_tooltip())

    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{stamp}] {text}")

    def show_about(self) -> None:
        message_box(
            self, f"关于 {APP_TITLE}",
            f"{APP_TITLE} — 预览 · 合集预览图 · 队列批量拆分\n\n"
            "主要功能：\n"
            "· 拖入 / 添加多个视频形成队列（大小不限，几 MB 到几十 GB 都行）\n"
            "· 每行标出来源文件夹，鼠标悬停可看完整路径\n"
            "· 可视化预览播放（画面 + 音频，可拖动时间轴）\n"
            "· 画面滚轮缩放、放大后按住左键平移、双击还原\n"
            "· 合集预览图：可设张数与间隔，显示已截取张数，导出 PNG / JPG\n"
            "· 预览图存图：视频名_缩略图_001.png\n"
            "· 按目标大小(MB) / 每段时长 / 段数 / 关键帧 / 自定义区间拆分\n"
            "· 队列依次批量拆分，默认无损流复制，输出自动编号命名\n\n"
            "输出位置：\n"
            "· 拆分片段：视频源文件夹\\视频名\\拆分片段\\\n"
            "· 预览图　：视频源文件夹\\视频名\\ 或直接放视频旁（可在输出设置里切换）\n\n"
            f"FFmpeg：{'已就绪' if self._ffmpeg_ready else '未就绪'}\n"
            f"QtMultimedia 音频预览：{'可用' if HAVE_AUDIO else '不可用（仅画面）'}\n"
            f"缓存目录：{vt.CACHE_DIR}",
            "info", [("知道了", "accept")])

    def closeEvent(self, event) -> None:  # noqa: N802
        running = [w for w in (self._probe_worker, self._thumb_worker,
                               self._split_worker, self._kf_worker, self._boot,
                               self._preview_worker)
                   if w is not None and w.isRunning()]
        if running:
            if not ask_yes_no(self, "正在处理",
                              f"仍有 {len(running)} 个任务在运行，确定要退出吗？\n\n"
                              "正在写入的拆分片段可能不完整。",
                              kind="warning", yes="退出", no="继续等待"):
                event.ignore()
                return
            for worker in running:
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(1500)
        self.player.shutdown()
        super().closeEvent(event)


class DropZoneHint(QWidget):
    """Compact hint shown when the queue is empty (the player shows the big art)."""

    pathsDropped = Signal(list)
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setCursor(Qt.PointingHandCursor)
        self._hot = False

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
        painter.drawText(rect, Qt.AlignCenter, "拖入视频文件\n或点击这里选择文件")
        painter.end()
