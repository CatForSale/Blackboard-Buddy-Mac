import os
"""Can you play what is in an output block? Clicks, keys, and does it survive the board redrawing it."""
import functools, http.server, threading, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
OUT = Path(__file__).resolve().parent.parent / "output"
(OUT / "_ptest.html").write_text((OUT / "dashboard.html").read_text(encoding="utf-8"), encoding="utf-8")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB = """sessionStorage.introSeen='1';sessionStorage.bootSound='1';window.__files={main:[]};
const A={ol_local_load:async b=>JSON.parse(JSON.stringify(__files[b||'main']||[])), ol_local_save:async (r,b)=>{__files[b||'main']=JSON.parse(JSON.stringify(r));return 'ok'},
 ol_local_saved_at:async()=>Date.now()+99999, ol_boards:async()=>[{id:'main',name:'Offline board'}]};
window.pywebview={api:new Proxy(A,{get:(t,k)=>k in t?t[k]:(k==='online_config'?undefined:async()=>({}))})};"""
SHOP = """<!doctype html><html><head><title>Shop</title></head><body><button id=s>buy sword</button><span id=r>-</span>
<script>s.onclick=()=>opener.postMessage({buy:'sword'},'*');addEventListener('message',e=>r.textContent=e.data);</script></body></html>"""
GAME = """<!doctype html><html><body style="margin:0;font:16px sans-serif">
<button id=b>clicks 0</button> <span id=k>keys 0</span> <a id=l href="shop.html" target="_blank">shop</a> <span id=got>none</span>
<button id=w>open window</button> <input id=t placeholder=type>
<canvas id=c width=200 height=80 tabindex=0 style="border:1px solid #888"></canvas>
<script>let n=0,m=0;b.onclick=()=>b.textContent='clicks '+(++n);
addEventListener('keydown',e=>{k.textContent='keys '+(++m)+' '+e.key});
addEventListener('message',e=>{if(e.data&&e.data.buy)got.textContent='bought '+e.data.buy});
w.onclick=()=>{const x=window.open('','pop','width=300,height=200');window.__pop=x;if(x){x.document.write('<h1 id=h>from opener</h1>');x.document.close();}};
</script></body></html>"""
bad = 0


