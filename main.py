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
from html.parser import HTMLParser
from pathlib import Path

import markdown
from dotenv import load_dotenv

import online
import vault as notebook

IS_MAC = sys.platform == "darwin"
HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LOCAL = (Path.home() / "Library" / "Application Support" if IS_MAC else Path(os.getenv("LOCALAPPDATA", HERE))) / "BlackboardBuddy"


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
# On a Mac the app is a sealed bundle that an update replaces whole, so everything lives in Application Support.
BASE = LOCAL if IS_MAC and getattr(sys, "frozen", False) else HERE if _writable(HERE) else LOCAL
BASE.mkdir(parents=True, exist_ok=True)


def open_path(path):
    """Open a file, folder or web address in its own app: Explorer and friends, or Finder on a Mac."""
    if IS_MAC:
        import subprocess
        subprocess.Popen(["open", str(path)])
    else:
        os.startfile(path)
ENV = BASE / ".env"
load_dotenv(ENV)

BB_URL = os.getenv("BB_URL", "https://blackboard.uwe.ac.uk")
VERSION = "1.2.0"  # bump this for every release you upload
UPDATE_REPO = "CatForSale/blackboard-buddy-releases"  # GitHub "owner/repo" whose Releases hold new versions; empty = no update checks
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "45"))
NOTES_DAYS = int(os.getenv("NOTES_DAYS", "14"))
NOTES_PER_RUN = int(os.getenv("NOTES_PER_RUN", "8"))
PLANS_PER_RUN = int(os.getenv("PLANS_PER_RUN", "6"))
AI_EFFORT = os.getenv("AI_EFFORT", "medium")
AI_NEW = False   # whether the last run_ai wrote anything new

DATA_DIR = BASE / "data"
notebook.ROOT = BASE / "Notebook"
OUT_DIR = BASE / "output"
PROFILE_DIR = LOCAL / "browser-profile"
MATERIALS = LOCAL / "materials"  # downloaded module files; kept out of OneDrive unless you pick a folder in Settings
MATERIALS_NAME = "Blackboard study files"
SNAPSHOT = DATA_DIR / "snapshot.json"
CACHE = DATA_DIR / "ai-cache.json"
SETTINGS = DATA_DIR / "settings.json"
SHARED = DATA_DIR / "shared"            # the notes someone shared with you, when you follow them
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


def following():
    """Whose shared notes this app shows instead of its own Blackboard sync, or None."""
    f = load_json(SETTINGS, {}).get("follow")
    return f if isinstance(f, dict) and f.get("code") else None


def snap_file():
    return SHARED / "snapshot.json" if following() else SNAPSHOT


def ai_file():
    return SHARED / "ai-latest.json" if following() else DATA_DIR / "ai-latest.json"


def share_payload():
    """Your synced Blackboard and Claude's notes, minus anything personal: no marks, feedback, submission
    status, Blackboard ID or file paths on this computer. Returns None before the first sync."""
    snap = load_json(SNAPSHOT, None)
    if not snap:
        return None
    ai = load_json(DATA_DIR / "ai-latest.json", {})
    keep_gb = ("id", "name", "description", "due", "contentId", "possible", "overall")
    courses = []
    for c in snap.get("courses", []):
        c = dict(c)
        c["gradebook"] = [{k: g.get(k) for k in keep_gb} for g in c.get("gradebook") or []]
        c["contents"] = [{**{k: v for k, v in i.items() if k != "files"}, "files": []} for i in c.get("contents") or []]
        courses.append(c)
    return {"v": 1, "from": os.getenv("USER_NAME") or "", "synced": snap.get("synced"),
            "snapshot": {"synced": snap.get("synced"), "me": {"id": None, "userName": "", "name": ""},
                         "courses": courses, "calendar": snap.get("calendar") or []},
            "ai": {"briefing": ai.get("briefing"), "plans": ai.get("plans") or [], "notes": ai.get("notes") or []}}


class _Clean(HTMLParser):
    """Keeps plain formatting tags and https links; drops scripts, styles, embeds and every other attribute."""
    OK = {"p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li", "a", "span", "div", "h1", "h2", "h3", "h4",
          "h5", "h6", "table", "thead", "tbody", "tr", "td", "th", "blockquote", "code", "pre", "hr", "sub", "sup"}
    DROP = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "textarea",
            "select", "form", "frame", "frameset", "title", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.skip += 1
            return
        if self.skip or tag not in self.OK:
            return
        a = dict(attrs)
        extra = ""
        if tag == "a" and re.match(r"(https?://|mailto:|#)", (a.get("href") or "").strip(), re.I):
            extra = f' href="{html.escape(a["href"].strip())}" target="_blank" rel="noopener"'
        if tag == "code" and re.fullmatch(r"language-[\w+-]{1,30}", a.get("class") or ""):
            extra = f' class="{a["class"]}"'
        self.out.append(f"<{tag}{extra}>")

    def handle_startendtag(self, tag, attrs):
        if tag not in self.DROP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in self.OK and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def safe_html(text):
    c = _Clean()
    c.feed(str(text or ""))
    c.close()
    return "".join(c.out)


def defang_share(x, key=""):
    """Someone else's shared notes are shown inside this app, so nothing in them may run: page HTML is cut
    down to plain formatting, addresses must be https, and other text can't open a tag."""
    if isinstance(x, dict):
        return {str(k)[:60]: defang_share(v, str(k)) for k, v in x.items()}
    if isinstance(x, list):
        return [defang_share(v, key) for v in x]
    if not isinstance(x, str):
        return x if isinstance(x, (int, float, bool)) or x is None else None
    if key in ("body", "html", "description"):
        return safe_html(x)
    if key == "url":
        return x if x.startswith("https://") and '"' not in x else ""
    if key in ("md", "briefing"):
        return x      # Markdown: md() cleans what it makes while following someone
    return x.replace("<", "‹")


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

def run_ai(snapshot, bb, deadlines, announcements, grades, old=None):
    global AI_NEW
    from ai import AI, file_to_blocks
    import anthropic

    cache = load_json(CACHE, {})
    ai = new_ai() or AI(effort=AI_EFFORT)
    results = {"briefing": None, "plans": [], "notes": [], "feedback": None}
    today = datetime.now().strftime("%A %d %B %Y")
    by_id = {c["id"]: c for c in snapshot["courses"]}

    def item_blocks(course_id, item):
        blocks = []
        if strip_html(item.get("body")):
            blocks.append({"type": "text", "text": "Item text:\n" + strip_html(item["body"])[:50_000]})
        for att in bb.attachments(course_id, item["id"]) if bb else []:
            data = bb.download(att["downloadPath"])
            if data:
                blocks += file_to_blocks(att["fileName"], data)
        return blocks

    # Downloads go through the one browser, so they happen first, in order; then everything that needs
    # writing is asked of Claude side by side (four at a time) instead of one after another.
    jobs = {}   # cache key -> (what the status says, the call that writes it)

    def want(key, label, fn):
        if key not in cache and key not in jobs:
            jobs[key] = (label, fn)

    def ask_all():
        if not jobs:
            return
        from concurrent.futures import ThreadPoolExecutor

        def job(label, fn):
            log(label)
            return fn()
        err = None
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {k: pool.submit(job, *v) for k, v in jobs.items()}
            for k, f in futures.items():
                try:
                    cache[k] = f.result()
                except Exception as e:   # keep what did get written, then report the first problem
                    err = err or e
        save_json(CACHE, cache)
        if err:
            raise err

    bkey = gkey = None
    plan_rows, note_rows = [], []
    # every item that had notes before, so a changed file gets fresh notes however old it is
    had = {}
    for k in cache:
        if k.startswith("notes:"):
            had.setdefault(k.split(":")[1], []).append(k)
    old_items = {c["id"]: {i["id"] for i in c.get("contents", [])} for c in (old or {}).get("courses", [])}
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
        want(bkey, "AI: writing weekly briefing", lambda: ai.weekly_briefing(*brief_in))

        open_tasks = [d for d in deadlines if not d["submitted"] and d["due"] > now()]
        for d in open_tasks[:PLANS_PER_RUN]:
            course = by_id.get(d["courseId"], {})
            item = next((i for i in course.get("contents", []) if i["id"] == d.get("contentId")), None)
            key = f"plan:{d['courseId']}:{d['title']}:{d['due'].isoformat()}:{(item or {}).get('modified')}"
            plan_rows.append((d, key))
            if key in cache:
                continue
            col = next((c for c in course.get("gradebook", []) if c["id"] == d.get("columnId")), {})
            info = {"title": d["title"], "due": d["due"].strftime("%A %d %B %Y %H:%M"),
                    "points_possible": col.get("possible"),
                    "description": strip_html(col.get("description", "")),
                    "instructions": strip_html((item or {}).get("body", ""))}
            blocks = item_blocks(d["courseId"], item) if item else []
            want(key, f"AI: assignment plan for {d['title']}",
                 lambda name=course.get("name", d["course"]), info=info, blocks=blocks: ai.assignment_plan(name, info, blocks, today))

        # Notes are written for: anything changed in the last NOTES_DAYS, anything that had notes and has
        # changed since (in any module, however old), and anything newly added to a module seen before.
        # A module seen for the first time doesn't count as all new, so adding earlier years costs nothing.
        cutoff = now() - timedelta(days=NOTES_DAYS)
        candidates = []
        for c in snapshot["courses"]:
            for i in c["contents"]:
                changed = parse_date(i.get("modified") or i.get("created"))
                renewed = i["id"] in had
                added = c["id"] in old_items and i["id"] not in old_items[c["id"]]
                if changed and (changed >= cutoff or renewed or added) and i["handler"] in FILE_HANDLERS \
                        and i["handler"] != "resource/x-bb-folder" and not ADMIN_PAGES.search(i["path"]) \
                        and not re.search(r"\.(csv|xlsx?|zip)$", i["title"], re.I):
                    candidates.append((changed, c, i, renewed))
        candidates.sort(key=lambda x: (x[3], x[0]), reverse=True)    # refreshes first, then newest
        new_count = renewed_count = 0
        for changed, c, i, renewed in candidates:
            key = f"notes:{i['id']}:{i.get('modified')}"
            if key not in cache:
                # refreshing notes you already had has its own allowance, so it never waits behind new files
                if (renewed_count >= 20) if renewed else (new_count >= settings()["notes_per_sync"]):
                    continue
                blocks = item_blocks(c["id"], i)
                if not blocks:
                    continue
                if renewed:
                    renewed_count += 1
                else:
                    new_count += 1
                want(key, f"AI: study notes for {i['title']}", lambda name=c["name"], path=i["path"], blocks=blocks: ai.study_notes(name, path, blocks))
            note_rows.append((changed, c, i, key))

        if grades:
            gkey = "feedback:" + hashlib.sha1(json.dumps(grades, sort_keys=True).encode()).hexdigest()
            want(gkey, "AI: summarising grades and feedback", lambda: ai.grade_feedback(grades))
        ask_all()
    except anthropic.AuthenticationError:
        log("AI skipped: your ANTHROPIC_API_KEY in .env is missing or invalid.")
    except anthropic.APIConnectionError:
        log("AI stopped: could not reach the Claude API (check your internet).")
    except anthropic.APIStatusError as e:
        log(f"AI stopped: Claude API error {e.status_code}: {e.message}")

    AI_NEW = bool(jobs)
    # whatever got written (all of it, unless something went wrong part way)
    results["briefing"] = cache.get(bkey)
    results["plans"] = [{"course": d["course"], "title": d["title"], "due": d["due"], "md": cache[key]}
                        for d, key in plan_rows if key in cache]
    results["notes"] = [{"course": c["name"], "title": i["title"], "path": i["path"], "changed": changed, "md": cache[key]}
                        for changed, c, i, key in note_rows if key in cache]
    # keep older notes too (including ones made on demand from the Notes tab)
    have = {(n["course"], n["path"]) for n in results["notes"]}
    for c in snapshot["courses"]:
        for i in c["contents"]:
            key = f"notes:{i['id']}:{i.get('modified')}"
            if key not in cache and had.get(i["id"]):    # changed, new notes not written yet: keep the last ones
                key = max(had[i["id"]])
            if key in cache and (c["name"], i["path"]) not in have:
                results["notes"].append({"course": c["name"], "title": i["title"], "path": i["path"],
                                         "changed": parse_date(i.get("modified") or i.get("created")),
                                         "md": cache[key]})
    results["feedback"] = cache.get(gkey) if gkey else None

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


def term_start(courses):
    """Monday of teaching week 1, in milliseconds. Module codes end in things like "26sep" (the
    September 2026 intake), and UWE teaching starts on the Monday on or after the 18th of that month
    (12th for a January start)."""
    seen = {}
    for c in courses:
        m = re.search(r"(\d{2})(sep|jan)", c.lower())
        if m:
            seen[m.groups()] = seen.get(m.groups(), 0) + 1
    if seen:
        yy, mon = max(seen, key=seen.get)
        d = datetime(2000 + int(yy), 9 if mon == "sep" else 1, 18 if mon == "sep" else 12)
    else:
        t = datetime.now()
        d = datetime(t.year if t.month >= 8 else t.year - 1, 9, 18)
    d += timedelta(days=(7 - d.weekday()) % 7)
    return int(d.timestamp() * 1000)


def said_week(*texts):
    """The teaching week a note's own title or folder names ("Week 5", "Wk5"), or 0."""
    m = re.search(r"\bw(?:ee)?k\s*0?(\d{1,2})(?!\d)", " ".join(t or "" for t in texts), re.I)
    return int(m.group(1)) if m and 0 < int(m.group(1)) <= 30 else 0


def when_ms(value):
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


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
/* the spinning arc lives on ::after: the ring's own transform is where the script puts it on the mouse,
   and an animation on that transform would throw the ring into the top-left corner */
html.busy .pointer-ring{border-color:transparent;opacity:.95}
html.busy .pointer-ring::after{content:"";position:absolute;inset:-1.4px;border-radius:inherit;border:2px solid transparent;
 border-top-color:var(--red);animation:ptr-spin .9s linear infinite}
@keyframes ptr-spin{to{transform:rotate(360deg)}}
html.no-anim .pointer-ring,html.no-anim .pointer-dot{transition:none}
html.no-anim.busy .pointer-ring::after{animation:none;border-color:var(--red)}

