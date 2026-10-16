"""
build_exe.py - package Video Splitter Studio as a standalone Windows .exe.

It builds with PyInstaller in **onedir** mode:

    dist/VideoSplitterStudio/
        VideoSplitterStudio.exe      <- double-click to run
        _internal/                   <- Qt runtime, no need to touch
        bin/                         <- ffmpeg goes here (auto-downloaded)
        README.txt

Why onedir instead of a single file: the app ships a full Qt stack plus a
100 MB FFmpeg build. A onefile exe would unpack ~200 MB to a temp folder on
every launch; a folder starts in about a second instead.

PyInstaller is fetched into .build_tools/ using the same direct-wheel approach
as fetch_deps.py, because pip cannot run in this environment.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, ".build_tools")
TMP = os.path.join(HERE, ".tmp")
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(HERE, "dist")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) VideoSplitterStudio/1.0"}

# PyInstaller and everything it needs; bundled wheels for the missing ones.
BUILD_REQUIREMENTS = [
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "pefile",
    "pywin32-ctypes",
    "packaging",
    "setuptools",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, dest: str, expect: int = 0, tries: int = 10) -> str:
    """Download with HTTP range resume and a size check."""
    last: Exception | None = None
    for attempt in range(tries):
        have = os.path.getsize(dest) if os.path.isfile(dest) else 0
        if expect and have >= expect:
            return dest
        headers = dict(UA)
        mode = "wb"
        if have:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=120) as resp:
                if have and resp.status == 200:
                    have, mode = 0, "wb"
                total = int(resp.headers.get("Content-Length") or 0)
                if resp.status == 206:
                    total += have
                got = have
                with open(dest, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                if total and os.path.getsize(dest) < total:
                    last = RuntimeError("incomplete download")
                    continue
            return dest
        except Exception as exc:                       # noqa: BLE001
            last = exc
            log(f"   retry {attempt + 1}/{tries}: {exc}")
            time.sleep(1.0 + attempt * 0.5)
    raise RuntimeError(f"download failed: {url}: {last}")


def pick_wheel(files: list[dict]) -> dict:
    wheels = [f for f in files if f["filename"].endswith(".whl")]
    pure = [f for f in wheels if "py3-none-any" in f["filename"]]
    win = [f for f in wheels if "win_amd64" in f["filename"]]
    pick = (pure or win or wheels)
    if not pick:
        raise RuntimeError("no wheel available")
    pick.sort(key=lambda f: f["filename"], reverse=True)
    return pick[0]


def pypi_metadata(package: str, tries: int = 6) -> dict:
    """Fetch a package's JSON metadata, retrying through flaky TLS."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(f"https://pypi.org/pypi/{package}/json",
                                           headers=UA), timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"{package}: {last}")


def install_build_tools() -> None:
    """Unpack PyInstaller + deps into .build_tools (no pip required)."""
    os.makedirs(TOOLS, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)
    for package in BUILD_REQUIREMENTS:
        try:
            meta = pypi_metadata(package)
        except Exception as exc:                       # noqa: BLE001
            # a package that is already unpacked (or genuinely unavailable)
            # must not abort the build
            if os.path.isdir(os.path.join(TOOLS, package.replace("-", "_"))):
                log(f"  {package}: using existing copy ({exc})")
                continue
            log(f"  {package}: skipped ({exc})")
            continue
        wheel = pick_wheel(meta["urls"])
        marker = os.path.join(TOOLS, "." + wheel["filename"] + ".done")
        if os.path.isfile(marker):
            log(f"  {package}: already unpacked")
            continue
        archive = os.path.join(TMP, wheel["filename"])
        log(f"  {package}: {wheel['filename']} ({wheel['size'] / 1048576:.1f} MB)")
        fetch(wheel["url"], archive, expect=wheel["size"])
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                target = os.path.join(TOOLS, member)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        with io.open(marker, "w", encoding="utf-8") as fh:
            fh.write(wheel["filename"])


