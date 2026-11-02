"""Auto-saving, the copies kept on this computer, and dragging the board with the right button."""
import functools
import http.server
import json
import sys
import threading
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(r"C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy\output")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                      functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/dashboard.html"

# A bridge that behaves the way the app's does: it keeps the board and the copies in memory and
# answers slowly enough that a fast close really would beat it.
BRIDGE = """
sessionStorage.introSeen='1'; sessionStorage.bootSound='1';
window.__file = {rows: [], at: 0};
window.__backups = {};
const slow = (fn) => (...a) => new Promise(r => setTimeout(() => r(fn(...a)), 120));
window.pywebview = {api: new Proxy({}, {get: (t, k) => {
  if (k === 'online_config') return undefined;
  if (k === 'ol_local_load') return slow(() => window.__file.rows);
  if (k === 'ol_local_save') return slow(rows => { window.__file = {rows, at: Date.now()}; return 'ok'; });
  if (k === 'ol_local_saved_at') return slow(() => window.__file.at);
  if (k === 'board_backup') return slow((name, rows) => {
    (window.__backups[name] = window.__backups[name] || []).push({when: new Date().toISOString(), rows});
    return true; });
  if (k === 'board_backups') return slow(name => (window.__backups[name] || []).map((b, i) => (
    {file: name + '-' + i + '.json', when: b.when, size: JSON.stringify(b.rows).length,
     latest: i === window.__backups[name].length - 1})).reverse());
  if (k === 'board_backup_read') return slow(file => {
    const [name, i] = [file.slice(0, file.lastIndexOf('-')), +file.split('-').pop().split('.')[0]];
    return (window.__backups[name] || [])[i]?.rows || null; });
  if (k === 'board_backup_folder') return slow(() => 'folder');
  return async () => ({});
}})};
"""
NOSERVER = ("(() => { let bb; Object.defineProperty(window, 'BB', {configurable: true, "
            "get: () => bb, set: v => { v.online = {}; bb = v; }}); })();")

errors, fails, passes = [], [], 0


def check(name, ok, extra=""):
    global passes
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   ({extra})" if extra else ""))
    if ok:
        passes += 1
    else:
        fails.append(name)


def board(br, script=BRIDGE):
    pg = br.new_context(viewport={"width": 1400, "height": 900}).new_page()
    pg.add_init_script(script)
    pg.add_init_script(NOSERVER)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL)
    pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]')
    pg.wait_for_timeout(500)
    if pg.is_visible(".ol-card .alt"):
        pg.click(".ol-card .alt")
        pg.wait_for_timeout(500)
    return pg


