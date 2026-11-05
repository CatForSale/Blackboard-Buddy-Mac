import os
"""Names in several writing systems: the intro and the big heading both come out in dots."""
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

src = Path(sys.argv[1]).resolve()
out = Path(__file__).parent
html = src.read_text(encoding="utf-8")
h1 = re.search(r"<header[^>]*>.*?<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
NAMES = {"ja": "Hi みん", "ko": "Hi 민칸", "zh": "Hi 明康", "ru": "Hi Мин", "ar": "Hi مين", "th": "Hi มิน", "hi": "Hi मिन", "en": "Hi Min"}
STUB = "window.pywebview={api:new Proxy({}, {get:(_, k) => k === 'state' ? (async () => ({})) : (async () => null)})};"
bad, errs = 0, []


def check(name, ok):
    global bad
    print(("PASS " if ok else "FAIL ") + name)
    bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge"))
    for lang, name in NAMES.items():
        f = src.parent / f"lang-{lang}.html"
        f.write_text(html.replace(f">{h1}</h1>", f">{name}</h1>", 1), encoding="utf-8")
        for w in (1180, 400):
            pg = b.new_page(viewport={"width": w, "height": 820}, color_scheme="dark")
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.add_init_script(STUB)
            pg.goto(f.as_uri()); pg.wait_for_timeout(2600)
            if w == 1180:
                lit = pg.evaluate("""(() => { const c = document.querySelector('.intro canvas'); if (!c) return 0;
                  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0;
                  for (let i = 3; i < d.length; i += 4) if (d[i] > 200) n++; return n })()""")
                check(f"{lang}: intro in dots ({lit})", lit > 2000)
                pg.screenshot(path=str(out / f"lang-intro-{lang}.png"))
            pg.evaluate("sessionStorage.introSeen='1'"); pg.reload(); pg.wait_for_timeout(2200)
            dotted = pg.evaluate("document.querySelector('header h1').classList.contains('dotted')")
            cw = pg.evaluate("(document.querySelector('.h1-dots')||{}).clientWidth || 0")
            check(f"{lang} @{w}: heading {'in dots' if lang != 'en' else 'in Doto'} ({cw}px)", dotted == (lang != "en") and (lang == "en" or 40 < cw <= w - 16))
            check(f"{lang} @{w}: page fits", pg.evaluate(f"document.documentElement.scrollWidth <= {w}"))
            if w == 1180:
                pg.locator("header").screenshot(path=str(out / f"lang-head-{lang}.png"))
            pg.close()
        f.unlink()
    b.close()
errs = [e for e in errs if "sessionStorage" not in e and "reading 'filter'" not in e]
check("no page errors " + str(errs[:3]), not errs)
print("FAILED" if bad else "ALL OK", bad)
