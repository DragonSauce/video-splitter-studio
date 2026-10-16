"""Smoke test: imports, FFmpeg bootstrap, thumbnail sheet, split plan + cut."""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The scripts live either in the project root or in tests/; resolve the
# project root either way so imports and paths keep working.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Nobody is at the keyboard here: a modal popup would block the run forever
# (that is how 9c-3b hung once). ui_common reads this and turns popups into
# no-ops that return the confirm role.
os.environ["VSS_NO_DIALOG"] = "1"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


step("1. import modules")
import video_tools as vt  # noqa: E402

print("video_tools OK")

step("2. locate or download ffmpeg")
found = vt.locate_ffmpeg()
if not found:
    found = vt.download_ffmpeg(progress=lambda m, f: print("  ", m, flush=True))
print("ffmpeg:", found)

step("3. create a synthetic test video")
test_dir = os.path.join(HERE, "testmedia")
os.makedirs(test_dir, exist_ok=True)
sample = os.path.join(test_dir, "sample.mp4")
if not os.path.isfile(sample):
    r = vt.run_hidden([
        found[0], "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", sample,
    ], timeout=300)
    print("ffmpeg rc:", r.returncode, (r.stderr or b"")[-300:])
print("sample:", sample, os.path.getsize(sample) if os.path.isfile(sample) else "MISSING")

step("4. probe")
media = vt.probe(sample)
print(f"  duration={media.duration:.3f} size={media.size_text} "
      f"res={media.resolution_text} fps={media.fps:.2f} v={media.video_codec} "
      f"a={media.audio_codec} bitrate={media.mux_bitrate} err={media.error!r}")

step("5. thumbnails")
data = vt.generate_thumbnails(sample, media.duration, count=8, columns=4, tile_height=120,
                              progress=lambda m, f: None)
print("  frames:", len(data["frames"]), "sheet:", data["sheet"],
      os.path.getsize(data["sheet"]) if data["sheet"] else 0)

step("6. keyframes")
keys = vt.list_keyframes(sample)
print("  keyframes:", len(keys), keys[:6])

step("6b. contact sheet options: custom times, JPG, single-frame save")
custom_times = [1.0, 5.0, 9.0, 14.0, 20.0]
jpg = vt.generate_thumbnails(sample, media.duration, tile_height=160,
                             times=custom_times, sheet_format="jpg", sheet_quality=80)
print(f"  custom times -> {len(jpg['frames'])} frames, sheet={os.path.basename(jpg['sheet'])} "
      f"({vt.human_size(os.path.getsize(jpg['sheet']))}) format={jpg['format']}")
single = vt.compose_sheet([jpg["frames"][0]], [custom_times[0]],
                          os.path.join(test_dir, "one_frame.jpg"), columns=1,
                          include_timeline_labels=False, quality=85)
print(f"  single frame -> {os.path.basename(single)} "
      f"({vt.human_size(os.path.getsize(single))})")
png = vt.generate_thumbnails(sample, media.duration, count=6, tile_height=120,
                             sheet_format="png")
print(f"  png sheet -> {os.path.basename(png['sheet'])} "
      f"({vt.human_size(os.path.getsize(png['sheet']))})")

step("7. plans")
for mode, kwargs in (
    (vt.MODE_BY_SIZE, dict(target_mb=1.0)),
    (vt.MODE_BY_DURATION, dict(segment_seconds=7)),
    (vt.MODE_BY_COUNT, dict(part_count=4)),
    (vt.MODE_BY_KEYFRAME, dict(keyframes=keys)),
):
    segs = vt.plan_split(media, mode, **kwargs)
    print(f"  {mode:9s} -> {len(segs)} segments, "
          f"first={segs[0].range_text if segs else '-'}, "
          f"est={vt.human_size(segs[0].estimated_bytes) if segs else '-'}")

step("8. cut (stream copy), start index 5 and 2-digit numbering")
segs = vt.plan_split(media, vt.MODE_BY_DURATION, segment_seconds=8)[:2]
out_dir = os.path.join(test_dir, "out")
results = vt.cut_all(sample, segs, out_dir, "sample", ext=".mp4", start_index=5, pad=2,
                     progress=lambda m, f: print(f"  {f*100:5.1f}% {m}", flush=True))
