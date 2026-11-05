import os
"""One output block fed by two code blocks keeps both results, each under its own name."""
import functools, http.server, threading
from pathlib import Path
from playwright.sync_api import sync_playwright
OUT = Path(__file__).resolve().parent.parent / "output"
(OUT / "_mtest.html").write_text((OUT / "dashboard.html").read_text(encoding="utf-8"), encoding="utf-8")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB = """sessionStorage.introSeen='1';sessionStorage.bootSound='1';window.__files={main:[]};
const A={ol_local_load:async b=>JSON.parse(JSON.stringify(__files[b||'main']||[])), ol_local_save:async (r,b)=>{__files[b||'main']=JSON.parse(JSON.stringify(r));return 'ok'},
 ol_local_saved_at:async()=>Date.now()+99999, ol_boards:async()=>[{id:'main',name:'Offline board'}]};
window.pywebview={api:new Proxy(A,{get:(t,k)=>k in t?t[k]:(k==='online_config'?undefined:async()=>({}))})};"""
bad = 0


def check(name, ok, extra=""):
    global bad
    print(("PASS " if ok else "FAIL ") + name, extra); bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge")); pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e) + " @ " + (getattr(e, "stack", "") or "")[:400]))
    pg.add_init_script(STUB)
    pg.goto(f"http://127.0.0.1:{srv.server_port}/_mtest.html"); pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]'); pg.wait_for_timeout(1200)
    pg.evaluate("bbOnline.useLocal(); 0"); pg.wait_for_timeout(800)
    ids = pg.evaluate("""(() => { const O = bbOnline,
      c1 = O.add('code', 100, 100, {lang: 'javascript', title: 'First', text: 'console.log("one")'}, {centre: false}),
      c2 = O.add('code', 100, 500, {lang: 'javascript', title: 'Second', text: 'console.log("two")'}, {centre: false}),
      o = O.add('output', 700, 300, {}, {centre: false});
      O.join(c1.id, o.id); O.join(c2.id, o.id); return [c1.id, c2.id, o.id] })()""")
    check("both code blocks join the one output", pg.evaluate(f"bbOnline.feedersOf('{ids[2]}').length") == 2)
    pg.evaluate(f"""(() => {{ const O = bbOnline;
      O.outStart('{ids[0]}'); O.outText('{ids[0]}', 'one\\n'); O.outDone('{ids[0]}', {{text: 'one\\n', ok: true}});
      O.outStart('{ids[1]}'); O.outText('{ids[1]}', 'two\\n'); O.outDone('{ids[1]}', {{text: 'two\\n', ok: true}}); }})()""")
    txt = pg.evaluate(f"bbOnline.S.blocks.get('{ids[2]}').data.text")
    check("the second run did not wipe out the first", "one" in txt and "two" in txt, repr(txt))
    check("each under its own name, top one first", txt.index("First") < txt.index("one") < txt.index("Second") < txt.index("two"), repr(txt))
    pg.evaluate(f"""(() => {{ const O = bbOnline; O.outStart('{ids[0]}'); O.outText('{ids[0]}', 'uno\\n'); O.outDone('{ids[0]}', {{text: 'uno\\n', ok: true}}); }})()""")
    txt = pg.evaluate(f"bbOnline.S.blocks.get('{ids[2]}').data.text")
    check("running one again only replaces its own part", "uno" in txt and "one" not in txt and "two" in txt, repr(txt))
    btn = pg.evaluate(f"(document.querySelector('.ol-b[data-id=\"{ids[2]}\"] [data-act=orun]') || {{}}).title")
    check("its Run button says it runs both", btn and "all 2" in btn, btn)
    pg.wait_for_timeout(300)
    pg.locator(f'.ol-b[data-id="{ids[2]}"]').screenshot(path=str(Path(__file__).with_name("multiout.png")))
    b.close()
(OUT / "_mtest.html").unlink()
errs = [e for e in errs if "sessionStorage" not in e and "notes.filter" not in e]
check("no page errors " + str(errs[:3]), not errs)
print("FAILED" if bad else "ALL OK", bad)