/* ---- no Claude key yet: just Online and Notebook */
html.locked nav button[data-tab]:not([data-tab=online]):not([data-tab=notebook]),html.locked .refresh{display:none!important}
html.following nav button[data-tab=grades]{display:none!important}
.sh-about{font-size:13px;line-height:1.5;color:var(--muted);margin:0 0 10px}
.sh-card{border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:0 0 16px;display:flex;flex-direction:column;gap:10px}
.sh-code{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sh-code b{font:700 26px/1 var(--mono);letter-spacing:.12em}
.sh-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.sh-card small{color:var(--muted);font-size:12px;line-height:1.45}
.sh-in{flex:1;min-width:140px;border:1px solid var(--line);border-radius:10px;padding:9px 11px;font:15px var(--mono);letter-spacing:.1em;text-transform:uppercase;background:var(--inset);color:var(--text)}
.sh-who{font-size:14px}
.sh-msg{display:block;min-height:16px;color:var(--red);font-size:12.5px}
.follow-note{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 14px;padding:9px 14px;border-radius:14px;background:var(--inset);font-size:13px}
.follow-note i{width:8px;height:8px;border-radius:50%;background:#2fd27a;box-shadow:0 0 0 4px rgba(47,210,122,.18);flex:none}
.follow-note button{margin-left:auto;border:0;background:none;color:var(--text);text-decoration:underline;cursor:pointer;font:inherit}
.unlock-b{margin-left:6px;border:1px solid var(--red);background:transparent;color:var(--red);border-radius:999px;padding:6px 12px;
 font:10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;white-space:nowrap}
.unlock-b:hover{background:var(--red);color:#fff}
.unlock{position:fixed;inset:0;z-index:90;display:grid;place-items:center;background:rgba(0,0,0,.55);backdrop-filter:blur(10px)}
.unlock-card{width:min(420px,90vw);padding:26px;border-radius:22px;background:var(--bg);color:var(--text);border:1px solid var(--line);box-shadow:0 24px 70px rgba(0,0,0,.35)}
.unlock-card h2{margin:0 0 6px} .unlock-card p{color:var(--muted);font-size:13.5px;margin:0 0 10px}
.unlock-card label{display:block;margin:12px 0 5px;font:10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.unlock-card input{width:100%;box-sizing:border-box;padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:transparent;color:inherit;font:inherit}
.unlock-card .umsg{color:var(--red);min-height:1em;margin-top:10px}
.unlock-card .ubtns{display:flex;gap:8px;justify-content:flex-end}

/* ---- where to put the window: our own Snap Layouts, since frameless loses the real one */
.snapmenu{position:fixed;top:38px;right:12px;z-index:62;display:none;grid-template-columns:repeat(4,1fr);gap:5px;padding:8px;
 border-radius:14px;background:var(--card);border:1px solid var(--line);box-shadow:0 16px 44px rgba(0,0,0,.26)}
.snapmenu:not([hidden]){display:grid}
.snapmenu button{width:38px;height:28px;border:1px solid var(--line);border-radius:7px;background:transparent;
 cursor:pointer;padding:0;position:relative;transition:border-color .18s,background .18s}
.snapmenu button:hover{border-color:var(--text);background:var(--inset)}
.snapmenu i{position:absolute;background:var(--text);opacity:.75;border-radius:2px}
.snapmenu .sl{inset:5px 50% 5px 5px} .snapmenu .sr{inset:5px 5px 5px 50%}
.snapmenu .sf{inset:5px} .snapmenu .stop{inset:5px 5px 50% 5px}
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
/* a Mac window keeps its own title bar and traffic lights, so none of the drawn ones */
html.mac .titlebar,html.mac .wctl,html.mac .redge,html.mac .snapmenu{display:none!important}
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
/* profile card */
.acct .av[data-shape]{width:34px;height:34px;display:grid;place-items:center;font-size:16px;border-radius:50%}
.av[data-shape=square],.pc-av[data-shape=square]{border-radius:12px}
.av[data-shape=hex],.pc-av[data-shape=hex]{border-radius:0;clip-path:polygon(25% 5%,75% 5%,100% 50%,75% 95%,25% 95%,0 50%)}
.pcard{position:relative;border-radius:22px;overflow:hidden;border:1px solid var(--line);margin:8px 0 6px;background:rgba(127,127,127,.06);--pc:#d71921}
.pc-ban{height:80px;transition:background .3s;position:relative;overflow:hidden}
.pc-av{position:absolute;left:18px;top:42px;width:68px;height:68px;display:grid;place-items:center;font:800 30px/1 var(--dotf);
 text-transform:uppercase;border-radius:50%;box-shadow:0 0 0 4px var(--bg),0 0 0 6px var(--pc),0 10px 28px rgba(0,0,0,.35);transition:border-radius .2s}
.pc-av[data-shape=hex]{box-shadow:none;filter:drop-shadow(0 6px 14px rgba(0,0,0,.4))}
.pc-txt{padding:38px 18px 16px;display:flex;flex-direction:column;gap:4px}
.pc-name{font:800 22px/1.1 var(--dotf);letter-spacing:.03em;overflow-wrap:anywhere}
.pc-tag{font-size:13px} .pc-tag:empty,.pc-meta:empty{display:none}
.pc-meta{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
.pc-meta::before{content:'';display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--pc);margin-right:7px;vertical-align:1px}
.pc-open{position:absolute;right:12px;top:90px;border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;
 padding:6px 14px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:border-color .2s}
.pc-open:hover{border-color:var(--pc)}
.pc-edit{display:grid;gap:10px;padding:10px 2px 4px} .pc-edit[hidden]{display:none}
.pc-two{display:grid;grid-template-columns:110px 1fr;gap:10px}
.pc-f{display:flex;flex-direction:column;gap:5px}
.pc-f span,.pc-l{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.pc-f input{width:100%;box-sizing:border-box;background:rgba(127,127,127,.08);border:1px solid var(--line);border-radius:12px;padding:10px 12px;
 color:var(--text);font:inherit;font-size:14px;outline:0;transition:border-color .2s,box-shadow .2s}
.pc-f input:focus{border-color:var(--pc);box-shadow:0 0 0 3px color-mix(in srgb,var(--pc) 30%,transparent)}
.pc-f-name input{font:800 20px/1.2 var(--dotf);letter-spacing:.04em;padding:12px 14px}
.pc-row{display:flex;flex-wrap:wrap;gap:8px}
.pc-chip{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:10px;padding:7px 12px;font:inherit;font-size:12px;
 cursor:pointer;transition:transform .15s}
.pc-chip:hover{transform:translateY(-1px)} .pc-chip.on{outline:2px solid var(--text);outline-offset:2px}
.pc-chip.ban{width:46px;height:28px;padding:0;border:0} .pc-chip.dot{width:26px;height:26px;padding:0;border-radius:50%}
.pc-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px} .pc-badges:empty{display:none}
.pc-badges span{font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;border:1px solid color-mix(in srgb,var(--pc) 55%,transparent);
 background:color-mix(in srgb,var(--pc) 14%,transparent)}
.pc-name[data-font=sans],.pc-name[data-font=serif],.pc-name[data-font=round]{letter-spacing:0;font-weight:700}
.pc-own{align-items:center} .pc-mini{font-size:11px;color:var(--muted)}
.pc-own input[type=color],.pc-acc-own{width:34px;height:28px;padding:0;border:1px solid var(--line);border-radius:8px;background:none;cursor:pointer}
.pc-acc-own{margin-top:-4px;width:26px;height:26px;border-radius:50%}
.pc-ring{position:absolute;left:12px;top:36px;width:80px;height:80px;border-radius:50%;display:none;
 background:conic-gradient(var(--pc),transparent 30%,var(--pc) 55%,transparent 80%,var(--pc));animation:pcspin 2.6s linear infinite}
.pc-ring[data-shape=square]{border-radius:18px} .pc-ring[data-shape=hex]{border-radius:0;clip-path:polygon(25% 5%,75% 5%,100% 50%,75% 95%,25% 95%,0 50%)}
.pcard[data-fx=ring] .pc-ring{display:block} .pcard[data-fx=ring] .pc-av{box-shadow:0 0 0 4px var(--bg)}
.pcard[data-fx=glow]{border-color:var(--pc);box-shadow:0 0 0 1px var(--pc),0 0 34px color-mix(in srgb,var(--pc) 45%,transparent)}
.pcard[data-fx=shimmer] .pc-ban::after{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 30%,rgba(255,255,255,.4) 50%,transparent 70%);
 background-size:250% 100%;animation:pcshim 3s linear infinite}
.pcard[data-fx=float] .pc-av{animation:pcfloat 3s ease-in-out infinite}
@keyframes pcspin{to{transform:rotate(1turn)}}
@keyframes pcshim{from{background-position:150% 0}to{background-position:-100% 0}}
@keyframes pcfloat{50%{transform:translateY(-6px)}}
@media (prefers-reduced-motion:reduce){.pc-ring,.pcard .pc-av,.pcard .pc-ban::after{animation:none!important}}
.pc-save{margin-top:4px;border:0;border-radius:14px;padding:12px;font:inherit;font-weight:700;cursor:pointer;background:var(--pc);color:var(--pc-ink,#fff)}
/* loading a backup */
.bk-modal{position:fixed;inset:0;z-index:60;display:grid;place-items:center;background:rgba(0,0,0,.55);backdrop-filter:blur(6px)}
.bk-box{width:min(440px,calc(100vw - 32px));max-height:min(560px,calc(100vh - 48px));overflow:auto;background:var(--bg);color:var(--text);
 border:1px solid var(--line);border-radius:24px;padding:20px 22px;box-shadow:0 24px 70px rgba(0,0,0,.5)}
.bk-box h3{margin:0 0 8px;font:800 20px/1.2 var(--dotf)}
.bk-box p{margin:0 0 12px;font-size:14px;line-height:1.5;color:var(--muted)}
.bk-box .bk-warn{border-left:3px solid var(--red);padding:10px 12px;background:rgba(215,25,33,.1);border-radius:10px;color:var(--text)}
.bk-list{display:flex;flex-direction:column;gap:6px}
.bk-list button{display:flex;justify-content:space-between;gap:10px;text-align:left;border:1px solid var(--line);background:transparent;color:var(--text);
 border-radius:12px;padding:10px 12px;font:inherit;font-size:14px;cursor:pointer}
.bk-list button:hover{border-color:var(--text)} .bk-list small{color:var(--muted)}
.un-zone{border-top:1px dashed color-mix(in srgb,var(--red) 50%,transparent);padding-top:14px}
.un-box{width:min(520px,calc(100vw - 32px));max-height:min(680px,calc(100vh - 48px))}
.un-steps{display:flex;gap:6px;margin:0 0 14px} .un-steps i{flex:1;height:4px;border-radius:2px;background:var(--line)} .un-steps i.on{background:var(--red)}
.un-list{display:flex;flex-direction:column;gap:6px}
.un-item{display:grid;grid-template-columns:auto 1fr auto;gap:4px 10px;align-items:start;border:1px solid var(--line);border-radius:12px;padding:10px 12px;cursor:pointer}
.un-item:hover{border-color:var(--text)} .un-item.on{border-color:var(--red);background:rgba(215,25,33,.07)}
.un-item input{margin:3px 0 0;accent-color:var(--red)} .un-item b{font-size:14px} .un-item small{grid-column:2/4;color:var(--muted);font-size:12px;line-height:1.4;overflow-wrap:anywhere}
.un-item em{font:11px var(--mono);font-style:normal;color:var(--muted);white-space:nowrap}
.un-item.lock{cursor:default;opacity:.8}
.un-all{display:flex;justify-content:space-between;align-items:center;margin:0 0 8px;font-size:13px;color:var(--muted)}
.un-all button{border:0;background:none;color:var(--text);text-decoration:underline;cursor:pointer;font:inherit}
.un-sum{margin:0 0 12px;padding:0;list-style:none;font-size:13.5px;line-height:1.6} .un-sum li::before{content:"✕  ";color:var(--red);font-weight:700}
.un-sum.keep li::before{content:"✓  ";color:var(--green,#2a9d5b)}
.un-type{width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:15px var(--mono);background:var(--inset);color:var(--text)}
.un-type.ok{border-color:var(--red)}
.bk-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.bk-btns button{border-radius:12px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--text)}
.bk-btns .bk-yes{background:var(--red);border-color:var(--red);color:#fff;min-width:150px}
.bk-btns button:disabled{opacity:.45;cursor:not-allowed}
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
.map-wrap{border-radius:22px;padding:12px 14px 12px;position:relative}
.nmap{display:block;width:100%;height:clamp(440px,64vh,680px);touch-action:none} .nmap.on-dot{cursor:pointer}
.map-wrap.full{position:fixed;inset:16px;z-index:58;display:flex;flex-direction:column;background:var(--bg);box-shadow:0 0 0 100vmax rgba(0,0,0,.6)}
.map-wrap.full .map-stage{flex:1;min-height:0} .map-wrap.full .nmap{height:100%}
.map-stage{position:relative}
.map-bar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 2px 8px}
.map-modes{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.map-modes span{margin-right:4px}
.map-modes button,.map-full{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;padding:4px 11px;font:11px var(--mono);letter-spacing:.04em;text-transform:uppercase;cursor:pointer}
.map-modes button.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.map-full{font-size:14px;padding:3px 10px;text-transform:none}
.map-legend{display:flex;align-items:center;flex-wrap:wrap;gap:6px 16px;margin:0 4px 6px;font:11px var(--mono);color:var(--muted);min-height:18px}
.map-legend .ml-ramp{display:inline-block;width:150px;height:8px;border-radius:4px;vertical-align:middle;margin:0 6px}
.map-legend .ml-sw{display:inline-flex;align-items:center;gap:5px} .map-legend .ml-sw i{width:9px;height:9px;border-radius:50%;background:var(--c)}
.map-legend .ml-shape{margin-left:auto;display:flex;flex-wrap:wrap;gap:4px 14px}
.map-legend .ml-key{display:inline-flex;align-items:center;gap:5px} .map-legend .ml-ico{flex:none;display:block}.map-legend .ml-hint{opacity:.65;font-size:.92em}
.map-weeks{display:flex;align-items:flex-end;gap:3px;margin:8px 2px 0;overflow-x:auto;padding-bottom:2px}
.map-weeks button{flex:1 0 34px;display:flex;flex-direction:column;align-items:stretch;gap:3px;border:1px solid transparent;background:transparent;
 color:var(--muted);border-radius:8px;padding:3px 3px 2px;cursor:pointer;font:10px var(--mono)}
.map-weeks button i{display:block;height:var(--h);min-height:2px;border-radius:3px;background:var(--c)}
.map-weeks button.empty i{opacity:.3}
.map-weeks button:hover{border-color:var(--line);color:var(--text)} .map-weeks button.on{border-color:var(--text);color:var(--text)}
.map-weeks button.now b{color:var(--text)} .map-weeks b{font-weight:400}
.map-tip{position:absolute;z-index:2;pointer-events:none;max-width:280px;background:var(--bg);color:var(--text);border:1px solid var(--line);
 border-radius:12px;padding:9px 11px;font-size:12.5px;line-height:1.45;box-shadow:0 8px 30px rgba(0,0,0,.35)}
.map-tip b{display:block;font-size:13.5px;margin-bottom:2px} .map-tip small{display:block;color:var(--muted);overflow-wrap:anywhere}
.map-tip .mt-row{display:flex;gap:6px;align-items:center} .map-tip .mt-row i{width:9px;height:9px;border-radius:50%;background:var(--c);flex:none}
.map-tip em{display:block;margin-top:4px;font:10.5px var(--mono);font-style:normal;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.map-tip .mt-h{margin-top:4px;color:var(--text)!important;font-weight:600}
.map-tip .mt-link{display:grid;gap:0;padding:3px 0 3px 9px;border-left:2px solid var(--muted);margin:3px 0}
.map-tip .mt-link span{font-size:12px} .map-tip .mt-link small{font-size:11px}
.map-tip .mt-link.mt-link{border-left-color:var(--muted)} .map-tip .mt-link.mt-sim{border-left-style:dashed}
.map-tip .mt-bar{height:5px;border-radius:3px;background:var(--inset);margin:6px 0 2px;overflow:hidden} .map-tip .mt-bar i{display:block;height:100%;background:var(--text)}
.map-tip .mt-new{display:inline-block;font:10px var(--mono);padding:0 5px;border-radius:4px;background:var(--text);color:var(--bg);margin-left:4px;vertical-align:1px}
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
      if (!(m.buttons & 1)) return stop();      // let go somewhere the page didn't hear about
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
    const grip = t.closest && t.closest('.ol-rs,[data-rz]');
    const gc = grip && (grip.dataset.c || grip.dataset.rz);
    const edge = t.classList && [...t.classList].find(c => c in EDGE_TURN);
    ring.dataset.turn = edge ? EDGE_TURN[edge] : gc ? (gc === 'ne' || gc === 'sw' ? -45 : 45) : 0;
    const shape = edge || gc ? 'rz' : off ? 'no' : near(SLIDER) ? 'slide' : near(TYPEABLE) ? 'text'
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
    el.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      if (!window.pywebview || ev.button !== 0) return;
      // Everything is listened for straight away: waiting for the window's size first meant a quick
      // click could be over before the page was listening for the button coming up, and the edge
      // then went on resizing the window whenever the mouse passed over it.
      const dpr = devicePixelRatio || 1, x0 = ev.screenX, y0 = ev.screenY;
      const [w_, n_, e_, s_] = side;
      let start = null, want = null, queued = false, live = true;
      try { el.setPointerCapture(ev.pointerId); } catch (_) {}
      const push = () => {
        queued = false;
        if (live && want) pywebview.api.win_set_bounds(want.x, want.y, want.w, want.h);
      };
      const move = m => {
        if (!(m.buttons & 1)) return stop();
        if (!start) return;
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
        live = false;
        el.removeEventListener('pointermove', move);
        el.removeEventListener('pointerup', stop);
        el.removeEventListener('pointercancel', stop);
        el.removeEventListener('lostpointercapture', stop);
        removeEventListener('blur', stop);
        try { el.releasePointerCapture(ev.pointerId); } catch (_) {}
      };
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerup', stop);
      el.addEventListener('pointercancel', stop);
      el.addEventListener('lostpointercapture', stop);
      addEventListener('blur', stop);
      pywebview.api.win_bounds().then(b => { if (!b) stop(); else start = b; }, stop);
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
document.querySelector('.acct .av').textContent = ((BB.profile && BB.profile.avatar) || BB.name || '?')[0].toUpperCase();
const SY = A.querySelector('.s-sync'), NP = A.querySelector('.s-notes'), NPV = A.querySelector('.s-notes-v');
SY.value = String(BB.settings.sync_hours); NP.value = BB.settings.notes_per_sync; NPV.textContent = NP.value;
const saveSettings = () => window.pywebview && pywebview.api.save_settings(
  {sync_hours: +SY.value, notes_per_sync: +NP.value, model: BB.settings.model,
   effort: BB.settings.effort, sounds: BB.settings.sounds, volume: BB.settings.volume});
SY.onchange = saveSettings; NP.oninput = () => { NPV.textContent = NP.value; }; NP.onchange = saveSettings;
A.querySelector('.s-files').onclick = async () => {
  const r = await pywebview.api.open_materials(); if (r && r !== 'ok') A.querySelector('.s-files-msg').textContent = r; };
A.querySelector('.s-files-path').textContent = BB.materials;
// ---- the profile card: a live preview, changed in place, saved to data/profile.json
const PC_BANNERS = {aurora: 'linear-gradient(135deg,#7f5af0,#2cb67d)', sunset: 'linear-gradient(135deg,#ff6b6b,#f7b267)',
  ocean: 'linear-gradient(135deg,#023e8a,#00b4d8)', night: 'linear-gradient(135deg,#0f0c29,#302b63,#24243e)',
  candy: 'linear-gradient(135deg,#f72585,#7209b7)', ember: 'linear-gradient(135deg,#d71921,#3a0306)', mono: 'linear-gradient(135deg,#1c1c1c,#4a4a4a)'};
const PC_PATTERNS = {none: '', dots: 'radial-gradient(rgba(255,255,255,.3) 1.2px,transparent 1.7px) 0 0/12px 12px',
  grid: 'linear-gradient(rgba(255,255,255,.16) 1px,transparent 1px) 0 0/16px 16px,linear-gradient(90deg,rgba(255,255,255,.16) 1px,transparent 1px) 0 0/16px 16px',
  stripes: 'repeating-linear-gradient(45deg,rgba(255,255,255,.14) 0 6px,transparent 6px 14px)'};
const PC_ACCENTS = ['#d71921', '#ff8a00', '#f5c400', '#2cb67d', '#00b4d8', '#4361ee', '#7f5af0', '#f72585', '#f2f2f2', '#111111'];
const PC_FONTS = {dot: ['Dot', 'var(--dotf)'], sans: ['Clean', '"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif'],
  serif: ['Serif', 'Georgia,"Times New Roman",serif'], mono: ['Code', '"Cascadia Code",Consolas,monospace'],
  round: ['Round', '"Arial Rounded MT Bold","Segoe UI",sans-serif']};
const PC_EFFECTS = {none: 'None', glow: 'Glow', shimmer: 'Shimmer', ring: 'Spinning ring', float: 'Float'};
let PC = {name: BB.name || '', tag: '', course: BB.programme || '', avatar: '', accent: '#d71921', banner: 'aurora', pattern: 'dots',
          shape: 'round', g1: '#7f5af0', g2: '#2cb67d', font: 'dot', effect: 'none', badges: [], avatarImg: '', bannerImg: '', ...(BB.profile || {})};
const inkOn = hex => { const n = parseInt(hex.slice(1), 16);
  return ((n >> 16) * 299 + ((n >> 8) & 255) * 587 + (n & 255) * 114) / 1000 > 150 ? '#0b0b0b' : '#ffffff'; };
function dressAvatar(el, p) {
  el.dataset.shape = p.shape;
  if (p.avatarImg) { el.textContent = ''; el.style.background = `center/cover no-repeat url("${p.avatarImg}")`; el.style.color = ''; return; }
  el.textContent = p.avatar || (p.name || '?')[0];
  el.style.background = p.accent; el.style.color = inkOn(p.accent);
}
const bannerOf = p => p.banner === 'image' && p.bannerImg ? `center/cover no-repeat url("${p.bannerImg}")`
  : p.banner === 'custom' ? `linear-gradient(135deg,${p.g1},${p.g2})` : (PC_BANNERS[p.banner] || PC_BANNERS.aurora);
const PCARD = A.querySelector('.pcard'), PCE = A.querySelector('.pc-edit');
function paintCard(p) {
  PCARD.querySelector('.pc-ban').style.background = [PC_PATTERNS[p.pattern], bannerOf(p)].filter(Boolean).join(',');
  dressAvatar(PCARD.querySelector('.pc-av'), p);
  const nm = PCARD.querySelector('.pc-name'); nm.textContent = p.name || 'Your name';
  nm.style.fontFamily = (PC_FONTS[p.font] || PC_FONTS.dot)[1]; nm.dataset.font = p.font;
  PCARD.querySelector('.pc-tag').textContent = p.tag || '';
  PCARD.querySelector('.pc-meta').textContent = p.course || '';
  PCARD.querySelector('.pc-badges').replaceChildren(...(p.badges || []).map(t => { const s = document.createElement('span'); s.textContent = t; return s; }));
  PCARD.dataset.fx = p.effect || 'none'; PCARD.querySelector('.pc-ring').dataset.shape = p.shape;
  [PCARD, PCE].forEach(el => { el.style.setProperty('--pc', p.accent); el.style.setProperty('--pc-ink', inkOn(p.accent)); });
  PCE.querySelector('.pc-rm-av').hidden = !p.avatarImg; PCE.querySelector('.pc-rm-ban').hidden = !p.bannerImg;
}
function applyProfile(p) {
  document.querySelectorAll('.acct .av, .who .av').forEach(el => dressAvatar(el, p));
  A.querySelector('.a-name').textContent = p.name || 'Student';
}
[['.pc-in-name', 'name'], ['.pc-in-tag', 'tag'], ['.pc-in-course', 'course'], ['.pc-in-av', 'avatar']].forEach(([cls, key]) => {
  const i = PCE.querySelector(cls); i.value = PC[key] || ''; i.oninput = () => { PC[key] = i.value; paintCard(PC); }; });
const BADGES = PCE.querySelector('.pc-in-badges');
BADGES.value = (PC.badges || []).join(', ');
BADGES.oninput = () => { PC.badges = BADGES.value.split(',').map(s => s.trim().slice(0, 18)).filter(Boolean).slice(0, 4); paintCard(PC); };
const chipSets = [];
const pcChips = (cls, list, key, dress) => {
  const box = PCE.querySelector(cls);
  const mark = () => box.querySelectorAll('.pc-chip').forEach(b => b.classList.toggle('on', b.dataset.v === PC[key]));
  const draw = () => {
    box.replaceChildren(...list().map(v => { const b = document.createElement('button'); b.type = 'button'; b.className = 'pc-chip';
      b.dataset.v = v; b.title = v; dress(b, v); b.onclick = () => { PC[key] = v; mark(); paintCard(PC); }; return b; }));
    mark();
  };
  chipSets.push(draw); draw();
};
const redrawChips = () => chipSets.forEach(d => d());
pcChips('.pc-banners', () => [...Object.keys(PC_BANNERS), 'custom', ...(PC.bannerImg ? ['image'] : [])], 'banner', (b, v) => {
  b.classList.add('ban'); b.style.background = bannerOf({...PC, banner: v}); b.title = v === 'custom' ? 'Your own colours' : v === 'image' ? 'Your image' : v; });
pcChips('.pc-patterns', () => Object.keys(PC_PATTERNS), 'pattern', (b, v) => { b.textContent = v[0].toUpperCase() + v.slice(1); });
pcChips('.pc-accents', () => [...new Set([...PC_ACCENTS, PC.accent])], 'accent', (b, v) => { b.classList.add('dot'); b.style.background = v; });
pcChips('.pc-fonts', () => Object.keys(PC_FONTS), 'font', (b, v) => { b.textContent = PC_FONTS[v][0]; b.style.fontFamily = PC_FONTS[v][1]; });
pcChips('.pc-shapes', () => ['round', 'square', 'hex'], 'shape', (b, v) => { b.textContent = {round: 'Round', square: 'Square', hex: 'Hexagon'}[v]; });
pcChips('.pc-effects', () => Object.keys(PC_EFFECTS), 'effect', (b, v) => { b.textContent = PC_EFFECTS[v]; });
// any colour at all for the accent, at the end of the swatches
const accOwn = document.createElement('input'); accOwn.type = 'color'; accOwn.className = 'pc-acc-own'; accOwn.title = 'Any colour'; accOwn.value = PC.accent;
PCE.querySelector('.pc-accents').after(accOwn);
accOwn.oninput = () => { PC.accent = accOwn.value; paintCard(PC); };
accOwn.onchange = redrawChips;
const G1 = PCE.querySelector('.pc-g1'), G2 = PCE.querySelector('.pc-g2'); G1.value = PC.g1; G2.value = PC.g2;
[[G1, 'g1'], [G2, 'g2']].forEach(([i, k]) => { i.oninput = () => { PC[k] = i.value; PC.banner = 'custom'; paintCard(PC); }; i.onchange = redrawChips; });
// pictures are cut to size here, so the card stays small enough to keep and back up
const pickImage = () => new Promise(ok => { const f = PCE.querySelector('.pc-file'); f.value = ''; f.onchange = () => ok(f.files[0] || null); f.click(); });
async function cutImage(file, w, h) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = url; });
    const s = Math.max(w / img.naturalWidth, h / img.naturalHeight), sw = w / s, sh = h / s;
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    c.getContext('2d').drawImage(img, (img.naturalWidth - sw) / 2, (img.naturalHeight - sh) / 2, sw, sh, 0, 0, w, h);
    let out = c.toDataURL('image/webp', .85);
    if (!out.startsWith('data:image/webp')) out = c.toDataURL('image/jpeg', .85);
    return out;
  } finally { URL.revokeObjectURL(url); }
}
const msgPC = t => { A.querySelector('.s-name-msg').textContent = t; };
PCE.querySelector('.pc-up-av').onclick = async () => { const f = await pickImage(); if (!f) return;
  try { PC.avatarImg = await cutImage(f, 256, 256); paintCard(PC); msgPC(''); } catch (_) { msgPC("That picture couldn't be opened"); } };
PCE.querySelector('.pc-rm-av').onclick = () => { PC.avatarImg = ''; paintCard(PC); };
PCE.querySelector('.pc-up-ban').onclick = async () => { const f = await pickImage(); if (!f) return;
  try { PC.bannerImg = await cutImage(f, 840, 240); PC.banner = 'image'; redrawChips(); paintCard(PC); msgPC(''); } catch (_) { msgPC("That image couldn't be opened"); } };
PCE.querySelector('.pc-rm-ban').onclick = () => { PC.bannerImg = ''; if (PC.banner === 'image') PC.banner = 'aurora'; redrawChips(); paintCard(PC); };
const closeEditor = () => { PCE.hidden = true; A.querySelector('.pc-open').textContent = 'Customize'; };
A.querySelector('.pc-open').onclick = () => { if (!PCE.hidden) return closeEditor(); PCE.hidden = false;
  A.querySelector('.pc-open').textContent = 'Close'; PCE.querySelector('.pc-in-name').focus(); };
A.querySelector('.pc-save').onclick = async () => {
  const r = await pywebview.api.profile_save(PC);
  if (!r || !r.ok) { msgPC((r && r.msg) || "Couldn't save"); return; }
  PC = {...PC, ...r.profile}; BB.profile = PC; BB.name = PC.name; paintCard(PC); applyProfile(PC); msgPC('Saved');
  closeEditor(); PCARD.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  try { localStorage.setItem('olName', JSON.stringify(PC.name)); localStorage.setItem('olColor', JSON.stringify(PC.accent)); } catch (_) {}   // the group board uses them too
};
paintCard(PC); if (BB.profile) applyProfile(PC);

// ---- loading a backup: pick one, read the warning, and Yes only wakes up after 5 seconds
async function loadBackup() {
  let list = []; try { list = await pywebview.api.backups_list() || []; } catch (_) {}
  const M = document.createElement('div'); M.className = 'bk-modal';
  const box = document.createElement('div'); box.className = 'bk-box'; M.append(box); document.body.append(M);
  let t = 0;
  const close = () => { clearInterval(t); M.remove(); };
  M.onclick = ev => { if (ev.target === M) close(); };
  M.addEventListener('keydown', ev => { if (ev.key === 'Escape') close(); });
  const when = s => new Date(s).toLocaleString(undefined, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  const pick = () => {
    box.innerHTML = '<h3>Load a backup</h3><p>Pick the copy to go back to. Newest first.</p><div class="bk-list"></div><div class="bk-btns"><button class="bk-no">Cancel</button></div>';
    const L = box.querySelector('.bk-list');
    if (!list.length) L.innerHTML = '<p>No backups yet.</p>';
    list.forEach(f => { const b = document.createElement('button'); b.innerHTML = '<span></span><small></small>';
      b.firstChild.textContent = when(f.when); b.lastChild.textContent = Math.max(1, Math.round(f.size / 1024)) + ' KB';
      b.onclick = () => warn(f); L.append(b); });
    box.querySelector('.bk-no').onclick = close; box.querySelector('.bk-no').focus();
  };
  const warn = f => {
    box.innerHTML = '<h3>Load this backup?</h3><p class="bk-warn"></p><p>Everything as it is right now is backed up first, so you can come back to it from this same list.</p><small class="bk-msg"></small><div class="bk-btns"><button class="bk-back">Back</button><button class="bk-yes" disabled></button></div>';
    box.querySelector('.bk-warn').textContent = 'Warning: your notebook, offline boards, profile and settings will be replaced with the copy from ' + when(f.when) + '. Anything you changed after that will be gone from the app.';
    const yes = box.querySelector('.bk-yes'), back = box.querySelector('.bk-back'), m = box.querySelector('.bk-msg');
    let n = 5;
    const tick = () => { yes.textContent = n > 0 ? 'Yes, load it (' + n + ')' : 'Yes, load it'; yes.disabled = n > 0; if (n-- <= 0) clearInterval(t); };
    clearInterval(t); tick(); t = setInterval(tick, 1000); back.focus();
    back.onclick = () => { clearInterval(t); pick(); };
    yes.onclick = async () => {
      yes.disabled = back.disabled = true; m.textContent = 'Backing up what you have now, then loading…';
      try { await window.bbLeave(); } catch (_) {}
      window.bbFrozen = true;                              // nothing on the page may save over what comes back
      let r; try { r = await pywebview.api.backup_restore(f.file); } catch (e) { r = {msg: String(e)}; }
      if (!r || !r.ok) { window.bbFrozen = false; m.textContent = (r && r.msg) || "Couldn't load it"; yes.disabled = back.disabled = false; return; }
      try { Object.keys(localStorage).filter(k => k.startsWith('olBoardMirror')).forEach(k => localStorage.removeItem(k)); } catch (_) {}
      m.textContent = r.msg + '. Reopening…';
      setTimeout(() => location.reload(), 600);
    };
  };
  pick();
}
A.querySelector('.s-bk-load').onclick = loadBackup;
A.querySelector('.s-bk-open').onclick = () => pywebview.api.backups_open();
A.querySelector('.s-bk-now').onclick = async () => { const m = A.querySelector('.s-bk-msg'); m.textContent = 'Backing up…';
  const r = await pywebview.api.backup_now(); m.textContent = (r && r.msg) || ''; };
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
const UN = {
  app: ['The app itself', 'The program, its files and the dashboard page. Always removed.'],
  settings: ['Settings and Claude keys', 'Your saved Claude accounts and API keys, Blackboard address and app settings.'],
  data: ['Your boards, profile and deadlines', 'Offline boards, your name card, ticked-off work, notes Claude made, what it cost.'],
  notebook: ['Notebook', 'Everything you wrote in the Notebook.'],
  materials: ['Downloaded Blackboard files', 'Lecture slides and files fetched from Blackboard. They can be downloaded again.'],
  backups: ['Backups', 'The automatic copies made every 15 minutes.'],
  browser: ['Sign-ins and browser data', 'The Blackboard sign-in and the app’s saved page data on this computer.'],
  projects: ['Code projects', 'Folders saved to Documents\\Blackboard Buddy projects from board code blocks.'],
};
const unSize = n => n < 1024 ? (n ? n + ' B' : '—') : n < 1048576 ? Math.round(n / 1024) + ' KB' : n < 1073741824 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1073741824).toFixed(2) + ' GB';
async function uninstallFlow() {
  let info; try { info = await pywebview.api.uninstall_info(); } catch (e) { info = {parts: [], blocked: String(e)}; }
  const M = document.createElement('div'); M.className = 'bk-modal';
  const box = document.createElement('div'); box.className = 'bk-box un-box'; M.append(box); document.body.append(M);
  const pick = new Set(['app']); let backup = true, t = 0, busy = false;
  const close = () => { if (busy) return; clearInterval(t); M.remove(); };
  M.onclick = ev => { if (ev.target === M) close(); };
  M.addEventListener('keydown', ev => { if (ev.key === 'Escape') close(); });
  const steps = n => '<div class="un-steps">' + [1, 2, 3].map(i => '<i class="' + (i <= n ? 'on' : '') + '"></i>').join('') + '</div>';
  const parts = (info.parts || []).filter(x => x.key === 'app' || x.found);
  const keepsData = () => ['data', 'notebook', 'settings'].some(k => !pick.has(k));
  // 1: what goes
  const choose = () => {
    box.innerHTML = steps(1) + '<h3>Uninstall Blackboard Buddy</h3><p>What should be removed along with the app? Anything you leave unticked stays on this computer, so a fresh install picks it straight back up.</p>' +
      '<div class="un-all"><span class="un-total"></span><span><button class="un-every">Tick everything</button> · <button class="un-none">Only the app</button></span></div><div class="un-list"></div>' +
      '<p class="bk-warn un-blocked" hidden></p><div class="bk-btns"><button class="bk-no">Cancel</button><button class="bk-yes un-next">Next</button></div>';
    const L = box.querySelector('.un-list');
    const total = () => { box.querySelector('.un-total').textContent = 'Frees about ' + unSize(parts.filter(x => pick.has(x.key)).reduce((a, x) => a + x.size, 0)); };
    parts.forEach(x => {
      const [name, what] = UN[x.key] || [x.key, ''];
      const row = document.createElement('label'); row.className = 'un-item' + (x.key === 'app' ? ' lock' : '');
      row.innerHTML = '<input type="checkbox"><b></b><em></em><small></small>';
      const cb = row.querySelector('input'); cb.checked = pick.has(x.key); cb.disabled = x.key === 'app'; row.classList.toggle('on', cb.checked);
      row.querySelector('b').textContent = name; row.querySelector('em').textContent = unSize(x.size);
      row.querySelector('small').textContent = what + (x.key === 'materials' && info.custom_materials ? ' In your own folder: ' + x.where : '');
      cb.onchange = () => { cb.checked ? pick.add(x.key) : pick.delete(x.key); row.classList.toggle('on', cb.checked); total(); };
      L.append(row);
    });
    const all = on => { L.querySelectorAll('input:not(:disabled)').forEach((cb, i) => { cb.checked = on; cb.onchange(); }); };
    box.querySelector('.un-every').onclick = () => all(true);
    box.querySelector('.un-none').onclick = () => all(false);
    total();
    box.querySelector('.bk-no').onclick = close;
    const next = box.querySelector('.un-next');
    if (info.blocked) { const w = box.querySelector('.un-blocked'); w.hidden = false; w.textContent = info.blocked; next.disabled = true; }
    next.onclick = keep; box.querySelector('.bk-no').focus();
  };
  // 2: a last backup?
  const keep = () => {
    box.innerHTML = steps(2) + '<h3>Keep a copy?</h3><p>A final backup holds your notebook, boards, profile and settings in one zip, so you can load it into Blackboard Buddy later with Settings › Load a backup.</p>' +
      '<div class="un-list"><label class="un-item"><input type="checkbox" class="un-bk"><b>Save a final backup to my Desktop</b><em></em><small></small></label></div>' +
      '<div class="bk-btns"><button class="bk-back">Back</button><button class="bk-yes un-next">Next</button></div>';
    const cb = box.querySelector('.un-bk'), row = cb.closest('.un-item');
    cb.checked = backup; row.classList.toggle('on', backup);
    row.querySelector('small').textContent = keepsData() ? 'Optional. Some of your things stay on this computer anyway.' : 'Recommended: everything of yours is about to be removed.';
    cb.onchange = () => { backup = cb.checked; row.classList.toggle('on', backup); };
    box.querySelector('.bk-back').onclick = choose; box.querySelector('.un-next').onclick = confirmIt; box.querySelector('.un-next').focus();
  };
  // 3: say it twice
  const confirmIt = () => {
    const gone = parts.filter(x => pick.has(x.key)), stay = parts.filter(x => !pick.has(x.key));
    box.innerHTML = steps(3) + '<h3>Are you sure?</h3><p>The app closes, then these go to the Recycle Bin:</p><ul class="un-sum"></ul>' +
      '<p class="un-stay-h">These stay:</p><ul class="un-sum keep"></ul><p class="un-note"></p>' +
      '<p>Type <b>uninstall</b> to confirm.</p><input class="un-type" autocomplete="off" spellcheck="false" aria-label="Type uninstall to confirm">' +
      '<small class="bk-msg"></small><div class="bk-btns"><button class="bk-back">Back</button><button class="bk-yes" disabled></button></div>';
    const [G, K] = box.querySelectorAll('.un-sum');
    gone.forEach(x => { const li = document.createElement('li'); li.textContent = UN[x.key][0] + ' · ' + unSize(x.size); G.append(li); });
    stay.forEach(x => { const li = document.createElement('li'); li.textContent = UN[x.key][0]; K.append(li); });
    if (!stay.length) { box.querySelector('.un-stay-h').remove(); K.remove(); }
    box.querySelector('.un-note').textContent = (backup ? 'A final backup goes to your Desktop first. ' : 'No final backup will be made. ') +
      'Groups you are in online stay on the server; leave them first if you want to.';
    const inp = box.querySelector('.un-type'), yes = box.querySelector('.bk-yes'), back = box.querySelector('.bk-back'), m = box.querySelector('.bk-msg');
    let n = 5;
    const ready = () => { const ok = inp.value.trim().toLowerCase() === 'uninstall'; inp.classList.toggle('ok', ok);
      yes.textContent = n > 0 ? 'Uninstall (' + n + ')' : 'Uninstall'; yes.disabled = n > 0 || !ok; };
    clearInterval(t); ready(); t = setInterval(() => { n--; ready(); if (n <= 0) clearInterval(t); }, 1000);
    inp.oninput = ready; inp.onkeydown = ev => { if (ev.key === 'Enter' && !yes.disabled) yes.click(); }; inp.focus();
    back.onclick = () => { clearInterval(t); keep(); };
    yes.onclick = async () => {
      busy = true; yes.disabled = back.disabled = inp.disabled = true;
      m.textContent = backup ? 'Saving your final backup…' : 'Closing…';
      try { await window.bbLeave(); } catch (_) {}
      let r; try { r = await pywebview.api.uninstall([...pick], backup); } catch (e) { r = {error: String(e)}; }
      if (!r || !r.ok) { busy = false; m.textContent = (r && r.error) || "Couldn't uninstall."; back.disabled = inp.disabled = false; ready(); return; }
      window.bbFrozen = true;
      box.innerHTML = '<h3>Goodbye 👋</h3><p>Blackboard Buddy is closing and removing itself. A message will pop up when it is done.</p>' +
        (r.saved ? '<p class="un-saved"></p>' : '');
      if (r.saved) box.querySelector('.un-saved').textContent = 'Your final backup: ' + r.saved;
    };
  };
  choose();
}
A.querySelector('.s-uninstall').onclick = () => { closeSheets(); uninstallFlow(); };

// ---- Settings > Share: give a code out, or follow someone else's
const SH = A.querySelector('[data-s=share].set-pane');
const shWhen = t => t ? new Date(t).toLocaleString(undefined, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}) : 'not yet';
const shCode = c => c ? c.slice(0, 4) + '-' + c.slice(4) : '';
async function drawShare(msg) {
  const M = SH.querySelector('.sh-msg'), own = SH.querySelector('.sh-own'), fol = SH.querySelector('.sh-follow');
  let st; try { st = await pywebview.api.share_status(); } catch (e) { M.textContent = 'Sharing works in the app.'; return; }
  M.textContent = msg || '';
  const act = async (b, fn, ...a) => { b.disabled = true; M.textContent = '';
    let r; try { r = await pywebview.api[fn](...a); } catch (e) { r = {error: String(e)}; }
    b.disabled = false; if (r && r.error) { M.textContent = r.error; return null; } return r; };
  // yours
  if (st.share) {
    own.innerHTML = '<div class="sh-card"><div class="sh-code"><b></b><button class="ghost sh-copy">Copy</button></div>'
      + '<small class="sh-when"></small><div class="sh-row"><button class="ghost sh-now">Send my latest now</button><button class="ghost danger sh-stop">Stop sharing</button></div></div>';
    own.querySelector('b').textContent = shCode(st.share.code);
    own.querySelector('.sh-when').textContent = 'Last sent ' + shWhen(st.share.updated) + (st.share.size ? ' · ' + Math.max(1, Math.round(st.share.size / 1024)) + ' KB' : '')
      + '. It goes out again by itself after every sync. Anyone with the code can see it, so only give it to people you trust.';
    own.querySelector('.sh-copy').onclick = async ev => { try { await navigator.clipboard.writeText(shCode(st.share.code)); ev.target.textContent = 'Copied'; setTimeout(() => ev.target.textContent = 'Copy', 1400); } catch (_) {} };
    own.querySelector('.sh-now').onclick = async ev => { ev.target.textContent = 'Sending…'; if (await act(ev.target, 'share_now')) drawShare('Sent.'); else ev.target.textContent = 'Send my latest now'; };
    own.querySelector('.sh-stop').onclick = async ev => {
      if (!confirm('Stop sharing? Everyone following your code loses your updates, and the code stops working.')) return;
      if (await act(ev.target, 'share_stop')) drawShare('Stopped. The code no longer works.'); };
  } else {
    own.innerHTML = '<div class="sh-card"><div class="sh-row"><button class="ghost sh-start">Make a share code</button></div><small></small></div>';
    own.querySelector('small').textContent = st.has_sync ? 'You get a code like ABCD-2345 to give to classmates.' : 'Sync with Blackboard once first, so there is something to share.';
    own.querySelector('.sh-start').disabled = !st.has_sync;
    own.querySelector('.sh-start').onclick = async ev => { ev.target.textContent = 'Making your code…'; if (await act(ev.target, 'share_start')) drawShare(); else ev.target.textContent = 'Make a share code'; };
  }
  // theirs
  if (st.follow) {
    fol.innerHTML = '<div class="sh-card"><span class="sh-who"></span><small class="sh-fwhen"></small>'
      + '<div class="sh-row"><button class="ghost sh-check">Check for new notes</button><button class="ghost danger sh-unf">Stop following</button></div></div>';
    fol.querySelector('.sh-who').textContent = 'Following ' + (st.follow.name || 'a classmate') + ' · ' + shCode(st.follow.code);
    fol.querySelector('.sh-fwhen').textContent = 'Their notes from ' + shWhen(st.follow.synced || st.follow.updated) + ' · new ones arrive by themselves every 15 minutes while the app is open.'
      + (st.has_key ? ' Stop following to go back to your own Blackboard sync.' : ' To sync your own Blackboard instead, add a Claude key in the Claude tab and stop following.');
    fol.querySelector('.sh-check').onclick = async ev => { await act(ev.target, 'follow_now'); M.textContent = 'Checking… the page updates if there is anything new.'; };
    fol.querySelector('.sh-unf').onclick = async ev => {
      if (!confirm('Stop following ' + (st.follow.name || 'them') + '? Your app goes back to your own sync' + (st.has_key ? '.' : ' (none yet: add a Claude key to start one).'))) return;
      if (await act(ev.target, 'unfollow')) { M.textContent = 'Stopped. Reloading…'; setTimeout(() => location.reload(), 500); } };
  } else {
    fol.innerHTML = '<div class="sh-card"><div class="sh-row"><input class="sh-in" maxlength="9" placeholder="ABCD-2345" aria-label="Share code" spellcheck="false" autocomplete="off"><button class="ghost sh-go">Follow</button></div>'
      + '<small>Enter a classmate\'s code to see their notes, deadlines and plans here, kept up to date. Your Notebook and boards stay yours.</small></div>';
    const inp = fol.querySelector('.sh-in'), go = fol.querySelector('.sh-go');
    inp.oninput = () => { const v = inp.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8); inp.value = v.length > 4 ? v.slice(0, 4) + '-' + v.slice(4) : v; };
    inp.onkeydown = ev => { if (ev.key === 'Enter') go.click(); };
    go.onclick = async () => { go.textContent = 'Getting their notes…';
      const r = await act(go, 'follow', inp.value); go.textContent = 'Follow';
      if (r) { M.textContent = 'Now following ' + (r.follow && r.follow.name || 'them') + '. Reloading…'; setTimeout(() => location.reload(), 600); } };
  }
}
SH && A.querySelector('.set-tab[data-s=share]').addEventListener('click', () => drawShare());
// whose notes these are, at the top of This week
if (BB.follow) {
  const n = document.createElement('div'); n.className = 'follow-note';
  n.innerHTML = '<i></i><span></span><button>Settings</button>';
  n.querySelector('span').textContent = 'Shared by ' + (BB.follow.name || 'a classmate') + ' · their notes from ' + shWhen(BB.follow.synced || BB.follow.updated);
  n.querySelector('button').onclick = () => { document.querySelector('.acct')?.click(); setTimeout(() => A.querySelector('.set-tab[data-s=share]').click(), 50); };
  document.getElementById('week')?.prepend(n);
}
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
  const b = ev.target.closest('.mk[data-item]'); if (!b || !window.pywebview) return;   // only the Notes tab's own buttons
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
  // when each note is from: its date, and the teaching week (week 1 starts on DATA.term)
  const DAY = 864e5, WK = 7 * DAY, T0 = DATA.term || 0, NEWDAYS = 7;
  const weekOf = t => t && T0 ? Math.max(0, Math.floor(Math.round((new Date(t).setHours(0, 0, 0, 0) - T0) / DAY) / 7) + 1) : 0;
  const wcOf = w => { const d = new Date(T0); d.setDate(d.getDate() + (w - 1) * 7); return d.toLocaleDateString(undefined, {day: 'numeric', month: 'short'}); };
  const dateOf = t => new Date(t).toLocaleDateString(undefined, {weekday: 'short', day: 'numeric', month: 'short'});
  const ago = t => { const d = Math.floor((Date.now() - t) / DAY); return d <= 0 ? 'today' : d === 1 ? 'yesterday' : d < 14 ? d + ' days ago' : Math.round(d / 7) + ' weeks ago'; };
  const weekName = w => w ? 'Week ' + w + ' · w/c ' + wcOf(w) : 'Before term';
  let mode = 'age', onlyWeek = null, tMin = 0, tMax = 0;
  try { mode = localStorage.mapColour || 'age'; } catch (_) {}
  // colours: a ramp from old (cool) to new (bright) for age, distinct hues for weeks and modules
  const RAMP = {dark: ['#5560d8', '#1aa3c4', '#46d68c', '#d6f046'], light: ['#3a3fb0', '#0b7f99', '#23905a', '#7a9a00']};
  const CAT = {dark: ['#4fc3f7', '#ffb74d', '#81c784', '#ba68c8', '#fff176', '#f48fb1', '#4db6ac', '#a1887f', '#9fa8da', '#e6ee9c'],
               light: ['#0277bd', '#e65100', '#2e7d32', '#7b1fa2', '#a07800', '#c2185b', '#00796b', '#6d4c41', '#3949ab', '#827717']};
  const hex = c => [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16));
  const ramp = (f, th) => { const S = RAMP[th], x = Math.max(0, Math.min(1, f)) * (S.length - 1), i = Math.min(S.length - 2, Math.floor(x)), u = x - i;
    const a = hex(S[i]), b = hex(S[i + 1]); return 'rgb(' + a.map((v, k) => Math.round(v + (b[k] - v) * u)).join(',') + ')'; };
  const cat = (i, th) => CAT[th][((i % CAT[th].length) + CAT[th].length) % CAT[th].length];
  const modIdx = k => Math.max(0, DATA.modules.findIndex(m => m.key === k));
  const isNew = n => n.t && Date.now() - n.t < NEWDAYS * DAY;
  function theme() { const c = document.createElement('canvas').getContext('2d');
    c.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#000';
    const [r, g, b] = hex(c.fillStyle.length === 7 ? c.fillStyle : '#000000'); return (r * 299 + g * 587 + b * 114) / 1000 < 128 ? 'dark' : 'light'; }
  function paint(n, fg, th) {
    if (n.kind === 'mod') return mode === 'module' ? cat(modIdx(n.key), th) : fg;
    if (mode === 'age') return n.t ? ramp(tMax > tMin ? (n.t - tMin) / (tMax - tMin) : 1, th) : fg;
    if (mode === 'week') return n.t || n.wk ? cat(n.wk, th) : fg;
    if (mode === 'module') return n.key ? cat(modIdx(n.key), th) : fg;
    return fg;
  }
  const shown = n => onlyWeek === null || n.kind === 'mod' || n.wk === onlyWeek;

  function build() {
    const old = new Map(nodes.map(n => [n.id, n]));
    const map = new Map(), E = new Map();
    const add = (id, o) => { if (!map.has(id)) { const p = old.get(id); map.set(id, {id, deg: 0, vx: 0, vy: 0,
      x: p ? p.x : (Math.random() - .5) * 260, y: p ? p.y : (Math.random() - .5) * 160, ...o}); } return map.get(id); };
    const join = (a, b, kind, info) => { if (!a || !b || a === b) return; const k = a.id < b.id ? a.id + '|' + b.id : b.id + '|' + a.id;
      if (!E.has(k) || kind !== 'sim') E.set(k, [a, b, kind, info]); };
    const hubs = DATA.modules.map(m => add('m:' + m.key, {kind: 'mod', label: m.name, key: m.key}));
    const hubFor = s => { s = (s || '').toLowerCase(); return hubs.find(h => s.includes(h.label.toLowerCase())); };
    const docs = [];
    DATA.study.forEach((n, i) => { const d = add('s:' + i, {kind: 'study', label: n.topic || n.title, key: n.key, title: n.title,
      path: n.path, t: n.t || 0, wk: n.week || weekOf(n.t)});
      join(d, map.get('m:' + n.key), 'mod'); docs.push([d, n.text]); });
    nbNotes.filter(n => !/^(Start here|Linking notes)$/.test(n.name)).forEach(n => {
      const d = add('n:' + n.name.toLowerCase(), {kind: 'nb', label: n.name.split('/').pop(), name: n.name, t: n.mtime || 0, wk: weekOf(n.mtime)});
      Object.assign(d, {t: n.mtime || 0, wk: weekOf(n.mtime)});
      const hub = hubFor(n.name.split('/')[0]) || hubFor(n.text.slice(0, 600)); if (hub) d.key = hub.key;
      join(d, hub, 'mod'); docs.push([d, n.text]);
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
    const shared = (a, b) => [...a].filter(([w]) => b.has(w)).map(([w, x]) => [w, x * b.get(w)])
      .sort((p, q) => q[1] - p[1]).slice(0, 5).map(p => p[0]);
    const cos = (a, b) => { let s = 0; const [p, q] = a.size < b.size ? [a, b] : [b, a]; p.forEach((x, w) => { const y = q.get(w); if (y) s += x * y; }); return s; };
    vecs.forEach((v, i) => vecs.map((u, j) => [j, i === j ? 0 : cos(v, u)]).sort((a, b) => b[1] - a[1]).slice(0, 2)
      .forEach(([j, s]) => { if (s >= .12) join(docs[i][0], docs[j][0], 'sim', {s, words: shared(v, vecs[j])}); }));
    nodes = [...map.values()]; edges = [...E.values()];
    edges.forEach(([a, b]) => { a.deg++; b.deg++; });
    const nS = docs.filter(([d]) => d.kind === 'study').length, nN = docs.length - nS, nSim = edges.filter(e => e[2] !== 'mod').length;
    const dated = nodes.filter(n => n.kind !== 'mod' && n.t);
    tMin = Math.min(...dated.map(n => n.t)); tMax = Math.max(...dated.map(n => n.t));
    const per = new Map(); dated.forEach(n => per.set(n.wk, (per.get(n.wk) || 0) + 1));
    const busy = [...per].sort((a, b) => b[1] - a[1])[0], fresh = dated.filter(isNew).length;
    META.textContent = `${nS} study note${nS === 1 ? '' : 's'} · ${nN} notebook note${nN === 1 ? '' : 's'} · ${nSim} connection${nSim === 1 ? '' : 's'} found`
      + (dated.length ? ` · ${fresh} new this week · newest ${ago(tMax)} · oldest ${dateOf(tMin)}` : '')
      + (busy && busy[1] > 1 ? ` · busiest: ${busy[0] ? 'week ' + busy[0] : 'before term'} (${busy[1]})` : '')
      + (docs.length ? '' : ' · make notes in the Notes or Notebook tab and they show up here');
    if (onlyWeek !== null && !per.has(onlyWeek)) onlyWeek = null;
    drawWeeks(per); drawLegend();
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
    V.edgeAt = ev => { const p = V.toMap(ev); let best = null, bd = 7 / V.k;
      edges.forEach(e => { const [a, b] = e; if (!shown(a) || !shown(b)) return;
        const dx = b.x - a.x, dy = b.y - a.y, L = dx * dx + dy * dy || 1, t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L));
        const d = Math.hypot(a.x + t * dx - p.x, a.y + t * dy - p.y); if (d < bd) { bd = d; best = e; } });
      return best; };
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
      const th = theme();
      const me = nodes.find(n => n.id === here) || null, focus = V.hover || (V.pop ? me : null);
      const near = new Set(); if (focus) edges.forEach(([a, b]) => { if (a === focus) near.add(b); if (b === focus) near.add(a); });
      edges.forEach(e => { const [a, b, k, info] = e;
        const on = (focus && (a === focus || b === focus)) || e === V.edge;
        const st = Math.min(1, ((info && info.s) || .12) / .45), w = k === 'sim' ? .7 + st * 2 : k === 'link' ? 1.8 : 1;
        g.strokeStyle = on ? red : k === 'link' ? fg : mut;
        g.globalAlpha = ((focus || V.edge) && !on) || !shown(a) || !shown(b) ? .08 : k === 'sim' ? .25 + st * .45 : k === 'link' ? .75 : .45;
        g.lineWidth = on ? w + .8 : w; g.setLineDash(k === 'sim' ? [2, 3 + (1 - st) * 3] : []);
        const [x1, y1] = P(a), [x2, y2] = P(b); g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
      });
      g.setLineDash([]);
      const labels = [];
      nodes.forEach(n => {
        const [x, y] = P(n), s = rad(n), lit = n === V.hover || n === me;
        g.globalAlpha = (focus && !lit && n !== focus && !near.has(n)) || !shown(n) ? (shown(n) ? .22 : .1) : 1;
        g.fillStyle = g.strokeStyle = lit ? red : paint(n, fg, th);
        g.beginPath();
        if (n.kind === 'nb') { g.moveTo(x, y - s - 1); g.lineTo(x + s + 1, y); g.lineTo(x, y + s + 1); g.lineTo(x - s - 1, y); g.closePath(); g.fill(); }
        else if (n.kind === 'mod') { g.lineWidth = 1.6; g.arc(x, y, s, 0, 7); g.stroke(); g.beginPath(); g.arc(x, y, 2.2, 0, 7); g.fill(); }
        else { g.arc(x, y, s, 0, 7); g.fill(); }
        if (isNew(n) && n.kind !== 'mod' && mode !== 'plain') { g.lineWidth = 1.2; g.beginPath(); g.arc(x, y, s + 3, 0, 7); g.stroke(); }
        if (n === me) {   // "you are here": a ring that breathes
          const t = (performance.now() % 1600) / 1600;
          g.globalAlpha = 1 - t; g.lineWidth = 1.5; g.strokeStyle = red; g.beginPath(); g.arc(x, y, s + 3 + t * 10, 0, 7); g.stroke(); g.globalAlpha = 1;
        }
        if (n.kind === 'mod' || lit || (focus && near.has(n) && !(V.pop && !V.hover)))   // pop-up: neighbours named on hover only
          labels.push({n, x, y, s, lit, a: shown(n) ? g.globalAlpha : .25, prio: lit ? 0 : focus && near.has(n) ? 1 : 2});
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
  const WRAP = CV.closest('.map-wrap'), WEEKS = WRAP.querySelector('.map-weeks'), LEG = WRAP.querySelector('.map-legend'),
    TIP = WRAP.querySelector('.map-tip'), FULL = WRAP.querySelector('.map-full');
  const esc = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
  // the legend draws the same shapes the map does
  // a line key is two small dots joined the way the map joins them
  const join = (c, line) => `<circle cx="3.5" cy="9" r="2.3" fill="${c}"/><circle cx="24.5" cy="9" r="2.3" fill="${c}"/>${line}`;
  const icon = (k, c) => `<svg class="ml-ico${/^(line|link|dash)$/.test(k) ? ' wide' : ''}" viewBox="0 0 ${/^(line|link|dash)$/.test(k) ? 28 : 18} 18" width="${/^(line|link|dash)$/.test(k) ? 28 : 18}" height="18" aria-hidden="true">` + ({
    study: `<circle cx="9" cy="9" r="4.2" fill="${c}"/>`,
    nb: `<path d="M9 3.6L14.4 9L9 14.4L3.6 9Z" fill="${c}"/>`,
    mod: `<circle cx="9" cy="9" r="6.4" fill="none" stroke="${c}" stroke-width="1.7"/><circle cx="9" cy="9" r="2.2" fill="${c}"/>`,
    new: `<circle cx="9" cy="9" r="4" fill="${c}"/><circle cx="9" cy="9" r="7" fill="none" stroke="${c}" stroke-width="1.3"/>`,
    line: join(c, `<path d="M6 9H22" stroke="${c}" stroke-width="1.2" opacity=".75"/>`),
    link: join(c, `<path d="M6 9H22" stroke="${c}" stroke-width="2.4" stroke-linecap="round"/>`),
    dash: join(c, `<path d="M6.5 9H22" stroke="${c}" stroke-width="1.6" stroke-dasharray="2.5 2.5"/>`),
  })[k] + '</svg>';
  function drawLegend() {
    const th = theme(); let h = '';
    if (mode === 'age' && tMax) h = `<span>oldest · ${esc(dateOf(tMin))}<i class="ml-ramp" style="background:linear-gradient(90deg,${RAMP[th].join(',')})"></i>newest · ${esc(dateOf(tMax))}</span>`;
    if (mode === 'week') h = '<span>Each colour is a teaching week · pick a week below to show just that one</span>';
    if (mode === 'module') h = DATA.modules.map((m, i) => `<span class="ml-sw" style="--c:${cat(i, th)}"><i></i>${esc(m.name)}</span>`).join('');
    if (mode === 'plain') h = '<span>Plain dots</span>';
    const fg = getComputedStyle(document.documentElement).getPropertyValue('--text').trim() || '#fff',
      mut = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#888';
    const nc = mode === 'age' ? ramp(1, th) : mode === 'week' ? cat(weekOf(Date.now()), th) : fg;
    if (mode !== 'plain' && tMax) h += `<span class="ml-key">${icon('new', nc)}New in the last ${NEWDAYS} days</span>`;
    LEG.innerHTML = h + `<span class="ml-shape"><span class="ml-key">${icon('study', fg)}Study note</span><span class="ml-key">${icon('nb', fg)}Notebook note</span>`
      + `<span class="ml-key">${icon('mod', fg)}Module</span><span class="ml-key">${icon('line', mut)}Same module</span>`
      + `<span class="ml-key">${icon('link', fg)}You linked them</span><span class="ml-key">${icon('dash', mut)}Similar topic</span><span class="ml-key ml-hint">thicker line = closer match</span></span>`;
    WRAP.querySelectorAll('.map-modes button').forEach(b => { b.classList.toggle('on', b.dataset.m === mode); b.setAttribute('aria-pressed', b.dataset.m === mode); });
  }
  function drawWeeks(per) {
    per = per || new Map(nodes.filter(n => n.kind !== 'mod' && n.t).reduce((m, n) => m.set(n.wk, (m.get(n.wk) || 0) + 1), new Map()));
    const ws = [...per.keys()], now = weekOf(Date.now());
    if (!ws.length) { WEEKS.innerHTML = ''; return; }
    const lo = Math.min(...ws), hi = Math.max(...ws, Math.min(now, Math.max(...ws) + 3)), top = Math.max(...per.values()), th = theme();
    WEEKS.innerHTML = '';
    for (let w = lo; w <= hi; w++) {
      const c = per.get(w) || 0, b = document.createElement('button');
      const ts = nodes.filter(n => n.kind !== 'mod' && n.t && n.wk === w).map(n => n.t), mid = ts.length ? ts.reduce((a, b) => a + b) / ts.length : T0 + (w - .5) * WK;
      const col = mode === 'week' ? cat(w, th) : mode === 'age' && T0 ? ramp(tMax > tMin ? (mid - tMin) / (tMax - tMin) : 1, th) : 'var(--muted)';
      b.style.setProperty('--h', Math.round(4 + 26 * c / top) + 'px'); b.style.setProperty('--c', col);
      b.className = (c ? '' : 'empty') + (w === now ? ' now' : '') + (onlyWeek === w ? ' on' : '');
      b.innerHTML = `<i></i><b>${w ? 'W' + w : 'Pre'}</b>`;
      b.title = `${weekName(w)} · ${c} note${c === 1 ? '' : 's'}${w === now ? ' · this week' : ''}`;
      b.setAttribute('aria-label', b.title); b.setAttribute('aria-pressed', onlyWeek === w);
      b.onclick = () => { onlyWeek = onlyWeek === w ? null : w; drawWeeks(); big.draw(); };
      WEEKS.append(b);
    }
  }
  WRAP.querySelectorAll('.map-modes button').forEach(b => b.onclick = () => { mode = b.dataset.m;
    try { localStorage.mapColour = mode; } catch (_) {} drawLegend(); drawWeeks(); big.draw(); if (!POP.hidden) mini.draw(); });
  function why(e, from) {
    const [a, b, k, info] = e, other = from ? (a === from ? b : a) : null;
    if (k === 'mod') return {kind: 'Same module', more: (other || b).kind === 'mod' ? 'Filed under this module' : 'Filed under this module'};
    if (k === 'link') return {kind: 'You linked them', more: 'A [[link]] written in your Notebook'};
    const pc = Math.round(info.s * 100);
    return {kind: `Similar topic · ${pc}% match`, more: info.words.length ? 'Both talk about: ' + info.words.join(', ') : 'Similar wording'};
  }
  function lineTip(e, ev) {
    if (!e) { TIP.hidden = true; return; }
    const [a, b, k] = e, w = why(e), fg = getComputedStyle(document.documentElement).getPropertyValue('--text').trim();
    TIP.innerHTML = `<div class="mt-row"><b>${esc(w.kind)}</b></div><small>${esc(a.label)}</small><small>↔ ${esc(b.label)}</small>`
      + `<small>${esc(w.more)}</small>` + (k === 'sim' ? `<div class="mt-bar"><i style="width:${Math.round(Math.min(1, e[3].s / .6) * 100)}%"></i></div>` : '')
      + '<em>Click a dot to open it</em>';
    TIP.hidden = false;
    const r = CV.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top, tw = TIP.offsetWidth, th = TIP.offsetHeight;
    TIP.style.left = Math.max(4, Math.min(r.width - tw - 4, x + 14)) + 'px';
    TIP.style.top = Math.max(4, Math.min(r.height - th - 4, y + 14)) + 'px';
  }
  function tip(n) {
    if (!n || drag) { TIP.hidden = true; return; }
    const th = theme(), fg = getComputedStyle(document.documentElement).getPropertyValue('--text').trim(), links = edges.filter(([a, b]) => a === n || b === n);
    const mod = DATA.modules.find(m => m.key === n.key);
    let h = `<div class="mt-row"><i style="--c:${paint(n, fg, th)}"></i><b>${esc(n.label)}${isNew(n) ? '<span class="mt-new">NEW</span>' : ''}</b></div>`;
    if (n.kind === 'mod') {
      const kids = nodes.filter(x => x.kind !== 'mod' && x.key === n.key && x.t), last = Math.max(0, ...kids.map(x => x.t));
      const wks = [...new Set(kids.map(x => x.wk))].sort((a, b) => a - b);
      h += `<small>Module · ${kids.length} note${kids.length === 1 ? '' : 's'}</small>`
        + (last ? `<small>Newest: ${esc(dateOf(last))} (${ago(last)})</small>` : '')
        + (wks.length ? `<small>Weeks: ${wks.map(w => w || 'pre').join(', ')}</small>` : '');
    } else {
      if (n.kind === 'study' && n.title && n.title !== n.label) h += `<small>${esc(n.title)}</small>`;
      h += `<small>${n.kind === 'nb' ? 'Notebook note' : 'Study note'}${mod ? ' · ' + esc(mod.name) : ''}</small>`;
      const pth = n.path || '', where = pth.endsWith(n.title || '') ? pth.slice(0, pth.length - (n.title || '').length).replace(/[ /]+$/, '') : pth;
      if (where) h += `<small>In: ${esc(where)}</small>`;
      if (n.t) h += `<small>${esc(weekName(n.wk))}</small><small>${n.kind === 'nb' ? 'Edited' : 'Posted or changed'} ${esc(dateOf(n.t))} · ${ago(n.t)}</small>`;
      h += `<small class="mt-h">${links.length} connection${links.length === 1 ? '' : 's'}</small>`;
      const order = {link: 0, sim: 1, mod: 2};
      links.slice().sort((p, q) => order[p[2]] - order[q[2]] || ((q[3] && q[3].s) || 0) - ((p[3] && p[3].s) || 0)).slice(0, 6).forEach(e => {
        const o = e[0] === n ? e[1] : e[0], w = why(e, n);
        h += `<div class="mt-link mt-${e[2]}"><span>${esc(o.label)}</span><small>${esc(w.kind)}${e[2] === 'sim' && e[3].words.length ? ' · ' + esc(e[3].words.slice(0, 3).join(', ')) : ''}</small></div>`;
      });
      if (links.length > 6) h += `<small>and ${links.length - 6} more</small>`;
    }
    TIP.innerHTML = h + '<em>Click to open · drag to move</em>'; TIP.hidden = false;
    const r = CV.getBoundingClientRect(), st = window.bbStick || {x: r.left, y: r.top};
    const x = st.x - r.left, y = st.y - r.top, w = TIP.offsetWidth, ht = TIP.offsetHeight;
    TIP.style.left = Math.max(4, Math.min(r.width - w - 4, x + 16 + w > r.width ? x - w - 16 : x + 16)) + 'px';
    TIP.style.top = Math.max(4, Math.min(r.height - ht - 4, y - ht / 2)) + 'px';
  }
  function setFull(on) {
    WRAP.classList.toggle('full', on); FULL.textContent = on ? '✕' : '⤢';
    FULL.setAttribute('aria-label', on ? 'Leave full screen' : 'Make the note map full screen'); FULL.title = on ? 'Leave full screen (Esc)' : 'Full screen';
    alpha = Math.max(alpha, .3); requestAnimationFrame(() => { big.draw(true); kick(); });
  }
  FULL.onclick = () => setFull(!WRAP.classList.contains('full'));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && WRAP.classList.contains('full')) setFull(false); });

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
    if (!drag) { const n = big.hit(e); if (n !== big.hover) { big.hover = n; CV.classList.toggle('on-dot', !!n); big.edge = null; big.draw(); tip(n); }
      if (!n) { const ed = big.edgeAt(e); if (ed !== big.edge) { big.edge = ed; big.draw(); } if (ed) lineTip(ed, e); else TIP.hidden = true; }
      return; }
    TIP.hidden = true;
    if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 3) drag.moved = true;
    const p = big.toMap(e); drag.node.x = p.x; drag.node.y = p.y; drag.node.vx = drag.node.vy = 0;
  };
  CV.onpointerup = () => { const d = drag; drag = null; if (d && !d.moved) openNode(d.node); kick(); };
  CV.onpointerleave = () => { TIP.hidden = true; if (big.edge) { big.edge = null; big.draw(); } if (big.hover && !drag) { big.hover = null; CV.classList.remove('on-dot'); big.draw(); } };
  new ResizeObserver(() => { big.draw(); kick(); }).observe(CV);
  const redraw = () => { drawLegend(); drawWeeks(); big.draw(); if (!POP.hidden) mini.draw(); };
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