for seg, path, err in results:
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    print(f"  #{seg.index} {seg.range_text} -> {os.path.basename(path)} "
          f"{vt.human_size(size)} err={err!r}")
    if os.path.isfile(path):
        sub = vt.probe(path)
        print(f"      cut duration={sub.duration:.3f} (target {seg.duration:.3f})")

step("8b. sparse keyframes: smart mode must still produce full-length segments")
sparse = os.path.join(test_dir, "sparse.mp4")
if not os.path.isfile(sparse):
    # keyframe every 10 s, so 4 s segments cannot align with them
    vt.run_hidden([
        found[0], "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "250", "-keyint_min", "250", "-sc_threshold", "0", sparse,
    ], timeout=300)
sparse_info = vt.probe(sparse)
print(f"  source keyframes: {vt.list_keyframes(sparse)}")
sparse_segs = vt.plan_split(sparse_info, vt.MODE_BY_COUNT, part_count=3)
smart_dir = os.path.join(test_dir, "smart_out")
shutil.rmtree(smart_dir, ignore_errors=True)
smart_results = vt.cut_all(sparse, sparse_segs, smart_dir, "s", ext=".mp4",
                           start_index=1, pad=2, smart=True)
for seg, path, err in smart_results:
    if os.path.isfile(path):
        sub = vt.probe(path)
        flag = "OK " if abs(sub.duration - seg.duration) < 0.4 else "BAD"
        print(f"    {flag} {os.path.basename(path)} dur={sub.duration:.3f} "
              f"(target {seg.duration:.3f}) err={err!r}")
    else:
        print(f"    BAD {path} missing err={err!r}")

