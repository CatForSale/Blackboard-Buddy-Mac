"""Blackboard Buddy Setup: the one .exe people download.

publish_update.py builds this into "Blackboard Buddy Setup <version>.exe" with the release zip inside.
Running it puts the app in %LOCALAPPDATA%\\Programs\\Blackboard Buddy, adds Start menu and Desktop
shortcuts, and opens the app. Running a newer Setup over an older install replaces the program files
only: the key, data and notes beside the exe are not in the zip, so they stay.
A fresh install looks for an older copy (a folder somewhere else with the exe and your info beside
it) and offers to copy its key, data and Notebook over. Nothing in the old folder is changed.
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import font as tkfont

NAME = "Blackboard Buddy"
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
ZIP = HERE / "app.zip"
# BB_SETUP_TEST=<folder> installs into that folder and puts both shortcuts there too, for testing
TEST = os.environ.get("BB_SETUP_TEST")
TARGET = Path(TEST) / NAME if TEST else Path(os.environ["LOCALAPPDATA"]) / "Programs" / NAME
EXE = TARGET / f"{NAME}.exe"
START_MENU = (Path(TEST) / "Start Menu" if TEST else
              Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs") / f"{NAME}.lnk"


def desktop():
    if TEST:
        return Path(TEST) / "Desktop"
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)  # the real Desktop, even when OneDrive moved it
    return Path(buf.value or Path.home() / "Desktop")


def shortcut(lnk, target):
    lnk.parent.mkdir(parents=True, exist_ok=True)
    q = lambda p: "'" + str(p).replace("'", "''") + "'"
    ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)}); $s.TargetPath = {q(target)}; "
          f"$s.WorkingDirectory = {q(target.parent)}; $s.IconLocation = {q(target)}; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=0x08000000, check=True, capture_output=True)


def app_is_open():
    """The old exe can't be replaced while it runs."""
    if not EXE.exists():
        return False
    try:
        with open(EXE, "r+b"):
            return False
    except PermissionError:
        return True


