"""Zoomed far out, the little map, the corner search, and boxes that size themselves."""
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
    pg = br.new_context(viewport={"width": 1400, "height": 900}).new_page()
    pg.add_init_script(STUB)
    pg.add_init_script(NOSERVER)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL)
    pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]')
    pg.wait_for_timeout(500)
    pg.click(".ol-card .alt")
    pg.wait_for_timeout(400)

    ids = pg.evaluate("""() => { const o = bbOnline;
        return {note: o.add('note', 200, 200, {html: 'Dijkstra shortest path revision'}).id,
                todo: o.add('todo', 700, 200, {items: [{t: 'revise graphs', done: false}]}).id,
                code: o.add('code', 200, 700, {lang: 'python', text: 'def main():'}).id,
                shape: o.add('shape', 900, 700, {title: 'ALPHA'}).id}; }""")
    pg.wait_for_timeout(500)

    # ================================================ zoomed far out
    def far(k):
        pg.evaluate(f"() => bbOnline.view({{x: 60, y: 40, k: {k}}})")
        pg.wait_for_timeout(400)

    far(0.8)
    check("close in, a block shows what is written on it",
          not pg.evaluate("document.querySelector('.ol').classList.contains('far')"))
    far(0.25)
    check("far out, the board knows it",
          pg.evaluate("document.querySelector('.ol').classList.contains('far')"))

    def tag(k):
        return pg.evaluate(f"""() => {{ const d = document.querySelector('.ol-b[data-id="{ids[k]}"]');
            const t = d.querySelector('.ol-tag');
            return {{text: t.textContent, shown: getComputedStyle(t).display !== 'none',
                     px: parseFloat(getComputedStyle(t).fontSize),
                     fits: t.getBoundingClientRect().right <= d.getBoundingClientRect().right + 1,
                     inner: [...d.children].filter(c => !c.classList.contains('ol-tag')
                        && !c.classList.contains('ol-rs') && !c.classList.contains('ol-srim')
                        && getComputedStyle(c).display !== 'none').length}}; }}""")

    n = tag('note')
    check("each block shows its name instead of a smudge", n["shown"] and "Dijkstra" in n["text"], n["text"])
    check("and the writing underneath is out of the way", n["inner"] == 0, str(n["inner"]))
    check("the name stays inside the block", n["fits"])
    check("and is the same size on screen however far out you are",
          abs(n["px"] * 0.25 - 13) < 1.5, f'{n["px"]:.0f}px at 25%')

    t = tag('todo')
    check("a checklist names what is still to do", t["text"].strip() == "revise graphs", t["text"])
    c = tag('code')
    check("code shows its first line", c["text"].strip() == "def main():", c["text"])

    sh = pg.evaluate(f"""() => {{ const d = document.querySelector('.ol-b[data-id="{ids['shape']}"]');
        return {{bg: getComputedStyle(d).backgroundColor,
                 rims: [...d.querySelectorAll('.ol-srim')].filter(r => getComputedStyle(r).display !== 'none').length,
                 name: d.querySelector('.ol-tag').textContent}}; }}""")
    check("a box stays a box rather than turning into a card",
          sh["bg"] in ("rgba(0, 0, 0, 0)", "transparent") and sh["rims"] > 0,
          f'{sh["bg"]} rims={sh["rims"]}')
    check("and keeps its name on", sh["name"].strip() == "ALPHA", sh["name"])

    far(1)

    # ================================================ the little map
    check("the map is put away to begin with", pg.is_hidden(".ol-map"))
    pg.click(".ol-mapb")
    pg.wait_for_timeout(500)
    check("the corner button brings it up", pg.is_visible(".ol-map"))
    check("it has drawn something",
          pg.evaluate("""() => { const c = document.querySelector('.ol-map canvas');
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }""") > 200)
    check("and shows which part you are looking at",
          pg.evaluate("() => document.querySelector('.ol-mapv').offsetWidth > 4"))

    was = pg.evaluate("() => ({...bbOnline.S.view})")
    box = pg.locator(".ol-map canvas").bounding_box()
    pg.mouse.click(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.8)
    pg.wait_for_timeout(400)
    now = pg.evaluate("() => ({...bbOnline.S.view})")
    check("clicking the map takes you there", abs(now["x"] - was["x"]) + abs(now["y"] - was["y"]) > 30,
          f'{was["x"]:.0f},{was["y"]:.0f} -> {now["x"]:.0f},{now["y"]:.0f}')

    pg.keyboard.press("m")
    pg.wait_for_timeout(400)
    check("M puts it away again", pg.is_hidden(".ol-map"))
    pg.keyboard.press("m")
    pg.wait_for_timeout(300)

    # ================================================ the search in the corner
    check("the search box is always there", pg.is_visible(".ol-hunt input"))
    pg.click(".ol-hunt input")
    pg.keyboard.type("dijkstra")
    pg.wait_for_timeout(600)
    check("typing finds it", pg.locator(".ol-huntn").inner_text() == "1/1",
          pg.locator(".ol-huntn").inner_text())
    check("and the block is picked out",
          pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{ids['note']}\"]').classList.contains('found')"))
    pg.locator(".ol-hunt input").fill("graphs")
    pg.wait_for_timeout(600)
    check("it looks inside checklists too", pg.locator(".ol-huntn").inner_text() == "1/1",
          pg.locator(".ol-huntn").inner_text())
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    check("Escape clears it", pg.input_value(".ol-hunt input") == "")

    # ================================================ a box sizes itself to what is in it
    pg.evaluate("""() => { const o = bbOnline;
        [...o.S.blocks.keys()].forEach(id => o.S.blocks.delete(id));
        document.querySelectorAll('.ol-b').forEach(d => d.remove());
        o.S.nodes.clear(); o.S.sel.clear(); }""")
    pg.wait_for_timeout(200)
    made = pg.evaluate("""() => { const o = bbOnline;
        const s = o.add('shape', 300, 300, {title: 'Week 1'});
        s.w = 400; s.h = 300; o.put(s);
        const n = o.add('sticky', 350, 400, {text: 'inside'});
        return {shape: s.id, note: n.id}; }""")
    pg.wait_for_timeout(400)
    pg.evaluate("() => bbOnline.view({x: 20, y: 20, k: 1})")
    pg.wait_for_timeout(300)

    def size(i):
        return pg.evaluate(f"() => {{ const b = bbOnline.S.blocks.get('{i}'); "
                           "return {x: b.x, y: b.y, w: b.w, h: b.h}; }")

    before = size(made["shape"])
    # drag the note into the bottom corner of the box, so it still belongs to the box but hangs
    # over its edge - the box should let itself out to cover it
    nb = pg.evaluate(f"""() => {{ const o = bbOnline, s = o.S.blocks.get('{made['shape']}'),
        n = o.S.blocks.get('{made['note']}'), v = o.S.view;
        const d = document.querySelector('.ol-b[data-id="{made['note']}"]');
        const r = d.getBoundingClientRect();
        const wantX = s.x + s.w - 14 - n.w / 2, wantY = s.y + s.h - 14 - n.h / 2;
        return {{x: r.left + r.width / 2, y: r.top + 10,
                 dx: (wantX - n.x) * v.k, dy: (wantY - n.y) * v.k}}; }}""")
    pg.mouse.move(nb["x"], nb["y"])
    pg.mouse.down()
    pg.mouse.move(nb["x"] + nb["dx"], nb["y"] + nb["dy"], steps=12)
    pg.mouse.up()
    pg.wait_for_timeout(700)
    after = size(made["shape"])
    held = pg.evaluate(f"""() => {{ const o = bbOnline, s = o.S.blocks.get('{made['shape']}'),
        n = o.S.blocks.get('{made['note']}');
        return n.x >= s.x && n.y >= s.y && n.x + n.w <= s.x + s.w && n.y + n.h <= s.y + s.h; }}""")
    check("dragging something out to the edge grows the box",
          after["w"] > before["w"] or after["h"] > before["h"],
          f'{before["w"]}x{before["h"]} -> {after["w"]}x{after["h"]}')
    check("and what was in it is still in it", held)

    # it cannot be pulled in smaller than its contents
    pg.click(f'.ol-b[data-id="{made["shape"]}"] .ol-srim')
    pg.wait_for_timeout(300)
    rs = pg.evaluate(f"""() => {{ const d = document.querySelector('.ol-b[data-id="{made['shape']}"] .ol-rs');
        if (!d) return null; const r = d.getBoundingClientRect();
        return {{x: r.left + r.width / 2, y: r.top + r.height / 2}}; }}""")
    if rs:
        was = size(made["shape"])
        pg.mouse.move(rs["x"], rs["y"])
        pg.mouse.down()
        pg.mouse.move(rs["x"] - 320, rs["y"] - 260, steps=14)
        pg.mouse.up()
        pg.wait_for_timeout(500)
        small = size(made["shape"])
        still = pg.evaluate(f"""() => {{ const o = bbOnline, s = o.S.blocks.get('{made['shape']}'),
            n = o.S.blocks.get('{made['note']}');
            return n.x + n.w <= s.x + s.w + 1 && n.y + n.h <= s.y + s.h + 1; }}""")
        check("it will not be dragged in smaller than what it holds", still,
              f'{was["w"]}x{was["h"]} -> {small["w"]}x{small["h"]}')
    else:
        check("it will not be dragged in smaller than what it holds", False, "no handle")

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
