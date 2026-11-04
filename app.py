"""Blackboard Buddy desktop app: a window around the dashboard, with syncing in the background."""

import atexit
import base64
import gzip
import secrets
import codecs
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
import signal
from datetime import datetime, timedelta
from pathlib import Path

import webview

import main
import online
import vault as notebook

AUTO_SYNC_AFTER = timedelta(hours=6)
STATE = {"busy": False, "quiet": False, "msg": "", "signin": False, "update": None}  # quiet: the AI is writing after the page already updated
window = None
viewer = None      # the window Blackboard links open in (Api.open_inside)
_maximised = False
BACKUP_EVERY = 300          # seconds between kept copies of a board
BACKUPS_KEPT = 20           # how many of those to hold on to, per board
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0     # CREATE_NO_WINDOW; anything but 0 is an error on a Mac
DETACHED = 0x00000008 if IS_WIN else 0      # DETACHED_PROCESS


def _file_name(name):
    """A board's name turned into something safe to call a file."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "board")).strip("-").lower()
    return (safe or "board")[:48]


# The first offline board keeps the file it always had; any more go in a folder of their own.
def _board_file(board):
    board = str(board or "main")
    if board == "main":
        return main.DATA_DIR / "online-board.json"
    if not re.fullmatch(r"[0-9a-f]{6,32}", board):
        raise ValueError("not a board")
    return main.DATA_DIR / "offline-boards" / f"{board}.json"


def _boards():
    kept = main.load_json(main.DATA_DIR / "offline-boards.json", [])
    kept = [b for b in kept if isinstance(b, dict) and b.get("id")] if isinstance(kept, list) else []
    if not any(b["id"] == "main" for b in kept):
        kept.insert(0, {"id": "main", "name": "Offline board"})
    return kept


def _boards_save(boards):
    _write_safely(main.DATA_DIR / "offline-boards.json", boards)


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
    elif msg.startswith("AI cost this run"):   # the last line of a check: nothing is still going on
        push(friendly(msg), False)
    elif not msg.startswith(("Signed in as", "Dashboard ready")):
        push(friendly(msg), not STATE["quiet"])


def reload(path):
    """Shows the new page - unless you're working on the Online or Notebook tab, where it waits for a tab switch."""
    url = json.dumps(path.as_uri() + "#updated")
    try:
        window.evaluate_js(f"window.bbReloadTo ? bbReloadTo({url}) : location.replace({url})")
    except Exception:
        window.load_url(path.as_uri() + "#updated")


def sync_worker(interactive):
    def ready(path):   # Blackboard's part is on the page already; the AI keeps writing quietly after
        STATE["quiet"] = True
        reload(path)
    try:
        backup_all("before a check")
        push("Checking Blackboard…")
        path = main.run(sync=True, use_ai=True, interactive=interactive, on_ready=ready)
        STATE["signin"] = False
        # Done before the page is reloaded, not after. The fresh page asks what is going on as
        # soon as it loads, and if it is told "still working" it puts up a message that nothing
        # is left to take down again - the old copy that would have said so is gone.
        STATE["busy"] = False
        push("Up to date", False)    # take the spinner down even if the page can't be reloaded
        if path and (not STATE["quiet"] or main.AI_NEW):
            reload(path)
        if main.settings().get("share"):          # classmates following you get this sync too
            threading.Thread(target=share_push, daemon=True).start()
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
        STATE["busy"] = STATE["quiet"] = False


