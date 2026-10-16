"""Fetch PySide6 into ./vendor by downloading wheels directly (no pip).

pip cannot be used here: the workspace ACL denies DeleteSubdirectoriesAndFiles
for Everyone, which breaks pip's temp-file bookkeeping. Downloading the wheels
and unpacking them with zipfile is equivalent and side-steps that completely.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
TMP = os.path.join(HERE, ".tmp")
PKGS = ["PySide6-Essentials", "PySide6-Addons", "shiboken6"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) VideoSplitterStudio/1.0"}


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, dest: str, expect: int = 0, tries: int = 10) -> str:
    """Download with HTTP range resume; verify size when `expect` is known."""
    last = None
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
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                if have and resp.status != 206:        # server ignored the range
                    have, mode = 0, "wb"
                total = int(resp.headers.get("Content-Length") or 0)
                if resp.status == 206 and expect:
                    total += have
                got = have
                with open(dest, mode) as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if total:
                            log(f"   {got * 100 // total:3d}%  "
                                f"{got / 1048576:.1f}/{total / 1048576:.1f} MB")
            size = os.path.getsize(dest)
            if expect and size < expect:
                log(f"   incomplete ({size}/{expect}), resuming …")
                last = RuntimeError("incomplete download")
                time.sleep(1.0)
                continue
            return dest
        except Exception as exc:                       # noqa: BLE001
            last = exc
            log(f"   retry {attempt + 1}/{tries}: {exc}")
            time.sleep(1.0 + attempt * 0.5)
    raise RuntimeError(f"download failed: {url}: {last}")


def pick_wheel(files: list[dict]) -> dict:
    cands = [
        f for f in files
        if f["filename"].endswith(".whl")
        and "win_amd64" in f["filename"]
        and "cp3" in f["filename"].split("-")[2]
    ]
    if not cands:
        raise RuntimeError("no win_amd64 wheel found")
    # prefer the most ABI-agnostic build (abi3 wins over cp312 etc.)
    cands.sort(key=lambda f: (0 if "abi3" in f["filename"] else 1, f["filename"]))
    return cands[0]


def main() -> int:
    os.makedirs(VENDOR, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)

    for pkg in PKGS:
        log(f"== {pkg} ==")
        with urllib.request.urlopen(
            urllib.request.Request(f"https://pypi.org/pypi/{pkg}/json", headers=UA),
            timeout=60,
        ) as r:
            meta = json.load(r)
        wheel = pick_wheel(meta["urls"])
        log(f"   wheel: {wheel['filename']}  ({wheel['size']/1048576:.1f} MB)")
        archive = os.path.join(TMP, wheel["filename"])
        fetch(wheel["url"], archive, expect=wheel["size"])
        log("   unpacking …")
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                target = os.path.join(VENDOR, member)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        log("   done")

    sys.path.insert(0, VENDOR)
    import PySide6  # noqa: F401
    log(f"\nOK  PySide6 {PySide6.__version__} -> {VENDOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
