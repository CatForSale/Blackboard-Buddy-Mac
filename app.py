"""Blackboard Buddy desktop app: a window around the dashboard, with syncing in the background."""

import html
import json
import os
from urllib.parse import urlparse
import re
import shutil
import uuid
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import webview

import main
import online
import vault as notebook

AUTO_SYNC_AFTER = timedelta(hours=6)
STATE = {"busy": False, "msg": "", "signin": False, "update": None}
window = None
viewer = None      # the window Blackboard links open in (Api.open_inside)
_maximised = False
BACKUP_EVERY = 300          # seconds between kept copies of a board
BACKUPS_KEPT = 20           # how many of those to hold on to, per board


def _file_name(name):
    """A board's name turned into something safe to call a file."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "board")).strip("-").lower()
    return (safe or "board")[:48]


def _write_safely(path, data):
    """Write beside the real file and swap it in, so a copy that is killed halfway through
    cannot leave a half-written board behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def friendly(msg):
    """Turns log lines into short status messages."""
    msg = re.sub(r"^AI: study notes for ", "Writing notes: ", msg)
    msg = re.sub(r"^AI: assignment plan for ", "Planning: ", msg)
    msg = re.sub(r"^AI: writing weekly briefing", "Writing your weekly briefing", msg)
    msg = re.sub(r"^AI: summarising grades.*", "Summarising your grades", msg)
    msg = re.sub(r"^Saving study materials for ", "Saving files: ", msg)
    msg = re.sub(r"\s+\d\d\w{3}_\d+$", "", msg)  # drop " 26sep_1" style suffixes
    return msg


def push(msg, busy=True):
    STATE["msg"] = msg
    if window:
        try:
            window.evaluate_js(f"window.bbStatus && bbStatus({json.dumps(msg)}, {str(busy).lower()})")
        except Exception:
            pass


def on_log(msg):
    if msg.startswith("Please sign in"):
        push("Sign in to Blackboard in the window that opened", True)
    elif not msg.startswith(("Signed in as", "Dashboard ready")):
        push(friendly(msg), True)


def sync_worker(interactive):
    try:
        push("Checking Blackboard…")
        path = main.run(sync=True, use_ai=True, interactive=interactive)
        STATE["signin"] = False
        # Done before the page is reloaded, not after. The fresh page asks what is going on as
        # soon as it loads, and if it is told "still working" it puts up a message that nothing
        # is left to take down again - the old copy that would have said so is gone.
        STATE["busy"] = False
        if path:
            window.load_url(path.as_uri() + "#updated")
    except main.SignInNeeded:
        # signed out: ask in the app instead of popping a browser window out of nowhere
        STATE["signin"] = True
        push("Signed out of Blackboard", False)
        try:
            window.evaluate_js("window.bbSignin && bbSignin()")
        except Exception:
            pass
    except Exception as e:
        main.log(f"Sync failed: {e!r}")
        push("Couldn't update. Check your internet and try ↻ again.", False)
    finally:
        STATE["busy"] = False


def start_sync(interactive=True):
    if STATE["busy"]:
        return False
    STATE["busy"] = True
    threading.Thread(target=sync_worker, args=(interactive,), daemon=True).start()
    return True


def _version(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag)[:3])


