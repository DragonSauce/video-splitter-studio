"""
verify_app.py - end-to-end verification with a real (visible) window.

Why this exists separately from smoke_test.py: audio playback can only be
measured against a live sound device. On Qt's `offscreen` platform the audio
sink accepts data but drains it far slower than real time, so the playback
clock looks stalled even though the code is correct. This script therefore
opens the actual window, plays a few seconds of the generated sample, and
reports the measured playback rate, audio progress and dropped samples.

Usage:
    python verify_app.py            # opens a window, runs ~10 s, closes itself
    
It also writes screenshots to ./shots and (with --keep) leaves the split output
in ./testmedia for inspection.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The scripts live either in the project root or in tests/; resolve the
# project root either way so imports and paths keep working.
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)
os.environ.pop("QT_QPA_PLATFORM", None)      # force a real window
# This run has no operator either: a popup would sit there forever (e.g. the
# "预览图已保存 / 要不要打开文件夹" question). ui_common makes them no-ops.
os.environ["VSS_NO_DIALOG"] = "1"

import video_tools as vt                      # noqa: E402
from PySide6.QtWidgets import QApplication    # noqa: E402
import main_window                            # noqa: E402

PLAY_SECONDS = 6.0
RATE_TOLERANCE = (0.85, 1.15)
SHOTS = os.path.join(HERE, "shots")


def ensure_sample() -> tuple[str, object]:
    """Create the synthetic test clip on first run."""
    found = vt.locate_ffmpeg()
    if not found:
        found = vt.download_ffmpeg(progress=lambda m, f: print("  ", m, flush=True))
    media_dir = os.path.join(HERE, "testmedia")
    os.makedirs(media_dir, exist_ok=True)
    sample = os.path.join(media_dir, "verify_sample.mp4")
    if not os.path.isfile(sample):
        print("creating test clip …", flush=True)
        r = vt.run_hidden([
            found[0], "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=40",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", sample,
        ], timeout=300)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[-400:])
    return sample, vt.probe(sample)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="keep the generated split output")
    args = parser.parse_args()

    sample, info = ensure_sample()
    print(f"clip: {info.duration:.1f}s {info.resolution_text} "
          f"video={info.video_codec} audio={info.audio_codec or '-'}")
    if info.error:
        print("probe error:", info.error)
        return 1

    app = QApplication(sys.argv)
    win = main_window.MainWindow()
    win.resize(*main_window.LAYOUT["window"])
    win.show()
    app.processEvents()

    # feed the clip straight into the queue and wait for probe + thumbnails
    win._ffmpeg_ready = True
    win._set_ui_enabled(True)
    win.add_paths([sample])
    deadline = time.time() + 40
    while time.time() < deadline:
        app.processEvents()
        if win.current is not None and win.current.info is not None and win.wall.tiles:
            break
        time.sleep(0.1)
    app.processEvents()
    tiles = len(win.wall.tiles)
    segments = len(win.current.segments) if win.current else 0
    print(f"queue: loaded={win.current is not None} segments={segments} "
          f"contact_sheet_tiles={tiles}")

    os.makedirs(SHOTS, exist_ok=True)
    win.grab().save(os.path.join(SHOTS, "idle.png"))

    # ---- preview zoom (mouse wheel) on a real window ----
    from PySide6.QtCore import QPoint, QPointF, Qt          # noqa: E402
    from PySide6.QtGui import QWheelEvent                   # noqa: E402

    surf = win.player.surface
    surf.set_source(win.wall.tiles[0]._pm if win.wall.tiles else None)
    app.processEvents()
    print(f"zoom: fit={surf._fit_size()} zoom={surf.zoom:.2f} "
          f"frame={surf.has_frame()}")
    for _ in range(5):
        surf.wheelEvent(QWheelEvent(QPointF(200, 120), QPointF(200, 120),
                                    QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                                    Qt.NoModifier, Qt.NoScrollPhase, False))
    app.processEvents()
    rect = surf._target_rect()
    print(f"zoom: after 5 wheel-ups zoom={surf.zoom:.2f} "
          f"painted={rect.width()}x{rect.height()} "
          f"(widget {surf.width()}x{surf.height()}) enlarged="
          f"{rect.width() > surf._fit_size()[0]}")
    win.grab().save(os.path.join(SHOTS, "zoom.png"))
    surf.reset_zoom()
    app.processEvents()
    print(f"zoom: reset -> zoom={surf.zoom:.2f} offset={surf._offset}")

    # ---- playback measurement ----
    print(f"playing {PLAY_SECONDS:.0f}s …", flush=True)
    win.player.seek(1.0)
    win.player.play()
    t0, p0 = time.time(), win.player.position
    frames = 0
    while time.time() - t0 < PLAY_SECONDS:
        app.processEvents()
        if win.player.surface.has_frame():
            frames += 1
        time.sleep(0.03)
    elapsed = time.time() - t0
    advanced = win.player.position - p0
    rate = advanced / elapsed if elapsed else 0.0
    win.grab().save(os.path.join(SHOTS, "playing.png"))

    sink = win.player.audio.sink_out.sink
    print(f"frames rendered : {frames}")
    print(f"media advanced  : {advanced:.2f}s over {elapsed:.2f}s wall (rate {rate:.2f}x)")
    print(f"audio backend   : {win.player.audio.backend}")
    if sink is not None:
        print(f"audio processed : {sink.processedUSecs() / 1e6:.2f}s "
              f"state={sink.state()}")
    print(f"pcm dropped     : {win.player.audio.source.dropped_seconds:.2f}s")

    low, high = RATE_TOLERANCE
    ok = low <= rate <= high and frames > 30
    print(f"playback timing : {'OK' if ok else 'OUT OF RANGE'}")

    # ---- stop and release ----
    win.player.pause()
    win.player.stop()
    app.processEvents()

    if not args.keep:
        out = os.path.join(HERE, "testmedia", "batch_out")
        if os.path.isdir(out):
            import shutil
            shutil.rmtree(out, ignore_errors=True)

    win.close()
    print("screenshots ->", SHOTS)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
