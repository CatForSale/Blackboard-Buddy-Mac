"""Looking around, drawing a box out at the size you want, and tidying what is in one."""
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

    def bare():
        """A spot on the board with nothing on it."""
        return pg.evaluate("""() => {
            const r = document.querySelector('.ol-board').getBoundingClientRect();
            for (let y = r.top + 120; y < r.bottom - 120; y += 40)
              for (let x = r.left + 120; x < r.right - 120; x += 40) {
                const el = document.elementFromPoint(x, y);
                if (el && !el.closest('.ol-b') && !el.closest('.ol-top') && !el.closest('.ol-tools')
                    && !el.closest('.ol-zoom') && !el.closest('.ol-corner') && !el.closest('.ol-fix')) return {x, y};
              }
            return null; }""")

    def view():
        return pg.evaluate("() => ({...bbOnline.S.view})")

    # ---------------------------------------------- dragging empty space looks around
    pg.evaluate("() => bbOnline.view({x: 80, y: 60, k: 1})")
    pg.wait_for_timeout(200)
    was = view()
    spot = bare()
    pg.mouse.move(spot["x"], spot["y"])
    pg.mouse.down()
    pg.mouse.move(spot["x"] + 150, spot["y"] + 90, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(250)
    now = view()
    check("left-dragging empty space does not move the board",
          abs(now["x"] - was["x"]) < 3 and abs(now["y"] - was["y"]) < 3,
          f'{was["x"]:.0f},{was["y"]:.0f} -> {now["x"]:.0f},{now["y"]:.0f}')
    # right-dragging is what moves it now, and save_test checks that

    # ---------------------------------------------- Shift-drag still picks several out
    pg.evaluate("""() => { bbOnline.view({x: 80, y: 60, k: 1});
        window.__a = bbOnline.add('sticky', 200, 200, {text: 'one'}).id;
        window.__b = bbOnline.add('sticky', 440, 200, {text: 'two'}).id; }""")
    pg.wait_for_timeout(350)
    box = pg.evaluate("""() => { const r = document.querySelector('.ol-board').getBoundingClientRect(),
        v = bbOnline.S.view;
        const at = (x, y) => ({x: r.left + x * v.k + v.x, y: r.top + y * v.k + v.y});
        return {from: at(700, 430), to: at(60, 80)}; }""")   # starts clear of both
    was = view()
    pg.mouse.move(box["from"]["x"], box["from"]["y"])
    pg.mouse.down()
    pg.mouse.move(box["to"]["x"], box["to"]["y"], steps=10)
    pg.mouse.up()
    pg.wait_for_timeout(300)
    check("dragging picks out what it covers", pg.evaluate("() => bbOnline.S.sel.size") >= 2,
          str(pg.evaluate("() => bbOnline.S.sel.size")))
    now = view()
    check("and that drag did not move the board", abs(now["x"] - was["x"]) < 3 and abs(now["y"] - was["y"]) < 3,
          f'{was["x"]:.0f},{was["y"]:.0f} -> {now["x"]:.0f},{now["y"]:.0f}')
    pg.evaluate("() => bbOnline.S.sel.clear()")

    # ---------------------------------------------- drawing a box out
    before = pg.evaluate("() => bbOnline.S.blocks.size")
    pg.click('.ol-tools button[data-tool="shape"]')
    pg.wait_for_timeout(250)
    where = pg.evaluate("""() => { const r = document.querySelector('.ol-board').getBoundingClientRect(),
        v = bbOnline.S.view;
        const at = (x, y) => ({x: r.left + x * v.k + v.x, y: r.top + y * v.k + v.y});
        return {from: at(260, 520), to: at(760, 860)}; }""")
    pg.mouse.move(where["from"]["x"], where["from"]["y"])
    pg.mouse.down()
    pg.mouse.move(where["to"]["x"], where["to"]["y"], steps=10)
    pg.mouse.up()
    pg.wait_for_timeout(500)
    made = pg.evaluate("""() => { const b = [...bbOnline.S.blocks.values()].filter(x => x.kind === 'shape').pop();
        return b ? {x: b.x, y: b.y, w: b.w, h: b.h, id: b.id} : null; }""")
    check("a box appears where it was drawn", made is not None, str(made))
    if made:
        check("at the size it was drawn", abs(made["w"] - 500) < 25 and abs(made["h"] - 340) < 25,
              f'{made["w"]}x{made["h"]} wanted about 500x340')
        check("starting where the drag started", abs(made["x"] - 260) < 25 and abs(made["y"] - 520) < 25,
              f'{made["x"]},{made["y"]}')
        pg.evaluate("() => window.__box = [...bbOnline.S.blocks.values()].filter(x => x.kind === 'shape').pop().id")
    check("and the tool goes back to picking things out",
          pg.evaluate("() => bbOnline.S.tool") == "select", pg.evaluate("() => bbOnline.S.tool"))

    # a quick click still gives one of the usual size
    pg.keyboard.press("Escape")
    pg.evaluate("() => bbOnline.S.tool = 'shape'")
    spot = bare()
    pg.mouse.click(spot["x"], spot["y"])
    pg.wait_for_timeout(450)
    clicked = pg.evaluate("""() => { const b = [...bbOnline.S.blocks.values()].filter(x => x.kind === 'shape').pop();
        return {w: b.w, h: b.h}; }""")
    check("a plain click still gives a box of the usual size",
          clicked["w"] == 400 and clicked["h"] == 280, str(clicked))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    pg.evaluate("""() => { const b = [...bbOnline.S.blocks.values()].filter(x => x.kind === 'shape').pop();
        bbOnline.S.sel = new Set([b.id]); }""")
    pg.keyboard.press("Delete")
    pg.wait_for_timeout(300)

    # ---------------------------------------------- tidying what is in a box
    pg.evaluate("""() => {
        const f = bbOnline.S.blocks.get(window.__box);
        window.__in = ['a', 'b', 'c', 'd'].map((t, i) =>
          bbOnline.add('sticky', f.x + 30 + i * 11, f.y + 60 + i * 23, {text: t},
                       {size: [150, 100], centre: false}).id); }""")
    pg.wait_for_timeout(400)
    pg.evaluate("() => { bbOnline.S.sel = new Set([window.__box]); }")
    at = pg.evaluate("""() => { const d = bbOnline.S.nodes.get(window.__box), r = d.getBoundingClientRect();
        return {x: r.left + 6, y: r.top + 6}; }""")
    pg.mouse.click(at["x"], at["y"], button="right")
    pg.wait_for_timeout(400)
    L = pg.locator(".ol-ctx button").all_inner_texts()
    check("a box offers to tidy what is in it", any("Tidy what is in here" in t for t in L), " | ".join(L)[:100])
    check("and to pick out what is in it", any("Pick out what is in here" in t for t in L), " | ".join(L)[:100])
    pg.locator(".ol-ctx button", has_text="Tidy what is in here").first.click()
    pg.wait_for_timeout(600)

    laid = pg.evaluate("""() => window.__in.map(i => { const b = bbOnline.S.blocks.get(i);
        return {x: b.x, y: b.y, w: b.w, h: b.h}; })""")
    frame = pg.evaluate("() => { const b = bbOnline.S.blocks.get(window.__box); return {x: b.x, y: b.y, w: b.w, h: b.h}; }")
    check("everything in it is still in it",
          all(b["x"] >= frame["x"] and b["y"] >= frame["y"]
              and b["x"] + b["w"] <= frame["x"] + frame["w"] + 1
              and b["y"] + b["h"] <= frame["y"] + frame["h"] + 1 for b in laid), str(laid))
    rows = sorted({b["y"] for b in laid})
    check("they line up in rows", len(rows) < len(laid), f"{len(laid)} things on {len(rows)} rows")
    first = [b for b in laid if b["y"] == rows[0]]
    check("and sit level with each other across a row", len({b["y"] for b in first}) == 1, str(first))
    check("the first one clears the box's own name", laid[0]["y"] >= frame["y"] + 40,
          f'{laid[0]["y"]} vs box top {frame["y"]}')
    check("none of them overlap", all(
        not (a["x"] < b["x"] + b["w"] and a["x"] + a["w"] > b["x"]
             and a["y"] < b["y"] + b["h"] and a["y"] + a["h"] > b["y"])
        for i, a in enumerate(laid) for b in laid[i + 1:]), str(laid))

    pg.keyboard.press("Control+z")
    pg.wait_for_timeout(400)
    check("undo puts them back where they were",
          pg.evaluate("() => bbOnline.S.blocks.get(window.__in[3]).y") != laid[3]["y"])

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
