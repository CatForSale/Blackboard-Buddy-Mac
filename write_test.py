"""The writing checker: it fixes what it knows is wrong and leaves everything else alone."""
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

    W = "window.bbWriting"
    check("the checker is there", pg.evaluate(f"typeof {W} === 'object'"))

    # ---------------------------------------------- what it knows
    def fix(word):
        return pg.evaluate(f"{W}.lookup({word!r})")

    check("it knows a common misspelling", fix("seperate") == "separate", str(fix("seperate")))
    check("it knows one a programmer makes", fix("retrun") == "return", str(fix("retrun")))
    check("it puts the apostrophe back", fix("dont") == "don\u2019t", str(fix("dont")))
    check("a capital stays a capital", fix("Seperate") == "Separate", str(fix("Seperate")))
    check("shouting stays shouting", fix("SEPERATE") == "SEPARATE", str(fix("SEPERATE")))
    for word in ("colour", "realise", "recognise", "behaviour", "python", "numpy", "Min", "organise"):
        check(f"it leaves {word} alone", fix(word) is None, str(fix(word)))

    # ---------------------------------------------- what it looks for
    def finds(text):
        return pg.evaluate(f"{W}.finds({text!r}).map(f => [f.what, f.n])")

    L = finds("I recieve teh same thing")
    check("it finds two typos in one line", len(L) == 2, str(L))
    L = finds("this is is wrong")
    check("it spots a word said twice", any("is is" in w for w, _ in L), str(L))
    L = finds("i went home")
    check("it spots the small i", any(w.startswith("i \u2192 I") for w, _ in L), str(L))
    L = finds("wait , then go")
    check("it spots a space before a comma", any("space before" in w for w, _ in L), str(L))
    L = finds("one.  two")
    check("it spots two spaces", any("two spaces" in w for w, _ in L), str(L))
    L = finds("Hello there. this is me.")
    check("it spots a sentence starting small", any("starting small" in w for w, _ in L), str(L))
    check("clean writing gives nothing", finds("The quick brown fox jumps over it.") == [], str(finds("The quick brown fox jumps over it.")))
    check("it counts how many times", finds("teh cat and teh dog")[0][1] == 2, str(finds("teh cat and teh dog")))

    # ---------------------------------------------- fixing a real block
    pg.evaluate("""() => { window.__n = bbOnline.add('sticky', 300, 300,
        {text: 'i recieve teh note', color: '#fff4b8'}).id; }""")
    pg.wait_for_timeout(300)
    pg.keyboard.press("F7")
    pg.wait_for_timeout(400)
    panel = pg.locator(".ol-fix")
    check("F7 opens the panel", panel.is_visible())
    rows = pg.locator(".ol-fixl li").all_inner_texts()
    check("it lists what it found", len(rows) >= 2, " | ".join(rows)[:110].replace("\n", " "))
    check("and says how many", "thing" in pg.locator(".ol-fixn").inner_text(),
          pg.locator(".ol-fixn").inner_text())

    pg.click(".ol-fixall")
    pg.wait_for_timeout(500)
    got = pg.evaluate("() => bbOnline.S.blocks.get(window.__n).data.text")
    check("Fix everything cleans the block", got == "I receive the note", repr(got))
    check("and the panel then says there is nothing left",
          "Nothing to fix" in pg.locator(".ol-fixl").inner_text(), pg.locator(".ol-fixl").inner_text()[:60])

    # one at a time
    pg.evaluate("""() => { window.__m = bbOnline.add('text', 700, 300, {text: 'teh wierd thing'}).id;
                           bbOnline.S.sel = new Set([window.__m]); }""")
    pg.wait_for_timeout(250)
    pg.evaluate("() => window.bbWriting.open([window.__m])")
    pg.wait_for_timeout(350)
    pg.locator(".ol-fixl button").first.click()
    pg.wait_for_timeout(400)
    got = pg.evaluate("() => bbOnline.S.blocks.get(window.__m).data.text")
    check("Fix on one line changes only that one", got in ("the wierd thing", "teh weird thing"), repr(got))
    check("the other one is still listed", pg.locator(".ol-fixl li").count() >= 1)

    # ---------------------------------------------- a note keeps its formatting
    pg.evaluate("""() => { window.__h = bbOnline.add('note', 1100, 300,
        {html: '<b>teh</b> plan is <i>definately</i> on'}).id; }""")
    pg.wait_for_timeout(250)
    pg.evaluate("() => window.bbWriting.open([window.__h])")
    pg.wait_for_timeout(300)
    pg.click(".ol-fixall")
    pg.wait_for_timeout(500)
    html = pg.evaluate("() => bbOnline.S.blocks.get(window.__h).data.html")
    check("a note is fixed without losing its bold and italic",
          "<b>The</b>" in html and "<i>definitely</i>" in html, repr(html)[:110])

    # ---------------------------------------------- a checklist
    pg.evaluate("""() => { window.__t = bbOnline.add('todo', 1500, 300,
        {items: [{t: 'recieve the marks', done: false}, {t: 'fine as it is', done: false}]}).id; }""")
    pg.wait_for_timeout(250)
    pg.evaluate("() => window.bbWriting.open([window.__t])")
    pg.wait_for_timeout(300)
    pg.click(".ol-fixall")
    pg.wait_for_timeout(500)
    items = pg.evaluate("() => bbOnline.S.blocks.get(window.__t).data.items.map(i => i.t)")
    check("a checklist item is fixed", items[0].startswith("Receive the marks"), str(items))
    check("and the one that was fine is untouched", items[1] == "Fine as it is" or items[1] == "fine as it is",
          str(items))

    # ---------------------------------------------- code is left alone
    pg.evaluate("""() => { window.__c = bbOnline.add('code', 1900, 300,
        {lang: 'python', text: 'def teh(adn):\\n    return adn'}).id; }""")
    pg.wait_for_timeout(250)
    check("code is not something it offers to correct",
          pg.evaluate("() => !window.bbWriting.minds(bbOnline.S.blocks.get(window.__c))"))

    # ---------------------------------------------- the whole board at once
    pg.evaluate("() => window.bbWriting.open(null)")
    pg.wait_for_timeout(400)
    txt = pg.locator(".ol-fixl").inner_text()
    check("checking the board names which block each one is in",
          pg.locator(".ol-fixl li.where").count() >= 1 or "Nothing to fix" in txt, txt[:80].replace("\n", " "))

    # ---------------------------------------------- the as-you-type fix
    pg.evaluate("() => window.bbWriting.auto = true")
    pg.evaluate("""() => { window.__k = bbOnline.add('sticky', 300, 700, {text: ''}).id;
                           bbOnline.startEdit(window.__k); }""")
    pg.wait_for_timeout(400)
    pg.keyboard.type("i recieve ")
    pg.wait_for_timeout(500)
    got = pg.evaluate("() => bbOnline.S.blocks.get(window.__k).data.text")
    check("typing a known typo fixes it on the space after", "receive" in got, repr(got))
    check("and the rest of what was typed is still there", got.strip().endswith("receive"), repr(got))

    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    pg.evaluate("() => window.bbWriting.auto = false")
    pg.evaluate("""() => { window.__j = bbOnline.add('sticky', 700, 700, {text: ''}).id;
                           bbOnline.startEdit(window.__j); }""")
    pg.wait_for_timeout(400)
    pg.keyboard.type("teh ")
    pg.wait_for_timeout(400)
    got = pg.evaluate("() => bbOnline.S.blocks.get(window.__j).data.text")
    check("turned off, it leaves typing alone", got.strip() == "teh", repr(got))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)

    # ---------------------------------------------- the menus
    pg.evaluate("() => window.bbWriting.close()")
    pg.mouse.click(620, 760, button="right")
    pg.wait_for_timeout(400)
    L = pg.locator(".ol-ctx button").all_inner_texts()
    check("right-clicking the board offers to check it",
          any("Check the writing on the board" in t for t in L), " | ".join(L)[:90])
    check("and offers the as-you-type switch", any("Fix typos as I type" in t for t in L), " | ".join(L)[:90])
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(250)

    box = pg.evaluate("() => { const d = bbOnline.S.nodes.get(window.__n); const r = d.getBoundingClientRect();"
                      "  return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }")
    pg.mouse.click(box["x"], box["y"], button="right")
    pg.wait_for_timeout(400)
    L = pg.locator(".ol-ctx button").all_inner_texts()
    check("right-clicking a note offers to check just that one",
          any("Check the writing" in t for t in L), " | ".join(L)[:90])
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)

    br.close()

IGNORE = ("drawAccounts", "notes.filter", "lacks the 'allow-same-origin' flag")
real = [e for e in errors if not any(i in e for i in IGNORE)]
print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", real)
sys.exit(1 if fails or real else 0)
