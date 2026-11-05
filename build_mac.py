"""Builds the Mac app. Run it on a Mac (or let .github/workflows/mac.yml run it on GitHub's Macs):

    python3 -m pip install -r requirements.txt
    python3 build_mac.py

It makes dist/Blackboard Buddy.app and zips it as dist/Blackboard Buddy - mac (<chip>).zip, ready to
upload to a release. A Mac takes the release zip with "mac" in its name; Windows takes the other one.
"""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
if sys.platform != "darwin":
    sys.exit("This builds the Mac app, so run it on a Mac.")

icon = HERE / "icon.icns"
if not icon.exists():                       # made from icon.png the first time
    from PIL import Image
    Image.open(HERE / "icon.png").convert("RGBA").save(icon)

subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                "--name", "Blackboard Buddy", "--icon", str(icon),
                "--osx-bundle-identifier", "app.blackboardbuddy",
                "--hidden-import", "main", "--hidden-import", "ai", "--hidden-import", "blackboard",
                "--hidden-import", "vault", "--hidden-import", "online", "--hidden-import", "certifi", "--collect-data", "certifi",
                "app.py"], cwd=HERE, check=True)

app = HERE / "dist" / "Blackboard Buddy.app"
chip = "Apple silicon" if platform.machine() == "arm64" else "Intel"
out = HERE / "dist" / f"Blackboard Buddy - mac ({chip}).zip"
out.unlink(missing_ok=True)
shutil.copy2(HERE / "READ ME FIRST (Mac).txt", HERE / "dist" / "READ ME FIRST (Mac).txt")
# ditto keeps the app's links and permissions, which a plain zip would break
subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(out)], check=True)
subprocess.run(["zip", "-j", str(out), str(HERE / "dist" / "READ ME FIRST (Mac).txt")], check=True)
print("made", out, round(out.stat().st_size / 1e6, 1), "MB")