with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    pg = board(br)

    # ---------------------------------------------- the ordinary save
    pg.evaluate("() => bbOnline.add('sticky', 300, 300, {text: 'one'})")
    pg.wait_for_timeout(900)
    check("a change reaches the file on its own",
          pg.evaluate("() => window.__file.rows.length") == 1,
          str(pg.evaluate("() => window.__file.rows.length")))

    # ---------------------------------------------- beating a fast close
    pg.evaluate("() => bbOnline.add('sticky', 600, 300, {text: 'two'})")
    pg.wait_for_timeout(40)                      # far less than the save takes
    mirror = pg.evaluate("() => (JSON.parse(localStorage.getItem('olBoardMirror') || '{}').rows || []).length")
    check("the instant copy has it before the file does", mirror == 2, str(mirror))
    check("and the file has not caught up yet",
          pg.evaluate("() => window.__file.rows.length") == 1,
          str(pg.evaluate("() => window.__file.rows.length")))

    # the app asks the page for the board as the window closes
    got = pg.evaluate("() => window.bbRows()")
    check("the app can take the board off the page at closing time",
          isinstance(got, dict) and len(got.get("rows", [])) == 2, str(len(got.get("rows", []))))
    check("and is told which board it is", got.get("mode") == "local" and got.get("name") == "offline", str(got.get("name")))

    # ---------------------------------------------- what comes back next time
    kept = pg.evaluate("() => JSON.parse(localStorage.getItem('olBoardMirror'))")
    pg.close()

    pg = board(br, BRIDGE + f"""
      // the file is a save behind, exactly as a fast close would leave it
      window.__file = {{rows: {json.dumps(kept['rows'][:1])}, at: Date.now() - 60000}};
      localStorage.setItem('olBoardMirror', {json.dumps(json.dumps(kept))});
    """)
    pg.wait_for_timeout(1200)
    n = pg.evaluate("() => bbOnline.S.blocks.size")
    check("next time it opens, nothing typed before the close is missing", n == 2, f"{n} blocks")
    pg.close()

    # the other way round: the file is the newer one, so the stale mirror is ignored
    pg = board(br, BRIDGE + f"""
      window.__file = {{rows: {json.dumps(kept['rows'])}, at: Date.now()}};
      localStorage.setItem('olBoardMirror', {json.dumps(json.dumps({'at': 1, 'rows': []}))});
    """)
    pg.wait_for_timeout(1200)
    check("an old instant copy does not wipe a newer file",
          pg.evaluate("() => bbOnline.S.blocks.size") == 2,
          str(pg.evaluate("() => bbOnline.S.blocks.size")))

    # ---------------------------------------------- copies on the computer
    pg.evaluate("() => bbOnline.add('note', 900, 500, {html: 'keep me'})")
    pg.wait_for_timeout(600)
    pg.mouse.click(700, 760, button="right")
    pg.wait_for_timeout(400)
    L = pg.locator(".ol-ctx button").all_inner_texts()
    check("the board menu offers to keep a copy", any("Back one up now" in t for t in L), " | ".join(L)[-90:])
    check("and to put an older one back", any("Put back an older copy" in t for t in L), " | ".join(L)[-90:])
    pg.locator(".ol-ctx button", has_text="Back one up now").first.click()
    pg.wait_for_timeout(700)
    check("a copy is kept under the board's name",
          pg.evaluate("() => (window.__backups['offline'] || []).length") >= 1,
          str(pg.evaluate("() => Object.keys(window.__backups)")))
    check("and it holds everything that is on the board",
          pg.evaluate("() => window.__backups['offline'].at(-1).rows.length") == 3,
          str(pg.evaluate("() => window.__backups['offline'].at(-1).rows.length")))

    # going away from the window keeps one too
    pg.evaluate("() => window.__backups = {}")
    pg.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    pg.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                           document.dispatchEvent(new Event('visibilitychange')); }""")
    pg.wait_for_timeout(600)
    check("leaving the window keeps one as well",
          pg.evaluate("() => (window.__backups['offline'] || []).length") >= 1,
          str(pg.evaluate("() => (window.__backups['offline'] || []).length")))

    # ---------------------------------------------- putting one back
    pg.evaluate("""() => { window.__backups = {offline: [{when: new Date().toISOString(),
        rows: [{id: 'old-1', kind: 'sticky', x: 100, y: 100, w: 200, h: 180, z: 1, data: {text: 'from before'}}]}]}; }""")
    pg.evaluate("() => { window.confirm = () => true; }")
    pg.mouse.click(700, 760, button="right")
    pg.wait_for_timeout(400)
    pg.locator(".ol-ctx button", has_text="Put back an older copy").first.click()
    pg.wait_for_timeout(900)
    L = pg.locator(".ol-ctx button").all_inner_texts()
    check("it lists the copies it has", any("newest copy" in t for t in L), " | ".join(L)[:90])
    pg.locator(".ol-ctx button", has_text="newest copy").first.click()
    pg.wait_for_timeout(1200)
    ids = pg.evaluate("() => [...bbOnline.S.blocks.keys()]")
    check("choosing one puts the board back to it", ids == ["old-1"], str(ids))
    check("and that goes to the file too",
          pg.evaluate("() => window.__file.rows.length") == 1,
          str(pg.evaluate("() => window.__file.rows.length")))

    # ---------------------------------------------- dragging with the right button
    pg.evaluate("() => bbOnline.view({x: 80, y: 60, k: 1})")
    pg.wait_for_timeout(200)
    was = pg.evaluate("() => ({...bbOnline.S.view})")
    spot = pg.evaluate("""() => { const r = document.querySelector('.ol-board').getBoundingClientRect();
        for (let y = r.top + 140; y < r.bottom - 140; y += 40)
          for (let x = r.left + 140; x < r.right - 140; x += 40) {
            const el = document.elementFromPoint(x, y);
            if (el && !el.closest('.ol-b') && !el.closest('.ol-top') && !el.closest('.ol-tools')
                && !el.closest('.ol-zoom') && !el.closest('.ol-corner')) return {x, y};
          }
        return null; }""")
    pg.mouse.move(spot["x"], spot["y"])
    pg.mouse.down(button="right")
    pg.mouse.move(spot["x"] - 120, spot["y"] + 70, steps=8)
    pg.mouse.up(button="right")
    pg.wait_for_timeout(400)
    now = pg.evaluate("() => ({...bbOnline.S.view})")
    check("right-dragging moves the board",
          abs(now["x"] - was["x"] + 120) < 12 and abs(now["y"] - was["y"] - 70) < 12,
          f'{was["x"]:.0f},{was["y"]:.0f} -> {now["x"]:.0f},{now["y"]:.0f}')
    check("and no menu is left open afterwards", not pg.is_visible(".ol-ctx.on"))

    # a right-click that does not move still opens the menu
    pg.mouse.click(spot["x"], spot["y"], button="right")
    pg.wait_for_timeout(450)
    check("a plain right-click still opens the menu", pg.is_visible(".ol-ctx.on"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
