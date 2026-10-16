"""
video_tools.py - FFmpeg bootstrap + media probing + thumbnail sheets + splitting engine.

Design goals
------------
* No third party Python dependency (pure stdlib) so it can be imported by the
  bootstrap loader before PySide6 has been imported.
* Self-contained: FFmpeg is looked up in (1) the app's `bin/` folder,
  (2) PATH, (3) common install locations; if missing it can auto-download a
  static build into `bin/`.
* Every heavy operation is exposed as a small pure-ish function so the GUI can
  run it on a QThread without touching Qt objects from a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

# When frozen by PyInstaller, __file__ lives in the temporary _internal tree,
# so the writable ffmpeg/cache folders must live next to the .exe instead.
_BASE_OVERRIDE = os.environ.get("VSS_BASE_DIR")
if getattr(sys, "frozen", False) or _BASE_OVERRIDE:
    APP_DIR = os.path.abspath(_BASE_OVERRIDE or os.path.dirname(os.path.abspath(sys.executable)))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(APP_DIR, "bin")
CACHE_DIR = os.path.join(APP_DIR, "cache")

# Static windows builds (essentials = smallest useful build).
FFMPEG_DOWNLOAD_SOURCES: list[tuple[str, str]] = [
    (
        "gyan.dev (release-essentials)",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    ),
    (
        "BtbN (master gpl)",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip",
    ),
]

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".3gp", ".rm", ".rmvb", ".asf",
    ".f4v", ".ogv", ".divx", ".mxf",
}

# Output folder layout. A job can either write into the user-chosen folder
# ("root" / "sub") or next to the source video, inside a folder named after it
# ("beside" / "beside_sub") - see build_output_dir().
OUTPUT_AUTO_SUBFOLDER = "拆分片段"
PREVIEW_SUBFOLDER = "预览图"
# Preview frames are named `<视频文件名>_缩略图.png`, so a folder full of them
# is self-describing whatever it sits in. Set PREVIEW_NUMBERING = True to get
# the old `<视频文件名>_缩略图_001.png` scheme back.
PREVIEW_NAME_SUFFIX = "缩略图"
PREVIEW_NUMBERING = False

CREATE_NO_WINDOW = 0x08000000


def _startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def run_hidden(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a console program without flashing a console window on Windows."""
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name == "nt":
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        kwargs.setdefault("startupinfo", _startupinfo())
    return subprocess.run(list(args), **kwargs)


def popen_hidden(args: Sequence[str], **kwargs) -> subprocess.Popen:
    if os.name == "nt":
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        kwargs.setdefault("startupinfo", _startupinfo())
    return subprocess.Popen(list(args), **kwargs)


# --------------------------------------------------------------------------- #
# FFmpeg discovery / bootstrap
# --------------------------------------------------------------------------- #

_FFMPEG_CACHE: tuple[str, str] | None = None