# ---------------------------------------------------------------- Study: flashcards made from your notes
# Made without Claude (free): each "### idea" becomes an Explain card, "A **term** is ..." a Define card,
# two-column tables become cards row by row, and a sentence with one bold term becomes a Fill-the-gap card.
_SKIP_HEAD = re.compile(r"before you start|deadline|question|check with|summary|key concepts|revision|source|"
                        r"what (this|the) (file|material)|links?$|further reading|next steps?|overview|catch.?up|order of work|"
                        r"preparing for|stretch task|contact|tutors?$|module leader|worksheets? \d", re.I)
_SKIP_ANS = re.compile(r"^who\b|module leader|\btutors?\b|https?://|check with|placeholder|\[insert|nothing specific|\bfolder\b|on blackboard|the (material|brief|file|slides?) "
                       r"(says|links|does ?n[o']t|doesn't)", re.I)


_SKIP_TABLE = {"name", "section", "level", "item", "column", "field", "customer", "channel", "module", "task", "due",
               "date", "week", "when", "deadline", "contact", "email", "role", "who", "time", "session", "room", "file"}


def _plain(t):
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t or "")
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+)(?<!\*)\*(?!\*)", r"\1", t)      # *italic* -> italic, **bold** stays
    return re.sub(r"[ \t]+", " ", t).strip()


