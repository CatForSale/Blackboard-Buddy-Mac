"""Puts out a new version: everyone's app sees "Version X is ready" within 12 hours, and one click updates it.

    python publish_update.py 1.2.0 "Note map colours, sharing by code, uninstall"

What it does:
  1. sets VERSION in main.py to the new number
  2. builds the exe with PyInstaller (in %LOCALAPPDATA%\\bb9, like the normal build)
  3. zips it like the share zip, and checks nothing personal is inside
  4. makes a GitHub release on UPDATE_REPO (main.py) with the zip attached

Before the first time:
  - make a public GitHub repo for releases only (no source needed in it), e.g. yourname/blackboard-buddy-releases
  - put its "owner/name" in UPDATE_REPO in main.py, and build + share that copy once: only copies made
    after UPDATE_REPO is set know where to look for updates
  - make a fine-grained token on github.com > Settings > Developer settings, with "Contents: read and write"
    on that repo only, and set it for this window:  set GITHUB_TOKEN=...   (it is never saved anywhere)

--dry-run builds and checks the zip but uploads nothing. --no-build reuses the last build.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP = Path(__file__).parent
BUILD = Path(os.environ["LOCALAPPDATA"]) / "bb9"
DIST = BUILD / "dist" / "Blackboard Buddy"
SOURCES = ("main.py", "app.py", "ai.py", "blackboard.py", "vault.py", "online.py", "icon.ico", "icon.png")


def ver(t):
    return tuple(int(x) for x in re.findall(r"\d+", t)[:3])


def set_version(new):
    f = APP / "main.py"
    hidden = _hidden(f)
    if hidden:
        _hide(f, False)
    try:
        text = f.read_text(encoding="utf-8")
        text, n = re.subn(r'^VERSION = "[^"]*"', f'VERSION = "{new}"', text, count=1, flags=re.M)
        assert n == 1, "VERSION line not found in main.py"
        f.write_text(text, encoding="utf-8")
    finally:
        if hidden:
            _hide(f, True)


def _hidden(f):
    import ctypes
    return bool(ctypes.windll.kernel32.GetFileAttributesW(str(f)) & 2)


def _hide(f, on):
    import ctypes
    a = ctypes.windll.kernel32.GetFileAttributesW(str(f))
    ctypes.windll.kernel32.SetFileAttributesW(str(f), (a | 2) if on else (a & ~2))


def build():
    for n in SOURCES:
        (BUILD / n).unlink(missing_ok=True)
        shutil.copyfile(APP / n, BUILD / n)
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "ERROR", "Blackboard Buddy.spec"],
                       cwd=BUILD, capture_output=True, text=True)
    if r.returncode:
        sys.exit("Build failed:\n" + r.stdout[-1500:] + r.stderr[-1500:])


def make_zip(version):
    out = APP.parent / f"Blackboard Buddy {version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(DIST.rglob("*")):
            if f.is_file():
                z.write(f, "Blackboard Buddy/" + f.relative_to(DIST).as_posix())
        z.write(APP / "READ ME FIRST.txt", "Blackboard Buddy/READ ME FIRST.txt")
    # nothing personal may go out: no keys, data, notes or your name
    bad = re.compile(r"(^|/)(\.env|data|output|Notebook|materials|browser-profile|webview)(/|$)|\.(py|md|json|log)$", re.I)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        flagged = [n for n in names if bad.search(n) and not n.startswith("Blackboard Buddy/_internal/")]
        leaks = [n for n in names if any(s in z.read(n) for s in (b"sk-ant-", b"ANTHROPIC_API_KEY=sk"))]
    if "Blackboard Buddy/Blackboard Buddy.exe" not in names or flagged or leaks:
        out.unlink()
        sys.exit(f"Zip refused. Missing exe, or personal files: {flagged + leaks}")
    return out


def api(method, url, token, data=None, ctype="application/json"):
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "BlackboardBuddy-publish",
        "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub said {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry, no_build = "--dry-run" in sys.argv, "--no-build" in sys.argv
    if not args:
        sys.exit(__doc__)
    new, notes = args[0].lstrip("vV"), (args[1] if len(args) > 1 else "")
    text = (APP / "main.py").read_text(encoding="utf-8")
    old = re.search(r'^VERSION = "([^"]*)"', text, re.M).group(1)
    repo = re.search(r'^UPDATE_REPO = "([^"]*)"', text, re.M).group(1)
    if ver(new) <= ver(old):
        sys.exit(f"{new} must be higher than the current version {old}.")
    if not repo and not dry:
        sys.exit("Set UPDATE_REPO in main.py first (see the top of this file).")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not dry:
        sys.exit("Set GITHUB_TOKEN for this window first (see the top of this file).")
    set_version(new)
    print(f"Version {old} -> {new}")
    if not no_build:
        print("Building…")
        build()
    z = make_zip(new)
    print(f"Zip ready: {z} ({z.stat().st_size / 1e6:.1f} MB)")
    if dry:
        set_version(old)
        print("Dry run: nothing uploaded, version put back to", old)
        return
    rel = api("POST", f"https://api.github.com/repos/{repo}/releases", token,
              json.dumps({"tag_name": f"v{new}", "name": f"Blackboard Buddy {new}", "body": notes or f"Version {new}"}).encode())
    up = rel["upload_url"].split("{")[0] + "?name=" + urllib.request.quote(f"Blackboard-Buddy-{new}.zip")
    print("Uploading…")
    api("POST", up, token, z.read_bytes(), "application/zip")
    print(f"Published {rel['html_url']}. Apps with update checks see it within 12 hours, or straight away with "
          "Settings > Check for updates.")


if __name__ == "__main__":
    main()