def start_sync(interactive=True):
    if STATE["busy"]:
        return False
    if main.following():                         # ↻ fetches their latest shared notes instead of Blackboard
        STATE["busy"] = True
        threading.Thread(target=follow_worker, daemon=True).start()
        return True
    if not main.signed_in():                     # no Claude key: Online and Notebook only, nothing to fetch
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
    zips = [a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")
            and ("mac" in a["name"].lower()) == (sys.platform == "darwin")]
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
            main.open_path(main.MATERIALS)
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
            main.open_path(f)

    def open_calendar(self):
        main.open_path(main.OUT_DIR / "deadlines.ics")

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
        folder = _proj_folder(title)
        name = folder.name
        _write_files(folder, files)
        if how == "folder":
            main.open_path(folder)
            return f"Saved to Documents\\Blackboard Buddy projects\\{name}"
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe"
        code = str(exe) if exe.exists() else shutil.which("code")
        if not IS_WIN and not code and Path("/Applications/Visual Studio Code.app").exists():
            subprocess.Popen(["open", "-a", "Visual Studio Code", str(folder)])
            return "Opened in VS Code"
        if code:
            subprocess.Popen([code, str(folder)], creationflags=0x08000000)
            return "Opened in VS Code"
        main.open_path(folder)
        return "VS Code isn't installed, so the folder opened instead"

    # ---------- running a project on this computer ----------
    # The board's copy is the shared one; these write it to the project's folder, run a command there and
    # stream what it prints back to the page (window.bbTerm), so any language installed here can run.
    def term_start(self, key, title, files, cmd):
        key, cmd = str(key or "")[:80], str(cmd or "").strip()
        if not key or not cmd:
            return {"error": "There's no command to run"}
        old = _TERMS.get(key)
        if old and old.poll() is None:
            return {"error": "Something is still running here. Stop it first."}
        cmd, missing = _check_tools(cmd)
        if missing:
            return {"error": missing}
        folder = _proj_folder(title)
        _write_files(folder, files)
        started = time.time() * 1000
        try:
            p = subprocess.Popen(cmd, shell=True, cwd=folder, env=_child_env(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                 start_new_session=not IS_WIN)
        except OSError as e:
            return {"error": f"Couldn't start it: {e}"}
        _TERMS[key] = p
        threading.Thread(target=_term_pump, args=(key, p), daemon=True).start()
        return {"ok": True, "folder": str(folder), "started": started, "cmd": cmd}

    def term_input(self, key, text):
        p = _TERMS.get(str(key))
        if not p or p.poll() is not None:
            return "not running"
        try:
            p.stdin.write((str(text) + "\n").encode("utf-8"))
            p.stdin.flush()
        except OSError:
            return "closed"
        return "ok"

    def term_stop(self, key):
        p = _TERMS.get(str(key))
        if p and p.poll() is None:
            _kill(p)
        return "ok"

    def proj_pull(self, title, since=0):
        """Text files in the project's folder changed since `since` (ms): what a program wrote or a command made."""
        folder = _proj_folder(title, make=False)
        out = []
        if not folder.is_dir():
            return out
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS and not d.startswith(".")]
            for n in names:
                f = Path(root) / n
                try:
                    st = f.stat()
                    if st.st_size > 400_000 or st.st_mtime * 1000 < float(since or 0):
                        continue
                    data = f.read_bytes()
                    if b"\x00" in data[:8192]:
                        continue
                    out.append({"n": f.relative_to(folder).as_posix(), "t": data.decode("utf-8").replace("\r\n", "\n")})
                except (OSError, UnicodeDecodeError):
                    continue
                if len(out) >= 300:
                    return out
        return out

    def pc_tools(self):
        return {n: bool(shutil.which(n)) for n in ("python", "py", "node", "npm", "java", "javac", "gcc", "g++",
                                                    "dotnet", "go", "cargo", "rustc", "git")}

    # ---------- code in and out of the board ----------
    _SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env", "dist", "build", ".idea",
                  ".vscode", ".next", "target", "bin", "obj"}

    @staticmethod
    def _safe_rel(name):
        rel = Path(str(name or "").replace("\\", "/"))
        return None if not rel.parts or rel.anchor or ".." in rel.parts else rel

    def ol_export(self, name, files):
        """Saves code from the board through a Save dialog: one file as itself, several as a .zip."""
        files = [f for f in list(files or [])[:600] if self._safe_rel(f.get("n"))]
        if not files:
            return {"error": "There's no code in there to save."}
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "code")).strip(" .")[:60] or "code"
        one = len(files) == 1
        fname = Path(files[0]["n"]).name if one else base + ".zip"
        kinds = ("All files (*.*)",) if one else ("Zip files (*.zip)",)
        picked = window.create_file_dialog(webview.SAVE_DIALOG, directory=str(Path.home() / "Documents"),
                                           save_filename=fname, file_types=kinds)
        if not picked:
            return {"cancelled": True}
        target = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        try:
            if one:
                target.write_text(str(files[0].get("t", "")), encoding="utf-8")
            else:
                if target.suffix.lower() != ".zip":
                    target = target.with_suffix(".zip")
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in files:
                        z.writestr(f"{base}/{self._safe_rel(f['n']).as_posix()}", str(f.get("t", "")))
        except OSError as e:
            return {"error": f"Couldn't save it: {e}"}
        return {"saved": str(target), "msg": f"Saved {target.name}"}

    def ol_import(self, how="files"):
        """Picks code files, a folder or a .zip from this computer and hands back their text.
        Pictures, build folders, binaries and anything over 400 KB are left out."""
        if how == "folder":
            picked = window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(Path.home() / "Documents"))
        else:
            picked = window.create_file_dialog(webview.OPEN_DIALOG, directory=str(Path.home() / "Documents"),
                                               allow_multiple=True)
        if not picked:
            return {"cancelled": True}
        picked = [Path(p) for p in (picked if isinstance(picked, (list, tuple)) else [picked])]
        out, skipped, size = [], 0, 0

        def take(rel, data):
            nonlocal skipped, size
            rel = self._safe_rel(rel)
            if (not rel or any(p in self._SKIP_DIRS for p in rel.parts[:-1]) or len(data) > 400_000
                    or size + len(data) > 3_000_000 or len(out) >= 300 or b"\x00" in data[:4096]):
                skipped += 1
                return
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            size += len(data)
            out.append({"n": rel.as_posix(), "t": text})

        name = picked[0].stem if len(picked) == 1 else "Imported code"
        for p in picked:
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and not (set(f.relative_to(p).parts[:-1]) & self._SKIP_DIRS):
                        take(f.relative_to(p).as_posix(), f.read_bytes() if f.stat().st_size <= 400_000 else b"\x00")
                    if len(out) >= 300:
                        break
            elif p.suffix.lower() == ".zip":
                with zipfile.ZipFile(p) as z:
                    names = [i for i in z.infolist() if not i.is_dir()]
                    # a zip that holds one folder: drop that folder from the names
                    tops = {i.filename.split("/")[0] for i in names}
                    cut = len(next(iter(tops))) + 1 if len(tops) == 1 and all("/" in i.filename for i in names) else 0
                    for i in names:
                        take(i.filename[cut:], z.read(i) if i.file_size <= 400_000 else b"\x00")
            elif p.is_file():
                take(p.name, p.read_bytes() if p.stat().st_size <= 400_000 else b"\x00")
        return {"name": name, "files": out, "skipped": skipped}

    def ol_local_load(self, board="main"):
        return main.load_json(_board_file(board), [])

    def ol_boards(self):
        return _boards()

    def ol_board_new(self, name):
        name = str(name or "").strip()[:60] or "New board"
        bid = uuid.uuid4().hex[:12]
        _write_safely(_board_file(bid), [])
        _boards_save(_boards() + [{"id": bid, "name": name}])
        return {"id": bid, "name": name}

    def ol_board_rename(self, board, name):
        name = str(name or "").strip()[:60]
        if name:
            _boards_save([{**b, "name": name} if b["id"] == board else b for b in _boards()])
        return _boards()

    def ol_board_remove(self, board):
        """Takes a board off the list. Its file goes in with the backups rather than being deleted."""
        if board == "main":
            return {"msg": "The first board stays"}
        f = _board_file(board)
        if f.exists():
            folder = main.DATA_DIR / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(folder / f"removed-board-{board}-{datetime.now():%Y%m%d-%H%M%S}.json"))
        _boards_save([b for b in _boards() if b["id"] != board])
        return {"msg": "Removed. A copy is kept in the backups folder"}

    def ol_local_save(self, rows, board="main"):
        if isinstance(rows, list):
            f = _board_file(board)
            # A board that suddenly loses most of its blocks is far more likely to be a page that
            # loaded too early than a real clear-out, so keep what was there first.
            old = main.load_json(f, [])
            if isinstance(old, list) and len(old) >= 4 and len(rows) < len(old) / 2:
                folder = main.DATA_DIR / "backups"
                folder.mkdir(parents=True, exist_ok=True)
                _write_safely(folder / f"offline-{f.stem}-before-shrink-{datetime.now():%Y%m%d-%H%M%S}.json", old)
                main.log_quiet(f"board went from {len(old)} blocks to {len(rows)}: kept a copy of the bigger one")
            _write_safely(f, rows)
        return "ok"

    def backups_open(self):
        """Opens the folder with the automatic copies of everything."""
        backup_all("asked")
        BACKUPS.mkdir(parents=True, exist_ok=True)
        main.open_path(BACKUPS)
        return "ok"

    def backup_now(self):
        made = backup_all("asked", force=True)
        return {"msg": f"Backed up to {made.name}" if made else "Couldn't make a backup"}

    def ol_local_saved_at(self, board="main"):
        """When the offline board was last written, in milliseconds, or 0 if it never was."""
        f = _board_file(board)
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
        main.open_path(folder)
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
        main.open_path(notebook.ROOT)

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
        if not IS_WIN:
            main.open_path(f"https://github.com/{main.UPDATE_REPO}/releases/latest")
            return "The download page is open in your browser. Unzip it and drag the new app into Applications."
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
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=NO_WINDOW | DETACHED)
        threading.Timer(0.5, window.destroy).start()
        return "ok"

    def study_save(self, state):
        """Your flashcard progress, focus minutes and daily goal (data/study.json)."""
        if not isinstance(state, dict) or len(json.dumps(state)) > 3_000_000:
            return False
        main.save_json(main.DATA_DIR / "study.json", state)
        return True

    # ---------- sharing ----------
    def share_status(self):
        sh, f = main.settings().get("share"), main.following()
        return {"share": {k: sh.get(k) for k in ("code", "updated", "size")} if sh else None,
                "follow": {k: f.get(k) for k in ("code", "name", "updated", "synced")} if f else None,
                "has_key": main.signed_in(), "has_sync": main.SNAPSHOT.exists()}

    def share_start(self):
        if main.settings().get("share"):
            return self.share_status()
        if not main.SNAPSHOT.exists():
            return {"error": "Sync with Blackboard once first, so there is something to share."}
        key = secrets.token_urlsafe(32)
        try:
            code = _rpc("share_create", p_secret=key, p_name=os.getenv("USER_NAME") or "A classmate")
            _set("share", {"code": code, "secret": key})
            share_push()
        except ShareError as e:
            return {"error": str(e)}
        return self.share_status()

    def share_now(self):
        try:
            share_push()
        except ShareError as e:
            return {"error": str(e)}
        return self.share_status()

    def share_stop(self):
        sh = main.settings().get("share")
        if sh:
            try:
                _rpc("share_stop", p_code=sh["code"], p_secret=sh["secret"])
            except ShareError as e:
                return {"error": str(e)}
        _set("share", None)
        return self.share_status()

    def follow(self, code):
        code = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
        if len(code) != 8:
            return {"error": "A share code is 8 letters and numbers, like ABCD-2345."}
        if (main.settings().get("share") or {}).get("code") == code:
            return {"error": "That's your own code. Give it to classmates instead."}
        if STATE["busy"]:
            return {"error": "Busy. Try again in a minute."}
        old = main.load_json(main.SETTINGS, {}).get("follow")
        _set("follow", {"code": code})
        STATE["busy"] = True
        try:
            follow_pull(force=True)
        except ShareError as e:
            _set("follow", old)
            msg = str(e)
            return {"error": "No one is sharing with that code. Check it, or ask them for it again." if "stopped sharing" in msg else msg}
        except Exception as e:
            _set("follow", old)
            main.log(f"Follow failed: {e!r}")
            return {"error": "Couldn't get their notes. Try again."}
        finally:
            STATE["busy"] = False
        return {"ok": True, **self.share_status()}

    def unfollow(self):
        _set("follow", None)
        try:
            main.run(sync=False, use_ai=False) if main.signed_in() and main.SNAPSHOT.exists() else main.build_lite()
        except Exception as e:
            main.log_quiet(f"rebuild after unfollow failed: {e!r}")
        return {"ok": True, **self.share_status()}

    def follow_now(self):
        return start_sync()

    # ---------- uninstall ----------
    def uninstall_info(self):
        """What uninstalling could remove, with sizes, and whether this copy can be uninstalled at all."""
        out = []
        for key, paths in _uninstall_parts().items():
            out.append({"key": key, "size": sum(_size(x) for x in paths),
                        "where": str(paths[0]) if paths else "", "found": any(x.exists() for x in paths)})
        why = _uninstall_blocked()
        return {"parts": out, "blocked": why, "custom_materials": main.MATERIALS != main.LOCAL / "materials"}

    def uninstall(self, choices, backup_first=True):
        """Sends the chosen parts to the Recycle Bin once the app has closed. The app files always go."""
        why = _uninstall_blocked()
        if why:
            return {"error": why}
        if STATE["busy"]:
            return {"error": "Busy syncing. Try again in a minute."}
        parts = _uninstall_parts()
        keep = set(choices or []) & set(parts)
        keep.add("app")
        saved = ""
        if backup_first:
            made = backup_all("uninstall", force=True)
            if not made:
                return {"error": "Couldn't make the final backup, so nothing was removed."}
            dest = _desktop() / made.name.replace("Blackboard Buddy ", "Blackboard Buddy final backup ")
            try:
                shutil.copy2(made, dest)
                saved = str(dest)
            except OSError as e:
                return {"error": f"Couldn't put the backup on your Desktop ({e.strerror}), so nothing was removed."}
        paths = [x for k in parts if k in keep for x in parts[k] if x.exists()]
        for x in paths:     # never a whole user folder, a drive, or the app's own LocalAppData folder
            x = x.resolve()
            if x == Path(x.anchor) or x in {main.LOCAL.resolve(), Path.home().resolve(), _desktop().resolve(), _proj_folder('x', make=False).parent.parent.resolve()}:
                return {"error": f"Refused to remove {x}. Nothing was removed."}
        app_dir = Path(sys.executable).parent
        ps1 = Path(tempfile.gettempdir()) / f"bb-uninstall-{os.getpid()}.ps1"
        q = lambda x: "'" + str(x).replace("'", "''") + "'"
        ps1.write_text(f"""$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName Microsoft.VisualBasic, System.Windows.Forms
while (Get-Process -Id {os.getpid()}) {{ Start-Sleep -Milliseconds 500 }}
Start-Sleep -Seconds 2
$left = @()
foreach ($p in @({", ".join(q(x) for x in paths)})) {{
  for ($i = 0; $i -lt 10 -and (Test-Path -LiteralPath $p); $i++) {{
    if (Test-Path -LiteralPath $p -PathType Container) {{
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, 'OnlyErrorDialogs', 'SendToRecycleBin')
    }} else {{
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin')
    }}
    if (Test-Path -LiteralPath $p) {{ Start-Sleep -Seconds 1 }}
  }}
  if (Test-Path -LiteralPath $p) {{ $left += $p }}
}}
foreach ($d in @({q(app_dir)}, {q(main.LOCAL)})) {{
  if ((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force)) {{ Remove-Item -LiteralPath $d }}
}}
$msg = "Blackboard Buddy has been uninstalled. What was removed is in the Recycle Bin, in case you need it back."
if ({q(saved)}) {{ $msg += "`n`nYour final backup is on the Desktop:`n" + {q(saved)} }}
if ($left) {{ $msg += "`n`nThese couldn't be removed (maybe still open somewhere):`n" + ($left -join "`n") }}
[System.Windows.Forms.MessageBox]::Show($msg, 'Blackboard Buddy') | Out-Null
Remove-Item -LiteralPath $PSCommandPath
""", encoding="utf-8-sig")
        main.log_quiet(f"uninstall: {sorted(keep)}")
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(ps1)],
                         creationflags=NO_WINDOW | DETACHED)
        threading.Timer(0.8, window.destroy).start()
        return {"ok": True, "saved": saved}

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
            # Only if the button is still down when Windows gets to it: a move started after the button
            # is up never ends, and the window then follows the mouse around until the next click.
            def go():
                btn = 0x02 if windll.user32.GetSystemMetrics(23) else 0x01     # left/right swapped?
                if windll.user32.GetAsyncKeyState(btn) & 0x8000:
                    windll.user32.DefWindowProcW(hwnd, 0x0112, 0xF012, 0)
            form.BeginInvoke(Action(go))
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

    def set_name(self, name):
        name = " ".join((name or "").split())[:30]
        if not name:
            return "Enter a name"
        main.set_env("USER_NAME", name)
        return "ok"

    def profile_save(self, p):
        """The profile card from Settings: kept in data/, so the backups carry it too."""
        p = p if isinstance(p, dict) else {}
        clip = lambda k, n: " ".join(str(p.get(k) or "").split())[:n]
        name = clip("name", 30)
        if not name:
            return {"msg": "Enter a name"}
        accent = str(p.get("accent") or "")
        clean = {"name": name, "tag": clip("tag", 60), "course": clip("course", 60), "avatar": clip("avatar", 4),
                 "accent": accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else "#d71921",
                 "banner": clip("banner", 20), "pattern": clip("pattern", 20),
                 "shape": p.get("shape") if p.get("shape") in ("round", "square", "hex") else "round"}
        pic = lambda k, n: v if isinstance(v := p.get(k), str) and len(v) <= n and re.fullmatch(
            r"data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+", v) else ""
        hexc = lambda k, d: v if re.fullmatch(r"#[0-9a-fA-F]{6}", v := str(p.get(k) or "")) else d
        badges = p.get("badges") if isinstance(p.get("badges"), list) else []
        clean.update(avatarImg=pic("avatarImg", 300_000), bannerImg=pic("bannerImg", 700_000),
                     g1=hexc("g1", "#7f5af0"), g2=hexc("g2", "#2cb67d"),
                     font=p.get("font") if p.get("font") in ("dot", "sans", "serif", "mono", "round") else "dot",
                     effect=p.get("effect") if p.get("effect") in ("none", "glow", "shimmer", "ring", "float") else "none",
                     badges=[" ".join(str(x).split())[:18] for x in badges[:4] if str(x).strip()])
        main.set_env("USER_NAME", name)
        main.save_json(main.DATA_DIR / "profile.json", clean)
        if not STATE["busy"]:       # a sync that's running builds the page with it anyway
            try:
                main.run(sync=False, use_ai=False)
            except Exception as e:
                main.log_quiet(f"profile_save rebuild failed: {e!r}")
        return {"ok": True, "profile": clean}

    def backups_list(self):
        BACKUPS.mkdir(parents=True, exist_ok=True)
        out = []
        for f in sorted(BACKUPS.glob("Blackboard Buddy *.zip"), reverse=True)[:40]:
            st = f.stat()
            out.append({"file": f.name, "when": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes"), "size": st.st_size})
        return out

    def backup_restore(self, file):
        """Puts a backup back. What is there now is zipped first, and files the backup doesn't have are left alone."""
        f = BACKUPS / Path(str(file or "")).name
        if not f.is_file() or not re.fullmatch(r"Blackboard Buddy [\d -]+\.zip", f.name):
            return {"msg": "Couldn't find that backup"}
        if STATE["busy"]:
            return {"msg": "Busy checking Blackboard. Try again in a minute."}
        if not backup_all("before loading a backup", force=True):
            return {"msg": "Couldn't back up what you have now, so nothing was changed"}
        roots, n = {"data": main.DATA_DIR, "Notebook": notebook.ROOT}, 0
        try:
            with zipfile.ZipFile(f) as z:
                for i in z.infolist():
                    top, _, rest = i.filename.partition("/")
                    root = roots.get(top)
                    if i.is_dir() or not root or not rest:
                        continue
                    dest = (root / rest).resolve()
                    if not dest.is_relative_to(root.resolve()):
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(i))
                    n += 1
        except (OSError, zipfile.BadZipFile) as e:
            main.log_quiet(f"loading {f.name} failed: {e!r}")
            return {"msg": "That backup couldn't be read. A copy of what you had was kept"}
        main.log_quiet(f"loaded backup {f.name}: {n} files")
        try:
            main.run(sync=False, use_ai=False) if main.signed_in() else main.build_lite()
        except Exception as e:
            main.log_quiet(f"rebuild after loading a backup failed: {e!r}")
        return {"ok": True, "msg": f"Loaded {n} files", "count": n}

    def unlock(self, key, url):
        """From the locked app: check and save the Claude key and the Blackboard address, then the first check."""
        u = urlparse((url or "").strip())
        if u.scheme != "https" or not u.netloc:
            return "Enter the full Blackboard address, starting with https://"
        r = self.account_add("My Claude account", key)
        if not r.get("ok"):
            return r.get("error") or "That key didn't work"
        main.BB_URL = f"https://{u.netloc}"
        main.set_env("BB_URL", main.BB_URL)
        start_sync()
        return "ok"

    def setup(self, key, url, name="", lite=False):
        """First run: save the name, Blackboard address and the Claude key, then sync. Without a key
        (lite) it opens straight onto Online and Notebook, and the rest unlocks once a key is added."""
        name = " ".join((name or "").split())[:30]
        if not os.getenv("USER_NAME"):
            if not name:
                return "Enter your name"
            main.set_env("USER_NAME", name)
        if lite:
            path = main.build_lite()
            threading.Thread(target=lambda: (time.sleep(.3), window.load_url(path.as_uri())), daemon=True).start()
            return "lite"
        if not main.signed_in() and not (key or "").strip():
            return "Enter your Claude API key, or go back and pick No"
        if url is not None and not os.getenv("BB_URL"):
            u = urlparse((url or "").strip())
            if u.scheme != "https" or not u.netloc:
                return "Enter the full address, starting with https://"
            main.BB_URL = f"https://{u.netloc}"
            main.set_env("BB_URL", main.BB_URL)
        if (key or "").strip():
            r = self.account_add("My Claude account", key)
            if not r.get("ok"):
                return r.get("error") or "That key didn't work"
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
.two{{display:flex;gap:10px}} .two .btn{{flex:1}} .btn.alt{{background:transparent;color:var(--text);border:1px solid var(--line)}}
.dots{{display:flex;gap:8px;justify-content:center;margin:6px 0 18px}} .dots i{{width:9px;height:9px;border-radius:50%;background:var(--text);animation:blink 1.2s infinite}}
.dots i:nth-child(2){{animation-delay:.2s}} .dots i:nth-child(3){{animation-delay:.4s}} @keyframes blink{{0%,80%,100%{{opacity:.2}}40%{{opacity:1}}}}
#load{{display:{'none' if form else 'block'};text-align:center}}
</style></head><body><div class="card">
<div id="ask" style="display:{'block' if need_key else 'none'}"><h1>Blackboard Buddy</h1>
 <p class="sub">Your week, deadlines, files and notes from Blackboard, in one app.</p>
 <div style="display:{'block' if need_name else 'none'}"><label for="n0">Your name</label>
 <input id="n0" autocomplete="off" spellcheck="false" maxlength="30" placeholder="What should the app call you?"></div>
 <label>Do you have a Claude API key?</label>
 <small>The week, deadlines, notes and Ask Claude use one. Without it you still get Online (group boards and code) and the Notebook, and you can add a key later to unlock the rest.</small>
 <div class="two"><button class="btn alt" onclick="noKey()">No, not yet</button><button class="btn" onclick="hasKey()">Yes</button></div>
 <p id="err0" class="err"></p></div>
<div id="form" style="display:{'block' if form and not need_key else 'none'}"><h1>Blackboard Buddy</h1>
 <p class="sub">Your week, deadlines, files and notes from Blackboard, in one app.</p>
 <div style="display:{'block' if need_name else 'none'}"><label for="n">Your name</label>
 <input id="n" autocomplete="off" spellcheck="false" maxlength="30" placeholder="What should the app call you?"></div>
 <div style="display:{'block' if need_url else 'none'}"><label for="u">Your Blackboard address</label>
 <input id="u" value="{url}" autocomplete="off" spellcheck="false">
 <small>The address you use to open Blackboard, e.g. https://blackboard.uwe.ac.uk</small></div>
 <div style="display:{'block' if need_key else 'none'}"><label for="k">Claude API key</label>
 <input id="k" type="password" placeholder="sk-ant-…" autocomplete="off">
 <small>Get one at platform.claude.com → API keys.</small></div>
 <p id="err" class="err"></p><button class="btn" onclick="save()">Continue</button></div>
<div id="load"><div class="dots"><i></i><i></i><i></i></div><h1 style="font-size:24px">Getting your week ready</h1>
 <p class="sub" id="msg">A Blackboard window will open. Sign in there.</p></div>
</div><script>
function loading(){{document.getElementById('form').style.display='none';document.getElementById('load').style.display='block'}}
const $id=i=>document.getElementById(i);
function hasKey(){{$id('n').value=$id('n0').value;$id('ask').style.display='none';$id('form').style.display='block';
 ($id('n').offsetParent?$id('n'):$id('k')).focus()}}
async function noKey(){{const r=await pywebview.api.setup('',null,$id('n0').value,true);
 if(r!=='lite')$id('err0').textContent=r;else{{$id('ask').innerHTML='<h1 style="font-size:24px">Opening…</h1>'}}}}
async function save(){{const r=await pywebview.api.setup(document.getElementById('k').value,document.getElementById('u').value,document.getElementById('n').value);
 if(r==='ok')loading();else document.getElementById('err').textContent=r}}
document.addEventListener('keydown',e=>{{if(e.key==='Enter'&&$id('form').style.display!=='none')save()}});
window.bbStatus=(m)=>{{document.getElementById('msg').textContent=m}};
</script></body></html>"""


# ---------- sharing your notes with a code ----------
class ShareError(Exception):
    pass


def _rpc(name, **args):
    """Calls one of the share functions on the group server. No sign-in needed: the code (and, to
    change a share, its key) is the permission."""
    cfg = online.config(main.settings())
    if not cfg:
        raise ShareError("No group server is set up.")
    req = urllib.request.Request(f"{cfg['url']}/rest/v1/rpc/{name}", data=json.dumps(args).encode(), method="POST",
                                 headers={"apikey": cfg["key"], "Authorization": f"Bearer {cfg['key']}",
                                          "Content-Type": "application/json", "User-Agent": "BlackboardBuddy"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        main.log_quiet(f"share {name} failed: {e.code} {body[:300]}")
        if "PGRST202" in body or "Could not find the function" in body:
            raise ShareError('Sharing needs a one-off update to the group server: open "Online setup.sql" from the app folder, '
                             "paste all of it into Supabase > SQL Editor and press Run.")
        if "not yours" in body:
            raise ShareError("This share code isn't yours any more. Stop sharing and start again for a new code.")
        if "too big" in body:
            raise ShareError("Your notes are too big to share in one go.")
        raise ShareError("The group server said no. Try again in a minute.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ShareError("Couldn't reach the group server. Are you online?")


def _set(key, value):
    cur = main.load_json(main.SETTINGS, {})
    if value is None:
        cur.pop(key, None)
    else:
        cur[key] = value
    main.save_json(main.SETTINGS, cur)


def share_push():
    """Sends your latest sync to everyone following your code."""
    sh = main.settings().get("share")
    if not sh:
        return None
    payload = main.share_payload()
    if not payload:
        raise ShareError("Sync with Blackboard once first, so there is something to share.")
    blob = base64.b64encode(gzip.compress(json.dumps(payload, default=str).encode("utf-8"))).decode()
    when = _rpc("share_put", p_code=sh["code"], p_secret=sh["secret"], p_name=payload["from"] or "A classmate", p_data=blob)
    _set("share", {**sh, "updated": when, "size": len(blob)})
    main.log_quiet(f"shared notes sent: {len(blob) // 1024} KB")
    return when


def follow_pull(force=False):
    """Fetches the notes of whoever you follow if they've changed, and remakes the page. Returns the new
    page's path, or None when nothing changed."""
    f = main.following()
    if not f:
        return None
    when = _rpc("share_when", p_code=f["code"])
    if not when:
        raise ShareError(f"{f.get('name') or 'They'} stopped sharing. Stop following in Settings › Share to use your own sync.")
    if when == f.get("updated") and not force and (main.SHARED / "snapshot.json").exists():
        return None
    rows = _rpc("share_get", p_code=f["code"]) or []
    if not rows or not rows[0].get("data"):
        raise ShareError("Nothing has been shared on that code yet. Ask them to sync once.")
    row = rows[0]
    try:
        data = json.loads(gzip.decompress(base64.b64decode(row["data"])).decode("utf-8"))
        assert data.get("v") == 1 and isinstance(data["snapshot"]["courses"], list) and isinstance(data["ai"], dict)
    except Exception:
        raise ShareError("The shared notes couldn't be read. Ask them to sync again.")
    data = main.defang_share(data)
    main.SHARED.mkdir(parents=True, exist_ok=True)
    main.save_json(main.SHARED / "snapshot.json", data["snapshot"])
    main.save_json(main.SHARED / "ai-latest.json", data["ai"])
    _set("follow", {"code": f["code"], "name": (row.get("name") or "A classmate")[:40], "updated": row["updated_at"],
                    "synced": data.get("synced")})
    return main.run(sync=False, use_ai=False)


def follow_worker():
    f = main.following() or {}
    try:
        push(f"Getting {f.get('name') or 'their'}'s latest notes…")
        path = follow_pull()
        STATE["busy"] = False
        push("Up to date", False)
        if path:
            reload(path)
    except ShareError as e:
        push(str(e), False)
    except Exception as e:
        main.log(f"Follow update failed: {e!r}")
        push("Couldn't get their latest notes. Try ↻ again.", False)
    finally:
        STATE["busy"] = False


def follow_loop():
    """Keeps a follower's app in step with the person they follow: shortly after start, then every 15 minutes."""
    time.sleep(25)
    while True:
        if main.following() and not STATE["busy"]:
            start_sync(interactive=False)
        time.sleep(15 * 60)


# ---------- uninstall ----------
def _uninstall_blocked():
    """Why this copy can't be uninstalled from inside the app, or '' when it can."""
    if not getattr(sys, "frozen", False):
        return "Uninstall only works in the installed app."
    if not IS_WIN:
        return ("On a Mac, quit the app and drag Blackboard Buddy from Applications to the Bin. "
                f"Your data stays in {main.LOCAL} until you remove that folder too.")
    app_dir = Path(sys.executable).parent
    if (app_dir / ".git").exists() or any(app_dir.glob("*.py")):
        return "This is the developer copy (it sits next to the source code), so uninstall is switched off here."
    return ""


def _uninstall_parts():
    """Each choice in the uninstall window, and the files and folders it stands for."""
    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else main.HERE
    mine = {main.ENV, main.DATA_DIR, notebook.ROOT}
    app = [x for x in app_dir.iterdir() if x not in mine] if app_dir.is_dir() else []
    if main.OUT_DIR not in app:
        app.append(main.OUT_DIR)
    app += [main.LOCAL / "run.log"]
    return {
        "app": app,
        "settings": [main.ENV, main.SETTINGS, main.ACCOUNTS],
        "data": [main.DATA_DIR],
        "notebook": [notebook.ROOT],
        "materials": [main.MATERIALS],
        "backups": [BACKUPS],
        "browser": [main.LOCAL / "webview", main.PROFILE_DIR],
        "projects": [_proj_folder("x", make=False).parent],
    }


def _size(path):
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def _desktop():
    if not IS_WIN:
        return Path.home() / "Desktop"
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)  # the real Desktop, even when OneDrive moved it
    return Path(buf.value or Path.home() / "Desktop")