def check(name, ok, extra=""):
    global bad
    print(("PASS " if ok else "FAIL ") + name, extra); bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge")); ctx = b.new_context(viewport={"width": 1400, "height": 900}); pg = ctx.new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pops = []; ctx.on("page", lambda q: pops.append(q))
    pg.add_init_script(STUB)
    pg.goto(f"http://127.0.0.1:{srv.server_port}/_ptest.html"); pg.wait_for_timeout(800)
    pg.click('nav button[data-tab="online"]'); pg.wait_for_timeout(1200)
    pg.evaluate("bbOnline.useLocal(); 0"); pg.wait_for_timeout(800)
    ids = pg.evaluate("""(g => { const O = bbOnline,
      c = O.add('proj', 100, 100, {title: 'Game', files: [{n: 'index.html', t: g[0]}, {n: 'shop.html', t: g[1]}]}, {centre: false}),
      o = O.add('output', 600, 100, {}, {centre: false, size: [560, 380]});
      O.join(c.id, o.id); O.S.view.x = 0; O.S.view.y = 0; O.S.view.k = 1; return [c.id, o.id] })""" , [GAME, SHOP])
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{ids[1]}\"] [data-act=orun]').dispatchEvent(new PointerEvent('pointerdown', {{bubbles: true, button: 0}}))")
    pg.wait_for_timeout(1500)
    fr = pg.frame_locator(f'.ol-b[data-id="{ids[1]}"] iframe')
    try:
        fr.locator("#b").click(timeout=3000); fr.locator("#b").click(timeout=3000)
        check("clicks reach the page", fr.locator("#b").inner_text() == "clicks 2", fr.locator("#b").inner_text())
    except Exception as e:
        check("clicks reach the page", False, str(e)[:200])
    try:
        fr.locator("#c").click(timeout=3000); pg.keyboard.press("ArrowLeft"); pg.keyboard.press("a")
        check("keys reach the page", fr.locator("#k").inner_text().startswith("keys 2"), fr.locator("#k").inner_text())
        fr.locator("#t").click(); pg.keyboard.type("hi")
        check("typing in its box", fr.locator("#t").input_value() == "hi")
    except Exception as e:
        check("keys reach the page", False, str(e)[:200])
    check("board did not grab the keys", pg.evaluate("[...bbOnline.S.blocks.values()].length") == 3)
    pg.evaluate(f"bbOnline.select(['{ids[0]}'])"); pg.wait_for_timeout(500)
    try:
        check("selecting something else keeps the game as it was", fr.locator("#b").inner_text(timeout=2000) == "clicks 2", fr.locator("#b").inner_text())
    except Exception as e:
        check("selecting something else keeps the game as it was", False, str(e)[:120])
    out = f'.ol-b[data-id="{ids[1]}"] iframe'
    pg.evaluate(f"(o => {{ o.x += 40; bbOnline.put(o); }})(bbOnline.S.blocks.get('{ids[1]}'))"); pg.wait_for_timeout(400)
    check("moving the output keeps the game going", fr.locator("#b").inner_text() == "clicks 2", fr.locator("#b").inner_text())
    pg.evaluate(f"bbOnline.paint(bbOnline.S.blocks.get('{ids[1]}'))"); pg.wait_for_timeout(400)
    check("so does the board redrawing it", fr.locator("#b").inner_text() == "clicks 2", fr.locator("#b").inner_text())
    pg.evaluate(f"(o => {{ o.data = {{...o.data, title: 'Arcade'}}; bbOnline.put(o); }})(bbOnline.S.blocks.get('{ids[1]}'))"); pg.wait_for_timeout(400)
    check("and renaming it", fr.locator("#b").inner_text() == "clicks 2", fr.locator("#b").inner_text())
    fr.locator("#w").click(); pg.wait_for_timeout(800)
    check("the page's own window opens as a pop-up in the output", fr.locator("[data-bb-pop]").count() == 1 and not pops, f"{fr.locator('[data-bb-pop]').count()} / outside {len(pops)}")
    check("and the page wrote into it", pg.frame_locator(out).frame_locator("[data-bb-pop] iframe").locator("#h").inner_text(timeout=3000) == "from opener")
    fr.locator("[data-bb-pop] button[title=Close]").click(); pg.wait_for_timeout(300)
    check("x closes it", fr.locator("[data-bb-pop]").count() == 0)
    fr.locator("#l").click(); pg.wait_for_timeout(1000)
    shop = pg.frame_locator(out).frame_locator("[data-bb-pop] iframe")
    check("a new-tab link to another page of the project opens it in a pop-up", shop.locator("#s").count() == 1 and not pops, str(len(pops)))
    check("its title shows on the pop-up", fr.locator("[data-bb-pop] span").first.inner_text() == "Shop", fr.locator("[data-bb-pop] span").first.inner_text())
    shop.locator("#s").click(); pg.wait_for_timeout(400)
    check("the pop-up talks back to the page (opener.postMessage)", fr.locator("#got").inner_text() == "bought sword", fr.locator("#got").inner_text())
    pg.frame_locator(out).locator("body").evaluate("() => { const w = window.open('', 'x2'); w.document.write('<p id=q>hi</p>'); w.document.close(); }")
    pg.wait_for_timeout(500)
    check("a second window can be open at the same time", fr.locator("[data-bb-pop]").count() == 2)
    pg.screenshot(path=str(Path(__file__).with_name("play.png")))
    pg.wait_for_timeout(1100)
    pg.evaluate(f"document.querySelector('.ol-b[data-id=\"{ids[1]}\"] [data-act=orun]').dispatchEvent(new PointerEvent('pointerdown', {{bubbles: true, button: 0}}))")
    pg.wait_for_timeout(1200)
    check("Run again starts it fresh", fr.locator("#b").inner_text() == "clicks 0", fr.locator("#b").inner_text())
    b.close()
(OUT / "_ptest.html").unlink()
errs = [e for e in errs if "sessionStorage" not in e and "notes.filter" not in e]
check("no page errors " + str(errs[:3]), not errs)
print("FAILED" if bad else "ALL OK", bad)
