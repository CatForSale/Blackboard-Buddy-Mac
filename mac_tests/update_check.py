"""The update feature on this machine, against the real latest release: an old copy finds it, picks the
download for this system, and Update now does what it should here (on a Mac: opens the release page)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main
import app

bad = 0


def check(name, ok):
    global bad
    print(("PASS " if ok else "FAIL ") + name)
    bad += not ok


main.VERSION = "1.2.0"                       # pretend to be an old copy
info = app.check_update()
print("found:", info)
check("an old copy sees the new version", bool(info) and info["version"] != "1.2.0")
name = (info or {}).get("url", "").rsplit("/", 1)[-1].lower()
check(f"it picks the download for this system ({name})", ("mac" in name) == (sys.platform == "darwin") and name.endswith(".zip"))
check("the update message is a sensible first line", bool(info) and 10 < len(info["notes"]) <= 160)

# Only on a Mac: there Update now just opens a page. On Windows it really downloads and swaps files.
if sys.platform == "darwin":
    opened = []
    main.open_path = lambda p: opened.append(str(p))
    sys.frozen = True                        # Update now only works in the built app
    msg = app.Api().install_update()
    print("Update now said:", msg, "| opened:", opened)
    check("Update now opens the release page", opened == [f"https://github.com/{main.UPDATE_REPO}/releases/latest"])
    check("and says what to do next", "Applications" in msg)

main.VERSION = "99.0.0"                      # a copy newer than anything published
app.STATE["update"] = None
check("an up-to-date copy sees no update", app.check_update() is None)
print("ALL OK" if not bad else f"FAILED {bad}")
sys.exit(1 if bad else 0)