def make_icon() -> str | None:
    """Render the app mark to a multi-resolution .ico for the exe."""
    ico = os.path.join(HERE, "app.ico")
    try:
        sys.path.insert(0, os.path.join(HERE, "vendor"))
        sys.path.insert(0, HERE)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QApplication

        from ui_common import make_app_icon
        from PIL import Image

        app = QApplication.instance() or QApplication([])
        icon = make_app_icon(256)
        sizes = [16, 24, 32, 48, 64, 128, 256]
        png_paths = []
        for size in sizes:
            pm = icon.pixmap(QSize(size, size))
            if pm.isNull():
                continue
            path = os.path.join(TMP, f"icon_{size}.png")
            os.makedirs(TMP, exist_ok=True)
            if pm.save(path, "PNG"):
                png_paths.append((size, path))
        if not png_paths:
            return None
        base = Image.open(png_paths[-1][1]).convert("RGBA")
        base.save(ico, format="ICO",
                  sizes=[(s, s) for s, _ in png_paths])
        return ico if os.path.isfile(ico) else None
    except Exception as exc:                            # noqa: BLE001
        log(f"   icon skipped ({exc})")
        return None


VERSION = (1, 0, 0, 0)
VERSION_STR = "1.0.0.0"


def make_version_file() -> str:
    """
    Write the Windows version resource for the exe.

    Explorer's Properties -> Details and the "publisher" line of the UAC /
    SmartScreen dialogs come from here. A binary with no version resource looks
    unattended, which is exactly what security prompts react to, so this is part
    of the same story as signing the exe (`sign_exe.py`).
    """
    path = os.path.join(HERE, "version_info.txt")
    contents = f'''# Generated by build_exe.py - edit that script, not this file.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION},
    prodvers={VERSION},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'Video Splitter Studio'),
         StringStruct('FileDescription', 'Video Splitter Studio - 视频拆分与预览工具'),
         StringStruct('FileVersion', '{VERSION_STR}'),
         StringStruct('InternalName', 'VideoSplitterStudio'),
         StringStruct('LegalCopyright', 'Video Splitter Studio'),
         StringStruct('OriginalFilename', 'VideoSplitterStudio.exe'),
         StringStruct('ProductName', 'Video Splitter Studio'),
         StringStruct('ProductVersion', '{VERSION_STR}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
'''
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(contents)
    return path


def make_spec() -> str:
    spec = os.path.join(HERE, "VideoSplitterStudio.spec")
    vendor = os.path.join(HERE, "vendor")
    icon = make_icon()
    icon_line = f"    icon={icon!r},\n" if icon else ""
    version_file = make_version_file()
    version_line = f"    version={version_file!r},\n"
    contents = f'''# -*- mode: python ; coding: utf-8 -*-
# Generated by build_exe.py - edit that script, not this file.

import os
import sys

HERE = {HERE!r}
VENDOR = {vendor!r}
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

block_cipher = None

hidden = [
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PIL", "PIL.Image", "PIL.ImageDraw",
    "video_tools", "ui_common", "player", "main_window",
]

a = Analysis(
    ["main.py"],
    pathex=[HERE, VENDOR],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data", "PySide6.QtWebEngineCore"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoSplitterStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
{version_line}{icon_line})
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VideoSplitterStudio",
)
'''
    with io.open(spec, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(contents)
    return spec


README_TXT = """Video Splitter Studio
====================

双击 VideoSplitterStudio.exe 即可运行。

首次运行时程序会：

  1. 检测本目录下的 bin\\ffmpeg.exe；
  2. 如果没有，就从网上下载一个静态 FFmpeg 到 bin\\（约 110 MB，只需一次）；
  3. 之后每次启动都是秒开，全程可以离线使用。

如果没有网络，也可以自己把 ffmpeg.exe / ffprobe.exe 放进 bin\\ 目录。

缓存目录：cache\\（缩略图与预览帧，可随时删除重建）
输出位置：默认写到「视频源文件旁的『视频名』文件夹」，可在界面里改成指定目录。

关于「无法验证发布者」：
  本程序已带数字签名（发布者：Video Splitter Studio）。
  要让本机不再提示，请运行一次：
      python sign_exe.py trust
  或双击 signing\\vss-selfsigned.cer，把它装进
  「受信任的根证书颁发机构」和「受信任的发布者」。
  如果是从网上下载的压缩包解出来的，可能还带「来自 Internet」标记，
  可以运行 python sign_exe.py unblock 清除。

功能：
  · 拖入 / 添加多个视频形成队列，每行标出来源文件夹
  · 可视化预览播放（画面 + 音频），鼠标滚轮缩放、放大后拖拽平移、双击还原
  · 视频合集预览图：可设张数与间隔，显示已截取张数，导出 PNG 或 JPG
  · 预览图存图：源视频旁「视频名」文件夹，命名 视频文件名_001.png …（点 📷）
  · 按目标大小(MB) / 每段时长 / 段数 / 关键帧 / 自定义区间拆分
  · 拆分片段默认写入 视频名\\拆分片段\\，队列依次批量拆分，自动编号命名
  · 源文件只读，不会被修改或删除

详细说明见 README.md。
"""


def copy_assets(dist_dir: str) -> None:
    """Ship the README and an empty bin/ so first run has a clear target."""
    os.makedirs(os.path.join(dist_dir, "bin"), exist_ok=True)
    with io.open(os.path.join(dist_dir, "README.txt"), "w",
                 encoding="utf-8", newline="\r\n") as fh:
        fh.write(README_TXT)
    # reuse an already-downloaded ffmpeg so the shipped folder works offline
    src_bin = os.path.join(HERE, "bin")
    if os.path.isdir(src_bin):
        for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
            src = os.path.join(src_bin, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dist_dir, "bin", name))
                log(f"  copied bin/{name}")
    for extra in ("README.md",):
        src = os.path.join(HERE, extra)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dist_dir, extra))


