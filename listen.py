"""Two ways to hear something without a Spotify account: live radio, and YouTube.

Radio needs nothing at all - no key, no account, no sign-in. The Radio Browser project keeps a
community list of a few tens of thousands of stations and hands out the stream address, which
plays in the Music tab like any other track. It asks only that programs using it say who they
are, which is what the User-Agent below is for.

YouTube needs a key, because the one thing that would have avoided it is gone: an embedded
player used to take a search term directly, and Google removed that in November 2020. So a
search goes through their Data API, which wants a key from a Google Cloud project, and what
comes back is played in YouTube's own embedded player. That matters: the video plays on their
terms, with their adverts, counting towards the people who made it. Nothing is pulled out of it,
and nothing is stored.

The key is each person's own. A key shipped inside the app would be one key for everybody, and
the free allowance is a hundred searches a day for the whole key, so the hundred-and-first
person that day would find it broken.
"""
import html
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import main

STORE = "listen.json"
_lock = threading.Lock()

# The project runs several machines behind one name. If the round-robin one is having a bad day,
# the others answer the same questions.
RB_HOSTS = ("https://all.api.radio-browser.info",
            "https://de1.api.radio-browser.info",
            "https://nl1.api.radio-browser.info")
AGENT = f"BlackboardBuddy/{main.VERSION} (study app; music tab)"

YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"


# ---------------------------------------------------------------- what is kept on disk

def _path():
    return main.DATA_DIR / STORE


def _read():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data):
    with _lock:
        main.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(_path())


def yt_key():
    return (os.getenv("BB_YOUTUBE_KEY") or _read().get("yt_key") or "").strip()


def set_yt_key(value):
    value = (value or "").strip()
    if value and ("://" in value or " " in value):
        return {"error": "That looks like a link, not a key. A YouTube key is one run of "
                         "letters, numbers, dashes and underscores, about 39 characters."}
    if value and not re.fullmatch(r"[A-Za-z0-9_\-]{30,60}", value):
        return {"error": "That doesn't look like a Google API key. Copy it from the Credentials "
                         "page of your project, where it sits under API keys."}
    data = _read()
    data["yt_key"] = value
    _write(data)
    return {"ok": True}


def status():
    return {"radio": True, "yt_key": bool(yt_key())}


# ---------------------------------------------------------------- radio

def _rb(path, params=None):
    """Asks one Radio Browser machine, then another if that one is out."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    last = None
    for host in RB_HOSTS:
        req = urllib.request.Request(host + path + query, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            continue
    main.log_quiet(f"radio: none of the servers answered: {last!r}")
    return {"error": "Couldn't reach the station list. Are you online?"}


def _station(s):
    """One station, cut down to what the tab draws and plays."""
    url = (s.get("url_resolved") or s.get("url") or "").strip()
    tags = [t.strip() for t in (s.get("tags") or "").split(",") if t.strip()][:3]
    return {"id": s.get("stationuuid", ""), "title": (s.get("name") or "").strip() or "Station",
            "url": url, "secure": url.startswith("https://"),
            "where": (s.get("country") or "").strip(),
            "tags": tags, "art": (s.get("favicon") or "").strip(),
            "codec": (s.get("codec") or "").upper(),
            "bitrate": s.get("bitrate") or 0,
            "hls": bool(s.get("hls")),
            "listeners": s.get("clickcount") or 0}


def _tidy(rows):
    """Drops the broken and the unplayable, puts https first, and never the same station twice.

    A page loaded off the disk will not always play a stream sent over plain http, and HLS is
    not something this window can play at all, so those go to the back rather than being offered
    as if they would work."""
    seen, out = set(), []
    for s in rows:
        if not isinstance(s, dict):
            continue
        row = _station(s)
        if not row["url"] or row["id"] in seen or row["hls"]:
            continue
        seen.add(row["id"])
        out.append(row)
    out.sort(key=lambda r: (not r["secure"], -r["listeners"]))
    return out


def radio_search(q="", limit=30):
    """Stations whose name matches, and stations tagged with it, together - someone typing
    'lofi' means either and should not have to know which."""
    q = (q or "").strip()
    limit = max(1, min(int(limit or 30), 60))
    if not q:
        got = _rb(f"/json/stations/topclick/{limit}")
        if isinstance(got, dict) and got.get("error"):
            return got
        return {"rows": _tidy(got)[:limit], "what": "The most listened-to stations"}
    rows = []
    for field in ("name", "tag"):
        got = _rb("/json/stations/search", {field: q, "limit": limit, "hidebroken": "true",
                                            "order": "clickcount", "reverse": "true"})
        if isinstance(got, dict) and got.get("error"):
            return got
        rows += got
    tidy = _tidy(rows)[:limit]
    if not tidy:
        return {"rows": [], "what": f"Nothing playable called '{q}'"}
    return {"rows": tidy, "what": f"Stations for '{q}'"}


def radio_clicked(station_id):
    """Radio Browser counts what gets played, and that count is what sorts everyone's results.
    Taking from the list without ever adding to it is a poor way to use a shared thing."""
    if not station_id:
        return {}
    threading.Thread(target=_rb, args=(f"/json/url/{station_id}",), daemon=True).start()
    return {"ok": True}


# ---------------------------------------------------------------- youtube

def yt_search(q, limit=20):
    q = (q or "").strip()
    if not q:
        return {"rows": []}
    key = yt_key()
    if not key:
        return {"error": "No YouTube key yet.", "setup": True}
    args = {"part": "snippet", "type": "video", "videoEmbeddable": "true",
            "maxResults": max(1, min(int(limit or 20), 25)), "q": q, "key": key}
    req = urllib.request.Request(YT_SEARCH + "?" + urllib.parse.urlencode(args),
                                 headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            got = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reason, message = "", ""
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
            message = err.get("message", "")
            reason = ((err.get("errors") or [{}])[0]).get("reason", "")
        except Exception:
            pass
        main.log_quiet(f"youtube: search failed {e.code} {reason} {message!r}")
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            return {"error": "That is today's hundred searches used up - the free allowance on a "
                             "YouTube key. It comes back at midnight Pacific time. Radio still "
                             "works, and a pasted link still plays."}
        if reason in ("keyInvalid", "badRequest") or e.code == 400:
            return {"error": "YouTube would not take that key. Check it is copied whole, and "
                             "that YouTube Data API v3 is switched on for its project."}
        if e.code == 403:
            return {"error": "YouTube refused the key" + (f": {message}" if message else ".")}
        return {"error": f"YouTube said no ({e.code})."}
    except Exception as e:
        main.log_quiet(f"youtube: search couldn't get through: {e!r}")
        return {"error": "Couldn't reach YouTube. Are you online?"}

    rows = []
    for it in got.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        snip = it.get("snippet") or {}
        if not vid:
            continue
        thumbs = snip.get("thumbnails") or {}
        thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        rows.append({"id": vid, "title": html.unescape(snip.get("title", "")),
                     "where": html.unescape(snip.get("channelTitle", "")),
                     "art": thumb,
                     "url": f"https://www.youtube.com/watch?v={vid}"})
    return {"rows": rows, "what": f"YouTube for '{q}'"}