# ---------- projects on this computer ----------
_TERMS = {}     # block id -> the process running in its folder


def _proj_folder(title, make=True):
    if IS_WIN:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # the real Documents folder, even when OneDrive moved it
        docs = Path(buf.value or Path.home() / "Documents")
    else:
        docs = Path.home() / "Documents"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or "Project")).strip(" .")[:60] or "Project"
    folder = docs / "Blackboard Buddy projects" / name
    if make:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write_files(folder, files):
    for f in list(files or [])[:300]:
        rel = Path(str(f.get("n", "")).replace("\\", "/"))
        if not rel.parts or rel.anchor or ".." in rel.parts:  # anchor covers C:/x, /x and C:x
            continue
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(f.get("t", "")), encoding="utf-8")


def _child_env():
    """What a project's program sees: the usual environment, minus anything of the app's own (keys above all)."""
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|ANTHROPIC|SUPABASE|^BB_|^_MEI|^_PYI", k, re.I)}
    for k in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env


_TOOL_HELP = {"python3": ("Python", "python.org/downloads"), "python": ("Python", "python.org/downloads"), "py": ("Python", "python.org/downloads"),
              "node": ("Node.js", "nodejs.org"), "npm": ("Node.js", "nodejs.org"), "npx": ("Node.js", "nodejs.org"),
              "java": ("Java", "adoptium.net"), "javac": ("Java (the JDK)", "adoptium.net"),
              "gcc": ("a C compiler", "winlibs.com"), "g++": ("a C++ compiler", "winlibs.com"),
              "dotnet": (".NET", "dot.net"), "go": ("Go", "go.dev/dl"), "cargo": ("Rust", "rustup.rs"),
              "rustc": ("Rust", "rustup.rs"), "git": ("Git", "git-scm.com"), "ruby": ("Ruby", "rubyinstaller.org"),
              "php": ("PHP", "php.net")}


