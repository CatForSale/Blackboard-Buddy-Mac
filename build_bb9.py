"""Stage sources into bb9, run PyInstaller there, copy the result over the app folder (all from Python)."""
import shutil
import subprocess
import sys
from pathlib import Path

src = Path(r"C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy")
dst = Path(r"C:\Users\minnk\AppData\Local\bb9")
for n in ("main.py", "app.py", "ai.py", "blackboard.py", "vault.py", "online.py",
          "relax.py", "spotify.py", "icon.ico", "icon.png"):
    (dst / n).unlink(missing_ok=True)  # a hidden file can't be opened for writing, but it can be deleted
    shutil.copyfile(src / n, dst / n)
r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "ERROR", "Blackboard Buddy.spec"],
                   cwd=dst, capture_output=True, text=True)
print("pyinstaller exit", r.returncode)
if r.returncode:
    print(r.stdout[-1500:], r.stderr[-1500:])
    sys.exit(1)
built = dst / "dist" / "Blackboard Buddy"
exe = built / "Blackboard Buddy.exe"
print("built exe", exe.stat().st_size, exe.stat().st_mtime)
n = 0
for f in built.rglob("*"):
    if f.is_file():
        t = src / f.relative_to(built)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        n += 1
print("copied", n, "files; app exe now", (src / "Blackboard Buddy.exe").stat().st_mtime)

# and the source goes up to GitHub, but only when asked for: the upload commits everything in
# the folder, and a build is no moment to publish whatever else happens to be lying there.
if "--upload" in sys.argv:
    try:
        import upload
        upload.main()
    except Exception as e:
        print("upload skipped:", e)
else:
    print("not uploaded (pass --upload to send the source to GitHub)")

