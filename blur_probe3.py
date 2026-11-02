"""Does a fractional pan offset explain the softness? And does snapping it to whole device
pixels get the sharpness back?"""
import functools
import http.server
import io
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


def sharpness(png):
    im = Image.open(io.BytesIO(png)).convert("L")
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
    for dpr in (1, 1.5):
        pg = br.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=dpr).new_page()
        pg.add_init_script(STUB)
        pg.add_init_script(NOSERVER)
        pg.goto(URL)
        pg.wait_for_timeout(700)
        pg.click('nav button[data-tab="online"]')
        pg.wait_for_timeout(400)
        pg.click(".ol-card .alt")
        pg.wait_for_timeout(250)
        bid = pg.evaluate("""() => bbOnline.add('text', 300, 240,
            {text: 'Shortest path', size: 15}, {size: [300, 120], centre: false}).id""")

        print(f"--- device scale {dpr}")
        for tag, x, y, k in (("whole pixels    ", -500, -400, 2.5),
                             ("half a pixel off", -499.5, -400.5, 2.5),
                             ("a third off     ", -499.67, -400.33, 2.5),
                             ("whole, 2.0x     ", -500, -400, 2.0),
                             ("half off, 2.0x  ", -499.5, -400.5, 2.0)):
            pg.evaluate("v => bbOnline.view(v)", {"x": x, "y": y, "k": k})
            pg.wait_for_timeout(450)
            box = pg.locator(f'.ol-b[data-id="{bid}"] .ol-c').bounding_box()
            s = sharpness(pg.screenshot(clip={"x": box["x"] + 3, "y": box["y"] + 3,
                                              "width": 240, "height": 46}))
            print(f"   {tag}  {s:6.2f}")
        pg.close()
    br.close()
