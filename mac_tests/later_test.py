import os
"""Later on the update prompt must not stop you updating: Check for updates always brings it back."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

dash = Path(sys.argv[1]).resolve().as_uri()
STUB = """window.pywebview={api:new Proxy({}, {get:(_, k) => k === 'state' ? (async () => ({})) :
 k === 'check_update' ? (async () => ({version: '9.9.9', notes: 'Test', url: 'https://github.com/x/y.zip'})) : (async () => null)})};
try{sessionStorage.introSeen='1'}catch(_){}"""
errs, bad = [], 0


def check(name, ok):
    global bad
    print(("PASS " if ok else "FAIL ") + name)
    bad += not ok


with sync_playwright() as p:
    b = (p.webkit.launch() if os.environ.get("BB_ENGINE", "webkit") == "webkit" else p.chromium.launch(channel="msedge"))
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.add_init_script(STUB)
    pg.goto(dash); pg.wait_for_timeout(1500)
    pg.evaluate("bbUpdate({version: '9.9.9', notes: 'Test'})"); pg.wait_for_timeout(300)
    check("prompt shows by itself", pg.locator(".update").is_visible())
    pg.click(".up-later"); pg.wait_for_timeout(300)
    check("Later hides it", pg.locator(".update").is_hidden())
    pg.evaluate("bbUpdate({version: '9.9.9', notes: 'Test'})"); pg.wait_for_timeout(300)
    check("the automatic check stays quiet after Later", pg.locator(".update").is_hidden())
    pg.evaluate("document.querySelector('.s-check').click()"); pg.wait_for_timeout(800)
    check("Check for updates brings it back", pg.locator(".update").is_visible())
    check("Update now is ready to press", pg.locator(".up-go").is_enabled() and pg.locator(".up-go").inner_text().lower() == "update now")
    pg.click(".up-later"); pg.wait_for_timeout(300)
    pg.evaluate("document.querySelector('.s-check').click()"); pg.wait_for_timeout(800)
    check("…and again after a second Later", pg.locator(".update").is_visible())
    b.close()
errs = [e for e in errs if "sessionStorage" not in e and "reading 'filter'" not in e]
check("no page errors " + str(errs), not errs)
print("FAILED" if bad else "ALL OK", bad)
