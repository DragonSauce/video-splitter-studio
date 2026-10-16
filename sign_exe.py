"""
sign_exe.py - code-signing toolchain for Video Splitter Studio.

Why this exists
---------------
An unsigned .exe makes Windows complain:

    打开文件 - 安全警告 / 无法验证发布者

That appears whenever the file has no signature Windows can verify. The fixes,
in the order they help:

  1. ship version metadata inside the .exe    (build_exe.py already does)
  2. sign the .exe                            (this script)
  3. let the target PC trust the certificate  (this script, `trust`)

A *self-signed* certificate removes the warning on machines where that
certificate is installed as trusted. To make other people's PCs stop warning you
need a certificate from a public CA (OV, or EV to skip SmartScreen reputation
building). Everything here works with such a certificate too: point `--cert` at
the .pfx the CA issues.

Note on the signer input: `signtool sign /f cert.pfx` imports the private key
into a Windows key container on the fly. On locked-down profiles that fails with
`Store::ImportCertObject() failed (0x80090010)`, so the reliable route is to have
the certificate - and therefore its key container - already installed
(`new-cert` does that) and sign by thumbprint.

Commands
--------
    python sign_exe.py doctor            # what is missing, and why
    python sign_exe.py tools             # fetch signtool.exe if absent
    python sign_exe.py new-cert          # create + install a self-signed cert
    python sign_exe.py sign              # sign dist\\...\\VideoSplitterStudio.exe
    python sign_exe.py verify            # show the Authenticode status
    python sign_exe.py trust             # make this PC trust the publisher
    python sign_exe.py unblock [path]    # drop the Zone.Identifier mark
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, ".build_tools")
TMP_DIR = os.path.join(HERE, ".tmp")
SIGNING = os.path.join(HERE, "signing")
DIST_EXE = os.path.join(HERE, "dist", "VideoSplitterStudio", "VideoSplitterStudio.exe")
PFX = os.path.join(SIGNING, "vss-selfsigned.pfx")
CER = os.path.join(SIGNING, "vss-selfsigned.cer")
# The password for the local self-signed key is NOT stored in this file: it used
# to be a literal, which is fine on one machine but leaks the moment the project
# is published. Set VSS_PFX_PASSWORD, or pass --password on the command line.
PFX_PASSWORD = os.environ.get("VSS_PFX_PASSWORD") or "change-me"

SUBJECT = "CN=Video Splitter Studio (self-signed), O=Video Splitter Studio"
SUBJECT_MATCH = "Video Splitter Studio"
TIMESTAMP_URL = "http://timestamp.digicert.com"
NUGET_PACKAGE = "microsoft.windows.sdk.buildtools"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) VideoSplitterStudio/1.0"}

# Pinned deliberately: a BuildTools layout change must not break signing, and
# this is the build verified here. `tools --any-version` takes the newest
# stable release instead.
KNOWN_GOOD_BUILDTOOLS = "10.0.28000.2705"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(args, **kwargs)


# --------------------------------------------------------------------------- #
# signtool discovery / bootstrap
# --------------------------------------------------------------------------- #

def _signtool_candidates() -> list[str]:
    """
    Where signtool.exe may live, cheapest lookup first.

    No recursive scan of the SDK trees: they hold tens of thousands of files and
    a full walk takes minutes. The SDK always lays the tools out in a
    version\\arch folder, so two levels of listing is enough.
    """
    found: list[str] = []
    which = shutil.which("signtool")
    if which:
        found.append(which)
    bundled = os.path.join(TOOLS, "bin", "signtool.exe")
    if os.path.isfile(bundled):
        found.append(bundled)
    kits = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Windows Kits", "10", "bin"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Windows Kits", "10", "bin"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Windows Kits", "8.1", "bin"),
    ]
    for root in kits:
        if not root or not os.path.isdir(root):
            continue
        try:
            versions = sorted(os.listdir(root), reverse=True)
        except OSError:
            continue
        for version in versions[:6]:
            for arch in ("x64", "x86", "arm64"):
                candidate = os.path.join(root, version, arch, "signtool.exe")
                if os.path.isfile(candidate):
                    found.append(candidate)
    return found


def find_signtool() -> str | None:
    candidates = _signtool_candidates()
    return candidates[0] if candidates else None


def _fetch(url: str, archive: str, label: str, tries: int = 12) -> str:
    """
    Download `url` to `archive`, resuming with HTTP ranges until complete.

    Connections drop mid-body fairly often (JSON metadata as well as packages),
    and a truncated response must not restart from zero or the download never
    finishes. Raises only after every attempt has been spent.
    """
    expected: int | None = None
    for attempt in range(tries):
        have = os.path.getsize(archive) if os.path.isfile(archive) else 0
        headers = dict(UA)
        mode = "wb"
        if have:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                if resp.status == 206:
                    total += have
                elif have and resp.status == 200:
                    have, mode = 0, "wb"          # server ignored the range
                expected = total if total else None
                with open(archive, mode) as fh:
                    while True:
                        # a cut-off body raises IncompleteRead here; it is caught
                        # below and the next attempt resumes from the new size
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
            got = os.path.getsize(archive)
            if expected is None or got >= expected:
                return archive
        except Exception:                              # noqa: BLE001
            pass
        if attempt < tries - 1:
            time.sleep(0.4 + 0.4 * attempt)
    got = os.path.getsize(archive) if os.path.isfile(archive) else 0
    raise RuntimeError(f"{label} 下载不完整：{got}"
                       + (f"/{expected}" if expected else "")
                       + " 字节（已重试多次）")


def _http_text(url: str, label: str = "元数据") -> str:
    """Fetch a small JSON document, surviving truncated responses."""
    scratch = os.path.join(TMP_DIR, "meta.json")
    os.makedirs(TMP_DIR, exist_ok=True)
    try:
        os.remove(scratch)
    except OSError:
        pass
    _fetch(url, scratch, label, tries=15)
    with open(scratch, "rb") as fh:
        data = fh.read()
    try:
        os.remove(scratch)
    except OSError:
        pass
    return data.decode("utf-8", "replace")


def fetch_signtool(force: bool = False, any_version: bool = False) -> str:
    """Install signtool.exe from the Windows SDK BuildTools NuGet package."""
    existing = find_signtool()
    if existing and not force:
        say(f"signtool 已存在：{existing}")
        return existing

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(os.path.join(TOOLS, "bin"), exist_ok=True)
    version = KNOWN_GOOD_BUILDTOOLS
    if any_version:
        meta_url = f"https://api.nuget.org/v3-flatcontainer/{NUGET_PACKAGE}/index.json"
        say(f"查询 NuGet：{NUGET_PACKAGE} …")
        versions = json.loads(_http_text(meta_url, "NuGet 元数据"))["versions"]
        stable = [v for v in versions if "-" not in v]
        version = (stable or versions)[-1]
    url = (f"https://api.nuget.org/v3-flatcontainer/{NUGET_PACKAGE}/{version}/"
           f"{NUGET_PACKAGE}.{version}.nupkg")
    archive = os.path.join(TMP_DIR, f"{NUGET_PACKAGE}.{version}.nupkg")
    say(f"下载 Windows SDK BuildTools {version}（约 21 MB，首次需要）…")
    _fetch(url, archive, "Windows SDK BuildTools")

    wanted = 0
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            low = member.lower().replace("\\", "/")
            base = os.path.basename(low)
            if base not in ("signtool.exe", "mssign32.dll", "wintrust.dll",
                            "appxsigning.dll"):
                continue
            if "/x64/" not in low:
                continue                     # keep only the 64-bit tool set
            target = os.path.join(TOOLS, "bin", base)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            wanted += 1
    if not wanted:
        raise RuntimeError("压缩包里没找到 x64 的 signtool.exe")
    try:
        os.remove(archive)
    except OSError:
        pass
    found = find_signtool()
    if not found:
        raise RuntimeError("解包完成但仍找不到 signtool.exe")
    say(f"signtool 就绪：{found}")
    return found


# --------------------------------------------------------------------------- #
# certificate
# --------------------------------------------------------------------------- #

def certificate_thumbprint(subject: str = SUBJECT_MATCH,
                           store: str = "CurrentUser") -> str | None:
    """Thumbprint of an installed code-signing certificate, if there is one."""
    ps = ("Get-ChildItem Cert:\\%s\\My | "
          "Where-Object { $_.Subject -like '*%s*' -and $_.HasPrivateKey } | "
          "Select-Object -First 1 | ForEach-Object { $_.Thumbprint }"
          % (store, subject))
    result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    out = (result.stdout or "").strip()
    return out or None


def new_cert(subject: str = SUBJECT, password: str = PFX_PASSWORD,
             years: int = 5, store: str = "CurrentUser") -> tuple[str, str]:
    """
    Create a self-signed code-signing certificate in the Windows store.

    The private key stays in a Windows key container - exactly what signtool
    needs - and is also exported to `signing/*.pfx` for reuse elsewhere; the
    public half goes to `signing/*.cer` so it can be trusted on other machines.
    """
    os.makedirs(SIGNING, exist_ok=True)
    ps = (
        "$ErrorActionPreference = 'Stop'; "
        "$c = New-SelfSignedCertificate -Type CodeSigningCert -Subject '%s' "
        "-CertStoreLocation 'Cert:\\%s\\My' -NotAfter (Get-Date).AddYears(%d) "
        "-KeyAlgorithm RSA -KeyLength 3072 -KeyExportPolicy Exportable "
        "-TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3'); "
        "$pw = ConvertTo-SecureString -String '%s' -Force -AsPlainText; "
        "Export-PfxCertificate -Cert $c -FilePath '%s' -Password $pw | Out-Null; "
        "Export-Certificate -Cert $c -FilePath '%s' -Type CERT | Out-Null; "
        "Write-Output \"$($c.Thumbprint)|$($c.Subject)|$($c.NotAfter)\""
        % (subject, store, max(1, years), password, PFX, CER))
    say(f"生成自签名代码签名证书（{years} 年有效期，私钥保存在 Windows 密钥容器）…")
    result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    text = (result.stdout or "").strip()
    if result.returncode != 0 or "|" not in text:
        detail = (text + (result.stderr or "")).strip()
        if any(token in detail for token in ("NTE_PERM", "0x80090010",
                                             "Access denied", "拒绝访问")):
            raise RuntimeError(
                "无法创建 Windows 密钥容器（NTE_PERM）。\n"
                "当前账户或沙盒不允许写入证书密钥库，请换一个普通命令行窗口"
                "（非沙盒、非受限账户）重新运行本命令。\n\n" + detail)
        raise RuntimeError("创建证书失败：\n" + detail)
    thumbprint, subj, not_after = text.splitlines()[-1].split("|", 2)
    say(f"指纹：{thumbprint.strip()}")
    say(f"主题：{subj}")
    say(f"到期：{not_after}")
    say(f"私钥副本：{PFX}")
    say(f"公钥：{CER}")
    say("提示：.pfx 含私钥，不要外发；.cer 可随程序分发，用于在对方电脑上信任发布者。")
    return PFX, CER


def store_certificate(subject: str = SUBJECT_MATCH,
                      store: str = "CurrentUser") -> str:
    """Thumbprint of the signing certificate, creating one when needed."""
    thumbprint = certificate_thumbprint(subject, store)
    if thumbprint:
        detail = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                      f"(Get-Item 'Cert:\\{store}\\My\\{thumbprint}').Subject"])
        say(f"使用证书存储中的证书：{(detail.stdout or '').strip() or thumbprint}")
        return thumbprint
    say("证书存储里还没有代码签名证书，正在创建 …")
    new_cert(store=store)
    thumbprint = certificate_thumbprint(subject, store)
    if not thumbprint:
        raise RuntimeError("证书创建后仍找不到，请检查证书存储权限")
    return thumbprint


# --------------------------------------------------------------------------- #
# signing
# --------------------------------------------------------------------------- #

def _signtool_sign(signtool: str, target: str, args: list[str],
                   timestamp: bool) -> subprocess.CompletedProcess:
    cmd = [signtool, "sign", "/fd", "SHA256", "/v"]
    if timestamp:
        cmd += ["/tr", TIMESTAMP_URL, "/td", "SHA256"]
    cmd += args + [target]
    return run(cmd)


def sign(target: str = DIST_EXE, pfx: str | None = None, password: str = PFX_PASSWORD,
         subject: str | None = None, timestamp: bool = True,
         thumbprint: str | None = None) -> None:
    """
    Sign `target` with signtool.

    Preferred input is a certificate already installed in the store (its key then
    already lives in a key container). `--cert file.pfx` is also accepted, and is
    what you use with a certificate bought from a public CA.
    """
    if not os.path.isfile(target):
        raise RuntimeError(f"找不到要签名的文件：{target}")
    tool = find_signtool() or fetch_signtool()

    if pfx:
        if not os.path.isfile(pfx):
            raise RuntimeError(f"找不到证书文件：{pfx}")
        args = ["/f", pfx, "/p", password]
        who = os.path.basename(pfx)
    else:
        chosen = thumbprint or store_certificate(subject or SUBJECT_MATCH)
        clean = "".join(ch for ch in chosen if ch.isalnum())
        if not clean:
            raise RuntimeError("证书指纹无效")
        args = ["/sha1", clean]
        who = f"证书存储 {clean}"

    say(f"签名（signtool / {who}）：{target}")
    result = _signtool_sign(tool, target, args, timestamp)
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0 and timestamp:
        # an unreachable timestamp server must not lose the signature
        say("带时间戳签名失败，改为不带时间戳重试"
            "（签名仍然有效，只是没有第三方时间证明）…")
        for line in text.splitlines()[-4:]:
            say("  " + line.strip())
        result = _signtool_sign(tool, target, args, False)
        text = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        for line in text.splitlines()[-6:]:
            say("  " + line.strip())
        if "ImportCertObject" in text or "0x80090010" in text:
            say("")
            say("提示：signtool 无法把 .pfx 里的私钥导入密钥容器。")
            say("      先运行 new-cert 把证书装进证书存储，再不带 --cert 重新签名。")
        raise RuntimeError("签名失败")
    for line in text.splitlines():
        if "Successfully signed" in line:
            say(line.strip())
    verify(target)


def verify(target: str = DIST_EXE) -> int:
    """Print the Authenticode status. Returns a process-style exit code."""
    if not os.path.isfile(target):
        say(f"找不到文件：{target}")
        return 2
    ps = (f"$s = Get-AuthenticodeSignature -FilePath '{target}';"
          "$s | Format-List Status, StatusMessage,"
          " @{n='Signer';e={$_.SignerCertificate.Subject}},"
          " @{n='Thumbprint';e={$_.SignerCertificate.Thumbprint}},"
          " @{n='NotAfter';e={$_.SignerCertificate.NotAfter}},"
          " @{n='Timestamped';e={$_.TimeStamperCertificate -ne $null}}")
    result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    say((result.stdout or "").strip())

    tool = find_signtool()
    if not tool:
        status = run(["powershell", "-NoProfile", "-Command",
                      f"(Get-AuthenticodeSignature -FilePath '{target}').Status"])
        text = (status.stdout or "").strip()
        say(f"签名状态：{text}")
        return 0 if text.lower() == "valid" else 1
    check = run([tool, "verify", "/pa", "/v", target])
    text = (check.stdout or "") + (check.stderr or "")
    ok = check.returncode == 0
    say(f"signtool verify: {'OK' if ok else '未通过'}")
    for line in text.splitlines():
        low = line.lower()
        if any(word in low for word in ("issued to", "error", "not signed",
                                        "successfully verified", "hash")):
            say("  " + line.strip())
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Internet mark / trust
# --------------------------------------------------------------------------- #

def unblock(path: str) -> int:
    """Remove the Zone.Identifier stream ("downloaded from the Internet")."""
    targets: list[str] = []
    if os.path.isdir(path):
        for base, _dirs, files in os.walk(path):
            targets += [os.path.join(base, name) for name in files]
    else:
        targets = [path]
    cleared = 0
    for item in targets:
        try:
            result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          f"Unblock-File -LiteralPath '{item}' -ErrorAction Stop"])
            if result.returncode == 0:
                cleared += 1
        except Exception:                              # noqa: BLE001
            continue
    say(f"已清除「来自 Internet」标记：{cleared}/{len(targets)} 个文件")
    return 0


def trust(cer: str = CER, machine: bool = False) -> int:
    """
    Install the certificate so Windows stops calling the publisher unknown.

    A signature only counts as "verified" when its certificate chains to a
    trusted root. For a self-signed certificate that means installing it into
    `Root` (so the chain validates) and `TrustedPublisher` on every PC that
    should run the program without a prompt.
    """
    if not os.path.isfile(cer):
        say(f"找不到证书文件：{cer}（先运行 new-cert）")
        return 1
    scope = "LocalMachine" if machine else "CurrentUser"
    prefix = [] if machine else ["-user"]
    say(f"把 {os.path.basename(cer)} 装进 {scope} 的 Root 与 TrustedPublisher …")
    ok = True
    for store in ("Root", "TrustedPublisher"):
        result = run(["certutil", *prefix, "-addstore", "-f", store, cer])
        good = result.returncode == 0
        say(f"  {store:16s} {'OK' if good else '失败'}")
        if not good:
            ok = False
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            for line in output.splitlines()[-2:]:
                say("    " + line.strip())
    say("")
    if ok:
        say("完成。这台电脑上再运行程序就不会提示「无法验证发布者」了。")
        say("（如果文件还带「来自 Internet」标记，再运行一次 unblock。）")
    else:
        say("安装失败：用管理员身份重试，或手动导入：")
        say(f"  1. 双击 {cer}")
        say("  2. 安装证书 → 本地计算机 → 将所有证书都放入下列存储")
        say("  3. 「受信任的根证书颁发机构」装一次，"
            "「受信任的发布者」再装一次")
    say("")
    say("要让*别人*的电脑也不提示，只有两条路：")
    say("  a) 把你的 .cer 发给对方，让他在自己电脑上执行 "
        "`python sign_exe.py trust`")
    say("  b) 买公开发证机构（CA）的代码签名证书；EV 证书可直接获得 "
        "SmartScreen 信誉，OV 证书需要累积一段时间的下载量。")
    say("")
    say("安全提醒：自签名证书一旦装进「受信任的根」，持有对应私钥的人就能伪造"
        "被这台电脑信任的签名。只在自己控制的机器上这样做，不要把 .pfx 外发。")
    return 0 if ok else 1


def doctor() -> int:
    say("== 签名环境自检 ==")
    say(f"待签名文件 : {DIST_EXE}  ({'存在' if os.path.isfile(DIST_EXE) else '不存在'})")
    tool = find_signtool()
    say(f"signtool   : {tool or '未找到（运行 python sign_exe.py tools 自动获取）'}")
    thumbprint = certificate_thumbprint()
    say(f"证书(存储) : {thumbprint or '未安装（运行 new-cert 创建）'}")
    say(f"证书私钥   : {PFX}  ({'存在' if os.path.isfile(PFX) else '不存在'})")
    say(f"证书公钥   : {CER}  ({'存在' if os.path.isfile(CER) else '不存在'})")
    if os.path.isfile(DIST_EXE):
        streams = run(["powershell", "-NoProfile", "-Command",
                       f"(Get-Item -LiteralPath '{DIST_EXE}' -Stream * "
                       "-ErrorAction SilentlyContinue).Stream -join ','"])
        marks = [s for s in (streams.stdout or "").split(",") if "Zone" in s]
        say(f"Internet标记: {marks or '无（好）'}")
        verify(DIST_EXE)
    say("")
    say("推荐顺序：python sign_exe.py tools && python sign_exe.py sign "
        "&& python sign_exe.py trust")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Video Splitter Studio 代码签名工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="检查签名环境")

    p = sub.add_parser("tools", help="下载 signtool.exe（如缺失）")
    p.add_argument("--force", action="store_true")
    p.add_argument("--any-version", action="store_true",
                   help="不锁定已验证的版本，改用 NuGet 上最新的稳定版")

    p = sub.add_parser("new-cert", help="生成并安装自签名代码签名证书")
    p.add_argument("--subject", default=SUBJECT)
    p.add_argument("--password", default=PFX_PASSWORD)
    p.add_argument("--years", type=int, default=5)

    p = sub.add_parser("sign", help="给 exe 签名")
    p.add_argument("target", nargs="?", default=DIST_EXE)
    p.add_argument("--cert", dest="pfx", default=None,
                   help="用 .pfx 签名（买来的证书走这条路）")
    p.add_argument("--password", default=PFX_PASSWORD)
    p.add_argument("--subject", default=None,
                   help="按主题在证书存储里挑证书（默认 Video Splitter Studio）")
    p.add_argument("--thumbprint", default=None, help="直接指定证书指纹")
    p.add_argument("--no-timestamp", action="store_true",
                   help="不打时间戳（离线时用；签名有效期会止于证书到期日）")

    p = sub.add_parser("verify", help="查看签名状态")
    p.add_argument("target", nargs="?", default=DIST_EXE)

    p = sub.add_parser("unblock", help="清除「来自 Internet」标记")
    p.add_argument("path", nargs="?",
                   default=os.path.join(HERE, "dist", "VideoSplitterStudio"))

    p = sub.add_parser("trust", help="把证书装进受信任存储（本机免提示）")
    p.add_argument("--cer", default=CER)
    p.add_argument("--machine", action="store_true",
                   help="装到 LocalMachine（需要管理员，对本机所有用户生效）")

    args = parser.parse_args(argv)
    cmd = args.cmd or "doctor"
    try:
        if cmd == "doctor":
            return doctor()
        if cmd == "tools":
            fetch_signtool(force=args.force, any_version=args.any_version)
            return 0
        if cmd == "new-cert":
            new_cert(args.subject, args.password, args.years)
            return 0
        if cmd == "sign":
            sign(args.target, args.pfx, args.password, args.subject,
                 not args.no_timestamp, args.thumbprint)
            return 0
        if cmd == "verify":
            return verify(args.target)
        if cmd == "unblock":
            return unblock(args.path)
        if cmd == "trust":
            return trust(args.cer, args.machine)
    except Exception as exc:                           # noqa: BLE001
        say(f"错误：{exc}")
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
