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
        const p = document.querySelector('.ol-links > path:not(.hit)');
        return !p.getAttribute('marker-end') && !p.getAttribute('marker-start'); }"""))

    def line_menu(text):
        h = pg.evaluate(ON_LINE)
        pg.mouse.click(h[0], h[1], button="right")
        pg.wait_for_timeout(300)
        pickmenu(text)

    line_menu("Arrows both ways")
    check("arrow heads on both ends", pg.evaluate("""() => {
        const p = document.querySelector('.ol-links > path:not(.hit)');
        return !!p.getAttribute('marker-end') && !!p.getAttribute('marker-start'); }"""))

    line_menu("Straight")
    d = pg.evaluate("document.querySelector('.ol-links > path:not(.hit)').getAttribute('d')")
    check("straight is one segment", d.count("L") == 1 and "Q" not in d, d)

    line_menu("Right angle")
    d = pg.evaluate("document.querySelector('.ol-links > path:not(.hit)').getAttribute('d')")
    check("right angle turns three times", d.count("L") == 3, d)

    was = pg.evaluate("""() => !!document.querySelector('.ol-links > path:not(.hit)').getAttribute('stroke-dasharray')""")
    line_menu("Dashed")
    check("Dashed switches the dashes", pg.evaluate("""() => !!document.querySelector('.ol-links > path:not(.hit)').getAttribute('stroke-dasharray')""") != was, was)
    line_menu("Dashed")
    check("and back again", pg.evaluate("""() => !!document.querySelector('.ol-links > path:not(.hit)').getAttribute('stroke-dasharray')""") == was)

    line_menu("Thicker")
    check("thicker", pg.evaluate("""() => document.querySelector('.ol-links > path:not(.hit)')
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


    # ================================================ lines that mean something, bend, and lead somewhere
    # a board of its own for this part, so nothing above it is in the way
    pg.evaluate("""() => { const o = bbOnline;
        [...o.S.blocks.keys()].forEach(id => o.S.blocks.delete(id));
        document.querySelectorAll('.ol-b').forEach(d => d.remove());
        o.S.nodes.clear(); o.S.sel.clear(); o.drawLinks(); }""")
    pg.evaluate("() => bbOnline.view({x: 60, y: 50, k: 1})")
    pg.wait_for_timeout(250)
    plan = pg.evaluate("""() => { const o = bbOnline;
        const mk = (x, y, t) => o.add('note', x, y, {html: t}, {size: [200, 110], centre: false}).id;
        const a = mk(120, 120, 'read the notes'), b = mk(560, 120, 'do the worksheet'),
              c = mk(1000, 120, 'hand it in');
        const l1 = o.add('arrow', 0, 0, {from: a, to: b}, {size: [0, 0], centre: false}).id;
        const l2 = o.add('arrow', 0, 0, {from: b, to: c}, {size: [0, 0], centre: false}).id;
        return {a, b, c, l1, l2}; }""")
    pg.wait_for_timeout(350)

    def line(lid):
        return pg.evaluate(f"() => bbOnline.S.blocks.get('{lid}').data")

    def line_d():
        return pg.evaluate("() => document.querySelector('.ol-links > path.ln').getAttribute('d')")

    def says():
        return pg.evaluate("""() => [...document.querySelectorAll('.ol-links text')].map(t => t.textContent)""")

    # ---- what a line means
    check("a new line means nothing in particular to begin with",
          pg.evaluate(f"() => bbOnline.relOf(bbOnline.S.blocks.get('{plan['l1']}'))") == "plain")
    check("and writes nothing on itself", says() == [], says())
    pg.evaluate(f"""() => {{ const o = bbOnline;
        o.S.blocks.get('{plan['l1']}').data = {{...o.S.blocks.get('{plan['l1']}').data,
          rel: 'depends', ...{{ends: 'to', dash: true, color: '#0a84ff'}}}};
        o.put(o.S.blocks.get('{plan['l1']}')); }}""")
    pg.wait_for_timeout(300)
    check("saying what it means writes the words on it", "depends on" in says(), says())
    check("and gives it the look that goes with it",
          line(plan["l1"])["color"] == "#0a84ff" and line(plan["l1"])["ends"] == "to")
    check("every meaning on offer has a name and a word",
          pg.evaluate("""() => Object.values(bbOnline.REL).every(r => typeof r.name === 'string' && r.name
              && typeof r.word === 'string')"""))
    # your own words win over the meaning
    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{plan['l1']}');
        b.data = {{...b.data, label: 'needs first'}}; o.put(b); }}""")
    pg.wait_for_timeout(300)
    check("your own words are shown instead of the meaning",
          "needs first" in says() and "depends on" not in says(), says())

    # ---- writing on a line where the line is
    ON = """(id) => { const p = [...document.querySelectorAll('.ol-links path.hit')]
        .find(h => h.dataset.id === id);
        const pt = p.getPointAtLength(p.getTotalLength() / 2), m = p.getScreenCTM();
        return [pt.x * m.a + pt.y * m.c + m.e, pt.x * m.b + pt.y * m.d + m.f]; }"""
    where = pg.evaluate(ON, plan["l2"])
    pg.mouse.dblclick(where[0], where[1])
    pg.wait_for_timeout(400)
    check("double-clicking a line opens a box to write in, on the line",
          pg.is_visible(".ol-lbl"))
    box = pg.locator(".ol-lbl").bounding_box()
    check("and the box really is over the middle of the line",
          abs(box["x"] + box["width"] / 2 - where[0]) < 40 and abs(box["y"] + box["height"] / 2 - where[1]) < 40,
          f'{box["x"]:.0f},{box["y"]:.0f} vs {where[0]:.0f},{where[1]:.0f}')
    pg.keyboard.type("then")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)
    check("what you type lands on the line", line(plan["l2"]).get("label") == "then",
          line(plan["l2"]).get("label"))
    check("and the box goes away", pg.is_hidden(".ol-lbl"))
    pg.mouse.dblclick(where[0], where[1])
    pg.wait_for_timeout(350)
    pg.keyboard.press("Control+a")
    pg.keyboard.type("scrapped")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(350)
    check("Escape leaves the line as it was", line(plan["l2"]).get("label") == "then",
          line(plan["l2"]).get("label"))

    # ---- bending a line, and straightening it
    pg.evaluate(f"() => bbOnline.select(['{plan['l2']}'])")
    pg.wait_for_timeout(300)
    check("a picked-out line grows a handle in its middle",
          pg.evaluate("() => document.querySelectorAll('.ol-links circle').length") == 1)
    hb = pg.evaluate("""() => { const c = document.querySelector('.ol-links circle');
        const r = c.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }""")
    pg.mouse.move(hb["x"], hb["y"])
    pg.mouse.down()
    pg.mouse.move(hb["x"] + 30, hb["y"] + 170, steps=12)
    pg.mouse.up()
    pg.wait_for_timeout(450)
    bend = line(plan["l2"]).get("bend")
    check("dragging the handle bends the line", bool(bend) and isinstance(bend, dict), bend)
    check("and the bend is kept on the line, so it survives a save",
          "bend" in pg.evaluate(f"() => JSON.stringify(bbOnline.S.blocks.get('{plan['l2']}').data)"))
    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{plan['l2']}');
        b.data = {{...b.data, bend: null}}; o.put(b); }}""")
    pg.wait_for_timeout(300)
    check("straightening it takes the bend away", line(plan["l2"]).get("bend") is None)

    # ---- which side each end attaches to
    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{plan['l1']}');
        b.data = {{...b.data, form: 'straight', a: 's', b: 'n', bend: null}}; o.put(b); }}""")
    pg.wait_for_timeout(300)
    ends = pg.evaluate(f"""() => {{ const o = bbOnline, l = o.S.blocks.get('{plan['l1']}');
        const p = o.linkPath(o.S.blocks.get(l.data.from), o.S.blocks.get(l.data.to), l.data, []);
        return p.d; }}""")
    sides = pg.evaluate(f"""() => {{ const o = bbOnline, a = o.S.blocks.get('{plan['a']}'),
        c = o.S.blocks.get('{plan['b']}'), l = o.S.blocks.get('{plan['l1']}');
        const p = o.linkPath(a, c, l.data, []);
        const m = p.d.match(/M([-\d.]+),([-\d.]+) L([-\d.]+),([-\d.]+)/).slice(1).map(Number);
        return {{startsBelow: m[1] > a.y + a.h - 1, endsAbove: m[3] < c.y + 1,
                 startMiddle: Math.abs(m[0] - (a.x + a.w / 2)) < 2}}; }}""")
    check("an end can be pinned to the bottom of its block", sides["startsBelow"], sides)
    check("and the other to the top of its block", sides["endsAbove"], sides)
    check("pinned ends sit in the middle of that side", sides["startMiddle"], sides)
    moved = pg.evaluate(f"""() => {{ const o = bbOnline, a = o.S.blocks.get('{plan['a']}');
        a.y += 260; o.put(a);
        const l = o.S.blocks.get('{plan['l1']}'), c = o.S.blocks.get('{plan['b']}');
        const p = o.linkPath(a, c, l.data, []);
        const m = p.d.match(/M([-\d.]+),([-\d.]+)/).slice(1).map(Number);
        return m[1] > a.y + a.h - 1; }}""")
    check("and the choice follows the block as it is dragged about", moved)
    pg.evaluate(f"""() => {{ const o = bbOnline, a = o.S.blocks.get('{plan['a']}');
        a.y -= 260; o.put(a);
        const b = o.S.blocks.get('{plan['l1']}'); b.data = {{...b.data, a: null, b: null, form: 'elbow'}}; o.put(b); }}""")
    pg.wait_for_timeout(300)

    # ---- stepping round what is in the way
    inway = pg.evaluate("""() => { const o = bbOnline;
        return o.inTheWay(0, 0, 100, 0, [{id: 'x', kind: 'note', x: 40, y: -10, w: 20, h: 20}], []); }""")
    check("a block sitting across a run of line is noticed", inway == 1, inway)
    check("and the blocks at the two ends are not counted",
          pg.evaluate("""() => bbOnline.inTheWay(0, 0, 100, 0,
              [{id: 'x', kind: 'note', x: 40, y: -10, w: 20, h: 20}], ['x'])""") == 0)
    dodged = pg.evaluate(f"""() => {{ const o = bbOnline;
        // something right between the first two, where the line would normally run
        const mid = o.add('note', 380, 100, {{html: 'in the way'}}, {{size: [160, 150], centre: false}});
        const l = o.S.blocks.get('{plan['l1']}');
        const a = o.S.blocks.get(l.data.from), c = o.S.blocks.get(l.data.to);
        const solids = [...o.S.blocks.values()].filter(x => !['arrow', 'shape', 'ink'].includes(x.kind));
        const plain = o.linkPath(a, c, {{...l.data, dodge: false}}, solids);
        const round_ = o.linkPath(a, c, {{...l.data, dodge: true}}, solids);
        const cost = d => {{ const n = d.match(/[ML]([-\d.]+),([-\d.]+)/g).map(s =>
              s.slice(1).split(',').map(Number));
            let t = 0;
            for (let i = 1; i < n.length; i++)
              t += o.inTheWay(n[i-1][0], n[i-1][1], n[i][0], n[i][1], solids, [a.id, c.id]);
            return t; }};
        return {{mid: mid.id, plain: cost(plain.d), round: cost(round_.d), d: round_.d}}; }}""")
    pg.wait_for_timeout(300)
    check("the usual line runs straight through it", dodged["plain"] > 0, dodged)
    check("and asked to, the line steps round it instead", dodged["round"] < dodged["plain"], dodged)
    pg.evaluate(f"""() => {{ const o = bbOnline; o.S.blocks.delete('{dodged['mid']}');
        document.querySelector('.ol-b[data-id="{dodged['mid']}"]')?.remove();
        o.S.nodes.delete('{dodged['mid']}'); o.drawLinks(); }}""")
    pg.wait_for_timeout(200)

    # ---- following the lines
    check("a block knows which lines are on it",
          pg.evaluate(f"() => bbOnline.linesOn('{plan['b']}').length") == 2)
    chain = pg.evaluate(f"() => bbOnline.chainFrom(['{plan['a']}']).length")
    check("one block picks out the whole chain it belongs to", chain == 5, chain)
    was = pg.evaluate("() => ({...bbOnline.S.view})")
    pg.evaluate(f"() => bbOnline.goTo(bbOnline.S.blocks.get('{plan['c']}'))")
    pg.wait_for_timeout(350)
    now = pg.evaluate("() => ({...bbOnline.S.view})")
    check("following a line travels to the block at the other end",
          abs(now["x"] - was["x"]) + abs(now["y"] - was["y"]) > 40,
          f'{was["x"]:.0f},{was["y"]:.0f} -> {now["x"]:.0f},{now["y"]:.0f}')
    check("and does not change how far in you are zoomed", abs(now["k"] - was["k"]) < 1e-9)
    check("and the block it travelled to is the one picked out",
          pg.evaluate(f"() => [...bbOnline.S.sel][0] === '{plan['c']}'"))

    # Enter on a picked-out line walks along it
    pg.evaluate("() => bbOnline.view({x: 60, y: 50, k: 1})")
    pg.evaluate(f"() => bbOnline.select(['{plan['l1']}'])")
    pg.wait_for_timeout(250)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)
    check("Enter on a line goes to what it points at",
          pg.evaluate(f"() => [...bbOnline.S.sel][0] === '{plan['b']}'"),
          pg.evaluate("() => [...bbOnline.S.sel]"))

    # hovering a line lights both blocks it joins
    hv = pg.evaluate(ON, plan["l2"])
    pg.mouse.move(hv[0], hv[1])
    pg.wait_for_timeout(350)
    check("hovering a line lights up both blocks it joins",
          pg.evaluate("() => document.querySelectorAll('.ol-b.lit').length") == 2,
          pg.evaluate("() => document.querySelectorAll('.ol-b.lit').length"))
    pg.mouse.move(hv[0], hv[1] - 300)
    pg.wait_for_timeout(300)
    check("and they go out again when it leaves",
          pg.evaluate("() => document.querySelectorAll('.ol-b.lit').length") == 0)

    # the lines of whatever is picked out stand out from the rest
    pg.evaluate(f"() => bbOnline.select(['{plan['a']}'])")
    pg.wait_for_timeout(300)
    check("picking a block out makes its own lines stand out",
          pg.evaluate("() => document.querySelectorAll('.ol-links path.ln.near').length") == 1,
          pg.evaluate("() => document.querySelectorAll('.ol-links path.ln.near').length"))

    # ---- numbering the steps
    k = pg.evaluate(f"() => bbOnline.numberChain('{plan['a']}')")
    pg.wait_for_timeout(400)
    check("numbering a chain numbers every line in it", k == 2, k)
    check("the first step really is the first one",
          line(plan["l1"])["step"] == 1 and line(plan["l2"])["step"] == 2,
          [line(plan["l1"]).get("step"), line(plan["l2"]).get("step")])
    check("and the number is written on the line",
          any(t.startswith("1.") for t in says()), says())
    pg.evaluate(f"() => bbOnline.clearSteps('{plan['a']}')")
    pg.wait_for_timeout(350)
    check("taking the numbers off leaves the words alone",
          line(plan["l1"]).get("step") is None and line(plan["l1"]).get("label") == "needs first")

    # ---- far out, and on the little map
    check("a line keeps one weight on screen however far out you pull",
          pg.evaluate("""() => getComputedStyle(document.querySelector('.ol-links path.ln'))
              .vectorEffect === 'non-scaling-stroke'"""))
    check("and stays as easy to click out there",
          pg.evaluate("""() => getComputedStyle(document.querySelector('.ol-links path.hit'))
              .vectorEffect === 'non-scaling-stroke'"""))
    pg.evaluate("() => bbOnline.view({x: 60, y: 50, k: 0.25})")
    pg.wait_for_timeout(400)
    check("the writing on a line is still about the same size far out",
          pg.evaluate("""() => { const t = document.querySelector('.ol-links text');
            return t ? Math.round(t.getBoundingClientRect().height) : 0; }""") >= 8,
          pg.evaluate("""() => { const t = document.querySelector('.ol-links text');
            return t ? Math.round(t.getBoundingClientRect().height) : 0; }"""))
    pg.evaluate("() => bbOnline.view({x: 60, y: 50, k: 1})")
    pg.wait_for_timeout(250)

    if pg.is_hidden(".ol-map"):
        pg.click(".ol-mapb")
        pg.wait_for_timeout(500)

    def map_ink():
        return pg.evaluate("""() => { const c = document.querySelector('.ol-map canvas');
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }""")

    with_lines = map_ink()
    pg.evaluate("""() => { const o = bbOnline;
        o.__kept = [...o.S.blocks.values()].filter(b => b.kind === 'arrow');
        o.__kept.forEach(b => o.S.blocks.delete(b.id));
        o.drawLinks(); o.view({...o.S.view}); }""")
    pg.wait_for_timeout(500)
    without = map_ink()
    pg.evaluate("""() => { const o = bbOnline;
        o.__kept.forEach(b => o.S.blocks.set(b.id, b)); o.drawLinks(); o.view({...o.S.view}); }""")
    pg.wait_for_timeout(400)
    check("the lines are drawn on the little map too", with_lines > without,
          f"{with_lines} with, {without} without")
    pg.keyboard.press("m")
    pg.wait_for_timeout(300)

    # ---- a line to an output block is one of these lines, not a special case
    joined = pg.evaluate("""() => { const o = bbOnline;
        const c = o.add('code', 140, 700, {lang: 'javascript', text: "console.log(1)"},
                        {size: [320, 160], centre: false});
        const out = o.add('output', 620, 700, {title: 'Result'}, {size: [300, 160], centre: false});
        const l = o.add('arrow', 0, 0, {from: out.id, to: c.id}, {size: [0, 0], centre: false});
        return {code: c.id, out: out.id, line: l.id}; }""")
    pg.wait_for_timeout(400)
    jl = line(joined["line"])
    check("a line drawn to an output block says what it is for", jl.get("rel") == "feeds", jl.get("rel"))
    check("and points from the code at the output, however it was drawn",
          jl["from"] == joined["code"] and jl["to"] == joined["out"])
    check("it has an arrowhead, like any line that leads somewhere", jl["ends"] == "to")
    check("and it is an ordinary line you can bend and write on",
          pg.evaluate(f"""() => !!bbOnline.lineText(bbOnline.S.blocks.get('{joined['line']}'))"""))

    # once joined, the result shows in the output block only
    check("the code block stops showing a result of its own and says where it went",
          pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{joined['code']}"] .ol-outgone')""")
          and not pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{joined['code']}"] .ol-out')"""))
    pg.evaluate(f"""() => {{ const o = bbOnline, c = o.S.blocks.get('{joined['code']}');
        c.data = {{...c.data, out: {{text: 'one', ok: true, by: 'Me'}}}}; o.put(c); }}""")
    pg.wait_for_timeout(300)
    check("even with a result of its own stored, the code block shows none",
          not pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{joined['code']}"] .ol-out')"""))
    pg.evaluate(f"() => bbOnline.remove(['{joined['line']}'])")
    pg.wait_for_timeout(400)
    check("taking the line away gives the code block its own panel back",
          pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{joined['code']}"] .ol-out')""")
          and not pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{joined['code']}"] .ol-outgone')"""))

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
