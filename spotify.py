"""Spotify playing in the app itself: your own account, whole tracks, no window of its own.

The embed player in the Music tab is Spotify's public one, and a public player gets 30 seconds of a
track. Whole tracks mean signing in, and Spotify only lets another program play them through its
Web Playback SDK, which has three requirements this file exists to meet.

It needs a real web address. The dashboard is a file on disk, and a page loaded from disk has no
origin worth the name: the SDK's own requests get refused and the sign-in has nowhere to come back
to. Serving the whole app over http instead would break every film and track on this computer,
because a page on http may not load a file off the disk. So there is a small server here, on the
loopback address nobody outside this computer can reach, serving exactly one page. That page sits
in the Music tab as an invisible frame, holds the SDK, and takes its orders from the tab through
postMessage. Everything you see is still drawn by the app.

It needs you to have said yes. That is the authorisation code flow with PKCE, the one meant for
programs that cannot keep a secret - no password is typed here, nothing is stored but the tokens
Spotify hands back, and those live in data/ with the rest of your things, which git ignores. The
page in the frame is given a short-lived access token when it asks and never sees the refresh
token.

It needs Premium. That is Spotify's rule for playing outside their own apps, not a choice made
here; without it the SDK refuses and the tab says so plainly and leaves the 30-second player where
it is.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import main

# Spotify matches the address it sends you back to exactly, port and all, against the list in the
# app's settings on their site. So the port cannot be "whatever is free": these three are tried in
# turn and all three go in that list, which leaves room for two other programs to have taken one.
PORTS = (8973, 8974, 8975)
REDIRECT = "http://127.0.0.1:%d/callback"

# streaming is the one that lets this be a speaker at all. The two user-read scopes are what the
# SDK asks for to know whether you may use it. The two playback ones are for starting a chosen
# track and for shuffle, which the SDK itself has no way to do.
SCOPES = ("streaming user-read-email user-read-private "
          "user-modify-playback-state user-read-playback-state")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

STORE = "spotify.json"
_lock = threading.Lock()
_pending = {}            # the verifier and state of a sign-in part way through
_server = None
_port = None


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


def _merge(**kw):
    data = _read()
    data.update(kw)
    _write(data)
    return data


def client_id():
    """Whose app this is to Spotify. An id is public by design in this flow - it is not a secret,
    and there is no secret here to keep. Set at build time or pasted in by whoever is using it."""
    return (os.getenv("BB_SPOTIFY_CLIENT_ID") or _read().get("client_id") or "").strip()


def set_client_id(value):
    value = (value or "").strip()
    if value and ("://" in value or "spotify.com" in value):
        return {"error": "That is a link to something on Spotify, not the app's ID. Links go in "
                         "the box further down, under Online, and play straight away. This box "
                         "is for the one-off set-up."}
    if value and not value.isalnum():
        return {"error": "That doesn't look like a client ID. It is 32 letters and numbers, "
                         "nothing else - copy it from the app's page on Spotify's dashboard."}
    _merge(client_id=value)
    return {"ok": True}


# ---------------------------------------------------------------- the server in the middle

def port():
    """Starts the server the first time anything needs it, and says which port it got."""
    global _server, _port
    if _port:
        return _port
    last = None
    for p in PORTS:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), _Handler)
        except OSError as e:          # something else has it; try the next
            last = e
            continue
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="spotify-loopback").start()
        _server, _port = srv, p
        main.log_quiet(f"spotify: player server on 127.0.0.1:{p}")
        return p
    main.log_quiet(f"spotify: no free port among {PORTS}: {last!r}")
    return None


def redirect_uri():
    p = port()
    return REDIRECT % p if p else ""


def player_url():
    p = port()
    return f"http://127.0.0.1:{p}/player" if p else ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "BlackboardBuddy"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # its chatter belongs in nobody's console
        pass

    def _send(self, code, body, kind="text/html; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/player", "/player/"):
            return self._send(200, PLAYER_HTML)
        if path == "/callback":
            return self._send(200, self._callback())
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, page("Nothing here", "That address isn't part of the app."))

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/token":
            return self._send(404, b"", "text/plain")
        tok = token()
        body = json.dumps({"access_token": tok} if tok else {"error": "not signed in"})
        self._send(200 if tok else 401, body, "application/json")

    def _callback(self):
        """Where Spotify sends you back after you say yes. The code in the address is swapped for
        tokens here, on this side, so it never reaches any page."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        one = lambda k: (q.get(k) or [""])[0]
        if one("error"):
            return page("Not connected", f"Spotify said: {one('error')}. Nothing was saved.")
        want = _pending.pop("state", None)
        if not want or one("state") != want:
            # a code arriving with the wrong state is not a sign-in this app started
            return page("Not connected", "That didn't come from this app. Try Connect again.")
        got = _exchange({"grant_type": "authorization_code", "code": one("code"),
                         "redirect_uri": redirect_uri(),
                         "code_verifier": _pending.pop("verifier", "")})
        if got.get("error"):
            return page("Not connected", got["error"])
        return page("Connected", "Spotify is connected. Close this tab and go back to the app - "
                                 "the Music tab has your account in it now.", good=True)