def note_week(n):
    """The teaching week a note belongs to: named in its title or folder, else from when its file changed,
    counted from the start of that module's own term."""
    wk = said_week(n["title"], n.get("path"))
    t = when_ms(n.get("changed"))
    if not wk and t:
        t0 = datetime.fromtimestamp(term_start([n["course"]]) / 1000).date()
        wk = (datetime.fromtimestamp(t / 1000).date() - t0).days // 7 + 1
    return wk if 0 < wk <= 30 else 0


def make_cards(notes, mod_key, modules, this_year=()):
    out = []
    for n in notes:
        wk, cur = note_week(n), n["course"] in this_year
        text = re.sub(r"```.*?```", "", n.get("md") or "", flags=re.S)
        text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
        key, note = mod_key(n["course"]), n.get("topic") or n["title"]
        seen, cards = set(), []

        def add(kind, q, a, ask):
            q, a = _plain(q), _plain(a)
            if len(q) < 3 or len(a) < 6 or len(a) > 900 or q.lower() in seen or _SKIP_ANS.search(q + " " + a):
                return
            seen.add(q.lower())
            cid = hashlib.sha1(f"{n['course']}|{kind}|{q}".encode()).hexdigest()[:12]
            cards.append({"id": cid, "key": key, "mod": modules.get(key, ""), "note": note, "title": n["title"],
                          "kind": kind, "ask": ask, "q": q, "a": a, "wk": wk, "cur": cur})

        # ideas: a ### heading and its first points
        for m in re.finditer(r"^###\s+(.+?)\s*$\n(.*?)(?=^#{1,3}\s|\Z)", text, re.M | re.S):
            head = re.sub(r"^[\d.]+\s*", "", m.group(1)).strip(" :*")
            if _SKIP_HEAD.search(head) or len(head) > 90:
                continue
            body = m.group(2)
            pts = [re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", l).strip() for l in body.splitlines()
                   if re.match(r"^\s*(?:[-*+]|\d+\.)\s+", l)]
            pts = [p for p in pts if not re.match(r"(?i)\**example", p)][:4]
            if not pts:
                para = next((l.strip() for l in body.splitlines() if l.strip() and not l.lstrip().startswith(("|", "#"))), "")
                pts = [para] if para else []
            if pts:
                add("idea", head, "\n".join("- " + p for p in pts) if len(pts) > 1 else pts[0], "Explain")
        # definitions: "A **scalar** is a single number"
        for m in re.finditer(r"\*\*([^*\n]{2,50})\*\*\s+(is|are|means|refers to|describes|stands for)\s+([^\n]{8,260}?)(?:\.(?:\s|$)|\n|$)", text):
            term = m.group(1).strip(" :")
            add("define", term, f"**{term}** {m.group(2)} {m.group(3).strip()}.", "Define")
        # tables: first column asks, the rest answers
        for block in re.findall(r"((?:^\|.*\|\s*$\n?){3,})", text, re.M):
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in block.strip().splitlines()]
            if len(rows[0]) < 2 or not re.match(r"^[\s:|-]+$", block.strip().splitlines()[1]):
                continue
            head = rows[0]
            h0 = re.sub(r"[*_`]", "", head[0]).strip().lower()
            if (h0 in _SKIP_TABLE or "\\" in h0 or "named" in h0 or not re.match(r"[a-z]", h0)):
                continue                       # contact lists, number grids, ticks-and-crosses, deadlines
            for r in rows[2:20]:
                if len(r) >= 2 and r[0] and not re.fullmatch(r"[\d\s.%£$-]+", r[0]):
                    ans = "\n".join(f"- {h}: {c}" for h, c in zip(head[1:], r[1:]) if c) if len(head) > 2 else r[1]
                    add("table", f"{r[0]}", ans, head[0] or "Recall")
        # fill the gap: a sentence with one bold term
        gaps = 0
        for sent in re.split(r"(?<=[.!?])\s+|\n", text):
            b = re.findall(r"\*\*([^*\n]{3,40})\*\*", sent)
            s = sent.strip(" -*")
            if len(b) == 1 and 50 <= len(s) <= 240 and not s.startswith(("#", "|")) and gaps < 6 \
                    and not re.search(r"\b(a|an|the|of|for|to|and|in|on|with|by)$", b[0].strip(), re.I):
                q = s.replace(f"**{b[0]}**", "_____", 1)
                if "_____" in q:
                    gaps += 1
                    add("gap", q, b[0], "Fill the gap")
        out += cards[:40]
    return out


def study_page(notes, mod_key, modules, term=0, this_year=()):
    cards = [c for c in make_cards(notes, mod_key, modules, this_year) if c["key"] in modules]
    data = json.dumps({"cards": cards, "state": load_json(DATA_DIR / "study.json", {}), "term": term,
                       "modules": [{"key": k, "name": v} for k, v in modules.items()
                                   if any(c["key"] == k for c in cards)]}).replace("</", "<\\/")
    return (STUDY_HTML + f'<script type="application/json" id="study-data">{data}</script>') if cards else \
        '<div class="panel glass"><p class="sub" style="margin:0">Study cards are made from your notes. Make some notes in the Notes tab and they show up here.</p></div>'