def _check_tools(cmd):
    """Says which tool isn't installed, before cmd.exe gives its own puzzling message. Prefers the py launcher,
    which is there with every python.org install, over a bare `python` that may only be the Store's stand-in."""
    if re.match(r"\s*python(\.exe)?\s", cmd + " ", re.I) and shutil.which("py"):
        cmd = re.sub(r"^\s*python(\.exe)?", "py", cmd, count=1, flags=re.I)
    elif not IS_WIN and re.match(r"\s*python\s", cmd + " ") and not shutil.which("python") and shutil.which("python3"):
        cmd = re.sub(r"^\s*python", "python3", cmd, count=1)      # a Mac has python3 but no python
    for part in re.split(r"&&|\|\||[|&]", cmd):
        word = part.strip().strip('"').split(" ")[0].lower().removesuffix(".exe")
        if word in _TOOL_HELP and not shutil.which(word):
            what, where = _TOOL_HELP[word]
            return cmd, f"{what} isn't on this computer yet. Install it from {where}, then try again."
    return cmd, None


def _kill(p):
    if not IS_WIN:
        try:
            os.killpg(p.pid, signal.SIGKILL)     # the shell and whatever it started
        except OSError:
            p.kill()
        return
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                   creationflags=NO_WINDOW)


def _term_pump(key, p):
    def send(text=None, code=None):
        try:
            window.evaluate_js(f"window.bbTerm && bbTerm({json.dumps(key)}, {json.dumps(text)}, {json.dumps(code)})")
        except Exception:
            pass
    dec, shown = codecs.getincrementaldecoder("utf-8")("replace"), 0
    while True:
        chunk = p.stdout.read1(65536)
        if not chunk:
            break
        shown += len(chunk)
        if shown <= 2_000_000:
            send(dec.decode(chunk))
        elif shown - len(chunk) <= 2_000_000:
            send("\n[That's a lot of output: the rest isn't shown]\n")
    rest = dec.decode(b"", final=True)
    if rest:
        send(rest)
    send(None, p.wait())


