"""The Notebook's saving, its file menu, and taking notes in and out as Word, PDF, Markdown and text.

Round 36. Three things are checked here:
  1. nothing typed is lost - the copy in browser storage, the file a moment later, and the older
     copies kept on the computer;
  2. the formats, both ways, at the Python level and through the menu;
  3. the File menu itself, the keys people already know, and find and replace.
"""
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())
import vault as notebook  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

vault = Path(tempfile.mkdtemp(prefix="nbfile-"))
notebook.ROOT = vault
notebook.init()
shots = Path(os.environ["LOCALAPPDATA"]) / "BlackboardBuddy" / "nbfileshots"
shots.mkdir(parents=True, exist_ok=True)
for old in shots.glob("*.png"):
    old.unlink()

html = (Path("output") / "dashboard.html").read_text(encoding="utf-8")
html = html.replace(json.dumps(Path("Notebook").resolve().as_uri())[1:-1], vault.resolve().as_uri())
page_file = vault / "dash.html"
page_file.write_text(html, encoding="utf-8")

STUB = """window.pywebview={api:new Proxy({}, {get:(_, k) => {
  const map = {nb_list:'all_notes', nb_save:'save', nb_delete:'delete', nb_image:'save_image', nb_render:'render',
               nb_saved_at:'saved_at', nb_backup:'backup', nb_backups:'backups', nb_backup_read:'backup_read'};
  if (map[k]) return (...a) => window._nb(map[k], a);
  if (k === 'nb_formats') return () => window._formats();
  if (k === 'nb_export') return (...a) => window._export(a);
  if (k === 'nb_import') return () => window._import();
  if (k === 'state') return async () => ({});
  return async () => null;
}})};"""

errs, fails, passes = [], [], 0


def check(name, ok, extra=""):
    global passes
    print(("PASS " if ok else "FAIL ") + name + (f"   [{extra}]" if extra and not ok else ""))
    if ok:
        passes += 1
    else:
        fails.append(name)


read = lambda n: (vault / f"{n}.md").read_text(encoding="utf-8")

# =============================================================== 1. the formats, on their own
SAMPLE = """# Revision plan

Some **bold**, some *italic*, `inline code`, ~~gone~~, ==marked== and <u>underlined</u>.
A [web link](https://example.com) and a [[Linking notes]] note link.

## Steps

- read the brief
- [ ] write the draft
- [x] hand it in
    - with the appendix

1. one
2. two

> Keep going.
> It is nearly done.

```python
def total(rows):
    return sum(r < 10 and r or 0 for r in rows)
```

| Module | Mark |
| --- | --- |
| UFCFxx | 72 |
| UFCFyy | 61 |

---

A line with a snake_case_word and 2 * 3 = 6 in it.
"""

blocks = notebook.outline(SAMPLE)
kinds = [b["k"] for b in blocks]
check("a note is read as the blocks it is made of",
      kinds == ["h", "p", "h", "list", "list", "quote", "code", "table", "hr", "p"], str(kinds))
check("a lone star is a times sign, not the start of italics",
      "2 * 3 = 6" in "".join(r["t"] for r in blocks[-1]["runs"]),
      "".join(r["t"] for r in blocks[-1]["runs"]))
check("a snake_case_word keeps its underscores",
      "snake_case_word" in "".join(r["t"] for r in blocks[-1]["runs"]))
check("tick boxes are read as ticked and unticked",
      [i["box"] for i in blocks[3]["items"]] == [None, False, True, None],
      str([i["box"] for i in blocks[3]["items"]]))

txt = notebook.to_text(SAMPLE)
check("plain text keeps every word", "read the brief" in txt and "It is nearly done." in txt)
check("plain text drops the markers", "**" not in txt and "[[" not in txt, txt[:120])
check("plain text lays a table out in columns", "Module  Mark" in txt, txt[txt.find("Module"):][:40])

