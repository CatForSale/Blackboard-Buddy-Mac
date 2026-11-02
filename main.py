"""Blackboard Buddy: pulls your Blackboard Learn account and turns it into a weekly dashboard.

  python main.py                 sync Blackboard, run AI, open the dashboard
  python main.py --no-ai         sync and build the dashboard without AI
  python main.py --rebuild       rebuild the dashboard from the last sync (no sign-in)
  python main.py ask "question"  ask Claude about your saved Blackboard data
"""

import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown
from dotenv import load_dotenv

import online
import vault as notebook

HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LOCAL = Path(os.getenv("LOCALAPPDATA", HERE)) / "BlackboardBuddy"


def _writable(folder):
    try:
        test = folder / ".bb-write-test"
        test.write_text("ok")
        test.unlink()
        return True
    except OSError:
        return False


# Your key, data and notes live next to the app; if that folder is read-only
# (say the app was opened straight from a zip), they go in your Windows profile instead.
BASE = HERE if _writable(HERE) else LOCAL
BASE.mkdir(parents=True, exist_ok=True)
ENV = BASE / ".env"
load_dotenv(ENV)

BB_URL = os.getenv("BB_URL", "https://blackboard.uwe.ac.uk")
VERSION = "1.1.0"  # bump this for every release you upload
UPDATE_REPO = ""  # GitHub "owner/repo" whose Releases hold new versions; empty = no update checks
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "45"))
NOTES_DAYS = int(os.getenv("NOTES_DAYS", "14"))
NOTES_PER_RUN = int(os.getenv("NOTES_PER_RUN", "8"))
PLANS_PER_RUN = int(os.getenv("PLANS_PER_RUN", "6"))
AI_EFFORT = os.getenv("AI_EFFORT", "medium")

DATA_DIR = BASE / "data"
notebook.ROOT = BASE / "Notebook"
OUT_DIR = BASE / "output"
PROFILE_DIR = LOCAL / "browser-profile"
MATERIALS = LOCAL / "materials"  # downloaded module files; kept out of OneDrive unless you pick a folder in Settings
MATERIALS_NAME = "Blackboard study files"
SNAPSHOT = DATA_DIR / "snapshot.json"
CACHE = DATA_DIR / "ai-cache.json"
SETTINGS = DATA_DIR / "settings.json"
ACTIVITY = DATA_DIR / "activity.json"
SPEND = DATA_DIR / "spend.json"
DONE = DATA_DIR / "done.json"           # assignments you've ticked off yourself, as "course|title"
ACCOUNTS = DATA_DIR / "accounts.json"
DEFAULT_MODEL = "claude-opus-5-5"  # ai.MODELS has the full list; kept as a literal so
#                                    importing main doesn't drag in the anthropic package
READABLE = (".pdf", ".pptx", ".docx", ".txt", ".md")  # files Claude can make notes from

ADMIN_PAGES = re.compile(r"team|introduction to the programme|welcome|contact|module information|timetable|announcement", re.I)  # not worth making study notes from
FILE_HANDLERS = ("resource/x-bb-file", "resource/x-bb-document", "resource/x-bb-folder")


LISTENERS = []  # callbacks that want progress messages (the app window)


def log_quiet(msg):
    """For the run log only - these never belong in the status bar the user sees.
    It is written to the local disk, not the OneDrive folder, where a sync can swallow it."""
    try:
        LOCAL.mkdir(parents=True, exist_ok=True)
        with open(LOCAL / "run.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%d %b %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    if sys.stdout:
        print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    for fn in LISTENERS:
        fn(msg)


def now():
    return datetime.now(timezone.utc)


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def strip_html(text):
    text = re.sub(r"<(br|/p|/div|/li|/h\d)[^>]*>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def safe_name(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip()[:80] or "untitled"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


def set_env(name, value):
    """Saves one setting to .env and applies it straight away."""
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"{name}={value}"
    if re.search(rf"^{name}=.*$", text, flags=re.M):
        text = re.sub(rf"^{name}=.*$", lambda _: line, text, flags=re.M)
    else:
        text = line + "\n" + text
    ENV.write_text(text, encoding="utf-8")
    os.environ[name] = value


def display_name(snapshot):
    """The name typed on the welcome screen, else the first name Blackboard has."""
    return os.getenv("USER_NAME") or snapshot["me"].get("name", "")


def settings():
    return {"sync_hours": 2, "notes_per_sync": NOTES_PER_RUN, "model": DEFAULT_MODEL,
            "effort": AI_EFFORT, "sounds": True, "volume": 35, **load_json(SETTINGS, {})}


MATERIALS = Path(settings().get("materials_dir") or MATERIALS)    # the folder chosen in Settings, if any


# ---------------------------------------------------------------- Claude accounts
# Several API keys can be saved, one of them active. The AI features stay off until one is.

def accounts():
    a = load_json(ACCOUNTS, {})
    return {"active": a.get("active"), "list": a.get("list", [])}


def active_account():
    a = accounts()
    return next((x for x in a["list"] if x.get("id") == a["active"]), None)


def active_key():
    """The key the app should use: the chosen account, else whatever is in .env."""
    acc = active_account()
    return (acc or {}).get("key") or os.getenv("ANTHROPIC_API_KEY") or ""


def signed_in():
    return bool(active_key())


def new_ai(effort=None, model=None):
    """An AI client on the active account, or None when nobody is signed in."""
    key = active_key()
    if not key:
        return None
    from ai import AI
    st = settings()
    return AI(effort=effort or st["effort"], model=model or st["model"], api_key=key)


def adopt_env_key():
    """Before accounts existed the key lived in .env; this turns it into the first saved account."""
    if accounts()["list"]:
        return
    key = os.getenv("ANTHROPIC_API_KEY") or ""
    if not key.startswith("sk-ant-"):
        return
    entry = {"id": uuid.uuid4().hex[:8], "label": "My Claude account", "key": key, "hint": "…" + key[-4:]}
    save_json(ACCOUNTS, {"active": entry["id"], "list": [entry]})


def spend_summary():
    """What Claude has cost, by month, plus this month and the running total."""
    s = load_json(SPEND, {})
    months = sorted(((k, round(float(v), 4)) for k, v in s.items() if isinstance(v, (int, float))), reverse=True)
    month = datetime.now().strftime("%Y-%m")
    return {"month": round(float(s.get(month, 0)), 4), "total": round(sum(v for _, v in months), 4),
            "months": [{"month": k, "cost": v} for k, v in months[:12]]}


def record_activity(kind):
    """One tick on today's square of the activity graph."""
    a = load_json(ACTIVITY, {})
    day = datetime.now().strftime("%Y-%m-%d")
    a[day] = a.get(day, 0) + 1
    save_json(ACTIVITY, a)


def add_spend(cost):
    if cost:
        s = load_json(SPEND, {})
        month = datetime.now().strftime("%Y-%m")
        s[month] = round(s.get(month, 0) + cost, 4)
        save_json(SPEND, s)


# ---------------------------------------------------------------- shaping data

def collect_deadlines(snapshot):
    """Gradebook due dates plus calendar items, de-duplicated, within the window."""
    start, end = now() - timedelta(days=2), now() + timedelta(days=LOOKAHEAD_DAYS)
    names = {c["id"]: c["name"] for c in snapshot["courses"]}
    seen, out = set(), []
    for c in snapshot["courses"]:
        if not c.get("current", True):
            continue
        for col in c["gradebook"]:
            due = parse_date(col.get("due"))
            if due and start <= due <= end:
                seen.add((col["name"].lower(), due.date()))
                out.append({"course": c["name"], "courseId": c["id"], "title": col["name"],
                            "due": due, "columnId": col["id"], "contentId": col.get("contentId"),
                            "submitted": col.get("status") in ("Graded", "NeedsGrading"),
                            "url": c["url"]})
    for item in snapshot["calendar"]:
        due = parse_date(item.get("end") or item.get("start"))
        if not due or not (start <= due <= end) or item.get("type") != "GradebookColumn":
            continue
        if (item["title"].lower(), due.date()) in seen:
            continue
        seen.add((item["title"].lower(), due.date()))
        out.append({"course": names.get(item.get("courseId"), item.get("calendarName", "")),
                    "courseId": item.get("courseId"), "title": item["title"], "due": due,
                    "columnId": None, "contentId": None, "submitted": False, "url": None})
    return sorted(out, key=lambda d: d["due"])


def recent_announcements(snapshot, days=14):
    cutoff = now() - timedelta(days=days)
    out = []
    for c in snapshot["courses"]:
        for a in c["announcements"]:
            d = parse_date(a.get("date"))
            if d and d >= cutoff:
                out.append({"id": a.get("id") or a["title"], "course": c["name"], "title": a["title"], "date": d,
                            "body": strip_html(a["body"]), "html": a["body"]})
    return sorted(out, key=lambda a: a["date"], reverse=True)


def timetable(snapshot, days=7):
    start, end = now() - timedelta(hours=2), now() + timedelta(days=days)
    out = []
    for i in snapshot["calendar"]:
        if i.get("type") == "GradebookColumn":
            continue
        st, en = parse_date(i.get("start")), parse_date(i.get("end"))
        if st and start <= st <= end:
            out.append({"title": i["title"], "module": i.get("calendarName", "").split(":")[0],
                        "start": st, "end": en, "location": i.get("location", "")})
    return sorted(out, key=lambda x: x["start"])


def released_grades(snapshot):
    out = []
    for c in snapshot["courses"]:
        for col in c["gradebook"]:
            if col.get("score") is not None or col.get("display"):
                out.append({"course": c["name"], "title": col["name"], "overall": col.get("overall", False),
                            "score": col.get("score"), "possible": col.get("possible"),
                            "grade": col.get("display"), "feedback": strip_html(col.get("feedback", ""))})
    return out


# ---------------------------------------------------------------- AI

def run_ai(snapshot, bb, deadlines, announcements, grades):
    from ai import AI, file_to_blocks
    import anthropic

    cache = load_json(CACHE, {})
    ai = new_ai() or AI(effort=AI_EFFORT)
    results = {"briefing": None, "plans": [], "notes": [], "feedback": None}
    today = datetime.now().strftime("%A %d %B %Y")
    by_id = {c["id"]: c for c in snapshot["courses"]}

    def cached(key, fn):
        if key not in cache:
            cache[key] = fn()
            save_json(CACHE, cache)
        return cache[key]

    def item_blocks(course_id, item):
        blocks = []
        if strip_html(item.get("body")):
            blocks.append({"type": "text", "text": "Item text:\n" + strip_html(item["body"])[:50_000]})
        for att in bb.attachments(course_id, item["id"]) if bb else []:
            data = bb.download(att["downloadPath"])
            if data:
                blocks += file_to_blocks(att["fileName"], data)
        return blocks

    try:
        brief_in = (
            today,
            [{**d, "due": d["due"].strftime("%a %d %b %Y %H:%M")} for d in deadlines],
            [{k: a[k] for k in ("course", "title", "date", "body")} for a in announcements],
            [g for g in grades][:40],
            [{**t, "start": t["start"].strftime("%a %d %b %H:%M"), "end": t["end"] and t["end"].strftime("%H:%M")}
             for t in timetable(snapshot)])
        # same day and nothing new on Blackboard: reuse the briefing instead of paying again
        # bump v2 when the briefing prompt changes
        bkey = "briefing:v2:" + hashlib.sha1(json.dumps(brief_in, default=str, sort_keys=True).encode()).hexdigest()
        if bkey not in cache:
            log("AI: writing weekly briefing")
        results["briefing"] = cached(bkey, lambda: ai.weekly_briefing(*brief_in))

        open_tasks = [d for d in deadlines if not d["submitted"] and d["due"] > now()]
        for d in open_tasks[:PLANS_PER_RUN]:
            course = by_id.get(d["courseId"], {})
            item = next((i for i in course.get("contents", []) if i["id"] == d.get("contentId")), None)
            key = f"plan:{d['courseId']}:{d['title']}:{d['due'].isoformat()}:{(item or {}).get('modified')}"
            log(f"AI: assignment plan for {d['title']}")
            col = next((c for c in course.get("gradebook", []) if c["id"] == d.get("columnId")), {})
            info = {"title": d["title"], "due": d["due"].strftime("%A %d %B %Y %H:%M"),
                    "points_possible": col.get("possible"),
                    "description": strip_html(col.get("description", "")),
                    "instructions": strip_html((item or {}).get("body", ""))}
            md = cached(key, lambda: ai.assignment_plan(
                course.get("name", d["course"]), info,
                item_blocks(d["courseId"], item) if item else [], today))
            results["plans"].append({"course": d["course"], "title": d["title"], "due": d["due"], "md": md})

        cutoff = now() - timedelta(days=NOTES_DAYS)
        candidates = []
        for c in snapshot["courses"]:
            for i in c["contents"]:
                changed = parse_date(i.get("modified") or i.get("created"))
                if changed and changed >= cutoff and i["handler"] in FILE_HANDLERS \
                        and i["handler"] != "resource/x-bb-folder" and not ADMIN_PAGES.search(i["path"]) \
                        and not re.search(r"\.(csv|xlsx?|zip)$", i["title"], re.I):
                    candidates.append((changed, c, i))
        candidates.sort(key=lambda x: x[0], reverse=True)
        new_count = 0
        for changed, c, i in candidates:
            key = f"notes:{i['id']}:{i.get('modified')}"
            if key not in cache:
                if new_count >= settings()["notes_per_sync"]:
                    continue
                blocks = item_blocks(c["id"], i)
                if not blocks:
                    continue
                log(f"AI: study notes for {i['title']}")
                new_count += 1
                cached(key, lambda: ai.study_notes(c["name"], i["path"], blocks))
            results["notes"].append({"course": c["name"], "title": i["title"], "path": i["path"],
                                     "changed": changed, "md": cache[key]})
        # keep older notes too (including ones made on demand from the Notes tab)
        have = {(n["course"], n["path"]) for n in results["notes"]}
        for c in snapshot["courses"]:
            for i in c["contents"]:
                key = f"notes:{i['id']}:{i.get('modified')}"
                if key in cache and (c["name"], i["path"]) not in have:
                    results["notes"].append({"course": c["name"], "title": i["title"], "path": i["path"],
                                             "changed": parse_date(i.get("modified") or i.get("created")),
                                             "md": cache[key]})

        if grades:
            gkey = "feedback:" + hashlib.sha1(json.dumps(grades, sort_keys=True).encode()).hexdigest()
            log("AI: summarising grades and feedback")
            results["feedback"] = cached(gkey, lambda: ai.grade_feedback(grades))
    except anthropic.AuthenticationError:
        log("AI skipped: your ANTHROPIC_API_KEY in .env is missing or invalid.")
    except anthropic.APIConnectionError:
        log("AI stopped: could not reach the Claude API (check your internet).")
    except anthropic.APIStatusError as e:
        log(f"AI stopped: Claude API error {e.status_code}: {e.message}")

    log(f"AI cost this run: about ${ai.cost:.2f}")
    add_spend(ai.cost)
    note_topics(results["notes"])

    # Notes also go to Markdown files you can open in Obsidian, Notion, Word etc.
    for n in results["notes"]:
        p = OUT_DIR / "notes" / safe_name(n["course"]) / f"{safe_name(n['path'].replace(' / ', ' - '))}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {n['title']}\n\n_{n['path']}_\n\n{n['md']}", encoding="utf-8")
    for pl in results["plans"]:
        p = OUT_DIR / "plans" / f"{safe_name(pl['course'])} - {safe_name(pl['title'])}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {pl['title']}\n\nDue {pl['due']:%A %d %B %Y %H:%M}\n\n{pl['md']}", encoding="utf-8")
    save_json(DATA_DIR / "ai-latest.json", results)
    return results


# ---------------------------------------------------------------- study materials

MATERIAL_HANDLERS = ("resource/x-bb-file", "resource/x-bb-document")