atexit.register(lambda: [_kill(p) for p in _TERMS.values() if p.poll() is None])


# ---------- copies of everything you make ----------
# Your notebook, the offline board, the copies of group boards, settings and the AI's notes, zipped
# every so often into a folder outside the app's own. Updating or rebuilding the app never touches it.
BACKUPS = main.LOCAL / "Backups"
KEEP_BACKUPS = 60
_backup_lock = threading.Lock()


def _backup_sources():
    skip = {"run.log", "snapshot.json"}          # the log, and what the next sync fetches again anyway
    for root, label in ((main.DATA_DIR, "data"), (notebook.ROOT, "Notebook")):
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file() and f.name not in skip and f.stat().st_size < 50_000_000:
                    yield f, f"{label}/{f.relative_to(root).as_posix()}"


def backup_all(reason="timer", force=False):
    """Zips everything worth keeping, unless nothing has changed since the last copy. Returns the zip."""
    with _backup_lock:
        try:
            files = list(_backup_sources())
            mark = f"{len(files)}:{sum(f.stat().st_size for f, _ in files)}:{max((f.stat().st_mtime for f, _ in files), default=0):.0f}"
            BACKUPS.mkdir(parents=True, exist_ok=True)
            stamp = BACKUPS / ".last"
            if not force and stamp.exists() and stamp.read_text() == mark:
                return None
            target = BACKUPS / f"Blackboard Buddy {datetime.now():%Y-%m-%d %H%M%S}.zip"
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for f, name in files:
                    try:
                        z.write(f, name)
                    except OSError:
                        pass
            stamp.write_text(mark)
            old = sorted(BACKUPS.glob("Blackboard Buddy *.zip"))
            for f in old[:-KEEP_BACKUPS]:
                f.unlink(missing_ok=True)
            main.log_quiet(f"backup ({reason}): {target.name}, {len(files)} files")
            return target
        except Exception as e:
            main.log_quiet(f"backup failed: {e!r}")
            return None


