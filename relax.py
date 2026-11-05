"""Relax mode: what the app becomes when you are not studying.

Four tabs - Watch, Music, Chill and Gallery - and a look of its own, built out of the same parts as
the rest of the app: the dot grid, the dot-matrix numerals, glass cards, Space Mono labels. Same
app, different mood, not a different program bolted on.

Nothing from Blackboard appears in here. No deadlines, no grades, no notifications. That is the
whole point of it.

Watch is built on sources, the way Stremio is built on add-ons: each source can list a catalogue
and hand back something playable. Three kinds ship with it - a folder on this computer, your own
media server (Jellyfin or Emby), and the Internet Archive's public-domain film collections - and
the streaming services you pay for open in a window of the app, signed in as you. What is
deliberately missing is a torrent or "online library" source: those index films from people who
have no right to share them, and this app will not carry one.
"""

import base64
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VIDEO = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
AUDIO = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
SUBS = (".vtt", ".srt")

# what a browser can actually play. .mkv and .avi usually cannot be decoded by the window's engine,
# so they are listed but marked, rather than quietly failing with a black screen.
PLAYABLE = {".mp4", ".m4v", ".webm", ".mov"}

# season/episode, written the half-dozen ways files are usually named
EPISODE = re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})(?:[^0-9]|$)", re.I)
EPISODE_X = re.compile(r"(?:^|[^a-z0-9])(\d{1,2})x(\d{2,3})(?:[^0-9]|$)", re.I)
# the noise release names carry; a title is cut at the first of these
JUNK = re.compile(r"\b(1080p|2160p|720p|480p|4k|uhd|hdr10?|sdr|x264|x265|h ?264|h ?265|hevc|avc|xvid|divx"
                  r"|web ?-?dl|web ?rip|bluray|blu ?-?ray|bdrip|brrip|dvdrip|hdrip|remux|proper|repack"
                  r"|aac\d?|ac3|dts(?:-hd)?|truehd|atmos|ddp?5 ?1|10bit|8bit|multi|dual|subbed|dubbed)\b", re.I)
YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