def page(title, body, good=False):
    """The two pages that show up in a browser rather than in the app, dressed to match it."""
    tick = ("<svg viewBox='0 0 24 24' fill='none' stroke='#d71921' stroke-width='2.5'>"
            "<path d='M4 13l5 5L20 6'/></svg>" if good else "")
    return f"""<!doctype html><meta charset="utf-8"><title>{title} - Blackboard Buddy</title>
<style>
 :root{{color-scheme:dark}}
 *{{box-sizing:border-box}}
 body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0d;color:#f2f2f3;
  font:15px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif;
  background-image:radial-gradient(rgba(215,25,33,.17) 1px,transparent 1px);
  background-size:22px 22px}}
 .card{{max-width:30rem;margin:1.5rem;padding:2rem 2.25rem;border-radius:18px;
  background:rgba(22,22,26,.82);border:1px solid rgba(255,255,255,.09);
  box-shadow:0 18px 50px rgba(0,0,0,.45);backdrop-filter:blur(14px)}}
 h1{{margin:0 0 .5rem;font:800 30px/1.05 "Doto","Space Mono",ui-monospace,monospace;
  text-transform:uppercase;letter-spacing:.01em}}
 p{{margin:0;color:#b9b9bf}}
 svg{{width:30px;height:30px;margin-bottom:.4rem}}
</style>
<div class="card">{tick}<h1>{title}</h1><p>{body}</p></div>
<script>setTimeout(function(){{ try {{ window.close(); }} catch (e) {{}} }}, 2500);</script>"""


# ---------------------------------------------------------------- signing in

def _verifier():
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def login_url():
    """The address to send someone to say yes at. Opened in their own browser, where they are
    probably already signed in to Spotify, and where their password is their business."""
    cid = client_id()
    if not cid:
        return {"error": "No client ID yet.", "setup": True}
    if not port():
        return {"error": "Couldn't open a port for the sign-in to come back to. Ports "
                         f"{', '.join(str(p) for p in PORTS)} are all taken - close whatever "
                         "has them and try again."}
    verifier = _verifier()
    _pending["verifier"] = verifier
    _pending["state"] = secrets.token_urlsafe(24)
    args = {"client_id": cid, "response_type": "code", "redirect_uri": redirect_uri(),
            "state": _pending["state"], "scope": SCOPES,
            "code_challenge_method": "S256", "code_challenge": _challenge(verifier),
            "show_dialog": "false"}
    return {"url": AUTH_URL + "?" + urllib.parse.urlencode(args), "redirect": redirect_uri()}


def _exchange(fields):
    """Asks for tokens, and keeps what comes back. The only place tokens are written."""
    cid = client_id()
    if not cid:
        return {"error": "No client ID is set."}
    body = urllib.parse.urlencode(dict(fields, client_id=cid)).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "BlackboardBuddy"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            got = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error_description", "")
        except Exception:
            pass
        main.log_quiet(f"spotify: token exchange failed {e.code} {detail!r}")
        if e.code in (400, 401) and "redirect" in detail.lower():
            return {"error": "Spotify doesn't recognise the address this app sends you back to. "
                             f"Add {redirect_uri()} to the Redirect URIs of your app on "
                             "Spotify's dashboard, save, and try again."}
        return {"error": f"Spotify refused the sign-in{': ' + detail if detail else ''}."}
    except Exception as e:
        main.log_quiet(f"spotify: token exchange couldn't reach Spotify: {e!r}")
        return {"error": "Couldn't reach Spotify. Are you online?"}
    if not got.get("access_token"):
        return {"error": "Spotify sent no token back."}
    keep = {"access_token": got["access_token"],
            "expires_at": time.time() + int(got.get("expires_in", 3600)) - 60,
            "scope": got.get("scope", "")}
    if got.get("refresh_token"):            # on a refresh they often rotate it; keep the new one
        keep["refresh_token"] = got["refresh_token"]
    _merge(**keep)
    return {"ok": True}


