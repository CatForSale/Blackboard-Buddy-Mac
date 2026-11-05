import os
"""The intro spells the name in dots, Japanese included, and Japanese text elsewhere uses the pixel font."""
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

src = Path(sys.argv[1]).resolve()
out = Path(__file__).parent
html = src.read_text(encoding="utf-8")
h1 = re.search(r"<header[^>]*>.*?<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
print("header says:", h1)
pages = {}
for tag, name in (("jp", None), ("latin", "Hi Min"), ("mixed", "Hi みん 123")):
    f = src.parent / f"intro-{tag}.html"
    f.write_text(html if name is None else html.replace(f">{h1}</h1>", f">{name}</h1>", 1), encoding="utf-8")
    pages[tag] = f
STUB = "window.pywebview={api:new Proxy({}, {get:(_, k) => k === 'state' ? (async () => ({})) : (async () => null)})};"
errs, bad = [], 0


def check(name, ok):
    global bad
    print(("PASS " if ok else "FAIL ") + name)
    bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge"))
    for tag, f in pages.items():
        pg = b.new_page(viewport={"width": 1180, "height": 820}, color_scheme="dark")
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script(STUB)
        pg.goto(f.as_uri()); pg.wait_for_timeout(3200)
        size = pg.evaluate("(() => { const c = document.querySelector('.intro canvas'); return c ? [c.clientWidth, c.clientHeight] : null })()")
        lit = pg.evaluate("""(() => { const c = document.querySelector('.intro canvas'); if (!c) return 0;
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0;
          for (let i = 3; i < d.length; i += 4) if (d[i] > 200) n++; return n })()""")
        pg.screenshot(path=str(out / f"intro-{tag}.png"))
        check(f"{tag}: the name is drawn in dots ({size}, {lit} lit pixels)", bool(size) and size[0] > 40 and lit > 2000)
        if tag == "jp":
            pg.wait_for_timeout(5000)
            font = pg.evaluate("getComputedStyle(document.querySelector('header h1')).fontFamily")
            used = pg.evaluate("document.fonts.check('60px \"DotGothic16\"', 'みん')")
            check(f"header font falls back to the pixel font ({font})", "DotGothic16" in font and used)
            pg.locator("header").screenshot(path=str(out / "header-jp.png"))
        pg.close()
    b.close()
for f in pages.values():
    f.unlink()
errs = [e for e in errs if "sessionStorage" not in e and "reading 'filter'" not in e]
check("no page errors " + str(errs), not errs)
print("FAILED" if bad else "ALL OK", bad)
