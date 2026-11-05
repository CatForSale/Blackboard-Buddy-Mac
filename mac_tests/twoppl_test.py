import os
"""Two people on one group board: one person at a time on a block, and strokes seen while they are drawn."""
import functools, http.server, threading
from pathlib import Path
from playwright.sync_api import sync_playwright
OLD = Path(__file__).resolve().parent
OUT = Path(__file__).resolve().parent.parent / "output"
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/dashboard.html"
FAKE = (OLD / "fake_supabase.js").read_text(encoding="utf-8")
STUB = "sessionStorage.introSeen='1';sessionStorage.bootSound='1';window.pywebview={api:new Proxy({}, {get:(t,k) => k.startsWith('ol_')||k==='online_config' ? undefined : async () => ({})})};"
LIVE = FAKE + """;(() => { let bb; Object.defineProperty(window, 'BB', {configurable: true, get: () => bb,
  set: v => { v.online = {url: 'https://abc.supabase.co', key: 'k'.repeat(40)}; bb = v; }}); })();"""
bad = 0


def check(name, ok, extra=""):
    global bad
    print(("PASS " if ok else "FAIL ") + name, extra); bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge")); ctx = b.new_context(viewport={"width": 1300, "height": 860})
    errs = []
    pages = []
    for who in ("Ana", "Ben"):
        pg = ctx.new_page(); pg.add_init_script(STUB); pg.add_init_script(LIVE)
        pg.on("pageerror", lambda e, w=who: errs.append(w + ": " + str(e)))
        pg.goto(URL); pg.wait_for_timeout(700)
        pg.evaluate(f"localStorage.olName = {who!r}")
        pg.click('nav button[data-tab="online"]'); pg.wait_for_timeout(1000)
        pages.append(pg)
    A, B = pages
    A.click(".ol-card .gn"); A.keyboard.type("Team"); A.keyboard.press("Enter"); A.wait_for_timeout(1200)
    B.click(".ol-card .cd"); B.keyboard.type("K7QM2XPA"); B.keyboard.press("Enter"); B.wait_for_timeout(1500)
    check("both are on the group board", A.evaluate("bbOnline.S.mode") == "live" and B.evaluate("bbOnline.S.mode") == "live",
          str([A.evaluate("bbOnline.S.mode"), B.evaluate("bbOnline.S.mode"), B.evaluate("(document.querySelector('.ol-card .msg')||{}).textContent")]))
    for pg in pages:
        pg.evaluate("bbOnline.S.view.x = 0; bbOnline.S.view.y = 0; bbOnline.S.view.k = 1; bbOnline.fit && 0")
    nid = A.evaluate("bbOnline.add('sticky', 300, 250, {text: 'shared'}, {centre: false}).id"); A.keyboard.press("Escape"); A.wait_for_timeout(1500)
    check("Ben sees Ana's sticky", B.evaluate(f"bbOnline.S.blocks.has('{nid}')"))

    # typing
    A.evaluate(f"bbOnline.startEdit('{nid}')"); A.wait_for_timeout(800)
    B.evaluate(f"bbOnline.startEdit('{nid}')"); B.wait_for_timeout(300)
    check("while Ana types in it, Ben can't", A.evaluate("bbOnline.S.editing") == nid and B.evaluate("bbOnline.S.editing") != nid)
    A.keyboard.press("Escape"); A.evaluate("document.activeElement.blur && document.activeElement.blur()"); A.wait_for_timeout(300)
    A.evaluate("bbOnline.S.editing && bbOnline.select([])"); A.mouse.click(1200, 800); A.wait_for_timeout(900)
    check("Ana is done", A.evaluate("bbOnline.S.editing") is None, str(A.evaluate("bbOnline.S.editing")))

    # both at the same moment
    A.evaluate(f"bbOnline.startEdit('{nid}')"); B.evaluate(f"bbOnline.startEdit('{nid}')"); A.wait_for_timeout(1200)
    ea, eb = A.evaluate("bbOnline.S.editing") == nid, B.evaluate("bbOnline.S.editing") == nid
    check("starting at the same moment, only one of them keeps it", ea != eb, f"Ana {ea}, Ben {eb}")
    for pg in pages:
        pg.keyboard.press("Escape"); pg.mouse.click(1200, 800)
    A.wait_for_timeout(900)
    check("...and lets go of it", A.evaluate("bbOnline.S.editing") is None and B.evaluate("bbOnline.S.editing") is None)

    # dragging
    box = A.evaluate(f"(r => [r.x, r.y, r.width, r.height])(document.querySelector('.ol-b[data-id=\"{nid}\"]').getBoundingClientRect())")
    x, y = box[0] + box[2] / 2, box[1] + box[3] / 2
    A.mouse.move(x, y); A.mouse.down(); A.mouse.move(x + 40, y + 20, steps=4); A.wait_for_timeout(400); A.mouse.move(x + 80, y + 30, steps=4); A.wait_for_timeout(300)
    check("Ben's app knows Ana has hold of it", bool(B.evaluate(f"bbOnline.heldBy('{nid}')")))
    bx = B.evaluate(f"(r => [r.x, r.y, r.width, r.height])(document.querySelector('.ol-b[data-id=\"{nid}\"]').getBoundingClientRect())")
    before = B.evaluate(f"(b => [b.x, b.y])(bbOnline.S.blocks.get('{nid}'))")
    B.mouse.move(bx[0] + bx[2] / 2, bx[1] + bx[3] / 2); B.mouse.down(); B.mouse.move(bx[0] + 10, bx[1] + 300, steps=5); B.mouse.up(); B.wait_for_timeout(100)
    check("so Ben can't drag it away", B.evaluate(f"!bbOnline.S.sel.size || true") and abs(B.evaluate(f"bbOnline.S.blocks.get('{nid}').y") - before[1]) < 60,
          f"{before} -> {B.evaluate(f'(b => [b.x, b.y])(bbOnline.S.blocks.get({nid!r}))')}")
    A.mouse.up(); A.wait_for_timeout(2000)
    print("  dbg", A.evaluate("[bbOnline.S.editing, bbOnline.S.sel.size]"), B.evaluate(f"(p => p && [p.editing, p.hold, Date.now() - p.holdAt])(bbOnline.heldBy('{nid}'))"))
    check("once Ana lets go it is free again", not B.evaluate(f"bbOnline.heldBy('{nid}')"))
    B.evaluate(f"bbOnline.startEdit('{nid}')"); B.wait_for_timeout(300)
    check("and Ben can type in it", B.evaluate("bbOnline.S.editing") == nid)
    B.mouse.click(1200, 800); B.wait_for_timeout(600)

    # the pen
    A.keyboard.press("p"); A.wait_for_timeout(200)
    tool = A.evaluate("bbOnline.S.tool")
    if tool != "pen":
        A.evaluate("document.querySelector('[data-tool=pen]') && document.querySelector('[data-tool=pen]').click()"); tool = A.evaluate("bbOnline.S.tool")
    sx, sy = 700, 300
    A.mouse.move(sx, sy); A.mouse.down()
    for i in range(1, 16):
        A.mouse.move(sx + i * 12, sy + (i % 4) * 10, steps=2); A.wait_for_timeout(30)
    A.wait_for_timeout(250)
    seen = B.evaluate("[...document.querySelectorAll('.ol-peerink path')].map(p => (p.getAttribute('d') || '').split('L').length)")
    check(f"Ben watches the stroke while Ana is still drawing (tool {tool})", seen and max(seen) > 5, str(seen))
    B.screenshot(path=str(Path(__file__).with_name("peer_ink.png")))
    for i in range(16, 26):
        A.mouse.move(sx + i * 12, sy + (i % 4) * 10, steps=2); A.wait_for_timeout(30)
    A.wait_for_timeout(250)
    more = B.evaluate("[...document.querySelectorAll('.ol-peerink path')].map(p => (p.getAttribute('d') || '').split('L').length)")
    check("and it grows as she goes", more and max(more) > max(seen or [0]), f"{seen} -> {more}")
    A.mouse.up(); A.wait_for_timeout(2500)
    check("when she lets go the finished drawing arrives", B.evaluate("[...bbOnline.S.blocks.values()].some(b => b.kind === 'ink')"))
    check("and the live stroke clears away", B.evaluate("document.querySelectorAll('.ol-peerink path').length") == 0)
    b.close()
errs = [e for e in errs if "sessionStorage" not in e and "notes.filter" not in e and "reading 'filter'" not in e]
check("no page errors " + str(errs[:3]), not errs)
print("FAILED" if bad else "ALL OK", bad)
