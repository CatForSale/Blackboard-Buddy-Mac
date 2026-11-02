import base64
import difflib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import vault as notebook  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

out = Path(os.environ["LOCALAPPDATA"]) / "BlackboardBuddy" / "nbshots"
out.mkdir(exist_ok=True)
for f in out.glob("w*.png"):
    f.unlink()
vault = Path(tempfile.mkdtemp(prefix="nbvault-"))
notebook.ROOT = vault
notebook.init()
html = (Path("output") / "dashboard.html").read_text(encoding="utf-8")
html = html.replace(json.dumps(Path("Notebook").resolve().as_uri())[1:-1], vault.resolve().as_uri())
page = vault / "dash.html"
page.write_text(html, encoding="utf-8")
STUB = """window.pywebview={api:new Proxy({}, {get:(_, k) => {
  const map = {nb_list:'all_notes', nb_save:'save', nb_delete:'delete', nb_image:'save_image', nb_render:'render'};
  if (map[k]) return (...a) => window._nb(map[k], a);
  if (k === 'state') return async () => ({});
  return async () => null;
}})};"""
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFklEQVR4nGP8z8DwnwEJMDGgAQYGBgB/7wQBtvbWWQAAAABJRU5ErkJggg==")
(vault / "pic.png").write_bytes(png)
errs, results = [], []
import atexit; atexit.register(lambda: print("PAGE ERRORS:", errs))
def check(name, ok, extra=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra and not ok else ""))
read = lambda n: (vault / f"{n}.md").read_text(encoding="utf-8")

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    pg = b.new_page(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.expose_function("_nb", lambda fn, a: getattr(notebook, fn)(*a))
    pg.add_init_script("sessionStorage.introSeen='1';localStorage.tab='notebook';" + STUB)
    pg.goto(page.as_uri()); pg.wait_for_timeout(1800)
    check("header hidden", pg.locator("header").is_hidden())
    box = pg.locator(".nb").bounding_box()
    check("notebook fills window", box["width"] > 1380 and box["height"] > 700, box)
    pg.screenshot(path=str(out / "w1-start.png"))

    # round trip: re-saving the guide must not change it
    before = read("Start here")
    pg.click(".nb-ed"); pg.keyboard.press("Control+End"); pg.keyboard.type("x"); pg.keyboard.press("Backspace"); pg.wait_for_timeout(1300)
    after = read("Start here")
    check("guide survives a re-save unchanged", before == after, "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)))

    # new blank note, Word-style typing
    pg.click(".nb-new"); pg.click(".nb-menu [data-t=blank]"); pg.wait_for_timeout(500)
    pg.keyboard.type("Week 1"); pg.keyboard.press("Enter"); pg.wait_for_timeout(700)
    check("named new note", (vault / "Week 1.md").exists())
    k = pg.keyboard
    k.press("Control+2"); k.type("Overview"); k.press("Enter")
    k.type("Hello "); k.press("Control+b"); k.type("bold"); k.press("Control+b"); k.type(" and "); k.press("Control+i"); k.type("italic"); k.press("Control+i"); k.type(" text."); k.press("Enter")
    k.type("- "); k.type("first"); k.press("Enter"); k.type("second"); k.press("Enter"); k.press("Enter")
    k.type("[] "); k.type("buy book"); k.press("Enter"); k.press("Enter")
    k.type("1. "); k.type("one"); k.press("Enter"); k.type("two"); k.press("Enter"); k.press("Enter")
    k.type("See [[Lin"); pg.wait_for_timeout(300)
    check("[[ autocomplete shows", pg.locator(".nb-ac").is_visible())
    k.press("Enter"); k.type("and "); k.press("Control+k"); pg.wait_for_timeout(200)
    check("Ctrl+K picker opens", pg.locator(".nb-ac.picker").is_visible())
    k.type("Start"); k.press("Enter"); pg.wait_for_timeout(200)
    k.press("Enter"); k.type("mark me")
    k.press("Shift+Home"); pg.click(".nb-tools [data-a=mark]"); k.press("End"); k.press("Enter")
    k.type("---"); k.press("Enter")
    pg.click(".nb-tools [data-a=web]"); pg.wait_for_timeout(200); k.type("example.com"); k.press("Enter"); pg.wait_for_timeout(200)
    k.press("Enter"); pg.click(".nb-tools [data-a=table]"); pg.wait_for_timeout(200)
    k.type("A"); k.press("Tab"); k.type("B"); k.press("Tab"); k.type("C"); k.press("Tab"); k.type("1")
    check("table tools appear in a table", pg.locator(".nb-tbl").is_visible())
    pg.locator(".nb-ed p").last.click()
    pg.locator(".nb-file").set_input_files(str(vault / "pic.png")); pg.wait_for_timeout(900)
    pg.wait_for_timeout(1200)
    md = read("Week 1")
    print("---- saved markdown ----\n" + md + "------------------------")
    for name, frag in [("heading", "## Overview"), ("bold", "**bold**"), ("italic", "*italic*"), ("bullets", "- first\n- second"),
                       ("checklist", "- [ ] buy book"), ("numbered", "1. one\n2. two"), ("note link typed", "[[Linking notes]]"),
                       ("note link picked", "[[Start here]]"), ("highlight", "==mark me=="), ("divider", "\n---\n"),
                       ("web link", "](https://example.com)"), ("table", "| A | B | C |\n| --- | --- | --- |\n| 1 |"), ("picture", "](attachments/pic-")]:
        check(name, frag in md)
    check("picture visible", pg.evaluate("[...document.querySelectorAll('.nb-ed img')].every(i => i.naturalWidth > 0)"))
    pg.screenshot(path=str(out / "w2-note.png"))

    # reopen: the saved note renders back and re-saves unchanged
    pg.click(".nb-item >> text=Start here"); pg.wait_for_timeout(600); pg.click(".nb-item >> text=Week 1"); pg.wait_for_timeout(800)
    before = read("Week 1")
    pg.click(".nb-ed h2"); k.press("End"); k.type("x"); k.press("Backspace"); pg.wait_for_timeout(1300)
    after = read("Week 1")
    check("own note round-trips unchanged", before == after, "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)))
    check("checkbox rendered", pg.locator(".nb-ed ul.tasks input[type=checkbox]").count() == 1)
    pg.locator(".nb-ed ul.tasks input").first.click(); pg.wait_for_timeout(1200)
    check("tick saves", "- [x] buy book" in read("Week 1"))
    check("outline lists heading", "Overview" in pg.locator(".nb-outline").inner_text())
    # chip click opens the linked note, and it shows the backlink
    pg.locator(".nb-ed a.wl", has_text="Linking notes").click(); pg.wait_for_timeout(700)
    check("chip opens note", pg.input_value(".nb-title") == "Linking notes")
    check("backlink listed", "Week 1" in pg.locator(".nb-back").inner_text())
    # comfort controls
    pg.click(".nb-toggle-side"); pg.click(".nb-toggle-info"); pg.click(".nb-tools [data-a=bigger]"); pg.click(".nb-tools [data-a=bigger]")
    pg.wait_for_timeout(300); pg.screenshot(path=str(out / "w3-focus.png"))
    check("panels hide", pg.locator(".nb-side").is_hidden() and pg.locator(".nb-info").is_hidden())
    pg.click(".nb-toggle-side"); pg.click(".nb-toggle-info"); pg.click(".nb-tools [data-a=smaller]"); pg.click(".nb-tools [data-a=smaller]")
    pg.click(".nb-tools [data-a=raw]"); pg.wait_for_timeout(300)
    check("markdown view", pg.locator(".nb-src").is_visible() and "Start here" in pg.input_value(".nb-src"))
    pg.click(".nb-tools [data-a=raw]"); pg.wait_for_timeout(400)
    # keyboard: toolbar arrow navigation
    pg.focus(".nb-tools [data-c=bold]"); k.press("ArrowRight")
    check("arrow keys move along toolbar", pg.evaluate("document.activeElement.dataset.c") == "italic")
    # Highlight and Code switch on and off with nothing selected, like Bold does
    pg.click(".nb-new"); pg.click(".nb-menu [data-t=blank]"); pg.wait_for_timeout(500)
    pg.keyboard.type("Toggles"); pg.keyboard.press("Enter"); pg.wait_for_timeout(600)
    press = lambda sel: pg.get_attribute(f".nb-tools [{sel}]", "aria-pressed")
    k.type("plain ")
    pg.click(".nb-tools [data-a=mark]"); pg.wait_for_timeout(250)
    check("highlight switches on with no selection", press("data-a=mark") == "true")
    k.type("lit")
    pg.click(".nb-tools [data-a=mark]"); pg.wait_for_timeout(250)
    check("highlight switches off again", press("data-a=mark") == "false")
    k.type(" after")
    pg.click(".nb-tools [data-a=code]"); pg.wait_for_timeout(250)
    check("code switches on with no selection", press("data-a=code") == "true")
    k.type("c()")
    pg.click(".nb-tools [data-a=code]"); pg.wait_for_timeout(250)
    check("code switches off again", press("data-a=code") == "false")
    k.type(" end"); pg.wait_for_timeout(1400)
    md = read("Toggles")
    check("toggled text saved as markdown", "==lit==" in md and "`c()`" in md, md)
    check("no stray markers in the saved file", "​" not in md, repr(md))

    # every inline button must switch off again when pressed a second time
    pg.click(".nb-ed"); pg.wait_for_timeout(200)
    for label, sel in [("bold", "data-c=bold"), ("italic", "data-c=italic"), ("underline", "data-c=underline"),
                       ("strikethrough", "data-c=strikeThrough"), ("highlight", "data-a=mark"), ("code", "data-a=code")]:
        seen = []
        for _ in range(4):
            pg.click(f".nb-tools [{sel}]"); pg.wait_for_timeout(220)
            seen.append(pg.get_attribute(f".nb-tools [{sel}]", "aria-pressed"))
        check(f"{label} alternates on and off", seen == ["true", "false", "true", "false"], seen)
    pg.evaluate("document.querySelector('.nb-ed').innerHTML='<p>plain</p>'")
    pg.click(".nb-ed"); k.press("End")
    pg.click(".nb-tools [data-c=bold]"); k.type("B")
    pg.click(".nb-tools [data-c=bold]"); k.type("n"); pg.wait_for_timeout(300)
    check("only the middle letter comes out bold", "plain<b>B</b>n" in pg.locator(".nb-ed").inner_html())

    # choosing which buttons sit on the toolbar
    pg.click(".nb-tools [data-a=tools]"); pg.wait_for_timeout(350)
    check("button chooser opens", pg.locator(".nb-tpick").is_visible())
    check("every button is listed", pg.locator(".nb-tpick-i").count() == len(pg.query_selector_all(".nb-tools .grp:not(.nb-tbl) button")) - 1,
          pg.locator(".nb-tpick-i").count())
    pg.locator(".nb-tpick-i", has_text="Underline").locator("input").uncheck(); pg.wait_for_timeout(300)
    check("unticked button leaves the toolbar", pg.locator(".nb-tools [data-c=underline]").is_hidden())
    check("arrow keys skip the hidden one",
          "underline" not in pg.evaluate("[...document.querySelectorAll('.nb-tools button:not([hidden])')].map(b=>b.dataset.c).join(',')"))
    for key in ("undo", "redo"):
        pg.locator(".nb-tpick-i", has_text="Undo" if key == "undo" else "Redo").locator("input").uncheck()
    pg.wait_for_timeout(300)
    check("an empty group disappears too", pg.locator(".nb-tools .grp").first.is_hidden())
    pg.reload(); pg.wait_for_timeout(1600)
    check("the choice survives a restart", pg.locator(".nb-tools [data-c=underline]").is_hidden())
    pg.click(".nb-tools [data-a=tools]"); pg.wait_for_timeout(300)
    pg.click(".nb-tpick-r"); pg.wait_for_timeout(300)
    check("show-them-all brings them back", pg.locator(".nb-tools [data-c=underline]").is_visible()
          and pg.locator(".nb-tools .grp").first.is_visible())
    pg.screenshot(path=str(out / "w7-toolpick.png"))
    pg.click(".nb-ed"); pg.wait_for_timeout(250)
    check("chooser closes when you click away", pg.locator(".nb-tpick").is_hidden())

    pg.evaluate("document.documentElement.dataset.theme='light'"); pg.click(".nb-item >> text=Week 1"); pg.wait_for_timeout(700)
    pg.screenshot(path=str(out / "w4-light.png"))
    pg.set_viewport_size({"width": 1000, "height": 800}); pg.wait_for_timeout(400)
    pg.screenshot(path=str(out / "w5-1000.png"))
    pg.set_viewport_size({"width": 640, "height": 800}); pg.wait_for_timeout(400)
    pg.screenshot(path=str(out / "w6-640.png"))
    b.close()
print("errors:", errs)
print(f"{sum(results)}/{len(results)} passed")
shutil.rmtree(vault, ignore_errors=True)