STUDY_HTML = """
<style>
.st-top{display:grid;grid-template-columns:1.4fr 1fr;gap:14px;margin-bottom:14px}
@media (max-width:760px){.st-top{grid-template-columns:1fr}}
.st-stats,.st-focus,.st-stage,.st-chart{border-radius:22px;padding:18px}
.st-stats{display:flex;align-items:center;gap:16px 20px;flex-wrap:wrap}
.st-ring{position:relative;width:96px;height:96px;flex:none}
.st-ring svg{width:96px;height:96px;transform:rotate(-90deg)}
.st-ring circle{fill:none;stroke-width:8} .st-ring .bg{stroke:var(--inset)} .st-ring .fg{stroke:var(--text);stroke-linecap:round;transition:stroke-dashoffset .7s cubic-bezier(.2,.9,.3,1),stroke .3s}
.st-ring.met .fg{stroke:#2fb36b} .st-ring.win{animation:st-win .7s}
.st-ring .in{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.st-ring b{font:800 24px/1 var(--dotf)} .st-ring small{font:10px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.st-nums{display:grid;grid-template-columns:repeat(3,minmax(70px,1fr));gap:10px 16px;flex:1;min-width:220px}
.st-nums b{display:block;font:800 22px/1.1 var(--dotf);transform-origin:left center} .st-nums small{font:10.5px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.st-streak.hot{color:#f5a524} .st-streak.hot::after{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#f5a524;margin-left:5px;vertical-align:5px;box-shadow:0 0 8px #f5a524}
.st-lvl{flex-basis:100%;display:grid;grid-template-columns:auto 1fr auto;align-items:baseline;gap:6px 10px}
.st-lvl b{font:800 13px var(--mono)} .st-lvl span{font:11px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.06em} .st-lvl small{font:10.5px var(--mono);color:var(--muted)}
.st-lvbar{grid-column:1/-1;height:6px;border-radius:3px;background:var(--inset);overflow:hidden}
.st-lvbar i{display:block;height:100%;width:0;border-radius:3px;background:linear-gradient(90deg,#3b82f6,#a78bfa,#f472b6);transition:width .8s cubic-bezier(.2,.9,.3,1)}
.st-focus{display:flex;flex-direction:column;gap:10px}
.st-fhead{display:flex;justify-content:space-between;align-items:center;gap:8px;font:11px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.st-time{font:800 44px/1 var(--dotf);letter-spacing:.04em}
.st-focus.running .st-time{animation:st-breathe 3.2s ease-in-out infinite}
.st-fbar{height:6px;border-radius:3px;background:var(--inset);overflow:hidden} .st-fbar i{display:block;height:100%;width:0;background:var(--text);transition:width .9s linear}
.st-focus.running .st-fbar i{background:linear-gradient(90deg,#3b82f6,#2fb36b)} .st-focus.brk .st-fbar i{background:#f5a524}
.st-fexp{font:11px/1.45 var(--mono);color:var(--muted)}
.st-rounds{display:inline-flex;gap:4px;margin-left:4px} .st-rounds i{width:8px;height:8px;border-radius:50%;border:1px solid var(--line);transition:background .3s}
.st-rounds i.on{background:#2fb36b;border-color:#2fb36b}
.st-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.st button.chip{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;padding:6px 13px;font:12px var(--mono);cursor:pointer;transition:background .15s,color .15s,border-color .15s,transform .15s}
.st button.chip:hover{border-color:var(--text)} .st button.chip:active{transform:scale(.96)}
.st button.chip.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.st button.chip:disabled{opacity:.4;cursor:default}
/* filters: one tidy line each, scrolling sideways when they don't fit */
.st-filters{display:grid;gap:8px;margin:0 0 14px}
.st-line{display:flex;align-items:center;gap:10px;min-width:0}
.st-line[hidden]{display:none}
.st-line>small{flex:none;width:58px;font:10px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.st-line{position:relative}
.st-scroll{position:relative;display:flex;gap:6px;overflow-x:auto;min-width:0;padding:3px 2px;scrollbar-width:none}
/* the fade and arrow only show on a side with more to see */
.st-line.r .st-scroll{-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 56px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 56px),transparent)}
.st-line.l .st-scroll{-webkit-mask-image:linear-gradient(90deg,transparent,#000 56px);mask-image:linear-gradient(90deg,transparent,#000 56px)}
.st-line.l.r .st-scroll{-webkit-mask-image:linear-gradient(90deg,transparent,#000 56px,#000 calc(100% - 56px),transparent);mask-image:linear-gradient(90deg,transparent,#000 56px,#000 calc(100% - 56px),transparent)}
.st-arr{position:absolute;top:50%;z-index:2;width:28px;height:28px;margin-top:-14px;padding:0;border-radius:50%;border:1px solid var(--line);background:var(--bg);color:var(--text);
 font:15px/1 var(--mono);cursor:pointer;display:none;place-items:center;box-shadow:0 2px 12px rgba(0,0,0,.35);transition:border-color .15s,transform .15s}
.st-arr:hover{border-color:var(--text);transform:scale(1.08)} .st-arr.l{left:66px} .st-arr.r{right:-6px}
.st-line.l .st-arr.l,.st-line.r .st-arr.r{display:grid}
.st-scroll::-webkit-scrollbar{display:none}
.st-scroll button.chip{flex:none;white-space:nowrap}
.st-modes{gap:10px;align-items:center} .st-seg{flex:none}
.st-seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:3px;gap:2px}
.st .st-seg button.chip{border:0;padding:6px 12px}
.st-modes button.chip b{font-weight:700;margin-left:5px;opacity:.75}
.st-weeks .wk{position:relative;flex:none;border:1px solid var(--line);background:transparent;color:var(--text);border-radius:12px;padding:6px 12px 8px;cursor:pointer;display:grid;gap:1px;text-align:left;overflow:hidden;transition:border-color .2s,transform .15s,background .15s}
.st-weeks .wk:hover{border-color:var(--text)}
.st-weeks .wk b{font:700 12px var(--mono);white-space:nowrap} .st-weeks .wk small{font:10px var(--mono);color:var(--muted);white-space:nowrap}
.st-weeks .wk i{position:absolute;left:0;bottom:0;height:3px;background:#2fb36b;transition:width .6s}
.st-weeks .wk.on{background:var(--text);color:var(--bg);border-color:var(--text)} .st-weeks .wk.on small{color:inherit;opacity:.7}
.st-weeks .wk.now:not(.on){border-color:#2fb36b}
.st-weeks .wk em{font-style:normal;color:#f5a524;margin-left:4px}
.st-stage{display:flex;flex-direction:column;gap:16px}
/* what you pick opens in a panel that drops down under the start screen */
.st-drop{display:flex;flex-direction:column;gap:14px;border-top:1px solid var(--line);padding-top:16px;scroll-margin-top:16px}
.st-drop[hidden]{display:none}
.st-drop.opening{animation:st-drop .45s cubic-bezier(.2,.9,.3,1.1) both;transform-origin:top center}
.st-dhead{display:flex;justify-content:space-between;align-items:center;gap:10px;font:700 12px var(--mono);text-transform:uppercase;letter-spacing:.08em}
.st-x{flex:none;width:30px;height:30px;padding:0;border-radius:50%;border:1px solid var(--line);background:transparent;color:var(--text);cursor:pointer;font-size:12px;transition:transform .25s,border-color .15s}
.st-x:hover{border-color:var(--text);transform:rotate(90deg)}
.st-body{display:flex;flex-direction:column;gap:14px;min-height:300px}
.st-body.swap>*{animation:st-fade .35s both}
.st-stage.open .st-hero b{font-size:24px}
.st-stage.open .st-tip{display:none}
@keyframes st-drop{from{opacity:0;transform:translateY(-16px) scaleY(.96)}}
/* rewards */
.st-rew{display:grid;grid-template-columns:1fr 1.25fr;gap:18px 24px;border-radius:22px;padding:18px;margin-bottom:14px}
@media (max-width:760px){.st-rew{grid-template-columns:1fr}}
.st-quests .st-fhead{flex-wrap:wrap;gap:4px 12px} .st-quests .st-fhead span{white-space:nowrap}
.st-qbonus{color:#f5a524;text-transform:none;letter-spacing:0} .st-qbonus.all{color:#2fb36b}
.st-qlist{display:grid;gap:12px;margin-top:12px}
.st-quest{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:6px 10px;font-size:13.5px}
.st-quest i.ck{width:18px;height:18px;border-radius:50%;border:1.5px solid var(--line);display:grid;place-items:center;transition:background .3s,border-color .3s}
.st-quest.done i.ck{background:#2fb36b;border-color:#2fb36b}
.st-quest.done i.ck::after{content:"";width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:translateY(-1px) rotate(45deg)}
.st-quest.just i.ck{animation:st-win .6s}
.st-quest small{font:11px var(--mono);color:var(--muted)}
.st-quest.done span{color:var(--muted)}
.st-qbar{grid-column:2/-1;height:4px;border-radius:2px;background:var(--inset);overflow:hidden}
.st-qbar i{display:block;height:100%;border-radius:2px;background:linear-gradient(90deg,#3b82f6,#2fb36b);transition:width .7s cubic-bezier(.2,.9,.3,1)}
.st-bgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(66px,1fr));gap:10px 6px;margin-top:12px}
.st-badge{display:grid;justify-items:center;gap:5px;text-align:center;cursor:default}
.st-badge b{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;font:800 11px var(--mono);border:1.5px dashed var(--line);color:var(--muted);transition:transform .25s cubic-bezier(.3,1.6,.5,1)}
.st-badge small{font:9.5px/1.25 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.st-badge.got b{border:0;color:#5a3a00;background:linear-gradient(135deg,#ffe08a,#f5a524);box-shadow:0 4px 14px -4px rgba(245,165,36,.6)}
.st-badge.got small{color:var(--text)}
.st-badge:hover b{transform:translateY(-2px) rotate(-8deg) scale(1.06)}
.st-badge.just b{animation:st-spin .8s cubic-bezier(.2,.9,.3,1.3)}
.st-cshead{margin-top:16px} .st-cs{margin-top:8px}
.st-cs i.csw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px;border:1px solid var(--line)}
.csw.plain{background:var(--inset)} .csw.ocean{background:linear-gradient(135deg,#3b82f6,#2fb36b)} .csw.sunset{background:linear-gradient(135deg,#f5a524,#e5484d)} .csw.aurora{background:linear-gradient(135deg,#a78bfa,#f472b6,#2fb36b)}
.st-next-un{text-transform:none;letter-spacing:0}
.st[data-cs=ocean] .st-face{background:linear-gradient(135deg,rgba(59,130,246,.2),rgba(47,179,107,.1)),var(--inset)}
.st[data-cs=sunset] .st-face{background:linear-gradient(135deg,rgba(245,165,36,.2),rgba(229,72,77,.12)),var(--inset)}
.st[data-cs=aurora] .st-face{background:linear-gradient(135deg,rgba(167,139,250,.22),rgba(244,114,182,.12) 55%,rgba(47,179,107,.12)),var(--inset)}
.st-meta{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font:11px var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.st-meta a{color:inherit;cursor:pointer;text-decoration:underline}
.st-combo{color:#f5a524;font-weight:700;display:inline-block}
.st-prog{height:4px;border-radius:2px;background:var(--inset);overflow:hidden} .st-prog i{display:block;height:100%;width:0;background:var(--text);transition:width .45s cubic-bezier(.2,.9,.3,1)}
/* the card: a stack you work through, a 3D turn, and a fling when graded */
.st-deck{position:relative;margin-bottom:14px}
.st-deck::before,.st-deck::after{content:"";position:absolute;left:14px;right:14px;bottom:-7px;height:7px;border-radius:0 0 14px 14px;border:1px solid var(--line);border-top:0;background:var(--inset);opacity:.7;transition:opacity .3s}
.st-deck::after{left:28px;right:28px;bottom:-13px;height:6px;opacity:.4}
.st-deck.d0::before,.st-deck.d0::after,.st-deck.d1::after{opacity:0}
.st-card{position:relative;z-index:1;perspective:1400px;cursor:pointer;outline:none}
.st-card:focus-visible .st-face{box-shadow:0 0 0 2px var(--text)}
.st-flip{position:relative;width:100%;min-height:230px;transition:transform .6s cubic-bezier(.3,1.35,.5,1);transform-style:preserve-3d}
.st-card.back .st-flip{transform:rotateY(180deg)}
.st-face{position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden;border:1px solid var(--line);border-radius:18px;padding:22px 24px;
 display:flex;flex-direction:column;justify-content:center;gap:10px;background:var(--inset);overflow:auto}
.st-face.b{transform:rotateY(180deg)}
.st-face.b::after{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(105deg,transparent 42%,rgba(255,255,255,.07) 50%,transparent 58%);transform:translateX(-130%)}
.st-card.back:not(.ghost) .st-face.b::after{animation:st-shine 1s .3s ease}
.st-card.in{animation:st-in .45s cubic-bezier(.2,.9,.3,1.15) both}
.st-card.lift{animation:st-lift .6s ease}
.st-card.ghost{position:absolute;inset:0;z-index:3;pointer-events:none;transition:transform .42s cubic-bezier(.5,0,.75,.3),opacity .42s}
.st-card.ghost.out{opacity:0}
.st-card.ghost.out.g0{transform:translateX(-55%) rotate(-9deg)} .st-card.ghost.out.g1{transform:translate(-18%,-30%) rotate(-4deg)}
.st-card.ghost.out.g2{transform:translateX(55%) rotate(8deg)} .st-card.ghost.out.g3{transform:translate(60%,-28%) rotate(14deg)}
.st-card.ghost.g0 .st-face{border-color:#e5484d;box-shadow:inset 0 0 0 2px rgba(229,72,77,.5)} .st-card.ghost.g1 .st-face{border-color:#f5a524;box-shadow:inset 0 0 0 2px rgba(245,165,36,.5)}
.st-card.ghost.g2 .st-face{border-color:#2fb36b;box-shadow:inset 0 0 0 2px rgba(47,179,107,.5)} .st-card.ghost.g3 .st-face{border-color:#3b82f6;box-shadow:inset 0 0 0 2px rgba(59,130,246,.5)}
.st-ask{font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:flex;align-items:center}
.st-ask i.k,.st-list summary i.k{flex:none;display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:var(--kc)}
.st-q{font-size:22px;line-height:1.35;font-weight:700} .st-pop{animation:st-pop .35s both}
.st-a{font-size:16px;line-height:1.55} .st-a ul{margin:0;padding-left:20px} .st-a code,.st-q code{font-family:var(--mono);font-size:.9em;background:var(--bg);padding:1px 5px;border-radius:5px}
.st-hint{font:11px var(--mono);color:var(--muted)}
.st-blank{display:inline-block;width:3.4em;height:.9em;border-bottom:2px solid currentColor;opacity:.55;margin:0 .15em;vertical-align:baseline}
.st-grade,.st-next{scroll-margin-bottom:96px}
.st-grade{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.st-grade button{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:14px;padding:10px 6px;cursor:pointer;font:600 14px var(--sans,inherit);display:flex;flex-direction:column;align-items:center;gap:2px;
 animation:st-pop .3s both;animation-delay:calc(var(--i) * 45ms);transition:background .15s,border-color .15s,transform .12s}
.st-grade button:hover{transform:translateY(-2px)} .st-grade button:active{transform:scale(.95)}
.st-grade button small{font:10.5px var(--mono);color:var(--muted)} .st-grade button kbd{font:10px var(--mono);opacity:.6}
.st-grade .g0:hover{border-color:#e5484d;background:rgba(229,72,77,.12)} .st-grade .g1:hover{border-color:#f5a524;background:rgba(245,165,36,.12)}
.st-grade .g2:hover{border-color:#2fb36b;background:rgba(47,179,107,.12)} .st-grade .g3:hover{border-color:#3b82f6;background:rgba(59,130,246,.12)}
.st-show,.st-cta{border:0;border-radius:14px;padding:12px 18px;background:var(--text);color:var(--bg);font-weight:700;cursor:pointer;font-size:15px;font-family:inherit;transition:transform .12s,opacity .15s}
.st-show:hover,.st-cta:hover{opacity:.9} .st-show:active,.st-cta:active{transform:scale(.98)}
.st-act{display:grid}
.st-opts{display:grid;gap:8px}
.st-opt,.st-tile,.st-pick,.st-note,.st-show,.st-arr{font-family:inherit}
.st-opt{text-align:left;border:1px solid var(--line);background:transparent;color:var(--text);border-radius:14px;padding:12px 14px;cursor:pointer;font-size:14.5px;line-height:1.45;display:flex;gap:10px;
 animation:st-pop .32s both;animation-delay:calc(var(--i) * 55ms);transition:border-color .15s,background .2s,opacity .25s,transform .12s}
.st-opt kbd{font:11px var(--mono);color:var(--muted);flex:none;margin-top:2px}
.st-opt:hover{border-color:var(--text);transform:translateX(3px)}
.st-opt.right{border-color:#2fb36b;background:rgba(47,179,107,.14);animation:st-good .45s} .st-opt.wrong{border-color:#e5484d;background:rgba(229,72,77,.14);animation:st-shake .4s}
.st-opt.dim{opacity:.45} .st-opt.right,.st-opt.wrong,.st-opt.dim{cursor:default;transform:none}
.st-done{text-align:center;display:grid;gap:10px;place-items:center;flex:1;align-content:center}
.st-done b{font:800 40px/1 var(--dotf)} .st-done p{margin:0;color:var(--muted)} .st-done .st-big{animation:st-win .6s .1s both}
.st-sub2{font:12px var(--mono)}
.st-medal{width:58px;height:58px;display:grid;place-items:center;border-radius:50%;background:var(--inset);border:1px solid var(--line);color:var(--muted);animation:st-spin .7s cubic-bezier(.2,.9,.3,1.3) both}
.st-medal.gold{background:linear-gradient(135deg,#ffe08a,#f5a524);border-color:#f5a524;color:#5a3a00;box-shadow:0 0 24px rgba(245,165,36,.45)}
/* start screen */
.st-home{display:grid;gap:18px}
.st-hero{display:grid;gap:8px;justify-items:start}
.st-hero b{font:800 32px/1.05 var(--dotf)} .st-hero p{margin:0;color:var(--muted)}
.st-picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.st-pick{position:relative;border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:16px;padding:14px;text-align:left;cursor:pointer;display:grid;gap:6px;align-content:start;
 transition:transform .18s,border-color .18s,box-shadow .18s,background .18s}
.st-home.intro .st-pick{animation:st-pop .4s both;animation-delay:calc(var(--i) * 70ms)}
.st-pick:hover{transform:translateY(-4px);border-color:var(--pc);box-shadow:0 10px 26px -14px var(--pc)}
/* the open one stays lit, with its arrow turned down toward its panel */
.st-pick.on{border-color:var(--pc);box-shadow:0 0 0 1px var(--pc),0 12px 28px -14px var(--pc)}
.st-chev{position:absolute;top:16px;right:14px;width:8px;height:8px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;transform:rotate(-45deg);transition:transform .25s,border-color .2s}
.st-pick:hover .st-chev{border-color:var(--text)} .st-pick.on .st-chev{transform:rotate(45deg);border-color:var(--pc)}
.st-pick b{font-size:15px} .st-pick small{color:var(--muted);font-size:12.5px;line-height:1.4}
.st-pi{width:36px;height:36px;border-radius:11px;display:grid;place-items:center;background:var(--pc);color:#fff;transition:transform .25s cubic-bezier(.3,1.6,.5,1)}
.st-pick:hover .st-pi{transform:rotate(-8deg) scale(1.1)}
.st-tip{margin:0;font-size:13px;color:var(--muted)} .st-tip b{font:700 10.5px var(--mono);text-transform:uppercase;letter-spacing:.08em;margin-right:8px;color:var(--text)}
/* Match */
.st-match{display:grid;grid-template-columns:1fr 1.5fr;gap:10px}
.st-col{display:grid;gap:8px;align-content:start}
.st-tile{border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:12px;padding:10px 12px;text-align:left;cursor:pointer;font-size:13.5px;line-height:1.4;
 animation:st-pop .3s both;animation-delay:calc(var(--i) * 40ms);transition:opacity .3s,transform .3s,border-color .15s,background .15s}
.st-tile.q{font-weight:700} .st-tile:hover{border-color:var(--text)}
.st-tile.sel{border-color:#3b82f6;background:rgba(59,130,246,.16);transform:scale(1.02)}
.st-tile.ok{border-color:#2fb36b;background:rgba(47,179,107,.18);animation:st-good .35s}
.st-tile.bad{border-color:#e5484d;animation:st-shake .4s}
.st-tile.gone{opacity:0;transform:scale(.85);pointer-events:none}
/* Type it */
.st-typebox{display:flex;gap:8px;flex-wrap:wrap}
.st-in{flex:1;min-width:180px;border:1px solid var(--line);border-radius:12px;padding:11px 14px;background:var(--inset);color:var(--text);font-family:inherit;font-size:16px;font-weight:600;outline:none;transition:border-color .2s,box-shadow .2s}
.st-in:focus{border-color:var(--text);box-shadow:0 0 0 3px rgba(127,127,127,.18)}
.st-in.right{border-color:#2fb36b;box-shadow:0 0 0 3px rgba(47,179,107,.22);animation:st-good .4s}
.st-in.wrong,.st-blurt.wrong{border-color:#e5484d;animation:st-shake .4s}
.st-fb{min-height:22px;font-size:15px} .st-fb>*{animation:st-fade .3s both} .st-fb .ok{color:#2fb36b} .st-fb .no{color:#e5484d}
.st-hintt{font:600 16px var(--mono);letter-spacing:.1em;color:var(--muted)}
/* Blurt */
.st-intro b{font:800 22px/1.2 var(--dotf)} .st-intro p{margin:6px 0 0;color:var(--muted)}
.st-notes{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
.st-note{border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:12px;padding:10px 12px;text-align:left;cursor:pointer;display:grid;gap:3px;
 animation:st-pop .3s both;animation-delay:min(calc(var(--i) * 30ms),.6s);transition:transform .15s,border-color .15s}
.st-note:hover{transform:translateY(-2px);border-color:#a78bfa} .st-note span{font-weight:700;font-size:14px} .st-note small{font:10.5px var(--mono);color:var(--muted)}
.st-blurt{min-height:220px;resize:vertical;border:1px solid var(--line);border-radius:14px;padding:14px;background:var(--inset);color:var(--text);font-family:inherit;font-size:15px;line-height:1.6;width:100%;box-sizing:border-box;outline:none}
.st-blurt:focus{border-color:var(--text)}
.st-wc{font:11px var(--mono);color:var(--muted)}
.st-bres{display:flex;gap:18px;align-items:center;flex-wrap:wrap} .st-bres p{margin:4px 0 0;color:var(--muted)} .st-btitle{font-size:20px}
.st-bres .st-ring .fg{transition:stroke-dashoffset 1.2s cubic-bezier(.2,.9,.3,1);stroke:#a78bfa}
.st-pts{display:grid;gap:6px;max-height:340px;overflow:auto}
.st-pt{display:flex;gap:10px;align-items:baseline;padding:7px 10px;border-radius:10px;border:1px solid var(--line);font-size:14px;animation:st-pop .3s both;animation-delay:min(calc(var(--i) * 45ms),1.4s)}
.st-pt i{flex:none;width:9px;height:9px;border-radius:50%;transform:translateY(1px)}
.st-pt.got{border-color:rgba(47,179,107,.5);background:rgba(47,179,107,.08)} .st-pt.got i{background:#2fb36b}
.st-pt.miss{opacity:.8} .st-pt.miss i{border:1.5px solid #e5484d}
/* All cards */
.st-list{display:grid;gap:6px;max-height:520px;overflow:auto}
.st-wh{margin:10px 0 2px;font:700 12px var(--mono);text-transform:uppercase;letter-spacing:.06em;display:flex;gap:8px;align-items:baseline}
.st-wh small{font-weight:400;color:var(--muted)} .st-list .st-wh:first-child{margin-top:0}
.st-list details{border:1px solid var(--line);border-radius:12px;padding:9px 12px} .st-list summary{cursor:pointer;font-size:14px}
.st-list summary small{font:10.5px var(--mono);color:var(--muted);margin-left:6px;text-transform:uppercase}
.st-list .lv.learnt{color:#2fb36b} .st-list .lv.weak{color:#e5484d} .st-list .lv.learning{color:#3b82f6}
.st-list .st-a{font-size:14px;margin-top:6px}
.st-search{border:1px solid var(--line);border-radius:12px;padding:9px 12px;background:var(--inset);color:var(--text);font:inherit;width:100%;box-sizing:border-box}
.st-chart{margin-top:14px} .st-bars{display:flex;align-items:flex-end;gap:5px;height:90px;margin-top:10px}
.st-bars div{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;font:10px var(--mono);color:var(--muted)}
.st-bars i{display:block;width:100%;border-radius:4px 4px 2px 2px;background:var(--text);min-height:2px;opacity:.85;transform-origin:bottom;animation:st-grow .5s both;animation-delay:calc(var(--i) * 25ms)}
.st-bars.seen i{animation:none;transition:height .5s cubic-bezier(.2,.9,.3,1)}
.st-bars .hit i{background:#3b82f6;opacity:1} .st-bars .today i{background:#2fb36b;opacity:1}
nav button[data-tab=study].has-due::after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#2fb36b;margin-left:5px;vertical-align:3px}
/* things that pop up over the page */
.st-float{position:fixed;z-index:9999;pointer-events:none;transform:translate(-50%,0);font:800 14px var(--mono);color:#2fb36b;text-shadow:0 1px 8px rgba(0,0,0,.35);animation:st-float .95s cubic-bezier(.2,.7,.3,1) forwards}
.st-confetti{position:fixed;inset:0;z-index:9998;pointer-events:none}
.st-toast{position:fixed;left:50%;top:22px;z-index:9999;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:10px 18px;border-radius:999px;font:700 13px var(--mono);pointer-events:none;box-shadow:0 10px 30px -10px rgba(0,0,0,.5);animation:st-toast 2.7s ease forwards}
.bump{animation:st-bump .35s}
@keyframes st-in{from{opacity:0;transform:translateY(18px) scale(.96)}}
@keyframes st-lift{40%{transform:translateY(-8px) scale(1.025)}}
@keyframes st-pop{from{opacity:0;transform:translateY(8px) scale(.94)}}
@keyframes st-fade{from{opacity:0;transform:translateY(6px)}}
@keyframes st-shake{20%,60%{transform:translateX(-6px)}40%,80%{transform:translateX(6px)}}
@keyframes st-good{40%{transform:scale(1.03)}}
@keyframes st-bump{40%{transform:scale(1.22)}}
@keyframes st-win{0%{opacity:0;transform:scale(.6)}55%{opacity:1;transform:scale(1.12)}100%{transform:scale(1)}}
@keyframes st-spin{from{opacity:0;transform:rotate(-120deg) scale(.4)}}
@keyframes st-grow{from{transform:scaleY(0)}}
@keyframes st-float{to{transform:translate(-50%,-46px);opacity:0}}
@keyframes st-shine{to{transform:translateX(130%)}}
@keyframes st-breathe{50%{opacity:.7}}
@keyframes st-toast{0%{opacity:0;transform:translate(-50%,-16px)}10%,85%{opacity:1;transform:translate(-50%,0)}100%{opacity:0;transform:translate(-50%,-10px)}}
@media (prefers-reduced-motion:reduce){.st *,.st-float,.st-toast{animation:none!important;transition:none!important}}
</style>
<div class="st">
 <div class="st-top">
  <div class="st-stats glass">
   <div class="st-ring" title="Today's goal"><svg viewBox="0 0 96 96"><circle class="bg" cx="48" cy="48" r="40"/><circle class="fg" cx="48" cy="48" r="40" stroke-dasharray="251.3" stroke-dashoffset="251.3"/></svg>
    <div class="in"><b class="st-goal-n">0</b><small class="st-goal-of">of 20</small></div></div>
   <div class="st-nums">
    <div><b class="st-due">0</b><small>due now</small></div><div><b class="st-new">0</b><small>new cards</small></div><div><b class="st-streak">0</b><small>day streak</small></div>
    <div><b class="st-acc">–</b><small>right today</small></div><div><b class="st-mins">0</b><small>focus min</small></div><div><b class="st-known">0</b><small>learnt</small></div>
   </div>
   <div class="st-lvl" title="You earn XP for every card, quiz answer, match and blurt"><b class="st-lv">Level 1</b><span class="st-lvname">Starter</span><small class="st-xp">0 / 100 XP</small><div class="st-lvbar"><i></i></div></div>
  </div>
  <div class="st-focus glass">
   <div class="st-fhead"><span class="st-fmode">Focus</span><span class="st-row"><button class="chip st-len on" data-w="25" data-b="5" title="25 minutes of focus, then a 5 minute break">25 / 5</button><button class="chip st-len" data-w="50" data-b="10" title="50 minutes of focus, then a 10 minute break">50 / 10</button></span></div>
   <div class="st-time">25:00</div><div class="st-fbar"><i></i></div>
   <small class="st-fexp"></small>
   <div class="st-row"><button class="chip st-go">Start</button><button class="chip st-reset">Reset</button><span class="st-rounds" title="Focus rounds finished today"></span><small class="st-fmsg" style="color:var(--muted)"></small></div>
  </div>
 </div>
 <div class="st-rew glass">
  <div class="st-quests"><div class="st-fhead"><span>Today's challenges</span><span class="st-qbonus"></span></div><div class="st-qlist"></div></div>
  <div class="st-bwrap"><div class="st-fhead"><span>Badges</span><span class="st-bcount"></span></div><div class="st-bgrid"></div>
   <div class="st-fhead st-cshead"><span>Card style</span><span class="st-next-un"></span></div><div class="st-row st-cs"></div></div>
 </div>
 <div class="st-filters">
  <div class="st-line"><small>Module</small><div class="st-scroll st-mods" role="group" aria-label="Module"></div></div>
  <div class="st-line st-wline"><small>Week</small><div class="st-scroll st-weeks" role="group" aria-label="Week"></div></div>
  <div class="st-line"><small>Study</small><div class="st-scroll st-modes" role="group" aria-label="How to study">
   <span class="st-seg"><button class="chip" data-m="due" title="Cards due for a review">Due<b class="n-due"></b></button><button class="chip" data-m="new" title="Cards you haven't seen yet">New<b class="n-new"></b></button><button class="chip" data-m="weak" title="Cards you keep getting wrong">Weak<b class="n-weak"></b></button></span>
   <span class="st-seg"><button class="chip" data-m="quiz">Quiz</button><button class="chip" data-m="match">Match</button><button class="chip" data-m="type">Type it</button><button class="chip" data-m="blurt">Blurt</button></span>
   <span class="st-seg"><button class="chip" data-m="all" title="Every card, by week, searchable">All<b class="n-all"></b></button></span>
  </div></div>
 </div>
 <div class="st-stage glass" aria-live="polite"></div>
 <div class="st-chart glass"><div class="st-fhead"><span>Last 14 days · cards reviewed</span><span class="st-goal-set">Daily goal <button class="chip st-gm">−</button> <b class="st-goal-v"></b> <button class="chip st-gp">+</button></span></div><div class="st-bars"></div></div>
</div>
"""