def install(step):
    with zipfile.ZipFile(ZIP) as z:
        files = [i for i in z.infolist() if not i.is_dir() and i.filename.startswith(NAME + "/")]
        for n, info in enumerate(files, 1):
            out = TARGET / info.filename[len(NAME) + 1:]
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(out, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            if n % 40 == 0 or n == len(files):
                step(n / len(files))
    shortcut(START_MENU, EXE)
    shortcut(desktop() / f"{NAME}.lnk", EXE)


ASKED = TARGET / ".brought-over"               # set once you've answered, so a later Setup doesn't ask again
MINE = (".env", "data", "Notebook")            # what's yours beside the exe; output is rebuilt
SKIP_DIRS = {"_internal", "node_modules", ".git", "venv", "__pycache__", "AppData", "site-packages"}


def _has_info(d):
    nb = d / "Notebook"
    return ((d / ".env").is_file() or (d / "data" / "settings.json").is_file()
            or (nb.is_dir() and any(nb.iterdir())))


def _last_used(d):
    newest = 0
    for n in MINE:
        f = d / n
        try:
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
            elif f.is_dir():
                newest = max([newest] + [x.stat().st_mtime for x in f.rglob("*") if x.is_file()])
        except OSError:
            pass
    return newest


def find_old_copy():
    """(folder, last used) of the most recently used other copy that has your info, or None.
    BB_SETUP_SEARCH=<dir>;<dir> searches only those (tests); test installs search nothing else."""
    local = Path(os.environ["LOCALAPPDATA"])
    if os.environ.get("BB_SETUP_SEARCH") is not None:
        roots = [Path(r) for r in os.environ["BB_SETUP_SEARCH"].split(os.pathsep) if r]
        extra = []
    elif TEST:
        roots, extra = [], []
    else:
        h = Path.home()
        roots = [desktop(), h / "Desktop", h / "Downloads", h / "Documents", h / "OneDrive" / "Desktop",
                 h / "OneDrive" / "Documents", local / "Programs"]
        extra = [local / "BlackboardBuddy"]       # where a copy keeps your info when its own folder is read-only
    found, seen = [], set()

    def walk(d, depth):
        try:
            key = os.path.normcase(str(d.resolve()))
            if key in seen:
                return
            seen.add(key)
            if (d / f"{NAME}.exe").is_file() and key != os.path.normcase(str(TARGET.resolve())) and _has_info(d):
                found.append(d)
            if depth:
                for e in os.scandir(d):
                    if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS and not e.name.startswith("."):
                        walk(Path(e.path), depth - 1)
        except OSError:
            pass

    for r in roots:
        walk(r, 3)
    found += [d for d in extra if _has_info(d)]
    if not found:
        return None
    best = max(found, key=_last_used)
    return best, _last_used(best)


def bring_over(src):
    """Copies your key, data and Notebook from an older copy. Whatever this copy had is moved to Backups first."""
    keep = ((Path(TEST) if TEST else Path(os.environ["LOCALAPPDATA"]) / "BlackboardBuddy") / "Backups"
            / f"Before bringing info over {time.strftime('%Y-%m-%d %H%M%S')}")
    for n in MINE:
        a, b = src / n, TARGET / n
        if not a.exists():
            continue
        if b.exists():
            keep.mkdir(parents=True, exist_ok=True)
            shutil.move(str(b), str(keep / n))
        if a.is_dir():
            shutil.copytree(a, b)
        else:
            shutil.copy2(a, b)


# 5x7 dot letters for the title, like the app's own dot headings
GLYPHS = {
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01111 10000 10000 10000 10000 10000 01111", "D": "11110 10001 10001 10001 10001 10001 11110",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "O": "01110 10001 10001 10001 10001 10001 01110", "R": "11110 10001 10001 11110 10100 10010 10001",
    "U": "10001 10001 10001 10001 10001 10001 01110", "Y": "10001 10001 01010 00100 00100 00100 00100",
}
INK, BG, GRID, DIM, MUTE, RED = "#F2F2F2", "#0A0A0A", "#171717", "#262626", "#8A8A8A", "#D71921"
OPEN_MSG = "Blackboard Buddy is open.\nClose it, then click Try again."


def _version():
    m = re.search(r"Setup ([\d.]+)", Path(sys.executable).stem)
    return m.group(1) if m else ""


def main():
    if "--quiet" in sys.argv:  # no window, no opening the app afterwards
        if app_is_open():
            sys.exit("Blackboard Buddy is open. Close it first.")
        install(lambda v: None)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title(f"{NAME} Setup")
    S = root.winfo_fpixels("1i") / 96          # everything below is in 96-dpi pixels
    W, H = 480, 320
    fams = set(tkfont.families())
    text = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), "TkDefaultFont")
    mono = next((f for f in ("Cascadia Mono", "Consolas") if f in fams), "TkFixedFont")
    px = lambda v: int(round(v * S))
    root.overrideredirect(True)
    root.configure(bg=BG)
    x, y = (root.winfo_screenwidth() - px(W)) // 2, (root.winfo_screenheight() - px(H)) // 2
    root.geometry(f"{px(W)}x{px(H)}+{x}+{y}")
    try:
        root.iconbitmap(default=str(HERE / "icon.ico"))
    except tk.TclError:
        pass
    c = tk.Canvas(root, width=px(W), height=px(H), bg=BG, highlightthickness=0, bd=0)
    c.pack()

    def dot(cx, cy, r, fill, tag=""):
        return c.create_oval(px(cx - r), px(cy - r), px(cx + r), px(cy + r), fill=fill, width=0, tags=tag)

    for gx in range(16, W, 16):                 # faint dot grid behind everything
        for gy in range(16, H, 16):
            dot(gx, gy, 0.7, GRID)
    c.create_rectangle(0, 0, px(W) - 1, px(H) - 1, outline="#1E1E1E")

    dot(30, 28, 3.2, RED, "pulse")
    ver = _version()
    c.create_text(px(42), px(28), text="SETUP" + (f"   v{ver}" if ver else ""), anchor="w", fill=MUTE, font=(mono, 8))
    close = c.create_oval(px(W - 38), px(18), px(W - 18), px(38), outline="#333333", width=px(1), tags="x")
    for a, b in ((-4, -4), (-4, 4)):
        c.create_line(px(W - 28 + a), px(28 + b), px(W - 28 - a), px(28 - b), fill=MUTE, width=px(1.4), tags="x")

    for row, word in enumerate(("BLACKBOARD", "BUDDY")):
        for k, ch in enumerate(word):
            for ry, bits in enumerate(GLYPHS[ch].split()):
                for rx, b in enumerate(bits):
                    if b == "1":
                        dot(30 + k * 30 + rx * 5, 66 + row * 46 + ry * 5, 1.45, INK)

    sub = c.create_text(px(28), px(176), anchor="nw", width=px(W - 56), fill=MUTE, font=(text, 10),
                        text="Installs for you only, no admin needed.\nIf it's already here, your settings and notes stay.")

    N = 34
    pitch = (W - 56 - 64) / (N - 1)
    pdots = [dot(28 + i * pitch, 238, 2.6, DIM) for i in range(N)]
    pct = c.create_text(px(W - 28), px(238), anchor="e", fill=MUTE, font=(mono, 9), text="000%")

    state = c.create_text(px(28), px(284), anchor="w", fill=MUTE, font=(mono, 8), text="READY")
    bx0, by0, bx1, by1 = W - 28 - 124, 266, W - 28, 302
    r = (by1 - by0) / 2
    pill = [c.create_oval(px(bx0), px(by0), px(bx0 + 2 * r), px(by1), width=0, tags="btn"),
            c.create_oval(px(bx1 - 2 * r), px(by0), px(bx1), px(by1), width=0, tags="btn"),
            c.create_rectangle(px(bx0 + r), px(by0), px(bx1 - r), px(by1), width=0, tags="btn")]
    label = c.create_text(px((bx0 + bx1) / 2), px((by0 + by1) / 2), text="Install", font=(text, 10, "bold"), tags="btn")
    skip = c.create_text(px(bx0 - 18), px((by0 + by1) / 2), anchor="e", text="Start fresh", fill=MUTE,
                         font=(mono, 8), state="hidden", tags="skip")
    ui = {"busy": False, "hover": False, "done": False, "blink": True, "offer": None}

    def paint_btn():
        if ui["busy"] or ui["done"]:
            fill, ink = "#1C1C1C", "#6A6A6A"
        else:
            fill, ink = ("#FFFFFF" if ui["hover"] else INK), "#0A0A0A"
        for it in pill:
            c.itemconfig(it, fill=fill)
        c.itemconfig(label, fill=ink)

    def progress(v):
        n = int(round(v * N))
        for i, d in enumerate(pdots):
            c.itemconfig(d, fill=RED if (i == n - 1 and v < 1) else INK if i < n else DIM)
        c.itemconfig(pct, text=f"{int(v * 100):03d}%", fill=INK if v else MUTE)

    def pulse():                                  # the red dot breathes until it's done
        if not ui["done"]:
            ui["blink"] = not ui["blink"]
            c.itemconfig("pulse", fill=RED if ui["blink"] else "#5A0A0E")
            root.after(700, pulse)

    def say(st, msg=None, color=MUTE):
        c.itemconfig(state, text=st, fill=color)
        if msg:
            c.itemconfig(sub, text=msg)

    def show_progress(on):
        for d in pdots + [pct]:
            c.itemconfig(d, state="normal" if on else "hidden")

    def finish(msg="All set. It's in the Start menu and on your Desktop.\nOpening Blackboard Buddy…"):
        if ui["offer"]:
            try:
                ASKED.write_text(str(ui["offer"]) if "here now" in msg else "started fresh", encoding="utf-8")
            except OSError:
                pass
        ui.update(busy=False, done=True, offer=None)
        c.itemconfig(skip, state="hidden")
        show_progress(True)
        progress(1.0)
        c.itemconfig("pulse", fill=INK)
        say("INSTALLED", msg, INK)
        c.itemconfig(label, text="Done")
        paint_btn()
        if not os.environ.get("BB_SETUP_NO_OPEN"):
            os.startfile(EXE)
        root.after(2400, root.destroy)

    def offer(old):
        folder, when = old
        try:
            where = str(folder.relative_to(Path.home()))
        except ValueError:
            where = str(folder)
        ui.update(busy=False, offer=folder)
        show_progress(False)
        c.itemconfig("pulse", fill=RED)
        say("FOUND YOUR INFO", f"You've used Blackboard Buddy before, in {where}"
            + (f" (last used {time.strftime('%d %b', time.localtime(when)).lstrip('0')})" if when else "")
            + ".\n\nBring your key, settings and notes over? The old folder stays as it is.", INK)
        c.itemconfig(label, text="Bring it over")
        c.itemconfig(skip, state="normal")
        paint_btn()

    def done(err, old=None):
        ui["busy"] = False
        if err:
            progress(0)
            c.itemconfig("pulse", fill=RED)
            say("NEEDS YOU", err, RED)
            c.itemconfig(label, text="Try again")
            paint_btn()
            return
        offer(old) if old else finish()

    def carry():
        try:
            bring_over(ui["offer"])
            root.after(0, finish, "Your key, settings and notes are here now.\nOpening Blackboard Buddy…")
        except Exception as e:  # a file held open, disk full…
            def failed():
                ui["busy"] = False
                say("NEEDS YOU", f"Couldn't copy everything: {e}\nTry again, or Start fresh.", RED)
                c.itemconfig(label, text="Try again")
                c.itemconfig(skip, state="normal")
                paint_btn()
            root.after(0, failed)

    def work():
        try:
            install(lambda v: root.after(0, progress, v))
            old = None if ASKED.exists() else find_old_copy()
            root.after(0, done, "", old)
        except PermissionError:
            root.after(0, done, OPEN_MSG)
        except Exception as e:  # disk full, antivirus holding a file…
            root.after(0, done, f"Couldn't install: {e}")

    def go(_=None):
        if ui["busy"] or ui["done"]:
            return
        if ui["offer"]:
            ui["busy"] = True
            paint_btn()
            c.itemconfig(skip, state="hidden")
            c.itemconfig(label, text="Copying")
            say("BRINGING IT OVER", "Copying your key, settings and notes…", INK)
            threading.Thread(target=carry, daemon=True).start()
            return
        if app_is_open():
            done(OPEN_MSG)
            return
        ui["busy"] = True
        paint_btn()
        c.itemconfig(label, text="Installing")
        say("INSTALLING", "Putting everything in place. This takes a few seconds.")
        threading.Thread(target=work, daemon=True).start()

    def hover(on):
        ui["hover"] = on
        c.config(cursor="hand2" if on and not (ui["busy"] or ui["done"]) else "")
        paint_btn()

    c.tag_bind("btn", "<Enter>", lambda e: hover(True))
    c.tag_bind("btn", "<Leave>", lambda e: hover(False))
    c.tag_bind("btn", "<Button-1>", go)
    c.tag_bind("skip", "<Enter>", lambda e: (c.itemconfig(skip, fill=INK), c.config(cursor="hand2")))
    c.tag_bind("skip", "<Leave>", lambda e: (c.itemconfig(skip, fill=MUTE), c.config(cursor="")))
    c.tag_bind("skip", "<Button-1>", lambda e: None if ui["busy"] else finish())
    c.tag_bind("x", "<Enter>", lambda e: c.itemconfig(close, fill=RED, outline=RED))
    c.tag_bind("x", "<Leave>", lambda e: c.itemconfig(close, fill="", outline="#333333"))
    c.tag_bind("x", "<Button-1>", lambda e: None if ui["busy"] else root.destroy())
    root.bind("<Return>", go)
    root.bind("<Escape>", lambda e: None if ui["busy"] else root.destroy())

    drag = {}                                     # no title bar, so drag it anywhere
    c.bind("<ButtonPress-1>", lambda e: drag.update(x=e.x, y=e.y))
    c.bind("<B1-Motion>", lambda e: root.geometry(f"+{root.winfo_x() + e.x - drag['x']}+{root.winfo_y() + e.y - drag['y']}"))

    def native():                                 # rounded corners on Windows 11, and a taskbar button
        try:
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            v = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(v), ctypes.sizeof(v))
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, (ex & ~0x80) | 0x40000)
            root.withdraw()
            root.after(10, root.deiconify)
        except (AttributeError, OSError):
            pass
        root.after(30, root.focus_force)

    paint_btn()
    root.after(0, native)
    pulse()
    shot = os.environ.get("BB_SETUP_SHOT")       # testing: save a picture of each step, then quit
    if shot:
        from PIL import ImageGrab

        def grab(name):
            root.lift()
            root.attributes("-topmost", True)
            root.update()
            ImageGrab.grab((root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx() + px(W),
                            root.winfo_rooty() + px(H))).save(Path(shot) / f"{name}.png")

        def steps():
            grab("1-ready")
            hover(True); grab("2-hover"); hover(False)
            ui["busy"] = True; paint_btn(); c.itemconfig(label, text="Installing")
            say("INSTALLING", "Putting everything in place. This takes a few seconds."); progress(0.47); grab("3-installing")
            os.environ["BB_SETUP_NO_OPEN"] = "1"; done(""); grab("4-done")
            ui["done"] = False; done(OPEN_MSG); grab("5-error")
            offer((Path.home() / "Desktop" / NAME, time.time() - 86400)); grab("6-offer")
            ui["hover"] = True; paint_btn(); c.itemconfig(skip, fill=INK); grab("7-offer-hover")
            root.destroy()
        root.after(900, steps)
    root.mainloop()


if __name__ == "__main__":
    main()
