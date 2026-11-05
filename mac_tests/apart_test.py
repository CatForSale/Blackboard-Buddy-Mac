import os
"""Groups on the board never end up half on top of each other."""
import functools, http.server, threading, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
OLD = Path(__file__).resolve().parent
sys.path.insert(0, str(OLD))
OUT = Path(__file__).resolve().parent.parent / "output"
(OUT / "_atest.html").write_text((OUT / "dashboard.html").read_text(encoding="utf-8"), encoding="utf-8")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB = """sessionStorage.introSeen='1';sessionStorage.bootSound='1';
window.__files={main:[]};
const A={ol_local_load:async b=>JSON.parse(JSON.stringify(__files[b||'main']||[])), ol_local_save:async (r,b)=>{__files[b||'main']=JSON.parse(JSON.stringify(r));return 'ok'},
 ol_local_saved_at:async()=>Date.now()+99999, ol_boards:async()=>[{id:'main',name:'Offline board'}]};
window.pywebview={api:new Proxy(A,{get:(t,k)=>k in t?t[k]:(k==='online_config'?undefined:async()=>({}))})};"""
bad = 0


def check(name, ok, extra=""):
    global bad
    print(("PASS " if ok else "FAIL ") + name, extra); bad += not ok


OVER = """(() => { const bx = [...bbOnline.S.blocks.values()].filter(b => b.kind === 'shape');
  const holds = (a, b) => b.x >= a.x && b.y >= a.y && b.x + b.w <= a.x + a.w && b.y + b.h <= a.y + a.h;
  let n = 0; for (const a of bx) for (const b of bx) if (a !== b && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h && !holds(a, b) && !holds(b, a)) n++;
  return n / 2 })()"""
with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge")); pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e) + " @ " + (getattr(e, "stack", "") or "")[:400]))
    pg.add_init_script(STUB)
    pg.goto(f"http://127.0.0.1:{srv.server_port}/_atest.html"); pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]'); pg.wait_for_timeout(1200)
    pg.evaluate("bbOnline.useLocal(); 0"); pg.wait_for_timeout(800)
    ids = pg.evaluate("""(() => { const O = bbOnline, A = O.add('shape', 100, 100, {text: 'Group A'}, {size: [440, 340], centre: false}),
      B = O.add('shape', 800, 100, {text: 'Group B'}, {size: [400, 300], centre: false});
      O.add('note', 130, 150, {text: 'in A'}, {centre: false}); const n = O.add('note', 830, 150, {text: 'in B'}, {centre: false});
      O.S.view.x = 0; O.S.view.y = 0; O.S.view.k = 1; O.fit && 0; return [A.id, B.id, n.id] })()""")
    pg.evaluate("document.activeElement && document.activeElement.blur()"); pg.keyboard.press("Escape"); pg.wait_for_timeout(500)
    check("two groups, apart", pg.evaluate(OVER) == 0)

    def drag_by(i, dx, dy):
        r = pg.evaluate(f"(() => {{ const r = document.querySelector('.ol-b[data-id=\"{i}\"]').getBoundingClientRect(); return [r.x, r.y, r.width] }})()")
        x, y = r[0] + r[2] / 2, r[1]
        print("  at", x, y, pg.evaluate(f"(e => e && (e.className + ' ' + (e.closest('.ol-b')||{{}}).dataset?.id + ' ' + bbOnline.S.editing + ' ' + bbOnline.S.tool))(document.elementFromPoint({x},{y}))"))
        pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x + dx / 2, y + dy / 2, steps=5); pg.mouse.move(x + dx, y + dy, steps=5); pg.mouse.up()
        pg.wait_for_timeout(500)

    pos = lambda i: pg.evaluate(f"(b => [b.x, b.y, b.w, b.h])(bbOnline.S.blocks.get('{ids[i]}'))")
    npos = lambda: pg.evaluate(f"(b => [b.x, b.y])(bbOnline.S.blocks.get('{ids[2]}'))")
    note0, a0, b0 = npos(), pos(0), pos(1)
    drag_by(ids[1], -400, 40)   # B's left part lands over A's right side, its middle stays outside A
    b1 = pos(1)
    check("B was dragged", b1 != b0, f"{b0} -> {b1}")
    check("after the drop no two groups overlap", pg.evaluate(OVER) == 0, f"A={pos(0)} B={b1}")
    check("B gave way, A stayed put", pos(0) == a0 and b1[0] >= a0[0] + a0[2], f"A={pos(0)} B={b1}")
    note1 = npos()
    check("what is in B went with it", note1[0] - note0[0] == b1[0] - b0[0] and note1[1] - note0[1] == b1[1] - b0[1], f"{note0}->{note1}")
    pg.screenshot(path=str(Path(__file__).with_name("apart.png")))
    pg.keyboard.press("Control+z"); pg.wait_for_timeout(400)
    check("one undo puts B back where it started", pos(1) == b0 and npos() == note0, f"{pos(1)} vs {b0}")
    drag_by(ids[1], -640, 30)   # B's middle lands inside A: B goes in A, A grows round it
    a2, b2 = pos(0), pos(1)
    check("dropped right in, B sits inside A", b2[0] >= a2[0] and b2[1] >= a2[1] and b2[0] + b2[2] <= a2[0] + a2[2] and b2[1] + b2[3] <= a2[1] + a2[3], f"A={a2} B={b2}")
    check("...and still no overlap", pg.evaluate(OVER) == 0)
    pg.screenshot(path=str(Path(__file__).with_name("apart_nested.png")))
    n = lambda: pg.evaluate("[...bbOnline.S.blocks.values()].filter(b => b.kind === 'arrow').length")
    notes = pg.evaluate("[...bbOnline.S.blocks.values()].filter(b => b.kind === 'note').map(b => b.id)")
    pg.evaluate(f"bbOnline.join('{notes[0]}', '{notes[1]}')")
    check("a line joins the two notes", n() == 1)
    pg.evaluate(f"bbOnline.join('{notes[0]}', '{notes[1]}'); bbOnline.join('{notes[1]}', '{notes[0]}')")
    check("a second line between them, either way round, is refused", n() == 1, str(n()))
    b.close()
(OUT / "_atest.html").unlink()
errs = [e for e in errs if "sessionStorage" not in e and "notes.filter" not in e]
check("no page errors " + str(errs[:3]), not errs)
print("FAILED" if bad else "ALL OK", bad)