docx_bytes = notebook.to_docx("Revision plan", SAMPLE)
(vault / "trip.docx").write_bytes(docx_bytes)
check("a Word document is written", docx_bytes[:2] == b"PK" and len(docx_bytes) > 10000, str(len(docx_bytes)))
back = notebook.from_docx(vault / "trip.docx")["text"]
for bit, label in [("# Revision plan", "its headings"), ("**bold**", "bold"), ("*italic*", "italic"),
                   ("`inline code`", "code"), ("~~gone~~", "strikethrough"), ("==marked==", "highlighting"),
                   ("<u>underlined</u>", "underlining"), ("[web link](https://example.com)", "web links"),
                   ("- read the brief", "bullets"), ("- [ ] write the draft", "tick boxes"),
                   ("1. one", "numbered lists"), ("> Keep going.", "quotes"),
                   ("```", "code blocks"), ("| Module | Mark |", "tables")]:
    check(f"a note that goes out to Word and comes back keeps {label}", bit in back, back[:400])
check("and the code block keeps its line breaks", "def total(rows):\n    return sum" in back,
      back[back.find("```"):][:120])

pdf_bytes = notebook.to_pdf("Revision plan", SAMPLE)
check("a PDF is written", pdf_bytes[:5] == b"%PDF-" and pdf_bytes.rstrip().endswith(b"%%EOF"), str(len(pdf_bytes)))
(vault / "trip.pdf").write_bytes(pdf_bytes)
pdf_text = notebook.from_pdf(vault / "trip.pdf")["text"]
check("and every word in the note is really on the page",
      all(w in pdf_text for w in ["Revision plan", "read the brief", "Keep going", "UFCFyy", "def total"]),
      pdf_text[:300])

long_note = "# Long\n\n" + "\n\n".join(f"## Part {i}\n\n" + "words " * 90 for i in range(1, 10))
import pypdf  # noqa: E402
import io  # noqa: E402
many = pypdf.PdfReader(io.BytesIO(notebook.to_pdf("Long", long_note)))
check("a long note runs on to more pages", len(many.pages) >= 2, str(len(many.pages)))
on_page = []
for pg in many.pages:
    body = pg.get_contents().get_data().decode("latin-1")
    on_page += [(float(a), float(b)) for a, b in
                __import__("re").findall(r"([\d.]+) ([\d.]+) Td", body)]
check("and nothing is written off the edge of the paper",
      all(0 <= x <= 596 and 0 <= y <= 842 for x, y in on_page), str([p for p in on_page if not (0 <= p[1] <= 842)][:3]))

RTF = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Calibri;}}\n"
       "\\fs32\\b Heading one\\b0\\fs22\\par\n"
       "Plain with \\b bold\\b0 , \\i italic\\i0  and \\ul under\\ulnone  text.\\par\n"
       "{\\pntext\\f0 \\'b7\\tab}\\ilvl0 A bullet item\\par\n"
       "Caf\\'e9 and \\u8212 ? a dash.\\par\n}")
(vault / "in.rtf").write_text(RTF, encoding="cp1252")
rtf = notebook.from_rtf(vault / "in.rtf")
check("rich text comes in with its headings and its bold",
      "# Heading one" in rtf["text"] and "**bold**" in rtf["text"], rtf["text"])
check("a bullet in rich text stays a bullet", "- A bullet item" in rtf["text"], rtf["text"])
check("and with letters that are not plain English", "Café" in rtf["text"] and "—" in rtf["text"], rtf["text"])
check("and it says what rich text could not carry", "tables" in " ".join(rtf["lost"]), str(rtf["lost"]))

(vault / "in.txt").write_bytes(b"hello\r\nthere\r\n")
check("a plain text file comes in with Windows line endings tidied",
      notebook.from_file(vault / "in.txt")["text"] == "hello\nthere\n")
check("a file the notebook can't read says so in plain words",
      "can't be brought in" in (notebook.from_file(vault / "in.zzz").get("error") or ""),
      str(notebook.from_file(vault / "in.zzz")))
check("every format that can be written says what it loses",
      all(f["loses"] and f["ext"] and f["name"] for f in notebook.EXPORTS))
check("and so does every format that can be brought in",
      all(f["loses"] and f["ext"] and f["name"] for f in notebook.IMPORTS))

# =============================================================== 2. the copies kept on the computer
notebook.save("Keeping", "first\n")
notebook.backup("Keeping", "first\n")
time.sleep(1.1)
notebook.backup("Keeping", "second\n", every=0)
kept = notebook.backups("Keeping")
check("a copy is kept each time, plus a dated one", len(kept) == 3, str([k["file"] for k in kept]))
check("the newest copy is the one marked latest", kept[0]["latest"] and kept[0]["words"] == 1, str(kept[0]))
notebook.backup("Keeping", "third\n")                     # too soon for another dated copy
check("copies made moments apart don't pile up", len(notebook.backups("Keeping")) == 3,
      str(len(notebook.backups("Keeping"))))