def main() -> int:
    log("== 1. installing build tools into .build_tools ==")
    install_build_tools()

    sys.path.insert(0, TOOLS)
    try:
        import PyInstaller  # noqa: F401
    except Exception as exc:                           # noqa: BLE001
        log(f"PyInstaller 不可用：{exc}")
        return 1

    log("== 2. writing spec ==")
    spec = make_spec()
    log(f"   {spec}")

    log("== 3. running PyInstaller (this takes a few minutes) ==")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([TOOLS, os.path.join(HERE, "vendor"), HERE])
    env["TMP"] = TMP
    env["TEMP"] = TMP
    cmd = [sys.executable, os.path.join(TOOLS, "PyInstaller", "__main__.py"),
           "--noconfirm", "--clean", "--distpath", DIST, "--workpath", BUILD, spec]
    result = subprocess.run(cmd, cwd=HERE, env=env)
    if result.returncode != 0:
        log(f"PyInstaller 失败，返回码 {result.returncode}")
        return result.returncode

    log("== 4. adding README / bin ==")
    dist_dir = os.path.join(DIST, "VideoSplitterStudio")
    if not os.path.isdir(dist_dir):
        log(f"没有找到输出目录：{dist_dir}")
        return 1
    copy_assets(dist_dir)

    exe = os.path.join(dist_dir, "VideoSplitterStudio.exe")

    log("== 5. signing ==")
    # A rebuild replaces the exe, which throws away any previous signature, so
    # signing has to be the last step of the build rather than a manual follow-up.
    if os.environ.get("VSS_SKIP_SIGN"):
        log("   VSS_SKIP_SIGN 已设置，跳过签名")
    else:
        try:
            sign_cmd = [sys.executable, os.path.join(HERE, "sign_exe.py"), "sign"]
            signed = subprocess.run(sign_cmd, cwd=HERE)
            if signed.returncode != 0:
                log("   签名未完成（见上面的输出）。")
                log("   程序本身可以正常使用，只是 Windows 仍会提示"
                    "「无法验证发布者」。")
                log("   修好后单独重跑：python sign_exe.py sign && "
                    "python sign_exe.py trust")
        except Exception as exc:                       # noqa: BLE001
            log(f"   签名步骤出错：{exc}")

    total = 0
    count = 0
    for root, _dirs, files in os.walk(dist_dir):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
            count += 1
    log("")
    log(f"完成：{exe}")
    log(f"文件数 {count}，总体积 {total / 1048576:.1f} MB")
    log("直接把 dist\\VideoSplitterStudio 整个文件夹发给别人即可运行。")
    log("要让本机不再提示「无法验证发布者」，再运行：python sign_exe.py trust")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
