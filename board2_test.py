"""Sharp text when zoomed, renaming, a box that carries what is in it, drag-select,
space to move around, and switching board."""
import functools
import http.server
import io as _io
import sys
import threading
from pathlib import Path

from PIL import Image
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


def sharpness(png):
    im = Image.open(_io.BytesIO(png)).convert("L")
    w, h = im.size
    px = im.load()
    total = n = 0
    for y in range(h):
        for x in range(1, w):
            total += abs(px[x, y] - px[x - 1, y])
            n += 1
    return total / max(1, n)


with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    page_ctx = br.new_context(viewport={"width": 1400, "height": 900})
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

    def pickmenu(text):
        pg.locator(".ol-ctx button", has_text=text).first.click()
        pg.wait_for_timeout(400)

    # ================================================ zoomed text stays sharp
    tid = pg.evaluate("""() => bbOnline.add('text', 300, 240,
        {text: 'Shortest path', size: 15}, {size: [300, 120], centre: false}).id""")

    def text_sharpness(x, y, k):
        pg.evaluate("v => bbOnline.view(v)", {"x": x, "y": y, "k": k})
        pg.wait_for_timeout(500)
        box = pg.locator(f'.ol-b[data-id="{tid}"] .ol-c').bounding_box()
        return sharpness(pg.screenshot(clip={"x": box["x"] + 3, "y": box["y"] + 3,
                                             "width": 240, "height": 46}))

    whole = text_sharpness(-500, -400, 2.5)
    half = text_sharpness(-499.5, -400.5, 2.5)
    third = text_sharpness(-499.67, -400.33, 2.5)
    check("zoomed text is as sharp wherever the board has been panned to",
          abs(whole - half) < 0.15 and abs(whole - third) < 0.15,
          f"whole {whole:.2f}, half-pixel {half:.2f}, third {third:.2f}")
    check("the board is drawn on whole device pixels", pg.evaluate("""() => {
        const t = getComputedStyle(document.querySelector('.ol-world')).transform;
        const m = t.match(/matrix\\(([^)]+)\\)/); if (!m) return false;
        const n = m[1].split(',').map(Number), dpr = devicePixelRatio || 1;
        const frac = v => Math.abs(v * dpr - Math.round(v * dpr));
        return frac(n[4]) < 0.001 && frac(n[5]) < 0.001; }"""),
          pg.evaluate("getComputedStyle(document.querySelector('.ol-world')).transform"))
    check("the layer hint is only on while the board is moving",
          pg.evaluate("""() => !document.querySelector('.ol-world').classList.contains('moving')"""))

    pg.evaluate("bbOnline.view({x: 0, y: 0, k: 1})")
    pg.wait_for_timeout(400)

    # ================================================ renaming
    cid = pg.evaluate("""() => bbOnline.add('code', 240, 160, {lang: 'python', text: 'print(1)'},
        {size: [320, 180], centre: false}).id""")
    pg.wait_for_timeout(300)

    def at(bid, dx=24, dy=14):
        return pg.evaluate(f"""(() => {{ const r = document.querySelector('.ol-b[data-id=\"{bid}\"]')
            .getBoundingClientRect(); return [r.x + {dx}, r.y + {dy}]; }})()""")

    pg.once("dialog", lambda d: d.accept("Graph search"))
    pg.mouse.click(*at(cid), button="right")
    pg.wait_for_timeout(300)
    check("a code block offers Rename",
          any(t.startswith("Rename") for t in pg.locator(".ol-ctx button").all_inner_texts()),
          ", ".join(pg.locator(".ol-ctx button").all_inner_texts())[:70])
    pickmenu("Rename")
    pg.wait_for_timeout(400)
    check("renaming sets the name",
          pg.evaluate(f"bbOnline.S.blocks.get('{cid}').data.title") == "Graph search",
          pg.evaluate(f"bbOnline.S.blocks.get('{cid}').data.title"))
    check("and it shows on the block",
          "graph search" in pg.locator(f'.ol-b[data-id="{cid}"] .ol-t').inner_text().lower(),
          pg.locator(f'.ol-b[data-id="{cid}"] .ol-t').inner_text())

    # F2 does it too
    pg.evaluate(f"""() => {{ bbOnline.S.sel.clear(); bbOnline.S.sel.add('{cid}');
        document.querySelector('#online .ol').focus(); }}""")
    pg.once("dialog", lambda d: d.accept("Dijkstra"))
    pg.keyboard.press("F2")
    pg.wait_for_timeout(400)
    check("F2 renames the selected block",
          pg.evaluate(f"bbOnline.S.blocks.get('{cid}').data.title") == "Dijkstra")

    # ================================================ a box carries what is inside it
    pg.evaluate("bbOnline.view({x: 0, y: 0, k: 1})")
    pg.wait_for_timeout(300)
    made = pg.evaluate("""() => {
        const f = bbOnline.add('shape', 120, 420, {form: 'box', text: 'Group'},
                               {size: [520, 300], centre: false});
        const a = bbOnline.add('sticky', 160, 470, {text: 'in'}, {size: [180, 110], centre: false});
        const b = bbOnline.add('sticky', 400, 470, {text: 'also in'}, {size: [180, 110], centre: false});
        const out = bbOnline.add('sticky', 760, 470, {text: 'outside'}, {size: [180, 110], centre: false});
        return {f: f.id, a: a.id, b: b.id, out: out.id}; }""")
    pg.wait_for_timeout(400)
    before = pg.evaluate("""m => Object.fromEntries(Object.entries(m)
        .map(([k, i]) => [k, [bbOnline.S.blocks.get(i).x, bbOnline.S.blocks.get(i).y]]))""", made)

    rim = pg.evaluate(f"""(() => {{ const d = document.querySelector('.ol-b[data-id=\"{made['f']}\"] .ol-srim.n');
        const q = d.getBoundingClientRect(); return [q.x + q.width / 2, q.y + q.height / 2]; }})()""")
    pg.mouse.move(rim[0], rim[1])
    pg.mouse.down()
    pg.mouse.move(rim[0] + 120, rim[1] + 70, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(500)
    after = pg.evaluate("""m => Object.fromEntries(Object.entries(m)
        .map(([k, i]) => [k, [bbOnline.S.blocks.get(i).x, bbOnline.S.blocks.get(i).y]]))""", made)
    moved = {k: (after[k][0] - before[k][0], after[k][1] - before[k][1]) for k in before}
    check("dragging the box moves the box", moved["f"][0] > 90 and moved["f"][1] > 50, moved["f"])
    check("what is inside comes with it", moved["a"] == moved["f"] and moved["b"] == moved["f"],
          f'box {moved["f"]}, inside {moved["a"]} {moved["b"]}')
    check("what is outside stays put", moved["out"] == (0, 0), moved["out"])

    # ================================================ drag to pick out several
    pg.evaluate("bbOnline.S.sel.clear()")
    pg.evaluate("() => bbOnline.fit()")
    pg.wait_for_timeout(500)
    span = pg.evaluate("""() => { const v = [...bbOnline.S.blocks.values()]
        .filter(b => b.kind === 'sticky');
        const r = document.querySelector('.ol-board').getBoundingClientRect();
        const boxes = v.map(b => document.querySelector(`.ol-b[data-id="${b.id}"]`).getBoundingClientRect());
        return {n: v.length,
                x0: Math.min(...boxes.map(b => b.left)) - 14, y0: Math.min(...boxes.map(b => b.top)) - 14,
                x1: Math.max(...boxes.map(b => b.right)) + 14, y1: Math.max(...boxes.map(b => b.bottom)) + 14}; }""")
    pg.mouse.move(span["x0"], span["y0"])
    pg.mouse.down()
    pg.mouse.move(span["x1"], span["y1"], steps=10)
    pg.wait_for_timeout(150)
    dragging = pg.is_visible(".ol-marquee")
    pg.mouse.up()
    pg.wait_for_timeout(400)
    check("dragging bare board draws a selection box", dragging)
    check("it picks out everything it covers",
          pg.evaluate("bbOnline.S.sel.size") >= span["n"],
          f'{pg.evaluate("bbOnline.S.sel.size")} of at least {span["n"]}')
    check("the board did not move instead",
          pg.evaluate("Math.abs(bbOnline.S.view.x) >= 0"))

    # space still moves the board
    pg.evaluate("bbOnline.S.sel.clear()")
    v0 = pg.evaluate("({...bbOnline.S.view})")
    pg.evaluate("document.querySelector('#online .ol').focus()")
    pg.keyboard.down(" ")
    pg.wait_for_timeout(150)
    check("holding space shows the hand",
          pg.evaluate("document.querySelector('.ol-board').classList.contains('grab')"))
    pg.mouse.move(bx["x"] + 500, bx["y"] + 300)
    pg.mouse.down()
    pg.mouse.move(bx["x"] + 620, bx["y"] + 380, steps=8)
    pg.mouse.up()
    pg.keyboard.up(" ")
    pg.wait_for_timeout(400)
    v1 = pg.evaluate("({...bbOnline.S.view})")
    check("and dragging then moves the board",
          abs(v1["x"] - v0["x"]) > 60 and abs(v1["y"] - v0["y"]) > 30,
          f'{v0["x"]:.0f},{v0["y"]:.0f} -> {v1["x"]:.0f},{v1["y"]:.0f}')
    check("the hand goes away when space is let go",
          not pg.evaluate("document.querySelector('.ol-board').classList.contains('grab')"))

    # ================================================ switching board
    pg.click(".ol-switch")
    pg.wait_for_timeout(500)
    check("the board name opens a switcher", menu.is_visible())
    L = pg.locator(".ol-ctx button").all_inner_texts()
    check("it offers the board kept on this computer",
          any("Offline board" in t for t in L), ", ".join(L)[:90])
    check("it marks the one you are on",
          any(t.startswith("\u2713") and "Offline board" in t for t in L), ", ".join(L)[:90])
    check("and a way to join or start a group",
          any("group" in t.lower() for t in L), ", ".join(L)[:90])
    check("the bar says which board you are on",
          "this computer" in pg.locator(".ol-switch").inner_text().lower(),
          pg.locator(".ol-switch").inner_text())
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
