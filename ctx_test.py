"""The right-click menu on the Online board. Forces local mode, same as online_test.py."""
import functools
import http.server
import sys
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(r"C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy\output")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                      functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/dashboard.html"

STUB = ("sessionStorage.introSeen='1';sessionStorage.bootSound='1';"
        "window.pywebview={api:new Proxy({}, {get:(t,k) => "
        "k==='ol_local_load'||k==='ol_local_save'||k==='online_config' ? undefined : async () => ({})})};")
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


with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge")
    ctxb = br.new_context(viewport={"width": 1400, "height": 900},
                          permissions=["clipboard-read", "clipboard-write"])
    pg = ctxb.new_page()
    pg.add_init_script(STUB)
    pg.add_init_script(NOSERVER)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL)
    pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]')
    pg.wait_for_timeout(500)
    pg.click(".ol-card .alt")                      # "just on this computer"
    pg.wait_for_timeout(300)
    check("local board opens", not pg.is_visible(".ol-gate.on"))

    bx = pg.locator(".ol-board").bounding_box()
    menu = pg.locator(".ol-ctx")

    def rclick(x, y):
        pg.mouse.click(bx["x"] + x, bx["y"] + y, button="right")
        pg.wait_for_timeout(300)

    def labels():
        return pg.locator(".ol-ctx button").all_inner_texts()

    def nblocks():
        return pg.evaluate("document.querySelectorAll('.ol-b').length")

    # ---------------------------------------------------------------- empty space
    rclick(700, 450)
    check("right-click on empty space opens a menu", menu.is_visible())
    L = labels()
    for want in ("Note", "Sticky note", "Big text", "Code", "Checklist",
                 "Paste here", "Select everything", "Show everything", "Back to 100%"):
        check(f"offers {want!r}", any(t.startswith(want) for t in L), ", ".join(L)[:90])
    # it opens at the pointer, except where it would hang off an edge: then it slides back in
    check("opens at the pointer", pg.evaluate("""([x, y]) => {
        const r = document.querySelector('.ol-ctx').getBoundingClientRect(),
              b = document.querySelector('.ol-board').getBoundingClientRect();
        const atX = Math.abs(r.left - x) < 2, downFrom = Math.abs(r.top - y) < 2;
        const pulledUp = r.bottom <= b.bottom + 1 && r.top >= b.top - 1;
        return atX && (downFrom || pulledUp); }""",
        [bx["x"] + 700, bx["y"] + 450]))

    n = nblocks()
    pg.locator(".ol-ctx button", has_text="Sticky note").first.click()
    pg.wait_for_timeout(500)
    check("the menu closes after choosing", not menu.is_visible())
    check("a sticky note is added", nblocks() == n + 1, nblocks())
    check("it lands where the click was", pg.evaluate("""() => {
        const b = [...bbOnline.S.blocks.values()].pop();
        return b.kind === 'sticky'; }"""))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(250)

    # ---------------------------------------------------------------- on a block
    bid = pg.evaluate("[...bbOnline.S.blocks.keys()].pop()")
    r = pg.evaluate(f"""(() => {{ const r = document.querySelector('.ol-b[data-id=\"{bid}\"]')
        .getBoundingClientRect(); return [r.x + 20, r.y + 14]; }})()""")
    pg.mouse.click(r[0], r[1], button="right")
    pg.wait_for_timeout(300)
    check("right-click on a block opens a menu", menu.is_visible())
    check("the menu names the block",
          pg.locator(".ol-ctx small").first.inner_text().lower() == "sticky note",
          pg.locator(".ol-ctx small").first.inner_text())
    BL = labels()
    for want in ("Edit the text", "Copy", "Duplicate", "Bring to the front", "Delete"):
        check(f"block menu offers {want!r}", any(t.startswith(want) for t in BL), ", ".join(BL)[:90])
    check("the block is selected by the right-click",
          pg.evaluate(f"bbOnline.S.sel.has('{bid}')"))
    check("a sticky offers a row of colours", pg.locator(".ol-ctx .cols i").count() >= 5,
          str(pg.locator(".ol-ctx .cols i").count()))

    pg.locator(".ol-ctx .cols i").nth(1).click()
    pg.wait_for_timeout(450)
    got = pg.evaluate(f"bbOnline.S.blocks.get('{bid}').data.color")
    check("picking a colour recolours it", got == "#ffe9a8", str(got))

    n = nblocks()
    pg.mouse.click(r[0], r[1], button="right")
    pg.wait_for_timeout(300)
    pg.locator(".ol-ctx button", has_text="Duplicate").first.click()
    pg.wait_for_timeout(500)
    check("Duplicate adds a copy", nblocks() == n + 1, nblocks())

    n = nblocks()
    pg.mouse.click(r[0], r[1], button="right")
    pg.wait_for_timeout(300)
    pg.locator(".ol-ctx button", has_text="Delete").first.click()
    pg.wait_for_timeout(600)
    check("Delete removes it", nblocks() == n - 1, nblocks())

    # ---------------------------------------------------------------- several at once
    for at in ((260, 520), (460, 520)):          # two more, so there is something to multi-select
        rclick(*at)
        pg.locator(".ol-ctx button", has_text="Note").first.click()
        pg.wait_for_timeout(400)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
    pg.evaluate("""() => { const ids = [...bbOnline.S.blocks.keys()];
        bbOnline.S.sel.clear(); ids.slice(0, 2).forEach(i => bbOnline.S.sel.add(i)); }""")
    ids = pg.evaluate("[...bbOnline.S.blocks.keys()].slice(0,2)")
    if len(ids) >= 2:
        r2 = pg.evaluate(f"""(() => {{ const r = document.querySelector('.ol-b[data-id=\"{ids[0]}\"]')
            .getBoundingClientRect(); return [r.x + 20, r.y + 14]; }})()""")
        pg.mouse.click(r2[0], r2[1], button="right")
        pg.wait_for_timeout(300)
        head = pg.locator(".ol-ctx small").first.inner_text()
        check("with two picked the menu says so", "2 blocks" in head.lower(), head)
        check("and offers to delete both",
              any("Delete 2 blocks" in t for t in labels()), ", ".join(labels())[:90])
        pg.keyboard.press("Escape")
    else:
        print("  SKIP  multi-select (not enough blocks)")

    # ---------------------------------------------------------------- closing
    rclick(500, 300)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(250)
    check("Escape closes it", not menu.is_visible())

    rclick(500, 300)
    pg.mouse.click(bx["x"] + 950, bx["y"] + 620)
    pg.wait_for_timeout(250)
    check("a click elsewhere closes it", not menu.is_visible())

    # scrolling the board closes it; scrolling with the pointer on the menu does not,
    # so that a menu taller than the board can still be scrolled through
    rclick(500, 300)
    pg.mouse.move(bx["x"] + 1000, bx["y"] + 200)
    pg.mouse.wheel(0, 120)
    pg.wait_for_timeout(250)
    check("scrolling the board closes it", not menu.is_visible())

    # ---------------------------------------------------------------- stays on screen
    # the lowest, right-most spot that is actually bare board: the tool bar and the zoom
    # buttons sit on top of it near the edges and are not part of it
    spot = pg.evaluate("""() => {
      const b = document.querySelector('.ol-board'), r = b.getBoundingClientRect();
      for (let dy = 12; dy < 220; dy += 12)
        for (let dx = 12; dx < 320; dx += 12) {
          const x = r.right - dx, y = r.bottom - dy, el = document.elementFromPoint(x, y);
          if (el && (el === b || b.contains(el)) && !el.closest('.ol-b')) return [x - r.left, y - r.top];
        }
      return null; }""")
    assert spot, "could not find a bare patch of board near the corner"
    print(f"  (corner test at {spot[0]:.0f},{spot[1]:.0f} of {bx['width']:.0f}x{bx['height']:.0f})")
    rclick(*spot)
    check("near the corner it stays fully on screen", pg.evaluate("""() => {
        const r = document.querySelector('.ol-ctx').getBoundingClientRect(),
              b = document.querySelector('.ol-board').getBoundingClientRect();
        return r.right <= b.right + 1 && r.bottom <= b.bottom + 1
            && r.left >= b.left - 1 && r.top >= b.top - 1; }"""),
          pg.evaluate("JSON.stringify(document.querySelector('.ol-ctx').getBoundingClientRect())"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)

    # ---------------------------------------------------------------- typing keeps the native menu
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    pg.locator(".ol-b").first.dblclick()
    pg.wait_for_timeout(500)
    inside = pg.evaluate("""() => { const d = document.querySelector('.ol-b.editing');
        return !!(d && d.querySelector('[contenteditable],textarea')); }""")
    if inside:
        tgt = pg.locator(".ol-b.editing [contenteditable], .ol-b.editing textarea").first
        tgt.click(button="right")
        pg.wait_for_timeout(300)
        check("inside text the board's menu stays shut", not menu.is_visible())
    else:
        print("  SKIP  typing check (no editor opened)")

    br.close()

# both of these come from the stub, not the page: nb_list returns {} so the note map has no
# list to filter, and the code preview's iframe is sandboxed without allow-same-origin
IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
