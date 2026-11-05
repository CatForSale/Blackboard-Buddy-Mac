import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
STUB = """window.pywebview={api:new Proxy({}, {get:(_, k) => k === 'state' ? (async () => ({})) : (async () => null)})};
try{sessionStorage.introSeen='1';localStorage.removeItem('bbIde')}catch(_){}"""
bad = 0


def check(name, ok):
    global bad
    print(("PASS " if ok else "FAIL ") + name)
    bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge"))
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e) + " @ " + (getattr(e, "stack", "") or "")[:400]))
    pg.add_init_script(STUB)
    pg.goto(Path(sys.argv[1]).resolve().as_uri()); pg.wait_for_timeout(1500)
    fs = lambda: pg.evaluate("getComputedStyle(document.querySelector('.ol-ide')).getPropertyValue('--ide-fs').trim()")
    out = lambda: pg.evaluate("getComputedStyle(document.querySelector('.ol-ide .ide-out')).fontSize")
    check("starts at 14px " + fs(), fs() == "14px" and pg.locator(".ide-zv").inner_text() == "14px")
    pg.evaluate("document.querySelector('.ide-zp').click(); document.querySelector('.ide-zp').click()")
    check("A+ twice -> 16px, output follows " + out(), fs() == "16px" and out() == "14.5px")
    check("remembered", '"size":16' in pg.evaluate("localStorage.bbIde"))
    pg.evaluate("document.querySelector('.ide-zm').click()")
    check("A- -> 15px", fs() == "15px")
    pg.evaluate("document.querySelector('.ide-zv').click()")
    check("click the number -> back to 14px", fs() == "14px")
    b.close()
errs = [e for e in errs if "sessionStorage" not in e and "reading 'filter'" not in e]
check("no page errors " + str(errs), not errs)
print("FAILED" if bad else "ALL OK", bad)
