"""Reads your Blackboard Learn (Ultra) account through its REST API,
using a browser session you sign into yourself."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

API = "/learn/api/public"


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Blackboard:
    def __init__(self, base_url, profile_dir, log=print, headless=False):
        self.base = base_url.rstrip("/")
        self.profile_dir = Path(profile_dir)
        self.log = log
        self.headless = headless
        self._pw = None
        self.context = None
        self.me = None

    # ---------- session ----------

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), channel="msedge", headless=self.headless,
            viewport={"width": 1100, "height": 800},
        )
        return self

    def __exit__(self, *exc):
        if self.context:
            self.context.close()
        if self._pw:
            self._pw.stop()

    def login(self, timeout_minutes=10):
        """Opens Blackboard and waits until you are signed in."""
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        page.goto(f"{self.base}/ultra/course")
        if not self.headless:
            page.bring_to_front()  # so the sign-in window shows up over the app
        deadline = time.time() + timeout_minutes * 60
        told = False
        while time.time() < deadline:
            resp = self.context.request.get(f"{self.base}{API}/v1/users/me", fail_on_status_code=False)
            if resp.ok:
                self.me = resp.json()
                self.log(f"Signed in as {self.me.get('name', {}).get('given', '')} "
                         f"({self.me.get('userName')})")
                return self.me
            if not told and not self.headless and time.time() > deadline - timeout_minutes * 60 + 12:
                self.log("Please sign in to Blackboard in the browser window that opened...")
                told = True
            time.sleep(3)
        raise TimeoutError("Timed out waiting for Blackboard sign-in.")

    # ---------- raw API ----------

    def get(self, path, params=None):
        resp = self.context.request.get(f"{self.base}{path}", params=params, fail_on_status_code=False)
        if not resp.ok:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def get_all(self, path, params=None, max_pages=20):
        """Follows Blackboard's paging.nextPage links."""
        results = []
        data = self.get(path, params)
        pages = 0
        while data and pages < max_pages:
            results.extend(data.get("results", []))
            nxt = (data.get("paging") or {}).get("nextPage")
            if not nxt:
                break
            data = self.get(nxt)
            pages += 1
        return results

    def download(self, path):
        resp = self.context.request.get(f"{self.base}{path}", fail_on_status_code=False)
        return resp.body() if resp.ok else None

    # ---------- data ----------

    def courses(self, current_days=300):
        """All open modules. 'current' marks this year's: enrolled in the last ~10 months,
        or a programme page (UWE names those starting with '-')."""
        uid = self.me["id"]
        memberships = self.get_all(f"{API}/v1/users/{uid}/courses",
                                   {"expand": "course", "limit": 100})
        cutoff = datetime.now(timezone.utc) - timedelta(days=current_days)
        out = []
        for m in memberships:
            c = m.get("course") or {}
            if (m.get("availability") or {}).get("available") == "No":
                continue
            if (c.get("availability") or {}).get("available") == "No":
                continue
            enrolled = parse_date(m.get("created"))
            name = c.get("name", "Untitled course")
            out.append({
                "id": c.get("id") or m.get("courseId"),
                "courseId": c.get("courseId", ""),
                "name": name,
                "url": f"{self.base}/ultra/courses/{c.get('id') or m.get('courseId')}/outline",
                "role": m.get("courseRoleId"),
                "current": bool(enrolled and enrolled >= cutoff) or name.startswith("-"),
            })
        return out

    def announcements(self, course_id):
        items = self.get_all(f"{API}/v1/courses/{course_id}/announcements", {"limit": 100})
        return [{
            "id": a.get("id"),
            "title": a.get("title", ""),
            "body": a.get("body", ""),
            "date": a.get("created") or (a.get("availability", {}).get("duration", {}) or {}).get("start"),
        } for a in items]

    def gradebook(self, course_id):
        """Gradebook columns (assessments, with due dates) merged with your grades."""
        cols = self.get_all(f"{API}/v2/courses/{course_id}/gradebook/columns", {"limit": 100})
        grades = self.get_all(f"{API}/v2/courses/{course_id}/gradebook/users/{self.me['id']}",
                              {"limit": 100})
        by_col = {g.get("columnId"): g for g in grades}
        out = []
        for col in cols:
            g = by_col.get(col.get("id"), {})
            shown = g.get("displayGrade") or {}
            out.append({
                "id": col.get("id"),
                "name": col.get("name", ""),
                "description": col.get("description", ""),
                "due": (col.get("grading") or {}).get("due"),
                "contentId": col.get("contentId"),
                "possible": (col.get("score") or {}).get("possible"),
                "score": g.get("score", shown.get("score")),
                "display": shown.get("text") or (f"{shown['score']:g}" if shown.get("score") is not None else None),
                "overall": (col.get("grading") or {}).get("type") == "Calculated",
                "status": g.get("status"),
                "feedback": g.get("feedback") or g.get("text") or "",
            })
        return out

    def calendar(self, days_back=7, days_ahead=60):
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        until = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        items = self.get_all(f"{API}/v1/calendars/items",
                             {"since": since, "until": until, "limit": 200})
        return [{
            "title": i.get("title", ""),
            "type": i.get("type"),
            "calendarName": i.get("calendarName", ""),
            "courseId": i.get("calendarId"),
            "start": i.get("start"),
            "end": i.get("end"),
            "location": i.get("location", ""),
            "description": i.get("description", ""),
        } for i in items]

    def contents(self, course_id, max_depth=4, max_items=400):
        """Walks the course outline, returning every item with a flat 'path'."""
        out = []

        def walk(path, parent_titles, parent_id, depth):
            if depth > max_depth or len(out) >= max_items:
                return
            for item in self.get_all(path, {"limit": 200}):
                title = item.get("title", "")
                if title == "ultraDocumentBody":
                    title = parent_titles[-1] if parent_titles else "Course page"
                titles = parent_titles + [title] if title not in parent_titles[-1:] else parent_titles
                ch = item.get("contentHandler") or {}
                handler = ch.get("id", "")
                out.append({
                    "id": item.get("id"),
                    "title": title,
                    "path": " / ".join(titles),
                    "body": item.get("body", "") or item.get("description", ""),
                    "handler": handler,
                    "created": item.get("created"),
                    "modified": item.get("modified"),
                    "hasChildren": item.get("hasChildren", False),
                    "parentId": parent_id,
                    "url": ch.get("url") or item.get("link") or "",
                })
                if item.get("hasChildren"):
                    walk(f"{API}/v1/courses/{course_id}/contents/{item['id']}/children",
                         titles, item["id"], depth + 1)

        walk(f"{API}/v1/courses/{course_id}/contents", [], None, 0)
        return out

    def attachments(self, course_id, content_id):
        items = self.get_all(f"{API}/v1/courses/{course_id}/contents/{content_id}/attachments")
        return [{
            "id": a.get("id"),
            "fileName": a.get("fileName", "file"),
            "mimeType": a.get("mimeType", ""),
            "downloadPath": f"{API}/v1/courses/{course_id}/contents/{content_id}"
                            f"/attachments/{a.get('id')}/download",
        } for a in items]