def backup_loop():
    backup_all("start")
    while True:
        time.sleep(15 * 60)
        backup_all()


# The group board lives on Supabase, not on anyone's computer, so a group carries on whoever has the app open.
# A free Supabase project goes to sleep after about a week with no visits, though, and then nobody can get in
# until it is woken by hand. Every copy of the app looks in once at start and once a day, which keeps it awake
# for as long as anyone in any group uses the app. It reads nothing: without a sign-in the answer is empty.
def online_keepalive():
    while True:
        try:
            cfg = online.config(main.settings())
            if cfg:
                req = urllib.request.Request(cfg["url"] + "/rest/v1/groups?select=id&limit=1",
                                             headers={"apikey": cfg["key"], "Authorization": "Bearer " + cfg["key"]})
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        r.read(200)
                except urllib.error.HTTPError:
                    pass      # "permission denied" comes from the database itself, which is the visit that counts
        except Exception as e:
            main.log_quiet(f"group server check-in failed: {e!r}")
        time.sleep(24 * 3600)


def on_start():
    _restore_resizing()
    threading.Thread(target=backup_loop, daemon=True).start()
    threading.Thread(target=online_keepalive, daemon=True).start()
    main.adopt_env_key()
    has_dashboard = (main.OUT_DIR / "dashboard.html").exists()
    threading.Thread(target=background_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=follow_loop, daemon=True).start()
    if not has_dashboard and (not os.getenv("ANTHROPIC_API_KEY") or not os.getenv("BB_URL") or not os.getenv("USER_NAME")):
        return  # the welcome page asks for the Blackboard address and key, then starts the sync
    if not main.signed_in():
        return  # no Claude key: Online and Notebook only until one is added
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
    if not IS_WIN:
        return
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
    if not IS_WIN or not lock.exists():
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
            _write_safely(_board_file(got.get("board") or "main"), rows)
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
    if not IS_WIN:
        return True        # macOS itself only ever opens one copy of an app
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
    dashboard = main.OUT_DIR / "dashboard.html"
    card = main.DATA_DIR / "profile.json"
    if dashboard.exists() and card.exists() and card.stat().st_mtime > dashboard.stat().st_mtime:
        try:
            main.run(sync=False, use_ai=False)
            main.log_quiet("launch: remade the page for a newer profile card")
        except Exception as e:
            main.log_quiet(f"launch: couldn't remake the page for the profile card: {e!r}")
    main.LISTENERS.append(on_log)
    if dashboard.exists():
        window = webview.create_window("Blackboard Buddy", url=dashboard.as_uri(), js_api=Api(),
                                       width=1180, height=860, min_size=(420, 560), background_color=start_colour(),
                                       frameless=IS_WIN, easy_drag=False)
    else:
        window = webview.create_window("Blackboard Buddy", html=welcome_page(not os.getenv("ANTHROPIC_API_KEY"), not os.getenv("BB_URL"), not os.getenv("USER_NAME")),
                                       js_api=Api(), width=1180, height=860, min_size=(420, 560),
                                       background_color=start_colour(), frameless=IS_WIN, easy_drag=False)
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
