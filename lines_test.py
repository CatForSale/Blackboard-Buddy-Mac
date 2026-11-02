"""Joining lines, boxes and circles, find, lining things up, locking, ready-made layouts.
Local mode, the same way online_test.py does it."""
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


with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    page_ctx = br.new_context(viewport={"width": 1400, "height": 900},
                              permissions=["clipboard-read", "clipboard-write"])
    pg = page_ctx.new_page()
    pg.add_init_script(STUB)
    pg.add_init_script(NOSERVER)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL)
    pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]')
    pg.wait_for_timeout(500)
    pg.click(".ol-card .alt")
    pg.wait_for_timeout(300)

    bx = pg.locator(".ol-board").bounding_box()
    menu = pg.locator(".ol-ctx")

    def rclick(x, y):
        pg.mouse.click(bx["x"] + x, bx["y"] + y, button="right")
        pg.wait_for_timeout(300)

    def rclick_bare():
        """Right-click a patch of board with nothing on it, wherever that is by now."""
        spot = pg.evaluate("""() => {
          const b = document.querySelector('.ol-board'), r = b.getBoundingClientRect();
          for (let y = 40; y < r.height - 60; y += 26)
            for (let x = 40; x < r.width - 60; x += 26) {
              const el = document.elementFromPoint(r.left + x, r.top + y);
              if (el && b.contains(el) && !el.closest('.ol-b') && !el.closest('.ol-tools,.ol-top,.ol-zoom'))
                return [x, y];
            }
          return null; }""")
        assert spot, "no bare board left to right-click"
        rclick(*spot)

    def pickmenu(text):
        pg.locator(".ol-ctx button", has_text=text).first.click()
        pg.wait_for_timeout(450)

    def add_note(x, y):
        rclick(x, y)
        pickmenu("Note")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(200)
        return pg.evaluate("[...bbOnline.S.blocks.keys()].pop()")

    def blk(bid):
        return pg.evaluate(f"bbOnline.S.blocks.get('{bid}')")

    def at(bid):
        return pg.evaluate(f"""(() => {{ const r = document.querySelector('.ol-b[data-id=\"{bid}\"]')
            .getBoundingClientRect(); return [r.x + 24, r.y + 16]; }})()""")

    def nlines():
        return pg.evaluate("[...bbOnline.S.blocks.values()].filter(b => b.kind === 'arrow').length")

    # ================================================ joining two blocks from the menu
    a_id = add_note(300, 240)
    b_id = add_note(820, 560)
    check("two notes to join", a_id != b_id and len(pg.evaluate("[...bbOnline.S.blocks.keys()]")) >= 2)

    pg.mouse.click(*at(a_id), button="right")
    pg.wait_for_timeout(300)
    check("a block offers to draw a line", any(t.startswith("Draw a line from here")
                                               for t in pg.locator(".ol-ctx button").all_inner_texts()))
    pickmenu("Draw a line from here")
    check("the board says it is waiting for the other end",
          pg.evaluate("document.querySelector('.ol-board').classList.contains('linking')"))
    pg.mouse.move(bx["x"] + 600, bx["y"] + 400)
    pg.wait_for_timeout(200)
    check("a line follows the mouse while picking",
          len(pg.evaluate("document.querySelector('.ol-draft path').getAttribute('d') || ''") or "") > 4)
    pg.mouse.click(*at(b_id))
    pg.wait_for_timeout(500)
    check("clicking the second block joins them", nlines() == 1, nlines())
    check("it is drawn", pg.locator(".ol-links path").count() >= 2)

    line_id = pg.evaluate("[...bbOnline.S.blocks.values()].find(b => b.kind === 'arrow').id")

    # ================================================ right-click the line itself
    pg.wait_for_timeout(200)
    ON_LINE = """() => { const p = document.querySelector('.ol-links path.hit');
        const pt = p.getPointAtLength(p.getTotalLength() / 2), m = p.getScreenCTM();
        return [pt.x * m.a + pt.y * m.c + m.e, pt.x * m.b + pt.y * m.d + m.f]; }"""
    hit = pg.evaluate(ON_LINE)
    pg.mouse.click(hit[0], hit[1], button="right")
    pg.wait_for_timeout(350)
    check("right-click on the line opens its own menu",
          menu.is_visible() and pg.locator(".ol-ctx small").first.inner_text().lower() == "line",
          pg.locator(".ol-ctx small").first.inner_text() if menu.is_visible() else "not open")
    L = pg.locator(".ol-ctx button").all_inner_texts()
    for want in ("Plain line, no arrow", "Straight", "Right angle", "Dashed", "Thicker",
                 "Write on the line", "Swap which end points"):
        check(f"line menu offers {want!r}", any(want in t for t in L), ", ".join(L)[:80])
    check("the line offers a row of colours", pg.locator(".ol-ctx .cols i").count() >= 8,
          str(pg.locator(".ol-ctx .cols i").count()))

    # plain line
    pickmenu("Plain line, no arrow")
    check("no arrow head when plain", pg.evaluate("""() => {
        const p = document.querySelector('.ol-links path:not(.hit)');
        return !p.getAttribute('marker-end') && !p.getAttribute('marker-start'); }"""))

    def line_menu(text):
        h = pg.evaluate(ON_LINE)
        pg.mouse.click(h[0], h[1], button="right")
        pg.wait_for_timeout(300)
        pickmenu(text)

    line_menu("Arrows both ways")
    check("arrow heads on both ends", pg.evaluate("""() => {
        const p = document.querySelector('.ol-links path:not(.hit)');
        return !!p.getAttribute('marker-end') && !!p.getAttribute('marker-start'); }"""))

    line_menu("Straight")
    d = pg.evaluate("document.querySelector('.ol-links path:not(.hit)').getAttribute('d')")
    check("straight is one segment", d.count("L") == 1 and "Q" not in d, d)

    line_menu("Right angle")
    d = pg.evaluate("document.querySelector('.ol-links path:not(.hit)').getAttribute('d')")
    check("right angle turns three times", d.count("L") == 3, d)

    line_menu("Dashed")
    check("dashed", pg.evaluate("""() => !!document.querySelector('.ol-links path:not(.hit)')
        .getAttribute('stroke-dasharray')"""))

    line_menu("Thicker")
    check("thicker", pg.evaluate("""() => document.querySelector('.ol-links path:not(.hit)')
        .style.strokeWidth === '3.6'"""))

    pg.evaluate(f"bbOnline.S.blocks.get('{line_id}').data.label = 'leads to'")
    pg.evaluate("bbOnline.drawLinks ? bbOnline.drawLinks() : 0")
    pg.wait_for_timeout(250)
    wrote = pg.evaluate("""() => { const t = document.querySelector('.ol-links text');
        return t ? {text: t.textContent, backing: !!document.querySelector('.ol-links rect')} : null; }""")
    check("a label is written on the line with a backing",
          bool(wrote) and wrote["text"] == "leads to" and wrote["backing"], wrote)

    # colour
    line_menu("Back to the plain colour")
    h = pg.evaluate(ON_LINE)
    pg.mouse.click(h[0], h[1], button="right")
    pg.wait_for_timeout(300)
    pg.locator(".ol-ctx .cols i").nth(1).click()
    pg.wait_for_timeout(400)
    check("picking a colour colours the line",
          pg.evaluate(f"bbOnline.S.blocks.get('{line_id}').data.color") == "#ff6b35",
          pg.evaluate(f"bbOnline.S.blocks.get('{line_id}').data.color"))

    # swap ends
    before = pg.evaluate(f"bbOnline.S.blocks.get('{line_id}').data.from")
    line_menu("Swap which end points")
    check("swapping turns it round",
          pg.evaluate(f"bbOnline.S.blocks.get('{line_id}').data.to") == before)

    # ================================================ boxes and circles
    rclick(380, 700)
    pickmenu("Box to group things")          # this arms the tool; the box is then drawn out
    pg.wait_for_timeout(250)
    pg.mouse.move(380, 640)
    pg.mouse.down()
    pg.mouse.move(700, 850, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(450)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(250)
    sh = pg.evaluate("[...bbOnline.S.blocks.values()].filter(b => b.kind === 'shape').pop()")
    check("a box is added", bool(sh), sh and sh["kind"])
    check("it sits behind the other blocks",
          pg.evaluate("""() => { const B = bbOnline.S.blocks, shz = [...B.values()]
              .filter(b => b.kind === 'shape').map(b => b.z),
              rest = [...B.values()].filter(b => b.kind !== 'shape' && b.kind !== 'arrow').map(b => b.z);
              return Math.max(...shz) < Math.min(...rest); }"""))
    check("only its rim takes clicks", pg.evaluate("""() => {
        const d = document.querySelector('.ol-b[data-kind=shape]');
        return getComputedStyle(d).pointerEvents === 'none'
            && d.querySelectorAll('.ol-srim').length === 4
            && getComputedStyle(d.querySelector('.ol-srim')).pointerEvents === 'auto'; }"""))

    sid = sh["id"]
    r = pg.evaluate(f"""(() => {{ const d = document.querySelector('.ol-b[data-id=\"{sid}\"] .ol-srim.n');
        const q = d.getBoundingClientRect(); return [q.x + q.width / 2, q.y + q.height / 2]; }})()""")
    pg.mouse.click(r[0], r[1], button="right")
    pg.wait_for_timeout(300)
    check("the box has its own options",
          any("Make it a circle" in t for t in pg.locator(".ol-ctx button").all_inner_texts()),
          ", ".join(pg.locator(".ol-ctx button").all_inner_texts())[:80])
    pickmenu("Make it a circle")
    check("it becomes a circle",
          pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{sid}\"]').dataset.form") == "ellipse")
    pg.mouse.click(r[0], r[1], button="right")
    pg.wait_for_timeout(300)
    pickmenu("Shade it in")
    check("shading it in",
          pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{sid}\"]').classList.contains('fill')"))

    # ================================================ locking
    pg.mouse.click(*at(a_id), button="right")
    pg.wait_for_timeout(300)
    pickmenu("Lock it in place")
    check("locked", pg.evaluate(f"!!bbOnline.S.blocks.get('{a_id}').data.lock"))
    check("it shows as locked",
          pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{a_id}\"]').classList.contains('locked')"))
    pos = blk(a_id)
    p0 = at(a_id)
    pg.mouse.move(p0[0], p0[1])
    pg.mouse.down()
    pg.mouse.move(p0[0] + 160, p0[1] + 90, steps=6)
    pg.mouse.up()
    pg.wait_for_timeout(400)
    after = blk(a_id)
    check("a locked block will not be dragged", after["x"] == pos["x"] and after["y"] == pos["y"],
          f'{pos["x"]},{pos["y"]} -> {after["x"]},{after["y"]}')
    pg.mouse.click(*at(a_id), button="right")
    pg.wait_for_timeout(300)
    pickmenu("Unlock it")
    check("unlocked again", not pg.evaluate(f"!!bbOnline.S.blocks.get('{a_id}').data.lock"))

    # ================================================ lining things up
    ids = pg.evaluate("""() => [...bbOnline.S.blocks.values()]
        .filter(b => b.kind === 'note').map(b => b.id)""")
    if len(ids) >= 2:
        pg.evaluate("""ids => { bbOnline.S.sel.clear(); ids.forEach(i => bbOnline.S.sel.add(i)); }""", ids)
        pg.mouse.click(*at(ids[0]), button="right")
        pg.wait_for_timeout(300)
        check("several picked offers lining up",
              any("Down the left" in t for t in pg.locator(".ol-ctx button").all_inner_texts()),
              ", ".join(pg.locator(".ol-ctx button").all_inner_texts())[:80])
        pickmenu("Down the left")
        xs = pg.evaluate("ids => ids.map(i => bbOnline.S.blocks.get(i).x)", ids)
        check("they all share a left edge", len(set(xs)) == 1, xs)

        pg.evaluate("""ids => { bbOnline.S.sel.clear(); ids.forEach(i => bbOnline.S.sel.add(i)); }""", ids)
        pg.mouse.click(*at(ids[0]), button="right")
        pg.wait_for_timeout(300)
        pickmenu("Spread out in a row")
        laid = pg.evaluate("""ids => ids.map(i => bbOnline.S.blocks.get(i))
            .sort((a, b) => a.x - b.x).map(b => [b.x, b.w])""", ids)
        gaps = [round(laid[i + 1][0] - (laid[i][0] + laid[i][1])) for i in range(len(laid) - 1)]
        check("spread out with an even gap", len(set(gaps)) == 1, gaps)
    else:
        print("  SKIP  lining up (not enough notes)")

    # ================================================ find on the board
    pg.evaluate(f"""() => {{ const b = bbOnline.S.blocks.get('{b_id}');
        b.data = {{...b.data, html: 'Dijkstra shortest path'}}; bbOnline.paint(b); }}""")
    pg.wait_for_timeout(200)
    pg.keyboard.press("Control+f")
    pg.wait_for_timeout(350)
    check("Ctrl+F puts the cursor in the corner search box",
          pg.evaluate("document.activeElement.classList.contains('ol-huntq')"))
    pg.keyboard.type("dijkstra")
    pg.wait_for_timeout(600)
    check("it finds the note", pg.locator(".ol-huntn").inner_text() == "1/1",
          pg.locator(".ol-huntn").inner_text())
    check("and highlights it",
          pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{b_id}\"]').classList.contains('found')"))
    check("and brings it into view", pg.evaluate(f"""() => {{
        const r = document.querySelector('.ol-b[data-id=\"{b_id}\"]').getBoundingClientRect(),
              b = document.querySelector('.ol-board').getBoundingClientRect();
        return r.left > b.left && r.right < b.right && r.top > b.top && r.bottom < b.bottom; }}"""))
    pg.keyboard.press("Control+a")                 # must not select the whole board while typing
    pg.wait_for_timeout(200)
    check("Ctrl+A in the finder does not select every block",
          pg.evaluate("bbOnline.S.sel.size") <= 1, pg.evaluate("bbOnline.S.sel.size"))
    pg.locator(".ol-hunt input").fill("nothingmatchesthis")
    pg.wait_for_timeout(500)
    check("says so when nothing matches",
          pg.locator(".ol-huntn").inner_text() == "none",
          pg.locator(".ol-huntn").inner_text())
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    check("Escape empties the search box", pg.input_value(".ol-hunt input") == "")

    # ================================================ ready-made layouts
    n0 = pg.evaluate("bbOnline.S.blocks.size")
    rclick_bare()
    check("empty space offers a ready-made layout",
          any("To do" in t for t in pg.locator(".ol-ctx button").all_inner_texts()),
          ", ".join(pg.locator(".ol-ctx button").all_inner_texts())[:90])
    pickmenu("To do")
    pg.wait_for_timeout(600)
    check("it adds the three columns and some stickies",
          pg.evaluate("bbOnline.S.blocks.size") >= n0 + 5, pg.evaluate("bbOnline.S.blocks.size"))
    check("the columns are boxes behind the stickies", pg.evaluate("""() => {
        const v = [...bbOnline.S.blocks.values()], sh = v.filter(b => b.kind === 'shape');
        return sh.length >= 3 && Math.max(...sh.map(b => b.z))
             < Math.min(...v.filter(b => b.kind === 'sticky').map(b => b.z)); }"""))

    n1 = pg.evaluate("bbOnline.S.blocks.size")
    rclick_bare()
    pickmenu("Revision plan")
    pg.wait_for_timeout(600)
    check("the revision plan comes with its lines joined up",
          pg.evaluate("[...bbOnline.S.blocks.values()].filter(b => b.kind === 'arrow').length") >= 3,
          pg.evaluate("[...bbOnline.S.blocks.values()].filter(b => b.kind === 'arrow').length"))
    check("and adds its blocks", pg.evaluate("bbOnline.S.blocks.size") > n1)

    # ================================================ a line survives a save and reload
    saved = pg.evaluate("""() => JSON.stringify([...bbOnline.S.blocks.values()]
        .filter(b => b.kind === 'arrow').map(b => [b.data.form, b.data.ends, b.data.label]))""")
    check("the line settings are part of the block data", "elbow" in saved and "then" in saved, saved[:90])

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
