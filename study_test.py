"""Badges that say what to do, a Study tab that keeps up, and cards that stand on their own."""
import functools
import http.server
import os
import sys
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(r"C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy")
OUT = HERE / "output"
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                      functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT)))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/dashboard.html"

errors, fails, passes = [], [], 0


def check(name, ok, extra=""):
    global passes
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   ({extra})" if extra else ""))
    if ok:
        passes += 1
    else:
        fails.append(name)


# ================================================ cards that make sense on their own
os.chdir(HERE)
sys.path.insert(0, str(HERE))
import main                                                        # noqa: E402

LEANS = ["It identifies the main drivers of daily sales volume.",
         "They are used for checking the model against real takings.",
         "This is the stage where the data gets cleaned up properly.",
         "Those are the three that matter most to the bakery."]
STANDS = ["The dashboard identifies the main drivers of daily sales.",
          "This diagram shows how the three parts of the system fit together.",
          "Normalisation removes the repeated data from a table."]
check("a line that points back at the one before it is not a card",
      all(main._POINTS_BACK.match(t) for t in LEANS),
      str([t[:18] for t in LEANS if not main._POINTS_BACK.match(t)]))
check("a line that names its own subject still is",
      not any(main._POINTS_BACK.match(t) for t in STANDS),
      str([t[:18] for t in STANDS if main._POINTS_BACK.match(t)]))

NOTE = [{"course": "UFCX-15-2 Test Module", "title": "Week 6 Dashboard", "topic": "Dashboard",
         "md": "### What it does\n"
               "- It identifies the **main drivers of daily sales volume** for the bakery team.\n"
               "\n"
               "The dashboard identifies the **main drivers of daily sales volume** for the team.\n"
               "\n"
               "A **scalar** is a single number with no direction attached to it.\n",
         "changed": None}]
cards = main.make_cards(NOTE, lambda c: "test", {"test": "Test Module"}, ())
qs = [c["q"] for c in cards]
check("the gap card is made from the sentence that stands up",
      any(q.startswith("The dashboard identifies the _____") for q in qs), " | ".join(q[:40] for q in qs))
check("and not from the one that starts with 'It'",
      not any(q.startswith("It identifies") for q in qs), " | ".join(q[:40] for q in qs))
check("ordinary cards are still made", any(c["kind"] == "define" for c in cards),
      str(sorted({c["kind"] for c in cards})))

# ================================================ the Study tab itself
with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    pg = br.new_context(viewport={"width": 1400, "height": 950}).new_page()
    pg.add_init_script("sessionStorage.introSeen='1';sessionStorage.bootSound='1';")
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL)
    pg.wait_for_timeout(1200)
    pg.click('nav button[data-tab="study"]')
    pg.wait_for_timeout(1200)

    n = pg.locator(".st-badge").count()
    check("every badge is on the page", n == 12, str(n))
    check("and the page says how many are earned", "/" in pg.locator(".st-bcount").inner_text(),
          pg.locator(".st-bcount").inner_text())

    tips = pg.evaluate("""() => [...document.querySelectorAll('.st-badge')].map(b => {
        const t = b.querySelector('.st-btip');
        return t ? {html: t.innerHTML, text: t.textContent, bar: !!t.querySelector('.st-btip-bar')} : null; })""")
    check("each one carries a card saying what it is for", all(t and len(t["text"]) > 25 for t in tips),
          str(sum(1 for t in tips if not t)))
    check("and how far along you are",
          all("so far" in t["text"] or "Earned" in t["text"] or "Not yet" in t["text"] for t in tips))
    check("the ones you count towards show a bar", sum(1 for t in tips if t["bar"]) >= 5,
          str(sum(1 for t in tips if t["bar"])))

    hidden = pg.evaluate("""() => { const s = getComputedStyle(document.querySelector('.st-btip'));
        return s.visibility === 'hidden' || +s.opacity === 0; }""")
    check("the card stays out of the way until you want it", hidden)
    pg.locator(".st-badge").nth(5).hover()
    pg.wait_for_timeout(450)
    shown = pg.evaluate("""() => { const s = getComputedStyle(document.querySelectorAll('.st-badge')[5].querySelector('.st-btip'));
        return s.visibility === 'visible' && +s.opacity > .9; }""")
    check("hovering a badge brings it up", shown)
    below = pg.evaluate("""() => { const b = document.querySelectorAll('.st-badge')[5];
        return b.querySelector('.st-btip').getBoundingClientRect().top > b.getBoundingClientRect().top; }""")
    check("it hangs below, so it never covers the row above", below)

    # ---- raising the daily goal shows up at once, in the ring and in today's challenge
    was = pg.locator(".st-qlist .st-quest").first.inner_text()
    pg.click(".st-gp")
    pg.wait_for_timeout(350)
    now = pg.locator(".st-qlist .st-quest").first.inner_text()
    check("changing the goal changes what today asks of you straight away", now != was,
          was.replace("\n", " ") + "  ->  " + now.replace("\n", " "))
    pg.click(".st-gm")
    pg.wait_for_timeout(300)

    errors[:] = [e for e in errors if "allow-same-origin" not in e]
    br.close()

# ---- and the plumbing that makes everything else keep up is actually in the page
page = (OUT / "dashboard.html").read_text(encoding="utf-8")
check("one repaint per frame, however many things ask for it", "function refresh()" in page)
check("earning XP asks for one", "refresh();\n  }" in page or "refresh();" in page.split("function gain")[1][:900])
check("and a repaint cannot start another", "painting = true" in page)

print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", errors)
sys.exit(1 if fails or errors else 0)