def _validate(ffmpeg_path: str, ffprobe_path: str) -> bool:
    try:
        r = run_hidden([ffprobe_path, "-version"], timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def locate_ffmpeg(refresh: bool = False) -> tuple[str, str] | None:
    """Return (ffmpeg, ffprobe) absolute paths or None."""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE and not refresh:
        return _FFMPEG_CACHE

    candidates: list[str] = []

    # 1. bundled / self-downloaded
    for sub in ("", "bin", "ffmpeg/bin"):
        candidates.append(os.path.join(BIN_DIR, sub))

    # 2. PATH
    which_ff = shutil.which("ffmpeg")
    which_fp = shutil.which("ffprobe")
    if which_ff and which_fp:
        _FFMPEG_CACHE = (which_ff, which_fp)
        return _FFMPEG_CACHE

    # 3. common install locations
    for root in (
        r"C:\ffmpeg\bin",
        r"C:\Program Files\ffmpeg\bin",
        r"C:\Program Files (x86)\ffmpeg\bin",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        os.path.expandvars(r"%ProgramData%\chocolatey\bin"),
        os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
    ):
        candidates.append(root)

    for folder in candidates:
        ff = os.path.join(folder, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        fp = os.path.join(folder, "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.isfile(ff) and os.path.isfile(fp) and _validate(ff, fp):
            _FFMPEG_CACHE = (ff, fp)
            return _FFMPEG_CACHE

    return None


def _download_file(url: str, dest: str,
                   progress: Callable[[str, float], None] | None = None,
                   cancel: Callable[[], bool] | None = None,
                   tries: int = 8) -> str:
    """
    Download `url` to `dest`, resuming with HTTP ranges and verifying that the
    full Content-Length arrived. Flaky connections are common, and a truncated
    zip would otherwise fail much later with a confusing message.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)

    def report(msg: str, frac: float = -1.0) -> None:
        if progress:
            progress(msg, frac)

    last_error: Exception | None = None
    for attempt in range(tries):
        have = os.path.getsize(dest) if os.path.isfile(dest) else 0
        headers = {"User-Agent": "VideoSplitterStudio/1.0"}
        mode = "wb"
        if have:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                if resp.status == 206:
                    total += have
                elif have and resp.status == 200:
                    have, mode = 0, "wb"      # server ignored the range
                done = have
                last_report = 0.0
                with open(dest, mode) as fh:
                    while True:
                        if cancel and cancel():
                            raise RuntimeError("已取消")
                        chunk = resp.read(512 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        frac = (done / total) if total else -1.0
                        if done - last_report >= 2 * 1024 * 1024 or done == total:
                            last_report = done
                            report(
                                f"正在下载 FFmpeg … {done / 1048576:.1f} MB"
                                + (f" / {total / 1048576:.1f} MB" if total else ""),
                                frac)
            size = os.path.getsize(dest)
            if total and size < total:
                last_error = RuntimeError(f"下载不完整（{size}/{total} 字节）")
                continue
            return dest
        except Exception as exc:                       # noqa: BLE001
            last_error = exc
            if "已取消" in str(exc):
                raise
            time.sleep(1.0 + attempt * 0.5)
    raise RuntimeError(f"下载失败：{url}（{last_error}）")


def download_ffmpeg(
    progress: Callable[[str, float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Download + extract a static FFmpeg build into BIN_DIR. Raises on failure."""
    os.makedirs(BIN_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    last_error: Exception | None = None

    def report(msg: str, frac: float = -1.0) -> None:
        if progress:
            progress(msg, frac)

    for label, url in FFMPEG_DOWNLOAD_SOURCES:
        try:
            report(f"正在从 {label} 下载 FFmpeg …", 0.0)
            archive = os.path.join(CACHE_DIR, "ffmpeg-download.zip")
            _download_file(url, archive, progress=progress, cancel=cancel)

            report("正在解压 FFmpeg …", -1.0)
            with zipfile.ZipFile(archive) as zf:
                wanted = [
                    n for n in zf.namelist()
                    if n.lower().endswith(("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"))
                ]
                if not any(n.lower().endswith("ffmpeg.exe") for n in wanted):
                    raise RuntimeError("压缩包中没有找到 ffmpeg.exe")
                for name in wanted:
                    target = os.path.join(BIN_DIR, os.path.basename(name))
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            try:
                os.remove(archive)
            except OSError:
                pass

            found = locate_ffmpeg(refresh=True)
            if not found:
                raise RuntimeError("解压完成但无法运行 FFmpeg")
            report("FFmpeg 已就绪", 1.0)
            return found
        except Exception as exc:  # try next mirror
            if "已取消" in str(exc):
                raise
            last_error = exc
            report(f"{label} 失败：{exc}", -1.0)

    raise RuntimeError(f"无法自动获取 FFmpeg：{last_error}")


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #

@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    bit_rate: int = 0
    container: str = ""
    rotation: int = 0
    has_audio: bool = False
    error: str = ""

    # ---- derived -------------------------------------------------------- #
    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def size_text(self) -> str:
        return human_size(self.size_bytes)

    @property
    def duration_text(self) -> str:
        return human_time(self.duration)

    @property
    def resolution_text(self) -> str:
        if not self.width:
            return "—"
        return f"{self.width}x{self.height}"

    @property
    def effective_bitrate(self) -> int:
        """bits per second actually consumed on disk (video+audio+container)."""
        if self.bit_rate:
            return self.bit_rate
        if self.duration > 0:
            return int(self.size_bytes * 8 / self.duration)
        return 0

    @property
    def mux_bitrate(self) -> int:
        """bitrate used for size math: prefer stream bitrates, else file bitrate."""
        if self.bit_rate > 0:
            return self.bit_rate
        return self.effective_bitrate


def _parse_fraction(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except Exception:
        return 0.0


def probe(path: str) -> MediaInfo:
    """Read stream metadata with ffprobe."""
    info = MediaInfo(path=path)
    try:
        info.size_bytes = os.path.getsize(path)
    except OSError:
        pass

    found = locate_ffmpeg()
    if not found:
        info.error = "未找到 FFmpeg"
        return info
    ffprobe = found[1]

    args = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    try:
        r = run_hidden(args, timeout=120)
        if r.returncode != 0:
            info.error = (r.stderr or b"").decode("utf-8", "replace").strip()[:400] or "ffprobe 失败"
            return info
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except Exception as exc:
        info.error = str(exc)
        return info

    fmt = data.get("format") or {}
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0
    try:
        info.bit_rate = int(float(fmt.get("bit_rate") or 0))
    except (TypeError, ValueError):
        info.bit_rate = 0
    info.container = (fmt.get("format_name") or "").split(",")[0]

    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not info.video_codec:
            info.video_codec = stream.get("codec_name") or ""
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.fps = _parse_fraction(str(stream.get("avg_frame_rate") or "0")) or \
                _parse_fraction(str(stream.get("r_frame_rate") or "0"))
            if not info.duration:
                try:
                    info.duration = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
            for side in stream.get("side_data_list") or []:
                if "rotation" in side:
                    try:
                        info.rotation = int(float(side["rotation"]))
                    except (TypeError, ValueError):
                        pass
        elif kind == "audio" and not info.audio_codec:
            info.audio_codec = stream.get("codec_name") or ""
            info.has_audio = True
        elif kind == "audio":
            info.has_audio = True

    # some containers (ts, mkv) only expose duration via tags
    if info.duration <= 0:
        tags = fmt.get("tags") or {}
        for key in ("DURATION", "duration"):
            if key in tags:
                info.duration = _parse_clock(tags[key])
                if info.duration > 0:
                    break
    if info.duration <= 0:
        info.error = info.error or "无法读取时长"
    if not info.width and not info.video_codec:
        info.error = info.error or "文件中没有视频流"
    return info


def _parse_clock(text: str) -> float:
    m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", text.strip())
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def human_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} TB"


def human_time(seconds: float, with_ms: bool = False) -> str:
    seconds = max(0.0, float(seconds or 0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if with_ms:
        core = f"{h:d}:{m:02d}:{s:06.3f}"
    else:
        core = f"{h:d}:{m:02d}:{int(s):02d}"
    return core


def parse_time(text: str) -> float:
    """Accept '90', '1:30', '0:01:30', '00:01:30.500'."""
    text = (text or "").strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    parts = text.split(":")
    try:
        parts_f = [float(p) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for value in parts_f:
        total = total * 60 + value
    return max(0.0, total)


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "output"


def _is_same_dir(folder: str, src_file: str) -> bool:
    """True when `folder` is the directory that directly holds `src_file`."""
    if not folder:
        return False
    return os.path.normcase(os.path.abspath(folder)) == os.path.normcase(
        os.path.dirname(os.path.abspath(src_file)))


# --------------------------------------------------------------------------- #
# Thumbnail sheet ("视频合集预览图")
# --------------------------------------------------------------------------- #

def _extract_frame(ffmpeg: str, path: str, at: float, out_path: str,
                   height: int, timeout: int = 60) -> bool:
    args = [
        ffmpeg, "-y", "-v", "error",
        "-ss", f"{max(0.0, at):.3f}",
        "-i", path,
        "-frames:v", "1",
        "-vf", f"scale=-2:{int(height)}",
        "-q:v", "3",
        out_path,
    ]
    try:
        r = run_hidden(args, timeout=timeout)
        return r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False


def _choose_times(duration: float, count: int, skip_head: float = 0.02,
                  skip_tail: float = 0.02) -> list[float]:
    count = max(1, int(count))
    if duration <= 0:
        return [0.0] * count
    start = min(duration * skip_head, max(0.0, duration - 0.1))
    end = max(start, duration - min(duration * skip_tail, duration * 0.5))
    if count == 1:
        return [(start + end) / 2]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def choose_times(duration: float, count: int, skip_head: float = 0.02,
                 skip_tail: float = 0.02) -> list[float]:
    """Evenly spaced capture points for the contact sheet (public helper)."""
    return _choose_times(duration, count, skip_head=skip_head, skip_tail=skip_tail)


def thumbnail_cache_dir(path: str) -> str:
    key = hashlib_key(path)
    folder = os.path.join(CACHE_DIR, "thumbs", key)
    return folder


def hashlib_key(path: str) -> str:
    import hashlib
    try:
        stat = os.stat(path)
        raw = f"{os.path.abspath(path)}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        raw = os.path.abspath(path)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def generate_thumbnails(
    path: str,
    duration: float,
    count: int = 12,
    columns: int = 4,
    tile_height: int = 180,
    sheet: bool = True,
    include_timeline_labels: bool = True,
    times: Sequence[float] | None = None,
    sheet_format: str = "png",
    sheet_quality: int = 92,
    progress: Callable[[str, float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict:
    """
    Extract `count` frames and (optionally) compose them into one contact sheet.

    `times` overrides the automatic evenly spaced sampling, so the GUI can let
    the user pick exact capture points. `sheet_format` is "png" or "jpg".

    Returns {"frames": [png...], "sheet": path|None, "times": [...],
             "columns": n, "format": str}
    Runs without Qt; the GUI wraps the returned PNGs into QPixmap.
    """
    from PIL import Image, ImageDraw  # Pillow is available in the DSH runtime

    found = locate_ffmpeg()
    if not found:
        raise RuntimeError("未找到 FFmpeg")
    ffmpeg = found[0]

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_dir = thumbnail_cache_dir(path)
    os.makedirs(out_dir, exist_ok=True)

    if times:
        chosen = [max(0.0, float(t)) for t in times]
    else:
        chosen = _choose_times(duration, count)
    fmt = "jpg" if str(sheet_format).lower() in ("jpg", "jpeg") else "png"
    stamp = hashlib_key(
        path + f"|{count}|{tile_height}|{fmt}|" + ",".join(f"{t:.3f}" for t in chosen))
    frames: list[str] = []

    for idx, at in enumerate(chosen):
        if cancel and cancel():
            raise RuntimeError("已取消")
        if progress:
            progress(f"提取预览帧 {idx + 1}/{len(chosen)}", idx / max(1, len(chosen)))
        out_png = os.path.join(out_dir, f"{stamp}_{idx:03d}.png")
        if not (os.path.isfile(out_png) and os.path.getsize(out_png) > 0):
            ok = _extract_frame(ffmpeg, path, at, out_png, tile_height)
            if not ok:
                # final fallback: very beginning of the file
                ok = _extract_frame(ffmpeg, path, 0.0, out_png, tile_height)
            if not ok:
                continue
        frames.append(out_png)

    result: dict = {"frames": frames, "sheet": None, "times": chosen,
                    "columns": columns, "format": fmt}
    if not sheet or not frames:
        if progress:
            progress("完成", 1.0)
        return result

    if progress:
        progress("合成预览图 …", 0.92)

    cols = max(1, min(int(columns), len(frames)))
    rows = (len(frames) + cols - 1) // cols
    pad = 6
    label_h = 22 if include_timeline_labels else 0

    tiles = []
    for f in frames:
        img = Image.open(f).convert("RGB")
        tiles.append(img.copy())
        img.close()

    tile_w = max(t.width for t in tiles)
    tile_h = max(t.height for t in tiles)

    sheet_w = pad + cols * (tile_w + pad)
    sheet_h = pad + rows * (tile_h + label_h + pad)
    canvas = Image.new("RGB", (sheet_w, sheet_h), (14, 20, 30))
    draw = ImageDraw.Draw(canvas)

    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (tile_w + pad)
        y = pad + r * (tile_h + label_h + pad)
        canvas.paste(tile, (x, y))
        if include_timeline_labels:
            t = chosen[i] if i < len(chosen) else 0.0
            label = human_time(t, with_ms=True)
            draw.rectangle([x, y + tile_h, x + tile_w, y + tile_h + label_h], fill=(24, 34, 48))
            draw.text((x + 6, y + tile_h + 5), label, fill=(190, 220, 255))

    ext = ".jpg" if fmt == "jpg" else ".png"
    sheet_path = os.path.join(out_dir, f"{stamp}_sheet{ext}")
    if fmt == "jpg":
        canvas.save(sheet_path, "JPEG", quality=max(40, min(100, int(sheet_quality))))
    else:
        canvas.save(sheet_path, "PNG")
    canvas.close()
    for t in tiles:
        t.close()

    result["sheet"] = sheet_path
    result["columns"] = cols
    if progress:
        progress("完成", 1.0)
    return result


def capture_times(duration: float, interval: float, count: int) -> list[float]:
    """
    Capture points for the preview frames, from the head to the tail of the clip.

    `interval` <= 0 means "fit `count` frames into the whole clip"; a positive
    interval means "one frame every N seconds, starting at 0". Either way the
    last point lands on the end of the clip, and the result never exceeds
    `count` points.
    """
    limit = max(1, int(count))
    if duration <= 0:
        return [0.0]
    if interval <= 0:
        return _choose_times(duration, limit)
    times: list[float] = []
    at = 0.0
    while at < duration and len(times) < limit:
        times.append(at)
        at += interval
    if not times:
        times = [0.0]
    return times


def build_preview_dir(src: str, flat: bool = False) -> str:
    """
    Where preview frames are written.

    flat=False (default) - a folder named after the video, beside it:
        D:\\clips\\a.mp4  ->  D:\\clips\\a\\a_缩略图.png
    flat=True - right beside the video, no folder at all:
        D:\\clips\\a.mp4  ->  D:\\clips\\a_缩略图.png
    """
    if flat:
        return os.path.dirname(os.path.abspath(src))
    folder = os.path.join(os.path.dirname(os.path.abspath(src)),
                          safe_filename(os.path.splitext(os.path.basename(src))[0]))
    os.makedirs(folder, exist_ok=True)
    return folder


def preview_frame_name(src: str, index: int = 1, pad: int = 3,
                       suffix: str = PREVIEW_NAME_SUFFIX,
                       numbered: bool = PREVIEW_NUMBERING) -> str:
    """
    `a.mp4` -> `a_缩略图.png` (default), or `a_缩略图_001.png` when numbered.

    Plain names are the default because that is what people expect to see next
    to the video; a second 📷 run then lands on `a_缩略图 (2).png` instead of
    climbing through `_002`, `_003`…
    """
    base = safe_filename(os.path.splitext(os.path.basename(src))[0])
    tail = f"_{suffix}" if suffix else ""
    if not numbered:
        return f"{base}{tail}.png"
    return f"{base}{tail}_{int(index):0{max(1, int(pad))}d}.png"


def preview_frame_sample(src: str = "我的片子.mp4", pad: int = 3,
                         suffix: str = PREVIEW_NAME_SUFFIX,
                         numbered: bool = PREVIEW_NUMBERING) -> str:
    """Human-readable example of the preview file name (used in the UI hints)."""
    return preview_frame_name(src, 1, pad, suffix, numbered)


def preview_frame_glob(src: str, suffix: str = PREVIEW_NAME_SUFFIX) -> str:
    """
    Glob that matches every preview frame of `src` in one folder.

    Matches the numbered form as well (`a_缩略图_001.png`), so frames written by
    an older build are still picked up and never overwritten.
    """
    base = safe_filename(os.path.splitext(os.path.basename(src))[0])
    tail = f"_{suffix}" if suffix else ""
    return f"{base}{tail}*"


def list_preview_files(src: str, flat: bool = False,
                       suffix: str = PREVIEW_NAME_SUFFIX) -> list[str]:
    """Every preview frame of `src` that already exists, oldest index first."""
    folder = build_preview_dir(src, flat=flat)
    if not os.path.isdir(folder):
        return []
    # Compare against the fixed part of the name: preview_frame_glob() ends in
    # "*", and including that wildcard in a prefix test can never match.
    prefix = preview_frame_glob(src, suffix).rstrip("*").lower()
    names = [n for n in os.listdir(folder)
             if n.lower().startswith(prefix) and n.lower().endswith(".png")]
    return [os.path.join(folder, n) for n in sorted(names)]


def next_preview_index(src: str, flat: bool = False,
                       suffix: str = PREVIEW_NAME_SUFFIX) -> int:
    """
    First free number for this video's preview frames.

    Saving the same video twice must not restart at 001 (that would make
    `unique_path` tack " (2)" onto the name), so the next free index is read
    back from the files already on disk.
    """
    highest = 0
    for path in list_preview_files(src, flat=flat, suffix=suffix):
        stem = os.path.splitext(os.path.basename(path))[0]
        tail = stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest + 1


def extract_preview_frames(
    src: str,
    out_dir: str,
    times: Sequence[float],
    *,
    height: int = 220,
    pad: int = 3,
    suffix: str = PREVIEW_NAME_SUFFIX,
    numbered: bool = PREVIEW_NUMBERING,
    keep_numbering: bool = True,
    flat: bool | None = None,
    progress: Callable[[str, float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    overwrite: bool = False,
) -> list[dict]:
    """
    Save one PNG per capture point as `<视频文件名>_缩略图.png`, in `out_dir`.

    With `numbered=True` the old `<视频文件名>_缩略图_001.png` scheme comes back
    and a re-run continues the numbering instead of clobbering earlier frames.
    Unnumbered runs go through `unique_path()`, so a second run lands on
    `_缩略图 (2).png` rather than overwriting. Returns
    [{"path", "time", "fresh"}], where `fresh` is False for a file that already
    existed.
    """
    found = locate_ffmpeg()
    if not found:
        raise RuntimeError("未找到 FFmpeg")
    ffmpeg = found[0]
    os.makedirs(out_dir, exist_ok=True)

    start = 1
    if numbered and keep_numbering and not overwrite:
        beside = _is_same_dir(out_dir, src) if flat is None else bool(flat)
        start = next_preview_index(src, flat=beside, suffix=suffix)

    written: list[dict] = []
    total = max(1, len(times))
    # One capture -> exactly `a_缩略图.png`. Several captures cannot all carry
    # that name, so they fall back to the numbered scheme rather than producing
    # a pile of " (2)", " (3)" copies.
    use_numbers = numbered or total > 1
    start = 1 if not use_numbers else start
    for idx, at in enumerate(times):
        if cancel and cancel():
            raise RuntimeError("已取消")
        at = max(0.0, float(at))
        if progress:
            progress(f"保存预览帧 {idx + 1}/{total}", idx / total)
        out_png = os.path.join(
            out_dir, preview_frame_name(src, start + idx, pad, suffix, use_numbers))
        if os.path.isfile(out_png) and os.path.getsize(out_png) > 0 and not overwrite:
            written.append({"path": out_png, "time": at, "fresh": False})
            continue
        if not _extract_frame(ffmpeg, src, at, out_png, height):
            continue
        written.append({"path": out_png, "time": at, "fresh": True})
    if progress:
        progress("完成", 1.0)
    return written


def compose_sheet(
    frames: Sequence[str],
    times: Sequence[float],
    out_path: str,
    columns: int = 4,
    include_timeline_labels: bool = True,
    quality: int = 92,
) -> str:
    """Combine already-extracted preview frames into a single sheet image."""
    from PIL import Image, ImageDraw

    if not frames:
        raise RuntimeError("没有可用的预览帧")
    fmt = "jpg" if os.path.splitext(out_path)[1].lower() in (".jpg", ".jpeg") else "png"
    cols = max(1, min(int(columns), len(frames)))
    rows = (len(frames) + cols - 1) // cols
    pad = 6
    label_h = 22 if include_timeline_labels else 0

    tiles = []
    for f in frames:
        img = Image.open(f).convert("RGB")
        tiles.append(img.copy())
        img.close()

    tile_w = max(t.width for t in tiles)
    tile_h = max(t.height for t in tiles)
    canvas = Image.new("RGB", (pad + cols * (tile_w + pad),
                               pad + rows * (tile_h + label_h + pad)), (14, 20, 30))
    draw = ImageDraw.Draw(canvas)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (tile_w + pad)
        y = pad + r * (tile_h + label_h + pad)
        canvas.paste(tile, (x, y))
        if include_timeline_labels:
            t = times[i] if i < len(times) else 0.0
            draw.rectangle([x, y + tile_h, x + tile_w, y + tile_h + label_h], fill=(24, 34, 48))
            draw.text((x + 6, y + tile_h + 5), human_time(t, with_ms=True),
                      fill=(190, 220, 255))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    if fmt == "jpg":
        canvas.save(out_path, "JPEG", quality=max(40, min(100, int(quality))))
    else:
        canvas.save(out_path, "PNG")
    canvas.close()
    for t in tiles:
        t.close()
    return out_path


# --------------------------------------------------------------------------- #
# Split planning
# --------------------------------------------------------------------------- #

MODE_BY_DURATION = "duration"   # each segment has the same length
MODE_BY_COUNT = "count"         # split into N equal parts
MODE_BY_SIZE = "size"           # each segment targets a max file size
MODE_BY_KEYFRAME = "keyframe"   # split at every keyframe (stream copy, frame exact)
MODE_CUSTOM = "custom"          # user provided ranges


@dataclass
class SplitSegment:
    index: int
    start: float
    end: float
    label: str = ""
    estimated_bytes: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def range_text(self) -> str:
        return f"{human_time(self.start, True)} → {human_time(self.end, True)}"


def list_keyframes(path: str, timeout: int = 600) -> list[float]:
    """All keyframe (packet with key_flag) timestamps, in seconds."""
    found = locate_ffmpeg()
    if not found:
        return []
    ffprobe = found[1]
    args = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
        "-of", "csv=p=0", path,
    ]
    try:
        r = run_hidden(args, timeout=timeout)
    except Exception:
        return []
    times: list[float] = []
    for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            times.append(float(line))
        except ValueError:
            continue
    times = sorted(t for t in times if t >= 0)
    return times


def plan_split(
    info: MediaInfo,
    mode: str,
    *,
    segment_seconds: float = 60.0,
    part_count: int = 2,
    target_mb: float = 100.0,
    custom_ranges: Sequence[tuple[float, float]] | None = None,
    keyframes: Sequence[float] | None = None,
) -> list[SplitSegment]:
    duration = max(0.0, info.duration)
    if duration <= 0:
        duration = 0.0

    if mode == MODE_BY_DURATION:
        seg = max(0.5, float(segment_seconds))
        segments: list[SplitSegment] = []
        start = 0.0
        idx = 1
        # avoid a tiny tail segment: merge it into the previous one
        while start < duration - 1e-6:
            end = min(duration, start + seg)
            remaining = duration - end
            if 0 < remaining < min(1.0, seg * 0.25):
                end = duration
            segments.append(SplitSegment(idx, start, end, f"片段 {idx:03d}"))
            idx += 1
            start = end
            if duration <= 0:
                break
        return _with_estimates(segments, info)

    if mode == MODE_BY_COUNT:
        n = max(1, int(part_count))
        if duration <= 0:
            return []
        step = duration / n
        segments = []
        for i in range(n):
            segments.append(
                SplitSegment(i + 1, i * step, duration if i == n - 1 else (i + 1) * step,
                             f"片段 {i + 1:03d}")
            )
        return _with_estimates(segments, info)

    if mode == MODE_BY_SIZE:
        target = max(1.0, float(target_mb)) * 1024 * 1024
        bitrate = info.mux_bitrate
        if bitrate <= 0 or duration <= 0:
            return []
        # bytes = bitrate/8 * seconds  ->  seconds = bytes * 8 / bitrate
        seg_seconds = target * 8.0 / bitrate
        # audio+video streams usually come out a few percent under the mux rate,
        # subtract a safety margin so we do not overshoot the requested size
        seg_seconds *= 0.97
        return plan_split(info, MODE_BY_DURATION, segment_seconds=seg_seconds,
                          keyframes=keyframes)

    if mode == MODE_BY_KEYFRAME:
        kfs = sorted(set(float(k) for k in (keyframes or []) if k >= 0))
        if not kfs:
            return []
        segments = []
        for i, start in enumerate(kfs):
            end = kfs[i + 1] if i + 1 < len(kfs) else duration
            if end - start <= 0.04:          # skip duplicate/near-zero spans
                continue
            segments.append(SplitSegment(len(segments) + 1, start, end,
                                         f"KF {len(segments) + 1:04d}"))
        return _with_estimates(segments, info)

    if mode == MODE_CUSTOM:
        segments = []
        for i, (a, b) in enumerate(custom_ranges or []):
            a = max(0.0, float(a))
            b = float(b)
            if duration > 0:
                b = min(b, duration)
            if b - a <= 0.04:
                continue
            segments.append(SplitSegment(len(segments) + 1, a, b, f"片段 {len(segments) + 1:03d}"))
        return _with_estimates(segments, info)

    return []


def _with_estimates(segments: list[SplitSegment], info: MediaInfo) -> list[SplitSegment]:
    bitrate = info.mux_bitrate
    if bitrate > 0:
        for s in segments:
            s.estimated_bytes = int(bitrate / 8.0 * s.duration)
    return segments


# --------------------------------------------------------------------------- #
# Keyframe-aware cut planning
# --------------------------------------------------------------------------- #

_KEYFRAME_CACHE: dict[tuple[str, int, int], list[float]] = {}


def cached_keyframes(path: str) -> list[float]:
    """Keyframe timestamps with a stat-based cache (scanning can be slow)."""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path).lower(), st.st_size, int(st.st_mtime))
    except OSError:
        key = (os.path.abspath(path).lower(), 0, 0)
    hit = _KEYFRAME_CACHE.get(key)
    if hit is not None:
        return hit
    keys = list_keyframes(path)
    _KEYFRAME_CACHE[key] = keys
    return keys


def segment_is_copy_safe(start: float, keyframes: Sequence[float],
                         tolerance: float = 0.25) -> bool:
    """
    True when a stream copy starting at `start` will begin on a keyframe.

    Stream copy can only start at a keyframe. When the requested start sits
    between two keyframes, FFmpeg silently snaps forward to the next one: the
    segment either comes out short or - if the next keyframe is beyond the
    segment's end - as an empty file.
    """
    if not keyframes:
        return False
    if start <= tolerance:
        return True                       # the file's first frame is a keyframe
    return any(abs(k - start) <= tolerance for k in keyframes)


def plan_needs_reencode(
    src: str,
    segments: Sequence[SplitSegment],
    progress: Callable[[str, float], None] | None = None,
) -> tuple[list[SplitSegment], list[float]]:
    """Split `segments` into (copy-safe, needs-reencode) groups."""
    if progress:
        progress("正在检查关键帧 …", -1.0)
    keys = cached_keyframes(src)
    safe = [s for s in segments if segment_is_copy_safe(s.start, keys)]
    unsafe = [s for s in segments if not segment_is_copy_safe(s.start, keys)]
    return safe, unsafe


# --------------------------------------------------------------------------- #
# Cutting
# --------------------------------------------------------------------------- #

@dataclass
class CutOptions:
    reencode: bool = False
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 20
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    extra_input_args: list[str] = field(default_factory=list)
    overwrite: bool = True


def _probe_duration(path: str) -> float:
    found = locate_ffmpeg()
    if not found:
        return 0.0
    try:
        r = run_hidden([
            found[1], "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", path,
        ], timeout=60)
        return float((r.stdout or b"0").decode("utf-8", "replace").strip().split(",")[0] or 0.0)
    except Exception:
        return 0.0


def cut_segment(
    src: str,
    out_path: str,
    start: float,
    end: float,
    opts: CutOptions | None = None,
    progress: Callable[[str, float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    duration_hint: float = 0.0,
) -> tuple[bool, str]:
    """Cut [start, end) out of `src` into `out_path`. Returns (ok, message)."""
    opts = opts or CutOptions()
    found = locate_ffmpeg()
    if not found:
        return False, "未找到 FFmpeg"
    ffmpeg = found[0]

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    args = [ffmpeg, "-hide_banner", "-v", "warning", "-stats"]
    if opts.overwrite:
        args.append("-y")

    # Seek twice: an input -ss gets us there fast (keyframe seek), an output
    # -ss/-t then trims exactly. With stream copy the second seek only snaps to
    # the next keyframe, which is precisely what we want; with re-encoding it
    # lands on the exact frame. Doing the input seek alone is unreliable: for
    # input seeking -t is evaluated against the *original* timestamps, so a
    # segment starting at 8 s could easily run twice as long as requested.
    start = max(0.0, float(start))
    length = max(0.05, float(end) - float(start))
    seek_to = max(0.0, start - 2.0)          # keyframes every few seconds
    preroll = start - seek_to

    args += ["-ss", f"{seek_to:.3f}", "-i", src, "-ss", f"{preroll:.3f}"]

    if opts.reencode:
        args += [
            "-t", f"{length:.3f}",
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c:v", opts.video_codec, "-preset", opts.preset, "-crf", str(int(opts.crf)),
            "-pix_fmt", "yuv420p",
            "-c:a", opts.audio_codec, "-b:a", opts.audio_bitrate,
            "-movflags", "+faststart",
            out_path,
        ]
    else:
        args += ["-t", f"{length:.3f}"]
        args += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
        if os.path.splitext(out_path)[1].lower() in (".mp4", ".m4v", ".mov"):
            args += ["-movflags", "+faststart"]
        # keep the cut starting at zero and avoid a gap of empty packets
        args += ["-avoid_negative_ts", "make_zero", "-reset_timestamps", "1", out_path]

    total = duration_hint or length
    try:
        proc = popen_hidden(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, f"无法启动 FFmpeg：{exc}"

    tail: list[str] = []
    time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel and cancel():
                try:
                    proc.kill()
                except Exception:
                    pass
                return False, "已取消"
            line = line.strip()
            if line:
                tail.append(line)
                if len(tail) > 6:
                    tail.pop(0)
            m = time_re.search(line)
            if m and progress and total > 0:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                progress("切割中", min(0.999, secs / total))
        proc.wait()
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"FFmpeg 运行异常：{exc}"

    if proc.returncode != 0:
        return False, "；".join(tail[-3:]) or f"FFmpeg 返回码 {proc.returncode}"

    if not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
        return False, "输出文件为空"

    if progress:
        progress("完成", 1.0)
    return True, ""


def cut_all(
    src: str,
    segments: Sequence[SplitSegment],
    out_dir: str,
    base_name: str,
    ext: str = "",
    opts: CutOptions | None = None,
    progress: Callable[[str, float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    on_file: Callable[[str], None] | None = None,
    start_index: int = 1,
    pad: int = 3,
    smart: bool = False,
    reencode_opts: CutOptions | None = None,
) -> list[tuple[SplitSegment, str, str]]:
    """
    Cut every segment in order. Returns [(segment, output_path, error)].

    `start_index` is the number used for the first output file (default 1), and
    `pad` the number of digits, so a 2-file job produces `_01`/`_02` when asked.

    `smart=True` re-encodes only those segments whose start is not keyframe
    aligned (see `segment_is_copy_safe`), so a source with sparse keyframes
    still yields segments of the requested length instead of empty files.
    Everything else is still stream-copied, which keeps the run fast.

    `progress(message, fraction)` is called with overall progress 0..1.
    """
    opts = opts or CutOptions()
    ext = ext or os.path.splitext(src)[1] or ".mp4"
    os.makedirs(out_dir, exist_ok=True)

    unsafe: set[int] = set()
    if smart and not opts.reencode:
        for seg in segments:
            if not segment_is_copy_safe(seg.start, cached_keyframes(src)):
                unsafe.add(id(seg))
        if unsafe and progress:
            progress(f"有 {len(unsafe)} 个片段无法从关键帧开始，将单独重新编码", -1.0)
    reencode_opts = reencode_opts or CutOptions(reencode=True)
    reencode_opts.reencode = True

    total = len(segments)
    results: list[tuple[SplitSegment, str, str]] = []
    digits = max(1, int(pad))
    offset = max(0, int(start_index) - 1)

    for i, seg in enumerate(segments):
        if cancel and cancel():
            break
        if not os.path.isfile(src):
            results.append((seg, "", f"源文件不存在：{src}"))
            break

        number = offset + i + 1
        out_path = os.path.join(out_dir, f"{base_name}_{number:0{digits}d}{ext}")
        seg_opts = reencode_opts if id(seg) in unsafe else opts

        def sub_progress(msg: str, frac: float, _i=i, _seg=seg) -> None:
            if progress:
                overall = (_i + max(0.0, min(1.0, frac))) / max(1, total)
                tag = "重编码" if _seg is not None and id(_seg) in unsafe else ""
                progress(f"[{_i + 1}/{total}] {_seg.range_text}"
                         f"{' · ' + tag if tag else ''} · {msg}", overall)

        def sub_cancel() -> bool:
            return bool(cancel and cancel())

        ok, msg = cut_segment(
            src, out_path, seg.start, seg.end, seg_opts,
            progress=sub_progress, cancel=sub_cancel, duration_hint=seg.duration,
        )
        if ok:
            try:
                seg.estimated_bytes = os.path.getsize(out_path)
            except OSError:
                pass
            if on_file:
                on_file(out_path)
            results.append((seg, out_path, ""))
        else:
            results.append((seg, out_path, msg))
            if msg == "已取消":
                break

    if progress:
        progress("全部完成", 1.0)
    return results


def build_output_dir(src: str, out_root: str, subfolder: bool = True,
                     mode: str = "root") -> str:
    """
    Decide where a source's split segments are written.

    mode="root"   : under the chosen output folder (optionally one sub-folder
                    named after the video)
    mode="beside" : next to the source video, in a folder named after it -
                    `D:\\clips\\a.mp4` -> `D:\\clips\\a\\` - with the segments
                    one level deeper in `拆分片段\\`, so they never mix with the
                    preview frames saved in `D:\\clips\\a\\`.
    """
    if mode == "beside":
        folder = os.path.join(os.path.dirname(os.path.abspath(src)),
                              safe_filename(os.path.splitext(os.path.basename(src))[0]))
        if subfolder:
            folder = os.path.join(folder, OUTPUT_AUTO_SUBFOLDER)
        return folder
    out_root = out_root or os.path.dirname(os.path.abspath(src))
    if not subfolder:
        return out_root
    folder = safe_filename(os.path.splitext(os.path.basename(src))[0])
    return os.path.join(out_root, folder)


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{root} ({n}){ext}"):
        n += 1
    return f"{root} ({n}){ext}"


def open_in_explorer(path: str) -> bool:
    """
    Reveal a file/folder in the OS file manager. Returns True when it worked.

    Never raises: this runs from a Qt signal handler, and an exception there
    (e.g. WinError 5 when the shell refuses to open the folder) aborts the rest
    of the handler and surfaces as a bare traceback in the console.
    """
    target = path if os.path.isdir(path) else os.path.dirname(path)
    if not target or not os.path.isdir(target):
        return False
    try:
        if os.name == "nt":
            os.startfile(target)  # noqa: S606
        else:
            popen_hidden(["xdg-open", target])
        return True
    except OSError:
        return False


def open_file(path: str) -> bool:
    """Open one file with its default application. Never raises."""
    if not os.path.isfile(path):
        return False
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            popen_hidden(["xdg-open", path])
        return True
    except OSError:
        return False


def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    folder = os.path.join(base, "VideoSplitterStudio")
    os.makedirs(folder, exist_ok=True)
    return folder


class Canceller:
    """Tiny thread-safe cancel flag shared with worker threads."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()

    # convenience for APIs expecting a callable
    def __call__(self) -> bool:
        return self._event.is_set()


__all__ = [
    "APP_DIR", "BIN_DIR", "CACHE_DIR", "VIDEO_EXTENSIONS",
    "OUTPUT_AUTO_SUBFOLDER", "PREVIEW_SUBFOLDER", "PREVIEW_NAME_SUFFIX",
    "MediaInfo", "probe", "locate_ffmpeg", "download_ffmpeg",
    "generate_thumbnails", "compose_sheet", "thumbnail_cache_dir", "choose_times",
    "capture_times", "build_preview_dir", "preview_frame_name", "extract_preview_frames",
    "preview_frame_sample", "preview_frame_glob", "list_preview_files",
    "next_preview_index",
    "SplitSegment", "CutOptions", "plan_split", "list_keyframes", "cached_keyframes",
    "segment_is_copy_safe", "plan_needs_reencode",
    "cut_segment", "cut_all", "build_output_dir", "unique_path",
    "human_size", "human_time", "parse_time", "safe_filename",
    "open_in_explorer", "open_file", "app_data_dir", "Canceller",
    "MODE_BY_DURATION", "MODE_BY_COUNT", "MODE_BY_SIZE",
    "MODE_BY_KEYFRAME", "MODE_CUSTOM",
    "run_hidden", "popen_hidden",
]