step("9. PySide6 import + widgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402
import player  # noqa: E402
import main_window  # noqa: E402
import ui_common  # noqa: E402

ui_common.set_quiet(True)      # belt and braces: no popups during the self-check

app = QApplication.instance() or QApplication([])
win = main_window.MainWindow()
win.resize(1440, 920)
win.show()
print("MainWindow constructed:", win.windowTitle())
print("HAVE_AUDIO:", player.HAVE_AUDIO)
app.processEvents()
win.player.load(media, None)
win.wall.show_job(player.ThumbJob(frames=data["frames"], times=data["times"],
                                  sheet=data["sheet"], columns=4))
segs = vt.plan_split(media, vt.MODE_BY_SIZE, target_mb=1.0)
win.split_panel.show_segments(segs, media)
# simulate a populated queue so the screenshot shows the real layout
win.items = [main_window.QueueItem(path=sample, info=media,
                                   segments=segs, poster=data["frames"][0])]
from PySide6.QtCore import Qt as _Qt  # noqa: E402
from PySide6.QtWidgets import QListWidgetItem as _LI  # noqa: E402
from PySide6.QtGui import QPixmap as _PM  # noqa: E402
row = _LI()
row.setData(_Qt.UserRole, win.items[0])
row.setData(_Qt.UserRole + 1, True)
row.setData(_Qt.UserRole + 2, _PM(data["frames"][0]))
win.list.addItem(row)
win.current = win.items[0]
win._update_queue_header()
app.processEvents()
app.processEvents()
shots = os.path.join(HERE, "shots")
os.makedirs(shots, exist_ok=True)
shot = os.path.join(shots, "ui.png")
win.grab().save(shot)
print("screenshot:", shot, os.path.getsize(shot))
print("widgets OK; segment rows:", win.split_panel.table.rowCount())

step("9b. contact-sheet UI plumbing (count / step / JPG)")
win.wall.sp_count.setValue(6)
win.wall.sp_step.setValue(5.0)          # one frame every 5 s
win.wall.cmb_format.setCurrentIndex(1)  # JPG
win.wall.sp_quality.setValue(85)
times = win._thumbnail_times(media.duration)
print(f"  ui times -> {len(times)} points: {[round(t, 1) for t in times]}")
win.wall.show_job(player.ThumbJob(frames=data["frames"], times=times,
                                  sheet=data["sheet"], columns=4))
app.processEvents()
print(f"  wall tiles={len(win.wall.tiles)} cols={win.wall._cols} "
      f"format={win.wall.sheet_format()} quality={win.wall.sheet_quality()}")
print(f"  area={win.wall.area.size()} holder={win.wall.holder.size()}")
win.wall.sp_step.setValue(0.0)
win.wall.cmb_format.setCurrentIndex(0)

step("9b-2. wall layout across tile counts (the sheet must be a real grid)")
base_frames = data["frames"]
for n in (6, 12, 24, 64):
    frames_n = [base_frames[i % len(base_frames)] for i in range(n)]
    win.wall.show_job(player.ThumbJob(frames=frames_n, times=[0.0] * n,
                                      sheet=None, columns=4))
    # show_job() builds the tiles at once but lets Qt lay out and delete the
    # previous batch on later event-loop turns. Poll until the count settles
    # instead of guessing a number of turns - reading too early shows the
    # previous job's count and looks like tiles were dropped.
    settled = 0
    deadline = time.time() + 5.0
    while time.time() < deadline:
        app.processEvents()
        app.sendPostedEvents()
        if len(win.wall.tiles) == n and win.wall._cols:
            settled += 1
            if settled >= 3:
                break
        else:
            settled = 0
        time.sleep(0.01)
    tile = win.wall.tiles[0] if win.wall.tiles else None
    print(f"  count={n:3d} -> tiles={len(win.wall.tiles):3d} cols={win.wall._cols:2d} "
          f"tile={tile.width() if tile else 0}x{tile.height() if tile else 0} "
          f"viewport={win.wall.area.viewport().size()} "
          f"{'OK' if len(win.wall.tiles) == n else 'LOST TILES'}")
win.wall.show_job(player.ThumbJob(frames=data["frames"], times=data["times"],
                                  sheet=data["sheet"], columns=4))
app.processEvents()

step("9c. new output layout: beside-source folder, preview frames, queue labels")
st = main_window.SplitSettings(output_dir=os.path.join(test_dir, "unused"),
                               subfolder=True, save_beside=True)
beside = vt.build_output_dir(sample, st.output_dir, st.subfolder, mode=st.out_mode())
print(f"  beside  -> {beside}")
print(f"  expected-> {os.path.join(test_dir, os.path.splitext(os.path.basename(sample))[0], vt.OUTPUT_AUTO_SUBFOLDER)}")
root = vt.build_output_dir(sample, os.path.join(test_dir, "out_root"), True, mode="root")
print(f"  root    -> {root}")
flat = vt.build_output_dir(sample, os.path.join(test_dir, "out_root"), False, mode="root")
print(f"  flat    -> {flat}")

print(f"  capture_times(30s, every 5s, max 20) -> "
      f"{[round(t, 1) for t in vt.capture_times(30.0, 5.0, 20)]}")
print(f"  capture_times(30s, auto, 6)          -> "
      f"{[round(t, 1) for t in vt.capture_times(30.0, 0.0, 6)]}")
print(f"  capture_times(30s, every 4s, max 4)  -> "
      f"{[round(t, 1) for t in vt.capture_times(30.0, 4.0, 4)]}")

preview_dir = vt.build_preview_dir(sample)
print(f"  preview dir  : {preview_dir}")
print(f"  expected     : {os.path.join(test_dir, os.path.splitext(os.path.basename(sample))[0])}"
      f"  -> {'OK' if os.path.normcase(preview_dir) == os.path.normcase(os.path.join(test_dir, os.path.splitext(os.path.basename(sample))[0])) else 'MISMATCH'}")
print(f"  dir exists   : {os.path.isdir(preview_dir)}")
print(f"  preview names: {vt.preview_frame_name(sample, 1)}, "
      f"{vt.preview_frame_name(sample, 12)}")
cap_times = vt.capture_times(media.duration, 6.0, 24)
saved = vt.extract_preview_frames(sample, preview_dir, cap_times, height=160)
print(f"  extracted {len(saved)} png at times {[round(s['time'], 1) for s in saved][:6]} …")
for entry in saved[:3] + saved[-1:]:
    print(f"    {os.path.basename(entry['path']):28s} "
          f"{vt.human_size(os.path.getsize(entry['path']))}")
# the wall must show the shot count, and re-running must not overwrite frames
win.wall.set_captures(len(saved))
print(f"  截取 label -> {win.wall.lbl_captures.text()}")
again = vt.extract_preview_frames(sample, preview_dir, cap_times[:2], height=160)
print(f"  re-run keeps existing files: {[s['fresh'] for s in again]}")

step("9c-2. drops of ANY size are accepted (the <2GB case)")
from PySide6.QtCore import QMimeData, QUrl, QPointF, Qt  # noqa: E402
from PySide6.QtGui import (QDragEnterEvent, QDragMoveEvent,  # noqa: E402
                           QDropEvent)

win.items = []
win.list.clear()
win.current = None
win._ffmpeg_ready = True                      # the drop path must not need a wait

# A tiny stand-in for "a small video": ffmpeg writes a real mpeg4 file a few KB
# long, which is exactly the kind of clip that used to be impossible to add.
small_dir = os.path.join(test_dir, "small")
os.makedirs(small_dir, exist_ok=True)
small = os.path.join(small_dir, "tiny.mp4")
if not os.path.isfile(small):
    vt.run_hidden([
        found[0], "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2",
        "-c:v", "mpeg4", "-q:v", "20", "-pix_fmt", "yuv420p", small,
    ], timeout=120)
print(f"  tiny clip: {os.path.basename(small)} "
      f"{vt.human_size(os.path.getsize(small)) if os.path.isfile(small) else 'MISSING'}")


def _drop(paths):
    """Feed a file drop to the window's own drop handler (no list widget)."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    enter = QDragEnterEvent(QPointF(400, 300), Qt.CopyAction, mime,
                            Qt.LeftButton, Qt.NoModifier)
    win.dragEnterEvent(enter)
    move = QDragMoveEvent(QPointF(400, 300), Qt.CopyAction, mime,
                          Qt.LeftButton, Qt.NoModifier)
    win.dragMoveEvent(move)
    drop = QDropEvent(QPointF(400, 300), Qt.CopyAction, mime,
                      Qt.LeftButton, Qt.NoModifier)
    win.dropEvent(drop)
    app.processEvents()


mime_small = QMimeData()
mime_small.setUrls([QUrl.fromLocalFile(small)])
accepted_enter = QDragEnterEvent(QPointF(400, 300), Qt.CopyAction, mime_small,
                                 Qt.LeftButton, Qt.NoModifier)
win.dragEnterEvent(accepted_enter)
print(f"  dragEnter accepts urls: {accepted_enter.isAccepted()}")

_drop([small])
print(f"  after drop of the small clip -> queue={len(win.items)} "
      f"name={os.path.basename(win.items[0].path) if win.items else '-'}")
# a directory drop must pick up the file inside it
_drop([small_dir])
print(f"  duplicate folder drop is a no-op: queue={len(win.items)}")
_drop([small_dir, sample])
print(f"  after folder + big clip drop  -> queue={len(win.items)} "
      f"(no size ceiling on either)")
if win._probe_worker is not None:
    win._probe_worker.wait(30000)
app.processEvents()
for item in win.items:
    print(f"    {os.path.basename(item.path):14s} size="
          f"{vt.human_size(item.info.size_bytes) if item.info else '?':>9s} "
          f"duration={item.info.duration if item.info else '?'} err="
          f"{item.info.error if item.info else '?'}")

step("9c-3. preview frames: folder mode and 直接放视频旁 (视频名_缩略图.png)")
folder_mode = vt.build_preview_dir(small, flat=False)
flat_mode = vt.build_preview_dir(small, flat=True)
print(f"  folder mode -> {folder_mode}")
print(f"  flat   mode -> {flat_mode}  (same as source dir: "
      f"{os.path.normcase(flat_mode) == os.path.normcase(small_dir)})")
print(f"  单张命名  -> {vt.preview_frame_name(small)}"
      f"  | 示例 {vt.preview_frame_sample('我的片子.mp4')}")
print(f"  编号开关  -> PREVIEW_NUMBERING={vt.PREVIEW_NUMBERING}"
      f"  强制编号 -> {vt.preview_frame_name(small, 12, 2, numbered=True)}")
# several capture points cannot share one file name, so they keep numbers
flat_saved = vt.extract_preview_frames(small, flat_mode, [0.2, 1.0, 1.8], height=90,
                                       flat=True)
print(f"  多张（自动带序号）-> {[os.path.basename(s['path']) for s in flat_saved]}")
single_saved = vt.extract_preview_frames(small, flat_mode, [0.5], height=90, flat=True)
print(f"  单张（无序号）    -> {[os.path.basename(s['path']) for s in single_saved]}")
print(f"  files beside the video: "
      f"{[n for n in sorted(os.listdir(small_dir)) if n.endswith('.png')]}")

step("9c-3b. 📷 writes 视频名_缩略图.png (no _001 suffix)")
frame_png = os.path.join(small_dir, os.path.basename(single_saved[0]["path"]))
# ---- the window's own 📷 path, with the default settings ----
win._ffmpeg_ready = True
win.current = main_window.QueueItem(path=small, info=vt.probe(small))
win.wall.show_job(player.ThumbJob(frames=[frame_png], times=[0.5],
                                  sheet=None, columns=1))
app.processEvents()
print(f"  default 预览图存到 = "
      f"{'直接放视频旁' if win.split_panel.preview_beside() else '「视频名」文件夹'}"
      f" | save_beside={win.split_panel.settings().save_beside}")
before_ui = {os.path.basename(p) for p in vt.list_preview_files(small, flat=True)}
win.save_preview_frames()
if win._preview_worker is not None:
    win._preview_worker.wait(20000)
app.processEvents()
after_ui = {os.path.basename(p) for p in vt.list_preview_files(small, flat=True)}
created = sorted(after_ui - before_ui)
print(f"  📷 新写入源视频旁的文件: {created}")
print(f"  没有 _001 后缀: {all('_缩略图_' not in n for n in created)}")
print(f"  {vt.PREVIEW_NUMBERING=} -> 再点一次不会覆盖旧图（走 unique_path）：")
again = main_window.PreviewSaveWorker(small, [frame_png], [0.6], small_dir, win)
done_a = []
again.finished_ok.connect(lambda saved, folder: done_a.append(saved))
again.start()
again.wait(20000)
app.processEvents()
names_a = [os.path.basename(p) for p, _t in done_a[0]]
print(f"  第二次 -> {names_a}")
print(f"  all files on disk: "
      f"{[n for n in sorted(os.listdir(small_dir)) if n.endswith('.png')]}")

step("9c-4. the log strip is at most 3 lines tall and can be collapsed")
win._log("日志行 1/4：检查最多三行")
win._log("日志行 2/4")
win._log("日志行 3/4")
win._log("日志行 4/4（这一行应该被滚动条藏起来）")
app.processEvents()
line_h = win.log.fontMetrics().lineSpacing()
print(f"  log height={win.log.height()} max={win.log.maximumHeight()} "
      f"line={line_h} -> visible lines ~{win.log.height() // max(1, line_h)}")
print(f"  log minimum height (can shrink to) = {win.log.minimumHeight()}")
col = win._preview_split
print(f"  preview column: player={win.player.height()} wall={win.wall.height()} "
      f"log={win.log.height()} sizes={col.sizes()}")

step("9d. preview surface zoom / pan / reset")
from PySide6.QtCore import QPoint as _QP, QPointF as _QPF  # noqa: E402
from PySide6.QtGui import QWheelEvent as _Wheel  # noqa: E402

surf = win.player.surface
surf.set_source(_PM(data["frames"][0]))
app.processEvents()
print(f"  start zoom={surf.zoom:.2f} fit={surf._fit_size()}")
for _ in range(4):
    surf.wheelEvent(_Wheel(_QPF(100, 60), _QPF(100, 60), _QP(0, 0), _QP(0, 120),
                           _Qt.NoButton, _Qt.NoModifier, _Qt.NoScrollPhase, False))
app.processEvents()
print(f"  after 4 wheel-ups zoom={surf.zoom:.2f} target={surf._target_rect()}")
win.grab().save(os.path.join(shots, "zoom.png"))
surf._offset = [40.0, -30.0]
print(f"  pan offset -> {surf._offset}")
for _ in range(20):
    surf.wheelEvent(_Wheel(_QPF(100, 60), _QPF(100, 60), _QP(0, 0), _QP(0, -120),
                           _Qt.NoButton, _Qt.NoModifier, _Qt.NoScrollPhase, False))
print(f"  clamped low zoom={surf.zoom:.2f} (min {surf.ZOOM_MIN})")
surf.reset_zoom()
print(f"  after reset zoom={surf.zoom:.2f} offset={surf._offset} "
      f"fit={surf._fit_size()}")
print(f"  frame present: {surf.has_frame()}")
# the zoom must actually be painted: the drawn rect has to exceed the label
surf.set_source(_PM(data["frames"][0]))
app.processEvents()
base_rect = surf._target_rect()
for _ in range(5):
    surf.wheelEvent(_Wheel(_QPF(50, 40), _QPF(50, 40), _QP(0, 0), _QP(0, 120),
                           _Qt.NoButton, _Qt.NoModifier, _Qt.NoScrollPhase, False))
app.processEvents()
zoom_rect = surf._target_rect()
print(f"  painted rect fit={base_rect.width()}x{base_rect.height()} -> "
      f"zoomed={zoom_rect.width()}x{zoom_rect.height()} "
      f"(widget {surf.width()}x{surf.height()})")
print(f"  zoom enlarges the picture: {zoom_rect.width() > base_rect.width()}")
surf.reset_zoom()

step("9e. queue rows carry the source folder (path distinction in the list)")
from PySide6.QtWidgets import QListWidgetItem as _LI2  # noqa: E402

win.list.clear()
row = _LI2()
row.setData(_Qt.UserRole, win.items[0])
win._decorate_row(row, win.items[0])
win.list.addItem(row)
print(f"  row tooltip:\n    " + win._row_tooltip(win.items[0]).replace("\n", "\n    "))
print(f"  row folder data -> {win.list.item(0).data(_Qt.UserRole + 3)}")
print(f"  delegate row height -> {main_window.QueueDelegate.ROW_HEIGHT}")
win.list.setCurrentRow(0)
app.processEvents()
win.grab().save(os.path.join(shots, "queue.png"))

step("10. playback: decode a second of video and check frames arrive")
win.player.seek(2.0)
win.player.play()
deadline = time.time() + 12
frames_seen = 0
while time.time() < deadline:
    app.processEvents()
    if win.player.surface.has_frame():
        frames_seen += 1
        if frames_seen > 20:
            break
    time.sleep(0.05)
print(f"  playing={win.player.playing} pos={win.player.position:.2f}s "
      f"frames_with_image={frames_seen} audio_backend={win.player.audio.backend} "
      f"audio_started={win.player.audio.audio_started_at is not None}")
win.grab().save(os.path.join(shots, "playing.png"))

step("11. playback clock rate (position must advance ~1:1 with wall time)")
t0, p0 = time.time(), win.player.position
time.sleep(4.0)
app.processEvents()
t1, p1 = time.time(), win.player.position
advanced, wall = p1 - p0, t1 - t0
print(f"  media advanced {advanced:.2f}s over {wall:.2f}s wall -> rate {advanced / wall:.2f}x")
print(f"  qt audio processedUSecs={win.player.audio.sink_out.clock_us / 1e6:.2f}s")
print("  clock source:", "audio" if win.player.audio.audio_started_at else "wall")
print(f"  rate in range: {0.7 <= (advanced / wall) <= 1.3} "
      f"(offscreen audio drains slowly; verify_app.py measures real playback)")

step("12. audio sink diagnostics")
sink = win.player.audio.sink_out.sink
if sink is not None:
    print(f"  state={sink.state()} error={sink.error()} "
          f"bufferSize={sink.bufferSize()} bytesFree={sink.bytesFree()} "
          f"processedUSecs={sink.processedUSecs()}")
    pcm = win.player.audio.source
    print(f"  pcm buffered={pcm.buffered_seconds:.3f}s dropped={pcm.dropped_seconds:.3f}s")
else:
    print("  no Qt sink")

win.player.pause()
win.player.stop()
app.processEvents()

step("13. batch queue split through the real SplitWorker")
out_root = os.path.join(test_dir, "batch_out")
second = os.path.join(test_dir, "sample2.mp4")
if not os.path.isfile(second):
    vt.run_hidden([
        found[0], "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", second,
    ], timeout=300)
info2 = vt.probe(second)

jobs = []
for path, info_obj, target in ((sample, media, 1.0), (second, info2, 0.3)):
    st = main_window.SplitSettings(mode=vt.MODE_BY_SIZE, target_mb=target,
                                   output_dir=out_root, subfolder=True,
                                   save_beside=(path is second))
    st.mode = vt.MODE_BY_COUNT
    st.part_count = 3
    item = main_window.QueueItem(path=path, info=info_obj, settings=st)
    item.segments = vt.plan_split(info_obj, vt.MODE_BY_COUNT, part_count=3)
    jobs.append(item)
    print(f"  queued {os.path.basename(path)}: {len(item.segments)} segments")

worker = main_window.SplitWorker(jobs, win)
finished = {}
worker.progressed.connect(lambda m, f: finished.setdefault("last", (m, f)))
worker.file_done.connect(lambda p, ok, msg: print(f"    {'OK ' if ok else 'ERR'} "
                                                  f"{os.path.basename(p) or msg} {msg}"))
done = []
worker.all_done.connect(lambda ok, fail: done.append((ok, fail)))
worker.start()
deadline = time.time() + 180
while time.time() < deadline and not done:
    app.processEvents()
    time.sleep(0.1)
worker.wait(5000)
print("  all_done:", done, "| last progress:", finished.get("last"))

produced = []
for root, _dirs, files in os.walk(out_root):
    for name in sorted(files):
        full = os.path.join(root, name)
        produced.append((os.path.relpath(full, out_root), os.path.getsize(full)))
for rel, size in produced:
    print(f"    {rel:40s} {vt.human_size(size)}")
print(f"  output files in chosen root: {len(produced)}")

step("13b. same batch with 输出到源文件旁: segments land in <视频名>\\拆分片段")
beside_jobs = []
for path, info_obj in ((sample, media), (second, info2)):
    # start from a clean folder so the listing proves what this run produced
    shutil.rmtree(vt.build_output_dir(path, out_root, True, mode="beside"),
                  ignore_errors=True)
    st2 = main_window.SplitSettings(mode=vt.MODE_BY_COUNT, part_count=2,
                                    output_dir=out_root, subfolder=True,
                                    save_beside=True)
    item = main_window.QueueItem(path=path, info=info_obj, settings=st2)
    item.segments = vt.plan_split(info_obj, vt.MODE_BY_COUNT, part_count=2)
    beside_jobs.append(item)
    print(f"  {os.path.basename(path)} -> "
          f"{vt.build_output_dir(path, out_root, True, mode='beside')}")

worker2 = main_window.SplitWorker(beside_jobs, win)
done2 = []
worker2.all_done.connect(lambda ok, fail: done2.append((ok, fail)))
worker2.start()
deadline = time.time() + 180
while time.time() < deadline and not done2:
    app.processEvents()
    time.sleep(0.1)
worker2.wait(5000)
print("  all_done:", done2)
for item in beside_jobs:
    folder = vt.build_output_dir(item.path, out_root, True, mode="beside")
    names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    print(f"    {os.path.basename(folder)}: {names}")

step("13c. queue path distinction: same filename, two source folders")
twin_dir = os.path.join(test_dir, "other_folder")
os.makedirs(twin_dir, exist_ok=True)
twin = os.path.join(twin_dir, os.path.basename(sample))
if not os.path.isfile(twin):
    shutil.copy2(sample, twin)
for p in (sample, twin):
    it = main_window.QueueItem(path=p, info=vt.probe(p))
    print(f"    {os.path.basename(p):14s} 来源 {os.path.dirname(p)}")
    print(f"      → 预览图 {vt.build_preview_dir(p)}")
    print(f"      → 拆分   {vt.build_output_dir(p, out_root, True, mode='beside')}")

win.close()
print("\nALL SMOKE STEPS DONE")
