"""Round 34: boxes stay behind what they hold when pasted, copying between boards, and the
output block joined to a code block with a line."""
import functools
import http.server
import json
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
# the board file and the board list come from the app; here the page keeps boards in its own
# storage instead, which is the path it already falls back to, and two boards are on offer
STUB = ("sessionStorage.introSeen='1';sessionStorage.bootSound='1';"
        "window.pywebview={api:new Proxy({}, {get:(t,k) => "
        "k==='ol_local_load'||k==='ol_local_save'||k==='online_config' ? undefined "
        ": k==='ol_boards' ? (async () => [{id:'main',name:'Offline board'},"
        "{id:'bbtest2',name:'Second board'}]) : async () => ({})})};")
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
    pg.click(".ol-card .alt")
    pg.wait_for_timeout(400)

    def wipe():
        pg.evaluate("""() => { const o = bbOnline;
            [...o.S.blocks.keys()].forEach(id => o.S.blocks.delete(id));
            document.querySelectorAll('.ol-b').forEach(d => d.remove());
            o.S.nodes.clear(); o.S.sel.clear(); o.drawLinks(); }""")
        pg.wait_for_timeout(150)

    def rows():
        return pg.evaluate("""() => [...bbOnline.S.blocks.values()].map(b =>
            ({id: b.id, kind: b.kind, z: b.z, x: b.x, y: b.y, w: b.w, h: b.h, data: b.data}))""")

    # ====================================================== a pasted box stays behind its contents
    print("\n-- a box and what is in it")
    wipe()
    made = pg.evaluate("""() => { const o = bbOnline;
        const s = o.add('shape', 400, 300, {text: 'Week 1'}, {size: [420, 320], centre: false});
        o.toBack([s.id]);
        const n = o.add('note', 470, 380, {html: 'inside me'}, {size: [200, 120], centre: false});
        const t = o.add('todo', 470, 520, {items: [{t: 'revise', done: false}]}, {size: [200, 120], centre: false});
        return {shape: s.id, note: n.id, todo: t.id}; }""")
    pg.wait_for_timeout(200)
    start = {r["id"]: r for r in rows()}
    check("a box drawn on the board sits behind what is put in it",
          start[made["shape"]]["z"] < start[made["note"]]["z"],
          f'{start[made["shape"]]["z"]} vs {start[made["note"]]["z"]}')

    # copying the box alone takes its contents with it
    pg.evaluate(f"() => bbOnline.copySel(['{made['shape']}'])")
    pg.wait_for_timeout(250)
    clip = pg.evaluate("() => bbOnline.Clip.rows().map(r => r.kind).sort()")
    check("copying a box copies what is inside it too", clip == ["note", "shape", "todo"], clip)

    n_before = len(rows())
    pg.evaluate("() => bbOnline.pasteRows(bbOnline.Clip.rows(), {x: 1500, y: 300})")
    pg.wait_for_timeout(400)
    after = rows()
    fresh = [r for r in after if r["id"] not in start]
    check("pasting it makes a second copy of all three", len(fresh) == 3,
          f"{n_before} -> {len(after)}")
    pbox = next((r for r in fresh if r["kind"] == "shape"), None)
    others = [r for r in fresh if r["kind"] != "shape"]
    check("the pasted box is behind everything that came with it",
          bool(pbox) and bool(others) and all(pbox["z"] < o["z"] for o in others),
          f'box {pbox and pbox["z"]} vs {[o["z"] for o in others]}')
    check("and the copies went in front of what was already there",
          bool(pbox) and min(r["z"] for r in fresh) > max(start[i]["z"] for i in start),
          f'{[r["z"] for r in fresh]} vs {[start[i]["z"] for i in start]}')
    check("the pasted box really holds the pasted blocks",
          pg.evaluate(f"""() => {{ const o = bbOnline, s = o.S.blocks.get('{pbox["id"]}');
            const ids = {json.dumps([o["id"] for o in others])};
            const have = [...o.S.blocks.values()].filter(b => b.kind === 'shape' && b.id === s.id)
              .flatMap(() => ids.map(i => {{ const b = o.S.blocks.get(i);
                return b.x + b.w / 2 > s.x && b.x + b.w / 2 < s.x + s.w
                    && b.y + b.h / 2 > s.y && b.y + b.h / 2 < s.y + s.h; }}));
            return have.every(Boolean); }}"""))
    check("nothing on the board kept an id it already had",
          len({r["id"] for r in after}) == len(after))

    # picking a box up must not bury what is in it either
    pg.evaluate(f"() => {{ bbOnline.select(['{made['shape']}']); bbOnline.toFront(['{made['shape']}']); }}")
    pg.wait_for_timeout(200)
    nowz = {r["id"]: r["z"] for r in rows()}
    check("bringing a box to the front brings its contents and stays under them",
          nowz[made["shape"]] < nowz[made["note"]] and nowz[made["shape"]] < nowz[made["todo"]],
          f'{nowz[made["shape"]]} vs {nowz[made["note"]]}, {nowz[made["todo"]]}')
    check("and it is really in front of the rest of the board now",
          nowz[made["note"]] > max(v for k, v in nowz.items() if k not in made.values()))

    # a real drag on the box, through the mouse
    pg.evaluate("() => bbOnline.view({x: 40, y: 30, k: 0.5})")
    pg.wait_for_timeout(250)
    spot = pg.evaluate(f"""() => {{ const o = bbOnline, s = o.S.blocks.get('{made['shape']}'), v = o.S.view;
        const r = document.querySelector('.ol-board').getBoundingClientRect();
        return {{x: r.left + (s.x + 12) * v.k + v.x, y: r.top + (s.y + 10) * v.k + v.y}}; }}""")
    pg.mouse.move(spot["x"], spot["y"])
    pg.mouse.down()
    pg.mouse.move(spot["x"] + 40, spot["y"] + 25, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(400)
    dz = {r["id"]: r["z"] for r in rows()}
    check("dragging a box about leaves it behind what it holds",
          dz[made["shape"]] < dz[made["note"]] and dz[made["shape"]] < dz[made["todo"]],
          f'{dz[made["shape"]]} vs {dz[made["note"]]}, {dz[made["todo"]]}')
    pg.evaluate("() => bbOnline.view({x: 80, y: 60, k: 1})")

    # ====================================================== copying between boards
    print("\n-- copying from one board to another")
    wipe()
    pair = pg.evaluate("""() => { const o = bbOnline;
        const a = o.add('note', 300, 300, {html: 'the first one'}, {size: [200, 120], centre: false});
        const b = o.add('note', 700, 300, {html: 'the second one'}, {size: [200, 120], centre: false});
        const c = o.add('note', 1200, 300, {html: 'not copied'}, {size: [200, 120], centre: false});
        const joined = o.add('arrow', 0, 0, {from: a.id, to: b.id, label: 'leads to'}, {size: [0, 0], centre: false});
        const dangling = o.add('arrow', 0, 0, {from: b.id, to: c.id}, {size: [0, 0], centre: false});
        return {a: a.id, b: b.id, c: c.id, joined: joined.id, dangling: dangling.id}; }""")
    pg.wait_for_timeout(250)
    pg.evaluate(f"() => bbOnline.copySel(['{pair['a']}', '{pair['b']}'])")
    pg.wait_for_timeout(250)
    held = pg.evaluate("() => bbOnline.Clip.rows().map(r => r.kind).sort()")
    check("copying two joined blocks takes the line between them", held == ["arrow", "note", "note"], held)
    check("the copy is kept in the page's own storage, not just a variable",
          pg.evaluate("() => { const c = JSON.parse(localStorage.getItem('olClip')); return c.rows.length; }") == 3)

    # now move to a different board: the whole board state is rebuilt
    pg.evaluate("""() => bbOnline.useLocal(true, 'bbtest2')""")
    pg.wait_for_timeout(900)
    check("switching board leaves an empty board", len(rows()) == 0, len(rows()))
    check("but the copy is still there after the switch", pg.evaluate("() => bbOnline.Clip.count()") == 2)

    n = pg.evaluate("() => bbOnline.pasteRows(bbOnline.Clip.rows(), {x: 500, y: 400})")
    pg.wait_for_timeout(400)
    there = rows()
    check("pasting on the other board brings the two blocks over", n == 2 and len([r for r in there if r["kind"] == "note"]) == 2,
          f'{n}, {[r["kind"] for r in there]}')
    check("the line came with them", len([r for r in there if r["kind"] == "arrow"]) == 1)
    check("the line with only one end copied was left out", len(there) == 3, len(there))
    arr = next((r for r in there if r["kind"] == "arrow"), None)
    notes = {r["id"] for r in there if r["kind"] == "note"}
    check("and it points at the new blocks, not the old ones",
          bool(arr) and arr["data"]["from"] in notes and arr["data"]["to"] in notes,
          arr and [arr["data"]["from"][:6], arr["data"]["to"][:6]])
    check("the line kept how it looked", bool(arr) and arr["data"].get("label") == "leads to")
    check("none of the old ids came across",
          all(r["id"] not in (pair["a"], pair["b"], pair["joined"]) for r in there))
    check("nothing that belongs to a board it came from was carried over",
          all(not any(k in r["data"] for k in ("_rev",)) for r in there)
          and pg.evaluate("() => [...bbOnline.S.blocks.values()].every(b => !b._remote && !b.group_id)"))

    # and back again
    pg.evaluate("() => bbOnline.useLocal(true, 'main')")
    pg.wait_for_timeout(900)
    check("going back to the first board shows what was on it", len(rows()) == 5, len(rows()))
    pg.evaluate("() => bbOnline.pasteRows(bbOnline.Clip.rows(), {x: 300, y: 900})")
    pg.wait_for_timeout(350)
    check("the same copy pastes onto the board it came from too", len(rows()) == 8, len(rows()))

    # the right-click menu says what is waiting
    pg.evaluate("() => bbOnline.useLocal(true, 'bbtest2')")
    pg.wait_for_timeout(800)
    wipe()
    pg.mouse.click(700, 500, button="right")
    pg.wait_for_timeout(350)
    labels = pg.evaluate("() => [...document.querySelectorAll('.ol-ctx button')].map(b => b.textContent)")
    check("the board menu offers the copy that is waiting",
          any("Paste 2 copied blocks here" in t for t in labels),
          [t for t in labels if "Paste" in t])
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)

    # ====================================================== the output block itself
    print("\n-- the output block")
    wipe()
    check("it has a tool of its own in the rail", pg.is_visible('.ol-tools button[data-tool="output"]'))
    pg.keyboard.press("y")
    pg.wait_for_timeout(200)
    check("Y picks the tool", pg.evaluate("() => bbOnline.S.tool") == "output")
    pg.mouse.click(600, 420)
    pg.wait_for_timeout(400)
    outs = [r for r in rows() if r["kind"] == "output"]
    check("clicking the board makes one", len(outs) == 1, [r["kind"] for r in rows()])
    oid = outs[0]["id"] if outs else ""
    check("it says what to do with it when nothing feeds it",
          "Draw a line from a code block" in pg.inner_text(f'.ol-b[data-id="{oid}"] .ol-outn'))
    check("it can be made bigger and smaller like the rest",
          pg.evaluate(f"""() => document.querySelectorAll('.ol-b[data-id="{oid}"] .ol-rs').length""") == 4)
    check("and it has the usual Run, Open, Copy and Clear",
          pg.evaluate(f"""() => [...document.querySelectorAll('.ol-b[data-id="{oid}"] .ol-h button')]
              .map(b => b.dataset.act).sort().join(',')""") == "obig,oclear,ocopy")

    # what it holds
    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{oid}');
        b.data = {{...b.data, title: 'Marks', text: 'average 71.5\\nhighest 94'}}; o.put(b); }}""")
    pg.wait_for_timeout(300)
    check("plain words show as they are",
          "average 71.5" in pg.inner_text(f'.ol-b[data-id="{oid}"] .ol-outb'))
    check("the far-out name is the first line of what it holds",
          pg.inner_text(f'.ol-b[data-id="{oid}"] .ol-tag').strip() == "Marks")
    pg.locator(".ol-hunt input").fill("highest 94")
    pg.wait_for_timeout(600)
    check("the search looks inside it", pg.locator(".ol-huntn").inner_text() == "1/1",
          pg.locator(".ol-huntn").inner_text())
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)

    DOT = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m"
           "P8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    pg.evaluate(f"""([id, src]) => {{ const o = bbOnline, b = o.S.blocks.get(id);
        b.data = {{...b.data, text: '', imgs: [src]}}; o.put(b); }}""", [oid, DOT])
    pg.wait_for_timeout(300)
    check("a picture shows as a picture",
          pg.evaluate(f"""() => document.querySelectorAll('.ol-b[data-id="{oid}"] .ol-outb img').length""") == 1)
    check("and it works out which it is on its own",
          pg.evaluate(f"() => bbOnline.outShow(bbOnline.S.blocks.get('{oid}'))") == "image")

    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{oid}');
        b.data = {{...b.data, imgs: [], html: '<h1>a page</h1>'}}; o.put(b); }}""")
    pg.wait_for_timeout(400)
    check("a web page shows as a page you can really use",
          pg.evaluate(f"""() => {{ const f = document.querySelector('.ol-b[data-id="{oid}"] .ol-outb iframe');
            return !!f && (f.srcdoc || '').includes('a page'); }}"""))
    check("the page is boxed in so it cannot touch the board",
          "allow-scripts" in pg.get_attribute(f'.ol-b[data-id="{oid}"] .ol-outb iframe', "sandbox"))

    # opened on its own
    pg.click(f'.ol-b[data-id="{oid}"] .ol-h button[data-act="obig"]')
    pg.wait_for_timeout(400)
    check("Open shows the result on its own, filling the board", pg.is_visible(".ol-big"))
    check("with the block's name on it",
          pg.evaluate("() => document.querySelector('.ol-bigh b').textContent") == "Marks")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    check("Escape puts it away again", pg.is_hidden(".ol-big"))

    # its own menu
    pg.evaluate(f"() => bbOnline.select(['{oid}'])")
    pg.click(f'.ol-b[data-id="{oid}"] .ol-h', button="right")
    pg.wait_for_timeout(350)
    labels = pg.evaluate("() => [...document.querySelectorAll('.ol-ctx button')].map(b => b.textContent)")
    want = ["Run the code again", "Open it on its own", "Copy what is in it", "Clear it",
            "Show it as", "How tall", "Wrap long lines", "Keep what came before", "Say who ran it"]
    missing = [w for w in want if not any(w in t for t in labels)]
    check("the right-click menu has everything it needs", not missing, missing)
    check("and says it is an output", pg.evaluate("() => document.querySelector('.ol-ctx small').textContent") == "output")

    # how tall, and wrap
    pg.evaluate("""() => { const items = [...document.querySelectorAll('.ol-ctx button')];
        items.find(b => b.textContent.includes('How tall')).click(); }""")
    pg.wait_for_timeout(300)
    pg.evaluate("""() => { const items = [...document.querySelectorAll('.ol-ctx button')];
        items.find(b => b.textContent.trim() === 'Tall').click(); }""")
    pg.wait_for_timeout(300)
    check("how tall really changes how tall it is",
          pg.evaluate(f"() => bbOnline.S.blocks.get('{oid}').h") == 460,
          pg.evaluate(f"() => bbOnline.S.blocks.get('{oid}').h"))

    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{oid}');
        b.data = {{...b.data, html: '', text: 'a very long line of output that would run off the end'}}; o.put(b); }}""")
    pg.wait_for_timeout(250)
    check("long lines wrap to begin with",
          pg.evaluate(f"""() => getComputedStyle(document.querySelector('.ol-b[data-id="{oid}"] .ol-outb pre')).whiteSpace""") == "pre-wrap")
    pg.evaluate(f"""() => {{ const o = bbOnline, b = o.S.blocks.get('{oid}');
        b.data = {{...b.data, wrap: false}}; o.put(b); }}""")
    pg.wait_for_timeout(250)
    check("and can be told not to",
          pg.evaluate(f"""() => getComputedStyle(document.querySelector('.ol-b[data-id="{oid}"] .ol-outb pre')).whiteSpace""") == "pre")

    # clear
    pg.click(f'.ol-b[data-id="{oid}"] .ol-h button[data-act="oclear"]')
    pg.wait_for_timeout(300)
    check("Clear empties it", pg.evaluate(f"() => bbOnline.S.blocks.get('{oid}').data.text") == "")

    check("it has a colour of its own on the little map",
          pg.evaluate("() => { const c = document.createElement('canvas'); return true; }")
          and pg.evaluate(f"() => bbOnline.S.blocks.get('{oid}').kind") == "output")
    pg.click(".ol-mapb")
    pg.wait_for_timeout(500)
    check("and is drawn on the map",
          pg.evaluate("""() => { const c = document.querySelector('.ol-map canvas');
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }""") > 50)
    pg.keyboard.press("m")
    pg.wait_for_timeout(300)

    # saving and loading
    kept = pg.evaluate("""() => { const rows = [...bbOnline.S.blocks.values()];
        return JSON.parse(JSON.stringify(rows)); }""")
    pg.evaluate("""([rows]) => { const o = bbOnline;
        [...o.S.blocks.keys()].forEach(id => o.S.blocks.delete(id));
        document.querySelectorAll('.ol-b').forEach(d => d.remove());
        o.S.nodes.clear(); o.S.sel.clear();
        rows.forEach(r => { o.S.blocks.set(r.id, r); o.paint(r); }); }""", [kept])
    pg.wait_for_timeout(400)
    check("an output block comes back after a save and a load",
          pg.evaluate("() => [...bbOnline.S.blocks.values()].filter(b => b.kind === 'output').length") == 1
          and pg.is_visible(f'.ol-b[data-id="{oid}"] .ol-outb'))

    # ====================================================== joined to a code block with a line
    print("\n-- a line from code to output")
    wipe()
    two = pg.evaluate("""() => { const o = bbOnline;
        const c = o.add('code', 300, 300, {lang: 'javascript', text: "console.log('seventy one')"},
                        {size: [420, 200], centre: false});
        const a = o.add('output', 900, 300, {title: 'First'}, {size: [360, 200], centre: false});
        const b = o.add('output', 900, 560, {title: 'Second'}, {size: [360, 200], centre: false});
        return {code: c.id, a: a.id, b: b.id}; }""")
    pg.wait_for_timeout(250)
    check("nothing is joined to begin with",
          pg.evaluate(f"() => bbOnline.outputsOf('{two['code']}').length") == 0)

    # draw the line the way a person would: the line tool, dragged from the code to the output
    pg.keyboard.press("a")
    pg.wait_for_timeout(250)
    ends = pg.evaluate(f"""() => {{ const at = i => {{
          const r = document.querySelector('.ol-b[data-id="' + i + '"]').getBoundingClientRect();
          return {{x: r.left + r.width / 2, y: r.bottom - 12}}; }};
        return {{from: at('{two['code']}'), to: at('{two['a']}')}}; }}""")
    pg.mouse.move(ends["from"]["x"], ends["from"]["y"])
    pg.mouse.down()
    pg.mouse.move(ends["to"]["x"], ends["to"]["y"], steps=10)
    pg.mouse.up()
    pg.wait_for_timeout(500)
    check("drawing a line from the code to the output joins them",
          pg.evaluate(f"() => bbOnline.outputsOf('{two['code']}').length") == 1)
    check("and the output knows which code feeds it",
          pg.evaluate(f"() => bbOnline.feederOf('{two['a']}')") == two["code"])
    check("so it grows a Run button of its own",
          pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{two['a']}"] .ol-h button[data-act="orun"]')"""))

    pg.evaluate(f"""() => {{ const o = bbOnline;
        o.add('arrow', 0, 0, {{from: '{two['code']}', to: '{two['b']}'}}, {{size: [0, 0], centre: false}}); }}""")
    pg.wait_for_timeout(300)
    check("several outputs can hang off one code block",
          pg.evaluate(f"() => bbOnline.outputsOf('{two['code']}').length") == 2)

    # run it for real
    pg.evaluate(f"() => bbOnline.boardRun(bbOnline.S.blocks.get('{two['code']}'))")
    pg.wait_for_timeout(3500)
    got = pg.evaluate(f"""() => ({{a: (bbOnline.S.blocks.get('{two['a']}').data.text || ''),
                                  b: (bbOnline.S.blocks.get('{two['b']}').data.text || ''),
                                  ok: bbOnline.S.blocks.get('{two['a']}').data.ok}})""")
    check("running the code puts its result in the output block", "seventy one" in got["a"], got["a"][:60])
    check("and in every other output hanging off it", "seventy one" in got["b"], got["b"][:60])
    check("it says the run went well", got["ok"] is True)
    check("the result is on screen, not only in the data",
          "seventy one" in pg.inner_text(f'.ol-b[data-id="{two["a"]}"] .ol-outb'))
    check("and it says who ran it",
          pg.is_visible(f'.ol-b[data-id="{two["a"]}"] .ol-outby'))

    # Run on the output block runs the code behind it
    pg.evaluate(f"""() => {{ const o = bbOnline, c = o.S.blocks.get('{two['code']}');
        c.data = {{...c.data, text: "console.log('ran from the output')"}}; o.put(c);
        const a = o.S.blocks.get('{two['a']}'); a.data = {{...a.data, text: ''}}; o.put(a); }}""")
    pg.wait_for_timeout(300)
    pg.click(f'.ol-b[data-id="{two["a"]}"] .ol-h button[data-act="orun"]')
    pg.wait_for_timeout(3000)
    check("the output's own Run runs the code that feeds it",
          "ran from the output" in pg.evaluate(f"() => bbOnline.S.blocks.get('{two['a']}').data.text"),
          pg.evaluate(f"() => bbOnline.S.blocks.get('{two['a']}').data.text")[:60])

    # an error is shown as one
    pg.evaluate(f"""() => {{ const o = bbOnline, c = o.S.blocks.get('{two['code']}');
        c.data = {{...c.data, text: "nope()"}}; o.put(c); }}""")
    pg.wait_for_timeout(200)
    pg.evaluate(f"() => bbOnline.boardRun(bbOnline.S.blocks.get('{two['code']}'))")
    pg.wait_for_timeout(3000)
    check("an error turns the output red and says so",
          pg.evaluate(f"() => bbOnline.S.blocks.get('{two['a']}').data.ok") is False
          and pg.evaluate(f"""() => document.querySelector('.ol-b[data-id="{two['a']}"] .ol-outb').classList.contains('bad')"""))

    # taking the line away unhooks them
    arrows = pg.evaluate("() => [...bbOnline.S.blocks.values()].filter(b => b.kind === 'arrow').map(b => b.id)")
    pg.evaluate("([ids]) => bbOnline.remove(ids)", [arrows])
    pg.wait_for_timeout(400)
    check("taking the lines away unhooks them",
          pg.evaluate(f"() => bbOnline.outputsOf('{two['code']}').length") == 0
          and pg.evaluate(f"() => bbOnline.feederOf('{two['a']}')") is None)
    check("and the output says so again",
          pg.evaluate(f"""() => !document.querySelector('.ol-b[data-id="{two['a']}"] .ol-h button[data-act="orun"]')"""))

    # a page goes to the output instead of opening the editor
    wipe()
    web = pg.evaluate("""() => { const o = bbOnline;
        const c = o.add('proj', 300, 300, {title: 'Site', files: [{n: 'index.html', t: '<h1>hello board</h1>'}], main: 'index.html'},
                        {size: [440, 240], centre: false});
        const a = o.add('output', 900, 300, {title: 'The page'}, {size: [380, 240], centre: false});
        o.add('arrow', 0, 0, {from: c.id, to: a.id}, {size: [0, 0], centre: false});
        return {code: c.id, out: a.id}; }""")
    pg.wait_for_timeout(300)
    pg.evaluate(f"() => bbOnline.boardRun(bbOnline.S.blocks.get('{web['code']}'))")
    pg.wait_for_timeout(900)
    check("a project that makes a page sends the page to the output",
          "hello board" in (pg.evaluate(f"() => bbOnline.S.blocks.get('{web['out']}').data.html") or ""))
    check("without opening the code editor over the board",
          pg.evaluate("() => !bbOnline.S.editing"))
    check("and the output shows it as a page",
          pg.evaluate(f"""() => !!document.querySelector('.ol-b[data-id="{web['out']}"] .ol-outb iframe')"""))

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