STUDY_JS = r"""
// ---- Study: cards from your notes with spaced repetition, plus a quiz, Match, Type it, Blurt, a focus timer and XP
(() => {
  const ROOT = document.getElementById('study'), DATA = document.getElementById('study-data');
  if (!ROOT || !DATA) return;
  const D = JSON.parse(DATA.textContent), CARDS = D.cards, $ = s => ROOT.querySelector(s);
  const ST = Object.assign({cards: {}, days: {}, goal: 20, xp: 0, best: {}, blurts: {}, badges: {}, flags: {}, style: 'plain'}, D.state || {});
  ST.best ||= {}; ST.blurts ||= {}; ST.xp ||= 0; ST.badges ||= {}; ST.flags ||= {}; ST.style ||= 'plain';
  const DAY = 864e5, MIN = 6e4, dayKey = d => (d || new Date()).toLocaleDateString('en-CA');
  let saveT = 0;
  const save = () => { clearTimeout(saveT); saveT = setTimeout(() => { try { pywebview.api.study_save(ST); } catch (_) {} }, 400); };
  window.addEventListener('beforeunload', () => { try { pywebview.api.study_save(ST); } catch (_) {} });
  const today = () => (ST.days[dayKey()] ||= {rev: 0, right: 0, mins: 0});
  const esc = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
  const fmt = t => { const lines = esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>').replace(/_{3,}/g, '<span class="st-blank"></span>').split('\n');
    const pts = lines.filter(l => l.startsWith('- '));
    return pts.length === lines.length ? '<ul>' + pts.map(l => '<li>' + l.slice(2) + '</li>').join('') + '</ul>' : lines.join('<br>'); };
  const short = t => t.length > 220 ? t.slice(0, 217) + '…' : t;
  const calm = () => document.documentElement.classList.contains('no-anim') || matchMedia('(prefers-reduced-motion: reduce)').matches;
  const sfx = (n, v) => { if (window.SFX) SFX.play(n, v); };
  const shuffle = a => { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
  // teaching week, counted the way the note map counts it
  const NOW_WK = D.term ? Math.max(0, Math.floor(Math.round((new Date().setHours(0, 0, 0, 0) - D.term) / DAY) / 7) + 1) : 0;

  let mod = 'all', wk = 'all', mode = null, session = [], at = 0, flipped = false, quiz = null, game = null, done = {n: 0, right: 0, xp: 0}, lastProg = 0;
  const pool = () => CARDS.filter(c => (mod === 'all' || c.key === mod) && (wk === 'all' || (c.wk || 0) === +wk));
  const st = c => ST.cards[c.id];
  const dueList = () => pool().filter(c => st(c) && st(c).due <= Date.now()).sort((a, b) => st(a).due - st(b).due);
  const newList = () => pool().filter(c => !st(c));
  const weakList = () => pool().filter(c => st(c) && (st(c).lapses > 0 || st(c).ease < 2.3)).sort((a, b) => (st(b).lapses - st(a).lapses) || (st(a).ease - st(b).ease));
  const CARD_MODES = ['due', 'new', 'weak', 'set'];

  // ---- little bits of fun: XP and levels, floating numbers, confetti, toasts, counting numbers
  const LEVELS = ['Starter', 'Note taker', 'Learner', 'Reviser', 'Quick thinker', 'Scholar', 'Expert', 'Master', 'Legend'];
  const lvlOf = xp => { let l = 0, need = 100; while (xp >= need) { xp -= need; l++; need = Math.round(need * 1.35); } return {l, into: Math.round(xp), need}; };
  function gain(n, from) {
    if (!n) return;
    const before = lvlOf(ST.xp).l; ST.xp += n; done.xp += n; today().xp = (today().xp || 0) + n;
    float(from, '+' + n + ' XP');
    const L = lvlOf(ST.xp);
    if (L.l > before) setTimeout(() => { const un = STYLES.find(x => x[2] === L.l + 1);
      toast(`Level up! Level ${L.l + 1} · ${LEVELS[Math.min(L.l, LEVELS.length - 1)]}` + (un ? ` · ${un[1]} cards unlocked` : '')); confetti(); sfx('done'); }, 300);
  }
  function float(el, text) {
    if (calm() || !el || !el.isConnected) return;
    const r = el.getBoundingClientRect(), f = document.createElement('div');
    f.className = 'st-float'; f.textContent = text; f.style.left = (r.left + r.width / 2) + 'px'; f.style.top = (r.top - 4) + 'px';
    document.body.appendChild(f); setTimeout(() => f.remove(), 1000);
  }
  function toast(msg) {
    const t = document.createElement('div'); t.className = 'st-toast'; t.textContent = msg;
    t.style.top = (22 + 50 * document.querySelectorAll('.st-toast').length) + 'px';     // several stack instead of overlapping
    document.body.appendChild(t); setTimeout(() => t.remove(), 2700);
  }
  function confetti() {
    if (calm()) return;
    const c = document.createElement('canvas'), x = c.getContext('2d'), W = c.width = innerWidth, H = c.height = innerHeight;
    c.className = 'st-confetti'; document.body.appendChild(c);
    const cols = ['#2fb36b', '#3b82f6', '#f5a524', '#e5484d', '#a78bfa', '#f472b6'];
    const P = [...Array(130)].map(() => ({x: W / 2 + (Math.random() - .5) * W * .35, y: H * .42, vx: (Math.random() - .5) * 15, vy: -Math.random() * 15 - 4,
      r: Math.random() * 6, vr: (Math.random() - .5) * .35, w: 5 + Math.random() * 6, h: 3 + Math.random() * 4, c: cols[Math.floor(Math.random() * cols.length)]}));
    const t0 = performance.now();
    (function frame(t) {
      const k = (t - t0) / 1700; x.clearRect(0, 0, W, H);
      P.forEach(p => { p.vy += .38; p.vx *= .985; p.x += p.vx; p.y += p.vy; p.r += p.vr;
        x.save(); x.globalAlpha = Math.max(0, 1 - k * k); x.translate(p.x, p.y); x.rotate(p.r); x.fillStyle = p.c; x.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); x.restore(); });
      if (k < 1) requestAnimationFrame(frame); else c.remove();
    })(t0);
  }
  const bump = el => { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); };
  function setNum(el, v, suf = '') {
    const from = +el.dataset.v; el.dataset.v = v;
    if (isNaN(from) || from === v || calm()) { el.textContent = v + suf; return; }
    bump(el);
    if (Math.abs(v - from) <= 3) { el.textContent = v + suf; return; }
    const t0 = performance.now();
    (function f(t) { const k = Math.min(1, (t - t0) / 500), e = 1 - Math.pow(1 - k, 3);
      el.textContent = Math.round(from + (v - from) * e) + suf; if (k < 1 && +el.dataset.v === v) requestAnimationFrame(f); })(t0);
  }
  const TIPS = [
    'Say the answer out loud before you turn the card. It sticks better than reading it.',
    'Short and often beats one long session. Ten cards a day adds up fast.',
    'Got one wrong? Good. The mistakes you fix are the ones you remember.',
    'Mix modules in one session. Switching topics feels harder now and pays off in the exam.',
    'Explain an idea as if to a friend. The gaps show up straight away.',
    'Try Blurt the day after a lecture to see what actually stuck.',
    'A quick review before bed helps. Sleep is when memories settle.',
    'Hard cards come back sooner. That is the system working, not you failing.',
    'Use a focus round: 25 minutes on, 5 off. Your phone goes in another room.',
  ];
  const cheer = pc => pc >= 90 ? 'Brilliant. You know this.' : pc >= 75 ? 'Great work. Nearly all of it.' : pc >= 50 ? 'Getting there. The misses come back soon.' : 'A tough one. Another go later will feel easier.';
  const STAR = '<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><path d="M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4l-5.3 3 1.2-6-4.5-4.1 6-.7z" fill="currentColor"/></svg>';

  // ---- spaced repetition: Again / Hard / Good / Easy, intervals in days
  function next(c, g) {
    const s = Object.assign({ivl: 0, ease: 2.5, n: 0, lapses: 0}, st(c) || {});
    if (g === 0) { s.n = 0; s.lapses++; s.ivl = 0; s.ease = Math.max(1.3, s.ease - .2); s.due = Date.now() + 10 * MIN; return s; }
    s.n++;
    s.ivl = g === 1 ? Math.max(1, s.ivl * 1.2) : s.n === 1 ? (g === 3 ? 4 : 1) : s.n === 2 ? (g === 3 ? 7 : 3) : s.ivl * s.ease * (g === 3 ? 1.3 : 1);
    if (g === 1) s.ease = Math.max(1.3, s.ease - .15); if (g === 3) s.ease = Math.min(3.2, s.ease + .15);
    s.ivl = Math.min(365, Math.round(s.ivl * 10) / 10); s.due = Date.now() + s.ivl * DAY; return s;
  }
  const when = s => s.ivl ? (s.ivl < 1.5 ? '1 day' : s.ivl < 30 ? Math.round(s.ivl) + ' days' : Math.round(s.ivl / 30) + ' mo') : '10 min';
  function grade(g) {
    const c = session[at]; if (!c || !flipped) return;
    const btn = $('.st-grade .g' + g), old = $('.st-card:not(.ghost)');
    ST.cards[c.id] = Object.assign(next(c, g), {seen: Date.now()});
    const t = today(); t.rev++; if (g >= 2) t.right++; done.n++; if (g >= 2) done.right++;
    if (g === 0) session.push(c);                 // see it again before the end
    sfx(g === 0 ? 'tap' : 'open', .5);
    gain([2, 5, 10, 12][g], btn);
    save(); at++; flipped = false; stats(); draw();
    fling(old, g);
  }
  // the graded card flies off (left for Again, right for Good) while the next one rises from the stack
  function fling(old, g) {
    const deck = $('.st-deck'); if (!old || !deck || calm()) return;
    const gh = old.cloneNode(true); gh.classList.add('ghost'); gh.classList.remove('in', 'lift'); gh.removeAttribute('tabindex');
    deck.appendChild(gh);
    requestAnimationFrame(() => requestAnimationFrame(() => gh.classList.add('out', 'g' + g)));
    setTimeout(() => gh.remove(), 450);
  }

  // ---- what's on screen: the start screen stays on top, and what you pick opens in a drop-down panel under it
  const STG = $('.st-stage');
  STG.innerHTML = '<div class="st-home"></div><div class="st-drop" hidden><div class="st-dhead"><span class="st-dtitle"></span>'
    + '<button class="st-x" aria-label="Close" title="Close (Esc)">✕</button></div><div class="st-body" aria-live="polite"></div></div>';
  const HOME = $('.st-home'), DROP = $('.st-drop'), S = $('.st-body');
  const TITLES = {due: 'Review due cards', new: 'Learn new cards', weak: 'Practise weak cards', set: 'The points you missed',
    quiz: 'Quiz', match: 'Match', type: 'Type it', blurt: 'Blurt', all: 'All cards'};
  const markPicks = () => HOME.querySelectorAll('[data-go]').forEach(b => { const on = b.dataset.go === (mode === 'set' ? 'blurt' : mode);
    b.classList.toggle('on', on); b.setAttribute('aria-expanded', on); });
  function openDrop() {
    $('.st-dtitle').textContent = TITLES[mode] || ''; STG.classList.add('open'); markPicks();
    if (!DROP.hidden) return;
    DROP.hidden = false; DROP.classList.remove('opening'); void DROP.offsetWidth; DROP.classList.add('opening');
    requestAnimationFrame(() => { if (DROP.getBoundingClientRect().top > innerHeight * .6) DROP.scrollIntoView({block: 'start', behavior: calm() ? 'auto' : 'smooth'}); });
  }
  function closeDrop() {
    if (game && game.timer) clearInterval(game.timer);
    game = null; quiz = null; mode = null; session = [];
    ROOT.querySelectorAll('.st-modes .chip').forEach(b => b.classList.remove('on'));
    DROP.hidden = true; STG.classList.remove('open'); S.innerHTML = ''; home(); sfx('tap', .3);
  }
  $('.st-x').onclick = closeDrop;
  function openNote(c) {
    show('notes'); openView('notes-' + c.key);
    setTimeout(() => { const d = [...document.querySelectorAll('#notes-' + c.key + ' details')].find(x => x.querySelector('summary b')?.textContent === c.title);
      if (d) { d.open = true; d.scrollIntoView({behavior: 'smooth', block: 'center'}); } }, 80);
  }
  const KIND = {define: '#3b82f6', idea: '#a78bfa', table: '#f5a524', gap: '#2fb36b'};
  const dot = c => `<i class="k" style="--kc:${KIND[c.kind] || 'var(--muted)'}"></i>`;
  function meta(c, i, n) {
    const w = Math.round(100 * (i - 1) / Math.max(1, n));
    return `<div class="st-meta"><span>${c.wk ? 'Wk ' + c.wk + ' · ' : ''}${c.mod ? esc(c.mod) + ' · ' : ''}<a class="st-open">${esc(c.note)}</a></span><span><span class="st-combo"></span>${i} / ${n}</span></div>`
      + `<div class="st-prog"><i style="width:${lastProg}%" data-w="${w}"></i></div>`;
  }
  // the progress bar slides from where it was rather than jumping
  function prog() { const i = $('.st-prog i[data-w]'); if (!i) return; lastProg = +i.dataset.w; requestAnimationFrame(() => requestAnimationFrame(() => { i.style.width = lastProg + '%'; })); }
  function combo(n) { const el = $('.st-combo'); if (!el) return; el.textContent = n >= 2 ? n + ' in a row · ' : ''; if (n >= 2) bump(el); }
  const inView = sel => requestAnimationFrame(() => { const el = $(sel); if (el && el.getBoundingClientRect().bottom > innerHeight - 90) el.scrollIntoView({block: 'end', behavior: 'smooth'}); });
  function swap() { S.classList.remove('swap'); void S.offsetWidth; S.classList.add('swap'); clearTimeout(swap.t); swap.t = setTimeout(() => S.classList.remove('swap'), 450); }

  function start(m, list) {
    if (game && game.timer) clearInterval(game.timer);
    game = null; quiz = null; mode = m; at = 0; flipped = false; done = {n: 0, right: 0, xp: 0}; lastProg = 0;
    ROOT.querySelectorAll('.st-modes .chip').forEach(b => b.classList.toggle('on', b.dataset.m === (m === 'set' ? 'blurt' : m)));
    if (m === 'set') session = list || [];
    if (m === 'due') session = dueList().slice(0, 100);
    if (m === 'new') session = newList().slice(0, Math.max(5, ST.goal - today().rev));
    if (m === 'weak') session = weakList().slice(0, 15);
    if (m === 'quiz') { const p = shuffle(pool()), can = [...p.filter(c => c.kind !== 'idea'), ...p.filter(c => c.kind === 'idea')];
      quiz = {list: can.slice(0, 10), at: 0, score: 0, picked: null, combo: 0, best: 0}; }
    if (m === 'match') startMatch();
    if (m === 'type') startType();
    if (m === 'blurt') game = {note: null, text: '', t0: 0, timer: 0, result: false};
    home(); swap(); draw(); edges(true);
  }
  function finish(big, line, sub, buttons, good) {
    S.innerHTML = `<div class="st-done"><div class="st-medal${good ? ' gold' : ''}">${STAR}</div><b class="st-big">${big}</b><p>${line}</p>${sub ? `<p class="st-sub2">${sub}</p>` : ''}`
      + `<div class="st-row">${buttons.map(([t, m]) => `<button class="chip" data-go="${m}">${t}</button>`).join('')}</div></div>`;
    S.querySelectorAll('[data-go]').forEach(b => b.onclick = () => start(b.dataset.go));
    sfx('done'); if (good) confetti();
    try { pywebview.api.track('study'); } catch (_) {}
  }
  function empty(msg, buttons) {
    S.innerHTML = `<div class="st-done"><b>Nothing here</b><p>${msg}</p><div class="st-row">${buttons.map(([t, m]) => `<button class="chip" data-go="${m}">${t}</button>`).join('')}</div></div>`;
    S.querySelectorAll('[data-go]').forEach(b => b.onclick = () => start(b.dataset.go));
  }

  function draw() {
    if (!mode) { DROP.hidden = true; STG.classList.remove('open'); return home(); }
    openDrop();
    if (mode === 'all') return browse();
    if (mode === 'quiz') return drawQuiz();
    if (mode === 'match') return drawMatch();
    if (mode === 'type') return drawType();
    if (mode === 'blurt') return drawBlurt();
    const c = session[at];
    if (!c) {
      if (!done.n) return empty(mode === 'due' ? 'No cards are due right now. Next review ' + nextDue() + '.' : mode === 'weak' ? 'No weak cards. Nice.' : 'No new cards left here.',
        [['Learn new', 'new'], ['Quiz me', 'quiz'], ['Match', 'match']]);
      const pc = Math.round(100 * done.right / done.n);
      return finish(pc + '%', cheer(pc), `${done.n} cards · ${done.right} right · +${done.xp} XP · next review ${nextDue()}`,
        [['Learn new', 'new'], ['Quiz me', 'quiz'], ['Match', 'match']], pc >= 80);
    }
    const s = st(c), left = session.length - at - 1;
    S.innerHTML = meta(c, at + 1, session.length)
      + `<div class="st-deck d${Math.min(2, left)}"><div class="st-card in" tabindex="0" aria-label="Card, press Space to turn"><div class="st-flip">`
      + `<div class="st-face a"><span class="st-ask">${dot(c)}${esc(c.ask)}${s ? '' : ' · new'}</span><div class="st-q">${fmt(c.q)}</div><span class="st-hint">Space or click to turn</span></div>`
      + `<div class="st-face b"><span class="st-ask">${dot(c)}${esc(c.q.length < 80 ? c.q : c.ask)}</span><div class="st-a">${fmt(c.a)}</div></div></div></div></div><div class="st-act"></div>`;
    $('.st-open').onclick = () => openNote(c);
    $('.st-card').onclick = flip;
    fit(); acts(); prog();
  }
  // both faces are stacked, so the card takes the height of the taller one
  function fit() {
    const f = $('.st-card:not(.ghost) .st-flip'); if (!f) return;
    const h = Math.max(...[...f.querySelectorAll('.st-face')].map(x => [...x.children].reduce((a, k) => a + k.offsetHeight + 10, 44)));
    f.style.height = Math.min(480, Math.max(230, h)) + 'px';
  }
  function acts() {
    const c = session[at], A = $('.st-act'); if (!c || !A) return;
    if (flipped) {
      const o = [0, 1, 2, 3].map(g => next(c, g));
      A.innerHTML = `<div class="st-grade">${['Again', 'Hard', 'Good', 'Easy'].map((t, g) => `<button class="g${g}" data-g="${g}" style="--i:${g}">${t}<small>${when(o[g])}</small><kbd>${g + 1}</kbd></button>`).join('')}</div>`;
      A.querySelectorAll('button').forEach(b => b.onclick = () => grade(+b.dataset.g));
      inView('.st-grade');
    } else {
      A.innerHTML = '<button class="st-show">Show answer <span style="opacity:.6">(Space)</span></button>';
      $('.st-show').onclick = flip;
    }
  }
  function flip() {
    const card = $('.st-card:not(.ghost)'); if (!card || !session[at]) return;
    flipped = !flipped; card.classList.remove('in', 'lift'); void card.offsetWidth;
    card.classList.toggle('back', flipped); card.classList.add('lift');
    sfx('tap', .35); acts();
  }
  function nextDue() { const t = Math.min(...pool().filter(st).map(c => st(c).due)); if (!isFinite(t)) return 'once you learn some cards';
    const h = (t - Date.now()) / 36e5; return h < 1 ? 'within the hour' : h < 24 ? 'in ' + Math.round(h) + ' h' : 'in ' + Math.round(h / 24) + ' days'; }

  // ---- the start screen: what's waiting, the ways to practise, and a tip
  const ICON = {
    quiz: '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><path d="M14 16.5l2 2 3.5-4"/>',
    match: '<rect x="3" y="6" width="7" height="12" rx="2"/><rect x="14" y="6" width="7" height="12" rx="2"/><path d="M10 12h4"/>',
    type: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 11h1M11 11h1M15 11h1M8 14h8"/>',
    blurt: '<path d="M4 20l4-1 11-11-3-3L5 16z"/><path d="M14 6l3 3"/>',
  };
  const PICKS = [
    ['quiz', 'Quiz me', 'Ten questions, four answers each. Streaks earn bonus XP.', '#3b82f6'],
    ['match', 'Match', 'Pair six terms with their meanings against the clock.', '#f5a524'],
    ['type', 'Type it', 'See the meaning, type the term. Close spelling counts.', '#2fb36b'],
    ['blurt', 'Blurt', 'Write all you remember about a note, then see what you missed.', '#a78bfa'],
  ];
  function home() {
    const due = dueList().length, nw = newList().length,
      thisWk = NOW_WK && wk === 'all' ? CARDS.filter(c => c.cur !== false && c.wk === NOW_WK && (mod === 'all' || c.key === mod)).length : 0;
    HOME.innerHTML = `<div class="st-hero"><b>${due ? due + ' due' : nw ? 'Ready when you are' : 'All caught up'}</b>`
      + `<p>${due ? 'Cards from your notes are waiting for a review.' : nw ? nw + ' new cards to learn' + (wk !== 'all' ? ' in week ' + wk : '') + '.' : 'Nothing due. A quick game keeps it fresh.'}</p>`
      + `<div class="st-row"><button class="st-cta st-begin">${due ? 'Review now' : nw ? 'Learn new cards' : 'Quiz me'}</button>`
      + (thisWk ? `<button class="chip st-thiswk">This week's cards · ${thisWk}</button>` : '') + '</div></div>'
      + `<div class="st-picks">${PICKS.map(([m, t, d, col], i) => `<button class="st-pick" data-go="${m}" style="--i:${i};--pc:${col}" aria-expanded="false"><span class="st-pi"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[m]}</svg></span><b>${t}</b><small>${d}</small><i class="st-chev" aria-hidden="true"></i></button>`).join('')}</div>`
      + `<p class="st-tip"><b>Tip</b>${TIPS[Math.floor(Math.random() * TIPS.length)]}</p>`;
    $('.st-begin').onclick = () => start(due ? 'due' : nw ? 'new' : 'quiz');
    // a pick opens its panel below; pressing the open one again closes it
    HOME.querySelectorAll('[data-go]').forEach(b => b.onclick = () => b.classList.contains('on') ? closeDrop() : start(b.dataset.go));
    const tw = $('.st-thiswk'); if (tw) tw.onclick = () => { wk = String(NOW_WK); stats(); start(newList().length ? 'new' : 'quiz'); };
    markPicks();
    if (!home.seen) { home.seen = true; HOME.classList.add('intro'); setTimeout(() => HOME.classList.remove('intro'), 1200); }
  }

  // ---- quiz: pick the right answer out of four
  // a definition's option leaves out its own term, or the answer would give itself away
  const opt = o => { if (o.kind !== 'define') return o.a;
    const t = o.a.replace(/^\*\*[^*]+\*\*\s+(is|are|means|refers to|describes|stands for)\s+/i, ''); return t.charAt(0).toUpperCase() + t.slice(1); };
  function drawQuiz() {
    const Q = quiz, c = Q.list[Q.at];
    if (!Q.list.length) return empty('No cards to quiz on here.', [['Learn new', 'new']]);
    if (!c) { const pc = Math.round(100 * Q.score / Q.list.length);
      if (!Q.recorded) { Q.recorded = true; const t = today(); t.qbest = Math.max(t.qbest || 0, Q.score);
        if (Q.score === Q.list.length && Q.list.length >= 5) ST.flags.perfect = 1; save(); stats(); }
      return finish(`${Q.score} / ${Q.list.length}`, cheer(pc), `Best streak ${Q.best} · +${done.xp} XP`, [['Another quiz', 'quiz'], ['Match', 'match'], ['Review due', 'due']], pc >= 80); }
    if (!Q.opts) { const others = shuffle(CARDS.filter(x => x.id !== c.id && opt(x) !== opt(c))),
        rank = x => (x.kind === c.kind ? 0 : 2) + (x.key === c.key ? 0 : 1);
      Q.opts = shuffle([c, ...others.sort((x, y) => rank(x) - rank(y)).filter((x, i, a) => a.findIndex(y => opt(y) === opt(x)) === i).slice(0, 3)]); }
    S.innerHTML = meta(c, Q.at + 1, Q.list.length) + `<span class="st-ask">${dot(c)}${esc(c.ask)}</span><div class="st-q st-pop">${fmt(c.q)}</div><div class="st-opts">`
      + Q.opts.map((o, i) => `<button class="st-opt" style="--i:${i}" data-i="${i}"><kbd>${i + 1}</kbd><span class="st-a">${fmt(short(opt(o)))}</span></button>`).join('')
      + '</div><div class="st-act"></div>';
    $('.st-open').onclick = () => openNote(c);
    ROOT.querySelectorAll('.st-opt').forEach(b => b.onclick = () => pick(+b.dataset.i));
    prog(); combo(Q.combo);
  }
  function pick(i) {
    const Q = quiz, c = Q.list[Q.at]; if (!c || Q.picked !== null) return;
    Q.picked = i; const ok = Q.opts[i] === c;
    if (ok) { Q.score++; Q.combo++; Q.best = Math.max(Q.best, Q.combo); } else Q.combo = 0;
    const t = today(); t.rev++; if (ok) t.right++;
    const bs = [...ROOT.querySelectorAll('.st-opt')];
    bs.forEach((b, j) => b.classList.add(Q.opts[j] === c ? 'right' : j === i ? 'wrong' : 'dim'));
    sfx(ok ? 'open' : 'error', .6);
    if (ok) gain(10 + Math.min(10, (Q.combo - 1) * 2), bs[i]);
    combo(Q.combo); t.combo = Math.max(t.combo || 0, Q.combo);
    $('.st-act').innerHTML = '<button class="st-show st-next">Next <span style="opacity:.6">(Space)</span></button>';
    $('.st-next').onclick = nextQ; inView('.st-next');
    save(); stats();
  }
  function nextQ() { quiz.at++; quiz.picked = null; quiz.opts = null; drawQuiz(); }

  // ---- Match: six terms, six meanings, against the clock
  function startMatch() {
    const seen = new Set();
    const pairs = shuffle(pool().filter(c => (c.kind === 'define' || c.kind === 'table') && c.q.length <= 50 && opt(c).length <= 120 && !c.a.includes('\n')
        && !/\b(it|this|that|they|these|those|its)\b/i.test(c.q)))
      .filter(c => { const k = c.q.toLowerCase(), v = opt(c).toLowerCase(); if (seen.has(k) || seen.has(v)) return false; seen.add(k); seen.add(v); return true; }).slice(0, 6);
    game = {pairs, L: shuffle(pairs.slice()), R: shuffle(pairs.slice()), sel: null, left: pairs.length, miss: 0, t0: 0, timer: 0};
  }
  function drawMatch() {
    const G = game;
    if (G.pairs.length < 3) return empty('Match needs short definitions, and there are not enough here. Try All modules or All weeks.', [['Quiz me', 'quiz']]);
    const best = ST.best[mod + '|' + wk];
    const tile = (c, side, i) => `<button class="st-tile ${side}" style="--i:${i}" data-id="${c.id}" data-side="${side}">${side === 'q' ? fmt(c.q) : fmt(short(opt(c)))}</button>`;
    S.innerHTML = `<div class="st-meta"><span>Match each term to its meaning${best ? ' · best ' + best + 's' : ''}</span><span><span class="st-mt">0.0s</span> · <span class="st-mm">0 misses</span></span></div>`
      + `<div class="st-match"><div class="st-col">${G.L.map((c, i) => tile(c, 'q', i)).join('')}</div><div class="st-col">${G.R.map((c, i) => tile(c, 'a', i + 1)).join('')}</div></div>`;
    S.querySelectorAll('.st-tile').forEach(b => b.onclick = () => tapTile(b));
  }
  function tapTile(b) {
    const G = game; if (!G || b.classList.contains('ok')) return;
    if (!G.t0) { G.t0 = Date.now(); G.timer = setInterval(() => { const el = $('.st-mt'); if (el) el.textContent = ((Date.now() - G.t0) / 1000).toFixed(1) + 's'; }, 100); }
    if (!G.sel || G.sel.dataset.side === b.dataset.side) {
      if (G.sel) G.sel.classList.remove('sel');
      G.sel = G.sel === b ? null : b; if (G.sel) b.classList.add('sel');
      sfx('tap', .3); return;
    }
    const a = G.sel; G.sel = null; a.classList.remove('sel');
    if (a.dataset.id === b.dataset.id) {
      [a, b].forEach(x => x.classList.add('ok'));
      setTimeout(() => [a, b].forEach(x => x.classList.add('gone')), calm() ? 0 : 380);
      G.left--; sfx('open', .5); gain(5, b);
      const t = today(); t.rev++; t.right++; save(); stats();
      if (!G.left) {
        today().matches = (today().matches || 0) + 1;
        clearInterval(G.timer); const secs = Math.round((Date.now() - G.t0) / 100) / 10, key = mod + '|' + wk, best = ST.best[key], nb = !best || secs < best;
        if (nb) ST.best[key] = secs; save();
        setTimeout(() => finish(secs.toFixed(1) + 's', nb && best ? 'New best time!' : nb ? 'Your first time. Beat it next go.' : `Your best is ${best}s. So close.`,
          `${G.miss} ${G.miss === 1 ? 'miss' : 'misses'} · +${done.xp} XP`, [['Play again', 'match'], ['Type it', 'type'], ['Quiz me', 'quiz']], nb), calm() ? 0 : 550);
      }
    } else {
      [a, b].forEach(x => { x.classList.remove('bad'); void x.offsetWidth; x.classList.add('bad'); });
      G.miss++; $('.st-mm').textContent = G.miss + (G.miss === 1 ? ' miss' : ' misses'); sfx('error', .4);
    }
  }

  // ---- Type it: see the meaning (or a sentence with a gap), type the term
  const tPrompt = c => c.kind === 'gap' ? c.q : opt(c);
  const tAns = c => (c.kind === 'gap' ? c.a : c.q).replace(/\*\*/g, '');
  const norm = s => s.toLowerCase().replace(/\([^)]*\)/g, ' ').replace(/[^a-z0-9]+/g, ' ').trim();
  function lev(a, b) {
    const d = [...Array(b.length + 1).keys()];
    for (let i = 1; i <= a.length; i++) { let p = d[0]; d[0] = i;
      for (let j = 1; j <= b.length; j++) { const t = d[j]; d[j] = Math.min(d[j] + 1, d[j - 1] + 1, p + (a[i - 1] === b[j - 1] ? 0 : 1)); p = t; } }
    return d[b.length];
  }
  // how close the typed answer is, 0 to 1; "A / B", "A or B" and "(B)" each count as answers
  function closeness(typed, ans) {
    const t = norm(typed); if (!t) return 0;
    const alts = [ans, ...ans.split(/\s*[\/,]\s*|\s+or\s+/), ...(ans.match(/\(([^)]+)\)/g) || []).map(x => x.slice(1, -1))].map(norm).filter(Boolean);
    return Math.max(...alts.map(o => 1 - lev(t, o) / Math.max(t.length, o.length)));
  }
  function startType() {
    const can = shuffle(pool().filter(c => c.kind === 'gap' || (c.kind === 'define' && c.q.length <= 40)
      || (c.kind === 'table' && c.q.length <= 32 && !c.q.includes('?') && c.q.split(' ').length <= 4 && !c.a.includes('\n'))));
    game = {list: can.slice(0, 10), at: 0, score: 0, combo: 0, best: 0, state: 'ask', hint: false};
  }
  function drawType() {
    const G = game, c = G.list[G.at];
    if (!G.list.length) return empty('No short answers to type here. Try All modules or All weeks.', [['Quiz me', 'quiz']]);
    if (!c) { const pc = Math.round(100 * G.score / G.list.length);
      return finish(`${G.score} / ${G.list.length}`, cheer(pc), `Best streak ${G.best} · +${done.xp} XP`, [['Again', 'type'], ['Match', 'match'], ['Quiz me', 'quiz']], pc >= 80); }
    S.innerHTML = meta(c, G.at + 1, G.list.length) + `<span class="st-ask">${dot(c)}${c.kind === 'gap' ? 'Fill the gap' : 'Name it'}</span><div class="st-q st-pop">${fmt(tPrompt(c))}</div>`
      + '<div class="st-typebox"><input class="st-in" autocomplete="off" spellcheck="false" placeholder="Type your answer, then Enter" aria-label="Your answer">'
      + '<button class="chip st-hintb">Hint</button><button class="chip st-skip">Show me</button></div><div class="st-fb" aria-live="polite"></div><div class="st-act"></div>';
    $('.st-open').onclick = () => openNote(c);
    const inp = $('.st-in'); inp.focus({preventScroll: true});
    inp.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); check(false); } };
    $('.st-hintb').onclick = () => { G.hint = true;
      $('.st-fb').innerHTML = `<span class="st-hintt">${esc(tAns(c).split(' ').map(w => w.charAt(0) + w.slice(1).replace(/[A-Za-z0-9]/g, '·')).join('  '))}</span>`; inp.focus(); };
    $('.st-skip').onclick = () => check(true);
    prog(); combo(G.combo);
  }
  function check(giveUp) {
    const G = game, c = G && G.list[G.at]; if (!c || G.state !== 'ask') return;
    const inp = $('.st-in');
    if (!giveUp && !inp.value.trim()) { inp.classList.remove('wrong'); void inp.offsetWidth; inp.classList.add('wrong'); return; }
    G.state = 'shown';
    const k = giveUp ? 0 : closeness(inp.value, tAns(c)), ok = k >= .75;
    inp.readOnly = true; inp.classList.add(ok ? 'right' : 'wrong');
    if (ok) { G.score++; G.combo++; G.best = Math.max(G.best, G.combo); } else G.combo = 0;
    ST.cards[c.id] = Object.assign(next(c, ok ? (G.hint ? 1 : 2) : 0), {seen: Date.now()});
    const t = today(); t.rev++; if (ok) { t.right++; t.typed = (t.typed || 0) + 1; } t.combo = Math.max(t.combo || 0, G.combo);
    $('.st-fb').innerHTML = ok ? (k > .99 ? '<b class="ok">Spot on.</b>' : `<b class="ok">Close enough.</b> It's spelt <b>${esc(tAns(c))}</b>.`)
      : `<b class="no">${giveUp ? 'The answer:' : 'Not quite.'}</b> <b>${esc(tAns(c))}</b>`;
    sfx(ok ? 'open' : 'error', .6);
    if (ok) gain((G.hint ? 6 : 12) + Math.min(10, (G.combo - 1) * 2), inp);
    combo(G.combo);
    $('.st-act').innerHTML = '<button class="st-show st-next">Next <span style="opacity:.6">(Enter)</span></button>';
    const nb = $('.st-next'); nb.onclick = () => { G.at++; G.state = 'ask'; G.hint = false; drawType(); };
    nb.focus({preventScroll: true}); inView('.st-next');
    save(); stats();
  }

  // ---- Blurt: write everything you remember about one note, then see which key points you got
  const STOP = new Set(('the and for with that this from into are was were has have its their about which when what your you can will not but also more than then them they these '
    + 'those each such only other some most very used uses using use how why who where between there here been being does done make made like just over under after before because '
    + 'while both all any one two way ways part parts type types').split(' '));
  const words = s => norm(s).split(' ').filter(w => w.length >= 3 && !STOP.has(w));
  const stem = w => w.slice(0, 5);
  const notesIn = () => { const m = new Map();
    pool().forEach(c => { const k = c.key + '|' + c.title; if (!m.has(k)) m.set(k, {k, note: c.note, mod: c.mod, wk: c.wk, cards: []}); m.get(k).cards.push(c); });
    return [...m.values()].filter(n => n.cards.length >= 3); };
  function drawBlurt() {
    const G = game;
    if (!G.note) {
      const ns = notesIn().sort((a, b) => (ST.blurts[a.k]?.at || 0) - (ST.blurts[b.k]?.at || 0) || (b.wk || 0) - (a.wk || 0));
      if (!ns.length) return empty('Blurt needs a note with a few key points. Try All modules or All weeks.', [['Quiz me', 'quiz']]);
      S.innerHTML = '<div class="st-intro"><b>Blurt it out</b><p>Pick a note. Write everything you remember about it, without looking. Then see which key points you got and which you missed.</p></div>'
        + `<div class="st-notes">${ns.slice(0, 24).map((n, i) => { const b = ST.blurts[n.k];
          return `<button class="st-note" style="--i:${i}" data-k="${esc(n.k)}"><span>${esc(n.note)}</span><small>${n.wk ? 'Wk ' + n.wk + ' · ' : ''}${esc(n.mod)} · ${n.cards.length} points${b ? ' · last ' + b.pc + '%' : ''}</small></button>`; }).join('')}</div>`;
      S.querySelectorAll('.st-note').forEach(b => b.onclick = () => { G.note = ns.find(n => n.k === b.dataset.k); G.t0 = Date.now(); G.text = ''; swap(); drawBlurt(); });
      return;
    }
    if (G.result) return blurtResult();
    S.innerHTML = `<div class="st-meta"><span>${G.note.wk ? 'Wk ' + G.note.wk + ' · ' : ''}${esc(G.note.mod)} · ${esc(G.note.note)}</span><span class="st-bt">0:00</span></div><div class="st-prog"><i class="st-bprog"></i></div>`
      + `<textarea class="st-blurt" placeholder="Everything you remember about ${esc(G.note.note)}: terms, ideas, how things connect…" aria-label="What you remember"></textarea>`
      + '<div class="st-row"><button class="st-show st-check">Compare with my note</button><button class="chip st-back">Pick another</button><small class="st-wc">0 words</small></div>';
    const ta = $('.st-blurt'); ta.value = G.text; ta.focus();
    const wc = () => { $('.st-wc').textContent = ta.value.trim().split(/\s+/).filter(Boolean).length + ' words'; };
    ta.oninput = () => { G.text = ta.value; wc(); }; wc();
    clearInterval(G.timer);
    G.timer = setInterval(() => { const el = $('.st-bt'); if (!el) return clearInterval(G.timer);
      const s = (Date.now() - G.t0) / 1000; el.textContent = Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');
      $('.st-bprog').style.width = Math.min(100, s / 1.8) + '%'; }, 500);        // three minutes fills the bar
    $('.st-check').onclick = () => { if (!G.text.trim()) { ta.classList.remove('wrong'); void ta.offsetWidth; ta.classList.add('wrong'); return; } clearInterval(G.timer); G.result = true; swap(); drawBlurt(); };
    $('.st-back').onclick = () => start('blurt');
  }
  function blurtResult() {
    const G = game, mine = new Set(words(G.text).map(stem)), seen = new Set();
    const pts = G.note.cards.map(c => { const key = (c.kind === 'gap' ? c.a : c.q).replace(/\*\*/g, ''), ws = [...new Set(words(key))];
      return {c, key, got: ws.length > 0 && ws.filter(w => mine.has(stem(w))).length / ws.length >= .6}; })
      .filter(p => { const k = p.key.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; });
    const got = pts.filter(p => p.got).length, pc = Math.round(100 * got / pts.length), missed = pts.filter(p => !p.got).map(p => p.c);
    const prev = ST.blurts[G.note.k]; ST.blurts[G.note.k] = {at: Date.now(), pc}; today().blurts = (today().blurts || 0) + 1; save();
    S.innerHTML = `<div class="st-bres"><div class="st-ring"><svg viewBox="0 0 96 96"><circle class="bg" cx="48" cy="48" r="40"/><circle class="fg" cx="48" cy="48" r="40" stroke-dasharray="251.3" stroke-dashoffset="251.3"/></svg>`
      + `<div class="in"><b class="st-bpc" data-v="0">0%</b><small>recalled</small></div></div>`
      + `<div><b class="st-btitle">${got} of ${pts.length} key points</b><p>${cheer(pc)}${prev ? ` Last time ${prev.pc}%${pc > prev.pc ? ', so that is up ' + (pc - prev.pc) + '.' : '.'}` : ''}</p></div></div>`
      + `<div class="st-pts">${pts.map((p, i) => `<div class="st-pt ${p.got ? 'got' : 'miss'}" style="--i:${i}"><i></i><span>${esc(p.key)}</span></div>`).join('')}</div>`
      + `<div class="st-row">${missed.length ? `<button class="st-show st-rmiss">Review the ${missed.length} I missed</button>` : ''}<button class="chip st-retry">Try this one again</button><button class="chip st-again">Blurt another</button></div>`;
    requestAnimationFrame(() => requestAnimationFrame(() => { $('.st-bres .fg').style.strokeDashoffset = 251.3 * (1 - pc / 100); setNum($('.st-bpc'), pc, '%'); }));
    const rm = $('.st-rmiss'); if (rm) rm.onclick = () => start('set', shuffle(missed));
    $('.st-retry').onclick = () => { G.result = false; G.text = ''; G.t0 = Date.now(); swap(); drawBlurt(); };
    $('.st-again').onclick = () => start('blurt');
    sfx('done'); gain(10 + 4 * got, $('.st-btitle')); if (pc >= 80) confetti();
    stats(); try { pywebview.api.track('study'); } catch (_) {}
  }

  // ---- every card, searchable, by week
  function browse() {
    S.innerHTML = '<input class="st-search" placeholder="Search your cards" aria-label="Search cards"><div class="st-list"></div>';
    const L = $('.st-list'), inp = $('.st-search');
    const paint = () => { const q = inp.value.trim().toLowerCase();
      const list = pool().filter(c => !q || (c.q + ' ' + c.a + ' ' + c.note).toLowerCase().includes(q)).sort((a, b) => (a.wk || 99) - (b.wk || 99)).slice(0, 300);
      let last = null;
      L.innerHTML = list.map(c => { const s = st(c), lvl = !s ? 'new' : s.ivl >= 21 ? 'learnt' : s.lapses > 1 ? 'weak' : 'learning';
        let h = '';
        if ((c.wk || 0) !== last) { last = c.wk || 0; const n = list.filter(x => (x.wk || 0) === last).length;
          h = `<h4 class="st-wh">${last ? 'Week ' + last : 'No week'}<small>${n} card${n === 1 ? '' : 's'}${last && last === NOW_WK ? ' · this week' : ''}</small></h4>`; }
        return h + `<details><summary>${dot(c)}${fmt(c.q.length > 140 ? c.q.slice(0, 137) + '…' : c.q)} <small>${esc(c.ask)} · <span class="lv ${lvl}">${lvl}</span>${s ? ' · due ' + (s.due <= Date.now() ? 'now' : new Date(s.due).toLocaleDateString(undefined, {day: 'numeric', month: 'short'})) : ''}</small></summary><div class="st-a">${fmt(c.a)}</div><div class="st-meta" style="margin-top:6px"><span>${esc(c.mod)} · ${esc(c.note)}</span></div></details>`; }).join('')
        || '<p class="sub">No cards match.</p>'; };
    inp.oninput = paint; paint();
  }

  // ---- weeks: one chip per teaching week, with how much of it you've studied
  const W = $('.st-weeks');
  function weeks() {
    const base = CARDS.filter(c => mod === 'all' || c.key === mod), per = new Map();
    base.forEach(c => { const w = c.wk || 0, r = per.get(w) || {n: 0, seen: 0, learnt: 0}; r.n++; if (st(c)) r.seen++; if (st(c) && st(c).ivl >= 21) r.learnt++; per.set(w, r); });
    const ws = [...per.keys()].sort((a, b) => (a || 99) - (b || 99));
    if (wk !== 'all' && !per.has(+wk)) wk = 'all';
    W.parentElement.hidden = ws.length < 2 && !ws[0];
    W.innerHTML = `<button class="wk${wk === 'all' ? ' on' : ''}" data-w="all"><b>All weeks</b><small>${base.length} cards</small></button>`
      + ws.map(w => { const r = per.get(w), p = Math.round(100 * r.seen / r.n), star = r.learnt / r.n >= .8;
        return `<button class="wk${String(w) === String(wk) ? ' on' : ''}${w && w === NOW_WK ? ' now' : ''}" data-w="${w}" title="${w ? 'Teaching week ' + w : 'Notes without a week'}: ${r.n} cards, ${r.seen} studied, ${r.learnt} learnt">`
          + `<b>${w ? 'Wk ' + w : 'Other'}${star ? '<em>★</em>' : ''}</b><small>${w && w === NOW_WK ? 'this week · ' : ''}${p}% done</small><i style="width:${p}%"></i></button>`; }).join('');
    W.querySelectorAll('button').forEach(b => b.onclick = () => { wk = b.dataset.w; sfx('tap', .3); stats(); if (mode) start(mode === 'set' ? 'due' : mode); else { swap(); draw(); } });
  }

  // ---- rewards: three daily challenges, badges, and card styles that unlock as you level up
  let booted = false;                  // things already earned before the page opened are added quietly
  const QUESTS = {
    rev: t => [`Review ${Math.min(ST.goal, 20)} cards`, t.rev || 0, Math.min(ST.goal, 20)],
    quiz: t => ['Score 8 or more in a quiz', t.qbest || 0, 8],
    match: t => ['Finish a Match', t.matches || 0, 1],
    type: t => ['Get 5 right in Type it', t.typed || 0, 5],
    blurt: t => ['Blurt a note', t.blurts || 0, 1],
    focus: t => ['Finish a focus round', t.rounds || 0, 1],
    combo: t => ['Get 5 right in a row', t.combo || 0, 5],
  };
  function todaysQuests() {           // the same three all day, a different mix each day
    let h = 7; for (const ch of dayKey()) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const rest = Object.keys(QUESTS).filter(k => k !== 'rev'), pick = ['rev'];
    while (pick.length < 3) { h = (h * 1103515245 + 12345) >>> 0; const k = rest[(h >>> 16) % rest.length]; if (!pick.includes(k)) pick.push(k); }
    return pick;
  }
  function quests() {
    const t = today(), ids = todaysQuests(); t.qdone ||= [];
    $('.st-qlist').innerHTML = ids.map(id => {
      const [label, v, n] = QUESTS[id](t), ok = v >= n, just = ok && !t.qdone.includes(id);
      if (just) { t.qdone.push(id); gain(25); save();
        if (booted) setTimeout(() => { toast('Challenge done: ' + label + ' · +25 XP'); sfx('open', .6); }, 400); }
      return `<div class="st-quest${ok ? ' done' : ''}${just && booted ? ' just' : ''}"><i class="ck"></i><span>${esc(label)}</span><small>${Math.min(v, n)} / ${n}</small>`
        + `<div class="st-qbar"><i style="width:${Math.round(100 * Math.min(1, v / n))}%"></i></div></div>`; }).join('');
    if (!t.qall && ids.every(id => t.qdone.includes(id))) { t.qall = 1; gain(50); save();
      if (booted) setTimeout(() => { toast('All three challenges done! +50 XP bonus'); confetti(); sfx('done'); }, 1100); }
    $('.st-qbonus').textContent = t.qall ? 'All done today' : '+25 XP each · +50 for all three';
    $('.st-qbonus').classList.toggle('all', !!t.qall);
  }
  function streak() {
    const t = today(); let n = 0; const d = new Date(); if (!(t.rev || t.mins)) d.setDate(d.getDate() - 1);
    for (;;) { const r = ST.days[dayKey(d)]; if (r && (r.rev || r.mins)) { n++; d.setDate(d.getDate() - 1); } else break; }
    return n;
  }
  const weekStar = () => { const per = new Map();
    CARDS.forEach(c => { if (!c.wk) return; const k = c.key + '|' + c.wk, r = per.get(k) || {n: 0, l: 0}; r.n++; if (st(c) && st(c).ivl >= 21) r.l++; per.set(k, r); });
    return [...per.values()].some(r => r.n >= 5 && r.l / r.n >= .8); };
  const BADGES = [
    ['first', '1', 'First card', 'Review your first card', () => Object.keys(ST.cards).length >= 1],
    ['c100', '100', 'Century', 'Study 100 different cards', () => Object.keys(ST.cards).length >= 100],
    ['learnt', '25', 'Locked in', 'Learn 25 cards for good (a 3-week gap)', () => CARDS.filter(c => st(c) && st(c).ivl >= 21).length >= 25],
    ['s3', '3d', 'On a roll', 'Study 3 days in a row', () => streak() >= 3],
    ['s7', '7d', 'Week warrior', 'Study 7 days in a row', () => streak() >= 7],
    ['goal7', '7×', 'Goal getter', 'Hit your daily goal on 7 days', () => Object.values(ST.days).filter(d => (d.rev || 0) >= ST.goal).length >= 7],
    ['perfect', '10', 'Perfect', 'Get every question right in a quiz', () => !!ST.flags.perfect],
    ['fast', '20s', 'Quick match', 'Finish a Match in under 20 seconds', () => Object.values(ST.best).some(t => t < 20)],
    ['blurt', '80%', 'Total recall', 'Score 80% or more in a Blurt', () => Object.values(ST.blurts).some(b => b.pc >= 80)],
    ['focus', '4×', 'Deep focus', 'Finish 4 focus rounds in one day', () => Object.values(ST.days).some(d => (d.rounds || 0) >= 4)],
    ['star', '★', 'Week mastered', "Learn 80% of a week's cards", weekStar],
    ['lv6', 'L6', 'Scholar', 'Reach level 6', () => lvlOf(ST.xp).l >= 5],
  ];
  function badges() {
    const fresh = BADGES.filter(([id, , , , ok]) => !ST.badges[id] && ok()).map(b => b[0]);
    fresh.forEach((id, i) => { ST.badges[id] = Date.now(); const name = BADGES.find(b => b[0] === id)[2];
      if (booted) setTimeout(() => { toast('Badge unlocked: ' + name); confetti(); sfx('done'); }, 800 + i * 1000); });
    if (fresh.length) save();
    $('.st-bgrid').innerHTML = BADGES.map(([id, g, name, how]) => { const got = ST.badges[id];
      return `<span class="st-badge${got ? ' got' : ''}${booted && fresh.includes(id) ? ' just' : ''}" title="${esc(name)}: ${esc(how)}${got ? '. Earned ' + new Date(got).toLocaleDateString(undefined, {day: 'numeric', month: 'short'}) : ''}">`
        + `<b>${g}</b><small>${esc(name)}</small></span>`; }).join('');
    $('.st-bcount').textContent = Object.keys(ST.badges).length + ' / ' + BADGES.length;
  }
  const STYLES = [['plain', 'Plain', 1], ['ocean', 'Ocean', 3], ['sunset', 'Sunset', 5], ['aurora', 'Aurora', 8]];
  function styles() {
    const lv = lvlOf(ST.xp).l + 1;
    if (!STYLES.some(([k, , need]) => k === ST.style && lv >= need)) ST.style = 'plain';
    $('.st').dataset.cs = ST.style;
    $('.st-cs').innerHTML = STYLES.map(([k, name, need]) => `<button class="chip${ST.style === k ? ' on' : ''}" data-cs="${k}"${lv < need ? ' disabled' : ''} title="${lv < need ? 'Unlocks at level ' + need : name + ' cards'}"><i class="csw ${k}"></i>${name}${lv < need ? ' · Lv ' + need : ''}</button>`).join('');
    $('.st-cs').querySelectorAll('button:not([disabled])').forEach(b => b.onclick = () => { ST.style = b.dataset.cs; save(); styles(); sfx('tap', .4); });
    const nx = STYLES.find(([, , need]) => need > lv);
    $('.st-next-un').textContent = nx ? `Next: ${nx[1]} cards at level ${nx[2]}` : 'Every card style unlocked';
  }

  // ---- numbers at the top, the level bar, and the last 14 days
  let prevRev = null;
  function stats() {
    quests(); badges();
    const t = today(), goal = ST.goal, ring = $('.st-stats .st-ring');
    setNum($('.st-goal-n'), t.rev); $('.st-goal-of').textContent = 'of ' + goal;
    $('.st-stats .st-ring .fg').style.strokeDashoffset = 251.3 * (1 - Math.min(1, t.rev / goal));
    ring.classList.toggle('met', t.rev >= goal);
    if (prevRev !== null && prevRev < goal && t.rev >= goal) {
      ring.classList.remove('win'); void ring.offsetWidth; ring.classList.add('win');
      setTimeout(() => { toast('Daily goal done. Nice work!'); confetti(); sfx('done'); }, 250);
    }
    prevRev = t.rev;
    setNum($('.st-due'), dueList().length); setNum($('.st-new'), newList().length);
    $('.st-acc').textContent = t.rev ? Math.round(100 * t.right / t.rev) + '%' : '–';
    setNum($('.st-mins'), Math.round(t.mins || 0));
    setNum($('.st-known'), CARDS.filter(c => st(c) && st(c).ivl >= 21).length);
    const n = streak();
    setNum($('.st-streak'), n); $('.st-streak').classList.toggle('hot', n >= 3);
    const L = lvlOf(ST.xp);
    $('.st-lv').textContent = 'Level ' + (L.l + 1); $('.st-lvname').textContent = LEVELS[Math.min(L.l, LEVELS.length - 1)];
    $('.st-xp').textContent = L.into + ' / ' + L.need + ' XP'; $('.st-lvbar i').style.width = (100 * L.into / L.need) + '%';
    styles();
    $('.n-due').textContent = dueList().length || ''; $('.n-new').textContent = newList().length || '';
    $('.n-weak').textContent = weakList().length || ''; $('.n-all').textContent = pool().length;
    $('.st-goal-v').textContent = goal;
    const r = Math.min(8, t.rounds || 0);
    $('.st-rounds').innerHTML = [...Array(Math.max(4, r))].map((_, i) => `<i class="${i < r ? 'on' : ''}"></i>`).join('');
    const days = [...Array(14)].map((_, i) => { const x = new Date(); x.setDate(x.getDate() - 13 + i); return x; });
    const top = Math.max(goal, ...days.map(x => (ST.days[dayKey(x)] || {}).rev || 0));
    $('.st-bars').innerHTML = days.map((x, i) => { const r = ST.days[dayKey(x)] || {};
      return `<div class="${i === 13 ? 'today' : ''}${(r.rev || 0) >= goal ? ' hit' : ''}" style="--i:${i}" title="${x.toLocaleDateString(undefined, {weekday: 'short', day: 'numeric', month: 'short'})}: ${r.rev || 0} cards, ${Math.round(r.mins || 0)} focus min, ${r.xp || 0} XP"><i style="height:${Math.round(70 * (r.rev || 0) / top)}px"></i>${x.toLocaleDateString(undefined, {weekday: 'narrow'})}</div>`; }).join('');
    const tab = document.querySelector('nav button[data-tab="study"]');
    if (tab) tab.classList.toggle('has-due', dueList().length > 0);
    weeks(); edges(true);
    if (ROOT.offsetParent && !stats.grown) { stats.grown = true; setTimeout(() => $('.st-bars').classList.add('seen'), 900); }   // the bars grow in once, not on every card
    if (!mode) home();
  }
  $('.st-gm').onclick = () => { ST.goal = Math.max(5, ST.goal - 5); prevRev = null; save(); stats(); };
  $('.st-gp').onclick = () => { ST.goal = Math.min(200, ST.goal + 5); prevRev = null; save(); stats(); };

  // ---- modules
  const M = $('.st-mods');
  M.innerHTML = '<button class="chip on" data-k="all">All modules</button>' + D.modules.map(m => `<button class="chip" data-k="${m.key}">${esc(m.name)}</button>`).join('');
  M.querySelectorAll('.chip').forEach(b => b.onclick = () => { mod = b.dataset.k; M.querySelectorAll('.chip').forEach(x => x.classList.toggle('on', x === b)); sfx('tap', .3); stats();
    if (mode) start(mode === 'set' ? 'due' : mode); else { swap(); draw(); } });
  ROOT.querySelectorAll('.st-modes .chip').forEach(b => b.onclick = () => start(b.dataset.m));

  // ---- filter rows scroll sideways: arrows, the mouse wheel, and the chosen chip kept in view
  const lines = [...ROOT.querySelectorAll('.st-line')].filter(L => L.querySelector('.st-scroll'));
  function edge(L) {
    const sc = L.querySelector('.st-scroll'), max = sc.scrollWidth - sc.clientWidth;
    L.classList.toggle('l', sc.scrollLeft > 2); L.classList.toggle('r', sc.scrollLeft < max - 2);
  }
  function edges(center) {
    lines.forEach(L => { const sc = L.querySelector('.st-scroll'), on = center && sc.querySelector('.on');
      if (on && (on.offsetLeft < sc.scrollLeft || on.offsetLeft + on.offsetWidth > sc.scrollLeft + sc.clientWidth))
        sc.scrollTo({left: on.offsetLeft - 40, behavior: calm() ? 'auto' : 'smooth'});
      edge(L); });
  }
  lines.forEach(L => {
    const sc = L.querySelector('.st-scroll');
    L.insertAdjacentHTML('beforeend', '<button class="st-arr l" aria-label="Scroll left" tabindex="-1">‹</button><button class="st-arr r" aria-label="Scroll right" tabindex="-1">›</button>');
    L.querySelector('.st-arr.l').onclick = () => sc.scrollBy({left: -sc.clientWidth * .7, behavior: 'smooth'});
    L.querySelector('.st-arr.r').onclick = () => sc.scrollBy({left: sc.clientWidth * .7, behavior: 'smooth'});
    sc.addEventListener('scroll', () => edge(L), {passive: true});
    // the wheel scrolls the row sideways, and the page again once the row reaches its end
    sc.addEventListener('wheel', e => { const max = sc.scrollWidth - sc.clientWidth;
      if (max < 2 || Math.abs(e.deltaX) > Math.abs(e.deltaY) || (e.deltaY > 0 && sc.scrollLeft >= max - 1) || (e.deltaY < 0 && sc.scrollLeft <= 0)) return;
      sc.scrollLeft += e.deltaY; e.preventDefault(); }, {passive: false});
    new ResizeObserver(() => edge(L)).observe(sc);     // also catches the tab first being shown
  });

  // ---- keys: Space turns / next, 1-4 grade or pick
  // capture phase, so 1-4 on a card grade it instead of switching tabs
  document.addEventListener('keydown', e => {
    if (!ROOT.closest('section')?.classList.contains('on') || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Escape' && mode && !document.querySelector('.viewer:not([hidden])') && !(e.target.matches('textarea') && e.target.value.trim())) { closeDrop(); e.preventDefault(); e.stopImmediatePropagation(); return; }
    if (e.target.closest('input,textarea,[contenteditable]')) return;
    const num = /^[1-4]$/.test(e.key), stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
    if (mode === 'quiz' && quiz) {
      if (num) { if (quiz.picked === null) pick(+e.key - 1); stop(); }
      else if ((e.key === ' ' || e.key === 'Enter') && quiz.picked !== null) { nextQ(); stop(); }
      return;
    }
    if (!CARD_MODES.includes(mode) || !session[at]) return;
    if (e.key === ' ' || e.key === 'Enter') { flip(); stop(); }
    else if (num) { if (flipped) grade(+e.key - 1); stop(); }
  }, true);

  // ---- focus timer (Pomodoro): focus, then a short break; focus minutes count toward today
  let len = {w: 25, b: 5}, phase = 'w', left = 25 * 60, run = 0, last = 0, title0 = document.title;
  const T = $('.st-time'), BAR = $('.st-fbar i'), GO = $('.st-go'), FM = $('.st-fmode'), MSG = $('.st-fmsg'), FX = $('.st-fexp'), FB = $('.st-focus');
  const mmss = s => String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(Math.floor(s % 60)).padStart(2, '0');
  const explain = () => { FX.textContent = phase === 'w' ? `Focus for ${len.w} min, then take a ${len.b} min break. Focus minutes count toward today.`
    : 'Break time. Stand up, drink some water, look away from the screen.'; };
  const paintT = () => { T.textContent = mmss(left); BAR.style.width = (100 * (1 - left / (len[phase] * 60))) + '%';
    FM.textContent = phase === 'w' ? 'Focus' : 'Break'; FB.classList.toggle('running', !!run); FB.classList.toggle('brk', phase === 'b');
    document.title = run ? `${mmss(left)} · ${phase === 'w' ? 'Focus' : 'Break'}` : title0; };
  function tick() {
    const now = Date.now(), dt = (now - last) / 1000; last = now; left -= dt;
    if (phase === 'w') { today().mins = (today().mins || 0) + dt / 60; }
    if (left <= 0) {
      sfx('done');
      if (phase === 'w') { try { pywebview.api.track('study'); } catch (_) {} today().rounds = (today().rounds || 0) + 1; gain(15, T);
        MSG.textContent = ''; toast('Focus round done. Take a break!'); phase = 'b'; }
      else { toast('Break over. Back to it.'); phase = 'w'; }
      left = len[phase] * 60; explain(); save(); stats();
    }
    paintT(); if (Math.round(left) % 30 === 0) { save(); stats(); }
  }
  GO.onclick = () => { if (run) { clearInterval(run); run = 0; GO.textContent = 'Start'; save(); stats(); }
    else { last = Date.now(); run = setInterval(tick, 1000); GO.textContent = 'Pause'; MSG.textContent = ''; } sfx('tap', .4); paintT(); };
  $('.st-reset').onclick = () => { clearInterval(run); run = 0; phase = 'w'; left = len.w * 60; GO.textContent = 'Start'; MSG.textContent = ''; explain(); paintT(); stats(); };
  ROOT.querySelectorAll('.st-len').forEach(b => b.onclick = () => { len = {w: +b.dataset.w, b: +b.dataset.b};
    ROOT.querySelectorAll('.st-len').forEach(x => x.classList.toggle('on', x === b)); if (!run) { phase = 'w'; left = len.w * 60; } explain(); paintT(); });

  explain(); stats(); draw(); paintT(); booted = true;
  window.bbStudy = {ST, CARDS, start, closeness};
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
  <button class="set-tab" data-s="share" role="tab" aria-selected="false">Share</button>
 </nav>

 <div class="set-pane on" data-s="general">
  <span class="label">Activity</span><div class="heat"></div>
  <label class="row">Check Blackboard in the background
   <select class="s-sync"><option value="0">Off</option><option value="1">Every hour</option><option value="2">Every 2 hours</option><option value="6">Every 6 hours</option><option value="12">Every 12 hours</option><option value="24">Once a day</option><option value="48">Every 2 days</option><option value="72">Every 3 days</option><option value="168">Once a week</option></select></label>
  <label class="row">New notes per sync <span><input class="s-notes" type="range" min="0" max="15" step="1"> <b class="s-notes-v"></b></span></label>
  <div class="row col"><span class="s-files-where">Study files are saved in <b class="s-files-path"></b></span>
   <span><button class="ghost s-files">Open folder</button> <button class="ghost s-files-move">Choose folder…</button></span>
   <small class="s-files-msg"></small></div>
  <span class="label">Profile card</span>
  <div class="pcard"><div class="pc-ban"></div><div class="pc-ring"></div><div class="pc-av"></div>
   <div class="pc-txt"><b class="pc-name"></b><span class="pc-tag"></span><span class="pc-meta"></span><div class="pc-badges"></div></div>
   <button class="pc-open" type="button">Customize</button></div>
  <div class="pc-edit" hidden>
   <label class="pc-f pc-f-name"><span>Name</span><input class="pc-in-name" maxlength="30" spellcheck="false" placeholder="What should the app call you?"></label>
   <div class="pc-two"><label class="pc-f"><span>Avatar</span><input class="pc-in-av" maxlength="4" spellcheck="false" placeholder="e.g. M"></label>
    <label class="pc-f"><span>Status</span><input class="pc-in-tag" maxlength="60" placeholder="Locked in for exams"></label></div>
   <div class="pc-row"><button class="pc-chip pc-up-av" type="button">Upload a picture</button><button class="pc-chip pc-rm-av" type="button">Remove picture</button></div>
   <label class="pc-f"><span>Course or role</span><input class="pc-in-course" maxlength="60" placeholder="Computer Science, year 2"></label>
   <label class="pc-f"><span>Badges</span><input class="pc-in-badges" maxlength="90" spellcheck="false" placeholder="Up to 4, with commas: SDGP, Year 2, Python"></label>
   <span class="pc-l">Banner</span><div class="pc-banners pc-row"></div>
   <div class="pc-row pc-own"><span class="pc-mini">Your own</span><input type="color" class="pc-g1" aria-label="Banner colour 1"><input type="color" class="pc-g2" aria-label="Banner colour 2">
    <button class="pc-chip pc-up-ban" type="button">Upload an image</button><button class="pc-chip pc-rm-ban" type="button">Remove image</button></div>
   <span class="pc-l">Pattern</span><div class="pc-patterns pc-row"></div>
   <span class="pc-l">Colour</span><div class="pc-accents pc-row"></div>
   <span class="pc-l">Name font</span><div class="pc-fonts pc-row"></div>
   <span class="pc-l">Avatar shape</span><div class="pc-shapes pc-row"></div>
   <span class="pc-l">Effect</span><div class="pc-effects pc-row"></div>
   <input type="file" class="pc-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
   <button class="pc-save" type="button">Save card</button>
  </div>
  <small class="s-name-msg"></small>
  <div class="row col"><span>Backups of your notebook, boards and settings are made every 15 minutes, kept outside the app folder</span>
   <span><button class="ghost s-bk-open">Open backups</button> <button class="ghost s-bk-now">Back up now</button> <button class="ghost s-bk-load">Load a backup…</button></span>
   <small class="s-bk-msg"></small></div>
  <div class="row"><span class="s-ver"></span><span><small class="s-check-msg"></small> <button class="ghost s-check">Check for updates</button></span></div>
  <div class="row col"><button class="ghost danger s-signout">Sign out of Blackboard</button><small class="s-signout-msg"></small></div>
  <div class="row col un-zone"><span>Remove Blackboard Buddy from this computer. You choose what else goes with it, and everything removed goes to the Recycle Bin.</span>
   <button class="ghost danger s-uninstall">Uninstall Blackboard Buddy…</button></div>
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

 <div class="set-pane" data-s="share">
  <span class="label">Share my notes</span>
  <p class="sh-about">Give classmates a code and their app shows your This week, deadlines, updates, notes and plans. They don't need a Claude key, and they get your new ones each time you sync. Your marks, feedback, Notebook, boards, files and keys are never shared.</p>
  <div class="sh-own"></div>
  <span class="label">Follow someone's notes</span>
  <div class="sh-follow"></div>
  <small class="sh-msg" aria-live="polite"></small>
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
    out = markdown.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists"])
    return safe_html(out) if following() else out   # someone else's notes: nothing in them may run


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
    mods = sorted(snapshot["courses"], key=lambda c: not c.get("current", True))   # every module, this year's first
    past = lambda c: "" if c.get("current", True) else '<span class="pill past">Earlier year</span>'
    mod_key = lambda name: hashlib.sha1(name.encode()).hexdigest()[:8]
    cards, views = [], []
    for c in mods:
        n_files = sum(len(i.get("files", [])) for i in c.get("contents", []))
        n_secs = sum(1 for i in c.get("contents", []) if not i.get("parentId"))
        cards.append(f'<button class="card glass course" data-open="mod-{mod_key(c["name"])}">{e(short(c["name"]))}{past(c)}'
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
    for c in mods:
        k = mod_key(c["name"])
        mine = [n for n in all_notes if n["course"] == c["name"]]
        todo = sorted((i for i in c.get("contents", []) if (c["name"], i["path"]) not in noted
                       and any(f["name"].lower().endswith(READABLE) for f in i.get("files", []))),
                      key=lambda i: order.get((c["name"], i["path"].split(" / ")[0]), 9999))
        ncards.append(f'<button class="card glass course" data-open="notes-{k}">{e(short(c["name"]))}{past(c)}'
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
                   "text": n["md"][:6000], "path": n.get("path") or "", "t": when_ms(n.get("changed")),
                   "week": said_week(n["title"], n.get("path"))}
                  for n in all_notes if any(c["name"] == n["course"] for c in current)],
        "term": term_start(c["name"] for c in current),
    }).replace("</", "<\\/")
    study_html = study_page([n for n in all_notes if any(c["name"] == n["course"] for c in mods)], mod_key,
                            {mod_key(c["name"]): short(c["name"]) for c in mods if not c["name"].startswith("-")},
                            term_start(c["name"] for c in current), {c["name"] for c in current})
    map_html = ('<h3 class="label">Note map</h3><div class="map-wrap glass">'
                '<div class="map-bar"><div class="map-modes" role="group" aria-label="Colour the dots by">'
                '<span>Colour by</span><button data-m="age">Age</button><button data-m="week">Week</button>'
                '<button data-m="module">Module</button><button data-m="plain">None</button></div>'
                '<button class="map-full" aria-label="Make the note map full screen" title="Full screen">⤢</button></div>'
                '<div class="map-legend"></div>'
                '<div class="map-stage"><canvas class="nmap" role="img"></canvas><div class="map-tip" hidden></div></div>'
                '<div class="map-weeks" role="group" aria-label="Show one week"></div>'
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
        ("study", "Study", study_html),
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
        "activity": load_json(ACTIVITY, {}), "settings": {k: v for k, v in settings().items() if k not in ("share", "follow")},
        "follow": {k: (following() or {}).get(k) for k in ("name", "updated", "synced")} if following() else None,
        "spend": spend.get(datetime.now().strftime("%Y-%m"), 0),
        "name": display_name(snapshot), "version": VERSION, "materials": str(MATERIALS), "online": online.config(settings()), "vault": notebook.ROOT.resolve().as_uri(), "user": snapshot["me"].get("userName", ""),
        "locked": not signed_in(), "bburl": BB_URL, "profile": load_json(DATA_DIR / "profile.json", None),
        "programme": programme.strip("- ").title(), "synced": f"{parse_date(snapshot['synced']).astimezone():%a %d %b, %H:%M}",
    }).replace("</", "<\\/")
    nav = "".join(f'<button data-tab="{k}">{e(t)}{"<i class=near></i>" if k == "deadlines" and first else ""}</button>'
                  for k, t, _ in tabs)
    panes = "".join(f'<section id="{k}">{body}</section>' for k, _, body in tabs)
    synced = parse_date(snapshot["synced"]).astimezone()
    n_current = sum(1 for c in snapshot["courses"] if c.get("current", True))
    n_past = len(snapshot["courses"]) - n_current

    page = f"""<!doctype html><html lang="en" class="{'following' if following() else 'locked' if not signed_in() else ''}{' mac' if IS_MAC else ''}"><head><meta charset="utf-8">
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
.course .pill.past{{display:table;margin-top:6px;font-size:9.5px;padding:2px 7px;opacity:.7}}
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
 <button data-s="top" title="Top half"><i class="stop"></i></button>
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
<div class="sub">Last synced {synced:%A %d %B, %H:%M} · {n_current} module{'' if n_current == 1 else 's'} this year{f' · {n_past} from earlier years' if n_past else ''}</div></header>
<nav class="glass"><span class="indicator"></span>{nav}</nav><main>{panes}</main>
<script>
const nav=document.querySelector('nav'),ind=nav.querySelector('.indicator'),btns=[...nav.querySelectorAll('button')];
const place=(b,instant)=>{{if(instant)ind.style.transition='none';ind.style.left=b.offsetLeft+'px';ind.style.width=b.offsetWidth+'px';
 if(instant){{ind.offsetWidth;ind.style.transition=''}}else{{ind.classList.add('moving');clearTimeout(place.t);place.t=setTimeout(()=>ind.classList.remove('moving'),220)}}
 nav.scrollTo({{left:b.offsetLeft-nav.clientWidth/2+b.offsetWidth/2,behavior:instant?'auto':'smooth'}})}};