PREVIEW_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>
:root{{--bg:#efefef;--card:#fff;--text:#0b0b0b;--muted:#6a6a6a;--line:rgba(0,0,0,.09)}}
:root[data-theme=dark]{{--bg:#000;--card:#111;--text:#f2f2f2;--muted:#8a8a8a;--line:rgba(255,255,255,.09)}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 "Segoe UI Variable","Segoe UI",system-ui,sans-serif;padding:28px 20px}}
main{{max-width:760px;margin:auto}}
section{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px 24px;margin:0 0 14px}}
.n{{font:11px "Space Mono",Consolas,monospace;color:var(--muted);letter-spacing:.12em}}
h2{{font-size:18px;margin:4px 0 8px}} p{{margin:6px 0}}
</style><script>if(location.hash==='#dark')document.documentElement.dataset.theme='dark'</script></head>
<body><main>{body}</main></body></html>"""


def file_name(name):
    """Like safe_name, but never cuts off the extension."""
    stem, dot, ext = name.rpartition(".")
    return safe_name(stem)[:70] + dot + safe_name(ext)[:8] if dot else safe_name(name)


def preview_page(name, data):
    """A readable HTML version of a Word or PowerPoint file, for the in-app viewer."""
    e = html.escape
    low = name.lower()
    if low.endswith(".pptx"):
        from pptx import Presentation
        parts = []
        for n, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
            texts = [sh.text_frame.text.strip() for sh in slide.shapes
                     if sh.has_text_frame and sh.text_frame.text.strip()]
            if texts:
                parts.append(f'<section><span class="n">SLIDE {n:02d}</span><h2>{e(texts[0])}</h2>'
                             + "".join(f"<p>{e(t)}</p>" for t in texts[1:]).replace("\n", "<br>") + "</section>")
        body = "".join(parts)
    elif low.endswith(".docx"):
        from docx import Document
        parts = []
        for para in Document(io.BytesIO(data)).paragraphs:
            t = para.text.strip()
            if t:
                tag = "h2" if para.style.name.lower().startswith(("heading", "title")) else "p"
                parts.append(f"<{tag}>{e(t)}</{tag}>")
        body = f"<section>{''.join(parts)}</section>" if parts else ""
    else:
        return None
    return PREVIEW_HTML.format(title=e(name), body=body) if body else None


def move_materials(new):
    """Moves the downloaded study files to a folder you chose, fixes the paths the last sync saved
    (so nothing downloads again) and remembers the folder for next time."""
    global MATERIALS
    old, new = MATERIALS.resolve(), Path(new).resolve()
    if new == old:
        return
    if new.is_relative_to(old):
        raise ValueError("pick a folder outside the current study files folder")
    new.mkdir(parents=True, exist_ok=True)
    if old.exists():
        for item in old.iterdir():
            dest = new / item.name
            if item.is_dir() and dest.exists():
                shutil.copytree(item, dest, dirs_exist_ok=True)
                shutil.rmtree(item, ignore_errors=True)
            else:
                shutil.move(str(item), str(dest))
        try:
            old.rmdir()
        except OSError:
            pass
    snap = load_json(SNAPSHOT, None)
    if snap:
        for c in snap.get("courses", []):
            for i in c.get("contents", []):
                for f in i.get("files", []):
                    if f.get("local", "").startswith(str(old)):
                        f["local"] = str(new) + f["local"][len(str(old)):]
        save_json(SNAPSHOT, snap)
    save_json(SETTINGS, {**load_json(SETTINGS, {}), "materials_dir": str(new)})
    MATERIALS = new


def sync_materials(bb, courses, old_snapshot):
    """Saves every file attached to module content into MATERIALS, downloading only what changed."""
    before = {i["id"]: i for c in (old_snapshot or {}).get("courses", []) for i in c.get("contents", [])}
    for c in courses:
        log(f"Saving study materials for {short(c['name'])}")
        root = MATERIALS / safe_name(short(c["name"]))
        for i in c["contents"]:
            if i["handler"] not in MATERIAL_HANDLERS:
                continue
            old = before.get(i["id"]) or {}
            if old.get("modified") == i.get("modified") and "files" in old \
                    and all(Path(f["local"]).exists() for f in old["files"]):
                i["files"] = old["files"]
                continue
            i["files"] = []
            try:
                for att in bb.attachments(c["id"], i["id"]):
                    data = bb.download(att["downloadPath"])
                    if not data:
                        continue
                    dest = root / safe_name(i["path"].split(" / ")[0]) / file_name(att["fileName"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    entry = {"name": att["fileName"], "local": str(dest)}
                    try:
                        page = preview_page(att["fileName"], data)
                    except Exception:
                        page = None
                    if page:
                        pv = dest.with_name(dest.name + ".preview.html")
                        pv.write_text(page, encoding="utf-8")
                        entry["preview"] = str(pv)
                    i["files"].append(entry)
            except Exception as ex:
                log(f"Couldn't save files from {i['title']}: {ex!r}")


def heading_topic(n):
    """A note's most telling heading, without "Revision notes", the module code or the module's name.
    Used when there's no API key to write a proper topic title."""
    junk = re.compile(r"(exam\s+)?revision\s+notes?|\b[A-Z0-9]{5,8}-\d+-\d\b|" + re.escape(short(n["course"])), re.I)
    for line in n["md"].splitlines():
        m = re.match(r"#{1,3}\s+(.*)", line)
        if not m or re.match(r"\d+\.", m.group(1)):          # "1. Summary", "2. Key concepts"...
            continue
        t = re.sub(r"\(\s*\)", "", re.sub(r"\s+", " ", junk.sub("", m.group(1)))).strip(" :–-,")
        if len(t) >= 6:
            return t[:60]
    return n["title"]


def note_topics(notes):
    """Gives each study note a "topic": a few words on what it's about, for the note map (Blackboard
    titles are often just "Read" or a file name). Claude Haiku writes each one once, for a fraction of a
    cent, and it's cached by the note's text; without an API key the note's best heading is used."""
    cache, topic_ai, new = load_json(CACHE, {}), None, False
    for n in notes:
        key = "topic:" + hashlib.sha1(n["md"].encode()).hexdigest()
        if key not in cache and signed_in():
            try:
                topic_ai = topic_ai or new_ai(effort="low", model="claude-haiku-5-5")
                cache[key] = topic_ai.note_topic(n["title"], n["md"])
                new = True
            except Exception as e:
                log_quiet(f"note topic failed for {n['title']}: {e!r}")
        # "... Revision Notes" says nothing about the topic, so it goes even if Claude added it
        n["topic"] = re.sub(r"[\s:–-]*((exam\s+)?revision\s+)?notes?$", "", cache.get(key) or "", flags=re.I) or heading_topic(n)
    if new:
        save_json(CACHE, cache)
    if topic_ai:
        add_spend(topic_ai.cost)


def make_notes_for(item_id):
    """Writes study notes for one module item from its downloaded files, then rebuilds the dashboard."""
    from ai import AI, file_to_blocks
    snapshot = load_json(SNAPSHOT, None)
    found = next(((c, i) for c in snapshot["courses"] for i in c["contents"] if i["id"] == item_id), None)
    if not found:
        return "That item is no longer on Blackboard."
    c, i = found
    blocks = [{"type": "text", "text": "Item text:\n" + strip_html(i["body"])[:50_000]}] if strip_html(i.get("body")) else []
    for f in i.get("files", []):
        if Path(f["local"]).exists() and f["name"].lower().endswith(READABLE):
            blocks += file_to_blocks(f["name"], Path(f["local"]).read_bytes())
    if not blocks:
        return "No readable files here yet. Press refresh to download them, then try again."
    ai = new_ai() or AI(effort=AI_EFFORT)
    note = ai.study_notes(c["name"], i["path"], blocks)
    add_spend(ai.cost)
    cache = load_json(CACHE, {})
    cache[f"notes:{i['id']}:{i.get('modified')}"] = note
    save_json(CACHE, cache)
    latest = load_json(DATA_DIR / "ai-latest.json", {})
    latest["notes"] = [n for n in latest.get("notes", []) if (n["course"], n["path"]) != (c["name"], i["path"])]
    latest["notes"].append({"course": c["name"], "title": i["title"], "path": i["path"],
                            "changed": i.get("modified") or i.get("created"), "md": note})
    note_topics(latest["notes"])
    save_json(DATA_DIR / "ai-latest.json", latest)
    out = OUT_DIR / "notes" / safe_name(c["name"]) / f"{safe_name(i['path'].replace(' / ', ' - '))}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# {i['title']}\n\n_{i['path']}_\n\n{note}", encoding="utf-8")
    record_activity("notes")
    run(sync=False)
    return "ok"


def chat_context(snapshot):
    """What Claude sees when you ask it something: modules, outlines, updates, deadlines, timetable, notes."""
    latest = load_json(DATA_DIR / "ai-latest.json", {})
    return json.dumps({
        "today": datetime.now().strftime("%A %d %B %Y"),
        "deadlines": [{**d, "due": d["due"].strftime("%a %d %b %Y %H:%M")} for d in collect_deadlines(snapshot)],
        "timetable": [{"start": t["start"].strftime("%a %d %b %H:%M"), "title": t["title"], "room": t["location"]}
                      for t in timetable(snapshot, days=14)],
        "modules": [{"name": c["name"],
                     "announcements": [{"title": a["title"], "date": a.get("date"), "text": strip_html(a["body"])[:1500]}
                                       for a in c.get("announcements", [])[:15]],
                     "assessments": [{"name": g["name"], "due": g["due"], "mark": g.get("display"), "out_of": g["possible"]}
                                     for g in c.get("gradebook", [])],
                     "outline": [i["path"] for i in c.get("contents", [])]}
                    for c in snapshot["courses"]],
        "weekly_briefing": latest.get("briefing"),
        "study_notes_available": [f'{short(n["course"])}: {n["path"]}' for n in latest.get("notes", [])],
    }, default=str)


EXTRA_CSS = """
[hidden]{display:none!important}
/* modules */
.tt td:first-child{white-space:nowrap} .tt td:first-child,.tt th:first-child{padding-left:30px}
.tt .near{margin:0 10px 0 -17px}
.course{width:100%;text-align:left;border:0;font:inherit;cursor:pointer;color:var(--text)}
.mod-view{animation:rise .6s var(--spring) both}
.mod-bar{display:flex;justify-content:space-between;margin:4px 0 10px}
.ghost{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;padding:8px 14px;
 font:11.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;text-decoration:none}
.ghost:hover{background:var(--inset)}
.mod-title{font:700 30px/1.15 var(--dotf);text-transform:uppercase;margin:18px 0 4px}
details.sec{border-radius:22px;margin:10px 0;padding:0;interpolate-size:allow-keywords}
details.sec > summary{padding:16px 20px;cursor:pointer;list-style:none;font-weight:600;display:flex;gap:14px;align-items:baseline}
details.sec > summary::-webkit-details-marker,details.node > summary::-webkit-details-marker{display:none}
details.sec > summary::after,details.node > summary::after{content:"›";margin-left:auto;color:var(--muted);transition:transform .5s var(--bounce)}
details[open] > summary::after{transform:rotate(90deg)}
.sec-n{font:11px var(--mono);color:var(--muted);letter-spacing:.1em}
.sec-in{padding:0 20px 16px}
details.node{border-top:1px solid var(--line)}
details.node > summary{padding:12px 2px;cursor:pointer;list-style:none;display:flex}
.node-in{padding:0 0 12px 14px;border-left:1px solid var(--line);margin-left:4px}
.page-body{font-size:14.5px;line-height:1.6;overflow-wrap:anywhere}
.page-body a{color:inherit} .page-body img{max-width:100%;height:auto;border-radius:12px}
.file{display:flex;align-items:center;gap:12px;width:100%;text-align:left;border:0;border-top:1px solid var(--line);
 background:transparent;color:var(--text);font:inherit;font-size:14.5px;padding:11px 2px;cursor:pointer;text-decoration:none}
.file:hover{background:var(--inset)} .file .ext{margin-left:auto;color:var(--muted)}
.tag{font:10.5px var(--mono);letter-spacing:.08em;color:var(--muted);border:1px solid var(--line);border-radius:6px;
 padding:2px 6px;min-width:58px;text-align:center;flex:none}
/* viewer */
.viewer{position:fixed;inset:0;z-index:30;background:var(--bg);display:flex;flex-direction:column;opacity:0;
 transform:scale(.98);transition:opacity .3s,transform .45s var(--spring)}
.viewer.on{opacity:1;transform:none}
.v-bar{display:flex;align-items:center;gap:12px;margin:12px;padding:8px 8px 8px 18px;border-radius:999px}
.v-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap} .v-act{margin-left:auto;display:flex;gap:6px;flex:none}
.v-body{flex:1;display:flex;padding:0 12px 12px} .v-body iframe,.v-body video{flex:1;border:0;border-radius:18px;background:var(--bg);width:100%}
.v-none{margin:auto;text-align:center;color:var(--muted)}
/* notes */
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 4px}
.chip{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;padding:7px 14px;
 font:11.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;cursor:pointer}
.chip.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.sec-label{font:11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:18px 6px 2px}
.group[hidden]{display:none}
/* updates */
.upd > summary{display:flex;align-items:center;gap:12px} .upd .ttl{flex:1;min-width:0}
.upd > summary::after{float:none;order:3} .upd .hide{flex:none;order:2;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:999px;
 padding:2px 10px;font:10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.upd .hide:hover{color:var(--text)} .upd.gone{display:none} #updates.reveal .upd.gone{display:block;opacity:.45}
.upd-bar{margin:0 0 4px;text-align:right}
/* notes to-do */
.todo{display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--line)} .todo:first-child{border-top:0}
.todo .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.todo .tag{min-width:0;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mk:disabled{opacity:.6;cursor:progress}
/* activity graph */
.heat-wrap{border-radius:22px;padding:16px 20px}
.heat{--cell:12px;--gap:5px} .sheet .heat{--cell:9px;--gap:4px}
.heat-bar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.yr{display:flex;align-items:center;gap:8px} .yr b{font:700 15px var(--mono);letter-spacing:.06em}
.yb{padding:2px 10px;font-size:14px} .yb:disabled{opacity:.3;cursor:default}
.seg{display:flex;border:1px solid var(--line);border-radius:999px;padding:3px}
.seg button{border:0;background:transparent;color:var(--muted);border-radius:999px;padding:5px 12px;font:10.5px var(--mono);
 letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.seg button.on{background:var(--text);color:var(--bg)}
.heat-scroll{overflow-x:auto;padding-bottom:6px;scrollbar-width:thin}
.months{display:flex;gap:18px;width:max-content}
.month{display:flex;flex-direction:column;gap:8px} .month span,.strip-labels span{font:11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.strip{width:max-content} .strip-labels{display:grid;column-gap:var(--gap);margin-bottom:6px} .strip-labels span{white-space:nowrap}
.heat-grid{display:grid;grid-template-rows:repeat(7,var(--cell));grid-auto-flow:column;grid-auto-columns:var(--cell);gap:var(--gap)}
.heat i{display:block;width:var(--cell);height:var(--cell);border-radius:50%;background:var(--dot)}
.heat-key i{display:block;border-radius:50%;background:var(--dot)}
.heat .l1,.heat-key .l1{background:color-mix(in srgb,var(--text) 30%,transparent)}
.heat .l2,.heat-key .l2{background:color-mix(in srgb,var(--text) 55%,transparent)}
.heat .l3,.heat-key .l3{background:color-mix(in srgb,var(--text) 78%,transparent)}
.heat .l4,.heat-key .l4{background:var(--text)}
.heat .pad{visibility:hidden} .heat .fut{opacity:.35} .heat .today{box-shadow:0 0 0 2px var(--bg),0 0 0 3px var(--muted)}
.heat-foot{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:12px;font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.heat-key{margin-left:auto;display:flex;align-items:center;gap:4px} .heat-key i{display:inline-block;width:10px;height:10px}
html.no-anim *,html.no-anim *::before,html.no-anim *::after{transition:none!important;animation:none!important}
html.no-anim .intro{display:none!important}

/* ---- the app's own pointer. Three shapes to choose from, each with its own hot spot,
   pale on a dark theme and dark on a light one. Red marks anything you can press. */
html.cur-ring{--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23fff%27%20stroke-width%3D%273.4%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%230a0a0a%27%20stroke-width%3D%271.5%27%2F%3E%3C%2Fsvg%3E") 13 13;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23fff%27%20stroke-width%3D%273.4%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%230a0a0a%27%20stroke-width%3D%271.5%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%272.4%27%20fill%3D%27%23d71921%27%2F%3E%3C%2Fsvg%3E") 13 13}
@media (prefers-color-scheme:dark){html.cur-ring:not([data-theme=light]){--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273.4%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23efefef%27%20stroke-width%3D%271.5%27%2F%3E%3C%2Fsvg%3E") 13 13;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273.4%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23efefef%27%20stroke-width%3D%271.5%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%272.4%27%20fill%3D%27%23d71921%27%2F%3E%3C%2Fsvg%3E") 13 13}}
html.cur-ring[data-theme=dark]{--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273.4%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23efefef%27%20stroke-width%3D%271.5%27%2F%3E%3C%2Fsvg%3E") 13 13;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273.4%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%276%27%20fill%3D%27none%27%20stroke%3D%27%23efefef%27%20stroke-width%3D%271.5%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%272.4%27%20fill%3D%27%23d71921%27%2F%3E%3C%2Fsvg%3E") 13 13}
html.cur-dot{--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27none%27%20stroke%3D%27%23fff%27%20stroke-width%3D%273%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27%230a0a0a%27%2F%3E%3C%2Fsvg%3E") 13 13;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27none%27%20stroke%3D%27%23fff%27%20stroke-width%3D%273%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27%23d71921%27%2F%3E%3C%2Fsvg%3E") 13 13}
@media (prefers-color-scheme:dark){html.cur-dot:not([data-theme=light]){--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27%23efefef%27%2F%3E%3C%2Fsvg%3E") 13 13;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27%23d71921%27%2F%3E%3C%2Fsvg%3E") 13 13}}
html.cur-dot[data-theme=dark]{--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27%23efefef%27%2F%3E%3C%2Fsvg%3E") 13 13;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27none%27%20stroke%3D%27%23000%27%20stroke-width%3D%273%27%20opacity%3D%27.55%27%2F%3E%3Ccircle%20cx%3D%2713%27%20cy%3D%2713%27%20r%3D%273.6%27%20fill%3D%27%23d71921%27%2F%3E%3C%2Fsvg%3E") 13 13}
html.cur-arrow{--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Cpath%20d%3D%27M4%202%20L4%2018.6%20L8.5%2014.6%20L11.2%2020.9%20L14.1%2019.6%20L11.3%2013.4%20L17.2%2013.1%20Z%27%20fill%3D%27%230a0a0a%27%20stroke%3D%27%23fff%27%20stroke-width%3D%271.5%27%20stroke-linejoin%3D%27round%27%2F%3E%3C%2Fsvg%3E") 4 2;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Cpath%20d%3D%27M4%202%20L4%2018.6%20L8.5%2014.6%20L11.2%2020.9%20L14.1%2019.6%20L11.3%2013.4%20L17.2%2013.1%20Z%27%20fill%3D%27%230a0a0a%27%20stroke%3D%27%23fff%27%20stroke-width%3D%271.5%27%20stroke-linejoin%3D%27round%27%2F%3E%3Ccircle%20cx%3D%2718.5%27%20cy%3D%274.5%27%20r%3D%273%27%20fill%3D%27%23d71921%27%20stroke%3D%27%23fff%27%20stroke-width%3D%271.2%27%2F%3E%3C%2Fsvg%3E") 4 2}
@media (prefers-color-scheme:dark){html.cur-arrow:not([data-theme=light]){--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Cpath%20d%3D%27M4%202%20L4%2018.6%20L8.5%2014.6%20L11.2%2020.9%20L14.1%2019.6%20L11.3%2013.4%20L17.2%2013.1%20Z%27%20fill%3D%27%23efefef%27%20stroke%3D%27%23000%27%20stroke-width%3D%271.5%27%20stroke-linejoin%3D%27round%27%2F%3E%3C%2Fsvg%3E") 4 2;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Cpath%20d%3D%27M4%202%20L4%2018.6%20L8.5%2014.6%20L11.2%2020.9%20L14.1%2019.6%20L11.3%2013.4%20L17.2%2013.1%20Z%27%20fill%3D%27%23efefef%27%20stroke%3D%27%23000%27%20stroke-width%3D%271.5%27%20stroke-linejoin%3D%27round%27%2F%3E%3Ccircle%20cx%3D%2718.5%27%20cy%3D%274.5%27%20r%3D%273%27%20fill%3D%27%23d71921%27%20stroke%3D%27%23000%27%20stroke-width%3D%271.2%27%2F%3E%3C%2Fsvg%3E") 4 2}}
html.cur-arrow[data-theme=dark]{--cur:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Cpath%20d%3D%27M4%202%20L4%2018.6%20L8.5%2014.6%20L11.2%2020.9%20L14.1%2019.6%20L11.3%2013.4%20L17.2%2013.1%20Z%27%20fill%3D%27%23efefef%27%20stroke%3D%27%23000%27%20stroke-width%3D%271.5%27%20stroke-linejoin%3D%27round%27%2F%3E%3C%2Fsvg%3E") 4 2;--cur-p:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%2726%27%20height%3D%2726%27%20viewBox%3D%270%200%2026%2026%27%3E%3Cpath%20d%3D%27M4%202%20L4%2018.6%20L8.5%2014.6%20L11.2%2020.9%20L14.1%2019.6%20L11.3%2013.4%20L17.2%2013.1%20Z%27%20fill%3D%27%23efefef%27%20stroke%3D%27%23000%27%20stroke-width%3D%271.5%27%20stroke-linejoin%3D%27round%27%2F%3E%3Ccircle%20cx%3D%2718.5%27%20cy%3D%274.5%27%20r%3D%273%27%20fill%3D%27%23d71921%27%20stroke%3D%27%23000%27%20stroke-width%3D%271.2%27%2F%3E%3C%2Fsvg%3E") 4 2}
html.fancy-cursor,html.fancy-cursor body{cursor:var(--cur),default}
html.fancy-cursor button,html.fancy-cursor a,html.fancy-cursor label,html.fancy-cursor select,
html.fancy-cursor summary,html.fancy-cursor [role=button],html.fancy-cursor input[type=checkbox],
html.fancy-cursor input[type=range],html.fancy-cursor .chip,html.fancy-cursor .swatch,
html.fancy-cursor .course,html.fancy-cursor .nb-item{cursor:var(--cur-p),pointer}
html.fancy-cursor input[type=text],html.fancy-cursor input[type=password],html.fancy-cursor textarea,
html.fancy-cursor [contenteditable=true]{cursor:text}
html.fancy-cursor button:disabled{cursor:var(--cur),not-allowed}
html.fancy-cursor .sh-grip{cursor:ew-resize}
html.cur-live .redge{cursor:none!important}      /* the drawn pointer shows the direction instead */

/* ---- the live pointer. Windows' own is hidden and the page draws two shapes instead:
   a dot that tracks exactly and a ring that chases it, so it stretches as you move. */
html.cur-live,html.cur-live *{cursor:none!important}
html{display:block!important}          /* nothing may hide the document itself */
.pointer-ring,.pointer-dot{display:none;position:fixed;left:0;top:0;pointer-events:none;z-index:999;
 will-change:transform;transition:width .3s var(--bounce),height .3s var(--bounce),
 opacity .22s,background .22s,border-color .22s,border-radius .3s var(--bounce)}
html.cur-live .pointer-ring,html.cur-live .pointer-dot{display:block}
html.cur-live.cur-away .pointer-ring,html.cur-live.cur-away .pointer-dot{display:none}
.pointer-ring{width:30px;height:30px;border:1.4px solid var(--text);border-radius:50%;opacity:.5}
.pointer-dot{width:6px;height:6px;border-radius:50%;background:var(--text)}
.pointer-ring.hot{width:46px;height:46px;border-color:var(--red);opacity:.95}
.pointer-dot.hot{width:4px;height:4px;background:var(--red)}
.pointer-ring.press{width:20px;height:20px;opacity:1;border-width:2px}
.pointer-dot.press{width:9px;height:9px}
.pointer-ring.text{width:3px;height:24px;border-radius:2px;background:var(--text);border-color:transparent;opacity:.75}
.pointer-dot.text{opacity:0}
/* grab something you can move, a slider, something switched off, and while the app is busy */
.pointer-ring.drag{width:38px;height:38px;border-style:dashed;opacity:.8}
.pointer-ring.slide{width:42px;height:16px;border-radius:9px;opacity:.85}
.pointer-dot.slide{width:4px;height:4px}
.pointer-ring.rz{width:40px;height:13px;border-radius:8px;opacity:.9;border-width:1.6px}
.pointer-dot.rz{width:4px;height:4px}
.pointer-ring.no{width:22px;height:22px;border-style:dotted;opacity:.4}
.pointer-dot.no{opacity:.3}
html.busy .pointer-ring{border-color:transparent;border-top-color:var(--red);opacity:.95;
 animation:spin .9s linear infinite}
html.no-anim .pointer-ring,html.no-anim .pointer-dot{transition:none}
html.no-anim.busy .pointer-ring{animation:none;border-color:var(--red)}

/* ---- where to put the window: our own Snap Layouts, since frameless loses the real one */
.snapmenu{position:fixed;top:38px;right:12px;z-index:62;display:none;grid-template-columns:repeat(4,1fr);gap:5px;padding:8px;
 border-radius:14px;background:var(--card);border:1px solid var(--line);box-shadow:0 16px 44px rgba(0,0,0,.26)}
.snapmenu:not([hidden]){display:grid}
.snapmenu button{width:38px;height:28px;border:1px solid var(--line);border-radius:7px;background:transparent;
 cursor:pointer;padding:0;position:relative;transition:border-color .18s,background .18s}
.snapmenu button:hover{border-color:var(--text);background:var(--inset)}
.snapmenu i{position:absolute;background:var(--text);opacity:.75;border-radius:2px}
.snapmenu .sl{inset:5px 50% 5px 5px} .snapmenu .sr{inset:5px 5px 5px 50%}
.snapmenu .sf{inset:5px} .snapmenu .st{inset:5px 5px 50% 5px}
.snapmenu .stl{inset:5px 50% 50% 5px} .snapmenu .str{inset:5px 5px 50% 50%}
.snapmenu .sbl{inset:50% 50% 5px 5px} .snapmenu .sbr{inset:50% 5px 5px 50%}
.snapmenu .swin{font-size:14px;line-height:1;color:var(--muted)}
.snapmenu .swin:hover{color:var(--text)}

/* ---- the window's own resize edges: a frameless window gets none from Windows */
.redge{position:fixed;z-index:60;display:none}
.app .redge{display:block}
.e-n{top:0;left:9px;right:9px;height:5px;cursor:n-resize!important}
.e-s{bottom:0;left:9px;right:9px;height:5px;cursor:s-resize!important}
.e-w{left:0;top:9px;bottom:9px;width:5px;cursor:w-resize!important}
.e-e{right:0;top:9px;bottom:9px;width:5px;cursor:e-resize!important}
.e-nw{top:0;left:0;width:9px;height:9px;cursor:nw-resize!important}
.e-ne{top:0;right:0;width:9px;height:9px;cursor:ne-resize!important}
.e-sw{bottom:0;left:0;width:9px;height:9px;cursor:sw-resize!important}
.e-se{bottom:0;right:0;width:9px;height:9px;cursor:se-resize!important}

/* Windows draws select menus and scrollbars itself, so it has to be told the theme */
:root{color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark}}
:root[data-theme=dark]{color-scheme:dark} :root[data-theme=light]{color-scheme:light}

/* ---- the Claude controls, repeated at the top of the Ask Claude panel */
.ai-bar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:9px 0 11px;border-bottom:1px solid var(--line)}
.ai-bar select{border:1px solid var(--line);background:var(--card);color:inherit;font:inherit;font-size:12px;
 padding:5px 7px;border-radius:9px;cursor:pointer;max-width:118px}
.ai-who{display:flex;align-items:center;gap:7px;max-width:152px;font:inherit;font-size:12.5px;color:inherit;
 padding:5px 10px;border-radius:9px;border:1px solid var(--line);background:transparent;cursor:pointer;
 transition:background .18s}
.ai-who:hover{background:var(--inset)}
.ai-who b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ai-who .dot{width:7px;height:7px;border-radius:50%;background:var(--red);flex:none}
.ai-who.in .dot{background:#1f9d55}
.ai-spend{margin-left:auto;font:11px var(--mono);color:var(--muted);letter-spacing:.04em}
.ai-accs{margin:8px 0 0;padding:5px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.ai-accs button{display:flex;align-items:center;gap:8px;width:100%;text-align:left;border:0;background:transparent;
 color:inherit;font:inherit;font-size:13px;padding:8px 9px;border-radius:9px;cursor:pointer}
.ai-accs button:hover{background:var(--inset)}
.ai-accs button.on b{font-weight:600} .ai-accs .hint{font:11px var(--mono);color:var(--muted)}
.ai-accs .sep{height:1px;background:var(--line);margin:4px 6px}

/* ---- settings: sections inside the account sheet */
.set-tabs{display:flex;gap:2px;margin:14px 0 10px;padding:3px;border-radius:12px;background:var(--inset)}
.set-tabs button{flex:1;border:0;background:transparent;color:var(--muted);cursor:pointer;padding:7px 4px;border-radius:9px;
 font:10.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;transition:background .2s,color .2s}
.set-tabs button.on{background:var(--card);color:var(--text)}
.set-pane{display:none;flex-direction:column;gap:2px} .set-pane.on{display:flex}

/* a row of colour choices */
.swatches{display:flex;gap:8px;flex-wrap:wrap}
.swatch{width:26px;height:26px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;
 box-shadow:inset 0 0 0 1px var(--rim)}
.swatch[aria-pressed=true]{box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--text)}

/* a plain on/off switch */
.sw{position:relative;width:38px;height:22px;border-radius:999px;border:0;cursor:pointer;background:var(--inset);
 transition:background .25s;flex:none;padding:0}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:var(--muted);
 transition:transform .25s var(--bounce),background .25s}
.sw[aria-pressed=true]{background:var(--text)} .sw[aria-pressed=true]::after{transform:translateX(16px);background:var(--bg)}

/* the saved Claude accounts */
.acc{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:12px;border:1px solid var(--line);margin:4px 0}
.acc.on{border-color:var(--text)}
.acc b{font-size:14px;font-weight:600} .acc .hint{font:11px var(--mono);color:var(--muted)}
.acc .grow{flex:1;min-width:0} .acc .grow b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acc .use{font:10.5px var(--mono);letter-spacing:.06em;text-transform:uppercase}
.sign{display:flex;flex-direction:column;gap:7px;padding:12px;border-radius:14px;background:var(--inset);margin-top:6px}
.sign input{border:1px solid var(--line);background:var(--card);color:inherit;font:inherit;font-size:13.5px;
 padding:9px 11px;border-radius:10px;width:100%;box-sizing:border-box}
.sign .msg{font-size:12px;color:var(--muted)} .sign .msg.bad{color:var(--red)}
.note{color:var(--muted);margin-top:7px;font-size:12.5px;line-height:1.45}
.locked{display:flex;gap:9px;align-items:flex-start;padding:11px 13px;border-radius:14px;background:var(--inset);font-size:13px}

/* tab list, with drag to reorder */
.tab-row{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:11px;border:1px solid var(--line);margin:4px 0;
 background:var(--card);cursor:grab}
.tab-row.drag{opacity:.45;cursor:grabbing} .tab-row .grab{color:var(--muted);font:13px var(--mono);cursor:grab}
.tab-row .grow{flex:1} .tab-row input{accent-color:var(--text);width:15px;height:15px;cursor:pointer}

/* usage */
.usage{display:grid;grid-template-columns:1fr auto;gap:6px 12px;font-size:13.5px;margin:4px 0}
.usage .m{color:var(--muted);font-family:var(--mono);font-size:12px}
.usage .c{text-align:right;font-family:var(--mono);font-size:12.5px}

/* the window's own title bar is off, so the app draws one: a strip to drag by and three buttons */
.titlebar{app-region:drag;-webkit-app-region:drag;
 position:fixed;top:0;left:0;right:0;height:38px;z-index:9;display:none;
 align-items:center;justify-content:center;pointer-events:auto}
.app .titlebar,.setup .titlebar{display:flex}
html.intro-on .titlebar span{opacity:0}
.titlebar button,.titlebar a{app-region:no-drag;-webkit-app-region:no-drag}
.titlebar span{font:9.5px var(--mono);letter-spacing:.26em;color:var(--muted);opacity:.65;user-select:none;
 transition:opacity .3s}
.wctl{position:fixed;top:0;right:0;z-index:12;display:none}
.app .wctl,.setup .wctl{display:flex}
html.intro-on .wctl{opacity:0}
.wctl button{position:relative;width:44px;height:34px;border:0;background:transparent;color:var(--muted);cursor:pointer;
 display:grid;place-items:center;padding:0;transition:color .2s;-webkit-app-region:no-drag}
/* the pill grows out from the middle rather than just appearing */
.wctl button::before{content:"";position:absolute;inset:3px 4px;border-radius:10px;background:var(--inset);
 transform:scale(.55);opacity:0;transition:transform .3s var(--bounce),opacity .18s}
.wctl button:hover::before,.wctl button:focus-visible::before{transform:scale(1);opacity:1}
.wctl button:hover{color:var(--text)}
.wctl button:focus-visible{outline:2px solid var(--text);outline-offset:-3px}
.wctl .wclose::before{background:var(--red)}
.wctl .wclose:hover,.wctl .wclose:focus-visible{color:#fff}
.wctl svg{display:block;width:10px;height:10px;position:relative;z-index:1;
 transition:transform .32s var(--bounce)}
.wctl button:hover svg{transform:scale(1.22)}
.wctl button:active svg{transform:scale(.82)}
.wctl .wmin:hover svg{transform:translateY(2px) scaleX(1.25)}      /* the line drops, like the window will */
.wctl .wmax:hover svg{transform:scale(1.28)}
.wctl .wclose:hover svg{transform:scale(1.2) rotate(90deg)}        /* the cross spins into a plus and back */
html.no-anim .wctl button::before{transition:none} html.no-anim .wctl svg{transition:none}

/* a sheet can be dragged wider by its inner edge */
.sh-grip{position:fixed;top:12px;bottom:12px;width:14px;z-index:27;cursor:ew-resize;display:grid;place-items:center}
.sh-grip::after{content:"";width:3px;height:40px;border-radius:3px;background:var(--text);opacity:0;transition:opacity .18s}
.sh-grip:hover::after,.sh-grip.dragging::after{opacity:.45}

/* in the app, theme and refresh sit in the title bar beside the window buttons, so they never cover the page */
.app .theme.glass{top:4px!important;right:142px!important;width:30px;height:30px;background:transparent;border-color:transparent;
 box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none}
.app .theme.refresh{right:176px!important}
.app .theme.glass:hover{background:var(--inset)} .app .theme svg{width:15px;height:15px}
/* corner buttons */
.acct,.ask{display:none!important} .app .acct,.app .ask{display:flex!important}
.acct{position:fixed;top:18px;left:18px;z-index:10;width:40px;height:40px;border-radius:50%;border:0;cursor:pointer;
 align-items:center;justify-content:center;padding:0;color:var(--text)}
.av{font:800 18px/1 var(--dotf);text-transform:uppercase}
.ask{position:fixed;right:22px;bottom:22px;z-index:10;border:0;border-radius:999px;padding:0 16px 0 13px;height:46px;gap:8px;
 align-items:center;cursor:pointer;color:var(--text);font:11.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;
 transition:transform .5s var(--bounce)}
.ask:hover{transform:scale(1.04)} .ask:active{transform:scale(.95)} .ask svg{width:18px;height:18px}
html.intro-on .acct,html.intro-on .ask{opacity:0}
@media (max-width:900px){.ask{bottom:84px}}
/* sheets */
.scrim{position:fixed;inset:0;z-index:25;background:rgba(0,0,0,.3);opacity:0;pointer-events:none;transition:opacity .3s}
.scrim.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;top:12px;bottom:12px;z-index:26;width:min(420px,calc(100vw - 24px));border-radius:28px;padding:18px 20px;
 display:flex;flex-direction:column;gap:10px;overflow-y:auto;background:var(--glass-strong);
 transition:transform .55s var(--spring),opacity .3s;opacity:0;pointer-events:none}
.sheet.left{left:12px;transform:translateX(-30px)} .sheet.right{right:12px;transform:translateX(30px)}
.sheet.on{transform:none;opacity:1;pointer-events:auto}
.sh-top{display:flex;align-items:center;justify-content:space-between} .sh-top .label{margin:0}
.sheet .label{margin:14px 2px 2px}
.who{display:flex;align-items:center;gap:14px} .who b{display:block;font-size:18px} .who .sub{margin:2px 0 0}
.av.big{width:54px;height:54px;border-radius:50%;display:grid;place-items:center;background:var(--text);color:var(--bg);font-size:26px}
.facts{display:grid;grid-template-columns:1fr auto;gap:8px 12px;margin:6px 0 0;font-size:14px}
.facts dt{color:var(--muted)} .facts dd{margin:0;text-align:right;font-family:var(--mono);font-size:12.5px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:14px;padding:8px 0;border-top:1px solid var(--line)}
.row.col{flex-direction:column;align-items:stretch} .row small{color:var(--muted);min-height:1em}
.row select,.row input[type=password]{background:transparent;color:var(--text);border:1px solid var(--line);border-radius:12px;padding:7px 10px;font:inherit;font-size:13px}
.row select option{background:var(--bg)} .key{display:flex;gap:8px} .key input{flex:1;min-width:0}
.row input[type=range]{accent-color:var(--text);vertical-align:middle}
.danger{color:var(--red);border-color:color-mix(in srgb,var(--red) 40%,transparent)}
/* chat */
.chat-log{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding:4px 2px}
.chat-hints{margin:auto 0 0;display:flex;flex-direction:column;align-items:flex-start;gap:8px}
.chat-hints .chip{text-transform:none;letter-spacing:0;font:13.5px inherit;font-family:inherit}
.msg{max-width:88%;padding:10px 14px;border-radius:18px;font-size:14.5px;line-height:1.55;overflow-wrap:anywhere}
.msg.me{align-self:flex-end;background:var(--text);color:var(--bg);border-bottom-right-radius:6px}
.msg.them{align-self:flex-start;background:var(--inset);border:1px solid var(--line);border-bottom-left-radius:6px}
.msg.them p{margin:.3em 0} .msg.them table{font-size:13px} .msg.err{color:var(--red)}
.msg.wait{display:flex;gap:5px} .msg.wait i{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:blink 1.2s infinite}
.msg.wait i:nth-child(2){animation-delay:.2s} .msg.wait i:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
.chat-form{display:flex;gap:8px;align-items:flex-end;border:1px solid var(--line);border-radius:22px;padding:6px 6px 6px 14px}
.chat-in{flex:1;border:0;background:transparent;color:var(--text);font:inherit;font-size:14.5px;resize:none;outline:none;padding:7px 0;max-height:140px}
.send{width:34px;height:34px;border-radius:50%;border:0;background:var(--text);color:var(--bg);font-size:16px;cursor:pointer;flex:none}
/* sign in */
.signin[hidden],.viewer[hidden]{display:none}
.update .si-card .near{background:var(--text)} .update .sub b{color:var(--text)}
.signin{position:fixed;inset:0;z-index:40;display:grid;place-items:center;background:rgba(0,0,0,.35);backdrop-filter:blur(6px)}
.si-card{width:min(380px,88vw);padding:28px;border-radius:28px;text-align:center;animation:rise .6s var(--spring) both}
.si-card .near{margin:0 0 10px} .si-card h3{margin:0 0 6px} .si-card .btn{width:100%;border:0;cursor:pointer;margin:14px 0 8px}
.askwarn .si-card{width:min(420px,88vw)} .askwarn .note{text-align:left;font-size:14px;line-height:1.55;margin:8px 0}
.askwarn .aw-meta b{font-family:var(--mono)}
.aw-check{display:flex;align-items:center;gap:8px;margin:12px 0 2px;font-size:13.5px;color:var(--muted);cursor:pointer}
.aw-check input{width:16px;height:16px;accent-color:var(--text)}
.map-wrap{border-radius:22px;padding:10px 12px 12px}
.nmap{display:block;width:100%;height:380px;touch-action:none} .nmap.on-dot{cursor:pointer}
.map-meta{margin:2px 8px 0;font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.map-key{float:right;letter-spacing:.04em}
/* a dropdown's list is drawn by Windows, which ignored the dark theme and showed grey on white: colour it ourselves */
select option{background-color:var(--bg);color:var(--text)} select option:checked{background-color:var(--text);color:var(--bg)}
select option:disabled{color:var(--muted)}
.done-btn{white-space:nowrap} .done-btn[aria-pressed=true]{background:var(--text);color:var(--bg);border-color:var(--text)}
details.card > summary{position:relative} details.card > summary .done-btn{position:absolute;right:52px;top:50%;transform:translateY(-50%)}
details.card:has(.done-btn) > summary{padding-right:170px}
details.card:has(.done-btn) > summary::after{position:absolute;right:22px;top:50%;margin-top:-11px;float:none}
tr.is-done td{opacity:.45} tr.is-done td:nth-child(4){text-decoration:line-through} tr.is-done td:last-child{opacity:1}
details.card.is-done > summary b{text-decoration:line-through;opacity:.55}
.map-btn{position:fixed;right:22px;bottom:80px;z-index:10;width:46px;height:46px;border-radius:50%;border:0;padding:0;
 display:grid;place-items:center;cursor:pointer;color:var(--text);transition:transform .5s var(--bounce)}
.map-btn[hidden]{display:none} .map-btn svg{width:20px;height:20px}
.map-btn:hover{transform:scale(1.06)} .map-btn:active{transform:scale(.94)} .map-btn.on{color:var(--red)}
/* grows and shrinks with the window: about a third of its width, and never taller than the space above the button */
.map-pop{position:fixed;right:22px;bottom:138px;z-index:30;width:min(clamp(300px,34vw,620px),calc(100vw - 44px));border-radius:22px;padding:12px 12px 10px;
 animation:rise .45s var(--spring) both}
.map-pop[hidden]{display:none}
.mp-bar{display:flex;align-items:center;gap:10px;margin:0 2px 4px} .mp-bar .label{margin:0;flex:none;color:var(--red)}
.mp-name{flex:1;min-width:0;font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mp-x{border:0;background:transparent;color:var(--muted);width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:13px;flex:none}
.mp-x:hover{background:var(--inset);color:var(--text)}
.mp-cv{display:block;width:100%;height:min(clamp(200px,42vh,560px),calc(100vh - 260px))} .mp-cv.on-dot{cursor:pointer}
.mp-hint{margin:2px 4px 0;font:10.5px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
html.intro-on .map-btn{opacity:0}
@media (max-width:900px){.map-btn{bottom:142px}.map-pop{bottom:200px}}
"""

EXTRA_JS = r"""
// external links (Blackboard, websites) open in a window of the app; Shift or Ctrl+click still uses your browser
document.querySelectorAll('.page-body a[href^="http"]').forEach(a => a.target = '_blank');
document.addEventListener('click', e => {
  const a = e.target.closest('a[href^="http"]'); if (!a || e.shiftKey || e.ctrlKey) return;
  const api = window.pywebview?.api; if (!api?.open_inside) return;
  e.preventDefault();
  api.open_inside(a.href, a.textContent.replace('↗', '').trim());
}, true);
// Modules and Notes: card grid -> one module's page and back
function openView(id) {
  const v = document.getElementById(id); if (!v) return;
  v.parentElement.querySelector('.mod-grid').hidden = true;
  v.parentElement.querySelectorAll('.mod-view').forEach(x => x.hidden = x !== v);
  scrollTo({top: 0, behavior: 'smooth'});
}
document.querySelectorAll('[data-open]').forEach(b => b.onclick = () => openView(b.dataset.open));
document.querySelectorAll('.mod-view .back').forEach(b => b.onclick = () => {
  const v = b.closest('.mod-view'); v.hidden = true;
  v.parentElement.querySelector('.mod-grid').hidden = false;
});
// notes: filter by module
document.querySelectorAll('.chips').forEach(row => row.onclick = ev => {
  const chip = ev.target.closest('.chip'); if (!chip) return;
  row.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c === chip));
  row.parentElement.querySelectorAll('.group').forEach(g => g.hidden = chip.dataset.f !== 'all' && g.dataset.mod !== chip.dataset.f);
});
// file viewer
const V = document.querySelector('.viewer'), VB = V.querySelector('.v-body');
const inline = /\.(pdf|png|jpe?g|gif|webp|svg|txt|md|html?)$/i, video = /\.(mp4|webm|mov|m4v)$/i;
document.addEventListener('click', ev => {
  const f = ev.target.closest('button.file'); if (!f) return;
  const name = f.dataset.name, dark = getComputedStyle(document.body).backgroundColor === 'rgb(0, 0, 0)';
  V.querySelector('.v-name').textContent = name;
  V.dataset.local = f.dataset.local; V.dataset.src = f.dataset.src;
  VB.innerHTML = '';
  if (video.test(name)) {
    const v = document.createElement('video'); v.src = f.dataset.src; v.controls = true; v.autoplay = true; VB.append(v);
  } else if (f.dataset.preview || inline.test(name)) {
    const fr = document.createElement('iframe'); fr.src = f.dataset.preview ? f.dataset.preview + (dark ? '#dark' : '') : f.dataset.src; VB.append(fr);
  } else {
    VB.innerHTML = '<div class="v-none"><p>This file can\'t be previewed here.</p><p>Use Open in app.</p></div>';
  }
  V.hidden = false; requestAnimationFrame(() => V.classList.add('on'));
});
function closeViewer() { V.classList.remove('on'); setTimeout(() => { V.hidden = true; VB.innerHTML = ''; }, 300); }
V.querySelector('.v-close').onclick = closeViewer;
addEventListener('keydown', ev => { if (ev.key === 'Escape' && !V.hidden) closeViewer(); });
V.querySelector('.v-open').onclick = () => {
  if (window.pywebview && pywebview.api.open_file) pywebview.api.open_file(V.dataset.local);
  else window.open(V.dataset.src);
};
// sign-in prompt, shown by the app when Blackboard has signed you out
const SI = document.querySelector('.signin:not(.update)');
window.bbSignin = () => { SI.hidden = false; };
SI.querySelector('.si-later').onclick = () => { SI.hidden = true; };
SI.querySelector('.si-go').onclick = () => { SI.hidden = true; pywebview.api.sign_in(); };
window.addEventListener('pywebviewready', () => pywebview.api.state().then(s => { if (s.signin) bbSignin(); if (s.update) bbUpdate(s.update); }));
// update prompt, shown when a newer version is on GitHub
const UP = document.querySelector('.update');
window.bbUpdate = u => {
  UP.dataset.v = u.version; try { if (sessionStorage.updLater === u.version) return; } catch (_) {}
  UP.querySelector('.up-msg').innerHTML = `Version <b>${u.version}</b> is ready (you have ${BB.version}).` + (u.notes ? `<br>${u.notes.replace(/[<>&]/g, '')}` : '');
  UP.querySelector('.up-go').disabled = false; UP.querySelector('.up-go').textContent = 'Update now';
  UP.hidden = false;
};
UP.querySelector('.up-later').onclick = () => { UP.hidden = true; try { sessionStorage.updLater = UP.dataset.v; } catch (_) {} };
UP.querySelector('.up-go').onclick = async () => {
  const b = UP.querySelector('.up-go'); b.disabled = true; b.textContent = 'Downloading…';
  const r = await pywebview.api.install_update();
  if (r !== 'ok') { b.disabled = false; b.textContent = 'Try again'; UP.querySelector('.up-msg').textContent = r; }
  else b.textContent = 'Restarting…';
};

// ---- updates: hide one at a time (remembered on this computer)
const UPD = document.getElementById('updates');
let gone = new Set();
try { gone = new Set(JSON.parse(localStorage.hiddenUpdates || '[]')); } catch (_) {}
function paintUpdates() {
  const all = [...UPD.querySelectorAll('.upd')];
  all.forEach(d => { const h = gone.has(d.dataset.id); d.classList.toggle('gone', h); d.querySelector('.hide').textContent = h ? 'Unhide' : 'Hide'; });
  const n = all.filter(d => gone.has(d.dataset.id)).length, sh = UPD.querySelector('.show-hidden');
  if (sh) { sh.hidden = !n; sh.textContent = (UPD.classList.contains('reveal') ? 'Hide ' : 'Show ') + n + ' hidden'; }
}
UPD.addEventListener('click', ev => {
  const b = ev.target.closest('.hide');
  if (b) {
    ev.preventDefault(); ev.stopPropagation();
    const id = b.closest('.upd').dataset.id;
    gone.has(id) ? gone.delete(id) : gone.add(id);
    try { localStorage.hiddenUpdates = JSON.stringify([...gone]); } catch (_) {}
    paintUpdates();
  } else if (ev.target.closest('.show-hidden')) { UPD.classList.toggle('reveal'); paintUpdates(); }
});
paintUpdates();

// ---- activity graph: a whole year of dots, by month blocks or as one GitHub-style strip
const heatKey = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const heatLvl = n => n === 0 ? 0 : n < 3 ? 1 : n < 6 ? 2 : n < 10 ? 3 : 4;
let heatData = {};
function heatDot(d, today) {
  if (d > today) return '<i class="fut"></i>';
  const n = heatData[heatKey(d)] || 0;
  return `<i class="l${heatLvl(n)}${+d === +today ? ' today' : ''}" title="${d.toDateString()}: ${n} action${n === 1 ? '' : 's'}"></i>`;
}
function drawHeat(el) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const year = +(el.dataset.year || today.getFullYear());
  let layout = 'months'; try { layout = localStorage.heatLayout || 'months'; } catch (_) {}
  const years = Object.keys(heatData).map(k => +k.slice(0, 4)), first = Math.min(today.getFullYear(), ...years);
  let body = '';
  if (layout === 'months') {
    for (let m = 0; m < 12; m++) {
      const d1 = new Date(year, m, 1), last = new Date(year, m + 1, 0).getDate();
      let cells = '<i class="pad"></i>'.repeat((d1.getDay() + 6) % 7);  // rows run Monday to Sunday
      for (let day = 1; day <= last; day++) cells += heatDot(new Date(year, m, day), today);
      body += `<div class="month"><div class="heat-grid">${cells}</div><span>${d1.toLocaleDateString('en-GB', {month: 'short'})}</span></div>`;
    }
    body = `<div class="months">${body}</div>`;
  } else {
    const start = new Date(year, 0, 1); start.setDate(start.getDate() - (start.getDay() + 6) % 7);
    let cells = '', labels = '', col = 0;
    for (let d = new Date(start); d.getFullYear() <= year; d.setDate(d.getDate() + 1)) {
      if ((d.getDay() + 6) % 7 === 0) col++;
      if (d.getFullYear() < year) { cells += '<i class="pad"></i>'; continue; }
      if (d.getDate() === 1) labels += `<span style="grid-column:${col} / span 4">${d.toLocaleDateString('en-GB', {month: 'short'})}</span>`;
      cells += heatDot(new Date(d), today);
    }
    body = `<div class="strip"><div class="strip-labels" style="grid-template-columns:repeat(${col},var(--cell))">${labels}</div><div class="heat-grid">${cells}</div></div>`;
  }
  const active = Object.entries(heatData).filter(([k, n]) => n && k.startsWith(year + '-')).length;
  let streak = 0; for (let d = new Date(today); heatData[heatKey(d)]; d.setDate(d.getDate() - 1)) streak++;
  el.innerHTML = `<div class="heat-bar">
      <span class="yr"><button class="ghost yb" data-d="-1" ${year <= first ? 'disabled' : ''} aria-label="Previous year">‹</button><b>${year}</b>
      <button class="ghost yb" data-d="1" ${year >= today.getFullYear() ? 'disabled' : ''} aria-label="Next year">›</button></span>
      <span class="seg"><button class="${layout === 'months' ? 'on' : ''}" data-l="months">Months</button><button class="${layout === 'strip' ? 'on' : ''}" data-l="strip">Year</button></span></div>
    <div class="heat-scroll">${body}</div>
    <div class="heat-foot"><span>${active} active day${active === 1 ? '' : 's'} in ${year}</span><span>Current streak ${streak} day${streak === 1 ? '' : 's'}</span>
    <span class="heat-key">Less <i class="l0"></i><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i> More</span></div>`;
  el.querySelectorAll('.yb').forEach(b => b.onclick = () => { el.dataset.year = year + +b.dataset.d; drawHeat(el); });
  el.querySelectorAll('.seg button').forEach(b => b.onclick = () => {
    try { localStorage.heatLayout = b.dataset.l; } catch (_) {}
    document.querySelectorAll('.heat').forEach(drawHeat);
  });
  // mouse wheel scrolls the year sideways; at either end it scrolls the page as normal
  el.querySelector('.heat-scroll').onwheel = ev => {
    const sc = ev.currentTarget, max = sc.scrollWidth - sc.clientWidth;
    if (Math.abs(ev.deltaY) <= Math.abs(ev.deltaX) || max <= 0) return;
    if ((ev.deltaY < 0 && sc.scrollLeft > 0) || (ev.deltaY > 0 && sc.scrollLeft < max - 1)) { ev.preventDefault(); sc.scrollLeft += ev.deltaY; }
  };
  // start scrolled to today (or the end of a past year)
  const sc = el.querySelector('.heat-scroll'), t = sc.querySelector('.today');
  requestAnimationFrame(() => { sc.scrollLeft = t ? t.offsetLeft - sc.clientWidth / 2 : (year < today.getFullYear() ? sc.scrollWidth : 0); });
}
const drawAllHeat = data => { heatData = data || {}; document.querySelectorAll('.heat').forEach(drawHeat); };
drawAllHeat(BB.activity);

// ---- corner sheets: account & settings (top left), Ask Claude (bottom right)
const scrim = document.querySelector('.scrim');
// ---- window buttons, since the window has no title bar of its own
(() => {
  const api = () => window.pywebview && pywebview.api;
  const bar = document.querySelector('.titlebar'), maxBtn = document.querySelector('.wmax');
  const RESTORE = '<svg viewBox="0 0 10 10" aria-hidden="true"><rect x="0.6" y="2.4" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M2.9 2.2V0.6h6.5v6.5H7.8" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>';
  const SQUARE = '<svg viewBox="0 0 10 10" aria-hidden="true"><rect x="0.6" y="0.6" width="8.8" height="8.8" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>';
  const paint = big => { maxBtn.innerHTML = big ? RESTORE : SQUARE;
    maxBtn.title = maxBtn.ariaLabel = big ? 'Restore down' : 'Maximise'; };
  const toggleMax = async () => { if (api()) paint(await api().win_maximize()); };
  document.querySelector('.wmin').onclick = () => api() && api().win_minimize();
  maxBtn.onclick = () => { if (menuJustOpened()) return; toggleMax(); };
  document.querySelector('.wclose').onclick = () => api() && api().win_close();
  // Who moves the window. The browser claims to understand app-region whether or not this
  // engine acts on it, so the app is asked instead - it is the side that turns it on. Until it
  // says otherwise the page does the moving itself, which always works.
  let ownDrag = true;
  bar.ondblclick = toggleMax;
  bar.classList.add('pywebview-drag-region');
  (async () => {
    // the bridge to the app arrives a moment after the page does, so wait for it before asking
    await new Promise(go => {
      if (api()) return go();
      addEventListener('pywebviewready', go, {once: true});
      setTimeout(go, 5000);
    });
    let mode = 'app';
    try { if (api()) mode = await api().win_drag_mode(); } catch (_) {}
    if (mode === 'browser') {      // the browser hands the drag to the window, snapping and all
      ownDrag = false;
      bar.ondblclick = null;       // it does the double-click to maximise as well
      bar.classList.remove('pywebview-drag-region');
    }
  })();
  bar.addEventListener('pointerdown', ev => {
    if (!ownDrag) return;                      // the browser is doing the dragging
    if (ev.button !== 0 || !api() || bar.classList.contains('pywebview-drag-region')) return;
    if (ev.target.closest('button,a,input')) return;
    const x0 = ev.screenX, y0 = ev.screenY;
    const move = m => {
      if (Math.abs(m.screenX - x0) < 4 && Math.abs(m.screenY - y0) < 4) return;
      stop(); api().win_drag();
    };
    const stop = () => {
      removeEventListener('pointermove', move, true);
      removeEventListener('pointerup', stop, true);
    };
    addEventListener('pointermove', move, true);
    addEventListener('pointerup', stop, true);
  });

  // Windows can snap, maximise or restore the window without telling the page, so the button
  // asks what actually happened rather than keeping its own count.
  let look;
  addEventListener('resize', () => {
    clearTimeout(look);
    look = setTimeout(async () => { if (api()) paint(await api().win_state()); }, 120);
  });
})();

// Interface size is a CSS zoom on the root. Mouse coordinates arrive unzoomed, but anything
// positioned inside the zoomed page is read back in zoomed units, so the two must be divided.
window.uiZoom = () => parseFloat(document.documentElement.style.zoom) || 1;

// ---- the live pointer: a dot on the mouse and a ring easing along behind it
(() => {
  const ring = document.createElement('i'), dot = document.createElement('i');
  ring.className = 'pointer-ring'; dot.className = 'pointer-dot';
  document.body.append(ring, dot);
  // the resize edges keep Windows' own arrows, so they are deliberately left out
  const PRESSABLE = 'button,a,label,select,summary,[role=button],input[type=checkbox],.chip,.swatch,.course,.nb-item';
  const TYPEABLE = 'input[type=text],input[type=password],input[type=search],textarea,[contenteditable="true"]';
  const DRAGGABLE = '.titlebar,.tab-row,.sh-grip,[draggable=true]';
  const SLIDER = 'input[type=range]';
  const SHAPES = ['hot', 'text', 'drag', 'slide', 'no', 'rz'];
  // at a window edge the ring lies down as a bar pointing the way it will stretch
  const EDGE_TURN = {'e-w': 0, 'e-e': 0, 'e-n': 90, 'e-s': 90, 'e-nw': 45, 'e-se': 45, 'e-ne': -45, 'e-sw': -45};
  let x = -200, y = -200, rx = -200, ry = -200, tx = -200, ty = -200, going = false, overCanvas = false;
  const live = () => document.documentElement.classList.contains('cur-live');
  const calm = () => document.documentElement.classList.contains('no-anim');
  // Magnets: over a small button the ring is pulled toward its middle and the button leans a few px toward
  // the mouse, springing back when you leave. Over a note-map dot (the map sets window.bbStick) the ring
  // snaps onto the dot. The dot always stays exactly on the mouse, so clicks land where you point.
  const STICKY = 'button,a.ghost,a.btn,a.file,.swatch,[role=button]';
  const pulled = new Map();          // element -> {ox, oy, gx, gy}: its lean now and where it's heading
  let magnet = null;

  function frame() {
    const ease = calm() ? 1 : .22, z = uiZoom(), st = overCanvas && window.bbStick;
    if (st) { tx = st.x * z; ty = st.y * z; }        // map dots hold the ring right on them
    if (overCanvas) { ring.classList.toggle('hot', !!st); dot.classList.toggle('hot', !!st); }
    rx += (tx - rx) * ease; ry += (ty - ry) * ease;
    ring.style.transform = `translate(${rx / z}px,${ry / z}px) translate(-50%,-50%) rotate(${ring.dataset.turn || 0}deg)`;
    // ...and draw the small dot partway too, so the pull is felt; the click still lands on the map dot
    const dx = st ? x + (st.x * z - x) * .45 : x, dy = st ? y + (st.y * z - y) * .45 : y;
    dot.style.transform = `translate(${dx / z}px,${dy / z}px) translate(-50%,-50%)`;
    for (const [el, p] of pulled) {
      p.ox += (p.gx - p.ox) * .25; p.oy += (p.gy - p.oy) * .25;
      if (!p.gx && !p.gy && Math.abs(p.ox) < .05 && Math.abs(p.oy) < .05) { el.style.translate = ''; pulled.delete(el); }
      else el.style.translate = `${p.ox.toFixed(2)}px ${p.oy.toFixed(2)}px`;
    }
    if (Math.abs(tx - rx) > .1 || Math.abs(ty - ry) > .1 || pulled.size || overCanvas) requestAnimationFrame(frame);
    else going = false;
  }
  function stick(t, off) {
    const z = uiZoom();
    let el = !off && t.closest && t.closest(STICKY);
    const r = el && el.getBoundingClientRect();
    if (!r || r.width > 240 || r.height > 72 || el.closest('.titlebar,.redge,.nb-ed')) el = null;
    if (magnet && magnet !== el && pulled.has(magnet)) Object.assign(pulled.get(magnet), {gx: 0, gy: 0});
    magnet = el;
    if (!el) { tx = x; ty = y; return; }
    const p = pulled.get(el) || {ox: 0, oy: 0, gx: 0, gy: 0};
    // the middle it had before leaning, so the lean doesn't feed back into itself
    const cx = (r.left + r.width / 2 - p.ox) * z, cy = (r.top + r.height / 2 - p.oy) * z;
    tx = x + (cx - x) * .55; ty = y + (cy - y) * .55;
    if (calm()) return;
    const lean = v => Math.max(-6, Math.min(6, v * .2 / z));
    p.gx = lean(x - cx); p.gy = lean(y - cy); pulled.set(el, p);
  }
  const nudge = () => { if (!going) { going = true; requestAnimationFrame(frame); } };
  // a reload starts where the mouse was, instead of drawing the pointer in the top-left corner until it moves
  try { const s = JSON.parse(sessionStorage.ptr || 'null'); if (s) { x = rx = tx = s[0]; y = ry = ty = s[1]; } } catch (_) {}
  addEventListener('pagehide', () => { try { sessionStorage.ptr = JSON.stringify([x, y]); } catch (_) {} });
  nudge();

  addEventListener('pointermove', ev => {
    if (!live()) return;
    x = ev.clientX; y = ev.clientY;
    document.documentElement.classList.remove('cur-away');
    const t = ev.target, near = sel => t.closest && t.closest(sel);
    const off = t.disabled || (t.closest && t.closest('button:disabled,[aria-disabled=true]'));
    const edge = t.classList && [...t.classList].find(c => c in EDGE_TURN);
    ring.dataset.turn = edge ? EDGE_TURN[edge] : 0;
    const shape = edge ? 'rz' : off ? 'no' : near(SLIDER) ? 'slide' : near(TYPEABLE) ? 'text'
      : near(DRAGGABLE) ? 'drag' : near(PRESSABLE) ? 'hot' : '';
    for (const k of SHAPES) { ring.classList.toggle(k, k === shape); dot.classList.toggle(k, k === shape); }
    overCanvas = t.tagName === 'CANVAS';
    stick(t, off);
    nudge();
  }, true);
  const shapeTo = want => { for (const k of SHAPES) { ring.classList.toggle(k, k === want); dot.classList.toggle(k, k === want); } };
  for (const kind of ['drag', 'dragover', 'dragenter']) {
    addEventListener(kind, ev => {
      if (!live() || (!ev.clientX && !ev.clientY)) return;
      x = tx = ev.clientX; y = ty = ev.clientY;
      document.documentElement.classList.remove('cur-away');
      shapeTo('drag');
      nudge();
    }, true);
  }
  for (const kind of ['dragend', 'drop']) addEventListener(kind, () => shapeTo(''), true);

  addEventListener('pointerdown', () => { ring.classList.add('press'); dot.classList.add('press'); }, true);
  addEventListener('pointerup', () => { ring.classList.remove('press'); dot.classList.remove('press'); }, true);
  const away = () => { document.documentElement.classList.add('cur-away'); overCanvas = false; stick(document.body, true); nudge(); };
  addEventListener('pointerleave', away);
  document.addEventListener('mouseleave', away);
})();

// ---- the window's resize edges. Windows runs the drag itself, so it stays smooth.
(() => {
  // Each edge says which sides of the window it moves. Handing the drag to Windows did not
  // work - the browser control keeps the mouse - so the page tracks it and sets the bounds.
  const SIDES = {'e-w': [1, 0, 0, 0], 'e-e': [0, 0, 1, 0], 'e-n': [0, 1, 0, 0], 'e-s': [0, 0, 0, 1],
                 'e-nw': [1, 1, 0, 0], 'e-ne': [0, 1, 1, 0], 'e-sw': [1, 0, 0, 1], 'e-se': [0, 0, 1, 1]};
  const MIN_W = 720, MIN_H = 480;
  document.querySelectorAll('.redge').forEach(el => {
    const side = SIDES[[...el.classList].find(c => SIDES[c])];
    el.addEventListener('pointerdown', async ev => {
      ev.preventDefault();
      if (!window.pywebview) return;
      const start = await pywebview.api.win_bounds();
      if (!start) return;
      const dpr = devicePixelRatio || 1, x0 = ev.screenX, y0 = ev.screenY;
      const [w_, n_, e_, s_] = side;
      let want = null, queued = false;
      el.setPointerCapture(ev.pointerId);
      const push = () => {
        queued = false;
        if (want) pywebview.api.win_set_bounds(want.x, want.y, want.w, want.h);
      };
      const move = m => {
        const dx = Math.round((m.screenX - x0) * dpr), dy = Math.round((m.screenY - y0) * dpr);
        let {x, y, w, h} = start;
        if (w_) { const take = Math.min(dx, w - MIN_W); x += take; w -= take; }
        if (e_) w = Math.max(MIN_W, w + dx);
        if (n_) { const take = Math.min(dy, h - MIN_H); y += take; h -= take; }
        if (s_) h = Math.max(MIN_H, h + dy);
        want = {x, y, w, h};
        if (!queued) { queued = true; requestAnimationFrame(push); }   // at most one call a frame
      };
      const stop = () => {
        el.removeEventListener('pointermove', move);
        el.removeEventListener('pointerup', stop);
        el.removeEventListener('pointercancel', stop);
      };
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerup', stop);
      el.addEventListener('pointercancel', stop);
    });
  });
  // hovering or right-clicking Maximise offers the half and quarter layouts
  const menu = document.querySelector('.snapmenu'), maxBtn = document.querySelector('.wmax');
  let hold;
  window.menuJustOpened = () => !menu.hidden;
  const showMenu = () => { menu.hidden = false; };
  const hideSoon = () => { clearTimeout(hold); hold = setTimeout(() => menu.hidden = true, 320); };
  const keep = () => clearTimeout(hold);
  // Only ever on purpose: holding the button down, or right-clicking it. It used to come up
  // on hover alone, which meant it appeared over the page when the mouse merely passed by.
  maxBtn.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    keep(); hold = setTimeout(showMenu, 550);
  });
  maxBtn.addEventListener('pointerup', () => clearTimeout(hold));
  maxBtn.addEventListener('pointerleave', () => { clearTimeout(hold); hideSoon(); });
  maxBtn.addEventListener('contextmenu', e => { e.preventDefault(); keep(); showMenu(); });
  menu.addEventListener('pointerenter', keep);
  menu.addEventListener('pointerleave', hideSoon);
  menu.querySelectorAll('button').forEach(b => b.onclick = () => {
    menu.hidden = true;
    if (!window.pywebview) return;
    if (b.dataset.s === 'win') pywebview.api.win_snap_menu();      // hand over to Windows' own
    else pywebview.api.win_snap(b.dataset.s);
  });
  addEventListener('keydown', e => { if (e.key === 'Escape') menu.hidden = true; });

  const rf = document.querySelector('.refresh');
  if (rf) rf.addEventListener('click', () => {      // one turn per press
    rf.classList.remove('turn'); void rf.offsetWidth; rf.classList.add('turn');
  });
})();

// ---- drag a sheet's inner edge to make it wider or narrower
(() => {
  const grip = document.querySelector('.sh-grip');
  const open = () => document.querySelector('.sheet.on');
  const widthOf = s => { try { return +localStorage['sheetW:' + s.dataset.sheet] || 0; } catch (_) { return 0; } };
  const limit = w => Math.max(320, Math.min(w, innerWidth - 24));
  window.applySheetWidth = s => { const w = widthOf(s); if (w) s.style.width = limit(w) + 'px'; place(); };
  function place() {
    const s = open();
    if (!s) { grip.hidden = true; return; }
    grip.hidden = false;
    const cs = getComputedStyle(s), right = s.classList.contains('right');
    const inset = parseFloat(right ? cs.right : cs.left) || 12;
    grip.style.left = right ? '' : (inset + s.offsetWidth - 7) + 'px';
    grip.style.right = right ? (inset + s.offsetWidth - 7) + 'px' : '';
  }
  window.placeSheetGrip = place;
  grip.addEventListener('pointerdown', e => {
    const s = open(); if (!s) return;
    e.preventDefault(); grip.setPointerCapture(e.pointerId); grip.classList.add('dragging');
    const right = s.classList.contains('right'), x0 = e.clientX, w0 = s.getBoundingClientRect().width;
    const move = ev => {
      const w = limit(w0 + (right ? x0 - ev.clientX : ev.clientX - x0));
      s.style.width = w + 'px';
      try { localStorage['sheetW:' + s.dataset.sheet] = w; } catch (_) {}
      place();
    };
    const up = () => { grip.classList.remove('dragging'); grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up); };
    grip.addEventListener('pointermove', move); grip.addEventListener('pointerup', up);
  });
  grip.addEventListener('dblclick', () => {  // back to the standard width
    const s = open(); if (!s) return;
    s.style.width = ''; try { delete localStorage['sheetW:' + s.dataset.sheet]; } catch (_) {}
    place();
  });
  addEventListener('resize', place);
})();

function openSheet(name) {
  document.querySelectorAll('.sheet').forEach(s => s.classList.toggle('on', s.dataset.sheet === name));
  const open = document.querySelector('.sheet.on');
  if (open) applySheetWidth(open); else placeSheetGrip();
  scrim.classList.add('on');
  if (name === 'ask') loadClaude();
  if (name === 'ask') setTimeout(() => document.querySelector('.chat-in').focus(), 300);
}
function closeSheets() { document.querySelectorAll('.sheet').forEach(s => s.classList.remove('on')); scrim.classList.remove('on'); placeSheetGrip(); }
document.querySelector('.acct').onclick = () => openSheet('acct');
// Ask Claude is paid per question from the API credit, so say so before opening it (until told not to)
const AW = document.querySelector('.askwarn');
function askWarned() { try { return localStorage.askWarned === '1'; } catch (_) { return false; } }
document.querySelector('.ask').onclick = () => {
  if (askWarned()) return openSheet('ask');
  AW.querySelector('.aw-spend').textContent = '$' + (+(BB.spend || 0)).toFixed(2);
  AW.querySelector('.aw-never').checked = false;
  AW.hidden = false; AW.querySelector('.aw-go').focus();
};
AW.querySelector('.aw-go').onclick = () => {
  if (AW.querySelector('.aw-never').checked) try { localStorage.askWarned = '1'; } catch (_) {}
  AW.hidden = true; openSheet('ask');
};
AW.querySelector('.aw-no').onclick = () => { AW.hidden = true; document.querySelector('.ask').focus(); };
AW.addEventListener('keydown', ev => { if (ev.key === 'Escape') AW.querySelector('.aw-no').click(); });
scrim.onclick = closeSheets;
document.querySelectorAll('.sheet .x').forEach(b => b.onclick = closeSheets);
addEventListener('keydown', ev => { if (ev.key === 'Escape') closeSheets(); });

const A = document.querySelector('[data-sheet=acct]');
A.querySelector('.av').textContent = (BB.name || '?')[0].toUpperCase();
A.querySelector('.a-name').textContent = BB.name || 'Student';
A.querySelector('.a-user').textContent = [BB.user, BB.programme].filter(Boolean).join(' · ');
A.querySelector('.a-sync').textContent = BB.synced;
A.querySelector('.a-spend').textContent = '$' + Number(BB.spend || 0).toFixed(2);
document.querySelector('.acct .av').textContent = (BB.name || '?')[0].toUpperCase();
const SY = A.querySelector('.s-sync'), NP = A.querySelector('.s-notes'), NPV = A.querySelector('.s-notes-v');
SY.value = String(BB.settings.sync_hours); NP.value = BB.settings.notes_per_sync; NPV.textContent = NP.value;
const saveSettings = () => window.pywebview && pywebview.api.save_settings(
  {sync_hours: +SY.value, notes_per_sync: +NP.value, model: BB.settings.model,
   effort: BB.settings.effort, sounds: BB.settings.sounds, volume: BB.settings.volume});
SY.onchange = saveSettings; NP.oninput = () => { NPV.textContent = NP.value; }; NP.onchange = saveSettings;
A.querySelector('.s-files').onclick = async () => {
  const r = await pywebview.api.open_materials(); if (r && r !== 'ok') A.querySelector('.s-files-msg').textContent = r; };
A.querySelector('.s-files-path').textContent = BB.materials;
A.querySelector('.s-files-move').onclick = async () => {
  const m = A.querySelector('.s-files-msg'); m.textContent = 'Pick a folder…';
  const r = await pywebview.api.choose_materials();
  if (r.error) { m.textContent = r.error; return; }
  if (!r.moved) { m.textContent = ''; return; }
  A.querySelector('.s-files-path').textContent = BB.materials = r.path;
  m.textContent = 'Moved. Reloading so the file links point there…';
  setTimeout(() => location.reload(), 1200);
};
A.querySelector('.s-ver').textContent = 'Version ' + BB.version;
A.querySelector('.s-check').onclick = async () => {
  const m = A.querySelector('.s-check-msg'); m.textContent = 'Checking…';
  const u = await pywebview.api.check_update();
  if (u && u.version) { m.textContent = ''; closeSheets(); bbUpdate(u); }
  else m.textContent = u && u.error ? u.error : 'You have the latest version.';
};
A.querySelector('.s-signout').onclick = async () => {
  if (!confirm('Sign out of Blackboard on this computer? You will be asked to sign in on the next sync.')) return;
  A.querySelector('.s-signout-msg').textContent = await pywebview.api.sign_out();
};

// ---- the four settings sections
A.querySelectorAll('.set-tab').forEach(t => t.onclick = () => {
  A.querySelectorAll('.set-tab').forEach(b => { const on = b === t; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); });
  A.querySelectorAll('.set-pane').forEach(pn => pn.classList.toggle('on', pn.dataset.s === t.dataset.s));
  if (t.dataset.s === 'claude') loadClaude();
  if (t.dataset.s === 'tabs') drawTabs();
});

// ---- Settings > Claude: accounts, model, effort, what it has cost
const AC = A.querySelector('.acc-list'), ACMSG = A.querySelector('.acc-msg');
const MODEL = A.querySelector('.s-model'), EFFORT = A.querySelector('.s-effort'), MNOTE = A.querySelector('.s-model-note');
let MODELS = [];

function drawAccounts(a) {
  AC.innerHTML = '';
  if (!a.list.length) {
    AC.innerHTML = '<div class="locked"><span>🔒</span><span>No Claude account yet. The AI features stay off until you add one.</span></div>';
    return;
  }
  for (const x of a.list) {
    const row = document.createElement('div');
    row.className = 'acc' + (x.id === a.active ? ' on' : '');
    row.innerHTML = '<div class="grow"><b></b><span class="hint"></span></div>';
    row.querySelector('b').textContent = x.label;
    row.querySelector('.hint').textContent = 'key ending ' + (x.hint || '').replace('…', '') + (x.id === a.active ? ' · in use' : '');
    if (x.id !== a.active) {
      const use = document.createElement('button');
      use.className = 'ghost use'; use.textContent = 'Use';
      use.onclick = async () => { await pywebview.api.account_use(x.id); loadClaude(); gateAI(); };
      row.append(use);
    }
    const rm = document.createElement('button');
    rm.className = 'ghost use'; rm.textContent = 'Remove';
    rm.onclick = async () => {
      if (!confirm('Remove "' + x.label + '" from this computer?')) return;
      await pywebview.api.account_remove(x.id); loadClaude(); gateAI();
    };
    row.append(rm);
    AC.append(row);
  }
}

function drawUsage(u) {
  const list = A.querySelector('.u-list');
  const money = n => '$' + Number(n || 0).toFixed(2);
  const month = m => { const [y, mo] = m.split('-');
    return new Date(y, mo - 1, 1).toLocaleDateString(undefined, {month: 'long', year: 'numeric'}); };
  let html = '<span>This month</span><span class="c">' + money(u.month) + '</span>'
           + '<span>Since you installed it</span><span class="c">' + money(u.total) + '</span>';
  if (u.months.length > 1) html += u.months.map(m => '<span class="m">' + month(m.month) + '</span><span class="c">' + money(m.cost) + '</span>').join('');
  list.innerHTML = html;
  A.querySelector('.a-spend').textContent = money(u.month);
}

function modelNote() {
  const m = MODELS.find(x => x.id === MODEL.value);
  MNOTE.textContent = m ? m.note + ' Costs $' + m.in.toFixed(2) + ' per million tokens read and $'
    + m.out.toFixed(2) + ' per million written — roughly 750,000 words either way.' : '';
}

async function loadClaude() {
  if (!window.pywebview) return;
  const a = await pywebview.api.accounts(), m = await pywebview.api.models();
  if (!a || !m) return;
  drawAccounts(a);
  MODELS = m.models;
  if (!MODEL.options.length) {
    MODEL.innerHTML = m.models.map(x => '<option value="' + x.id + '">' + x.label + '</option>').join('');
    EFFORT.innerHTML = m.efforts.map(x => '<option value="' + x + '">' + x[0].toUpperCase() + x.slice(1) + '</option>').join('');
  }
  MODEL.value = m.model; EFFORT.value = m.effort; modelNote();
  const u = await pywebview.api.usage(); if (u) drawUsage(u);
  fillAiBar(m, a, u || {});
}

MODEL.onchange = () => { modelNote(); BB.settings.model = MODEL.value; saveSettings(); };
EFFORT.onchange = () => { BB.settings.effort = EFFORT.value; saveSettings(); };

A.querySelector('.acc-add').onclick = async () => {
  const label = A.querySelector('.acc-label'), key = A.querySelector('.acc-key');
  if (!key.value.trim()) { ACMSG.className = 'msg bad'; ACMSG.textContent = 'Paste your Claude API key first.'; return; }
  ACMSG.className = 'msg'; ACMSG.textContent = 'Checking with Anthropic…';
  const r = await pywebview.api.account_add(label.value, key.value);
  if (r && r.ok) {
    ACMSG.className = 'msg'; ACMSG.textContent = 'Signed in.';
    label.value = ''; key.value = '';
    SFX.play('done'); loadClaude(); gateAI();
  } else {
    ACMSG.className = 'msg bad'; ACMSG.textContent = (r && r.error) || 'Could not sign in.';
    SFX.play('error');
  }
};

// ---- the same controls at the top of Ask Claude, kept in step with the Settings ones
const ASK = document.querySelector('[data-sheet=ask]');
const AIMODEL = ASK.querySelector('.ai-model'), AIEFF = ASK.querySelector('.ai-effort');
const AIWHO = ASK.querySelector('.ai-who'), AIACCS = ASK.querySelector('.ai-accs');

function fillAiBar(m, accounts, usage) {
  if (!AIMODEL.options.length) {
    AIMODEL.innerHTML = MODEL.innerHTML;
    AIEFF.innerHTML = EFFORT.innerHTML;
  }
  AIMODEL.value = m.model; AIEFF.value = m.effort;
  const hit = accounts.list.find(x => x.id === accounts.active);
  AIWHO.classList.toggle('in', !!hit);
  AIWHO.querySelector('.ai-acc').textContent = hit ? hit.label : 'Not signed in';
  ASK.querySelector('.ai-spend').textContent = '$' + Number(usage.month || 0).toFixed(2) + ' this month';
}

AIMODEL.onchange = () => { MODEL.value = AIMODEL.value; MODEL.onchange(); };
AIEFF.onchange = () => { EFFORT.value = AIEFF.value; EFFORT.onchange(); };

AIWHO.onclick = async () => {
  if (!AIACCS.hidden) { AIACCS.hidden = true; AIWHO.setAttribute('aria-expanded', 'false'); return; }
  if (!window.pywebview) return;
  const a = await pywebview.api.accounts(); if (!a) return;
  AIACCS.innerHTML = '';
  for (const x of a.list) {
    const b = document.createElement('button');
    b.className = x.id === a.active ? 'on' : '';
    b.innerHTML = '<b></b><span class="hint"></span>';
    b.querySelector('b').textContent = x.label;
    b.querySelector('.hint').textContent = x.id === a.active ? 'in use' : 'key ending ' + (x.hint || '').replace('\u2026', '');
    b.onclick = async () => {
      if (x.id !== a.active) await pywebview.api.account_use(x.id);
      AIACCS.hidden = true; AIWHO.setAttribute('aria-expanded', 'false');
      loadClaude(); gateAI();
    };
    AIACCS.append(b);
  }
  if (a.list.length) AIACCS.insertAdjacentHTML('beforeend', '<div class="sep"></div>');
  const add = document.createElement('button');
  add.innerHTML = '<b>Add or remove accounts…</b>';
  add.onclick = () => {
    AIACCS.hidden = true; AIWHO.setAttribute('aria-expanded', 'false');
    openSheet('acct');
    A.querySelector('.set-tab[data-s=claude]').click();
  };
  AIACCS.append(add);
  AIACCS.hidden = false; AIWHO.setAttribute('aria-expanded', 'true');
};
document.addEventListener('click', e => {
  if (!AIACCS.hidden && !e.target.closest('.ai-accs,.ai-who')) {
    AIACCS.hidden = true; AIWHO.setAttribute('aria-expanded', 'false');
  }
});

// the AI features are off until an account is signed in, and the page says so
async function gateAI() {
  if (!window.pywebview) return;
  const u = await pywebview.api.usage(); if (!u) return;
  const hints = document.querySelector('.chat-hints');
  let note = hints && hints.querySelector('.locked');
  if (!u.signed_in && hints && !note) {
    note = document.createElement('div');
    note.className = 'locked';
    note.innerHTML = '<span>🔒</span><span>Sign in to your Claude account under <b>Settings → Claude</b> and the AI features switch on.</span>';
    hints.prepend(note);
  } else if (u.signed_in && note) note.remove();
  if (hints) hints.hidden = false;
  const inp = document.querySelector('.chat-in');
  if (inp) {
    inp.dataset.ph = inp.dataset.ph || inp.placeholder;
    inp.disabled = !u.signed_in;
    inp.placeholder = u.signed_in ? inp.dataset.ph : 'Sign in to Claude in Settings first';
  }
}

// ---- Settings > Look
const LOOK_DEF = {accent: '#d71921', zoom: 100, dots: true, anim: true, intro: true, cursor: 'live'};
// it used to be a yes/no switch, so turn an old saved value into a shape
const curStyle = v => (v === true || v === undefined ? 'live' : v === false ? 'off' : v);
const ACCENTS = ['#d71921', '#e2571e', '#c98a00', '#1f9d55', '#1565d8', '#7c3aed', '#d6336c'];
const look = () => { try { return {...LOOK_DEF, ...JSON.parse(localStorage.look || '{}')}; } catch (_) { return {...LOOK_DEF}; } };
function applyLook(L) {
  const r = document.documentElement;
  r.style.setProperty('--red', L.accent);
  r.style.setProperty('--dot', L.dots ? '' : 'transparent');
  r.classList.toggle('no-anim', !L.anim);
  const cs = curStyle(L.cursor);
  r.classList.toggle('fancy-cursor', cs !== 'off' && cs !== 'live');
  ['ring', 'dot', 'arrow', 'live'].forEach(k => r.classList.toggle('cur-' + k, k === cs));
  r.style.zoom = L.zoom === 100 ? '' : L.zoom / 100;
  if (window.bbPlaceInd) bbPlaceInd();
  if (window.placeSheetGrip) placeSheetGrip();
}
function saveLook(patch) {
  const L = {...look(), ...patch};
  try { localStorage.look = JSON.stringify(L); } catch (_) {}
  applyLook(L); drawLook(); return L;
}
function drawLook() {
  const L = look();
  const sw = A.querySelector('.swatches');
  if (!sw.children.length) ACCENTS.forEach(c => {
    const b = document.createElement('button');
    b.className = 'swatch'; b.style.background = c; b.dataset.c = c;
    b.setAttribute('aria-label', 'Highlight colour ' + c);
    b.onclick = () => saveLook({accent: c});
    sw.append(b);
  });
  [...sw.children].forEach(b => b.setAttribute('aria-pressed', b.dataset.c === L.accent));
  const Z = A.querySelector('.s-zoom'), V = A.querySelector('.s-vol');
  Z.value = L.zoom; A.querySelector('.s-zoom-v').textContent = L.zoom + '%';
  V.value = BB.settings.volume; A.querySelector('.s-vol-v').textContent = BB.settings.volume + '%';
  A.querySelector('.s-dots').setAttribute('aria-pressed', !!L.dots);
  A.querySelector('.s-anim').setAttribute('aria-pressed', !!L.anim);
  A.querySelector('.s-intro').setAttribute('aria-pressed', !!L.intro);
  A.querySelector('.s-cursor').value = curStyle(L.cursor);
  A.querySelector('.s-sfx').setAttribute('aria-pressed', !!BB.settings.sounds);
}
A.querySelector('.s-zoom').oninput = e => { A.querySelector('.s-zoom-v').textContent = e.target.value + '%'; };
A.querySelector('.s-zoom').onchange = e => saveLook({zoom: +e.target.value});
A.querySelector('.s-dots').onclick = () => saveLook({dots: !look().dots});
A.querySelector('.s-anim').onclick = () => saveLook({anim: !look().anim});
A.querySelector('.s-intro').onclick = () => saveLook({intro: !look().intro});
A.querySelector('.s-cursor').onchange = e => saveLook({cursor: e.target.value});
A.querySelector('.s-sfx').onclick = () => { BB.settings.sounds = !BB.settings.sounds; saveSettings(); drawLook();
  if (BB.settings.sounds) SFX.play('on'); };
A.querySelector('.s-vol').oninput = e => { A.querySelector('.s-vol-v').textContent = e.target.value + '%'; BB.settings.volume = +e.target.value; };
A.querySelector('.s-vol').onchange = () => { saveSettings(); SFX.play('tap'); };
A.querySelector('.s-reset-look').onclick = () => {
  try { delete localStorage.look; } catch (_) {}
  applyLook(LOOK_DEF); drawLook();
};

// ---- Settings > Tabs: which ones show, and in what order
const tabsPref = () => { try { return JSON.parse(localStorage.tabsPref || '{}'); } catch (_) { return {}; } };
function saveTabs(p) {
  try { localStorage.tabsPref = JSON.stringify({...tabsPref(), ...p}); } catch (_) {}
  applyTabs(); drawTabs();
}
// The saved order first, then anything it doesn't mention. The tab bar and the settings
// list both read this, so what you drag is exactly what you get.
function tabOrder() {
  const want = tabsPref().order || [], seen = new Set(), out = [];
  for (const k of want) {
    const b = bbTabs.find(x => x.dataset.tab === k);
    if (b && !seen.has(k)) { out.push(b); seen.add(k); }
  }
  for (const b of bbTabs) if (!seen.has(b.dataset.tab)) out.push(b);
  return out;
}
function applyTabs() {
  if (!window.bbTabs) return;
  const active = bbTabs.find(b => b.classList.contains('on'));
  const now = active ? active.dataset.tab : null;
  const bar = document.querySelector('nav'), hidden = tabsPref().hidden || [];
  tabOrder().forEach(b => bar.append(b));
  bbTabs.forEach(b => b.hidden = hidden.includes(b.dataset.tab) && b.dataset.tab !== now);
  bbPlaceInd();
}
function drawTabs() {
  const list = A.querySelector('.tab-list'), p = tabsPref(), hidden = p.hidden || [];
  list.innerHTML = '';
  if (!window.bbTabs) return;
  tabOrder().forEach(b => {
    const k = b.dataset.tab;
    const row = document.createElement('div');
    row.className = 'tab-row'; row.dataset.tab = k;
    row.innerHTML = '<span class="grab">⣿</span><label class="grow"></label><input type="checkbox">';
    row.querySelector('label').textContent = b.textContent.trim();
    const box = row.querySelector('input');
    box.checked = !hidden.includes(k);
    box.onchange = () => {
      const h = new Set(tabsPref().hidden || []);
      box.checked ? h.delete(k) : h.add(k);
      if (!box.checked && b.classList.contains('on')) {         // don't hide what's on screen: move off it first
        const next = bbTabs.find(x => !h.has(x.dataset.tab));
        if (next) bbShow(next.dataset.tab); else { h.delete(k); box.checked = true; }
      }
      saveTabs({hidden: [...h]});
      SFX.play(box.checked ? 'on' : 'off');
    };
    row.onpointerdown = e => {
      if (e.target.closest('input')) return;            // the tick box is not a handle
      e.preventDefault();
      row.classList.add('drag');
      const move = m => {
        const under = document.elementFromPoint(m.clientX, m.clientY);
        const over = under && under.closest('.tab-row');
        if (!over || over === row || over.parentElement !== list) return;
        const r = over.getBoundingClientRect();
        list.insertBefore(row, m.clientY - r.top > r.height / 2 ? over.nextSibling : over);
      };
      const up = () => {
        row.classList.remove('drag');
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        document.removeEventListener('pointercancel', up);
        saveTabs({order: [...list.children].map(r => r.dataset.tab)});
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
      document.addEventListener('pointercancel', up);
    };
    list.append(row);
  });
}
A.querySelector('.s-reset-tabs').onclick = () => { try { delete localStorage.tabsPref; } catch (_) {} location.reload(); };

// ---- sound effects, synthesised so the app carries no audio files
const SFX = (() => {
  // Sounds are built from tones (one oscillator, or two slightly detuned) and filtered noise ticks,
  // through a light generated reverb and a compressor.
  let ctx = null, out = null, verb = null, nbuf = null;
  const ac = () => {
    try {
      if (!ctx) {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
        const comp = ctx.createDynamicsCompressor(); comp.threshold.value = -16; comp.ratio.value = 4; comp.connect(ctx.destination);
        out = ctx.createGain(); out.connect(comp);
        const len = Math.floor(ctx.sampleRate * 1.6), ir = ctx.createBuffer(2, len, ctx.sampleRate);
        for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3.2); }
        const conv = ctx.createConvolver(); conv.buffer = ir;
        verb = ctx.createGain(); verb.gain.value = .1; verb.connect(conv); conv.connect(comp);
      }
      if (ctx.state === 'suspended') ctx.resume();
      return ctx;
    } catch (_) { return null; }
  };
  const env = (g, t, a, d, peak) => { g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(Math.max(.0002, peak), t + a); g.gain.exponentialRampToValueAtTime(.0001, t + a + d); };
  const route = (node, pan, wet) => {
    const p = ctx.createStereoPanner(); p.pan.value = pan || 0; node.connect(p); p.connect(out);
    if (wet !== 0) { const s = ctx.createGain(); s.gain.value = wet ?? 1; p.connect(s); s.connect(verb); }
  };
  function tone(t, v, vol) {
    const a = v.a || .006, g = ctx.createGain();
    env(g, t, a, v.d, vol * (v.g ?? .3) * (v.det ? .6 : 1)); route(g, v.pan, v.wet);
    for (const cents of v.det ? [-v.det, v.det] : [0]) {
      const o = ctx.createOscillator(); o.type = v.w || 'sine'; o.detune.value = cents;
      o.frequency.setValueAtTime(v.f, t); if (v.to) o.frequency.exponentialRampToValueAtTime(v.to, t + a + v.d * .7);
      o.connect(g); o.start(t); o.stop(t + a + v.d + .05);
    }
  }
  function noise(t, v, vol) {
    if (!nbuf) { nbuf = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate); const d = nbuf.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1; }
    const a = v.a || .002, s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    s.buffer = nbuf; s.loop = true; f.type = v.ft || 'bandpass'; f.Q.value = v.q || 1;
    f.frequency.setValueAtTime(v.f || 3000, t); if (v.to) f.frequency.exponentialRampToValueAtTime(v.to, t + a + v.d);
    env(g, t, a, v.d, vol * (v.g ?? .2)); s.connect(f); f.connect(g); route(g, v.pan, v.wet ?? .3);
    s.start(t); s.stop(t + a + v.d + .05);
  }
  const T = (at, v) => [tone, at, v], N = (at, v) => [noise, at, v];
  // Nothing style: dry and precise. Short pure pips on a fixed grid, like a dot matrix lighting up,
  // a tight tick under each one, and only a breath of room.
  const pip = (at, f, o = {}) => T(at, {f, d: o.d || .045, g: o.g ?? .3, a: .002, pan: o.pan, wet: o.wet ?? .12});
  const tick = (at, o = {}) => N(at, {ft: 'highpass', f: o.f || 6000, q: .7, d: o.d || .012, g: o.g ?? .45, wet: 0});
  const SOUNDS = {
    tap:   [tick(0, {g: .6}), pip(0, 2400, {d: .018, g: .14, wet: 0})],
    tab:   [tick(0), pip(0, 1760, {d: .03, g: .24})],
    on:    [tick(0), pip(0, 1320, {d: .03}), pip(.055, 1760, {d: .04})],
    off:   [tick(0), pip(0, 1760, {d: .03}), pip(.055, 1320, {d: .04, g: .24})],
    open:  [tick(0, {f: 4000}), pip(0, 880, {d: .03, g: .22}), pip(.04, 1320, {d: .03, g: .22}), pip(.08, 1760, {d: .05, g: .2})],
    close: [tick(0, {f: 4000}), pip(0, 1760, {d: .03, g: .2}), pip(.04, 1320, {d: .03, g: .2}), pip(.08, 880, {d: .05, g: .2})],
    send:  [tick(0), pip(0, 1047, {d: .025}), pip(.035, 1568, {d: .025}), pip(.07, 2093, {d: .06, g: .22, wet: .25})],
    reply: [pip(0, 1568, {d: .05, g: .26, pan: -.2}), pip(.09, 1568, {d: .05, g: .2, pan: .2}), pip(.18, 2093, {d: .09, g: .18, wet: .3})],
    done:  [tick(0), ...[1047, 1319, 1568, 2093].map((f, i) => pip(i * .06, f, {d: .04, g: .24, pan: -.3 + i * .2})),
            pip(.3, 2093, {d: .25, g: .1, wet: .35})],
    error: [tick(0, {f: 2500, g: .5}), T(0, {f: 330, w: 'square', d: .05, g: .1, wet: 0}), T(.09, {f: 330, w: 'square', d: .05, g: .1, wet: 0}),
            T(.18, {f: 247, w: 'square', d: .1, g: .1, wet: 0})],
    // the boot sequence counts in on a grid, then lands on a held chord
    boot:  [...[0, .09, .18, .27, .36, .45].map((at, i) => tick(at, {g: .25 + i * .05, f: 7000 - i * 500})),
            ...[523, 784, 1047, 1568].map((f, i) => pip(.54 + i * .09, f, {d: .05, g: .26, pan: -.3 + i * .2})),
            T(.9, {f: 1047, d: .9, g: .1, a: .01, det: 3, wet: .3}), T(.9, {f: 1568, d: .8, g: .07, a: .01, wet: .3})],
    flash: [tick(0, {g: .5}), pip(0, 2093, {d: .03, g: .22}), pip(.05, 3136, {d: .12, g: .14, wet: .3})],
    scroll: [tick(0, {f: 5200, d: .008, g: .32}), pip(0, 1900, {d: .012, g: .07, wet: 0})],
    edge:   [N(0, {ft: 'lowpass', f: 900, q: .9, d: .05, g: .4, wet: .05}), pip(0, 440, {d: .05, g: .14, wet: .05})],
  };
  return {play(name, k = 1, lvl = 1) {
    const st = BB.settings;
    if (!st.sounds || !(st.volume > 0) || !SOUNDS[name]) return;
    const c = ac(); if (!c) return;
    const vol = (st.volume / 100) * .25 * lvl, t0 = c.currentTime + .01;
    for (const [voice, at, v] of SOUNDS[name]) voice(t0 + at, k === 1 ? v : {...v, f: v.f * k, to: v.to && v.to * k}, vol);
  }};
})();
window.SFX = SFX;
// Scrolling clicks like a wheel with notches: the same tick for every 56px travelled, never more
// than one every 30 ms, and a soft bump when a list hits its end.
(() => {
  const NOTCH = 56, pos = new WeakMap();
  let lastTick = 0, lastEdge = 0;
  document.addEventListener('scroll', e => {
    const el = e.target === document ? document.scrollingElement : e.target;
    if (!el || !el.scrollHeight) return;
    const y = el.scrollTop, p = pos.get(el) || {y, run: 0};
    const dy = y - p.y; p.y = y; p.run += Math.abs(dy); pos.set(el, p);
    const now = performance.now();
    if (p.run >= NOTCH && now - lastTick > 30) { SFX.play('scroll'); p.run = 0; lastTick = now; }
    const atEnd = dy && (y <= 0 || y >= el.scrollHeight - el.clientHeight - 1);
    if (atEnd && now - lastEdge > 400) { SFX.play('edge'); lastEdge = now; p.run = 0; }
  }, {capture: true, passive: true});
})();

// one listener covers the whole app: it works out which sound a click deserves
addEventListener('click', ev => {
  const t = ev.target.closest('button'); if (!t || t.disabled) return;
  let s = 'tap';
  if (t.dataset.tab && t.closest('nav')) s = 'tab';
  else if (t.classList.contains('acct') || t.classList.contains('ask')) s = 'open';
  else if (t.classList.contains('x')) s = 'close';
  else if (t.classList.contains('send')) s = 'send';
  else if (t.classList.contains('refresh')) s = 'tap';
  else if (t.classList.contains('sw') || t.classList.contains('theme')) s = t.getAttribute('aria-pressed') === 'true' ? 'off' : 'on';
  else if (t.classList.contains('swatch') || t.classList.contains('chip') || t.classList.contains('set-tab')) return;
  SFX.play(s);
}, true);

applyLook(look()); applyTabs(); drawLook(); drawTabs(); gateAI();
bbMarks.push('ready');
// start-up chime, once per launch (reloads after a sync keep the same session, so they stay quiet)
try { if (!sessionStorage.bootSound) { sessionStorage.bootSound = '1'; SFX.play('boot'); } } catch (_) {}
addEventListener('pywebviewready', () => {
  gateAI(); loadClaude();
  const theme = () => document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  pywebview.api.note_theme(theme());                 // so the next launch opens in this colour
  pywebview.api.note_start(bbMarks.join(' | '));
  document.addEventListener('click', e => {
    if (e.target.closest('.theme:not(.refresh)')) setTimeout(() => pywebview.api.note_theme(theme()), 50);
  });
});   // the bridge isn't ready when this script first runs

// ---- Ask Claude
const chat = [], LOG = document.querySelector('.chat-log'), IN = document.querySelector('.chat-in');
function bubble(cls, html) { const d = document.createElement('div'); d.className = 'msg ' + cls; d.innerHTML = html; LOG.append(d); LOG.scrollTop = LOG.scrollHeight; return d; }
async function send(q) {
  q = (q || '').trim(); if (!q || !window.pywebview) return;
  document.querySelector('.chat-hints').hidden = true;
  chat.push({role: 'user', content: q});
  bubble('me', q.replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c])));
  IN.value = ''; IN.style.height = '';
  const wait = bubble('them wait', '<i></i><i></i><i></i>');
  const r = await pywebview.api.ask(chat);
  wait.remove();
  if (r.error) { chat.pop(); bubble('them err', r.error); return; }
  chat.push({role: 'assistant', content: r.md});
  bubble('them', r.html);
  LOG.querySelectorAll('a[href^="http"]').forEach(a => a.target = '_blank');
}
document.querySelector('.chat-form').onsubmit = ev => { ev.preventDefault(); send(IN.value); };
IN.addEventListener('keydown', ev => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); send(IN.value); } });
IN.addEventListener('input', () => { IN.style.height = ''; IN.style.height = Math.min(IN.scrollHeight, 140) + 'px'; });
document.querySelectorAll('.chat-hints button').forEach(b => b.onclick = () => send(b.textContent));

// ---- Notes: make notes for one file on demand
document.addEventListener('click', async ev => {
  const b = ev.target.closest('.mk'); if (!b || !window.pywebview) return;
  b.disabled = true; b.textContent = 'Writing…';
  const r = await pywebview.api.make_notes(b.dataset.item, b.dataset.view);
  if (r !== 'ok') { b.disabled = false; b.textContent = 'Make notes'; b.title = r; alert(r); }
});
// after a reload from the app (sync / new notes), reopen where you were
if (location.hash.startsWith('#updated:')) {
  const view = location.hash.split(':')[1];
  setTimeout(() => { show(view.startsWith('notes-') ? 'notes' : 'courses', true); openView(view); }, 50);
}
window.addEventListener('pywebviewready', () => {
  if (!sessionStorage.counted) { sessionStorage.counted = 1; pywebview.api.track('open'); }
  pywebview.api.activity().then(drawAllHeat);
});
// ---- Deadlines and Plans: "Mark done" ticks an assignment off. The page shows it at once, then the app
// rebuilds the dashboard so the red dot, countdown and Upcoming move on to the next one, and we come back to
// the same spot.
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-done]'); if (!b) return;
  e.preventDefault(); e.stopPropagation();          // inside a plan's summary: don't open or close the card
  const on = b.getAttribute('aria-pressed') !== 'true';
  b.setAttribute('aria-pressed', on); b.textContent = on ? '✓ Done' : 'Mark done';
  b.closest('tr, details')?.classList.toggle('is-done', on);
  if (on && window.SFX) setTimeout(() => SFX.play('done'), 40);
  const api = window.pywebview?.api; if (!api?.set_done) return;
  b.disabled = true;
  const r = await api.set_done(b.dataset.done, on);
  if (r === 'ok') { try { sessionStorage.keepScroll = scrollY; } catch (_) {} location.reload(); } else b.disabled = false;
}, true);
try { if (sessionStorage.keepScroll) { const y = +sessionStorage.keepScroll; delete sessionStorage.keepScroll; setTimeout(() => scrollTo(0, y), 120); } } catch (_) {}
// ---- This week > Note map: every note a dot, joined up automatically. Study notes hang off their module,
// Notebook notes join what they [[link]] to and any module they're filed under or mention, and any two notes
// that share enough unusual words (TF-IDF cosine) get a fainter line. Click a dot to open the note.
// While a note is open, the map button above Ask Claude pops up a small copy showing where you are.
(() => {
  const CV = document.querySelector('.nmap'); if (!CV) return;
  const META = document.querySelector('.map-meta');
  const DATA = JSON.parse(document.getElementById('map-data').textContent);
  const STOP = new Set(('about above after again against also because been before being below between both could does doing down during each '
    + 'from further have having here hers herself himself into itself just more most myself once only other ought ours over same should some '
    + 'such than that their theirs them themselves then there these they this those through under until very what when where which while '
    + 'whom will with would your yours yourself yourselves note notes week lecture slide slides example examples using used use make made '
    + 'like also many much well able will within without each every first second third part section page summary point points key one two '
    + 'three include includes including following based need needs want know mean means things thing time different important main').split(' '));
  const words = t => (t.toLowerCase().replace(/```[\s\S]*?```|`[^`]*`|!?\[\[[^\]]*\]\]|https?:\S+/g, ' ').match(/[a-z][a-z0-9]{3,}/g) || [])
    .filter(w => !STOP.has(w)).map(w => w.length > 4 && w.endsWith('s') && !w.endsWith('ss') ? w.slice(0, -1) : w);
  const LINK = /\[\[([^\]|#\n]+)/g;
  let nodes = [], edges = [], drag = null, alpha = 0, raf = 0, nbNotes = [], here = null;

  function build() {
    const old = new Map(nodes.map(n => [n.id, n]));
    const map = new Map(), E = new Map();
    const add = (id, o) => { if (!map.has(id)) { const p = old.get(id); map.set(id, {id, deg: 0, vx: 0, vy: 0,
      x: p ? p.x : (Math.random() - .5) * 260, y: p ? p.y : (Math.random() - .5) * 160, ...o}); } return map.get(id); };
    const join = (a, b, kind) => { if (!a || !b || a === b) return; const k = a.id < b.id ? a.id + '|' + b.id : b.id + '|' + a.id;
      if (!E.has(k) || kind !== 'sim') E.set(k, [a, b, kind]); };
    const hubs = DATA.modules.map(m => add('m:' + m.key, {kind: 'mod', label: m.name, key: m.key}));
    const hubFor = s => { s = (s || '').toLowerCase(); return hubs.find(h => s.includes(h.label.toLowerCase())); };
    const docs = [];
    DATA.study.forEach((n, i) => { const d = add('s:' + i, {kind: 'study', label: n.topic || n.title, key: n.key, title: n.title});
      join(d, map.get('m:' + n.key), 'mod'); docs.push([d, n.text]); });
    nbNotes.filter(n => !/^(Start here|Linking notes)$/.test(n.name)).forEach(n => {
      const d = add('n:' + n.name.toLowerCase(), {kind: 'nb', label: n.name.split('/').pop(), name: n.name});
      join(d, hubFor(n.name.split('/')[0]) || hubFor(n.text.slice(0, 600)), 'mod'); docs.push([d, n.text]);
    });
    // written [[links]] between Notebook notes
    const nbs = [...map.values()].filter(x => x.kind === 'nb');
    nbNotes.forEach(n => { const a = map.get('n:' + n.name.toLowerCase());
      for (const m of n.text.matchAll(LINK)) { const t = m[1].trim().toLowerCase();
        join(a, nbs.find(x => x.name.toLowerCase() === t) || nbs.find(x => x.label.toLowerCase() === t), 'link'); } });
    // shared topics: TF-IDF vectors, then each note's two closest neighbours if they're close enough
    const tfs = docs.map(([, t]) => { const tf = new Map(); words(t).forEach(w => tf.set(w, (tf.get(w) || 0) + 1)); return tf; });
    const df = new Map(); tfs.forEach(tf => tf.forEach((_, w) => df.set(w, (df.get(w) || 0) + 1)));
    const vecs = tfs.map(tf => { const v = new Map(); let len = 0;
      tf.forEach((c, w) => { const x = (1 + Math.log(c)) * Math.log((docs.length + 1) / df.get(w)); if (x > 0) { v.set(w, x); len += x * x; } });
      len = Math.sqrt(len) || 1; v.forEach((x, w) => v.set(w, x / len)); return v; });
    const cos = (a, b) => { let s = 0; const [p, q] = a.size < b.size ? [a, b] : [b, a]; p.forEach((x, w) => { const y = q.get(w); if (y) s += x * y; }); return s; };
    vecs.forEach((v, i) => vecs.map((u, j) => [j, i === j ? 0 : cos(v, u)]).sort((a, b) => b[1] - a[1]).slice(0, 2)
      .forEach(([j, s]) => { if (s >= .12) join(docs[i][0], docs[j][0], 'sim'); }));
    nodes = [...map.values()]; edges = [...E.values()];
    edges.forEach(([a, b]) => { a.deg++; b.deg++; });
    const nS = docs.filter(([d]) => d.kind === 'study').length, nN = docs.length - nS, nSim = edges.filter(e => e[2] !== 'mod').length;
    META.textContent = `${nS} study note${nS === 1 ? '' : 's'} · ${nN} notebook note${nN === 1 ? '' : 's'} · ${nSim} connection${nSim === 1 ? '' : 's'} found`
      + (docs.length ? '' : ' · make notes in the Notes or Notebook tab and they show up here');
    CV.setAttribute('aria-label', 'Map of your notes. ' + META.textContent);
    alpha = 1; for (let i = 0; i < 160; i++) step(); kick();
  }
  function step() {
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j]; let dx = b.x - a.x, dy = b.y - a.y; const d2 = dx * dx + dy * dy || 1, d = Math.sqrt(d2), f = 1800 / d2 * alpha;
      dx /= d; dy /= d; a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
    }
    edges.forEach(([a, b, k]) => { const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, f = (d - (k === 'mod' ? 55 : 85)) * (k === 'sim' ? .008 : .025) * alpha;
      a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f; });
    nodes.forEach(n => { if (n === drag?.node) return; n.vx -= n.x * .006 * alpha; n.vy -= n.y * .012 * alpha;
      n.vx *= .8; n.vy *= .8; n.x += n.vx; n.y += n.vy; });
    alpha = Math.max(alpha * .985, drag ? .25 : 0);
  }
  const rad = n => n.kind === 'mod' ? 8 : 3.5 + Math.min(3, Math.sqrt(n.deg));

  // one drawing routine for both the big map and the pop-up; each has its own canvas, view and hover
  function makeView(canvas, pad) {
    const V = {canvas, g: canvas.getContext('2d'), k: 1, x: 0, y: 0, hover: null, pad};
    V.fit = (w, h, snap) => {
      if (!nodes.length) return;
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      nodes.forEach(n => { x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y); });
      const k = Math.min(1.6, (w - pad[0]) / Math.max(1, x1 - x0), (h - pad[1]) / Math.max(1, y1 - y0)), e = snap ? 1 : .2;
      if (drag && V === big) return;
      V.k += (k - V.k) * e; V.x += (-(x0 + x1) / 2 - V.x) * e; V.y += (-(y0 + y1) / 2 - V.y) * e;
    };
    V.toMap = ev => { const r = canvas.getBoundingClientRect();
      return {x: (ev.clientX - r.left - r.width / 2) / V.k - V.x, y: (ev.clientY - r.top - r.height / 2) / V.k - V.y}; };
    // generous, so the pointer catches a dot from a little way off; it also tells the live pointer where to snap
    V.hit = ev => { const p = V.toMap(ev);
      const n = nodes.reduce((best, n) => { const d = Math.hypot(n.x - p.x, n.y - p.y) * V.k;
        return d < rad(n) + 22 && (!best || d < best.d) ? {n, d} : best; }, null)?.n || null;
      const r = canvas.getBoundingClientRect();
      window.bbStick = n ? {x: r.left + r.width / 2 + (n.x + V.x) * V.k, y: r.top + r.height / 2 + (n.y + V.y) * V.k} : null;
      return n; };
    V.draw = (snap) => {
      const r = canvas.getBoundingClientRect(), dpr = devicePixelRatio || 1, g = V.g;
      if (!r.width) return;
      if (canvas.width !== Math.round(r.width * dpr) || canvas.height !== Math.round(r.height * dpr)) { canvas.width = r.width * dpr; canvas.height = r.height * dpr; }
      V.fit(r.width, r.height, snap);
      const css = getComputedStyle(document.documentElement), fg = css.getPropertyValue('--text').trim(),
        mut = css.getPropertyValue('--muted').trim(), red = css.getPropertyValue('--red').trim();
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, r.width, r.height);
      const P = n => [r.width / 2 + (n.x + V.x) * V.k, r.height / 2 + (n.y + V.y) * V.k];
      const me = nodes.find(n => n.id === here) || null, focus = V.hover || (V.pop ? me : null);
      const near = new Set(); if (focus) edges.forEach(([a, b]) => { if (a === focus) near.add(b); if (b === focus) near.add(a); });
      edges.forEach(([a, b, k]) => {
        const on = focus && (a === focus || b === focus);
        g.strokeStyle = on ? red : mut; g.globalAlpha = focus && !on ? .08 : k === 'sim' ? .35 : .5;
        g.lineWidth = on ? 1.4 : 1; g.setLineDash(k === 'sim' ? [2, 4] : []);
        const [x1, y1] = P(a), [x2, y2] = P(b); g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
      });
      g.setLineDash([]);
      const labels = [];
      nodes.forEach(n => {
        const [x, y] = P(n), s = rad(n), lit = n === V.hover || n === me;
        g.globalAlpha = focus && !lit && n !== focus && !near.has(n) ? .22 : 1;
        g.fillStyle = g.strokeStyle = lit ? red : fg;
        g.beginPath();
        if (n.kind === 'nb') { g.moveTo(x, y - s - 1); g.lineTo(x + s + 1, y); g.lineTo(x, y + s + 1); g.lineTo(x - s - 1, y); g.closePath(); g.fill(); }
        else if (n.kind === 'mod') { g.lineWidth = 1.6; g.arc(x, y, s, 0, 7); g.stroke(); g.beginPath(); g.arc(x, y, 2.2, 0, 7); g.fill(); }
        else { g.arc(x, y, s, 0, 7); g.fill(); }
        if (n === me) {   // "you are here": a ring that breathes
          const t = (performance.now() % 1600) / 1600;
          g.globalAlpha = 1 - t; g.lineWidth = 1.5; g.strokeStyle = red; g.beginPath(); g.arc(x, y, s + 3 + t * 10, 0, 7); g.stroke(); g.globalAlpha = 1;
        }
        if (n.kind === 'mod' || lit || (focus && near.has(n) && !(V.pop && !V.hover)))   // pop-up: neighbours named on hover only
          labels.push({n, x, y, s, lit, a: g.globalAlpha, prio: lit ? 0 : focus && near.has(n) ? 1 : 2});
      });
      // Labels go wherever there's room: under the dot, over it, to the right, to the left, or a line further out.
      // One that fits nowhere is left out, except the one you're on, which always shows.
      const placed = nodes.map(n => { const [x, y] = P(n), s = rad(n); return [x - s, y - s, 2 * s, 2 * s]; }),
        bg = css.getPropertyValue('--bg').trim() || css.getPropertyValue('--card').trim();
      g.textAlign = 'left'; g.textBaseline = 'top';
      labels.sort((a, b) => a.prio - b.prio).forEach(L => {
        const {n, x, y, s} = L, mod = n.kind === 'mod';
        g.font = mod ? `${V.pop ? 9.5 : 10.5}px "Space Mono", monospace` : `${V.pop ? 11.5 : 12.5}px "Segoe UI", system-ui, sans-serif`;
        const full = mod ? n.label.toUpperCase() : n.label, max = L.prio ? (V.pop ? 22 : 28) : (V.pop ? 30 : 40);
        const t = full.length > max ? full.slice(0, max - 1) + '…' : full;
        const w = g.measureText(t).width, h = mod ? 11 : 14, d = s + 5;
        const spots = [[x - w / 2, y + d], [x - w / 2, y - d - h], [x + d, y - h / 2], [x - d - w, y - h / 2],
                       [x - w / 2, y + d + h + 3], [x - w / 2, y - d - 2 * h - 3]];
        const free = ([bx, by]) => bx >= 2 && bx + w <= r.width - 2 && by >= 2 && by + h <= r.height - 2
          && !placed.some(p => bx - 3 < p[0] + p[2] && bx + w + 3 > p[0] && by - 2 < p[1] + p[3] && by + h + 2 > p[1]);
        let spot = spots.find(free);
        if (!spot) { if (L.prio) return; spot = [Math.max(2, Math.min(r.width - w - 2, x - w / 2)), y + d]; }
        placed.push([spot[0], spot[1], w, h]);
        g.globalAlpha = L.a * .75; g.fillStyle = bg; g.fillRect(spot[0] - 3, spot[1] - 1, w + 6, h + 2);
        g.globalAlpha = L.a; g.fillStyle = L.lit ? fg : mut; g.fillText(t, spot[0], spot[1]);
      });
      g.textBaseline = 'alphabetic'; g.globalAlpha = 1;
    };
    return V;
  }
  const big = makeView(CV, [120, 70]);

  // the pop-up: a button above Ask Claude, and a small card with its own canvas
  const BTN = document.createElement('button');
  BTN.className = 'map-btn glass'; BTN.hidden = true; BTN.title = 'Where this note sits in your note map';
  BTN.setAttribute('aria-label', 'Show this note on the note map'); BTN.setAttribute('aria-expanded', 'false');
  BTN.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="6" cy="7" r="2.3" fill="currentColor"/>'
    + '<circle cx="18" cy="6" r="2.3" fill="currentColor"/><circle cx="12" cy="17" r="2.6" fill="currentColor"/><path d="M7.8 8.3l2.8 6.6M16.4 7.8l-3.2 7.1M8.3 7l7.4-.8"/></svg>';
  const POP = document.createElement('div');
  POP.className = 'map-pop glass'; POP.hidden = true; POP.setAttribute('role', 'dialog'); POP.setAttribute('aria-label', 'Note map');
  POP.innerHTML = '<div class="mp-bar"><span class="label">You are here</span><span class="mp-name"></span><button class="mp-x x" aria-label="Close note map">✕</button></div>'
    + '<canvas class="mp-cv" role="img"></canvas><p class="mp-hint">Click a dot to go to that note</p>';
  document.body.append(BTN, POP);
  const mini = makeView(POP.querySelector('canvas'), [60, 50]); mini.pop = true;
  let pulse = 0;
  const pulseLoop = () => { if (POP.hidden) { pulse = 0; return; } mini.draw(); pulse = requestAnimationFrame(pulseLoop); };

  // which note is on screen right now: a Notebook note, or the open study-note card in the Notes tab
  function current() {
    const on = document.querySelector('section.on')?.id;
    if (on === 'notebook') { const c = typeof cur === 'string' ? cur : null; return c ? nodes.find(n => n.id === 'n:' + c.toLowerCase()) : null; }
    if (on === 'notes') {
      const v = [...document.querySelectorAll('#notes .mod-view')].find(x => !x.hidden); if (!v) return null;
      const open = [...v.querySelectorAll('details[open]')], titles = open.map(d => d.querySelector('summary b')?.textContent);
      const key = v.id.slice(6), want = nodes.find(n => n.id === here && n.kind === 'study' && n.key === key && titles.includes(n.title));
      return want || nodes.find(n => n.kind === 'study' && n.key === key && titles.includes(n.title)) || null;
    }
    return null;
  }
  let lastRefresh = 0;
  function sync() {
    const c = current();
    // a Notebook note made since the map was built: read the notes again (at most every 3 s)
    if (!c && typeof cur === 'string' && cur && document.querySelector('section.on')?.id === 'notebook'
        && !/^(Start here|Linking notes)$/.test(cur) && Date.now() - lastRefresh > 3000) { lastRefresh = Date.now(); refresh(); }
    if (c) here = c.id;
    BTN.hidden = !c;
    if (!c && !POP.hidden) closePop();
    if (c && !POP.hidden) POP.querySelector('.mp-name').textContent = c.label;
  }
  function openPop() {
    const c = current(); if (!c) return;
    here = c.id; POP.querySelector('.mp-name').textContent = c.label;
    POP.hidden = false; BTN.setAttribute('aria-expanded', 'true'); BTN.classList.add('on');
    mini.draw(true); if (!pulse) pulseLoop();
    POP.querySelector('canvas').setAttribute('aria-label', `Note map. You are at ${c.label}.`);
  }
  function closePop() { POP.hidden = true; BTN.setAttribute('aria-expanded', 'false'); BTN.classList.remove('on'); }
  BTN.onclick = () => POP.hidden ? openPop() : closePop();
  POP.querySelector('.mp-x').onclick = () => { closePop(); BTN.focus(); };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !POP.hidden) { closePop(); BTN.focus(); } });
  document.addEventListener('pointerdown', e => { if (!POP.hidden && !POP.contains(e.target) && !BTN.contains(e.target)) closePop(); });
  const mcv = mini.canvas;
  mcv.onpointermove = e => { const n = mini.hit(e); if (n !== mini.hover) { mini.hover = n; mcv.classList.toggle('on-dot', !!n); } };
  mcv.onpointerleave = () => { mini.hover = null; mcv.classList.remove('on-dot'); };
  mcv.onclick = e => { const n = mini.hit(e); if (n && n.id !== here) { openNode(n); setTimeout(sync, 200); } };

  function openNode(n) {
    if (window.SFX) SFX.play('open');
    here = n.id;
    if (n.kind === 'nb') { show('notebook'); setTimeout(() => open(n.name), 60); return; }  // the Notebook's open(), not window.open
    show('notes'); openView('notes-' + n.key);
    if (n.kind !== 'study') return;
    setTimeout(() => { const d = [...document.querySelectorAll('#notes-' + n.key + ' details')].find(x => x.querySelector('summary b')?.textContent === n.title);
      if (d) { d.open = true; d.scrollIntoView({behavior: 'smooth', block: 'center'}); } }, 80);
  }

  function loop() { if (alpha > .003) step(); big.draw(); raf = alpha > .003 || drag ? requestAnimationFrame(loop) : 0; }
  function kick() { if (!raf) raf = requestAnimationFrame(loop); }
  CV.onpointerdown = e => { const n = big.hit(e); if (!n) return; CV.setPointerCapture(e.pointerId);
    drag = {node: n, sx: e.clientX, sy: e.clientY, moved: false}; alpha = Math.max(alpha, .25); kick(); };
  CV.onpointermove = e => {
    if (!drag) { const n = big.hit(e); if (n !== big.hover) { big.hover = n; CV.classList.toggle('on-dot', !!n); big.draw(); } return; }
    if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 3) drag.moved = true;
    const p = big.toMap(e); drag.node.x = p.x; drag.node.y = p.y; drag.node.vx = drag.node.vy = 0;
  };
  CV.onpointerup = () => { const d = drag; drag = null; if (d && !d.moved) openNode(d.node); kick(); };
  CV.onpointerleave = () => { if (big.hover && !drag) { big.hover = null; CV.classList.remove('on-dot'); big.draw(); } };
  new ResizeObserver(() => { big.draw(); kick(); }).observe(CV);
  const redraw = () => { big.draw(); if (!POP.hidden) mini.draw(); };
  new MutationObserver(redraw).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redraw);
  async function refresh() {   // Notebook notes are read live, so new ones appear without a sync
    try { if (window.pywebview?.api?.nb_list) nbNotes = await pywebview.api.nb_list() || []; } catch (_) {}
    build();
  }
  build();
  window.addEventListener('pywebviewready', refresh);
  document.querySelector('nav button[data-tab="week"]')?.addEventListener('click', refresh);
  document.querySelector('nav button[data-tab="notebook"]')?.addEventListener('click', () => setTimeout(refresh, 300));
  // follow along as notes are opened and closed, in either tab
  document.addEventListener('toggle', e => { if (e.target.closest?.('#notes')) setTimeout(sync, 0); }, true);
  document.addEventListener('click', () => setTimeout(sync, 120), true);
  setInterval(sync, 1000);
})();
"""

CORNER_HTML = """
<button class="acct glass" aria-label="Account and settings"><span class="av"></span></button>
<button class="ask glass" aria-label="Ask Claude"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.5l1.9 5.6 5.6 1.9-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.9z"/><path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>Ask Claude</button>
<div class="scrim"></div>
<aside class="sheet left glass" data-sheet="acct">
 <div class="sh-top"><span class="label">Account</span><button class="ghost x" aria-label="Close">✕</button></div>
 <div class="who"><span class="av big"></span><div><b class="a-name"></b><span class="sub a-user"></span></div></div>
 <dl class="facts"><dt>Last synced</dt><dd class="a-sync"></dd><dt>Claude spend this month</dt><dd class="a-spend"></dd></dl>
 <nav class="set-tabs" role="tablist">
  <button class="set-tab on" data-s="general" role="tab" aria-selected="true">General</button>
  <button class="set-tab" data-s="claude" role="tab" aria-selected="false">Claude</button>
  <button class="set-tab" data-s="look" role="tab" aria-selected="false">Look</button>
  <button class="set-tab" data-s="tabs" role="tab" aria-selected="false">Tabs</button>
 </nav>

 <div class="set-pane on" data-s="general">
  <span class="label">Activity</span><div class="heat"></div>
  <label class="row">Check Blackboard in the background
   <select class="s-sync"><option value="0">Off</option><option value="1">Every hour</option><option value="2">Every 2 hours</option><option value="6">Every 6 hours</option></select></label>
  <label class="row">New notes per sync <span><input class="s-notes" type="range" min="0" max="15" step="1"> <b class="s-notes-v"></b></span></label>
  <div class="row col"><span class="s-files-where">Study files are saved in <b class="s-files-path"></b></span>
   <span><button class="ghost s-files">Open folder</button> <button class="ghost s-files-move">Choose folder…</button></span>
   <small class="s-files-msg"></small></div>
  <div class="row"><span class="s-ver"></span><span><small class="s-check-msg"></small> <button class="ghost s-check">Check for updates</button></span></div>
  <div class="row col"><button class="ghost danger s-signout">Sign out of Blackboard</button><small class="s-signout-msg"></small></div>
 </div>

 <div class="set-pane" data-s="claude">
  <span class="label">Signed in as</span>
  <div class="acc-list"></div>
  <div class="sign">
   <input class="acc-label" type="text" placeholder="A name for this account" autocomplete="off">
   <input class="acc-key" type="password" placeholder="sk-ant-…" autocomplete="off">
   <button class="btn acc-add">Sign in</button>
   <small class="msg acc-msg">Your key is checked with Anthropic and kept on this computer only.</small>
  </div>
  <span class="label">Model</span>
  <label class="row">Model <select class="s-model"></select></label>
  <small class="note s-model-note"></small>
  <label class="row">Effort <select class="s-effort"></select></label>
  <small class="note">Higher effort means Claude thinks for longer. It costs more and is slower.</small>
  <span class="label">Usage</span>
  <div class="usage u-list"></div>
 </div>

 <div class="set-pane" data-s="look">
  <span class="label">Highlight colour</span>
  <div class="swatches"></div>
  <span class="label">Interface size</span>
  <label class="row">Size <span><input class="s-zoom" type="range" min="85" max="130" step="5"> <b class="s-zoom-v"></b></span></label>
  <span class="label">Style</span>
  <label class="row">Dot grid <button class="sw s-dots" aria-pressed="true" aria-label="Dot grid"></button></label>
  <label class="row">Animations <button class="sw s-anim" aria-pressed="true" aria-label="Animations"></button></label>
  <label class="row">Opening screen <button class="sw s-intro" aria-pressed="true" aria-label="Opening screen"></button></label>
  <small class="note">Live hides Windows' pointer and draws one that stretches as you move,
   swells over anything you can press and pinches when you click.</small>
  <label class="row">Pointer <select class="s-cursor">
   <option value="live">Live</option><option value="ring">Ring</option><option value="dot">Dot</option>
   <option value="arrow">Arrow</option><option value="off">Windows pointer</option></select></label>
  <span class="label">Sound</span>
  <label class="row">Sound effects <button class="sw s-sfx" aria-pressed="true" aria-label="Sound effects"></button></label>
  <label class="row">Volume <span><input class="s-vol" type="range" min="0" max="100" step="5"> <b class="s-vol-v"></b></span></label>
  <div class="row"><button class="ghost s-reset-look">Back to the standard look</button></div>
 </div>

 <div class="set-pane" data-s="tabs">
  <small class="note">Untick a tab to hide it. Drag to put them in the order you want.</small>
  <div class="tab-list"></div>
  <div class="row"><button class="ghost s-reset-tabs">Show them all again</button></div>
 </div>
</aside>
<aside class="sheet right glass" data-sheet="ask">
 <div class="sh-top"><span class="label">Ask Claude</span><button class="ghost x" aria-label="Close">✕</button></div>
 <div class="ai-bar">
  <button class="ai-who" aria-expanded="false" title="Change Claude account"><i class="dot"></i><b class="ai-acc">Not signed in</b></button>
  <select class="ai-model" aria-label="Model"></select>
  <select class="ai-effort" aria-label="Effort"></select>
  <span class="ai-spend"></span>
 </div>
 <div class="ai-accs" hidden></div>
 <div class="chat-log"><div class="chat-hints"><p class="sub">Ask about your modules, deadlines, updates and notes.</p>
  <button class="chip">What should I focus on this week?</button><button class="chip">Explain my next assignment</button>
  <button class="chip">Summarise this week's updates</button></div></div>
 <form class="chat-form"><textarea class="chat-in" rows="1" placeholder="Message Claude"></textarea><button class="send" aria-label="Send">↑</button></form>
</aside>
"""



# ---------------------------------------------------------------- calendar export

def write_ics(deadlines):
    def esc(s):
        return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Blackboard Buddy//EN", "CALSCALE:GREGORIAN"]
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    for d in deadlines:
        due = d["due"].astimezone(timezone.utc)
        uid = hashlib.sha1(f"{d['course']}{d['title']}".encode()).hexdigest()
        lines += ["BEGIN:VEVENT", f"UID:{uid}@blackboard-buddy", f"DTSTAMP:{stamp}",
                  f"DTSTART:{(due - timedelta(minutes=30)):%Y%m%dT%H%M%SZ}",
                  f"DTEND:{due:%Y%m%dT%H%M%SZ}",
                  f"SUMMARY:{esc('DUE: ' + d['title'])}", f"DESCRIPTION:{esc(d['course'])}",
                  "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", "DESCRIPTION:Due tomorrow",
                  "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    path = OUT_DIR / "deadlines.ics"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- dashboard

def short(name):
    """'UFCF8S-30-2 - Advanced software development 26sep_1' -> 'Advanced software development'."""
    name = re.sub(r"^[A-Z0-9]{5,7}-\d+-\d+\s*-\s*", "", name or "")
    name = re.sub(r"\s+\d\d[a-z]{3}_\d+$", "", name).strip(" -") or name
    # UWE writes some titles as "Artificial intelligence ii"; the numeral belongs in capitals
    return re.sub(r"(?<=\s)(ii|iii|iv)$", lambda m: m.group(1).upper(), name)


def md(text):
    return markdown.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists"])


def days_left(due):
    delta = (due - now()).total_seconds() / 86400
    if delta < 0:
        return '<span class="pill late">past</span>'
    if delta < 1:
        return '<span class="pill urgent">today</span>'
    cls = "urgent" if delta < 3 else "soon" if delta < 7 else "later"
    return f'<span class="pill {cls}">{int(delta)}d</span>'


FILE_TAGS = {"pdf": "PDF", "pptx": "SLIDES", "ppt": "SLIDES", "docx": "DOC", "doc": "DOC", "mp4": "VIDEO",
             "mov": "VIDEO", "xlsx": "SHEET", "xls": "SHEET", "csv": "SHEET", "zip": "ZIP", "png": "IMAGE",
             "jpg": "IMAGE", "jpeg": "IMAGE", "txt": "TEXT", "md": "TEXT"}
NOTE_KINDS = (("Lecture", r"lecture|slides|\.pptx?$"), ("Lab", r"\blab|practical|worksheet|wsheet|exercise|tutorial"),
              ("Assessment", r"assess|coursework|specification|brief|scenario|project spec"),
              ("Reading", r"reading|book|paper|article"))


def note_kind(text):
    return next((k for k, pat in NOTE_KINDS if re.search(pat, text, re.I)), "Material")


_TAG = re.compile(r"""<([a-zA-Z][\w:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>""")
_STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_COLOUR_ATTR = re.compile(r"""\s(?:color|bgcolor)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_COLOUR_DECL = re.compile(r"(color|background|background-color|-webkit-text-fill-color)$", re.I)


def _no_colour(m):
    """One tag with its colours taken out, so it follows the app's light/dark theme."""
    def fix_style(sm):
        css = sm.group(1) if sm.group(1) is not None else sm.group(2)
        kept = [d.strip() for d in css.split(";")
                if d.strip() and not _COLOUR_DECL.match(d.split(":", 1)[0].strip())]
        return f' style="{"; ".join(kept)}"' if kept else ""

    attrs = _COLOUR_ATTR.sub("", _STYLE_ATTR.sub(fix_style, m.group(2)))
    return f"<{m.group(1)}{attrs}>"


def clean_body(body):
    """Blackboard page HTML, minus scripts, the embeds that need a signed-in browser, and its own colours."""
    body = re.sub(r"<(script|style)\b.*?</\1>", "", body or "", flags=re.S | re.I)
    body = re.sub(r"<iframe\b.*?</iframe>", '<p class="muted">Embedded media: open this page on Blackboard.</p>',
                  body, flags=re.S | re.I)
    body = re.sub(r"<img\b[^>]*(?:@X@|bbcswebdav)[^>]*>", "", body, flags=re.I)
    body = _TAG.sub(_no_colour, body)
    return body.replace("@X@EmbeddedFile.requestUrlStub@X@", BB_URL + "/")


def module_page(c):
    """One module's outline, rebuilt as nested sections, pages, files and links."""
    e = html.escape
    kids = {}
    for i in c.get("contents", []):
        kids.setdefault(i.get("parentId"), []).append(i)

    def file_btn(f):
        name = f["name"]
        tag = FILE_TAGS.get(name.rpartition(".")[2].lower(), "FILE")
        pv = f' data-preview="{e(Path(f["preview"]).as_uri())}"' if f.get("preview") else ""
        return (f'<button class="file" data-name="{e(name)}" data-src="{e(Path(f["local"]).as_uri())}" '
                f'data-local="{e(f["local"])}"{pv}><span class="tag">{tag}</span>{e(name)}</button>')

    def bb_link(i, label="BLACKBOARD"):
        # tools like Panopto have to be launched by Blackboard itself (it signs the request), so go straight to
        # that item's launcher; anything else opens the module's page
        url = c["url"]
        if i.get("handler") == "resource/x-bb-blti-link":
            url = (c["url"].split("/ultra/")[0] + "/webapps/blackboard/execute/blti/launchLink"
                   f"?course_id={c['id']}&content_id={i['id']}&from_ultra=true")
        return (f'<a class="file" href="{e(url)}" target="_blank"><span class="tag">{label}</span>'
                f'{e(i["title"])}<span class="ext">↗</span></a>')

    def node(i, top=False):
        h = i["handler"]
        # Ultra wraps a page in folders/documents with the same title; fold those into one level
        body, files, rest, todo = i.get("body") or "", list(i.get("files", [])), [], list(kids.get(i["id"], []))
        while todo:
            k = todo.pop(0)
            if k["title"] == i["title"] and k["handler"] in ("resource/x-bb-document", "resource/x-bb-folder"):
                body += k.get("body") or ""
                files += k.get("files", [])
                todo = kids.get(k["id"], []) + todo
            else:
                rest.append(k)
        if h == "resource/x-bb-externallink":
            url = i.get("url") or c["url"]
            return (f'<a class="file" href="{e(url)}" target="_blank"><span class="tag">LINK</span>'
                    f'{e(i["title"])}<span class="ext">↗</span></a>')
        if h in ("resource/x-bb-blti-link", "resource/x-bb-asmt-test-link", "resource/x-bb-assignment"):
            return bb_link(i, "SUBMIT" if "asmt" in h or "assignment" in h else "BLACKBOARD")
        if h == "resource/x-bb-file" and not rest:
            return "".join(file_btn(f) for f in files) or bb_link(i, "FILE")
        text = strip_html(body)  # some pages only repeat their own title; video-only pages have no text
        show = (text and text != i["title"].strip()) or re.search(r"<(iframe|video|embed)\b", body, re.I)
        inner = (f'<div class="page-body">{clean_body(body)}</div>' if show else "") \
            + "".join(file_btn(f) for f in files) + "".join(node(k) for k in rest)
        if not inner or top:  # a top-level section is already its own collapsible card
            return inner
        return (f'<details class="node"><summary>{e(i["title"])}</summary>'
                f'<div class="node-in">{inner}</div></details>')

    filled = [(i, node(i, top=True)) for i in kids.get(None, [])]
    sections = "".join(
        f'<details class="sec glass"><summary><span class="sec-n">{n:02d}</span>{e(i["title"])}</summary>'
        f'<div class="sec-in">{body}</div></details>'
        for n, (i, body) in enumerate([x for x in filled if x[1]], 1))
    return sections or '<p class="sub">No content found for this module yet. Sync to fetch it.</p>'


def done_key(d):
    return f"{d['course']}|{d['title']}"


def set_done(key, on):
    """Ticks an assignment off (or back on). Done ones count as handed in everywhere on the dashboard."""
    done = set(load_json(DONE, []))
    done.add(key) if on else done.discard(key)
    save_json(DONE, sorted(done))


def build_dashboard(snapshot, deadlines, announcements, grades, ai_results):
    e = html.escape
    ai_results = ai_results or {}
    # the red dot marks whatever is closest: the next deadline you haven't handed in or ticked off, and its plan
    near = '<i class="near" title="Closest"></i>'
    done = set(load_json(DONE, []))
    upcoming = sorted((d for d in deadlines if not d["submitted"] and done_key(d) not in done and d["due"] > now()),
                      key=lambda d: d["due"])
    first = upcoming[0] if upcoming else None
    is_first = lambda d: first is not None and (d["course"], d["title"], d["due"]) == (first["course"], first["title"], first["due"])

    def done_btn(d):
        on = done_key(d) in done
        return (f'<button class="ghost mk done-btn" data-done="{e(done_key(d))}" aria-pressed="{str(on).lower()}" '
                f'title="{"Mark as not done" if on else "Tick this off"}">{"✓ Done" if on else "Mark done"}</button>')

    dl_rows = "".join(
        f"<tr{' class=is-done' if done_key(d) in done else ''}><td>{days_left(d['due'])}</td><td>{d['due'].astimezone():%a %d %b, %H:%M}</td>"
        f"<td>{e(short(d['course']))}</td><td>{near if is_first(d) else ''}{e(d['title'])}</td>"
        f"<td>{'Submitted' if d['submitted'] else done_btn(d)}</td></tr>" for d in deadlines
    ) or '<tr><td colspan="5">Nothing due in the next few weeks 🎉</td></tr>'

    ann = "".join(
        f'<details class="card glass upd" data-id="{e(str(a["id"]))}"><summary><span class="ttl"><b>{e(a["title"])}</b> · {e(short(a["course"]))}'
        f' <span class="muted">{a["date"].astimezone():%a %d %b}</span></span>'
        f'<button class="hide" title="Hide this update" aria-label="Hide this update">Hide</button></summary>'
        f'<div class="body">{a["html"]}</div></details>' for a in announcements
    ) or "<p>No announcements in the last two weeks.</p>"
    if announcements:
        ann = '<p class="upd-bar"><button class="ghost show-hidden" hidden></button></p>' + ann

    gr_rows = "".join(
        f"<tr><td>{e(short(g['course']))}</td><td>{e(g['title'])}</td>"
        f"<td>{e(str(g['grade'] or g['score']))}{' / ' + str(g['possible']) if g['possible'] else ''}</td>"
        f"<td>{e(g['feedback'][:300])}</td></tr>" for g in grades
    ) or '<tr><td colspan="4">No marks released yet.</td></tr>'

    current = [c for c in snapshot["courses"] if c.get("current", True)]
    mod_key = lambda name: hashlib.sha1(name.encode()).hexdigest()[:8]
    cards, views = [], []
    for c in current:
        n_files = sum(len(i.get("files", [])) for i in c.get("contents", []))
        n_secs = sum(1 for i in c.get("contents", []) if not i.get("parentId"))
        cards.append(f'<button class="card glass course" data-open="mod-{mod_key(c["name"])}">{e(short(c["name"]))}'
                     f'<span class="muted">{n_secs} sections · {n_files} files</span></button>')
        views.append(f'<div class="mod-view" id="mod-{mod_key(c["name"])}" hidden><div class="mod-bar">'
                     f'<button class="ghost back">‹ All modules</button>'
                     f'<a class="ghost" href="{e(c["url"])}" target="_blank">Blackboard ↗</a></div>'
                     f'<h2 class="mod-title">{e(short(c["name"]))}</h2><p class="sub">{e(c["courseId"])}</p>'
                     f'{module_page(c)}</div>')
    courses = f'<div class="grid mod-grid">{"".join(cards)}</div>{"".join(views)}'

    # where each top-level section sits in its module, so notes follow the module's own order
    order = {}
    for c in snapshot["courses"]:
        for n, i in enumerate(c.get("contents", [])):
            order.setdefault((c["name"], i["path"].split(" / ")[0]), n)

    def doc_list(items, label, by_section=False, heading=True):
        if not items:
            return f"<p>No {label} yet. Add your API key to <code>.env</code> and run a sync.</p>"
        mods = {}
        for i in items:
            mods.setdefault(i["course"], []).append(i)

        def card(i):
            tag = f'<span class="pill">{note_kind(i.get("path", "") + " " + i["title"])}</span> ' if by_section else ""
            due = f' <span class="muted">due {i["due"].astimezone():%a %d %b}</span>' if i.get("due") else ""
            tick = done_btn(i) if "due" in i else ""          # plans can be ticked off too
            return (f'<details class="card glass{" is-done" if tick and done_key(i) in done else ""}"><summary>{near if "due" in i and is_first(i) else ""}'
                    f'{tag}<b>{e(i["title"])}</b>{due}{tick}</summary><div class="body">{md(i["md"])}</div></details>')

        chips = ""
        if by_section and len(mods) > 1:
            chips = '<div class="chips"><button class="chip on" data-f="all">All</button>' + "".join(
                f'<button class="chip" data-f="{mod_key(m)}">{e(short(m))}</button>' for m in sorted(mods, key=short)) + "</div>"
        groups = []
        for course in sorted(mods, key=short):
            its = mods[course]
            if by_section:
                secs = {}
                for i in sorted(its, key=lambda i: i.get("path", "")):
                    secs.setdefault(i.get("path", "").split(" / ")[0], []).append(i)
                inner = "".join(f'<p class="sec-label">{e(sec)}</p>' + "".join(card(i) for i in si)
                                for sec, si in sorted(secs.items(), key=lambda kv: order.get((course, kv[0]), 9999)))
            else:
                inner = "".join(card(i) for i in its)
            head = f'<h3 class="label">{e(short(course))}</h3>' if heading else ""
            groups.append(f'<div class="group" data-mod="{mod_key(course)}">{head}{inner}</div>')
        return chips + "".join(groups)

    # Notes tab: one card per module, like Modules; inside, notes by section plus files still to do
    all_notes = ai_results.get("notes") or []
    noted = {(n["course"], n["path"]) for n in all_notes}
    ncards, nviews = [], []
    for c in current:
        k = mod_key(c["name"])
        mine = [n for n in all_notes if n["course"] == c["name"]]
        todo = sorted((i for i in c.get("contents", []) if (c["name"], i["path"]) not in noted
                       and any(f["name"].lower().endswith(READABLE) for f in i.get("files", []))),
                      key=lambda i: order.get((c["name"], i["path"].split(" / ")[0]), 9999))
        ncards.append(f'<button class="card glass course" data-open="notes-{k}">{e(short(c["name"]))}'
                      f'<span class="muted">{len(mine)} note{"" if len(mine) == 1 else "s"} · '
                      f'{len(todo)} file{"" if len(todo) == 1 else "s"} without notes</span></button>')
        todo_html = "".join(
            f'<div class="todo"><span class="tag">{e(i["path"].split(" / ")[0][:24])}</span>'
            f'<span class="t">{e(i["title"])}</span><button class="ghost mk" data-item="{e(i["id"])}" '
            f'data-view="notes-{k}">Make notes</button></div>' for i in todo)
        nviews.append(
            f'<div class="mod-view" id="notes-{k}" hidden><div class="mod-bar"><button class="ghost back">‹ All modules</button></div>'
            f'<h2 class="mod-title">{e(short(c["name"]))}</h2><p class="sub">{len(mine)} notes</p>'
            + (doc_list(mine, "notes", by_section=True, heading=False) if mine else '<p class="sub">No notes for this module yet.</p>')
            + (f'<h3 class="label">Not in your notes yet · about $0.10 each</h3><div class="panel glass todo-list">{todo_html}</div>'
               if todo else "") + "</div>")
    notes_tab = f'<div class="grid mod-grid">{"".join(ncards)}</div>{"".join(nviews)}'

    # the assignments still to hand in, soonest first (the timetable still goes to Claude, just not here)
    up_html = ""
    if upcoming:
        rows = "".join(f"<tr><td>{near if is_first(d) else ''}{days_left(d['due'])}</td><td>{d['due'].astimezone():%a %d %b, %H:%M}</td>"
                       f"<td>{e(d['title'])}</td><td>{e(short(d['course']))}</td></tr>" for d in upcoming[:6])
        more = len(upcoming) - 6
        up_html = (f'<h3 class="label">Upcoming assignments · {len(upcoming)} to hand in</h3><div class="table-wrap glass"><table class="tt">'
                   f"<tr><th>Left</th><th>Due</th><th>Assignment</th><th>Module</th></tr>{rows}</table></div>"
                   + (f'<p class="sub">and {more} more in Deadlines</p>' if more > 0 else ""))

    # Note map (This week): the study notes and modules go in the page; Notebook notes are read live
    map_data = json.dumps({
        "modules": [{"key": mod_key(c["name"]), "name": short(c["name"])} for c in current
                    if not c["name"].startswith("-")],          # "-BSc (Hons) ..." is the programme, not a module
        "study": [{"key": mod_key(n["course"]), "title": n["title"], "topic": n.get("topic") or heading_topic(n),
                   "text": n["md"][:6000]}
                  for n in all_notes if any(c["name"] == n["course"] for c in current)],
    }).replace("</", "<\\/")
    map_html = ('<h3 class="label">Note map</h3><div class="map-wrap glass"><canvas class="nmap" role="img"></canvas>'
                '<p class="map-meta"></p></div>'
                f'<script type="application/json" id="map-data">{map_data}</script>')

    count_html = ""
    if upcoming:
        nxt = upcoming[0]
        left = (nxt["due"] - now()).total_seconds() / 86400
        num, unit = (f"{int(left)}", "DAYS") if left >= 1 else (f"{max(1, int(left * 24))}", "HOURS")
        dots = "".join(f'<i class="{"on" if i < min(int(left) + 1, 30) else ""}"></i>' for i in range(30))
        count_html = (f'<h3 class="label">Next deadline</h3><div class="count glass{" hot" if left < 3 else ""}">'
                      f'<div><span class="num">{num}</span><span class="unit">{unit}</span><div class="dots">{dots}</div></div>'
                      f'<div class="what"><b>{near}{e(nxt["title"])}</b><span>{e(short(nxt["course"]))} · '
                      f'{nxt["due"].astimezone():%a %d %b, %H:%M}</span></div></div>')

    tabs = [
        ("week", "This week", count_html + up_html
         + '<h3 class="label">Activity</h3><div class="heat-wrap glass"><div class="heat"></div></div>' + map_html + ( f'<div class="panel glass">{md(ai_results["briefing"])}</div>' if ai_results.get("briefing")
         else '<div class="panel glass"><p class="sub" style="margin:0">Your AI briefing appears here once '
              'your API key is set.</p></div>')),
        ("deadlines", "Deadlines",
         f'<p><a class="btn" href="deadlines.ics" download>Add to calendar (.ics)</a> '
         f'</p>'
         f"<div class=\"table-wrap glass\"><table><tr><th></th><th>Due</th><th>Module</th><th>Task</th><th></th></tr>{dl_rows}</table></div>"),
        ("updates", "Updates", ann),
        ("plans", "Plans", doc_list(ai_results.get("plans"), "assignment plans")),
        ("notes", "Notes", notes_tab),
        ("notebook", "Notebook", notebook.NB_HTML),
        ("online", "Online", online.ON_HTML),
        ("grades", "Grades",
         (f'<div class="panel glass">{md(ai_results["feedback"])}</div>' if ai_results.get("feedback") else "")
         + f"<div class=\"table-wrap glass\"><table><tr><th>Module</th><th>Assessment</th><th>Mark</th><th>Feedback</th></tr>{gr_rows}</table></div>"),
        ("courses", "Modules", courses),
    ]
    spend = load_json(SPEND, {})
    notebook.init()
    programme = next((short(c["name"]) for c in snapshot["courses"] if c["name"].startswith("-")), "")
    boot = json.dumps({
        "activity": load_json(ACTIVITY, {}), "settings": settings(),
        "spend": spend.get(datetime.now().strftime("%Y-%m"), 0),
        "name": display_name(snapshot), "version": VERSION, "materials": str(MATERIALS), "online": online.config(settings()), "vault": notebook.ROOT.resolve().as_uri(), "user": snapshot["me"].get("userName", ""),
        "programme": programme.strip("- ").title(), "synced": f"{parse_date(snapshot['synced']).astimezone():%a %d %b, %H:%M}",
    }).replace("</", "<\\/")
    nav = "".join(f'<button data-tab="{k}">{e(t)}{"<i class=near></i>" if k == "deadlines" and first else ""}</button>'
                  for k, t, _ in tabs)
    panes = "".join(f'<section id="{k}">{body}</section>' for k, _, body in tabs)
    synced = parse_date(snapshot["synced"]).astimezone()
    n_current = sum(1 for c in snapshot["courses"] if c.get("current", True))

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Blackboard Buddy</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Doto:wght@700;800;900&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<script>try{{const t=localStorage.theme;if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}}catch(_){{}}</script>
<style>
:root{{--spring:linear(0.000,0.023,0.078,0.151,0.234,0.318,0.400,0.476,0.547,0.611,0.667,0.717,0.760,0.798,0.830,0.857,0.881,0.900,0.917,0.931,0.943,0.953,0.961,0.968,0.973,0.978,0.982,0.985,0.988,0.990,0.992,0.993,0.995,0.996,0.996,0.997,0.998,0.998,0.998,0.999,0.999,0.999,0.999,0.999,1.000,1.000,1.000,1.000,1);--bounce:linear(0.000,0.032,0.112,0.220,0.340,0.461,0.576,0.679,0.768,0.843,0.903,0.949,0.983,1.008,1.024,1.033,1.038,1.038,1.036,1.033,1.029,1.024,1.019,1.015,1.011,1.008,1.005,1.003,1.002,1.000,0.999,0.999,0.999,0.999,0.999,0.999,0.999,0.999,0.999,0.999,0.999,1.000,1.000,1.000,1.000,1.000,1.000,1.000,1);
 --dotf:"Doto","Space Mono",ui-monospace,monospace;--mono:"Space Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;
 --text:#0b0b0b;--muted:#6a6a6a;--accent:#0b0b0b;--red:#d71921;--line:rgba(0,0,0,.09);--dot:rgba(0,0,0,.13);
 --glass:rgba(255,255,255,.6);--glass-strong:rgba(255,255,255,.95);--rim:rgba(0,0,0,.08);--inset:rgba(0,0,0,.035);
 --shadow:0 1px 2px rgba(0,0,0,.04),0 8px 28px rgba(0,0,0,.05);--bg:#efefef}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--text:#f2f2f2;--muted:#8a8a8a;--accent:#f2f2f2;
 --line:rgba(255,255,255,.09);--dot:rgba(255,255,255,.11);--glass:rgba(18,18,18,.62);--glass-strong:rgba(64,64,64,.75);
 --rim:rgba(255,255,255,.1);--inset:rgba(255,255,255,.04);--shadow:0 8px 32px rgba(0,0,0,.6);--bg:#000}}}}
:root[data-theme=dark]{{--text:#f2f2f2;--muted:#8a8a8a;--accent:#f2f2f2;
 --line:rgba(255,255,255,.09);--dot:rgba(255,255,255,.11);--glass:rgba(18,18,18,.62);--glass-strong:rgba(64,64,64,.75);
 --rim:rgba(255,255,255,.1);--inset:rgba(255,255,255,.04);--shadow:0 8px 32px rgba(0,0,0,.6);--bg:#000}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;font:15px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable","Segoe UI",system-ui,sans-serif;
 color:var(--text);background:var(--bg);-webkit-font-smoothing:antialiased;padding-bottom:110px;transition:background-color .5s,color .5s}}
::selection{{background:var(--red);color:#fff}}
/* background: Nothing-style dot grid */
.ambient{{position:fixed;inset:0;z-index:-1;background:var(--bg);transition:background-color .5s}}
.ambient::before{{content:"";position:absolute;inset:0;background-image:radial-gradient(var(--dot) 1.1px,transparent 1.5px);
 background-size:22px 22px;background-position:11px 11px}}
/* corner buttons + status bubble */
.status{{position:fixed;top:18px;left:50%;z-index:20;transform:translate(-50%,-80px);display:flex;align-items:center;gap:10px;
 padding:9px 16px 9px 12px;border-radius:999px;font:11.5px/1.2 var(--mono);text-transform:uppercase;letter-spacing:.08em;
 max-width:min(560px,calc(100vw - 140px));transition:transform .6s var(--bounce)}}
.status.show{{transform:translate(-50%,0)}} .status span{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.spin{{width:8px;height:8px;flex:none;border-radius:50%;background:var(--red);animation:blink 1s steps(2) infinite}}
.status:not(.busy) .spin{{display:none}} @keyframes blink{{50%{{opacity:.15}}}} @keyframes spin{{to{{transform:rotate(360deg)}}}}
.refresh{{top:66px!important;right:70px!important;display:none!important}} .app .refresh{{display:grid!important}}
.refresh.busy{{animation:spin 1.2s linear infinite}}
.theme.glass{{position:fixed;top:66px;right:22px;z-index:10;width:40px;height:40px;border-radius:50%;cursor:pointer;color:var(--text);
 font-size:16px;display:grid;place-items:center;transition:transform .5s var(--bounce)}}
.theme:not(.refresh){{transition:transform .8s var(--bounce)}}
.refresh:hover{{transform:scale(1.1)}} .refresh:active{{transform:scale(.92)}}
.refresh.turn{{animation:spin .75s cubic-bezier(.33,0,.15,1)}}
/* the theme button carries an inline rotate from the script, so its hover is a ring, not a scale */
.theme:not(.refresh)::after{{content:"";position:absolute;inset:-4px;border-radius:50%;border:1.5px solid var(--text);
 transform:scale(.78);opacity:0;pointer-events:none;transition:transform .32s var(--bounce),opacity .2s}}
.theme:not(.refresh):hover::after{{transform:scale(1);opacity:.4}}
/* light/dark switch: circular reveal (see the script); no cross-fade underneath */
::view-transition-old(root),::view-transition-new(root){{animation:none;mix-blend-mode:normal}}
::view-transition-new(root){{z-index:2}} .switching *:not(.theme),.switching body{{transition:none!important}}
.theme svg{{display:block;width:18px;height:18px}} .theme.glass{{padding:0;line-height:0}}
/* intro: dot-matrix name, typed subtitle, then a flash and it's gone */
.intro{{position:fixed;inset:0;z-index:100;display:grid;place-content:center;justify-items:center;gap:26px;cursor:pointer;
 background:var(--bg) radial-gradient(var(--dot) 1.1px,transparent 1.5px) 11px 11px/22px 22px}}
.intro-top{{display:flex;align-items:center;gap:10px;font:11.5px var(--mono);letter-spacing:.2em;color:var(--muted)}}
.intro-top i{{width:9px;height:9px;border-radius:50%;background:var(--red);animation:blink 1s steps(2) infinite}}
.intro-sub{{font:12px var(--mono);letter-spacing:.2em;color:var(--muted);min-height:1.2em}}
.intro.out{{animation:flashout .75s cubic-bezier(.3,0,.2,1) forwards;pointer-events:none}}
@keyframes flashout{{0%{{}} 10%{{background:var(--text);filter:brightness(1.6)}} 22%{{background:var(--bg)}}
 100%{{opacity:0;transform:scale(1.06);filter:blur(10px)}}}}
html.intro-on header,html.intro-on main,html.intro-on nav,html.intro-on .theme,html.intro-on .status{{opacity:0}}
@media (prefers-reduced-motion:reduce){{*,*::before,*::after,*::details-content{{animation:none!important;transition:none!important}}}}
/* Liquid Glass material, kept neutral */
.glass{{background:var(--glass);backdrop-filter:blur(22px) saturate(140%);-webkit-backdrop-filter:blur(22px) saturate(140%);
 border:1px solid var(--rim);box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.12);position:relative}}
.glass > *{{position:relative}}
/* dot-matrix countdown */
.count{{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;border-radius:28px;padding:22px 26px;margin:14px 0}}
.count .num{{font:800 92px/0.85 var(--dotf);letter-spacing:.02em}} .count .unit{{font:12px var(--mono);letter-spacing:.14em;color:var(--muted);margin-left:6px}}
.count .what{{text-align:right;min-width:0}} .count .what b{{display:block;font-size:17px}}
.count .what span{{font:11.5px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
.dots{{display:flex;gap:5px;flex-wrap:wrap;margin-top:14px}} .dots i{{width:7px;height:7px;border-radius:50%;background:var(--line)}}
.dots i.on{{background:var(--text)}} .count.hot .num,.count.hot .dots i.on{{color:var(--red);background-color:transparent}}
.count.hot .dots i.on{{background:var(--red)}}
@media (max-width:600px){{.count{{flex-direction:column;align-items:flex-start}} .count .what{{text-align:left}} .count .num{{font-size:72px}}}}
header{{max-width:880px;margin:56px auto 8px;padding:0 20px}}
header h1{{margin:0;font:800 60px/1 var(--dotf);text-transform:uppercase;letter-spacing:.01em}}
.sub{{color:var(--muted);margin-top:10px;font:11.5px/1.4 var(--mono);text-transform:uppercase;letter-spacing:.12em}}
/* floating tab bar */
nav.glass{{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:10;display:flex;gap:4px;padding:6px;
 border-radius:999px;max-width:calc(100vw - 24px);overflow-x:auto;scrollbar-width:none;
 background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px)}}
main::after{{content:'';display:block;height:96px}}  /* room so the last lines clear the tab bar */
nav button{{position:relative;z-index:1;border:0;background:transparent;color:var(--text);padding:9px 16px;border-radius:999px;cursor:pointer;
 font:11.5px/1 var(--mono);text-transform:uppercase;letter-spacing:.1em;white-space:nowrap;transition:color .3s,transform .5s var(--bounce)}}
nav button:active{{transform:scale(.9)}} nav button.on{{color:var(--text);font-weight:700}}
.indicator{{position:absolute;top:6px;bottom:6px;left:0;width:0;border-radius:999px;background:var(--glass-strong);
 box-shadow:0 2px 12px rgba(0,0,0,.12),inset 0 1px 0 var(--rim);transition:left .6s var(--bounce),width .6s var(--bounce),transform .35s var(--spring)}}
.indicator.moving{{transform:scaleY(.86) scaleX(1.06)}}
main{{max-width:880px;margin:auto;padding:18px 20px 0}}
section{{display:none}} section.on{{display:block}}
section.on > *{{animation:rise .7s var(--spring) both;animation-delay:calc(var(--i,0) * 45ms)}}
@keyframes rise{{from{{opacity:0;transform:translateY(14px) scale(.98);filter:blur(6px)}}}}
header > *{{animation:rise .9s var(--spring) both}} header .sub{{animation-delay:80ms}}
.panel{{border-radius:28px;padding:22px 26px;margin:14px 0}}
.table-wrap{{border-radius:24px;overflow:hidden;margin:14px 0}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:13px 18px;border-bottom:1px solid var(--line);vertical-align:top}}
tr:last-child td{{border-bottom:0}} th{{font:11px var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}}
.card{{display:block;border-radius:22px;padding:16px 20px;margin:12px 0;color:inherit;text-decoration:none;transition:transform .55s var(--bounce),box-shadow .4s}}
.card:not(details):hover,details.card:not([open]):hover{{transform:translateY(-2px) scale(1.005)}}
.card:not(details):active{{transform:scale(.985)}}
/* press feedback on the title only: shrinking a tall open card moved the title out from under the mouse */
details.card > summary{{transition:transform .4s var(--bounce)}} details.card > summary:active{{transform:scale(.985)}}
details.card{{interpolate-size:allow-keywords}}
details.card::details-content{{height:0;overflow:clip;opacity:0;transition:height .55s var(--spring),opacity .35s,content-visibility .55s allow-discrete}}
details.card[open]::details-content{{height:auto;opacity:1}}
.card summary{{cursor:pointer;list-style:none}} .card summary::-webkit-details-marker{{display:none}}
.card summary b{{font-weight:600}} .card summary::after{{content:"›";float:right;color:var(--muted);font-size:20px;line-height:1;transition:transform .25s}}
.card summary::after{{transition:transform .5s var(--bounce)}} .card[open] summary::after{{transform:rotate(90deg)}}
.body{{margin-top:12px;overflow-x:auto}} .body img{{max-width:100%;border-radius:12px}}
.body table,.panel table{{background:var(--inset);border-radius:14px;overflow:hidden}}
.muted{{color:var(--muted);font-size:13px;margin-left:6px}} .course .muted{{display:block;margin:6px 0 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}} .grid .card{{margin:0;font-weight:600}}
.near{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--red);margin:0 6px 0 -13px;vertical-align:.1em;
 box-shadow:0 0 0 0 rgba(215,25,33,.5);animation:near 2.4s ease-out infinite}}
@keyframes near{{0%{{box-shadow:0 0 0 0 rgba(215,25,33,.45)}}70%,100%{{box-shadow:0 0 0 7px rgba(215,25,33,0)}}}}
nav button .near{{position:absolute;top:7px;right:7px;width:5px;height:5px;margin:0;animation:none}}
@media (prefers-reduced-motion:reduce){{.near{{animation:none}}}}
.pill{{display:inline-block;padding:3px 9px;border-radius:999px;font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;border:1px solid var(--line)}}
.late{{color:var(--muted);text-decoration:line-through}} .urgent{{background:var(--red);border-color:var(--red);color:#fff}}
.soon{{background:var(--text);border-color:var(--text);color:var(--bg)}} .later{{color:var(--text)}}
.btn{{transition:transform .5s var(--bounce),box-shadow .3s}} .btn:hover{{transform:scale(1.03)}} .btn:active{{transform:scale(.96)}}
.btn{{display:inline-block;background:var(--text);color:var(--bg);padding:11px 20px;border-radius:999px;text-decoration:none;
 font:11.5px var(--mono);text-transform:uppercase;letter-spacing:.1em}}
.label{{font:11.5px var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:28px 4px 8px;font-weight:400}}
.panel h1,.panel h2{{letter-spacing:-.01em}} .panel h1:first-child,.panel h2:first-child{{margin-top:0}}
code{{background:rgba(127,127,127,.15);padding:1px 6px;border-radius:6px}}
@media (max-width:600px){{header h1{{font-size:42px}} .panel{{padding:18px}} th,td{{padding:10px 12px}}}}
{EXTRA_CSS}{notebook.NB_CSS}{online.ON_CSS}</style>
<script>/* the look is read straight from storage so the page never flashes the standard one first */
(function(){{try{{const L=JSON.parse(localStorage.look||'{{}}'),r=document.documentElement;
 if(L.accent)r.style.setProperty('--red',L.accent);
 if(L.dots===false)r.style.setProperty('--dot','transparent');
 if(L.anim===false)r.classList.add('no-anim');
 const cs=L.cursor===true||L.cursor===undefined?'live':L.cursor===false?'off':L.cursor;
 if(cs!=='off'){{if(cs!=='live')r.classList.add('fancy-cursor');r.classList.add('cur-'+cs)}}
 if(L.zoom&&L.zoom!==100)r.style.zoom=L.zoom/100;
 if(L.intro===false||L.anim===false)sessionStorage.introSeen='1';
 const T=JSON.parse(localStorage.tabsPref||'{{}}');if(T.order||T.hidden)r.dataset.tabsPref='1';
}}catch(_){{}}
 // nothing below may leave the page hidden: this runs whatever else breaks
 window.bbMarks=['head'];
 const unhide=why=>{{const r=document.documentElement;if(!r.classList.contains('intro-on'))return;
  bbMarks.push('forced:'+why);r.classList.remove('intro-on');
  const i=document.querySelector('.intro');if(i)i.remove()}};
 setTimeout(()=>unhide('timeout'),6000);
 addEventListener('error',e=>{{bbMarks.push('error:'+(e.message||'').slice(0,60));unhide('error')}});
 addEventListener('DOMContentLoaded',()=>bbMarks.push('dom'));
}})();</script></head><body>
<div class="intro"><div class="intro-top"><i></i>LOADING YOUR WEEK</div><canvas></canvas><div class="intro-sub"></div></div>
<div class="ambient"></div>
<div class="titlebar"><span>BLACKBOARD BUDDY</span></div>
<div class="wctl">
<button class="wmin" title="Minimise" aria-label="Minimise"><svg viewBox="0 0 10 10" aria-hidden="true"><path d="M0 5h10" stroke="currentColor" stroke-width="1.2"/></svg></button>
<button class="wmax" title="Maximise" aria-label="Maximise"><svg viewBox="0 0 10 10" aria-hidden="true"><rect x="0.6" y="0.6" width="8.8" height="8.8" fill="none" stroke="currentColor" stroke-width="1.2"/></svg></button>
<button class="wclose" title="Close" aria-label="Close"><svg viewBox="0 0 10 10" aria-hidden="true"><path d="M0 0l10 10M10 0L0 10" stroke="currentColor" stroke-width="1.2"/></svg></button>
</div>
<i class="sh-grip" hidden></i>
<i class="redge e-n"></i><i class="redge e-s"></i><i class="redge e-w"></i><i class="redge e-e"></i>
<i class="redge e-nw"></i><i class="redge e-ne"></i><i class="redge e-sw"></i><i class="redge e-se"></i>
<div class="snapmenu" hidden role="menu" aria-label="Put the window">
 <button data-s="left" title="Left half"><i class="sl"></i></button>
 <button data-s="right" title="Right half"><i class="sr"></i></button>
 <button data-s="full" title="Fill the screen"><i class="sf"></i></button>
 <button data-s="tl" title="Top left"><i class="stl"></i></button>
 <button data-s="tr" title="Top right"><i class="str"></i></button>
 <button data-s="bl" title="Bottom left"><i class="sbl"></i></button>
 <button data-s="br" title="Bottom right"><i class="sbr"></i></button>
 <button data-s="top" title="Top half"><i class="st"></i></button>
 <button data-s="win" class="swin" title="Windows layouts (Win+Z)">⊞</button>
</div>
<button class="theme glass" title="Light / dark / automatic" aria-label="Change appearance">
<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/></svg></button>
<button class="theme refresh glass" title="Check Blackboard for updates" aria-label="Refresh">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/></svg></button>
<div class="status glass" role="status"><i class="spin"></i><span></span></div>
<div class="viewer" hidden><div class="v-bar glass"><b class="v-name"></b><span class="v-act">
<button class="ghost v-open">Open in app</button><button class="ghost v-close" aria-label="Close">✕</button></span></div>
<div class="v-body"></div></div>
<div class="signin update" hidden><div class="si-card glass"><i class="near"></i><h3>Update available</h3>
<p class="sub up-msg"></p><button class="btn up-go">Update now</button><button class="ghost up-later">Later</button></div></div>
<div class="signin" hidden><div class="si-card glass"><i class="near"></i><h3>Signed out of Blackboard</h3>
<p class="sub">Sign in again to keep your modules, files and deadlines up to date.</p>
<button class="btn si-go">Sign in</button><button class="ghost si-later">Later</button></div></div>
<div class="signin askwarn" hidden role="alertdialog" aria-labelledby="aw-h" aria-describedby="aw-t"><div class="si-card glass"><i class="near"></i>
<h3 id="aw-h">This uses your API credit</h3>
<p class="note" id="aw-t">Ask Claude is paid per question from your Claude API credit, not your Claude.ai plan.
A question costs about $0.10 on Opus, less on cheaper models. If you use it a lot, the credit will run out,
and Ask Claude, the briefing, plans and notes stop working until you top it up.</p>
<p class="note aw-meta">Spent this month: <b class="aw-spend"></b></p>
<label class="aw-check"><input type="checkbox" class="aw-never"> Don't show this again</label>
<button class="btn aw-go">I understand, continue</button><button class="ghost aw-no">Cancel</button></div></div>
<header><h1>{'Hi ' + e(display_name(snapshot)) if display_name(snapshot) else 'This week'}</h1>
<div class="sub">Last synced {synced:%A %d %B, %H:%M} · {n_current} module{'' if n_current == 1 else 's'} this year</div></header>
<nav class="glass"><span class="indicator"></span>{nav}</nav><main>{panes}</main>
<script>
const nav=document.querySelector('nav'),ind=nav.querySelector('.indicator'),btns=[...nav.querySelectorAll('button')];
const place=(b,instant)=>{{if(instant)ind.style.transition='none';ind.style.left=b.offsetLeft+'px';ind.style.width=b.offsetWidth+'px';
 if(instant){{ind.offsetWidth;ind.style.transition=''}}else{{ind.classList.add('moving');clearTimeout(place.t);place.t=setTimeout(()=>ind.classList.remove('moving'),220)}}
 nav.scrollTo({{left:b.offsetLeft-nav.clientWidth/2+b.offsetWidth/2,behavior:instant?'auto':'smooth'}})}};
const show=(k,instant)=>{{btns.forEach(b=>b.classList.toggle('on',b.dataset.tab===k));
 document.querySelectorAll('section').forEach(sec=>{{sec.classList.toggle('on',sec.id===k);
  if(sec.id===k)[...sec.children].forEach((c,i)=>{{c.style.setProperty('--i',Math.min(i,12));c.style.animation='none';c.offsetWidth;c.style.animation=''}})}});
 place(btns.find(b=>b.dataset.tab===k),instant);try{{localStorage.tab=k}}catch(_){{}}}};
btns.forEach(b=>b.onclick=()=>show(b.dataset.tab));
// a hidden tab is skipped by the number and arrow keys too
const vis=()=>btns.filter(b=>!b.hidden);
window.bbShow=show;window.bbTabs=btns;window.bbPlaceInd=()=>place(btns.find(b=>b.classList.contains('on')),true);
document.addEventListener('keydown',ev=>{{if(ev.target.closest('input,textarea,[contenteditable="true"],#notebook,#online'))return;
 const v=vis(),i=v.findIndex(b=>b.classList.contains('on'));
 if(/^[1-9]$/.test(ev.key)&&v[ev.key-1])show(v[ev.key-1].dataset.tab);
 if(ev.key==='ArrowRight')show(v[(i+1)%v.length].dataset.tab);
 if(ev.key==='ArrowLeft')show(v[(i-1+v.length)%v.length].dataset.tab)}});
addEventListener('resize',()=>place(btns.find(b=>b.classList.contains('on')),true));
// what's on screen right now: the saved choice, else whatever Windows is set to
const shown=()=>document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
// theme switch: the new look spreads out in a circle from the button
const tb=document.querySelector('.theme');let turns=0,switching=false;
tb.onclick=()=>{{if(switching)return;  // ignore clicks until the current switch has finished
 const r=document.documentElement,next=shown()==='dark'?'light':'dark';  // always flip what you see
 const apply=()=>{{r.dataset.theme=next;try{{localStorage.theme=next}}catch(_){{}}}};
 tb.style.transform=`rotate(${{++turns*180}}deg)`;
 if(!document.startViewTransition||matchMedia('(prefers-reduced-motion: reduce)').matches)return apply();
 const b=tb.getBoundingClientRect(),x=b.left+b.width/2,y=b.top+b.height/2,
  rad=Math.hypot(Math.max(x,innerWidth-x),Math.max(y,innerHeight-y));
 switching=true;r.classList.add('switching');
 const vt=document.startViewTransition(apply);
 vt.ready.then(()=>r.animate({{clipPath:[`circle(0px at ${{x}}px ${{y}}px)`,`circle(${{rad}}px at ${{x}}px ${{y}}px)`]}},
  {{duration:850,easing:getComputedStyle(r).getPropertyValue('--spring'),pseudoElement:'::view-transition-new(root)'}})).catch(()=>{{}});
 vt.finished.finally(()=>{{r.classList.remove('switching');switching=false}})}};

// intro: name lights up dot by dot (5x7 dot-matrix font), then lifts away
(function intro(){{
 const el=document.querySelector('.intro');if(!el)return;
 let seen=false;try{{seen=sessionStorage.introSeen==='1';sessionStorage.introSeen='1'}}catch(_){{}}
 if(seen||location.hash.startsWith('#updated')||matchMedia('(prefers-reduced-motion: reduce)').matches){{el.remove();return}}
 document.documentElement.classList.add('intro-on');
 const F={{A:'0E11111F111111',B:'1E11111E11111E',C:'0E11101010110E',D:'1E11111111111E',E:'1F10101E10101F',
  F:'1F10101E101010',G:'0E11101711110F',H:'1111111F111111',I:'0E04040404040E',J:'0702020202120C',K:'11121418141211',
  L:'1010101010101F',M:'111B1515111111',N:'11111915131111',O:'0E11111111110E',P:'1E11111E101010',Q:'0E11111115120D',
  R:'1E11111E141211',S:'0F10100E01011E',T:'1F040404040404',U:'1111111111110E',V:'11111111110A04',
  W:'1111111515150A',X:'11110A040A1111',Y:'1111110A040404',Z:'1F01020408101F',' ':'00000000000000'}};
 const text=(document.querySelector('header h1').textContent||'HELLO').toUpperCase().replace(/[^A-Z ]/g,'').slice(0,12)||'HELLO';
 const cols=text.length*6-1,rows=7,cv=el.querySelector('canvas'),ctx=cv.getContext('2d'),dpr=devicePixelRatio||1;
 const pitch=Math.min(26,Math.floor(Math.min(innerWidth*.78/cols,innerHeight*.32/rows)));
 cv.width=cols*pitch*dpr;cv.height=rows*pitch*dpr;cv.style.width=cols*pitch+'px';cv.style.height=rows*pitch+'px';ctx.scale(dpr,dpr);
 const dots=[];
 [...text].forEach((ch,ci)=>{{const g=F[ch]||F[' '];for(let r=0;r<7;r++){{const bits=parseInt(g.substr(r*2,2),16);
  for(let c=0;c<5;c++)dots.push({{x:ci*6+c,y:r,on:!!(bits&(16>>c))}})}}}});
 dots.forEach(d=>d.t=d.on?200+d.x*62+Math.random()*320:0);
 const css=getComputedStyle(document.documentElement),fg=css.getPropertyValue('--text').trim(),dim=css.getPropertyValue('--line').trim();
 const spring=t=>t>=1?1:1-Math.exp(-7*t)*Math.cos(9*t);  // overshoots a little, then settles
 const t0=performance.now();let done=false;
 function frame(now){{const t=now-t0;ctx.clearRect(0,0,cols*pitch,rows*pitch);
  for(const d of dots){{const cx=d.x*pitch+pitch/2,cy=d.y*pitch+pitch/2;
   ctx.fillStyle=dim;ctx.beginPath();ctx.arc(cx,cy,pitch*.12,0,7);ctx.fill();
   if(d.on&&t>d.t){{const s=spring((t-d.t)/560);ctx.fillStyle=fg;ctx.beginPath();ctx.arc(cx,cy,Math.max(0,pitch*.36*s),0,7);ctx.fill()}}}}
  if(!done)requestAnimationFrame(frame)}}
 requestAnimationFrame(frame);
 // subtitle types itself out
 const sub=el.querySelector('.intro-sub'),
  full='BLACKBOARD BUDDY · '+new Date().toLocaleDateString('en-GB',{{weekday:'short',day:'2-digit',month:'short'}}).toUpperCase().replace(',','');let n=0;
 const typer=setTimeout(function type(){{sub.textContent=full.slice(0,++n);if(n<full.length)setTimeout(type,45)}},200+cols*62);
 const finish=()=>{{if(done)return;done=true;clearTimeout(typer);el.classList.add('out');
  try{{window.SFX&&SFX.play('flash')}}catch(_){{}}
  document.documentElement.classList.remove('intro-on');
  document.querySelectorAll('header > *').forEach(c=>{{c.style.animation='none';c.offsetWidth;c.style.animation=''}});
  const cur=btns.find(b=>b.classList.contains('on'));if(cur)show(cur.dataset.tab,true);
  setTimeout(()=>el.remove(),900)}};
 setTimeout(finish,4200+text.length*80);
 setTimeout(()=>{{if(document.documentElement.classList.contains('intro-on'))finish()}},5500);  // never stay blank
 window.bbMarks&&bbMarks.push('intro');
}})();
// app window hooks (only inside the desktop app)
const st=document.querySelector('.status'),rf=document.querySelector('.refresh');let hideT;
let watchT;
const bbStatusOff=()=>{{st.classList.remove('show','busy');rf.classList.remove('busy');
 document.documentElement.classList.remove('busy')}};
window.bbStatus=(msg,busy)=>{{st.querySelector('span').textContent=msg;st.classList.toggle('busy',!!busy);rf.classList.toggle('busy',!!busy);
 document.documentElement.classList.toggle('busy',!!busy);
 st.classList.add('show');clearTimeout(hideT);clearTimeout(watchT);
 if(!busy){{hideT=setTimeout(()=>st.classList.remove('show'),4000);return}}
 // A busy message waits for the next one to replace it, so if that never comes - the page was
 // reloaded mid-job, say - it would sit there for good. Check now and then whether it is still true.
 const watch=()=>{{watchT=setTimeout(()=>{{if(!window.pywebview)return bbStatusOff();
  pywebview.api.state().then(s=>{{if(s&&s.busy){{if(s.msg)st.querySelector('span').textContent=s.msg;watch()}}
   else bbStatusOff()}}).catch(()=>bbStatusOff())}},8000)}};
 watch()}};
window.addEventListener('pywebviewready',()=>{{document.body.classList.add('app');
 rf.onclick=()=>pywebview.api.refresh();
 document.querySelectorAll('a[href="deadlines.ics"]').forEach(a=>a.onclick=ev=>{{ev.preventDefault();pywebview.api.open_calendar()}});
 if(location.hash.startsWith('#updated'))bbStatus('Up to date',false);
 pywebview.api.state().then(s=>{{if(s.busy)bbStatus(s.msg,true)}})}});
let t='week';try{{t=localStorage.tab||t}}catch(_){{}};show(document.getElementById(t)?t:'week',true);
</script>{CORNER_HTML}<script>window.BB={boot}</script><script>{EXTRA_JS}</script><script>{notebook.NB_JS}</script><script>{online.ON_JS}</script></body></html>"""
    path = OUT_DIR / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path


def rehydrate_dates(ai_results):
    for key in ("plans", "notes"):
        for item in ai_results.get(key) or []:
            for f in ("due", "changed"):
                if isinstance(item.get(f), str):
                    item[f] = parse_date(item[f])
    return ai_results


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "ask"])
    parser.add_argument("question", nargs="*")
    parser.add_argument("--no-ai", action="store_true", help="skip the Claude features")
    parser.add_argument("--rebuild", action="store_true", help="rebuild from the last sync without signing in")
    parser.add_argument("--no-open", action="store_true", help="don't open the dashboard afterwards")
    args = parser.parse_args()

    if args.command == "ask":
        snapshot = load_json(SNAPSHOT, None)
        if not snapshot:
            sys.exit("Run a sync first: python main.py")
        from ai import AI
        slim = {"courses": [{"name": c["name"], "announcements": c["announcements"][:30],
                             "gradebook": c["gradebook"],
                             "contents": [{"path": i["path"], "modified": i["modified"]} for i in c["contents"]]}
                            for c in snapshot["courses"]], "calendar": snapshot["calendar"]}
        print(AI(effort=AI_EFFORT).answer(" ".join(args.question) or "What should I focus on this week?",
                                          json.dumps(slim, default=str)))
        return

    path = run(sync=not args.rebuild, use_ai=not args.no_ai)
    if path and not args.no_open:
        webbrowser.open(path.as_uri())


class SignInNeeded(Exception):
    """Blackboard signed you out and this sync isn't allowed to open a window."""


def open_blackboard(interactive=True):
    """Signs in invisibly when Microsoft remembers you; otherwise shows a sign-in window."""
    from blackboard import Blackboard
    bb = Blackboard(BB_URL, PROFILE_DIR, log=log, headless=True).__enter__()
    try:
        bb.login(timeout_minutes=0.75)
        return bb
    except TimeoutError:
        bb.__exit__()
    if not interactive:
        raise SignInNeeded()
    log("Please sign in to Blackboard in the window that opened")
    bb = Blackboard(BB_URL, PROFILE_DIR, log=log, headless=False).__enter__()
    bb.login()
    return bb


def run(sync=True, use_ai=True, interactive=True):
    """Syncs Blackboard (or reuses the last sync), runs the AI and writes the dashboard."""
    ai_wanted = use_ai and bool(os.getenv("ANTHROPIC_API_KEY"))
    if use_ai and not ai_wanted:
        log("No Claude API key yet, so building the dashboard without AI.")

    if not sync:
        snapshot = load_json(SNAPSHOT, None)
        if not snapshot:
            log("No previous sync found.")
            return None
        deadlines, announcements, grades = (collect_deadlines(snapshot),
                                            recent_announcements(snapshot), released_grades(snapshot))
        ai_results = rehydrate_dates(load_json(DATA_DIR / "ai-latest.json", {}))
    else:
        bb = open_blackboard(interactive)
        try:
            me = bb.me
            courses = [c for c in bb.courses() if c["current"]]
            log(f"Reading your {len(courses)} modules")
            for c in courses:
                log(f"Reading {c['name']}")
                c["gradebook"] = bb.gradebook(c["id"])
                c["announcements"] = bb.announcements(c["id"])
                c["contents"] = bb.contents(c["id"])
            sync_materials(bb, courses, load_json(SNAPSHOT, None))
            snapshot = {"synced": now().isoformat(),
                        "me": {"id": me.get("id"), "userName": me.get("userName"),
                               "name": (me.get("name") or {}).get("given", "")},
                        "courses": courses, "calendar": bb.calendar(days_ahead=LOOKAHEAD_DAYS + 30)}
            save_json(SNAPSHOT, snapshot)
            deadlines, announcements, grades = (collect_deadlines(snapshot),
                                                recent_announcements(snapshot), released_grades(snapshot))
            log(f"{len(deadlines)} deadlines, {len(announcements)} recent announcements, {len(grades)} marks")
            ai_results = run_ai(snapshot, bb, deadlines, announcements, grades) if ai_wanted \
                else rehydrate_dates(load_json(DATA_DIR / "ai-latest.json", {}))
        finally:
            bb.__exit__()

    write_ics(deadlines)
    path = build_dashboard(snapshot, deadlines, announcements, grades, ai_results)
    log("Dashboard ready")
    return path


if __name__ == "__main__":
    main()