check("the newest copy is still updated anyway",
      notebook.backup_read("Keeping-latest.md") == "third\n", repr(notebook.backup_read("Keeping-latest.md")))
check("a copy can be read back by name", notebook.backup_read(kept[1]["file"]) is not None)
check("and nothing outside the copies folder can be",
      notebook.backup_read("../../Keeping.md") is None and notebook.backup_read("..\\Keeping.md") is None)
check("the copies folder is hidden from the notes list",
      not any(n["name"].startswith(".backups") for n in notebook.all_notes()),
      str([n["name"] for n in notebook.all_notes()]))
check("when a note was last written is known in milliseconds",
      abs(notebook.saved_at("Keeping") - time.time() * 1000) < 60000, str(notebook.saved_at("Keeping")))
check("and a note that doesn't exist answers zero, not an error", notebook.saved_at("No such note") == 0)

# =============================================================== 3. the notebook itself
exported = {}


def fake_export(args):
    name, text, fmt = args
    data = notebook.to_file(name, text, fmt)
    out = vault / f"exported-{fmt}{dict((f['id'], f['ext']) for f in notebook.EXPORTS)[fmt]}"
    out.write_bytes(data)
    exported[fmt] = out
    return {"saved": str(out), "msg": f"Saved {out.name}"}


coming_in = vault / "trip.docx"
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge")
    ctx = br.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.expose_function("_nb", lambda fn, a: getattr(notebook, fn)(*a))
    pg.expose_function("_formats", lambda: {"out": notebook.EXPORTS, "in": notebook.IMPORTS})
    pg.expose_function("_export", fake_export)
    pg.expose_function("_import", lambda: notebook.from_file(coming_in))
    pg.add_init_script("sessionStorage.introSeen='1';sessionStorage.bootSound='1';localStorage.tab='notebook';" + STUB)
    pg.goto(page_file.as_uri())
    pg.wait_for_timeout(1800)

    # ---- everything saves itself
    pg.evaluate("""() => { const b = [...document.querySelectorAll('.nb-item')].find(x => x.dataset.n === 'Keeping'); b && b.click(); }""")
    pg.wait_for_timeout(700)
    pg.click(".nb-ed")
    pg.keyboard.press("Control+End")
    pg.keyboard.type(" straight away")
    pg.wait_for_timeout(60)                                  # far less than the wait before the file is written
    kept_now = pg.evaluate("() => JSON.parse(localStorage.nbMirror || 'null')")
    check("a keystroke is in browser storage before the file is written",
          kept_now and "straight away" in kept_now["body"], str(kept_now)[:200])
    check("and the copy says which note it belongs to and when", kept_now["name"] == "Keeping" and kept_now["at"] > 0)
    check("the file itself has not caught up yet", "straight away" not in read("Keeping"), read("Keeping"))
    pg.wait_for_timeout(1100)
    check("a moment later the file has it too", "straight away" in read("Keeping"), read("Keeping"))
    check("and an older copy of it was kept on the computer",
          any("Keeping" in b["file"] for b in notebook.backups("Keeping")))

    # ---- a copy newer than the file is picked up on the way back in
    (vault / "Keeping.md").write_text("what the file says\n", encoding="utf-8")
    pg.evaluate("""() => localStorage.nbMirror = JSON.stringify(
        {name: 'Keeping', at: Date.now() + 4000, raw: false, body: '<p>rescued from before</p>'})""")
    pg.reload()
    pg.wait_for_timeout(2200)
    check("the newer copy wins when the notebook opens again", "rescued from before" in read("Keeping"),
          read("Keeping"))
    check("and the page says so", "before" in pg.locator(".nb-saved").inner_text().lower(),
          pg.locator(".nb-saved").inner_text())

    (vault / "Keeping.md").write_text("the file is the newer one\n", encoding="utf-8")
    pg.evaluate("""() => localStorage.nbMirror = JSON.stringify(
        {name: 'Keeping', at: Date.now() - 600000, raw: false, body: '<p>stale rubbish</p>'})""")
    pg.reload()
    pg.wait_for_timeout(2200)
    check("an old copy left over from before does not win", "stale rubbish" not in read("Keeping"), read("Keeping"))

    # ---- the File menu
    pg.click(".nb-fbtn")
    pg.wait_for_timeout(250)
    labels = pg.eval_on_selector_all(".nb-fmenu button", "els => els.map(e => e.textContent)")
    for want in ["New note", "Open a note", "Save now", "Save a copy", "Export as a file", "Import a file",
                 "Go back to an earlier save", "Rename", "Duplicate", "Move to a folder", "Move to the trash",
                 "Print", "Find and replace", "Word count", "Undo", "Redo"]:
        check(f"the File menu has {want}", any(want in l for l in labels), " | ".join(labels))
    check("and the keys are written next to the ones that have them",
          pg.locator(".nb-fmenu .key").count() >= 6, str(pg.locator(".nb-fmenu .key").count()))
    pg.screenshot(path=str(shots / "menu.png"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    check("Escape closes the menu", pg.locator(".nb-fmenu").is_hidden())

    pg.click(".nb-fbtn")
    pg.wait_for_timeout(200)
    recent = pg.eval_on_selector_all(".nb-fmenu [data-open]", "els => els.map(e => e.dataset.open)")
    check("the notes you had open lately are listed", "Start here" in recent, str(recent))
    pg.keyboard.press("Escape")

    # ---- find and replace
    pg.evaluate("""() => { const b = [...document.querySelectorAll('.nb-item')].find(x => x.dataset.n === 'Start here'); b.click(); }""")
    pg.wait_for_timeout(900)
    pg.click(".nb-ed")
    pg.keyboard.press("Control+f")
    pg.wait_for_timeout(250)
    check("Ctrl+F opens find inside the note", not pg.locator(".nb-far").is_hidden())
    pg.fill(".nb-far-q", "toolbar")
    pg.wait_for_timeout(350)
    count_text = pg.locator(".nb-far-n").inner_text()
    check("it counts what it found", "OF" in count_text.upper() and count_text.strip()[0] == "1", count_text)
    many_hits = pg.evaluate("() => CSS.highlights.get('nb-find').size + CSS.highlights.get('nb-find-at').size")
    check("and paints them without touching the writing", many_hits >= 2, str(many_hits))
    before_find = read("Start here")
    pg.click(".nb-far-down")
    pg.wait_for_timeout(250)
    check("the next one can be stepped to", pg.locator(".nb-far-n").inner_text().strip().startswith("2"),
          pg.locator(".nb-far-n").inner_text())
    check("and searching changed nothing in the note", read("Start here") == before_find)
    pg.screenshot(path=str(shots / "find.png"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    check("Escape closes the find bar", pg.locator(".nb-far").is_hidden())
    check("and the painting is cleared with it",
          pg.evaluate("() => !CSS.highlights.get('nb-find') || CSS.highlights.get('nb-find').size === 0"))

    pg.evaluate("""() => { const b = [...document.querySelectorAll('.nb-item')].find(x => x.dataset.n === 'Keeping'); b.click(); }""")
    pg.wait_for_timeout(700)
    pg.click(".nb-ed")
    pg.keyboard.press("Control+End")
    pg.keyboard.press("Enter")
    pg.keyboard.type("wombat here and wombat there and one more wombat")
    pg.wait_for_timeout(900)
    pg.keyboard.press("Control+h")
    pg.wait_for_timeout(250)
    check("Ctrl+H opens it with the cursor in the replace box",
          pg.evaluate("() => document.activeElement.classList.contains('nb-far-r')"))
    pg.fill(".nb-far-q", "wombat")
    pg.fill(".nb-far-r", "badger")
    pg.wait_for_timeout(350)
    pg.click(".nb-far-one")
    pg.wait_for_timeout(1100)
    check("Replace changes one of them", read("Keeping").count("badger") == 1 and read("Keeping").count("wombat") == 2,
          read("Keeping"))
    pg.click(".nb-far-all")
    pg.wait_for_timeout(1100)
    check("Replace all changes the rest", read("Keeping").count("badger") == 3 and "wombat" not in read("Keeping"),
          read("Keeping"))
    pg.click(".nb-far-x")

    # ---- Ctrl+Shift+F still searches every note, and Ctrl+Shift+H still highlights
    pg.click(".nb-ed")
    pg.keyboard.press("Control+Shift+f")
    pg.wait_for_timeout(300)
    check("Ctrl+Shift+F puts the cursor in the search-every-note box",
          pg.evaluate("() => document.activeElement.classList.contains('nb-q')"))
    pg.click(".nb-ed")
    pg.keyboard.press("Control+End")
    pg.keyboard.press("Enter")
    pg.keyboard.press("Control+Shift+h")
    pg.keyboard.type("lit up")
    pg.wait_for_timeout(1100)
    check("and Ctrl+Shift+H still highlights rather than opening find", "==lit up==" in read("Keeping"),
          read("Keeping"))

    # ---- word count
    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="count"]')
    pg.wait_for_timeout(400)
    counts = pg.eval_on_selector_all(".nb-counts small", "els => els.map(e => e.textContent)")
    check("word count shows more than just words",
          {"words", "characters", "paragraphs"} <= set(counts), str(counts))
    check("and the numbers are real", all(int(t) >= 0 for t in
                                          pg.eval_on_selector_all(".nb-counts b", "els => els.map(e => e.textContent)")))
    pg.screenshot(path=str(shots / "count.png"))
    pg.keyboard.press("Escape")

    # ---- duplicate, save a copy, move to a folder
    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="dup"]')
    pg.wait_for_timeout(1300)
    check("Duplicate makes another note with the same writing",
          (vault / "Keeping copy.md").exists() and read("Keeping copy") == read("Keeping"),
          str(sorted(f.name for f in vault.glob("*.md"))))
    check("and moves you into it", pg.locator(".nb-title").input_value() == "Keeping copy",
          pg.locator(".nb-title").input_value())

    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="sas"]')
    pg.wait_for_timeout(400)
    check("Save a copy offers a name that is not taken yet",
          pg.locator(".nb-sheet-b .s-n").input_value() not in [f.stem for f in vault.glob("*.md")],
          pg.locator(".nb-sheet-b .s-n").input_value())
    pg.fill(".nb-sheet-b .s-n", "Second thoughts")
    pg.click(".nb-sheet-b .s-ok")
    pg.wait_for_timeout(1300)
    check("and makes it when you say so", (vault / "Second thoughts.md").exists(),
          str(sorted(f.name for f in vault.glob("*.md"))))

    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="move"]')
    pg.wait_for_timeout(400)
    pg.fill(".nb-sheet-b .s-f", "Modules/Databases")
    pg.click(".nb-sheet-b .s-ok")
    pg.wait_for_timeout(1800)
    check("Move to a folder really moves the file",
          (vault / "Modules" / "Databases" / "Second thoughts.md").exists()
          and not (vault / "Second thoughts.md").exists(),
          str(sorted(str(f.relative_to(vault)) for f in vault.rglob("*.md"))))

    # ---- open a note
    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="open"]')
    pg.wait_for_timeout(400)
    pg.fill(".nb-sheet-b .s-q", "Linking")
    pg.wait_for_timeout(300)
    check("Open a note narrows down as you type", pg.locator(".nb-sheet-b .nb-pickrow").count() == 1,
          str(pg.locator(".nb-sheet-b .nb-pickrow").count()))
    pg.click(".nb-sheet-b .nb-pickrow")
    pg.wait_for_timeout(900)
    check("and opens the one you pick", pg.locator(".nb-title").input_value() == "Linking notes",
          pg.locator(".nb-title").input_value())

    # ---- out as a file
    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="exp"]')
    pg.wait_for_timeout(400)
    rows = pg.eval_on_selector_all(".nb-sheet-b .nb-pickrow", "els => els.map(e => e.textContent)")
    check("every format is offered", len(rows) == 4, str(len(rows)))
    check("and each one says what it will lose",
          all(len(r) > 70 for r in rows[:2]) and ".docx" in " ".join(rows) and ".pdf" in " ".join(rows),
          " || ".join(r[:50] for r in rows))
    pg.screenshot(path=str(shots / "export.png"))
    pg.click('.nb-sheet-b [data-f="pdf"]')
    pg.wait_for_timeout(1200)
    check("picking one writes the file", "pdf" in exported and exported["pdf"].read_bytes()[:5] == b"%PDF-",
          str(list(exported)))
    check("and the page says it was saved", "saved" in pg.locator(".nb-saved").inner_text().lower(),
          pg.locator(".nb-saved").inner_text())

    # ---- in from a file
    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="imp"]')
    pg.wait_for_timeout(1600)
    check("a Word document comes in as a new note", (vault / "trip.md").exists(),
          str(sorted(f.name for f in vault.glob("*.md"))))
    check("with its headings and bold intact",
          "# Revision plan" in read("trip") and "**bold**" in read("trip"), read("trip")[:200])
    check("and the notebook says what that kind of file could not carry",
          pg.locator(".nb-sheet").is_visible() and "did not come across" in pg.locator(".nb-sheet-b").inner_text(),
          pg.locator(".nb-sheet-b").inner_text()[:160] if pg.locator(".nb-sheet").is_visible() else "no sheet")
    pg.screenshot(path=str(shots / "import.png"))
    pg.click(".nb-sheet-b .s-ok")

    # ---- going back to an earlier save
    pg.evaluate("""() => { const b = [...document.querySelectorAll('.nb-item')].find(x => x.dataset.n === 'Keeping'); b.click(); }""")
    pg.wait_for_timeout(900)
    notebook.backup("Keeping", "# An earlier version\n\nFrom before all of that.\n", every=0)
    pg.click(".nb-fbtn")
    pg.click('.nb-fmenu [data-k="revert"]')
    pg.wait_for_timeout(700)
    rows = pg.locator(".nb-sheet-b .nb-pickrow").count()
    check("the copies on the computer are listed", rows >= 2, str(rows))
    check("with the time and the word count of each",
          "word" in pg.locator(".nb-sheet-b .nb-pickrow").first.inner_text(),
          pg.locator(".nb-sheet-b .nb-pickrow").first.inner_text())
    pg.screenshot(path=str(shots / "revert.png"))
    pg.on("dialog", lambda d: d.accept())
    pg.click(".nb-sheet-b .nb-pickrow")
    pg.wait_for_timeout(1800)
    check("putting one back writes it over the note",
          "From before all of that." in read("Keeping"), read("Keeping"))
    check("and what the note said a moment ago was kept as a copy too",
          any("badger" in (notebook.backup_read(b["file"]) or "") for b in notebook.backups("Keeping")),
          str([b["file"] for b in notebook.backups("Keeping")]))

    # ---- printing
    pg.evaluate("() => { window.__printed = 0; window.print = () => window.__printed++; }")
    pg.keyboard.press("Control+p")
    pg.wait_for_timeout(400)
    check("Ctrl+P asks the printer for a copy of the note",
          pg.evaluate("() => window.__printed") == 1)
    check("and that copy is only the note, with its name on top",
          pg.evaluate("""() => { const b = document.querySelector('.nb-printing');
            return !!b && b.querySelector('h1').textContent === 'Keeping' && !b.querySelector('[contenteditable]'); }"""))
    check("everything else is set aside while it prints",
          pg.evaluate("() => document.documentElement.classList.contains('nb-print')"))
    pg.evaluate("() => dispatchEvent(new Event('afterprint'))")
    pg.wait_for_timeout(300)
    check("and put back afterwards",
          pg.evaluate("""() => !document.querySelector('.nb-printing')
            && !document.documentElement.classList.contains('nb-print')"""))

    # ---- undo and redo
    pg.click(".nb-ed")
    pg.keyboard.press("Control+End")
    pg.keyboard.type("ZZTOP")
    pg.wait_for_timeout(900)
    pg.keyboard.press("Control+z")
    pg.wait_for_timeout(1000)
    check("Ctrl+Z takes typing back out", "ZZTOP" not in read("Keeping"), read("Keeping")[-80:])
    pg.keyboard.press("Control+Shift+z")
    pg.wait_for_timeout(1000)
    check("Ctrl+Shift+Z puts it back", "ZZTOP" in read("Keeping"), read("Keeping")[-80:])

    errs[:] = [e for e in errs if "allow-same-origin" not in e]
    br.close()

print(f"\n{passes}/{passes + len(fails)} passed")
if fails:
    print("failed:", fails)
print("PAGE ERRORS:", errs)
print("pictures in", shots)
sys.exit(1 if fails or errs else 0)