const TH={{l:[],i:-1,walk:false}};
const tabHist=k=>{{if(TH.walk){{TH.walk=false;return}}if(TH.l[TH.i]===k)return;
 TH.l=TH.l.slice(0,TH.i+1);TH.l.push(k);if(TH.l.length>50)TH.l.shift();TH.i=TH.l.length-1}};
const show=(k,instant)=>{{if(window.bbPending&&!instant){{const u=window.bbPending;window.bbPending=null;try{{localStorage.tab=k}}catch(_){{}};return bbLeave().then(()=>location.replace(u))}}
 tabHist(k);btns.forEach(b=>b.classList.toggle('on',b.dataset.tab===k));
 document.querySelectorAll('section').forEach(sec=>{{sec.classList.toggle('on',sec.id===k);
  if(sec.id===k)[...sec.children].forEach((c,i)=>{{c.style.setProperty('--i',Math.min(i,12));c.style.animation='none';c.offsetWidth;c.style.animation=''}})}});
 place(btns.find(b=>b.dataset.tab===k),instant);try{{localStorage.tab=k}}catch(_){{}}}};
btns.forEach(b=>b.onclick=()=>show(b.dataset.tab));
// a hidden tab is skipped by the number and arrow keys too
const vis=()=>btns.filter(b=>!b.hidden);
window.bbShow=show;window.bbTabs=btns;
// mouse side buttons: back closes whatever is open on top first, then both step through the tabs you've visited
const overlay=()=>(typeof V!=='undefined'&&!V.hidden)||document.querySelector('.sheet.on,.map-wrap.full,.signin:not([hidden]),[role=dialog]:not([hidden]),[role=alertdialog]:not([hidden])');
const walk=d=>{{const j=TH.i+d;if(j<0||j>=TH.l.length)return;const b=btns.find(x=>x.dataset.tab===TH.l[j]);
 if(!b||b.hidden){{TH.l.splice(j,1);if(d<0)TH.i--;return walk(d)}}TH.i=j;TH.walk=true;show(TH.l[j])}};