def check_update():
    """Looks at the latest GitHub release; returns {version, notes, url} if it's newer than this app."""
    if not main.UPDATE_REPO:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{main.UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": "BlackboardBuddy", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.load(r)
    zips = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")]
    if not zips or _version(rel.get("tag_name", "")) <= _version(main.VERSION):
        return None
    notes = (rel.get("body") or "").strip().splitlines()
    info = {"version": rel["tag_name"].lstrip("vV"), "notes": notes[0][:160] if notes else "",
            "url": zips[0]["browser_download_url"]}
    STATE["update"] = info
    return info


def update_loop():
    """Checks for a new version shortly after start, then twice a day."""
    time.sleep(20)
    while True:
        try:
            info = check_update()
            if info and window:
                window.evaluate_js(f"window.bbUpdate && bbUpdate({json.dumps(info)})")
        except Exception as e:
            main.log(f"Update check failed: {e!r}")
        time.sleep(12 * 3600)


def background_loop():
    """Quietly re-syncs every couple of hours; that is also how a sign-out gets noticed."""
    while True:
        time.sleep(15 * 60)
        synced, hours = last_synced(), main.settings()["sync_hours"]  # 0 = background sync off
        if hours and not STATE["signin"] and (main.OUT_DIR / "dashboard.html").exists() and (not synced or datetime.now() - synced > timedelta(hours=hours)):
            start_sync(interactive=False)


class Api:
    def refresh(self):
        return start_sync()

    def state(self):
        return STATE

    def sign_in(self):
        STATE["signin"] = False
        return start_sync(interactive=True)

    def track(self, kind):
        main.record_activity(kind)

    def activity(self):
        return main.load_json(main.ACTIVITY, {})

    def save_settings(self, values):
        from ai import MODELS, EFFORTS
        cur = main.settings()
        clean = {
            "sync_hours": int(values.get("sync_hours", cur["sync_hours"])),
            "notes_per_sync": max(0, min(15, int(values.get("notes_per_sync", cur["notes_per_sync"])))),
            "model": values.get("model") if values.get("model") in MODELS else cur["model"],
            "effort": values.get("effort") if values.get("effort") in EFFORTS else cur["effort"],
            "sounds": bool(values.get("sounds", cur["sounds"])),
            "volume": max(0, min(100, int(values.get("volume", cur["volume"])))),
        }
        main.save_json(main.SETTINGS, {**main.load_json(main.SETTINGS, {}), **clean})
        return "ok"

    def models(self):
        """The models Min can choose from, with what each costs."""
        from ai import MODELS, EFFORTS
        st = main.settings()
        return {"models": [{"id": k, **v} for k, v in MODELS.items()], "efforts": EFFORTS,
                "model": st["model"], "effort": st["effort"]}

    def usage(self):
        """What Claude has cost, for the usage panel."""
        return {**main.spend_summary(), "signed_in": main.signed_in()}

    # ---------- Claude accounts ----------

    def accounts(self):
        """The saved accounts, without ever handing the keys back to the page."""
        a = main.accounts()
        return {"active": a["active"],
                "list": [{"id": x["id"], "label": x.get("label", ""), "hint": x.get("hint", "")}
                         for x in a["list"]]}

    def account_add(self, label, key):
        """Checks the key against Anthropic before saving it, so a bad key can't be stored."""
        key = (key or "").strip()
        label = (label or "").strip() or "My Claude account"
        if not key.startswith("sk-ant-"):
            return {"ok": False, "error": "That doesn't look like a Claude API key. It starts with sk-ant-"}
        try:
            import anthropic
            anthropic.Anthropic(api_key=key).models.list(limit=1)
        except Exception as e:
            name = type(e).__name__
            if "Authentication" in name or "PermissionDenied" in name:
                return {"ok": False, "error": "Anthropic rejected that key. Check you copied all of it."}
            if "Connection" in name or "Timeout" in name:
                return {"ok": False, "error": "Couldn't reach Anthropic. Check your internet and try again."}
            return {"ok": False, "error": f"Couldn't sign in: {e}"}
        a = main.accounts()
        entry = {"id": uuid.uuid4().hex[:8], "label": label, "key": key, "hint": "…" + key[-4:]}
        a["list"] = [x for x in a["list"] if x.get("key") != key] + [entry]
        a["active"] = entry["id"]
        main.save_json(main.ACCOUNTS, a)
        main.set_env("ANTHROPIC_API_KEY", key)
        return {"ok": True}

    def account_use(self, account_id):
        a = main.accounts()
        hit = next((x for x in a["list"] if x["id"] == account_id), None)
        if not hit:
            return {"ok": False, "error": "That account is no longer saved."}
        a["active"] = account_id
        main.save_json(main.ACCOUNTS, a)
        main.set_env("ANTHROPIC_API_KEY", hit["key"])
        return {"ok": True}

    def account_remove(self, account_id):
        a = main.accounts()
        a["list"] = [x for x in a["list"] if x["id"] != account_id]
        if a["active"] == account_id:
            a["active"] = a["list"][-1]["id"] if a["list"] else None
        main.save_json(main.ACCOUNTS, a)
        main.set_env("ANTHROPIC_API_KEY", (main.active_account() or {}).get("key", ""))
        return {"ok": True}

    def account_sign_out(self):
        """Forgets every saved account, so the AI features stop until one is added again."""
        main.save_json(main.ACCOUNTS, {"active": None, "list": []})
        main.set_env("ANTHROPIC_API_KEY", "")
        return {"ok": True}

    def update_key(self, key):
        """Like save_key, but from Settings: no sync needed afterwards."""
        key = (key or "").strip()
        if not key.startswith("sk-ant-"):
            return "That doesn't look like a Claude API key. It starts with sk-ant-"
        main.set_env("ANTHROPIC_API_KEY", key)
        return "ok"

    def open_inside(self, url, title=""):
        """Opens a Blackboard page or a module's link in a window of the app instead of the browser.
        It shares the app's browser data, so after signing in to UWE once it stays signed in."""
        global viewer
        url = str(url)
        if not url.startswith(("https://", "http://")):
            return "Can't open that link."
        title = (str(title).strip() or "Blackboard")[:80] + " · Blackboard Buddy"
        if viewer in webview.windows:
            viewer.load_url(url)
            viewer.set_title(title)
            viewer.restore()
            viewer.show()
        else:
            viewer = webview.create_window(title, url, width=1120, height=820, min_size=(420, 400),
                                           background_color=start_colour())
        return "ok"

    def open_materials(self):
        try:
            main.MATERIALS.mkdir(parents=True, exist_ok=True)
            os.startfile(main.MATERIALS)
            return "ok"
        except OSError as e:
            main.log_quiet(f"open study files folder failed: {e!r}")
            return "Couldn't open that folder. Use Choose folder… to put your study files somewhere else."

    def choose_materials(self):
        """Lets you pick where study files live, then moves what's already downloaded there."""
        if STATE["busy"]:
            return {"error": "Busy syncing. Try again in a minute."}
        picked = window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(Path.home() / "Documents"))
        if not picked:
            return {"moved": False}
        folder = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        # picking Documents (or any folder with things in it) gets its own subfolder, not module folders dumped in it
        if folder.name != main.MATERIALS_NAME and folder.exists() and any(folder.iterdir()):
            folder = folder / main.MATERIALS_NAME
        STATE["busy"] = True
        try:
            push("Moving your study files…")
            main.move_materials(folder)
            main.run(sync=False, use_ai=False)
            push("Study files moved", False)
        except Exception as e:
            main.log_quiet(f"moving study files failed: {e!r}")
            push("Couldn't move the study files", False)
            return {"error": f"Couldn't move them: {e}"}
        finally:
            STATE["busy"] = False
        return {"moved": True, "path": str(main.MATERIALS)}

    def sign_out(self):
        if STATE["busy"]:
            return "Busy syncing. Try again in a minute."
        shutil.rmtree(main.PROFILE_DIR, ignore_errors=True)
        return "Signed out. The next sync will ask you to sign in."

    def ask(self, history):
        """Ask Claude about your Blackboard data. history: [{role, content}], ending with your question."""
        msgs = [{"role": m["role"], "content": str(m["content"])[:8000]} for m in (history or [])[-20:]
                if m.get("role") in ("user", "assistant")]
        if not msgs or msgs[0]["role"] != "user":
            return {"error": "Nothing to send."}
        if not os.getenv("ANTHROPIC_API_KEY"):
            return {"error": "Add your Claude API key in Settings first."}
        snapshot = main.load_json(main.SNAPSHOT, None)
        if not snapshot:
            return {"error": "Sync with Blackboard first."}
        from ai import AI
        try:
            ai = main.new_ai(effort="low")
            if not ai:
                return "Add a Claude account in Settings first, then I can answer."
            text = ai.chat(msgs, main.chat_context(snapshot))
        except Exception as e:
            main.log(f"Ask Claude failed: {e!r}")
            return {"error": "Couldn't reach Claude. Check your internet and API key."}
        main.add_spend(ai.cost)
        main.record_activity("ask")
        return {"md": text, "html": main.md(text)}

    def set_done(self, key, on):
        """Ticks an assignment off, then rebuilds the page from the last sync (no Blackboard, no AI)."""
        main.set_done(str(key), bool(on))
        if STATE["busy"]:
            return "saved"          # the sync that's running builds the page with it anyway
        try:
            main.run(sync=False, use_ai=False)
            return "ok"
        except Exception as e:
            main.log_quiet(f"set_done rebuild failed: {e!r}")
            return "saved"

    def make_notes(self, item_id, view):
        if STATE["busy"]:
            return "Busy syncing. Try again in a minute."
        if not os.getenv("ANTHROPIC_API_KEY"):
            return "Add your Claude API key in Settings first."
        STATE["busy"] = True
        try:
            push("Writing notes…")
            r = main.make_notes_for(item_id)
        except Exception as e:
            main.log(f"Make notes failed: {e!r}")
            r = "Couldn't write notes. Check your internet and API key."
        finally:
            STATE["busy"] = False
        if r == "ok":
            window.load_url((main.OUT_DIR / "dashboard.html").as_uri() + "#updated:" + re.sub(r"[^\w-]", "", view))
        else:
            push(r, False)
        return r

    def open_file(self, path):
        """Opens a downloaded study file in its own app (Word, PowerPoint...)."""
        f = Path(path).resolve()
        if f.is_relative_to(main.MATERIALS.resolve()) and f.exists():
            os.startfile(f)

    def open_calendar(self):
        os.startfile(main.OUT_DIR / "deadlines.ics")

    # ---- Notebook
    def online_config(self, url, key):
        """Saves the group's Supabase address and public key typed on the Online tab."""
        url, key = str(url).strip().rstrip("/"), str(key).strip()
        if not online.URL_OK.match(url):
            return "The address looks like https://something.supabase.co"
        if len(key) < 30 or key.startswith("sk-"):
            return "Use the long \"anon public\" key, not a secret one"
        main.save_json(main.SETTINGS, {**main.load_json(main.SETTINGS, {}), "online_url": url, "online_key": key})
        return "ok"

    def code_ask(self, history, files, current="", output=""):
        """Ask Claude about an Online code project. history: [{role, content}], ending with the question."""
        msgs = [{"role": m["role"], "content": str(m["content"])[:8000]} for m in (history or [])[-10:]
                if m.get("role") in ("user", "assistant")]
        if not msgs or msgs[0]["role"] != "user":
            return {"error": "Nothing to send."}
        files = [{"n": str(f.get("n", ""))[:120], "t": str(f.get("t", ""))} for f in (files or [])[:40] if isinstance(f, dict)]
        try:
            ai = main.new_ai(effort="low")
            if not ai:
                return {"error": "Add a Claude account in Settings first, then I can help."}
            text = ai.code_help(msgs, files, str(current or ""), str(output or ""))
        except Exception as e:
            main.log(f"Code help failed: {e!r}")
            return {"error": "Couldn't reach Claude. Check your internet and API key."}
        main.add_spend(ai.cost)
        main.record_activity("ask")
        return {"md": text}

    def ol_vscode(self, title, files, how="code"):
        """Saves a board project's files to Documents\\Blackboard Buddy projects\\<name> and opens that folder in VS Code
        (or in File Explorer when VS Code isn't installed, or when how="folder"). The board stays the shared copy."""
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # the real Documents folder, even when OneDrive moved it
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or "Project")).strip(" .")[:60] or "Project"
        folder = Path(buf.value or Path.home() / "Documents") / "Blackboard Buddy projects" / name
        folder.mkdir(parents=True, exist_ok=True)
        for f in list(files or [])[:300]:
            rel = Path(str(f.get("n", "")).replace("\\", "/"))
            if not rel.parts or rel.anchor or ".." in rel.parts:  # anchor covers C:/x, /x and C:x
                continue
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(f.get("t", "")), encoding="utf-8")
        if how == "folder":
            os.startfile(folder)
            return f"Saved to Documents\\Blackboard Buddy projects\\{name}"
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe"
        code = str(exe) if exe.exists() else shutil.which("code")
        if code:
            subprocess.Popen([code, str(folder)], creationflags=0x08000000)
            return "Opened in VS Code"
        os.startfile(folder)
        return "VS Code isn't installed, so the folder opened instead"

    def ol_local_load(self):
        return main.load_json(main.DATA_DIR / "online-board.json", [])

    def ol_local_save(self, rows):
        if isinstance(rows, list):
            _write_safely(main.DATA_DIR / "online-board.json", rows)
        return "ok"

    def ol_local_saved_at(self):
        """When the offline board was last written, in milliseconds, or 0 if it never was."""
        f = main.DATA_DIR / "online-board.json"
        try:
            return int(f.stat().st_mtime * 1000)
        except OSError:
            return 0

    def board_backup(self, name, rows, every=BACKUP_EVERY):
        """Keep a copy of the board on this computer, whichever board it is. The group board
        lives on the server, so without this there is nothing here to fall back on.

        One file is always the newest copy; older ones are kept at a distance in time from
        each other so a week of work does not turn into a thousand near-identical files."""
        if not isinstance(rows, list):
            return False
        who = _file_name(name)
        folder = main.DATA_DIR / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        _write_safely(folder / f"{who}-latest.json", rows)
        kept = sorted(folder.glob(f"{who}-20*.json"))
        try:
            gap = time.time() - kept[-1].stat().st_mtime if kept else None
        except OSError:
            gap = None
        if gap is None or gap >= float(every or 0):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            _write_safely(folder / f"{who}-{stamp}.json", rows)
            kept = sorted(folder.glob(f"{who}-20*.json"))
            for old in kept[:-BACKUPS_KEPT]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return True

    def board_backups(self, name):
        """What copies of this board are on the computer, newest first."""
        folder = main.DATA_DIR / "backups"
        who = _file_name(name)
        out = []
        for f in sorted(folder.glob(f"{who}-*.json"), reverse=True):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({"file": f.name, "when": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "size": st.st_size, "latest": f.name.endswith("-latest.json")})
        return out[:40]

    def board_backup_read(self, file):
        """One of those copies, by the name board_backups gave."""
        folder = main.DATA_DIR / "backups"
        f = folder / Path(str(file)).name          # never step outside the backups folder
        if not f.exists() or f.parent != folder:
            return None
        return main.load_json(f, None)

    def board_backup_folder(self):
        folder = main.DATA_DIR / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)
        return str(folder)

    def nb_list(self):
        return notebook.all_notes()

    def nb_save(self, name, text, old=None):
        return notebook.save(name, text, old)

    def nb_delete(self, name):
        return notebook.delete(name)

    def nb_image(self, data_url, filename=""):
        return notebook.save_image(data_url, filename)

    def nb_render(self, text):
        return notebook.render(text)

    def nb_open_folder(self):
        notebook.init()
        os.startfile(notebook.ROOT)

    def check_update(self):
        if not main.UPDATE_REPO:
            return {"error": "Updates aren't set up for this copy."}
        try:
            return check_update() or {}
        except Exception:
            return {"error": "Couldn't check. Are you online?"}

    def install_update(self):
        """Downloads the new version, swaps it in after the app closes, and starts it again."""
        info = STATE["update"]
        if not info:
            return "No update found."
        if not getattr(sys, "frozen", False):
            return "Updates only work in the installed app."
        if not info["url"].startswith("https://github.com/"):
            return "That download doesn't come from GitHub."
        if STATE["busy"]:
            return "Busy syncing. Try again in a minute."
        work = Path(tempfile.mkdtemp(prefix="bb-update-"))
        try:
            push("Downloading the update…")
            req = urllib.request.Request(info["url"], headers={"User-Agent": "BlackboardBuddy"})
            with urllib.request.urlopen(req, timeout=120) as r, open(work / "update.zip", "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(work / "update.zip") as z:
                z.extractall(work / "new")
            exe = next((work / "new").rglob("Blackboard Buddy.exe"), None)
            if not exe or not (exe.parent / "_internal").is_dir():
                return "The update file looks broken. Try again later."
        except Exception as e:
            main.log(f"Update download failed: {e!r}")
            return "Couldn't download the update. Check your internet."
        app_dir = Path(sys.executable).parent
        # a little script waits for this window to close, copies the new files over
        # (your .env, data and notes are not in the zip, so they stay), then reopens the app
        bat = work / "update.bat"
        bat.write_text(f"""@echo off
:wait
tasklist /FI "PID eq {os.getpid()}" | find "{os.getpid()}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
robocopy "{exe.parent}" "{app_dir}" /E /R:3 /W:1 /NFL /NDL /NJH /NJS >nul
start "" "{app_dir / 'Blackboard Buddy.exe'}"
""", encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        threading.Timer(0.5, window.destroy).start()
        return "ok"

    def win_minimize(self):
        window.minimize()

    def win_maximize(self):
        """Maximise, or put the window back if it already is. Returns the new state."""
        global _maximised
        if _zoomed():
            window.restore()
            _maximised = False
        else:
            window.maximize()
            _maximised = True
        return _maximised

    def win_drag_mode(self):
        """Who is doing the dragging: 'browser' when the browser hands it to the window itself,
        'app' when the page has to move the window on its own."""
        return "browser" if STATE.get("drag_regions") else "app"

    def win_drag_ready(self):
        """The page asks before relying on it, because an older copy of the app has no win_drag
        and would leave the window unable to move at all."""
        return True

    def win_drag(self):
        """Hand the drag to Windows itself. Doing the move ourselves meant Windows never saw a
        drag, so none of its own snapping happened: no zones at the screen edges, no Snap
        Assist, no drag to the top to maximise. This gives all of that back."""
        try:
            from ctypes import windll, wintypes
            hwnd = _hwnd()
            if not hwnd:
                return False
            if _zoomed():                 # dragging a maximised window puts it back first
                window.restore()
            # A window with no border of its own is one WinForms refuses to move: its own
            # message handling drops SC_MOVE and WM_NCLBUTTONDOWN before Windows ever sees
            # them. So hand the message straight to Windows' own handler, on the window's own
            # thread, which is where a move loop has to run. Windows then does the dragging,
            # and with it the snap zones and the layouts bar at the top of the screen.
            form = getattr(webview.windows[0], "native", None)
            if form is None:
                return False
            from System import Action
            windll.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                                     wintypes.WPARAM, wintypes.LPARAM]
            windll.user32.DefWindowProcW.restype = wintypes.LPARAM
            # BeginInvoke, because the move loop does not come back until the drag is over
            form.BeginInvoke(Action(lambda: windll.user32.DefWindowProcW(hwnd, 0x0112, 0xF012, 0)))
            return True
        except Exception as e:
            main.log_quiet(f"Could not start a window drag: {e}")
            return False

    def win_snap_menu(self):
        """Open Windows' own layout chooser for this window, the same one Win+Z opens."""
        try:
            from ctypes import windll
            hwnd = _hwnd()
            if not hwnd:
                return False
            windll.user32.SetForegroundWindow(hwnd)
            # WM_SYSCOMMAND with SC_MAXIMIZE's neighbour: Windows shows the layouts for the
            # window under the keyboard, so send it the shortcut itself.
            _tap_win_z()
            return True
        except Exception as e:
            main.log_quiet(f"Could not open the layout chooser: {e}")
            return False

    def win_close(self):
        window.destroy()

    def win_resize_edge(self, direction):
        """Hands the drag to Windows itself, which runs its own resize loop - no polling, no lag."""
        try:
            from ctypes import windll
            hwnd = _hwnd()
            d = int(direction)
            if not hwnd or not 1 <= d <= 8:
                return False
            windll.user32.ReleaseCapture()
            windll.user32.PostMessageW(hwnd, 0x0112, 0xF000 + d, 0)   # WM_SYSCOMMAND, SC_SIZE
            return True
        except Exception as e:
            main.log_quiet(f"Could not start a resize: {e}")
            return False

    def win_bounds(self):
        """Where the window is on screen, in real pixels."""
        from ctypes import windll, wintypes, byref
        hwnd = _hwnd()
        if not hwnd:
            return None
        r = wintypes.RECT()
        windll.user32.GetWindowRect(hwnd, byref(r))
        return {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}

    def win_set_bounds(self, x, y, w, h):
        """Put the window exactly here. The page drives resizing itself, because a frameless
        window hands its edges to the browser control and Windows never sees the drag."""
        from ctypes import windll
        hwnd = _hwnd()
        if not hwnd:
            return False
        # SWP_NOZORDER | SWP_NOACTIVATE
        windll.user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)
        return True

    def work_area(self):
        """The usable part of the screen the window is on, taskbar excluded."""
        from ctypes import windll, wintypes, byref, Structure, sizeof

        class MONITORINFO(Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        hwnd = _hwnd()
        if not hwnd:
            return None
        mon = windll.user32.MonitorFromWindow(hwnd, 2)       # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = sizeof(MONITORINFO)
        windll.user32.GetMonitorInfoW(mon, byref(mi))
        r = mi.rcWork
        return {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top}

    def win_snap(self, where):
        """Half and quarter screen layouts, since a frameless window gets no Snap Layouts menu."""
        a = self.work_area()
        if not a:
            return False
        x, y, w, h = a["x"], a["y"], a["w"], a["h"]
        half_w, half_h = w // 2, h // 2
        spots = {
            "left": (x, y, half_w, h), "right": (x + half_w, y, w - half_w, h),
            "top": (x, y, w, half_h), "bottom": (x, y + half_h, w, h - half_h),
            "tl": (x, y, half_w, half_h), "tr": (x + half_w, y, w - half_w, half_h),
            "bl": (x, y + half_h, half_w, h - half_h), "br": (x + half_w, y + half_h, w - half_w, h - half_h),
            "full": (x, y, w, h),
        }
        if where not in spots:
            return False
        if window and window.maximized:
            window.restore()
        return self.win_set_bounds(*spots[where])

    def note_theme(self, name):
        """The page tells us its theme so the next launch can open in the right colour."""
        if name in ("light", "dark"):
            main.save_json(main.SETTINGS, {**main.load_json(main.SETTINGS, {}), "theme": name})
        return "ok"

    def note_start(self, marks):
        """A line in the run log saying how far the page got, for when it comes up blank."""
        STATE["page_ok"] = True
        main.log_quiet("page start: " + str(marks)[:400])
        return "ok"

    def win_state(self):
        return _zoomed()

    def setup(self, key, url, name=""):
        """First run: save the name, Blackboard address and (optionally) the Claude key, then sync."""
        name = " ".join((name or "").split())[:30]
        if not os.getenv("USER_NAME"):
            if not name:
                return "Enter your name"
            main.set_env("USER_NAME", name)
        if url is not None and not os.getenv("BB_URL"):
            u = urlparse((url or "").strip())
            if u.scheme != "https" or not u.netloc:
                return "Enter the full address, starting with https://"
            main.BB_URL = f"https://{u.netloc}"
            main.set_env("BB_URL", main.BB_URL)
        key = (key or "").strip()
        if key:
            if not key.startswith("sk-ant-"):
                return "That doesn't look like a Claude API key. It starts with sk-ant-"
            main.set_env("ANTHROPIC_API_KEY", key)
        start_sync()
        return "ok"


def last_synced():
    snap = main.load_json(main.SNAPSHOT, None)
    return main.parse_date(snap["synced"]).replace(tzinfo=None) if snap else None


def welcome_page(need_key, need_url, need_name=False):
    """First-run screen: name, Blackboard address and Claude key, then a loading view while the first sync runs."""
    url = html.escape(main.BB_URL)
    form = need_key or need_url or need_name
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Doto:wght@800&family=Space+Mono&display=swap" rel="stylesheet">
<style>
:root{{color-scheme:light dark;--bg:#efefef;--card:rgba(255,255,255,.75);--text:#0b0b0b;--muted:#7b7b7b;--line:rgba(0,0,0,.1);--dot:rgba(0,0,0,.13)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:rgba(18,18,18,.75);--text:#f2f2f2;--muted:#8a8a8a;--line:rgba(255,255,255,.12);--dot:rgba(255,255,255,.11)}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
 background-image:radial-gradient(var(--dot) 1.2px,transparent 1.3px);background-size:22px 22px;
 font:15px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif}}
.card{{width:min(420px,88vw);padding:34px;border-radius:28px;background:var(--card);border:1px solid var(--line);
 backdrop-filter:blur(22px);animation:rise .8s cubic-bezier(.2,.9,.3,1.2) both}}
@keyframes rise{{from{{opacity:0;transform:translateY(16px) scale(.97);filter:blur(6px)}}}}
h1{{margin:0 0 4px;font:800 34px/1.1 "Doto","Space Mono",monospace;text-transform:uppercase}}
.sub{{color:var(--muted);margin:0 0 22px;font-size:14px}}
label{{display:block;margin:16px 0 6px;font:11px "Space Mono",Consolas,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
input{{width:100%;box-sizing:border-box;padding:12px 16px;border-radius:14px;border:1px solid var(--line);background:transparent;color:inherit;font:inherit}}
small{{display:block;color:var(--muted);margin-top:6px;font-size:12.5px}}
.btn{{margin-top:22px;width:100%;padding:12px;border:0;border-radius:999px;background:var(--text);color:var(--bg);
 font:12px "Space Mono",Consolas,monospace;letter-spacing:.1em;text-transform:uppercase;cursor:pointer}}
.err{{color:#d71921;font-size:13px;min-height:1em;margin:10px 0 0}}
.dots{{display:flex;gap:8px;justify-content:center;margin:6px 0 18px}} .dots i{{width:9px;height:9px;border-radius:50%;background:var(--text);animation:blink 1.2s infinite}}
.dots i:nth-child(2){{animation-delay:.2s}} .dots i:nth-child(3){{animation-delay:.4s}} @keyframes blink{{0%,80%,100%{{opacity:.2}}40%{{opacity:1}}}}
#load{{display:{'none' if form else 'block'};text-align:center}}
</style></head><body><div class="card">
<div id="form" style="display:{'block' if form else 'none'}"><h1>Blackboard Buddy</h1>
 <p class="sub">Your week, deadlines, files and notes from Blackboard, in one app.</p>
 <div style="display:{'block' if need_name else 'none'}"><label for="n">Your name</label>
 <input id="n" autocomplete="off" spellcheck="false" maxlength="30" placeholder="What should the app call you?"></div>
 <div style="display:{'block' if need_url else 'none'}"><label for="u">Your Blackboard address</label>
 <input id="u" value="{url}" autocomplete="off" spellcheck="false">
 <small>The address you use to open Blackboard, e.g. https://blackboard.uwe.ac.uk</small></div>
 <div style="display:{'block' if need_key else 'none'}"><label for="k">Claude API key (optional)</label>
 <input id="k" type="password" placeholder="sk-ant-…" autocomplete="off">
 <small>Needed for the briefing, notes, plans and Ask Claude. Get one at platform.claude.com → API keys. You can add it later in Settings.</small></div>
 <p id="err" class="err"></p><button class="btn" onclick="save()">Continue</button></div>
<div id="load"><div class="dots"><i></i><i></i><i></i></div><h1 style="font-size:24px">Getting your week ready</h1>
 <p class="sub" id="msg">A Blackboard window will open. Sign in there.</p></div>
</div><script>
function loading(){{document.getElementById('form').style.display='none';document.getElementById('load').style.display='block'}}
async function save(){{const r=await pywebview.api.setup(document.getElementById('k').value,document.getElementById('u').value,document.getElementById('n').value);
 if(r==='ok')loading();else document.getElementById('err').textContent=r}}
document.addEventListener('keydown',e=>{{if(e.key==='Enter')save()}});
window.bbStatus=(m)=>{{document.getElementById('msg').textContent=m}};
</script></body></html>"""


def on_start():
    _restore_resizing()
    main.adopt_env_key()
    has_dashboard = (main.OUT_DIR / "dashboard.html").exists()
    threading.Thread(target=background_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    if not has_dashboard and (not os.getenv("ANTHROPIC_API_KEY") or not os.getenv("BB_URL") or not os.getenv("USER_NAME")):
        return  # the welcome page asks for the Blackboard address and key, then starts the sync
    synced = last_synced()
    if not has_dashboard or not synced or datetime.now() - synced > AUTO_SYNC_AFTER:
        start_sync(interactive=not has_dashboard)


def _zoomed():
    """Is the window maximised right now, as far as Windows is concerned?"""
    try:
        from ctypes import windll
        hwnd = _hwnd()
        return bool(hwnd and windll.user32.IsZoomed(hwnd))
    except Exception:
        return _maximised


def _tap_win_z():
    """Press and let go of Win+Z, which is how Windows is asked for its layout chooser."""
    from ctypes import windll
    VK_LWIN, VK_Z, KEYUP = 0x5B, 0x5A, 0x0002
    windll.user32.keybd_event(VK_LWIN, 0, 0, 0)
    windll.user32.keybd_event(VK_Z, 0, 0, 0)
    windll.user32.keybd_event(VK_Z, 0, KEYUP, 0)
    windll.user32.keybd_event(VK_LWIN, 0, KEYUP, 0)


def _centre_when_settled():
    """WinForms is still sizing the window for a moment after it appears, so centring it
    straight away lands it off by however much it changed afterwards. Wait for its size to
    stop moving, then put it in the middle."""
    from ctypes import windll, wintypes, byref
    last, same = None, 0
    for _ in range(40):
        time.sleep(0.2)
        hwnd = _hwnd()
        if not hwnd:
            continue
        r = wintypes.RECT()
        windll.user32.GetWindowRect(hwnd, byref(r))
        size = (r.right - r.left, r.bottom - r.top)
        same = same + 1 if size == last else 0
        last = size
        if same >= 2:
            break
    try:
        centre_window()
    except Exception as e:
        main.log_quiet(f"Could not centre the window: {e}")


def centre_window():
    """Open in the middle of the screen the mouse is on, rather than wherever Windows felt like
    putting it. A screen smaller than the window gets the window shrunk to fit it."""
    from ctypes import windll, wintypes, byref, Structure, sizeof

    class MONITORINFO(Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    hwnd = _hwnd()
    if not hwnd:
        return False
    r = wintypes.RECT()
    windll.user32.GetWindowRect(hwnd, byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    pt = wintypes.POINT()
    windll.user32.GetCursorPos(byref(pt))
    mon = windll.user32.MonitorFromPoint(pt, 2) or windll.user32.MonitorFromWindow(hwnd, 2)
    mi = MONITORINFO()
    mi.cbSize = sizeof(MONITORINFO)
    windll.user32.GetMonitorInfoW(mon, byref(mi))
    a = mi.rcWork
    aw, ah = a.right - a.left, a.bottom - a.top
    w, h = min(w, aw), min(h, ah)
    x, y = a.left + (aw - w) // 2, a.top + (ah - h) // 2
    # SWP_NOZORDER | SWP_NOACTIVATE
    windll.user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)
    main.log_quiet(f"launch: window centred at {x},{y} ({w}x{h})")
    return True


# WinForms drops the sizing border along with the title bar, so add it back: WS_THICKFRAME
# gives the window its resize edges, WS_MAXIMIZEBOX lets Windows snap and maximise it.
def _hwnd():
    """The window's handle. GetActiveWindow is per-thread and on_start runs on its own
    thread, so ask WinForms for the real one and fall back to looking it up by title."""
    try:
        native = getattr(webview.windows[0], "native", None)
        handle = getattr(native, "Handle", None)
        if handle is not None:
            return int(handle.ToInt64())
    except Exception:
        pass
    return _find_window(os.getpid())


def _find_window(pid=None):
    """The "Blackboard Buddy" window of process `pid`, or of any copy of this program when pid is None.
    Going by the title alone also finds an Explorer window open on the app's folder, which has
    the same name - that is what made a fresh launch think the app was already open."""
    import ctypes
    from ctypes import windll, wintypes
    k, u = windll.kernel32, windll.user32
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    exe, found = os.path.basename(sys.executable).lower(), []

    def check(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        u.GetWindowTextW(hwnd, buf, 64)
        if buf.value != "Blackboard Buddy":
            return True
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if pid is not None:
            ours = owner.value == pid
        else:
            h = k.OpenProcess(0x1000, False, owner.value)     # PROCESS_QUERY_LIMITED_INFORMATION
            path, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
            ours = bool(h) and k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)) \
                and os.path.basename(path.value).lower() == exe and owner.value != os.getpid()
            if h:
                k.CloseHandle(h)
        if ours:
            found.append(hwnd)
        return not ours

    u.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(check), 0)
    return found[0] if found else 0


def _restore_resizing():
    from ctypes import windll
    # WS_SYSMENU matters as much as the rest: without the system menu Windows ignores SC_MOVE
    # and SC_SIZE, so handing it a drag does nothing and none of its snapping can happen.
    GWL_STYLE, WS_THICKFRAME, WS_MAXIMIZEBOX, WS_MINIMIZEBOX = -16, 0x00040000, 0x00010000, 0x00020000
    WS_SYSMENU = 0x00080000
    for attempt in range(20):            # the form may not exist for the first moment
        try:
            hwnd = _hwnd()
            if hwnd:
                style = windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
                windll.user32.SetWindowLongW(hwnd, GWL_STYLE,
                                             style | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX | WS_SYSMENU)
                # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
                windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0020)
                now = windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
                main.log_quiet(f"Resize border {'on' if now & WS_THICKFRAME else 'MISSING'} (window {hwnd})")
                threading.Thread(target=_centre_when_settled, daemon=True).start()
                return
        except Exception as e:
            main.log_quiet(f"Could not restore the resize border: {e}")
            return
        time.sleep(0.25)
    main.log_quiet("Could not find the window to restore its resize border")


def wait_for_profile(seconds=6.0):
    """Reopening quickly can find the browser's profile still locked by the copy that just
    closed, and then the window opens empty with nothing to show for it. Wait for the lock."""
    from ctypes import windll
    lock = main.LOCAL / "webview" / "EBWebView" / "lockfile"
    if not lock.exists():
        return True
    GENERIC_READ, OPEN_EXISTING, INVALID = 0x80000000, 3, -1
    waited = 0.0
    while waited < seconds:
        # dwShareMode 0: this only succeeds when nobody else holds the file
        h = windll.kernel32.CreateFileW(str(lock), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
        if h != INVALID:
            windll.kernel32.CloseHandle(h)
            if waited:
                main.log_quiet(f"launch: the last copy let go of the browser profile after {waited:.1f}s")
            return True
        time.sleep(0.25)
        waited += 0.25
    main.log_quiet("launch: the browser profile is still locked, carrying on anyway")
    return False


def watch_first_paint():
    """If the page never reports in, the window is sitting there empty. Load it again."""
    for attempt in (1, 2):
        time.sleep(20)
        if STATE.get("page_ok") or not window:
            return
        main.log_quiet(f"launch: nothing from the page after 20s, reloading (try {attempt})")
        try:
            window.load_url((main.OUT_DIR / "dashboard.html").as_uri())
        except Exception as e:
            main.log_quiet(f"launch: could not reload: {e}")
    if not STATE.get("page_ok"):
        main.log_quiet("launch: the page never reported in. The window is probably blank.")


# NOT wired to window.events.closing on purpose: that event is raised on the window's own
# thread, and evaluate_js waits for that same thread to answer, so the app would hang on the way
# out. A fast close is covered instead by the page mirroring every change into browser storage
# the instant it happens, which it reads back on the next open. This is kept for a caller that
# already has a thread of its own.
def save_on_close():
    """Take the board off the page and write it here. Never call this from the window's own
    thread - see above."""
    try:
        raw = window.evaluate_js("window.bbRows ? JSON.stringify(window.bbRows()) : ''")
        if not raw:
            return True
        got = json.loads(raw)
        rows = got.get("rows") or []
        if got.get("mode") == "local":
            _write_safely(main.DATA_DIR / "online-board.json", rows)
        if rows:
            folder = main.DATA_DIR / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            _write_safely(folder / f"{_file_name(got.get('name'))}-latest.json", rows)
        main.log_quiet(f"closing: saved {len(rows)} blocks from the {got.get('mode')} board")
    except Exception as e:
        main.log_quiet(f"closing: could not save the board: {e}")
    return True


def start_colour():
    """What the empty window shows while the browser engine starts. Matching the last theme
    means no white flash on a dark setup."""
    return "#0b0b0b" if main.settings().get("theme") == "dark" else "#efefef"


def only_one_copy():
    """True if this is the only copy running. A second one would fight the first for the
    browser's data folder and open as an empty window, so it bows out and raises the first."""
    from ctypes import windll
    handle = windll.kernel32.CreateMutexW(None, True, "BlackboardBuddy.OneCopy")
    if windll.kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        hwnd = _find_window()
        if hwnd and windll.user32.IsWindowVisible(hwnd):
            windll.user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            windll.user32.SetForegroundWindow(hwnd)
            return False
        # The last copy has closed its window but takes a few seconds to shut down. Bowing out
        # here would leave nothing on screen, so wait for it to let go and open as normal.
        main.log_quiet("launch: the last copy is still shutting down, waiting for it")
        if windll.kernel32.WaitForSingleObject(handle, 20000) not in (0, 0x80):   # WAIT_OBJECT_0, WAIT_ABANDONED
            main.log_quiet("launch: the last copy never finished shutting down")
            return False
    globals()["_one_copy"] = handle                    # held for the life of the process
    return True


def run():
    global window
    main.log_quiet("launch: starting")
    if not only_one_copy():
        main.log_quiet("launch: another copy is already running, raising it instead")
        return
    wait_for_profile()
    # WebView2 mutes a page until it's clicked; let the start-up chime play (kept alongside any other flags)
    flags = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if "autoplay-policy" not in flags:
        flags = (flags + " --autoplay-policy=no-user-gesture-required").strip()
    # Draggable regions are off unless asked for. With them on, the browser hands a drag of the
    # title strip to the window itself, which is the only way Windows gets to do the dragging -
    # and with it the snap zones at the screen edges.
    if "DraggableRegions" not in flags:
        flags = (flags + " --enable-features=msWebView2EnableDraggableRegions").strip()
        STATE["drag_regions"] = True
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = flags
    main.LISTENERS.append(on_log)
    dashboard = main.OUT_DIR / "dashboard.html"
    if dashboard.exists():
        window = webview.create_window("Blackboard Buddy", url=dashboard.as_uri(), js_api=Api(),
                                       width=1180, height=860, min_size=(420, 560), background_color=start_colour(),
                                       frameless=True, easy_drag=False)
    else:
        window = webview.create_window("Blackboard Buddy", html=welcome_page(not os.getenv("ANTHROPIC_API_KEY"), not os.getenv("BB_URL"), not os.getenv("USER_NAME")),
                                       js_api=Api(), width=1180, height=860, min_size=(420, 560),
                                       background_color=start_colour(), frameless=True, easy_drag=False)
    # closing the app closes the Blackboard window too, so nothing is left running in the background
    window.events.closed += lambda: viewer in webview.windows and viewer.destroy()
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    # only the title strip itself drags the window, never the buttons sitting on it
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    main.log_quiet("launch: window made, handing over to the browser engine")
    threading.Thread(target=watch_first_paint, daemon=True).start()
    try:
        webview.start(on_start, private_mode=False, storage_path=str(main.LOCAL / "webview"))
    except Exception:
        import traceback
        main.log_quiet("launch FAILED: " + traceback.format_exc()[-600:])
        raise
    main.log_quiet("launch: closed")


if __name__ == "__main__":
    run()