def token():
    """A usable access token, refreshed if it is about to run out. None if nobody is signed in."""
    data = _read()
    if data.get("access_token") and time.time() < data.get("expires_at", 0):
        return data["access_token"]
    if not data.get("refresh_token"):
        return None
    got = _exchange({"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
    if got.get("error"):
        return None
    return _read().get("access_token")


def logout():
    """Forgets the tokens. Spotify has no way for a program to undo the permission itself - that
    lives at spotify.com/account/apps - so this says so rather than pretending."""
    data = _read()
    for k in ("access_token", "refresh_token", "expires_at", "scope"):
        data.pop(k, None)
    _write(data)
    return {"ok": True, "note": "Signed out here. To take the app's permission away for good, "
                                "there is a list at spotify.com/account/apps."}


# ---------------------------------------------------------------- asking Spotify things

def _call(method, path, body=None, params=None):
    tok = token()
    if not tok:
        return {"error": "Not signed in to Spotify.", "signed_out": True}
    url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json",
        "User-Agent": "BlackboardBuddy"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.loads(e.read().decode("utf-8")).get("error") or {}).get("message", "")
        except Exception:
            pass
        main.log_quiet(f"spotify: {method} {path} -> {e.code} {detail!r}")
        if e.code == 401:
            return {"error": "Spotify wants you to sign in again.", "signed_out": True}
        if e.code == 403:
            return {"error": detail or "Spotify said no. Playing in another app needs Premium.",
                    "premium": True}
        if e.code == 404:
            return {"error": "The player isn't ready yet. Give it a moment and try again."}
        if e.code == 429:
            return {"error": "Spotify is asking for a pause. Try again in a minute."}
        return {"error": detail or f"Spotify said no ({e.code})."}
    except Exception as e:
        main.log_quiet(f"spotify: {method} {path} couldn't reach Spotify: {e!r}")
        return {"error": "Couldn't reach Spotify. Are you online?"}


def status():
    """Everything the Music tab needs to know in one answer, so it can draw itself once."""
    data = _read()
    out = {"client_id": bool(client_id()), "signed_in": bool(data.get("refresh_token")),
           "redirect": redirect_uri(), "player": player_url(), "ports": list(PORTS)}
    if not out["redirect"]:
        out["error"] = ("Ports " + ", ".join(str(p) for p in PORTS) + " are all taken by "
                        "something else on this computer, and the sign-in needs one of them to "
                        "come back on. Close whatever has them, or restart the app.")
    if not out["signed_in"]:
        return out
    me = _call("GET", "/me")
    if me.get("signed_out"):
        return dict(out, signed_in=False)
    if me.get("error"):
        return dict(out, error=me["error"])
    out["who"] = me.get("display_name") or me.get("id") or "your account"
    out["product"] = me.get("product", "")          # premium | free | open
    out["premium"] = me.get("product") == "premium"
    return out


def search(q, kind="track", limit=20):
    q = (q or "").strip()
    if not q:
        return {"rows": []}
    kind = kind if kind in ("track", "album", "playlist", "artist") else "track"
    got = _call("GET", "/search", params={"q": q, "type": kind,
                                          "limit": max(1, min(int(limit or 20), 50))})
    if got.get("error"):
        return got
    items = ((got.get(kind + "s") or {}).get("items") or [])
    rows = []
    for it in items:
        if not it:                                   # Spotify leaves holes in playlist results
            continue
        art = (it.get("album") or it).get("images") or []
        rows.append({"uri": it.get("uri", ""), "title": it.get("name", ""),
                     "artist": ", ".join(a.get("name", "") for a in (it.get("artists") or []))
                               or (it.get("owner") or {}).get("display_name", ""),
                     "album": (it.get("album") or {}).get("name", ""),
                     "art": (art[-1] if art else {}).get("url", ""),
                     "ms": it.get("duration_ms") or 0,
                     "kind": kind})
    return {"rows": rows}


def play(device_id, uri, position_ms=0):
    """Starts something on this app's own speaker. The SDK can pause and skip but has no way to
    say what to play, so this is the one thing that has to go through the Web API."""
    if not device_id:
        return {"error": "The player in the app hasn't started yet. Give it a moment."}
    body = ({"context_uri": uri} if any(k in uri for k in (":album:", ":playlist:", ":artist:"))
            else {"uris": [uri]})
    body["position_ms"] = int(position_ms or 0)
    return _call("PUT", "/me/player/play", body=body, params={"device_id": device_id})


def shuffle(on, device_id):
    return _call("PUT", "/me/player/shuffle",
                 params={"state": "true" if on else "false", "device_id": device_id or ""})


# ---------------------------------------------------------------- the page in the frame

# Served from 127.0.0.1, which counts as a proper secure origin, so the SDK's requests and the
# protected playback it needs both behave the way they do on any website. It draws nothing: the
# Music tab asks it for things and it reports back.
PLAYER_HTML = r"""<!doctype html><meta charset="utf-8"><title>Spotify player</title>
<style>html,body{margin:0;background:transparent;overflow:hidden}</style>
<script src="https://sdk.scdn.co/spotify-player.js"></script>
<script>
'use strict';
var player = null, deviceId = '', lastState = null;

function up(msg) {
  try { parent.postMessage(Object.assign({ from: 'bb-spotify' }, msg), '*'); } catch (e) {}
}

function freshToken(cb) {
  fetch('/token', { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.access_token) { cb(j.access_token); }
      else { up({ kind: 'signed-out' }); }
    })
    .catch(function (e) { up({ kind: 'error', what: 'token', why: String(e) }); });
}

window.onSpotifyWebPlaybackSDKReady = function () {
  player = new Spotify.Player({
    name: 'Blackboard Buddy',
    getOAuthToken: freshToken,
    volume: 0.8,
  });

  player.addListener('ready', function (e) {
    deviceId = e.device_id;
    up({ kind: 'ready', device_id: deviceId });
  });
  player.addListener('not_ready', function () { up({ kind: 'not-ready' }); });

  // Each of these means something different to whoever is sitting there, so each gets its own
  // word rather than one shrug. account_error is the Premium one, and much the most likely.
  player.addListener('initialization_error', function (e) {
    up({ kind: 'error', what: 'start', why: e.message }); });
  player.addListener('authentication_error', function (e) {
    up({ kind: 'error', what: 'auth', why: e.message }); });
  player.addListener('account_error', function (e) {
    up({ kind: 'error', what: 'premium', why: e.message }); });
  player.addListener('playback_error', function (e) {
    up({ kind: 'error', what: 'playback', why: e.message }); });

  player.addListener('player_state_changed', function (s) { send(s); });

  player.connect().then(function (ok) {
    if (!ok) up({ kind: 'error', what: 'connect', why: 'Spotify would not take the player.' });
  });

  // The SDK only speaks up when something changes, and a moving progress bar needs more than
  // that, so the state is read once a second while something is playing.
  setInterval(function () {
    if (!player) return;
    player.getCurrentState().then(function (s) { if (s && !s.paused) send(s); });
  }, 1000);
};

function send(s) {
  if (!s) { up({ kind: 'state', empty: true }); return; }
  var t = s.track_window && s.track_window.current_track;
  lastState = s;
  up({ kind: 'state', paused: s.paused, position: s.position, duration: s.duration,
       shuffle: !!s.shuffle,
       title: t ? t.name : '',
       artist: t ? (t.artists || []).map(function (a) { return a.name; }).join(', ') : '',
       art: t && t.album && t.album.images && t.album.images.length
            ? t.album.images[0].url : '',
       uri: t ? t.uri : '' });
}

window.addEventListener('message', function (ev) {
  var m = ev.data;
  if (!m || m.to !== 'bb-spotify' || !player) return;
  if (m.do === 'toggle') player.togglePlay();
  else if (m.do === 'next') player.nextTrack();
  else if (m.do === 'prev') player.previousTrack();
  else if (m.do === 'seek') player.seek(Math.max(0, m.ms | 0));
  else if (m.do === 'volume') player.setVolume(Math.max(0, Math.min(1, m.v)));
  else if (m.do === 'pause') player.pause();
  else if (m.do === 'state') player.getCurrentState().then(send);
});

// If the script itself never arrives - no internet, or something blocking it - nothing above ever
// runs, and the tab would wait for ever. Say so instead.
setTimeout(function () {
  if (!player) up({ kind: 'error', what: 'sdk',
                    why: "Spotify's player script didn't load. Check you are online." });
}, 12000);
</script>
"""