['mousedown','auxclick'].forEach(t=>addEventListener(t,ev=>{{if(ev.button===3||ev.button===4)ev.preventDefault()}},true));
addEventListener('mouseup',ev=>{{if(ev.button!==3&&ev.button!==4)return;ev.preventDefault();
 if(ev.button===3&&overlay()){{(document.activeElement||document.body).dispatchEvent(new KeyboardEvent('keydown',{{key:'Escape',bubbles:true}}));return}}
 walk(ev.button===3?-1:1)}},true);window.bbPlaceInd=()=>place(btns.find(b=>b.classList.contains('on')),true);
document.addEventListener('keydown',ev=>{{if(ev.defaultPrevented||ev.target.closest('input,textarea,[contenteditable="true"],#notebook,#online'))return;
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
// the spinning pointer only shows for the first few seconds of a job; the pill up top keeps the progress
let curT,curDone=false;
const bbStatusOff=()=>{{st.classList.remove('show','busy');rf.classList.remove('busy');
 document.documentElement.classList.remove('busy');clearTimeout(curT);curDone=false}};
window.bbStatus=(msg,busy)=>{{st.querySelector('span').textContent=msg;st.classList.toggle('busy',!!busy);rf.classList.toggle('busy',!!busy);
 if(!busy){{document.documentElement.classList.remove('busy');clearTimeout(curT);curDone=false}}
 else if(!curDone&&!document.documentElement.classList.contains('busy')){{document.documentElement.classList.add('busy');
  curT=setTimeout(()=>{{document.documentElement.classList.remove('busy');curDone=true}},5000)}}
 st.classList.add('show');clearTimeout(hideT);clearTimeout(watchT);
 if(!busy){{hideT=setTimeout(()=>st.classList.remove('show'),4000);return}}
 // A busy message waits for the next one to replace it, so if that never comes - the page was
 // reloaded mid-job, say - it would sit there for good. Check now and then whether it is still true.
 const watch=()=>{{watchT=setTimeout(()=>{{if(!window.pywebview)return bbStatusOff();
  Promise.race([pywebview.api.state(),new Promise(r=>setTimeout(()=>r(null),6000))]).then(s=>{{if(s&&s.busy){{if(s.msg)st.querySelector('span').textContent=s.msg;watch()}}
   else bbStatusOff()}}).catch(()=>bbStatusOff())}},8000)}};
 watch()}};
window.addEventListener('pywebviewready',()=>{{document.body.classList.add('app');
 rf.onclick=()=>pywebview.api.refresh();
 document.querySelectorAll('a[href="deadlines.ics"]').forEach(a=>a.onclick=ev=>{{ev.preventDefault();pywebview.api.open_calendar()}});
 if(location.hash.startsWith('#updated'))bbStatus('Up to date',false);
 pywebview.api.state().then(s=>{{if(s.busy)bbStatus(s.msg,!s.quiet)}})}});
// The app's link to the page (pywebview.api) turns up a moment after the page does. Anything that
// reads your files waits for it, or it would start from an empty board and save that over yours.
window.bbApiReady=new Promise(res=>{{const ok=()=>!!(window.pywebview&&pywebview.api&&typeof pywebview.api.state==='function');
 if(ok())return res(true);addEventListener('pywebviewready',()=>res(true));
 const t0=Date.now(),poll=setInterval(()=>{{if(ok()||Date.now()-t0>8000){{clearInterval(poll);res(ok())}}}},100)}});
// Whatever is still waiting to be saved, written out before the page goes (each tab adds its own)
window.bbBeforeLeave=window.bbBeforeLeave||[];
window.bbLeave=()=>Promise.race([Promise.all(bbBeforeLeave.map(f=>{{try{{return Promise.resolve(f()).catch(()=>{{}})}}catch(_){{}}}})),
 new Promise(r=>setTimeout(r,3000))]);
// a finished update reloads the page, but not under your hands on Online or Notebook: there it waits for a tab switch
window.bbReloadTo=url=>{{let t='';try{{t=localStorage.tab||''}}catch(_){{}}
 if(t!=='online'&&t!=='notebook')return bbLeave().then(()=>location.replace(url));
 window.bbPending=url;bbStatus('Updated. Switch tab to see it',false)}};
// No Claude key yet: Online and Notebook only, with a button to unlock the rest
if(document.documentElement.classList.contains('locked')){{
 const nv=document.querySelector('nav.glass'),ub=document.createElement('button');ub.className='unlock-b';ub.type='button';
 ub.textContent='Unlock everything';ub.title='Add a Claude API key to get your week, deadlines, notes and the rest';nv.appendChild(ub);
 ub.onclick=()=>{{const d=document.createElement('div');d.className='unlock';
  d.innerHTML=`<div class="unlock-card glass"><h2>Unlock everything</h2>
   <p>Your week, deadlines, marks, study notes and Ask Claude need a Claude API key. Get one at platform.claude.com, under API keys.</p>
   <label>Claude API key</label><input class="uk" type="password" placeholder="sk-ant-…" autocomplete="off">
   <label>Your Blackboard address</label><input class="uu" spellcheck="false">
   <p class="umsg"></p><div class="ubtns"><button class="ghost ux">Not now</button><button class="btn ugo">Unlock</button></div></div>`;
  document.body.appendChild(d);d.querySelector('.uu').value=BB.bburl||'https://blackboard.uwe.ac.uk';d.querySelector('.uk').focus();
  d.querySelector('.ux').onclick=()=>d.remove();d.onclick=ev=>{{if(ev.target===d)d.remove()}};
  d.querySelector('.ugo').onclick=async()=>{{const m=d.querySelector('.umsg');m.textContent='Checking the key…';
   const r=await pywebview.api.unlock(d.querySelector('.uk').value,d.querySelector('.uu').value);
   if(r!=='ok'){{m.textContent=r;return}}
   try{{localStorage.tab='week'}}catch(_){{}}
   m.textContent='Unlocked. A Blackboard window will open: sign in there, and your week loads after.'}}}}}}
let t='week';try{{t=localStorage.tab||t}}catch(_){{}};
if(document.documentElement.classList.contains('locked')&&t!=='online'&&t!=='notebook')t='online';
show(document.getElementById(t)?t:'week',true);
</script>{CORNER_HTML}<script>window.BB={boot}</script><script>{EXTRA_JS}</script><script>{STUDY_JS}</script><script>{notebook.NB_JS}</script><script>{online.ON_JS}</script></body></html>"""
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


def build_lite():
    """The app for someone without a Claude key yet: Online and Notebook work, the rest waits."""
    if following() and (SHARED / "snapshot.json").exists():
        return run(sync=False, use_ai=False)
    snapshot = load_json(SNAPSHOT, None) or {"synced": now().isoformat(), "me": {"name": ""},
                                             "courses": [], "calendar": []}
    return build_dashboard(snapshot, [], [], [], {})


def run(sync=True, use_ai=True, interactive=True, on_ready=None):
    """Syncs Blackboard (or reuses the last sync), runs the AI and writes the dashboard.
    on_ready(path) is called as soon as the Blackboard part is on the page, before the AI writes."""
    ai_wanted = use_ai and bool(os.getenv("ANTHROPIC_API_KEY"))
    if use_ai and not ai_wanted:
        log("No Claude API key yet, so building the dashboard without AI.")

    if not sync:
        snapshot = load_json(snap_file(), None)
        if not snapshot:
            log("No previous sync found.")
            return None
        deadlines, announcements, grades = (collect_deadlines(snapshot),
                                            recent_announcements(snapshot), released_grades(snapshot))
        ai_results = rehydrate_dates(load_json(ai_file(), {}))
    else:
        bb = open_blackboard(interactive)
        try:
            me = bb.me
            courses = bb.courses()
            n_old = sum(not c["current"] for c in courses)
            log(f"Reading your {len(courses)} modules" + (f" ({n_old} from earlier years)" if n_old else ""))
            bb.read_courses(courses)
            old_snap = load_json(SNAPSHOT, None)
            sync_materials(bb, courses, old_snap)
            snapshot = {"synced": now().isoformat(),
                        "me": {"id": me.get("id"), "userName": me.get("userName"),
                               "name": (me.get("name") or {}).get("given", "")},
                        "courses": courses, "calendar": bb.calendar(days_ahead=LOOKAHEAD_DAYS + 30)}
            save_json(SNAPSHOT, snapshot)
            deadlines, announcements, grades = (collect_deadlines(snapshot),
                                                recent_announcements(snapshot), released_grades(snapshot))
            log(f"{len(deadlines)} deadlines, {len(announcements)} recent announcements, {len(grades)} marks")
            if ai_wanted and on_ready:   # show what's new on Blackboard now; the AI's writing follows
                write_ics(deadlines)
                on_ready(build_dashboard(snapshot, deadlines, announcements, grades,
                                         rehydrate_dates(load_json(DATA_DIR / "ai-latest.json", {}))))
            ai_results = run_ai(snapshot, bb, deadlines, announcements, grades, old_snap) if ai_wanted \
                else rehydrate_dates(load_json(DATA_DIR / "ai-latest.json", {}))
        finally:
            bb.__exit__()

    write_ics(deadlines)
    path = build_dashboard(snapshot, deadlines, announcements, grades, ai_results)
    log("Dashboard ready")
    return path


if __name__ == "__main__":
    main()