def tidy(stem):
    """'The.Thing.1982.1080p.BluRay.x264-GRP' -> ('The Thing', '1982')."""
    s = re.sub(r"[._]+", " ", str(stem))
    s = re.sub(r"[\[(][^\])]*[\])]", " ", s)
    year = ""
    m = YEAR.search(s)
    if m and m.start() > 2:
        year = m.group(1)
        s = s[:m.start()]
    cut = JUNK.search(s)
    if cut and cut.start() > 2:
        s = s[:cut.start()]
    s = re.sub(r"\s*-\s*[A-Za-z0-9]+\s*$", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -_")
    return (s or str(stem)), year


def episode_of(stem):
    m = EPISODE.search(stem) or EPISODE_X.search(stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


def subtitle_for(path):
    """A subtitle file sitting beside the video, including the name.en.srt shape."""
    for ext in SUBS:
        side = path.with_suffix(ext)
        if side.exists():
            return side
    try:
        for side in sorted(path.parent.glob(glob_escape(path.stem) + ".*")):
            if side.suffix.lower() in SUBS:
                return side
    except OSError:
        pass
    return None


def glob_escape(text):
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def scan(folder, exts, limit=4000):
    """Every media file under a folder, newest first."""
    root = Path(folder)
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*"):
        if len(out) >= limit:
            break
        try:
            if not p.is_file() or p.suffix.lower() not in exts or p.name.startswith("."):
                continue
            st = p.stat()
        except OSError:
            continue
        out.append({"id": str(p), "path": str(p), "url": p.as_uri(), "file": p.name,
                    "size": st.st_size, "mtime": int(st.st_mtime), "folder": p.parent.name,
                    "plays": p.suffix.lower() in PLAYABLE, "ext": p.suffix.lower().lstrip(".")})
    out.sort(key=lambda d: -d["mtime"])
    return out


def video_library(folder):
    """Films on their own; series gathered into one card each, episodes in order."""
    films, series = [], {}
    for item in scan(folder, VIDEO):
        stem = Path(item["file"]).stem
        title, year = tidy(stem)
        ep = episode_of(stem)
        item["title"], item["year"] = title, year
        item["subs"] = bool(subtitle_for(Path(item["path"])))
        if ep:
            name = tidy(re.split(r"(?:^|[^a-z0-9])(?:s\d{1,2}[ ._-]*e\d{1,3}|\d{1,2}x\d{2,3})",
                                 stem, flags=re.I)[0])[0] or item["folder"]
            item["season"], item["episode"] = ep
            item["label"] = f"S{ep[0]} E{ep[1]}"
            s = series.setdefault(name.lower(), {"kind": "series", "title": name, "id": "series:" + name.lower(),
                                                 "episodes": [], "mtime": 0, "year": year})
            s["episodes"].append(item)
            s["mtime"] = max(s["mtime"], item["mtime"])
        else:
            item["kind"] = "film"
            films.append(item)
    for s in series.values():
        s["episodes"].sort(key=lambda d: (d.get("season", 0), d.get("episode", 0)))
        s["count"] = len(s["episodes"])
    rows = films + list(series.values())
    rows.sort(key=lambda d: -d["mtime"])
    return rows


def audio_library(folder):
    rows = scan(folder, AUDIO)
    for r in rows:
        r["title"], _ = tidy(Path(r["file"]).stem)
        r["artist"] = r["folder"]
    return rows


def image_library(folder):
    rows = scan(folder, IMAGE)
    for r in rows:
        r["title"] = Path(r["file"]).stem
    return rows


SRT_TIME = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


def to_vtt(path):
    """Subtitles the page can use. SRT differs from VTT by a header and its commas."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
    if p.suffix.lower() == ".vtt":
        return text
    return "WEBVTT\n\n" + SRT_TIME.sub(r"\1.\2", text)


def subtitle_url(path):
    """A track the page can point at without us running a web server for it."""
    return "data:text/vtt;base64," + base64.b64encode(to_vtt(path).encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------- sources (the add-on idea)

# A source is a small record: {"id", "kind", "name", plus whatever that kind needs}. Each kind
# knows how to list a catalogue and how to turn a row into something playable. Adding a kind here
# is all it takes to add a source; nothing else in the app has to know about it.

SOURCE_KINDS = [
    {"kind": "folder", "name": "Folder on this computer",
     "note": "Films and series you already have. Picks up subtitles sitting next to them.",
     "fields": [{"key": "path", "label": "Folder", "type": "folder"}]},
    {"kind": "jellyfin", "name": "Your own media server",
     "note": "Jellyfin or Emby, running at home or on a server you own. Needs its address and an API key "
             "(Dashboard › API Keys).",
     "fields": [{"key": "url", "label": "Server address", "type": "text", "hint": "http://192.168.1.10:8096"},
                {"key": "key", "label": "API key", "type": "password"}]},
    {"kind": "archive", "name": "Internet Archive",
     "note": "Public-domain and freely licensed films - silent era, noir, newsreels, animation. "
             "Nothing to sign into.",
     "fields": [{"key": "collection", "label": "Collection", "type": "choice",
                 "choices": ["feature_films", "film_noir", "silent_films", "animationandcartoons",
                             "classic_tv", "sci-fi_horror", "newsandpublicaffairs"]}]},
]
KIND_NAMES = {k["kind"]: k["name"] for k in SOURCE_KINDS}


def new_source(kind, values):
    """Builds the stored record for a source, and refuses one it cannot work with."""
    kind = str(kind)
    if kind not in KIND_NAMES:
        return {"error": "Unknown kind of source."}
    cfg = {"id": uuid.uuid4().hex[:10], "kind": kind, "added": int(time.time())}
    v = {str(k): str(values.get(k, "")).strip() for k in (f["key"] for f in
         next(s for s in SOURCE_KINDS if s["kind"] == kind)["fields"])}
    if kind == "folder":
        if not v["path"] or not Path(v["path"]).is_dir():
            return {"error": "That folder isn't there."}
        cfg["path"] = v["path"]
        cfg["name"] = Path(v["path"]).name or v["path"]
    elif kind == "jellyfin":
        url = v["url"].rstrip("/")
        if not url.startswith(("http://", "https://")):
            return {"error": "The address needs to start with http:// or https://"}
        if not v["key"]:
            return {"error": "That server needs an API key."}
        cfg["url"], cfg["key"] = url, v["key"]
        cfg["name"] = urllib.parse.urlparse(url).hostname or "Media server"
    else:
        cfg["collection"] = v["collection"] or "feature_films"
        cfg["name"] = "Internet Archive · " + cfg["collection"].replace("_", " ")
    return cfg


def catalogue(cfg, query="", limit=60):
    """What this source has, as rows the Watch tab can draw."""
    kind = cfg.get("kind")
    if kind == "folder":
        rows = video_library(cfg.get("path", ""))
        if query:
            q = query.lower()
            rows = [r for r in rows if q in r.get("title", "").lower()]
        return rows[:limit]
    if kind == "jellyfin":
        return _jellyfin(cfg, query, limit)
    if kind == "archive":
        return _archive(cfg, query, limit)
    return []


def _get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _jellyfin(cfg, query, limit):
    base, key = cfg["url"], cfg["key"]
    q = urllib.parse.urlencode({
        "Recursive": "true", "IncludeItemTypes": "Movie,Series", "SortBy": "DateCreated",
        "SortOrder": "Descending", "Limit": str(limit), "Fields": "ProductionYear",
        "SearchTerm": query or "", "api_key": key})
    data = _get(f"{base}/Items?{q}")
    rows = []
    for it in data.get("Items", []):
        iid, is_series = it.get("Id"), it.get("Type") == "Series"
        row = {"id": f"jf:{iid}", "title": it.get("Name", "Untitled"),
               "year": str(it.get("ProductionYear") or ""), "kind": "series" if is_series else "film",
               "poster": f"{base}/Items/{iid}/Images/Primary?maxHeight=480&api_key={key}",
               "plays": True, "source": cfg["id"], "mtime": 0}
        if is_series:
            row["lazy"] = iid            # episodes are fetched when the card is opened
            row["count"] = it.get("ChildCount") or 0
        else:
            row["url"] = f"{base}/Videos/{iid}/stream?static=true&api_key={key}"
        rows.append(row)
    return rows


def jellyfin_episodes(cfg, series_id):
    base, key = cfg["url"], cfg["key"]
    data = _get(f"{base}/Shows/{series_id}/Episodes?" + urllib.parse.urlencode(
        {"api_key": key, "Fields": "Overview"}))
    out = []
    for it in data.get("Items", []):
        iid = it.get("Id")
        out.append({"id": f"jf:{iid}", "title": it.get("Name", ""), "plays": True,
                    "label": f"S{it.get('ParentIndexNumber', 0)} E{it.get('IndexNumber', 0)}",
                    "season": it.get("ParentIndexNumber") or 0, "episode": it.get("IndexNumber") or 0,
                    "url": f"{base}/Videos/{iid}/stream?static=true&api_key={key}"})
    out.sort(key=lambda d: (d["season"], d["episode"]))
    return out


def _archive(cfg, query, limit):
    """The Internet Archive's own search. Everything here is free to watch by licence."""
    terms = f'collection:({cfg.get("collection", "feature_films")}) AND mediatype:(movies)'
    if query:
        terms += f' AND title:({query})'
    q = urllib.parse.urlencode({"q": terms, "fl[]": "identifier", "rows": str(limit),
                                "page": "1", "output": "json", "sort[]": "downloads desc"})
    data = _get("https://archive.org/advancedsearch.php?" + q,
                {"User-Agent": "BlackboardBuddy/relax"})
    rows = []
    for doc in data.get("response", {}).get("docs", []):
        ident = doc.get("identifier")
        if not ident:
            continue
        rows.append({"id": f"ia:{ident}", "title": ident.replace("_", " ").strip(), "kind": "film",
                     "year": "", "poster": f"https://archive.org/services/img/{ident}",
                     "resolve": ident, "plays": True, "source": cfg["id"], "mtime": 0})
    return rows


def archive_stream(identifier):
    """The actual playable file for one Archive item, picked for what the window can decode."""
    data = _get(f"https://archive.org/metadata/{urllib.parse.quote(identifier)}",
                {"User-Agent": "BlackboardBuddy/relax"})
    files = data.get("files", [])
    best = None
    for f in files:
        name = f.get("name", "")
        ext = Path(name).suffix.lower()
        if ext in (".mp4", ".m4v", ".webm", ".ogv"):
            size = int(f.get("size") or 0)
            # the smallest decent copy starts fastest over the network
            if best is None or size < best[0]:
                best = (size, name)
    if not best:
        return {"error": "Nothing in that item plays in this window."}
    return {"url": f"https://archive.org/download/{urllib.parse.quote(identifier)}/"
                   f"{urllib.parse.quote(best[1])}", "title": identifier.replace('_', ' ')}


# Services you sign into yourself. They open in a window of the app, which keeps its own cookies,
# so signing in once is enough.
# "inside" says whether the service lets its pages be shown inside another page. The ones that do
# not are not a shortcoming here: they send a header that forbids it, and no app can override that
# without breaking their terms. Those get a one-click window instead, and say why.
SERVICES = [
    ("YouTube", "https://www.youtube.com", True),
    ("Vimeo", "https://vimeo.com", True),
    ("Dailymotion", "https://www.dailymotion.com", True),
    ("Internet Archive", "https://archive.org/details/movies", True),
    ("Twitch", "https://www.twitch.tv", True),
    ("Netflix", "https://www.netflix.com", False),
    ("Prime Video", "https://www.primevideo.com", False),
    ("Disney+", "https://www.disneyplus.com", False),
    ("iPlayer", "https://www.bbc.co.uk/iplayer", False),
    ("ITVX", "https://www.itv.com/watch", False),
    ("Channel 4", "https://www.channel4.com", False),
    ("Apple TV+", "https://tv.apple.com", False),
]

SERVICE_HTML = "".join(
    f'<button class="rx-serv" data-url="{url}" data-name="{name}" data-inside="{int(inside)}">{name}</button>'
    for name, url, inside in SERVICES)

KIND_HTML = "".join(
    f'<button class="rx-kind" data-kind="{k["kind"]}"><b>{k["name"]}</b><span>{k["note"]}</span></button>'
    for k in SOURCE_KINDS)


RX_HTML = {
    "watch": f"""
<header class="rx-top"><h1>Watch</h1><div class="sub">Yours, your server's, and the services you pay for</div></header>

<div class="rx-resume" hidden><h3 class="label">Carry on watching</h3><div class="rx-rail"></div></div>

<h3 class="label">Services</h3>
<div class="rx-servs">{SERVICE_HTML}</div>
<div class="rx-online glass">
 <div class="rx-link-row">
  <input class="rx-wlink" type="url" aria-label="Link to watch"
   placeholder="Paste a YouTube, Vimeo, Archive or Twitch link and it plays in here">
  <button class="btn rx-wlink-go">Play here</button></div>
 <p class="note rx-wlink-note">Everything that can play inside a page plays here, in this tab.</p>
 <div class="rx-embed-wrap rx-wembed-wrap" hidden>
  <iframe class="rx-embed rx-wembed" title="Player" frameborder="0"
   allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
   allowfullscreen></iframe>
  <div class="rx-embed-act"><b class="rx-wembed-what"></b>
   <span><button class="ghost rx-wembed-window" hidden>Open in a window</button>
   <button class="ghost rx-wembed-close">Close player</button></span></div>
 </div>
</div>

<h3 class="label">Sources</h3>
<div class="rx-srcs"></div>
<div class="rx-search" hidden><input class="rx-q" type="search" placeholder="Search this source" aria-label="Search this source"></div>

<div class="rx-empty rx-watch-empty">
 <p class="sub">Nothing added yet. A source is where films come from: a folder on this computer,
 your own media server, or the Internet Archive's public-domain collections.</p>
 <button class="btn rx-add-src">Add a source</button></div>

<div class="rx-grid rx-watch-grid"></div>
<p class="note rx-note" hidden></p>

<div class="rx-player" hidden>
 <video class="rx-video" controls playsinline></video>
 <div class="rx-pbar glass"><b class="rx-ptitle"></b>
  <span class="rx-pact"><button class="ghost rx-prev-ep" hidden>Previous</button>
  <button class="ghost rx-next-ep" hidden>Next episode</button>
  <button class="ghost rx-pclose">Close</button></span></div>
</div>

<div class="sheet right rx-sheet rx-eps"><div class="sh-top"><span class="label rx-eps-title"></span>
 <button class="ghost rx-sheet-x">Close</button></div><div class="rx-eps-list"></div></div>

<div class="sheet right rx-sheet rx-newsrc"><div class="sh-top"><span class="label">Add a source</span>
 <button class="ghost rx-sheet-x">Close</button></div>
 <div class="rx-kinds">{KIND_HTML}</div>
 <div class="rx-fields" hidden></div></div>
""",

    "music": """
<header class="rx-top"><h1>Music</h1><div class="sub">Keeps playing while you wander off to another tab</div></header>

<div class="rx-now glass" hidden>
 <div class="rx-now-text"><b class="rx-now-title">Nothing playing</b><span class="rx-now-artist sub"></span></div>
 <div class="rx-now-ctl">
  <button class="rx-ctl rx-prev" title="Previous" aria-label="Previous">
   <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 5v14L8 12zM6 5h2v14H6z" fill="currentColor"/></svg></button>
  <button class="rx-ctl rx-play" title="Play" aria-label="Play">
   <svg class="i-play" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l13 8-13 8z" fill="currentColor"/></svg>
   <svg class="i-pause" viewBox="0 0 24 24" aria-hidden="true" hidden><path d="M7 4h4v16H7zM13 4h4v16h-4z" fill="currentColor"/></svg></button>
  <button class="rx-ctl rx-next" title="Next" aria-label="Next">
   <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5v14l10-7zM16 5h2v14h-2z" fill="currentColor"/></svg></button>
  <button class="rx-ctl rx-shuffle" title="Shuffle" aria-label="Shuffle">
   <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 17h4l10-10h4M3 7h4l3 3M14 14l3 3h4"/><path d="M18 3l3 4-3 4M18 13l3 4-3 4"/></svg></button>
 </div>
 <div class="rx-seek"><span class="rx-at">0:00</span>
  <input type="range" class="rx-seek-in" value="0" min="0" max="1000" aria-label="Seek">
  <span class="rx-dur">0:00</span></div>
</div>

<h3 class="label">Online</h3>
<div class="rx-online glass">
 <div class="rx-servs rx-msrv">
  <button class="rx-serv" data-url="https://open.spotify.com" data-name="Spotify" data-inside="0">Spotify</button>
  <button class="rx-serv" data-url="https://music.youtube.com" data-name="YouTube Music" data-inside="0">YouTube Music</button>
  <button class="rx-serv" data-url="https://soundcloud.com" data-name="SoundCloud" data-inside="0">SoundCloud</button>
  <button class="rx-serv" data-url="https://music.apple.com" data-name="Apple Music" data-inside="0">Apple Music</button>
  <button class="rx-serv" data-url="https://bandcamp.com" data-name="Bandcamp" data-inside="0">Bandcamp</button>
 </div>
 <div class="rx-link-row">
  <input class="rx-link" type="url" aria-label="Link to play"
   placeholder="Paste a track, album or playlist link and it plays in here">
  <button class="btn rx-link-go">Play here</button></div>
 <p class="note rx-link-note">Browse on a service to copy a link, then paste it. It plays in this
 tab, not in another window.</p>
 <div class="rx-embed-act">
  <button class="ghost rx-signin-sp">Sign in to Spotify</button>
  <span><button class="ghost rx-reload-player" hidden>Reload player</button>
  <button class="ghost rx-mwindow" hidden>Open in a window</button></span></div>
 <div class="rx-embed-wrap rx-membed-wrap" hidden>
  <iframe class="rx-embed rx-membed" title="Music player" frameborder="0" loading="lazy"
   allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"></iframe>
  <div class="rx-embed-act"><b class="rx-membed-what"></b>
   <button class="ghost rx-membed-close">Close player</button></div>
 </div>
</div>

<h3 class="label">On this computer</h3>
<div class="rx-empty rx-audio-empty"><p class="sub">Point it at the folder your music is in and the lot becomes a playlist.</p>
 <button class="btn rx-pick" data-kind="audio">Choose a folder</button></div>
<h3 class="label rx-tracks-label" hidden>Tracks</h3>
<div class="rx-tracks"></div>
""",

    "chill": """
<header class="rx-top"><h1>Chill</h1><div class="sub">Sound made on the spot, not played from a file</div></header>

<h3 class="label">Mix</h3>
<div class="rx-mix"></div>

<h3 class="label">Wind-down</h3>
<div class="rx-timer glass">
 <div class="rx-clock"><span class="rx-left">00:00</span></div>
 <div class="rx-timer-set">
  <button class="ghost rx-mins" data-m="10">10 min</button>
  <button class="ghost rx-mins" data-m="20">20 min</button>
  <button class="ghost rx-mins" data-m="45">45 min</button>
  <button class="ghost rx-mins" data-m="90">90 min</button>
  <button class="ghost rx-stop-timer">Stop</button></div>
 <p class="note">It fades out over the last half minute rather than cutting off.</p>
</div>

<h3 class="label">Breathe</h3>
<div class="rx-breathe glass">
 <div class="rx-orb"><i></i></div>
 <p class="note rx-breathe-text">In as it grows, out as it shrinks. Four seconds each way.</p>
 <button class="ghost rx-breathe-go">Start</button>
</div>
""",

    "gallery": """
<header class="rx-top"><h1>Gallery</h1><div class="sub">Your pictures, and somewhere to draw</div></header>

<div class="rx-seg"><button class="on" data-seg="photos">Photos</button><button data-seg="draw">Draw</button></div>

<div class="rx-seg-pane" data-seg="photos">
 <div class="rx-empty rx-image-empty"><p class="sub">Choose a folder of pictures to see them here.</p>
  <button class="btn rx-pick" data-kind="image">Choose a folder</button></div>
 <div class="rx-photos"></div>
</div>

<div class="rx-seg-pane" data-seg="draw" hidden>
 <div class="rx-draw-bar glass"><span class="rx-swatches"></span>
  <input type="range" class="rx-size" min="1" max="60" value="6" aria-label="Brush size">
  <button class="ghost rx-erase">Erase</button><button class="ghost rx-undo">Undo</button>
  <button class="ghost rx-clear">Clear</button><button class="btn rx-save-draw">Save</button></div>
 <canvas class="rx-canvas"></canvas>
</div>

<div class="rx-light" hidden><button class="rx-light-nav rx-light-prev" aria-label="Previous">&lsaquo;</button>
 <img class="rx-light-img" alt=""><button class="rx-light-nav rx-light-next" aria-label="Next">&rsaquo;</button>
 <button class="ghost rx-light-close">Close</button></div>
""",
}

RX_TABS = [("watch", "Watch", RX_HTML["watch"]), ("music", "Music", RX_HTML["music"]),
           ("chill", "Chill", RX_HTML["chill"]), ("gallery", "Gallery", RX_HTML["gallery"])]
RX_KEYS = [k for k, _, _ in RX_TABS]


RX_CSS = r"""
/* ---------------------------------------------------------------- relax mode
   Same parts as the rest of the app - dot grid, Doto numerals, Space Mono labels, glass - with
   the red moved from a warning colour to the colour of the mode itself. */
html.relax{--dot:rgba(215,25,33,.17);--accent:var(--red)}
@media (prefers-color-scheme:dark){html.relax:not([data-theme=light]){--dot:rgba(215,25,33,.26)}}
html.relax[data-theme=dark]{--dot:rgba(215,25,33,.26)}
html.relax .ambient::before{background-size:30px 30px;background-position:15px 15px}
html.relax .ambient::after{content:"";position:absolute;inset:0;
 background:radial-gradient(120% 80% at 50% -10%,rgba(215,25,33,.13),transparent 60%)}
html.relax > body > header{display:none}            /* every relax tab brings its own */
html.relax .refresh{display:none}
/* Ask Claude is a school thing, so it goes - and with nothing in the bottom right to clear,
   the dock has no reason to sit left of centre any more. */
html.relax .ask{display:none!important}
html.relax .status{display:none!important}   /* "Checking Blackboard..." is not relaxing */
html.relax nav.glass{--dockr:0px}
html.relax .indicator{background:var(--red);box-shadow:0 2px 18px rgba(215,25,33,.45)}
html.relax nav button.on{color:#fff}
html:not(.relax) nav button[data-mode=relax]{display:none!important}
html.relax nav button[data-mode=study]{display:none!important}

/* the switch itself, sitting with the theme and refresh buttons */
.rx-toggle svg{width:19px;height:19px}
html.relax .rx-toggle{background:var(--red);border-color:transparent;color:#fff}

/* the sweep between the two modes: the app's own dot grid, pulled across the window */
.rx-wipe{position:fixed;inset:0;z-index:60;pointer-events:none;display:grid;place-items:center;
 background:var(--bg);clip-path:circle(0% at var(--x,50%) var(--y,8%));opacity:1}
.rx-wipe::before{content:"";position:absolute;inset:0;
 background-image:radial-gradient(var(--red) 1.4px,transparent 1.8px);background-size:26px 26px;opacity:.5}
.rx-wipe b{font:800 clamp(38px,11vw,96px)/1 var(--dotf);text-transform:uppercase;letter-spacing:.06em;
 color:var(--text);position:relative;transform:scale(.94);opacity:0}
.rx-wipe.go{animation:rxIn .42s cubic-bezier(.22,1,.36,1) forwards}
.rx-wipe.out{animation:rxOut .5s cubic-bezier(.4,0,.2,1) forwards}
.rx-wipe.go b{animation:rxWord .42s var(--spring) forwards}
.rx-wipe.out b{animation:rxWordOut .3s ease forwards}
@keyframes rxIn{from{clip-path:circle(0% at var(--x,50%) var(--y,8%))}to{clip-path:circle(150% at var(--x,50%) var(--y,8%))}}
@keyframes rxOut{from{clip-path:circle(150% at var(--x,50%) var(--y,8%));opacity:1}
 to{clip-path:circle(150% at var(--x,50%) var(--y,8%));opacity:0}}
@keyframes rxWord{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes rxWordOut{to{transform:scale(1.06);opacity:0}}
@media (prefers-reduced-motion:reduce){.rx-wipe,.rx-wipe b{animation-duration:.01s}}

/* a tab's own header, in the same dot-matrix as the dashboard's */
.rx-top{margin:2px 0 10px}
/* The dashboard heading switches to a pixel font that can draw any writing system, because it
   carries a person's name. These titles are fixed English words, so they stay in Doto, in capitals,
   whatever the name does - hence matching html.intl's own specificity here. */
.rx-top h1,html.intl .rx-top h1{margin:0;font-family:"Doto","Space Mono",ui-monospace,monospace;
 font-weight:800;font-size:54px;line-height:1;text-transform:uppercase;letter-spacing:.01em;
 font-synthesis:none}
.rx-top .sub{color:var(--muted);font-size:13.5px}
@media (max-width:600px){.rx-top h1,html.intl .rx-top h1{font-size:38px}}

.rx-empty{border:1px dashed var(--line);border-radius:28px;padding:26px;text-align:center;margin:14px 0}
.rx-empty p{max-width:52ch;margin:0 auto 14px}
.rx-note{color:var(--muted)}

/* services and sources: pill rows in the app's mono */
.rx-servs,.rx-srcs{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}
.rx-serv,.rx-src{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;
 padding:9px 16px;font:11.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;
 transition:transform .5s var(--bounce),background-color .3s,border-color .3s;display:inline-flex;align-items:center;gap:8px}
.rx-serv:hover,.rx-src:hover{background:var(--inset);transform:translateY(-1px)}
.rx-serv:active,.rx-src:active{transform:scale(.96)}
.rx-src.on{background:var(--red);border-color:transparent;color:#fff}
.rx-src .x{opacity:.55;font-size:13px}
.rx-src .x:hover{opacity:1}
.rx-add-src-pill{border-style:dashed}

.rx-search{margin:10px 0 2px}
.rx-q{width:100%;border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:999px;
 padding:10px 16px;font:13px var(--mono)}
.rx-q:focus{outline:none;border-color:var(--red)}

/* poster wall */
.rx-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin:12px 0 10px}
.rx-rail{display:flex;gap:14px;overflow-x:auto;padding:2px 2px 10px;scroll-snap-type:x mandatory}
.rx-rail .rx-item{min-width:210px;scroll-snap-align:start}
.rx-item{border:0;padding:0;background:transparent;color:inherit;cursor:pointer;text-align:left;
 transition:transform .55s var(--bounce)}
.rx-item:hover{transform:translateY(-3px)}
.rx-item:active{transform:scale(.97)}
.rx-poster{position:relative;aspect-ratio:2/3;border-radius:20px;overflow:hidden;background:var(--inset);
 border:1px solid var(--rim);display:grid;place-items:center}
.rx-rail .rx-poster{aspect-ratio:16/9}
.rx-poster img{width:100%;height:100%;object-fit:cover;display:block}
.rx-poster .ph{font:800 34px/1 var(--dotf);color:var(--muted);text-transform:uppercase;padding:10px;text-align:center}
.rx-poster .tag{position:absolute;left:8px;top:8px;background:var(--red);color:#fff;border-radius:999px;
 padding:3px 9px;font:9.5px var(--mono);letter-spacing:.1em;text-transform:uppercase}
.rx-poster .warn{position:absolute;right:8px;top:8px;background:var(--glass-strong);border-radius:999px;
 padding:3px 9px;font:9.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.rx-poster .bar{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(127,127,127,.35)}
.rx-poster .bar i{display:block;height:100%;background:var(--red)}
.rx-item b{display:block;font-size:13.5px;margin:8px 2px 0;line-height:1.3}
.rx-item span{display:block;font:10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:3px 2px}

/* the player, over everything */
.rx-player{position:fixed;inset:0;z-index:40;background:#000;display:flex;flex-direction:column}
.rx-video{flex:1;width:100%;height:100%;min-height:0;background:#000}
.rx-pbar{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 16px}
.rx-pbar b{font-size:14px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rx-pact{display:flex;gap:8px;flex:none}

/* episode list in the app's own side sheet */
.rx-eps-list,.rx-fields{display:flex;flex-direction:column;gap:6px;overflow-y:auto}
.rx-ep{display:flex;gap:12px;align-items:baseline;border:0;background:transparent;color:inherit;cursor:pointer;
 text-align:left;padding:11px 12px;border-radius:16px}
.rx-ep:hover{background:var(--inset)}
.rx-ep i{font:10.5px var(--mono);letter-spacing:.08em;color:var(--red);font-style:normal;flex:none}
.rx-ep.done i{color:var(--muted)}
.rx-kinds{display:flex;flex-direction:column;gap:8px}
.rx-kind{text-align:left;border:1px solid var(--line);background:transparent;color:inherit;border-radius:20px;
 padding:14px 16px;cursor:pointer;transition:transform .5s var(--bounce),background-color .3s}
.rx-kind:hover{background:var(--inset);transform:translateY(-1px)}
.rx-kind b{display:block;font-size:14px}
.rx-kind span{display:block;color:var(--muted);font-size:12.5px;margin-top:4px}
.rx-field{display:flex;flex-direction:column;gap:6px;margin:10px 0}
.rx-field label{font:10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.rx-field input,.rx-field select{border:1px solid var(--line);background:var(--inset);color:var(--text);
 border-radius:14px;padding:11px 14px;font:13px var(--mono)}
.rx-field input:focus,.rx-field select:focus{outline:none;border-color:var(--red)}

/* music */
.rx-now{border-radius:28px;padding:20px 24px;margin:14px 0;display:grid;gap:14px;
 grid-template-columns:1fr auto;align-items:center}
.rx-now-title{display:block;font:800 26px/1.05 var(--dotf);text-transform:uppercase;letter-spacing:.01em}
.rx-now-artist{font:10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase}
.rx-now-ctl{display:flex;gap:6px;align-items:center}
.rx-ctl{border:1px solid var(--line);background:transparent;color:var(--text);width:42px;height:42px;
 border-radius:999px;display:grid;place-items:center;cursor:pointer;transition:transform .5s var(--bounce),background-color .3s}
.rx-ctl svg{width:17px;height:17px}
.rx-ctl:hover{background:var(--inset)}
.rx-ctl:active{transform:scale(.9)}
.rx-play{background:var(--text);color:var(--bg);border-color:transparent;width:48px;height:48px}
.rx-ctl.on{background:var(--red);color:#fff;border-color:transparent}
.rx-seek{grid-column:1/-1;display:flex;align-items:center;gap:12px}
.rx-seek span{font:10.5px var(--mono);color:var(--muted);min-width:42px}
.rx-seek span:last-child{text-align:right}
input[type=range].rx-seek-in,input[type=range].rx-vol{flex:1;-webkit-appearance:none;appearance:none;height:3px;
 border-radius:999px;background:var(--line);cursor:pointer}
input[type=range].rx-seek-in::-webkit-slider-thumb,input[type=range].rx-vol::-webkit-slider-thumb{
 -webkit-appearance:none;width:14px;height:14px;border-radius:999px;background:var(--red);border:0}
.rx-tracks{display:flex;flex-direction:column;margin:4px 0 10px}
.rx-track{display:grid;grid-template-columns:30px 1fr auto;gap:12px;align-items:center;border:0;background:transparent;
 color:inherit;text-align:left;cursor:pointer;padding:12px 14px;border-radius:18px;border-bottom:1px solid var(--line)}
.rx-track:last-child{border-bottom:0}
.rx-track:hover{background:var(--inset)}
.rx-track i{font:10.5px var(--mono);color:var(--muted);font-style:normal}
.rx-track.on i,.rx-track.on b{color:var(--red)}
.rx-track b{font-weight:500;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rx-track span{font:10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}

/* the online player, sitting in the tab rather than in a window of its own */
.rx-online{border-radius:28px;padding:20px 24px;margin:10px 0 4px;display:flex;flex-direction:column;gap:12px}
.rx-link-row{display:flex;gap:8px;flex-wrap:wrap}
.rx-link,.rx-wlink{flex:1 1 320px;min-width:0;border:1px solid var(--line);background:var(--inset);
 color:var(--text);border-radius:999px;padding:12px 18px;font:12.5px var(--mono)}
.rx-link:focus,.rx-wlink:focus{outline:none;border-color:var(--red)}
.rx-link::placeholder,.rx-wlink::placeholder{color:var(--muted)}
.rx-link-row .btn{flex:0 0 auto;border:0;cursor:pointer}
.rx-link-note{margin:0;color:var(--muted)}
.rx-embed-wrap{display:flex;flex-direction:column;gap:10px}
.rx-embed{width:100%;height:352px;border:0;border-radius:20px;background:var(--inset)}
.rx-embed.small{height:172px}
.rx-wembed{height:min(58vh,460px)}
.rx-serv[data-inside="0"]{border-style:dashed;color:var(--muted)}
.rx-embed-act{display:flex;align-items:center;justify-content:space-between;gap:12px}
.rx-embed-act b{font:10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:400}

/* chill */
.rx-mix{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px;margin:10px 0}
.rx-sound{border:1px solid var(--line);border-radius:22px;padding:16px 18px;display:flex;flex-direction:column;gap:10px;
 transition:border-color .3s,background-color .3s}
.rx-sound.on{border-color:var(--red);background:rgba(215,25,33,.06)}
.rx-sound b{font:11.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;font-weight:400}
.rx-sound.on b{color:var(--red)}
.rx-wave{height:28px;width:100%}
.rx-wave path{fill:none;stroke:var(--muted);stroke-width:1.4}
.rx-sound.on .rx-wave path{stroke:var(--red)}
.rx-timer{border-radius:28px;padding:22px 26px;margin:12px 0;display:flex;flex-wrap:wrap;gap:16px;align-items:center}
.rx-clock .rx-left{font:800 56px/0.9 var(--dotf);letter-spacing:.02em}
.rx-timer-set{display:flex;gap:8px;flex-wrap:wrap}
.rx-timer .note{flex-basis:100%;margin:0;color:var(--muted)}
.rx-breathe{border-radius:28px;padding:26px;margin:12px 0;display:flex;flex-direction:column;align-items:center;gap:14px}
.rx-orb{width:140px;height:140px;display:grid;place-items:center}
.rx-orb i{display:block;width:70px;height:70px;border-radius:999px;border:1px solid var(--red);
 background:radial-gradient(circle at 50% 40%,rgba(215,25,33,.3),transparent 70%)}
.rx-orb.go i{animation:rxBreathe 8s ease-in-out infinite}
@keyframes rxBreathe{0%,100%{transform:scale(.72)}50%{transform:scale(1.5)}}

/* gallery */
.rx-seg{display:flex;gap:2px;padding:3px;border-radius:999px;background:var(--inset);width:max-content;margin:12px 0}
.rx-seg button{border:0;background:transparent;color:var(--muted);border-radius:999px;padding:8px 18px;cursor:pointer;
 font:11.5px var(--mono);letter-spacing:.1em;text-transform:uppercase}
.rx-seg button.on{background:var(--glass-strong);color:var(--text)}
.rx-photos{columns:3 190px;column-gap:12px;margin:12px 0}
.rx-photo{width:100%;border-radius:18px;display:block;margin:0 0 12px;cursor:zoom-in;border:1px solid var(--rim);
 transition:transform .55s var(--bounce)}
.rx-photo:hover{transform:scale(1.015)}
.rx-light{position:fixed;inset:0;z-index:45;background:rgba(0,0,0,.92);display:flex;align-items:center;
 justify-content:center;gap:10px;padding:28px}
.rx-light-img{max-width:88vw;max-height:84vh;border-radius:14px}
.rx-light-nav{border:0;background:transparent;color:#fff;font-size:44px;line-height:1;cursor:pointer;padding:0 10px;opacity:.6}
.rx-light-nav:hover{opacity:1}
.rx-light-close{position:absolute;top:18px;right:18px;color:#fff;border-color:rgba(255,255,255,.3)}
.rx-draw-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border-radius:999px;padding:10px 14px;margin:12px 0}
.rx-swatches{display:flex;gap:6px}
.rx-sw{width:22px;height:22px;border-radius:999px;border:1px solid var(--rim);cursor:pointer;padding:0}
.rx-sw.on{outline:2px solid var(--red);outline-offset:2px}
.rx-size{width:110px}
.rx-canvas{width:100%;height:min(62vh,540px);border-radius:28px;border:1px solid var(--line);
 background:var(--glass);touch-action:none;display:block}
"""


RX_JS = r"""
/* Relax mode. Everything in here is inert until the mode is switched on. */
(() => {
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = () => (window.pywebview && window.pywebview.api) || null;
const R = document.documentElement;
const RELAX_TABS = ['watch', 'music', 'chill', 'gallery'];
const store = (k, v) => { try { v === undefined ? delete localStorage[k] : (localStorage[k] = v); } catch (_) {} };
const read = k => { try { return localStorage[k]; } catch (_) { return undefined; } };
const mmss = s => (s = Math.max(0, Math.round(s || 0)), `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`);

// ---------------------------------------------------------------- the switch

let mode = read('bbMode') === 'relax' ? 'relax' : 'study';
const studyTab = () => read('bbStudyTab') || 'week';

function setMode(next, from) {
  if (next === mode) return;
  const was = mode;
  mode = next;
  store('bbMode', next);
  if (was === 'study') store('bbStudyTab', read('tab') || 'week');
  const wipe = document.createElement('div');
  wipe.className = 'rx-wipe';
  wipe.innerHTML = `<b>${next === 'relax' ? 'Relax' : 'Study'}</b>`;
  if (from) {
    const r = from.getBoundingClientRect();
    wipe.style.setProperty('--x', `${r.left + r.width / 2}px`);
    wipe.style.setProperty('--y', `${r.top + r.height / 2}px`);
  }
  document.body.appendChild(wipe);
  requestAnimationFrame(() => wipe.classList.add('go'));
  setTimeout(() => {                      // swap underneath, while the sweep covers the window
    R.classList.toggle('relax', next === 'relax');
    const want = next === 'relax' ? (RELAX_TABS.includes(read('rxTab')) ? read('rxTab') : 'watch') : studyTab();
    try { window.bbShow(want, true); } catch (_) {}
    try { window.bbPlaceInd && window.bbPlaceInd(); } catch (_) {}
    if (next === 'relax') wake();
    wipe.classList.remove('go');
    wipe.classList.add('out');
    setTimeout(() => wipe.remove(), 520);
  }, 440);
}

addEventListener('DOMContentLoaded', () => {
  const btn = $('.rx-toggle');
  if (btn) btn.onclick = () => setMode(mode === 'relax' ? 'study' : 'relax', btn);
  if (mode === 'relax') {
    R.classList.add('relax');
    const want = RELAX_TABS.includes(read('rxTab')) ? read('rxTab') : 'watch';
    try { window.bbShow(want, true); } catch (_) {}
    wake();
  } else if (RELAX_TABS.includes(read('tab'))) {
    // started in study mode on a tab that only exists in relax: the page would come up with
    // nothing showing and no tab lit, so put it back on a school one
    try { window.bbShow(studyTab(), true); } catch (_) {}
  }
  // remember which tab each mode was left on
  document.addEventListener('click', ev => {
    const b = ev.target.closest('nav button[data-tab]');
    if (!b) return;
    store(RELAX_TABS.includes(b.dataset.tab) ? 'rxTab' : 'bbStudyTab', b.dataset.tab);
  }, true);
});
addEventListener('keydown', ev => {
  if (ev.altKey && !ev.ctrlKey && !ev.metaKey && (ev.key === 'r' || ev.key === 'R')) {
    if (ev.target.closest('input,textarea,[contenteditable="true"]')) return;
    ev.preventDefault();
    setMode(mode === 'relax' ? 'study' : 'relax', $('.rx-toggle'));
  }
});

// the API is injected after the page loads, so nothing may ask for data before it is there:
// starting in relax mode used to leave every tab empty until the window was reloaded.
const ready = () => new Promise(done => {
  if (api()) return done();
  addEventListener('pywebviewready', () => done(), { once: true });
  const t = setInterval(() => { if (api()) { clearInterval(t); done(); } }, 120);
  setTimeout(() => { clearInterval(t); done(); }, 15000);
});

let woken = false;
async function wake() {
  if (woken) return;
  woken = true;
  await ready();
  watchInit(); musicInit(); chillInit(); galleryInit();
}

// ---------------------------------------------------------------- Watch

const W = { sources: [], current: null, rows: [], progress: {}, queue: [], at: -1 };

async function watchInit() {
  const a = api(); if (!a) return;
  const st = await a.rx_state();
  W.sources = st.sources || [];
  W.progress = st.progress || {};
  drawSources();
  drawResume();
  if (W.sources.length) pickSource(W.sources[0].id);
}

function drawSources() {
  const wrap = $('.rx-srcs'); if (!wrap) return;
  wrap.innerHTML = '';
  for (const s of W.sources) {
    const b = document.createElement('button');
    b.className = 'rx-src' + (W.current === s.id ? ' on' : '');
    b.dataset.id = s.id;
    b.innerHTML = `<span>${esc(s.name)}</span><span class="x" title="Remove">✕</span>`;
    b.onclick = ev => ev.target.classList.contains('x') ? removeSource(s) : pickSource(s.id);
    wrap.appendChild(b);
  }
  const add = document.createElement('button');
  add.className = 'rx-src rx-add-src-pill';
  add.textContent = '+ Add source';
  add.onclick = () => openNewSource();
  wrap.appendChild(add);
  $('.rx-watch-empty').hidden = W.sources.length > 0;
  $('.rx-search').hidden = W.sources.length === 0;
}

async function removeSource(s) {
  if (!confirm(`Remove ${s.name}? Nothing on your computer is deleted.`)) return;
  await api().rx_source_remove(s.id);
  W.sources = W.sources.filter(x => x.id !== s.id);
  if (W.current === s.id) { W.current = null; $('.rx-watch-grid').innerHTML = ''; }
  drawSources();
}

async function pickSource(id, query) {
  W.current = id;
  drawSources();
  const grid = $('.rx-watch-grid'), note = $('.rx-note');
  grid.innerHTML = '<p class="sub">Reading…</p>';
  note.hidden = true;
  const res = await api().rx_catalogue(id, query || '');
  if (res && res.error) { grid.innerHTML = ''; note.hidden = false; note.textContent = res.error; return; }
  W.rows = res || [];
  grid.innerHTML = '';
  if (!W.rows.length) { note.hidden = false; note.textContent = 'Nothing found in that source.'; return; }
  for (const row of W.rows) grid.appendChild(card(row));
}

function card(row) {
  const b = document.createElement('button');
  b.className = 'rx-item';
  const p = W.progress[row.id || row.path] || null;
  const pct = p && p.d ? Math.min(100, Math.round(p.t / p.d * 100)) : 0;
  b.innerHTML = `<div class="rx-poster">
    ${row.poster ? `<img loading="lazy" src="${esc(row.poster)}" alt="">` : `<span class="ph">${esc(initials(row.title))}</span>`}
    ${row.kind === 'series' ? `<span class="tag">${row.count || ''} ${row.count === 1 ? 'ep' : 'eps'}</span>` : ''}
    ${row.plays === false ? '<span class="warn">no preview</span>' : ''}
    ${pct ? `<span class="bar"><i style="width:${pct}%"></i></span>` : ''}
  </div><b>${esc(row.title)}</b><span>${esc(row.year || (row.kind === 'series' ? 'Series' : 'Film'))}</span>`;
  b.onclick = () => row.kind === 'series' ? openEpisodes(row) : play(row, [row], 0);
  return b;
}

const initials = t => (String(t || '?').trim()[0] || '?').toUpperCase();
const esc = t => String(t == null ? '' : t).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function openEpisodes(row) {
  const sheet = $('.rx-eps'), list = $('.rx-eps-list');
  $('.rx-eps-title').textContent = row.title;
  list.innerHTML = '<p class="sub">Reading…</p>';
  sheet.classList.add('on');
  let eps = row.episodes;
  if (!eps && row.lazy) eps = await api().rx_episodes(W.current, row.lazy);
  eps = eps || [];
  list.innerHTML = '';
  eps.forEach((ep, i) => {
    const p = W.progress[ep.id || ep.path];
    const b = document.createElement('button');
    b.className = 'rx-ep' + (p && p.d && p.t / p.d > 0.92 ? ' done' : '');
    b.innerHTML = `<i>${esc(ep.label || '')}</i><span>${esc(ep.title || ep.file || '')}</span>`;
    b.onclick = () => { sheet.classList.remove('on'); play(ep, eps, i); };
    list.appendChild(b);
  });
}

async function play(row, queue, at) {
  const v = $('.rx-video'), box = $('.rx-player');
  let url = row.url;
  if (!url && row.resolve) {
    const r = await api().rx_resolve(W.current, row.resolve);
    if (r && r.error) return alert(r.error);
    url = r.url;
  }
  if (!url) return;
  if (row.plays === false &&
      !confirm(`${row.ext ? '.' + row.ext : 'This file'} usually will not play in this window. Try anyway?`)) return;
  W.queue = queue || [row]; W.at = at || 0;
  box.hidden = false;
  $('.rx-ptitle').textContent = row.title || row.file || '';
  $('.rx-next-ep').hidden = !(W.queue.length > W.at + 1);
  $('.rx-prev-ep').hidden = !(W.at > 0);
  [...v.querySelectorAll('track')].forEach(t => t.remove());
  const key = row.id || row.path;
  const p = W.progress[key];
  // handlers first: a file the window cannot read fires its events the moment the source is set
  v.onloadedmetadata = () => { if (p && p.t && v.duration && p.t < v.duration - 20) v.currentTime = p.t; };
  v.onerror = () => {
    const what = v.error && v.error.code === 4 ? 'This window cannot decode that file'
                                               : 'That file would not play';
    $('.rx-ptitle').textContent = `${what} — ${row.title || row.file || ''}`;
  };
  // some files report no duration at all, and then say they have ended the instant they load.
  // Only a real ending - time actually passed, and at the end of what there is - moves on.
  v.onended = () => {
    const known = isFinite(v.duration) && v.duration > 0;
    const done = known ? v.currentTime >= v.duration - 1.5 : v.currentTime > 1;
    if (!done) return;
    W.queue.length > W.at + 1 ? play(W.queue[W.at + 1], W.queue, W.at + 1) : closePlayer();
  };
  v.src = url;
  if (row.path) {
    const sub = await api().rx_subtitles(row.path);
    if (sub && sub.url) {
      const t = document.createElement('track');
      t.kind = 'subtitles'; t.label = 'Subtitles'; t.default = true; t.src = sub.url;
      v.appendChild(t);
    }
  }
  v.play().catch(() => {});
  let last = 0;
  v.ontimeupdate = () => {
    if (!v.duration || v.currentTime - last < 5) return;
    last = v.currentTime;
    W.progress[key] = { t: v.currentTime, d: v.duration, title: row.title || row.file, at: Date.now() };
    api().rx_progress(key, v.currentTime, v.duration, row.title || row.file || '', row.poster || '');
  };
}

function closePlayer() {
  const v = $('.rx-video');
  v.pause(); v.removeAttribute('src'); v.load();
  $('.rx-player').hidden = true;
  drawResume();
}

function drawResume() {
  const box = $('.rx-resume'); if (!box) return;
  const rows = Object.entries(W.progress)
    .filter(([, p]) => p.d && p.t / p.d > 0.02 && p.t / p.d < 0.95)
    .sort((a, b) => b[1].at - a[1].at).slice(0, 12);
  box.hidden = !rows.length;
  const rail = $('.rx-rail', box);
  rail.innerHTML = '';
  for (const [key, p] of rows) {
    const b = document.createElement('button');
    b.className = 'rx-item';
    const pct = Math.round(p.t / p.d * 100);
    b.innerHTML = `<div class="rx-poster">${p.poster ? `<img src="${esc(p.poster)}" alt="">`
      : `<span class="ph">${esc(initials(p.title))}</span>`}
      <span class="bar"><i style="width:${pct}%"></i></span></div>
      <b>${esc(p.title || '')}</b><span>${mmss(p.d - p.t)} left</span>`;
    b.onclick = () => {
      const row = W.rows.find(r => (r.id || r.path) === key)
        || (W.rows.flatMap(r => r.episodes || []).find(r => (r.id || r.path) === key));
      if (row) play(row, [row], 0); else alert('That one came from a source that is not open. Pick its source first.');
    };
    rail.appendChild(b);
  }
}

// Watch, in the tab. Services that allow being shown inside a page are loaded straight in;
// the rest say so plainly and offer the window, rather than leaving an empty black box.
function watchEmbedFor(link) {
  let u;
  try { u = new URL(String(link).trim()); } catch (_) { return null; }
  const host = u.hostname.replace(/^www\./, ''), path = u.pathname;
  if (host.endsWith('youtube.com')) {
    const list = u.searchParams.get('list'), v = u.searchParams.get('v');
    if (v) return { url: `https://www.youtube.com/embed/${v}${list ? '?list=' + list : ''}`, what: 'YouTube' };
    if (list) return { url: `https://www.youtube.com/embed/videoseries?list=${list}`, what: 'YouTube playlist' };
    if (path.startsWith('/embed/')) return { url: u.href, what: 'YouTube' };
    return { url: 'https://www.youtube.com/embed?listType=user_uploads', what: 'YouTube' };
  }
  if (host === 'youtu.be' && path.length > 1)
    return { url: `https://www.youtube.com/embed/${path.slice(1)}`, what: 'YouTube' };
  if (host === 'vimeo.com' && /^\/\d+/.test(path))
    return { url: `https://player.vimeo.com/video/${path.split('/')[1]}`, what: 'Vimeo' };
  if (host.endsWith('dailymotion.com')) {
    const id = path.split('/video/')[1];
    if (id) return { url: `https://www.dailymotion.com/embed/video/${id}`, what: 'Dailymotion' };
  }
  if (host === 'dai.ly' && path.length > 1)
    return { url: `https://www.dailymotion.com/embed/video/${path.slice(1)}`, what: 'Dailymotion' };
  if (host.endsWith('archive.org')) {
    const id = (path.match(/\/(?:details|embed|download)\/([^/]+)/) || [])[1];
    if (id) return { url: `https://archive.org/embed/${id}`, what: 'Internet Archive' };
  }
  if (host.endsWith('twitch.tv')) {
    const chan = path.split('/')[1];
    if (chan) return { url: `https://player.twitch.tv/?channel=${chan}&parent=localhost&parent=null`,
                       what: 'Twitch' };
  }
  return null;
}

function watchHere(link, fallbackUrl, fallbackName) {
  const got = watchEmbedFor(link);
  const note = $('.rx-wlink-note'), win = $('.rx-wembed-window');
  if (!got) {
    note.textContent = fallbackName
      ? `${fallbackName} does not allow its pages to be shown inside another app. ` +
        'Everything else here plays in this tab; this one needs a window.'
      : "That link isn't one this can play here: YouTube, Vimeo, Dailymotion, Internet Archive or Twitch.";
    win.hidden = !fallbackUrl;
    win.onclick = () => api() && api().open_inside(fallbackUrl, fallbackName || '');
    if (fallbackUrl) { $('.rx-wembed-wrap').hidden = false; $('.rx-wembed').src = 'about:blank';
                       $('.rx-wembed-what').textContent = fallbackName || ''; }
    return false;
  }
  const v = $('.rx-video');
  if (v && !v.paused) { v.pause(); $('.rx-player').hidden = true; }
  $('.rx-wembed').src = got.url;
  $('.rx-wembed-what').textContent = got.what;
  $('.rx-wembed-wrap').hidden = false;
  win.hidden = true;
  note.textContent = 'Playing in this tab. Sign in on the service if it asks.';
  store('rxWatchEmbed', link);
  return true;
}

// adding a source
function openNewSource() {
  const sheet = $('.rx-newsrc');
  $('.rx-fields', sheet).hidden = true;
  $('.rx-kinds', sheet).hidden = false;
  sheet.classList.add('on');
}

async function kindForm(kind) {
  const sheet = $('.rx-newsrc'), box = $('.rx-fields', sheet);
  const kinds = await api().rx_kinds();
  const def = kinds.find(k => k.kind === kind);
  $('.rx-kinds', sheet).hidden = true;
  box.hidden = false;
  box.innerHTML = `<p class="note">${esc(def.note)}</p>`;
  for (const f of def.fields) {
    const row = document.createElement('div');
    row.className = 'rx-field';
    if (f.type === 'folder') {
      row.innerHTML = `<label>${esc(f.label)}</label><input data-k="${f.key}" readonly placeholder="No folder chosen">`;
      const pick = document.createElement('button');
      pick.className = 'ghost';
      pick.textContent = 'Choose folder…';
      pick.onclick = async () => {
        const r = await api().rx_pick_folder();
        if (r && r.path) $(`[data-k="${f.key}"]`, row).value = r.path;
      };
      row.appendChild(pick);
    } else if (f.type === 'choice') {
      row.innerHTML = `<label>${esc(f.label)}</label><select data-k="${f.key}">` +
        f.choices.map(c => `<option value="${esc(c)}">${esc(c.replace(/_/g, ' '))}</option>`).join('') + '</select>';
    } else {
      row.innerHTML = `<label>${esc(f.label)}</label><input data-k="${f.key}" type="${f.type === 'password' ? 'password' : 'text'}"
        placeholder="${esc(f.hint || '')}">`;
    }
    box.appendChild(row);
  }
  const save = document.createElement('button');
  save.className = 'btn';
  save.textContent = 'Add source';
  save.onclick = async () => {
    const values = {};
    $$('[data-k]', box).forEach(i => values[i.dataset.k] = i.value);
    save.disabled = true;
    const res = await api().rx_source_add(kind, values);
    save.disabled = false;
    if (res && res.error) return alert(res.error);
    W.sources.push(res);
    sheet.classList.remove('on');
    drawSources();
    pickSource(res.id);
  };
  box.appendChild(save);
}

// ---------------------------------------------------------------- Music

const M = { rows: [], at: -1, shuffle: false, audio: null };

function musicInit() {
  M.audio = new Audio();
  M.audio.preload = 'metadata';
  M.audio.onended = () => step(1);
  M.audio.ontimeupdate = () => {
    if (!M.audio.duration) return;
    $('.rx-seek-in').value = String(M.audio.currentTime / M.audio.duration * 1000);
    $('.rx-at').textContent = mmss(M.audio.currentTime);
    $('.rx-dur').textContent = mmss(M.audio.duration);
  };
  $('.rx-play').onclick = () => M.audio.paused ? (M.at < 0 ? playTrack(0) : M.audio.play()) : M.audio.pause();
  M.audio.onplay = M.audio.onpause = () => {
    const on = !M.audio.paused;
    $('.i-play').hidden = on; $('.i-pause').hidden = !on;
  };
  $('.rx-prev').onclick = () => step(-1);
  $('.rx-next').onclick = () => step(1);
  $('.rx-shuffle').onclick = ev => { M.shuffle = !M.shuffle; ev.currentTarget.classList.toggle('on', M.shuffle); };
  $('.rx-seek-in').oninput = ev => { if (M.audio.duration) M.audio.currentTime = ev.target.value / 1000 * M.audio.duration; };
  const link = $('.rx-link');
  $('.rx-link-go').onclick = () => playEmbed(link.value);
  link.onkeydown = ev => { if (ev.key === 'Enter') playEmbed(link.value); };
  $('.rx-membed-close').onclick = () => {
    $('.rx-membed').src = 'about:blank';
    $('.rx-membed-wrap').hidden = true;
    store('rxEmbed', undefined);
  };
  // Signing in is the part this app cannot do for you, and should not: it happens on Spotify's
  // own page. The window keeps its own cookies, so once is enough, and the player in this tab is
  // then the same signed-in you. Full tracks still need Premium; a free account gets previews.
  $('.rx-signin-sp').onclick = () => {
    api() && api().open_inside('https://accounts.spotify.com/login', 'Spotify');
    $('.rx-link-note').textContent = 'Sign in on the Spotify window, then close it and press ' +
      'Reload player. Full tracks need Premium; a free account plays 30-second previews.';
    $('.rx-reload-player').hidden = false;
  };
  $('.rx-reload-player').onclick = () => {
    const url = $('.rx-membed').src;
    if (!url || url === 'about:blank') return;
    $('.rx-membed').src = 'about:blank';
    setTimeout(() => { $('.rx-membed').src = url; }, 80);
  };
  const last = read('rxEmbed');
  if (last) { link.value = last; playEmbed(last); }
  loadMusic();
}

async function loadMusic() {
  const res = await api().rx_library('audio');
  M.rows = (res && res.rows) || [];
  $('.rx-audio-empty').hidden = M.rows.length > 0 || !!(res && res.folder);
  $('.rx-tracks-label').hidden = !M.rows.length;
  const box = $('.rx-tracks');
  box.innerHTML = '';
  M.rows.forEach((t, i) => {
    const b = document.createElement('button');
    b.className = 'rx-track';
    b.innerHTML = `<i>${String(i + 1).padStart(2, '0')}</i><b>${esc(t.title)}</b><span>${esc(t.artist || '')}</span>`;
    b.onclick = () => playTrack(i);
    box.appendChild(b);
  });
}

// Each service has an official embed player. Turning a normal link into one keeps playback here
// in the tab, and keeps it on the service's own player rather than pulling the audio out of it.
function embedFor(link) {
  let u;
  try { u = new URL(link.trim()); } catch (_) { return null; }
  const host = u.hostname.replace(/^www\./, ''), path = u.pathname;
  if (host === 'open.spotify.com') {
    const m = path.match(/\/(track|album|playlist|artist|show|episode)\/([A-Za-z0-9]+)/);
    if (m) return { url: `https://open.spotify.com/embed/${m[1]}/${m[2]}`, what: `Spotify ${m[1]}`,
                    small: m[1] === 'track' || m[1] === 'episode' };
  }
  if (host === 'music.youtube.com' || host === 'youtube.com' || host === 'm.youtube.com') {
    const list = u.searchParams.get('list'), v = u.searchParams.get('v');
    if (list) return { url: `https://www.youtube.com/embed/videoseries?list=${list}`, what: 'YouTube playlist' };
    if (v) return { url: `https://www.youtube.com/embed/${v}`, what: 'YouTube' };
  }
  if (host === 'youtu.be' && path.length > 1)
    return { url: `https://www.youtube.com/embed/${path.slice(1)}`, what: 'YouTube' };
  if (host === 'soundcloud.com')
    return { url: 'https://w.soundcloud.com/player/?url=' + encodeURIComponent(u.href) +
                  '&color=%23d71921&auto_play=true', what: 'SoundCloud', small: true };
  if (host === 'music.apple.com')
    return { url: u.href.replace('music.apple.com', 'embed.music.apple.com'), what: 'Apple Music' };
  if (host.endsWith('bandcamp.com'))
    return { url: u.href, what: 'Bandcamp' };
  return null;
}

function playEmbed(link) {
  const got = embedFor(link);
  const note = $('.rx-link-note');
  if (!got) {
    note.textContent = "That link isn't one of the services this can play: Spotify, YouTube, " +
      'SoundCloud, Apple Music or Bandcamp.';
    return false;
  }
  if (M.audio && !M.audio.paused) M.audio.pause();   // two things playing at once helps nobody
  const frame = $('.rx-membed');
  frame.classList.toggle('small', !!got.small);
  frame.src = got.url;
  $('.rx-membed-what').textContent = got.what;
  $('.rx-membed-wrap').hidden = false;
  note.textContent = 'Playing in this tab. Only hearing 30 seconds? That is the service asking ' +
    'who you are - sign in to Spotify below, then press Reload player.';
  $('.rx-reload-player').hidden = false;
  store('rxEmbed', link);
  return true;
}

function playTrack(i) {
  if (!M.rows[i]) return;
  M.at = i;
  if (!$('.rx-membed-wrap').hidden) $('.rx-membed-close').click();
  M.audio.src = M.rows[i].url;
  M.audio.play().catch(() => {});
  $('.rx-now').hidden = false;
  $('.rx-now-title').textContent = M.rows[i].title;
  $('.rx-now-artist').textContent = M.rows[i].artist || '';
  $$('.rx-track').forEach((b, j) => b.classList.toggle('on', i === j));
}

function step(d) {
  if (!M.rows.length) return;
  const next = M.shuffle ? Math.floor(Math.random() * M.rows.length)
                         : (M.at + d + M.rows.length) % M.rows.length;
  playTrack(next);
}

// ---------------------------------------------------------------- Chill

const SOUNDS = [
  { key: 'rain', name: 'Rain' }, { key: 'waves', name: 'Waves' }, { key: 'wind', name: 'Wind' },
  { key: 'fire', name: 'Fire' }, { key: 'hum', name: 'Room hum' },
];
const C = { ctx: null, nodes: {}, timer: null, ends: 0 };

function chillInit() {
  const mix = $('.rx-mix');
  if (!mix) return;
  mix.innerHTML = '';
  for (const s of SOUNDS) {
    const d = document.createElement('div');
    d.className = 'rx-sound';
    d.dataset.sound = s.key;
    d.innerHTML = `<svg class="rx-wave" viewBox="0 0 120 28" preserveAspectRatio="none" aria-hidden="true">
      <path d="${wavePath(s.key)}"/></svg><b>${s.name}</b>
      <input type="range" class="rx-vol" min="0" max="100" value="0" aria-label="${s.name} volume">`;
    $('.rx-vol', d).oninput = ev => setSound(s.key, ev.target.value / 100, d);
    mix.appendChild(d);
  }
  $$('.rx-mins').forEach(b => b.onclick = () => startTimer(+b.dataset.m));
  $('.rx-stop-timer').onclick = stopTimer;
  const orb = $('.rx-orb'), go = $('.rx-breathe-go');
  go.onclick = () => {
    const on = orb.classList.toggle('go');
    go.textContent = on ? 'Stop' : 'Start';
  };
}

function wavePath(kind) {
  let d = 'M0 14';
  for (let x = 0; x <= 120; x += 3) {
    const n = kind === 'rain' ? Math.random() * 10 - 5
      : kind === 'waves' ? Math.sin(x / 14) * 7
      : kind === 'wind' ? Math.sin(x / 9) * 4 + Math.sin(x / 3) * 1.5
      : kind === 'fire' ? Math.random() * 6 - 3 + Math.sin(x / 7) * 2
      : Math.sin(x / 5) * 1.6;
    d += ` L${x} ${(14 + n).toFixed(1)}`;
  }
  return d;
}

function noiseBuffer(ctx, brown) {
  const len = ctx.sampleRate * 4;
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const data = buf.getChannelData(0);
  let last = 0;
  for (let i = 0; i < len; i++) {
    const white = Math.random() * 2 - 1;
    if (brown) { last = (last + 0.02 * white) / 1.02; data[i] = last * 3.2; }
    else data[i] = white;
  }
  return buf;
}

function buildSound(key) {
  const ctx = C.ctx;
  const src = ctx.createBufferSource();
  src.buffer = noiseBuffer(ctx, key === 'waves' || key === 'hum');
  src.loop = true;
  const filter = ctx.createBiquadFilter();
  const gain = ctx.createGain();
  gain.gain.value = 0;
  if (key === 'rain') { filter.type = 'highpass'; filter.frequency.value = 1100; }
  else if (key === 'waves') { filter.type = 'lowpass'; filter.frequency.value = 420; }
  else if (key === 'wind') { filter.type = 'bandpass'; filter.frequency.value = 600; filter.Q.value = 0.6; }
  else if (key === 'fire') { filter.type = 'lowpass'; filter.frequency.value = 1600; }
  else { filter.type = 'lowpass'; filter.frequency.value = 180; }
  src.connect(filter).connect(gain).connect(ctx.destination);
  src.start();
  if (key === 'waves' || key === 'wind' || key === 'fire') {
    // slow swell, so it never sits perfectly still
    const lfo = ctx.createOscillator(), depth = ctx.createGain();
    lfo.frequency.value = key === 'waves' ? 0.09 : key === 'wind' ? 0.16 : 0.8;
    depth.gain.value = 0.45;
    lfo.connect(depth).connect(gain.gain);
    lfo.start();
  }
  return gain;
}

function setSound(key, level, el) {
  if (!C.ctx) C.ctx = new (window.AudioContext || window.webkitAudioContext)();
  if (C.ctx.state === 'suspended') C.ctx.resume();
  if (!C.nodes[key]) C.nodes[key] = buildSound(key);
  C.nodes[key].gain.setTargetAtTime(level * 0.6, C.ctx.currentTime, 0.2);
  el.classList.toggle('on', level > 0.01);
}

function startTimer(mins) {
  stopTimer();
  C.ends = Date.now() + mins * 60000;
  C.timer = setInterval(() => {
    const left = Math.max(0, C.ends - Date.now()) / 1000;
    $('.rx-left').textContent = `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(Math.floor(left % 60)).padStart(2, '0')}`;
    if (left <= 30 && C.ctx) for (const g of Object.values(C.nodes)) g.gain.setTargetAtTime(0, C.ctx.currentTime, 10);
    if (left <= 0) { stopTimer(); $$('.rx-vol').forEach(v => v.value = 0); $$('.rx-sound').forEach(s => s.classList.remove('on')); }
  }, 500);
}

function stopTimer() {
  clearInterval(C.timer);
  C.timer = null;
  $('.rx-left').textContent = '00:00';
}

// ---------------------------------------------------------------- Gallery

const G = { rows: [], at: 0, undo: [], colour: '#d71921', size: 6, erase: false };

function galleryInit() {
  $$('.rx-seg button').forEach(b => b.onclick = () => {
    $$('.rx-seg button').forEach(x => x.classList.toggle('on', x === b));
    $$('.rx-seg-pane').forEach(p => p.hidden = p.dataset.seg !== b.dataset.seg);
    if (b.dataset.seg === 'draw') sizeCanvas();
  });
  loadPhotos();
  drawInit();
  $('.rx-light-close').onclick = () => $('.rx-light').hidden = true;
  $('.rx-light-prev').onclick = () => lightStep(-1);
  $('.rx-light-next').onclick = () => lightStep(1);
  addEventListener('keydown', ev => {
    if ($('.rx-light').hidden) return;
    if (ev.key === 'Escape') $('.rx-light').hidden = true;
    if (ev.key === 'ArrowLeft') lightStep(-1);
    if (ev.key === 'ArrowRight') lightStep(1);
  });
}

async function loadPhotos() {
  const res = await api().rx_library('image');
  G.rows = (res && res.rows) || [];
  $('.rx-image-empty').hidden = G.rows.length > 0;
  const box = $('.rx-photos');
  box.innerHTML = '';
  G.rows.forEach((p, i) => {
    const img = document.createElement('img');
    img.className = 'rx-photo';
    img.loading = 'lazy';
    img.src = p.url;
    img.alt = p.title;
    img.onclick = () => { G.at = i; openLight(); };
    box.appendChild(img);
  });
}

function openLight() {
  $('.rx-light').hidden = false;
  $('.rx-light-img').src = G.rows[G.at].url;
}
function lightStep(d) {
  if (!G.rows.length) return;
  G.at = (G.at + d + G.rows.length) % G.rows.length;
  $('.rx-light-img').src = G.rows[G.at].url;
}

function drawInit() {
  const c = $('.rx-canvas'); if (!c) return;
  const ctx = c.getContext('2d');
  const sw = $('.rx-swatches');
  ['#d71921', '#0b0b0b', '#f2f2f2', '#2fb36b', '#2b7fff', '#ffb02e'].forEach((col, i) => {
    const b = document.createElement('button');
    b.className = 'rx-sw' + (i === 0 ? ' on' : '');
    b.style.background = col;
    b.onclick = () => {
      G.colour = col; G.erase = false;
      $$('.rx-sw').forEach(x => x.classList.toggle('on', x === b));
    };
    sw.appendChild(b);
  });
  $('.rx-size').oninput = ev => G.size = +ev.target.value;
  $('.rx-erase').onclick = ev => { G.erase = !G.erase; ev.currentTarget.classList.toggle('on', G.erase); };
  $('.rx-clear').onclick = () => { pushUndo(c); ctx.clearRect(0, 0, c.width, c.height); };
  $('.rx-undo').onclick = () => {
    const last = G.undo.pop();
    if (last) ctx.putImageData(last, 0, 0);
  };
  $('.rx-save-draw').onclick = async () => {
    const res = await api().rx_save_drawing(c.toDataURL('image/png'));
    alert(res && res.path ? `Saved to ${res.path}` : (res && res.error) || 'Could not save that.');
  };
  let drawing = false, lastPt = null;
  const pt = ev => {
    const r = c.getBoundingClientRect();
    return { x: (ev.clientX - r.left) * (c.width / r.width), y: (ev.clientY - r.top) * (c.height / r.height) };
  };
  c.addEventListener('pointerdown', ev => {
    pushUndo(c); drawing = true; lastPt = pt(ev); c.setPointerCapture(ev.pointerId);
  });
  c.addEventListener('pointermove', ev => {
    if (!drawing) return;
    const p = pt(ev);
    ctx.globalCompositeOperation = G.erase ? 'destination-out' : 'source-over';
    ctx.strokeStyle = G.colour;
    ctx.lineWidth = G.size;
    ctx.lineCap = ctx.lineJoin = 'round';
    ctx.beginPath(); ctx.moveTo(lastPt.x, lastPt.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    lastPt = p;
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(e => c.addEventListener(e, () => drawing = false));
  sizeCanvas();
  addEventListener('resize', () => { if (!$('.rx-seg-pane[data-seg=draw]').hidden) sizeCanvas(); });
}

function pushUndo(c) {
  try { G.undo.push(c.getContext('2d').getImageData(0, 0, c.width, c.height)); } catch (_) {}
  if (G.undo.length > 20) G.undo.shift();
}

function sizeCanvas() {
  const c = $('.rx-canvas'); if (!c) return;
  const keep = c.width && c.height ? c.getContext('2d').getImageData(0, 0, c.width, c.height) : null;
  const r = c.getBoundingClientRect();
  c.width = Math.round(r.width); c.height = Math.round(r.height);
  if (keep) c.getContext('2d').putImageData(keep, 0, 0);
}

// ---------------------------------------------------------------- wiring the sheets up

addEventListener('DOMContentLoaded', () => {
  document.addEventListener('click', ev => {
    const t = ev.target;
    if (t.closest('.rx-serv')) {
      const b = t.closest('.rx-serv');
      if (b.closest('#watch')) {
        b.dataset.inside === '1' ? watchHere(b.dataset.url)
                                 : watchHere('', b.dataset.url, b.dataset.name);
      } else if (b.closest('#music')) {
        // their own sites send a header forbidding this, so the player link is the way in
        $('.rx-link-note').textContent = `${b.dataset.name} does not allow its site to be shown ` +
          `inside another page. Copy a track, album or playlist link from it and paste it above: ` +
          `that plays right here.`;
        const win = $('.rx-mwindow');
        win.hidden = false;
        win.onclick = () => api() && api().open_inside(b.dataset.url, b.dataset.name);
      } else {
        api() && api().open_inside(b.dataset.url, b.dataset.name);
      }
    }
    if (t.closest('.rx-sheet-x')) t.closest('.rx-sheet').classList.remove('on');
    if (t.closest('.rx-add-src')) openNewSource();
    if (t.closest('.rx-kind')) kindForm(t.closest('.rx-kind').dataset.kind);
    if (t.closest('.rx-pclose')) closePlayer();
    if (t.closest('.rx-next-ep')) play(W.queue[W.at + 1], W.queue, W.at + 1);
    if (t.closest('.rx-prev-ep')) play(W.queue[W.at - 1], W.queue, W.at - 1);
    if (t.closest('.rx-pick')) pickFolderFor(t.closest('.rx-pick').dataset.kind);
  });
  const wlink = $('.rx-wlink');
  if (wlink) {
    $('.rx-wlink-go').onclick = () => watchHere(wlink.value);
    wlink.onkeydown = ev => { if (ev.key === 'Enter') watchHere(wlink.value); };
    $('.rx-wembed-close').onclick = () => {
      $('.rx-wembed').src = 'about:blank';
      $('.rx-wembed-wrap').hidden = true;
      store('rxWatchEmbed', undefined);
    };
    const last = read('rxWatchEmbed');
    if (last) { wlink.value = last; watchHere(last); }
  }
  const q = $('.rx-q');
  if (q) {
    let t = null;
    q.oninput = () => { clearTimeout(t); t = setTimeout(() => pickSource(W.current, q.value.trim()), 350); };
  }
});

async function pickFolderFor(kind) {
  const res = await api().rx_set_folder(kind);
  if (!res || !res.path) return;
  if (kind === 'audio') loadMusic(); else if (kind === 'image') loadPhotos();
}

window.bbRelax = { setMode: m => setMode(m, $('.rx-toggle')), mode: () => mode, embedFor, playEmbed, watchEmbedFor, watchHere };
})();
"""
