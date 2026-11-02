"""Notebook: a Word-style editor for your own notes, with [[links]], pictures, tags and a graph.

Notes are plain .md files in one folder (the "vault"), so Obsidian or any editor can open it too.
"""

import base64
import hashlib
import re
from datetime import datetime
from pathlib import Path

import markdown

ROOT = Path("Notebook")  # main.py points this at the real folder before anything else runs
IMAGE_TYPES = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "gif": "gif", "webp": "webp"}

GUIDE = """# Start here

Welcome to your **Notebook**. It works like Word: just type, and use the toolbar at the top for styles.
Everything is saved automatically as a normal file in the Notebook folder, so it's never locked inside the app.

## Writing

- **Bold**, *italic* and <u>underline</u>: select some text and click **B**, *I* or U, or press Ctrl+B, Ctrl+I or Ctrl+U.
- ==Highlight== and ~~strikethrough~~ are in the toolbar too.
- **Headings:** pick *Heading 1*, *Heading 2* or *Heading 3* from the style box, or press Ctrl+1, Ctrl+2 or Ctrl+3 (Ctrl+0 goes back to normal text).
- **Lists:** use the bullet, numbered or checklist buttons. Press Tab to indent and Shift+Tab to go back. Press Enter on an empty line to stop the list.
- **Checklists:** click a box to tick it off.
- **Tables:** click *Table*. Press Tab to move between cells (Tab in the last cell adds a row). The *+ Row*, *+ Column* and *− Row* buttons appear while you're in a table.
- **Pictures:** paste a screenshot (Ctrl+V), drag a picture onto the page, or click *Picture*.
- **Undo** and **redo:** Ctrl+Z and Ctrl+Y, or the arrows at the left of the toolbar.

Shortcuts that save time: at the start of a line, type **-** then a space to start a list, **1.** then a space for a numbered list,
`[]` then a space for a checklist, **#** then a space for a heading and **>** then a space for a quote.
Type **---** and press Enter for a dividing line.

## Linking notes

- Click **Link note** (or press Ctrl+K) and pick the note to link. You can also type `[[` and choose from the list.
- Linking to a note that doesn't exist yet makes it when you click the link.
- Click a link to open that note. Links look like this: [[Linking notes]].
- The panel on the right shows which notes **link here**, which notes this one **links to**, an **outline**
  of your headings (click one to jump to it) and notes that **mention** this one, with a button to link them.
- **Graph** shows every note as a dot and every link as a line. Click a dot to open it.

## Organising

- **+ New note** has templates for lectures, assignments and group meetings.
- **Today** opens a note for today in the *Daily* folder.
- **Rename** a note by clicking its name at the top. Links to it are updated everywhere.
- **Search** looks through the names and text of every note.
- **Tags:** write a word starting with # (like #exam) anywhere in a note, then click the tag on the left to see every note with it.
- **Pin** keeps a note at the top of the list. **Delete** moves it to the trash folder, so you can get it back.

## Making it comfortable

- **A−** and **A+** change the text size. **Wide** uses the whole width of the window.
- The buttons at either end of the title hide or show the notes list and the side panel, to give your writing more room.
- Everything works from the keyboard. Tab moves between areas, and the arrow keys move along the toolbar.
- **Markdown** shows the note as plain text, if you ever want it.
- **✦ Ask Claude** sends the note to Claude so you can ask about it, get a summary or be quizzed on it.

## Keyboard shortcuts

| Keys | Does |
| --- | --- |
| Ctrl+N | New note |
| Ctrl+S | Save now (it also saves by itself) |
| Ctrl+F | Search your notes |
| Ctrl+G | Graph |
| Ctrl+K | Link a note |
| Ctrl+B, Ctrl+I, Ctrl+U | Bold, italic, underline |
| Ctrl+Shift+H | Highlight |
| Ctrl+0 to Ctrl+3 | Normal text, Heading 1 to 3 |
| Ctrl+Shift+8, 7, 9 | Bullet list, numbered list, checklist |

#guide
"""

EXAMPLE = """# Linking notes

This is an example note. It links back to [[Start here]], so it appears under **Linked from** in that note,
and as a line in the **Graph**.

- [x] Open the notebook
- [ ] Make a note for each module and link them together
- [ ] Try the graph

#guide
"""


def init():
    """Makes the folder and, the first time, the guide notes."""
    (ROOT / "attachments").mkdir(parents=True, exist_ok=True)
    if not any(_notes()):
        (ROOT / "Start here.md").write_text(GUIDE, encoding="utf-8")
        (ROOT / "Linking notes.md").write_text(EXAMPLE, encoding="utf-8")


def _notes():
    return (p for p in ROOT.rglob("*.md") if not any(part.startswith(".") for part in p.relative_to(ROOT).parts))


def note_path(name):
    """'Module/Week 1' -> ROOT/Module/Week 1.md, kept safely inside the folder."""
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "", s).strip(" .")[:80] for s in re.split(r"[\\/]", name or "")]
    parts = [p for p in parts if p] or ["Untitled"]
    path = ROOT.joinpath(*parts[:-1], parts[-1] + ".md").resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("bad note name")
    return path


def note_name(path):
    return path.relative_to(ROOT.resolve()).with_suffix("").as_posix()


def all_notes():
    init()
    return [{"name": note_name(p.resolve()), "text": p.read_text(encoding="utf-8", errors="replace"),
             "mtime": int(p.stat().st_mtime * 1000)} for p in _notes()]


def _relink(old, new):
    """Points [[old]] links (with any #heading or |alias) at the new name, in every note."""
    pat = re.compile(r"\[\[\s*" + re.escape(old) + r"\s*(#[^\]|\n]*)?(\|[^\]\n]*)?\]\]", re.I)
    for p in _notes():
        text = p.read_text(encoding="utf-8", errors="replace")
        fixed = pat.sub(lambda m: f"[[{new}{m.group(1) or ''}{m.group(2) or ''}]]", text)
        if fixed != text:
            p.write_text(fixed, encoding="utf-8")


def save(name, text, old=None):
    """Writes a note. With old set to another name, renames it first and fixes links to it."""
    try:
        path = note_path(name)
    except ValueError:
        return {"error": "That name can't be used."}
    if old:
        src = note_path(old)
        if src != path:
            if path.exists() and path.resolve() != src.resolve():  # allow changing only the case
                return {"error": f"There's already a note called “{note_name(path)}”."}
            path.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                src.rename(path)
            _relink(old, note_name(path))
            text = re.sub(r"\[\[\s*" + re.escape(old) + r"\s*(?=[#|\]])", lambda _: "[[" + note_name(path), text, flags=re.I)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"name": note_name(path), "mtime": int(path.stat().st_mtime * 1000)}


def delete(name):
    """Moves a note to .trash, so it can be got back."""
    path = note_path(name)
    if not path.exists():
        return "ok"
    trash = ROOT / ".trash"
    trash.mkdir(exist_ok=True)
    path.rename(trash / f"{datetime.now():%Y-%m-%d %H%M%S} {path.name}")
    return "ok"


def save_image(data_url, filename=""):
    """Saves a pasted or dropped picture into attachments/ and returns its path for the note."""
    m = re.match(r"data:image/(\w+);base64,(.+)", data_url or "", re.S)
    if not m or m.group(1).lower() not in IMAGE_TYPES:
        return {"error": "Only PNG, JPG, GIF and WebP pictures can be added."}
    data = base64.b64decode(m.group(2))
    if len(data) > 20 * 1024 * 1024:
        return {"error": "That picture is over 20 MB."}
    stem = re.sub(r"[^\w-]+", "-", Path(filename).stem).strip("-")[:40] or f"image-{datetime.now():%Y%m%d}"
    name = f"{stem}-{hashlib.sha1(data).hexdigest()[:6]}.{IMAGE_TYPES[m.group(1).lower()]}"
    (ROOT / "attachments").mkdir(parents=True, exist_ok=True)
    (ROOT / "attachments" / name).write_bytes(data)
    return {"path": f"attachments/{name}"}


BLOCK_START = re.compile(r"\s*([-*+]\s|\d+\.\s|>|\|)")
INLINE = [(re.compile(r"~~(\S(?:.*?\S)?)~~"), r"<del>\1</del>"), (re.compile(r"==(\S(?:.*?\S)?)=="), r"<mark>\1</mark>")]


def render(text):
    out, fenced, prev = [], False, ""
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            # like Obsidian, a list, quote or table may start straight under a line of text, with no blank line
            if prev.strip() and BLOCK_START.match(line) and not BLOCK_START.match(prev):
                out.append("")
            if re.match(r"#[A-Za-z]", line):  # "#tag" at the start of a line is a tag, not a heading
                line = "\\" + line
            for pat, rep in INLINE:  # ~~strike~~ and ==highlight==
                line = pat.sub(rep, line)
        out.append(line)
        prev = line
    return markdown.markdown("\n".join(out), extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


def _icon(paths):
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{paths}</svg>'


ICONS = {
    "more": '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>',
    "undo": _icon('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'),
    "redo": _icon('<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>'),
    "ul": _icon('<circle cx="5" cy="6" r="1.2"/><circle cx="5" cy="12" r="1.2"/><circle cx="5" cy="18" r="1.2"/><path d="M10 6h10M10 12h10M10 18h10"/>'),
    "ol": _icon('<path d="M10 6h10M10 12h10M10 18h10M4 4.5h1.5V9M4 9h3M4 14.5c0-1 2.8-1 2.8.4 0 1-2.8 2.1-2.8 3.6h3"/>'),
    "task": _icon('<rect x="3" y="4" width="6" height="6" rx="1.2"/><path d="m4.6 7 1.2 1.3L8 5.8M12 7h8M12 17h8"/><rect x="3" y="14" width="6" height="6" rx="1.2"/>'),
    "outdent": _icon('<path d="M11 6h9M11 12h9M11 18h9M7 9l-3 3 3 3"/>'),
    "indent": _icon('<path d="M11 6h9M11 12h9M11 18h9M4 9l3 3-3 3"/>'),
    "side": _icon('<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>'),
    "info": _icon('<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16"/>'),
}

NB_HTML = f"""
<div class="nb glass">
 <aside class="nb-side" aria-label="Your notes">
  <div class="nb-find"><input class="nb-q" type="search" placeholder="Search notes" aria-label="Search notes"></div>
  <div class="nb-acts">
   <span class="nb-newwrap"><button class="btn nb-new" aria-haspopup="menu" title="New note (Ctrl+N)">+ New note</button>
    <span class="nb-menu glass" role="menu" hidden>
     <button role="menuitem" data-t="blank">Blank note</button><button role="menuitem" data-t="lecture">Lecture notes</button>
     <button role="menuitem" data-t="assignment">Assignment</button><button role="menuitem" data-t="meeting">Group meeting</button></span></span>
   <button class="ghost nb-today" title="Open today's note">Today</button>
   <button class="ghost nb-graph-btn" title="See how your notes connect (Ctrl+G)">Graph</button>
  </div>
  <div class="nb-tags" aria-label="Filter by tag"></div>
  <div class="nb-list" role="list"></div>
  <div class="nb-foot"><span class="nb-count"></span><button class="ghost nb-folder" title="Open the folder your notes are saved in">Folder</button></div>
 </aside>
 <div class="nb-main">
  <div class="nb-top">
   <button class="nb-ib nb-toggle-side" aria-pressed="true" title="Show or hide your notes list">{ICONS['side']}</button>
   <input class="nb-title" spellcheck="false" aria-label="Note name (click to rename)" title="Click to rename">
   <button class="ghost nb-askbtn" title="Ask Claude about this note">✦ Ask Claude</button>
   <button class="ghost nb-pin" aria-pressed="false" title="Keep this note at the top of the list">Pin</button>
   <button class="ghost danger nb-del" title="Move this note to the trash">Delete</button>
   <button class="nb-ib nb-toggle-info" aria-pressed="true" title="Show or hide links and outline">{ICONS['info']}</button>
  </div>
  <div class="nb-tools" role="toolbar" aria-label="Formatting">
   <span class="grp"><button data-c="undo" title="Undo (Ctrl+Z)" aria-label="Undo">{ICONS['undo']}</button><button data-c="redo" title="Redo (Ctrl+Y)" aria-label="Redo">{ICONS['redo']}</button></span>
   <span class="grp"><select class="nb-style" aria-label="Text style" title="Text style (Ctrl+0 to Ctrl+3)">
     <option value="p">Normal text</option><option value="h1">Heading 1</option><option value="h2">Heading 2</option><option value="h3">Heading 3</option>
     <option value="blockquote">Quote</option><option value="pre">Code block</option></select></span>
   <span class="grp"><button data-c="bold" title="Bold (Ctrl+B)" aria-label="Bold" aria-pressed="false"><b>B</b></button>
    <button data-c="italic" title="Italic (Ctrl+I)" aria-label="Italic" aria-pressed="false"><i style="font-family:Georgia,serif">I</i></button>
    <button data-c="underline" title="Underline (Ctrl+U)" aria-label="Underline" aria-pressed="false"><u>U</u></button>
    <button data-c="strikeThrough" title="Strikethrough" aria-label="Strikethrough" aria-pressed="false"><s>S</s></button>
    <button data-a="mark" title="Highlight (Ctrl+Shift+H)" aria-label="Highlight" aria-pressed="false"><span class="hl">ab</span></button>
    <button data-a="code" title="Code" aria-label="Code" aria-pressed="false">&lt;/&gt;</button>
    <button data-a="clear" title="Clear formatting" aria-label="Clear formatting">Clear</button></span>
   <span class="grp"><button data-c="insertUnorderedList" title="Bullet list (Ctrl+Shift+8)" aria-label="Bullet list" aria-pressed="false">{ICONS['ul']}</button>
    <button data-c="insertOrderedList" title="Numbered list (Ctrl+Shift+7)" aria-label="Numbered list" aria-pressed="false">{ICONS['ol']}</button>
    <button data-a="task" title="Checklist (Ctrl+Shift+9)" aria-label="Checklist" aria-pressed="false">{ICONS['task']}</button>
    <button data-c="outdent" title="Decrease indent (Shift+Tab)" aria-label="Decrease indent">{ICONS['outdent']}</button>
    <button data-c="indent" title="Increase indent (Tab)" aria-label="Increase indent">{ICONS['indent']}</button></span>
   <span class="grp"><button data-a="note" class="txt" title="Link to another note (Ctrl+K)">Link note</button>
    <button data-a="web" class="txt" title="Link to a website">Web link</button>
    <button data-a="img" class="txt" title="Add a picture (or paste / drag one in)">Picture</button>
    <button data-a="table" class="txt" title="Add a table">Table</button>
    <button data-a="hr" class="txt" title="Add a dividing line">Line</button></span>
   <span class="grp nb-tbl" hidden><button data-a="row" class="txt" title="Add a row below">+ Row</button>
    <button data-a="col" class="txt" title="Add a column to the right">+ Column</button>
    <button data-a="delrow" class="txt" title="Delete this row">− Row</button></span>
   <span class="nb-gap"></span>
   <span class="grp"><button data-a="smaller" title="Smaller text" aria-label="Smaller text">A−</button><button data-a="bigger" title="Bigger text" aria-label="Bigger text">A+</button>
    <button data-a="wide" class="txt" title="Use the full width for the page" aria-pressed="false">Wide</button>
    <button data-a="raw" class="txt" title="Show the note as Markdown text" aria-pressed="false">Markdown</button>
    <button data-a="help" title="How the notebook works" aria-label="Help">?</button>
    <button data-a="tools" class="nb-tcfg" title="Choose which buttons show" aria-label="Choose which buttons show" aria-pressed="false">{ICONS['more']}</button></span>
   <input type="file" class="nb-file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden>
  </div>
  <div class="nb-tpick" hidden role="dialog" aria-label="Choose which buttons show">
   <b>Buttons on the toolbar</b><div class="nb-tpick-l"></div>
   <button class="nb-tpick-r">Show them all</button>
  </div>
  <div class="nb-scroll">
   <div class="nb-ed" contenteditable="true" spellcheck="true" role="textbox" aria-multiline="true" aria-label="Note text" data-ph="Start typing…"></div>
   <textarea class="nb-src" spellcheck="true" aria-label="Note as Markdown" hidden></textarea>
  </div>
  <div class="nb-status"><span class="nb-words"></span><span class="nb-saved" role="status" aria-live="polite"></span></div>
  <div class="nb-ac glass" role="dialog" aria-label="Link a note" hidden><input class="nb-ac-q" placeholder="Find a note to link" aria-label="Find a note"><div class="nb-ac-list" role="listbox"></div></div>
 </div>
 <aside class="nb-info" aria-label="Links and outline">
  <span class="label">Linked from</span><div class="nb-back"></div>
  <span class="label">Links to</span><div class="nb-out"></div>
  <span class="label">Outline</span><div class="nb-outline"></div>
  <span class="label">Mentioned in</span><div class="nb-ment"></div>
 </aside>
 <div class="nb-graph" hidden>
  <div class="nb-gbar"><span class="label">Graph</span><span class="seg nb-gscope"><button data-s="all">All notes</button><button data-s="local">This note</button></span>
   <span class="sub" style="margin:0">Click a dot to open · drag to move · scroll to zoom</span>
   <button class="ghost nb-gclose" aria-label="Close graph">✕ Close</button></div>
  <canvas aria-label="Graph of your notes and the links between them"></canvas>
 </div>
</div>
"""

NB_CSS = """
html:has(#notebook.on) header{display:none}
main:has(#notebook.on){max-width:none;padding:66px 16px 0}
main:has(#notebook.on)::after{height:0}
.nb{position:relative;--page:#fff;--fs:17px;--pw:840px;display:grid;grid-template-columns:auto minmax(0,1fr) auto;height:max(480px,calc(100vh - 160px));
 border-radius:24px;overflow:hidden;position:relative;margin:0}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .nb{--page:#121212}}
:root[data-theme=dark] .nb{--page:#121212}
.nb.wide{--pw:100%}
.nb aside{display:flex;flex-direction:column;min-height:0;padding:14px}
.nb-side{grid-column:1;width:260px;box-sizing:border-box;border-right:1px solid var(--line)}
.nb-main{grid-column:2} .nb-info{grid-column:3;width:250px;box-sizing:border-box;border-left:1px solid var(--line);overflow-y:auto;gap:2px}
.nb.no-side .nb-side,.nb.no-info .nb-info{display:none}
.nb :focus-visible{outline:2px solid var(--text);outline-offset:2px}
.nb-q{width:100%;box-sizing:border-box;padding:10px 16px;border-radius:999px;border:1px solid var(--line);background:var(--inset);color:inherit;font:inherit;font-size:15px}
.nb-acts{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 6px} .nb-acts .ghost{padding:7px 12px;font-size:11px}
.nb-new{border:0;cursor:pointer;padding:8px 14px;font-size:11.5px}
.nb-newwrap{position:relative}
.nb-menu{position:absolute;top:calc(100% + 6px);left:0;z-index:9;display:flex;flex-direction:column;padding:6px;border-radius:16px;min-width:190px;
 background:var(--glass-strong);animation:rise .35s var(--spring) both}
.nb-menu button{border:0;background:transparent;color:var(--text);text-align:left;padding:10px 12px;border-radius:10px;cursor:pointer;font:inherit;font-size:15px}
.nb-menu button:hover,.nb-menu button:focus-visible{background:var(--inset)}
.nb-tags{display:flex;flex-wrap:wrap;gap:5px;margin:4px 0 8px} .nb-tags:empty{display:none}
.nb-tag{font:11px var(--mono);letter-spacing:.04em;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 10px;cursor:pointer;background:transparent}
.nb-tag.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.nb-list{flex:1;overflow-y:auto;margin:0 -6px;padding:0 6px;scrollbar-width:thin}
.nb-item{display:block;width:100%;text-align:left;border:0;background:transparent;color:inherit;padding:10px 12px;border-radius:12px;cursor:pointer;
 transition:background .2s,transform .4s var(--bounce)} .nb-item:hover{background:var(--inset)} .nb-item:active{transform:scale(.98)}
.nb-item.on{background:var(--glass-strong);box-shadow:inset 0 1px 0 var(--rim)}
.nb-item b{display:flex;align-items:center;gap:6px;font-weight:600;font-size:15px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.nb-item b .pin{width:7px;height:7px;border-radius:50%;background:var(--text);flex:none}
.nb-item small{display:block;color:var(--muted);font-size:13px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;margin-top:2px}
.nb-item small.when{font:11px var(--mono);letter-spacing:.05em;text-transform:uppercase}
.nb-item mark{background:color-mix(in srgb,var(--red) 30%,transparent);color:inherit;border-radius:3px}
.nb-foot{display:flex;align-items:center;justify-content:space-between;padding-top:10px;border-top:1px solid var(--line);margin-top:8px;
 font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)} .nb-foot .ghost{padding:6px 10px;font-size:10.5px}
.nb-main{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
.nb-top{display:flex;align-items:center;gap:8px;padding:10px 12px 6px}
.nb-top .ghost{padding:7px 12px;font-size:11px;flex:none}
.nb-ib{display:grid;place-items:center;width:36px;height:36px;border:0;border-radius:10px;background:transparent;color:var(--muted);cursor:pointer;flex:none}
.nb-ib svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.nb-ib:hover{background:var(--inset);color:var(--text)} .nb-ib[aria-pressed=true]{color:var(--text)}
.nb-title{flex:1;min-width:0;border:0;background:transparent;color:inherit;font:650 24px/1.2 "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
 padding:6px 10px;border-radius:10px;outline:none;text-overflow:ellipsis} .nb-title:hover{background:var(--inset)} .nb-title:focus{background:var(--inset)}
.nb-pin[aria-pressed=true]{background:var(--text);color:var(--bg)}
.nb-tools{display:flex;flex-wrap:wrap;align-items:center;gap:4px 2px;padding:4px 10px 8px;border-bottom:1px solid var(--line)}
.nb-tools .grp{display:flex;align-items:center;gap:1px;padding:0 6px;border-right:1px solid var(--line)} .nb-tools .grp:last-of-type{border-right:0}
.nb-tools button{display:grid;place-items:center;border:0;background:transparent;color:var(--text);min-width:34px;height:34px;padding:0 8px;border-radius:9px;cursor:pointer;
 font:15px/1 "Segoe UI",system-ui,sans-serif;transition:background .2s,transform .4s var(--bounce)}
.nb-tools button.txt{font-size:13.5px} .nb-tools button svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.nb-tools button svg circle{fill:currentColor;stroke:none}
.nb-tools button:hover{background:var(--inset)} .nb-tools button:active{transform:scale(.92)}
.nb-tools button.on,.nb-tools button[aria-pressed=true]{background:var(--text);color:var(--bg)}
.nb-tools .hl{background:#ffe066;color:#000;padding:0 3px;border-radius:3px;font-size:13px}
.nb-tpick{position:absolute;right:14px;z-index:40;width:244px;max-height:62vh;overflow:auto;padding:12px;border-radius:16px;
 background:var(--card);border:1px solid var(--line);box-shadow:0 18px 50px rgba(0,0,0,.22)}
.nb-tpick > b{display:block;font-size:13.5px;margin-bottom:8px}
.nb-tpick-i{display:flex;align-items:center;gap:9px;padding:5px 4px;border-radius:8px;cursor:pointer;font-size:13.5px}
.nb-tpick-i:hover{background:var(--inset)} .nb-tpick-i input{accent-color:var(--text);width:15px;height:15px;cursor:pointer}
.nb-tpick-r{margin-top:9px;width:100%;border:1px solid var(--line);background:transparent;color:inherit;font:inherit;
 font-size:12.5px;padding:7px;border-radius:10px;cursor:pointer}
.nb-tpick-r:hover{background:var(--inset)}
.nb-style{height:34px;border:1px solid var(--line);border-radius:9px;background:var(--page);color:var(--text);padding:0 10px;font:14px "Segoe UI",system-ui,sans-serif;cursor:pointer;min-width:140px}
.nb-gap{flex:1}
.nb-scroll{flex:1;overflow-y:auto;min-height:0;scrollbar-gutter:stable}
.nb-ed{position:relative;box-sizing:border-box;max-width:var(--pw);min-height:calc(100% - 40px);margin:20px auto;padding:48px clamp(22px,6vw,72px) 80px;background:var(--page);
 border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.08),0 0 0 1px var(--line);outline:none;font:var(--fs)/1.7 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
 color:var(--text);overflow-wrap:anywhere;caret-color:var(--red)}
.nb.wide .nb-ed{margin:0;border-radius:0;box-shadow:none}
.nb-ed:empty::before,.nb-ed > p:only-child:has(> br:only-child)::before{content:attr(data-ph);color:var(--muted);pointer-events:none;position:absolute}
.nb-ed p{margin:0 0 .6em} .nb-ed h1{font-size:1.9em;line-height:1.2;margin:.6em 0 .35em} .nb-ed h2{font-size:1.45em;margin:.9em 0 .3em}
.nb-ed h3{font-size:1.2em;margin:.9em 0 .25em} .nb-ed h4{font-size:1.05em;margin:.8em 0 .2em} .nb-ed > :first-child{margin-top:0}
.nb-ed ul,.nb-ed ol{padding-left:1.6em;margin:.2em 0 .7em} .nb-ed li{margin:.15em 0}
.nb-ed ul:has(> li > input[type=checkbox]){padding-left:1.45em}
.nb-ed li:has(> input[type=checkbox]){list-style:none;margin-left:-1.15em}
.nb-ed li > input[type=checkbox]{width:1.05em;height:1.05em;margin:0 .55em 0 0;vertical-align:-.15em;accent-color:var(--text);cursor:pointer}
.nb-ed li:has(> input:checked){color:var(--muted);text-decoration:line-through}
.nb-ed blockquote{margin:.8em 0;padding:.2em 18px;border-left:4px solid var(--text);color:var(--muted)}
.nb-ed code{font:.85em var(--mono);background:var(--inset);padding:2px 6px;border-radius:5px}
.nb-ed pre{background:var(--inset);padding:14px 16px;border-radius:12px;overflow-x:auto;font:.85em/1.6 var(--mono);white-space:pre-wrap} .nb-ed pre code{background:none;padding:0;font:inherit}
.nb-ed mark{background:#ffe066;color:#000;border-radius:3px;padding:0 2px}
.nb-ed table{border-collapse:collapse;margin:.8em 0;min-width:50%} .nb-ed th,.nb-ed td{border:1px solid var(--line);padding:8px 12px;min-width:60px;text-align:left;vertical-align:top}
.nb-ed th{background:var(--inset);font-weight:600}
.nb-ed hr{border:0;border-top:2px solid var(--line);margin:1.4em 0}
.nb-ed img{max-width:100%;height:auto;border-radius:10px;display:block;margin:10px 0;cursor:default} .nb-ed img:hover{box-shadow:0 0 0 2px var(--line)}
.nb-ed a[href]{color:inherit;text-decoration:underline;text-underline-offset:3px}
.nb-ed a.wl{display:inline-block;padding:0 7px;margin:0 1px;border-radius:7px;background:var(--inset);border:1px solid var(--line);color:inherit;text-decoration:none;
 cursor:pointer;line-height:1.5;user-select:all} .nb-ed a.wl::before{content:"↗ ";opacity:.5;font-size:.85em}
.nb-ed a.wl:hover{border-color:var(--text)} .nb-ed a.wl.missing{border-style:dashed;opacity:.7}
.nb-src{flex:1;box-sizing:border-box;width:100%;height:100%;resize:none;border:0;outline:none;background:var(--page);color:inherit;padding:28px 32px;font:15px/1.7 var(--mono);tab-size:4}
.nb.raw .nb-ed{display:none} .nb.raw .nb-src{display:block}
.nb-status{display:flex;justify-content:space-between;padding:8px 18px;border-top:1px solid var(--line);font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.nb-saved.busy::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--red);margin-right:7px;animation:blink 1s steps(2) infinite}
.nb.drop .nb-scroll{outline:3px dashed var(--muted);outline-offset:-12px}
.nb-ac{position:absolute;z-index:8;width:280px;padding:6px;border-radius:14px;background:var(--glass-strong);box-shadow:var(--shadow)}
.nb-ac-q{width:100%;box-sizing:border-box;margin-bottom:4px;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--page);color:inherit;font:inherit;font-size:14px}
.nb-ac:not(.picker) .nb-ac-q{display:none}
.nb-ac-list{max-height:260px;overflow-y:auto}
.nb-ac-list button{display:block;width:100%;text-align:left;border:0;background:transparent;color:inherit;padding:8px 10px;border-radius:9px;cursor:pointer;font:inherit;font-size:14.5px}
.nb-ac-list button.on{background:var(--text);color:var(--bg)} .nb-ac-list small{opacity:.6;margin-left:6px;font-size:12px}
.nb-info .label{margin:12px 2px 4px} .nb-info .label:first-child{margin-top:0}
.nb-info button.lk{display:block;width:100%;text-align:left;border:0;background:transparent;color:inherit;padding:7px 9px;border-radius:10px;cursor:pointer;font:inherit;font-size:14px}
.nb-info button.lk:hover{background:var(--inset)} .nb-info button.lk small{display:block;color:var(--muted);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nb-info button.lk.missing{opacity:.6} .nb-info .none{color:var(--muted);font-size:13px;padding:2px 9px;margin:0}
.nb-outline button.lk{padding:4px 9px;font-size:13.5px} .nb-ment .ghost{padding:3px 9px;font-size:10px;margin-left:6px;flex:none}
.nb-graph{position:absolute;inset:0;z-index:10;display:flex;flex-direction:column;background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(20px);
 animation:rise .45s var(--spring) both}
.nb-gbar{display:flex;align-items:center;flex-wrap:wrap;gap:14px;padding:14px 18px} .nb-gbar .label{margin:0} .nb-gclose{margin-left:auto}
.nb-graph canvas{flex:1;min-height:0;width:100%;cursor:grab} .nb-graph canvas:active{cursor:grabbing}
@media (max-width:1150px){.nb .nb-info{display:none} .nb-toggle-info{display:none}}
@media (max-width:900px){main:has(#notebook.on){padding-top:64px} .nb{height:max(480px,calc(100vh - 205px))}}
@media (max-width:720px){.nb-side{position:absolute;inset:0 auto 0 0;width:min(300px,86%);z-index:9;background:var(--glass-strong);backdrop-filter:blur(24px)}
 .nb-top .ghost{display:none} .nb-ed{padding:28px 20px 60px;margin:0;border-radius:0}}
@media (prefers-reduced-motion:reduce){.nb *,.nb{animation:none!important;transition:none!important}}
"""

NB_JS = r"""
// ---- Notebook: a Word-style editor for your own linked notes, saved as Markdown files
(() => {
const NB = document.querySelector('.nb'); if (!NB) return;
const $ = s => NB.querySelector(s);
const api = () => (window.pywebview && pywebview.api && pywebview.api.nb_list) ? pywebview.api : null;
const store = (k, v) => { try { if (v === undefined) return localStorage[k]; localStorage[k] = v; } catch (_) {} };
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const ED = $('.nb-ed'), SRC = $('.nb-src'), TITLE = $('.nb-title'), AC = $('.nb-ac'), ACQ = $('.nb-ac-q'), ACL = $('.nb-ac-list');
let notes = [], cur = null, saveT = 0, tracked = new Set(), filterTag = null, raw = false, lastRange = null;
let pins = []; try { pins = JSON.parse(store('nbPins') || '[]'); } catch (_) {}

const LINK = /(!?)\[\[([^\]|#\n]+)(#[^\]|\n]*)?(?:\|([^\]\n]*))?\]\]/g;
const IMG = /\.(png|jpe?g|gif|webp|svg)$/i;
const base = n => n.split('/').pop();
const codeless = t => t.replace(/```[\s\S]*?```|`[^`\n]*`/g, '');
const linksOf = t => [...codeless(t).matchAll(LINK)].filter(m => !m[1]).map(m => m[2].trim());
const tagsOf = t => [...codeless(t).matchAll(/(^|\s)#([A-Za-z][\w\/-]*)/g)].map(m => m[2].toLowerCase());
const find = n => { n = (n || '').trim().toLowerCase(); return notes.find(x => x.name.toLowerCase() === n) || notes.find(x => base(x.name).toLowerCase() === n); };
const note = () => notes.find(x => x.name === cur);
const ago = ms => { const s = (Date.now() - ms) / 1000; return s < 60 ? 'just now' : s < 3600 ? Math.floor(s / 60) + ' min ago' : s < 86400 ? Math.floor(s / 3600) + ' h ago'
  : new Date(ms).toLocaleDateString('en-GB', {day: 'numeric', month: 'short'}); };
const status = (t, busy) => { const el = $('.nb-saved'); el.textContent = t; el.classList.toggle('busy', !!busy); };
document.execCommand('defaultParagraphSeparator', false, 'p');

// ================= Markdown -> editor =================
function prep(t) {  // [[links]] and ![[pictures]] -> HTML, but never inside code
  return t.split(/(```[\s\S]*?```|`[^`\n]*`)/).map((part, i) => i % 2 ? part : part.replace(LINK, (m, bang, target, head, alias) => {
    target = target.trim();
    if (bang && IMG.test(target)) return `![${esc(base(target))}](${encodeURI(target.includes('/') ? target : 'attachments/' + target)})`;
    return chip(target, alias, head);
  })).join('');
}
const chip = (name, alias, head) => `<a class="wl${find(name) ? '' : ' missing'}" contenteditable="false" data-n="${esc(name)}"${alias ? ` data-a="${esc(alias)}"` : ''}${head ? ` data-h="${esc(head)}"` : ''} title="Open ${esc(name)}">${esc((alias || name + (head || '')).trim())}</a>`;
function tidy(root) {
  root.querySelectorAll('script,style,iframe,object,embed,meta,link,title,svg,form,button,select,textarea').forEach(e => e.remove());
  root.querySelectorAll('*').forEach(el => [...el.attributes].forEach(at => {
    const n = at.name, t = el.tagName;
    const keep = (n === 'class' && (el.matches('a.wl') || el.matches('ul.tasks') || (t === 'CODE' && /^language-/.test(at.value))))
      || (t === 'A' && ['href', 'data-n', 'data-a', 'data-h', 'contenteditable', 'title'].includes(n))
      || (t === 'IMG' && ['src', 'alt', 'data-path'].includes(n)) || (t === 'INPUT' && ['type', 'checked', 'contenteditable'].includes(n));
    if (!keep || /^on/i.test(n) || /^\s*javascript:/i.test(at.value)) el.removeAttribute(n);
  }));
  root.querySelectorAll('input:not([type=checkbox])').forEach(e => e.remove());
  root.querySelectorAll('font,span,o\\:p').forEach(el => el.replaceWith(...el.childNodes));
  root.querySelectorAll('img').forEach(img => {
    const p = img.dataset.path || img.getAttribute('src') || '';
    if (!/^(https?:|data:|file:)/i.test(p)) { img.dataset.path = decodeURI(p).replace(/^\.?\//, ''); img.src = BB.vault + '/' + encodeURI(img.dataset.path); }
  });
  root.querySelectorAll('li').forEach(li => {  // "[ ] task" -> a real tick box
    if (li.querySelector(':scope > input[type=checkbox]')) return;
    const first = el => [...el.childNodes].find(n => n.nodeType !== 3 || n.textContent.trim());
    let t = first(li); if (t && t.nodeType === 1 && t.tagName === 'P') t = first(t);
    if (!t || t.nodeType !== 3) return;
    const m = t.textContent.match(/^\s*\[( |x|X)\]\s?/); if (!m) return;
    t.textContent = t.textContent.slice(m[0].length);
    li.insertAdjacentHTML('afterbegin', `<input type="checkbox" contenteditable="false"${m[1] !== ' ' ? ' checked' : ''}>`);
    li.parentElement.classList.add('tasks');
  });
  boxes();
  root.querySelectorAll('a.wl').forEach(a => { a.contentEditable = 'false'; a.classList.toggle('missing', !find(a.dataset.n)); });
  flatten(root);
}
// Edge/Chromium puts a new list, table or paragraph *inside* the <p> the caret was in,
// which is invalid and loses the content on save. Lift those blocks back out to the top level.
const BLKRE = /^(UL|OL|P|DIV|BLOCKQUOTE|PRE|TABLE|HR|H[1-6])$/;
const INNER = ':scope > ul, :scope > ol, :scope > p, :scope > div, :scope > blockquote, :scope > pre, :scope > table, :scope > hr, :scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > h6';
function flatten(root) {
  for (let pass = 0; pass < 40; pass++) {
    const p = [...root.querySelectorAll('p')].find(el => el.querySelector(INNER));
    if (!p) return;
    const parts = []; let run = null;
    [...p.childNodes].forEach(n => {
      if (n.nodeType === 1 && BLKRE.test(n.tagName)) { run = null; parts.push({el: n, blk: true}); }
      else { if (!run) { run = document.createElement('p'); parts.push({el: run, blk: false}); } run.appendChild(n); }
    });
    p.replaceWith(...parts.filter(x => x.blk || x.el.textContent.trim() || x.el.querySelector('img,a,br')).map(x => x.el));
  }
}
// the outermost block the caret sits in, i.e. a direct child of the editor
const hostBlock = el => { let n = el; while (n && n.parentElement && n.parentElement !== ED) n = n.parentElement; return n && n.parentElement === ED ? n : null; };
const addBox = li => li.insertAdjacentHTML('afterbegin', '<input type="checkbox" contenteditable="false">');
function boxes() {
  ED.querySelectorAll('ul.tasks').forEach(ul => {
    const items = [...ul.children].filter(li => li.tagName === 'LI');
    if (!items.some(li => li.querySelector(':scope > input[type=checkbox]'))) return;
    items.forEach(li => { if (!li.querySelector(':scope > input[type=checkbox]') && !li.textContent.trim()) addBox(li); });
  });
}
async function toEditor(md) {
  ED.innerHTML = (await api().nb_render(prep(md))) || '';
  tidy(ED);
  if (!ED.innerHTML.trim()) ED.innerHTML = '<p><br></p>';
}

// ================= editor -> Markdown =================
const escText = s => s.replace(/\u00a0/g, ' ').replace(/([\\`*_\[\]])/g, '\\$1').replace(/</g, '&lt;');
const around = (m, s) => { const t = s.trim(); if (!t) return s; return s.match(/^\s*/)[0] + m + t + m + s.match(/\s*$/)[0]; };
const guard = s => s.split('\n').map(l => l.replace(/^(\s*)(#{1,6}\s|>|[-+*]\s|---)/, '$1\\$2').replace(/^(\s*\d+)\.(\s)/, '$1\\.$2')).join('\n');
function inline(node, oneLine) {
  let out = '';
  node.childNodes.forEach(n => {
    if (n.nodeType === 3) { out += escText(n.textContent.replace(/\s*\n\s*/g, ' ')); return; }  // a newline in HTML is just a space
    if (n.nodeType !== 1) return;
    const t = n.tagName, inner = () => inline(n, oneLine);
    if (t === 'B' || t === 'STRONG') out += around('**', inner());
    else if (t === 'I' || t === 'EM') out += around('*', inner());
    else if (t === 'U') { const s = inner(); out += s.trim() ? `<u>${s}</u>` : s; }
    else if (t === 'S' || t === 'DEL' || t === 'STRIKE') out += around('~~', inner());
    else if (t === 'MARK') out += around('==', inner());
    else if (t === 'CODE') out += n.textContent ? '`' + n.textContent + '`' : '';
    else if (t === 'BR') out += oneLine ? ' ' : '\n';
    else if (t === 'IMG') out += `![${(n.alt || '').replace(/[\[\]]/g, '')}](${encodeURI(decodeURI(n.dataset.path || n.getAttribute('src') || ''))})`;
    else if (t === 'A' && n.classList.contains('wl')) out += `[[${n.dataset.n}${n.dataset.h || ''}${n.dataset.a ? '|' + n.dataset.a : ''}]]`;
    else if (t === 'A') { const s = inner(); out += n.getAttribute('href') ? `[${s}](${n.getAttribute('href')})` : s; }
    else if (t === 'UL' || t === 'OL' || t === 'INPUT') {}
    else out += inner();
  });
  return out.replace(/ *\n */g, '\n');
}
function list(el, depth) {
  const ordered = el.tagName === 'OL'; let i = 1;
  return [...el.children].map(c => {
    if (c.tagName === 'UL' || c.tagName === 'OL') return list(c, depth + 1);  // Chrome nests lists straight in lists when indenting
    if (c.tagName !== 'LI') return '';
    const cb = c.querySelector(':scope > input[type=checkbox]');
    const box = cb ? (cb.checked ? '[x] ' : '[ ] ') : '';
    const subs = [...c.children].filter(x => x.tagName === 'UL' || x.tagName === 'OL').map(x => list(x, depth + 1));
    const pad = '    '.repeat(depth), mark = ordered ? (i++) + '.' : '-';
    const body = inline(c, false).trim().replace(/\n/g, '\n' + pad + ' '.repeat(mark.length + 1));
    return pad + mark + ' ' + box + body + (subs.length ? '\n' + subs.join('\n') : '');
  }).filter(Boolean).join('\n');
}
function table(t) {
  const rows = [...t.querySelectorAll('tr')].map(tr => [...tr.children].map(c => inline(c, true).trim().replace(/\|/g, '\\|') || ' '));
  if (!rows.length) return '';
  const w = Math.max(...rows.map(r => r.length)); rows.forEach(r => { while (r.length < w) r.push(' '); });
  return [rows[0], Array(w).fill('---'), ...rows.slice(1)].map(r => '| ' + r.join(' | ') + ' |').join('\n');
}
function blocks(root) {
  const out = [], kind = []; let loose = '';
  const flush = () => { if (loose.trim()) out.push(guard(loose.trim())); loose = ''; };
  root.childNodes.forEach(n => {
    if (n.nodeType === 3) { loose += escText(n.textContent.replace(/\s*\n\s*/g, ' ')); return; }
    if (n.nodeType !== 1) return;
    const t = n.tagName;
    if (/^(B|STRONG|I|EM|U|S|DEL|STRIKE|MARK|CODE|A|IMG|SPAN|BR|SUB|SUP|SMALL)$/.test(t)) { const d = document.createElement('div'); d.appendChild(n.cloneNode(true)); loose += inline(d); return; }
    flush();
    if (t === 'P' || t === 'DIV') { const s = inline(n).trim(); if (s) out.push(guard(s)); }
    else if (/^H[1-6]$/.test(t)) { const s = inline(n, true).trim(); if (s) out.push('#'.repeat(Math.min(+t[1], 4)) + ' ' + s); }
    else if (t === 'UL' || t === 'OL') { const s = list(n, 0); if (s) { out.push(s); kind[out.length - 1] = t; } }
    else if (t === 'BLOCKQUOTE') { const s = blocks(n); if (s.trim()) out.push(s.split('\n').map(l => '> ' + l).join('\n')); }
    else if (t === 'PRE') { const c = n.querySelector('code'), lang = (c && (c.className.match(/language-(\S+)/) || [])[1]) || ''; out.push('```' + lang + '\n' + n.textContent.replace(/\n$/, '') + '\n```'); }
    else if (t === 'HR') out.push('---');
    else if (t === 'TABLE') out.push(table(n));
    else { const s = blocks(n); if (s.trim()) out.push(s); }
  });
  flush();
  return out.map((b, i) => (i === 0 ? '' : kind[i] && kind[i] === kind[i - 1] ? '\n' : '\n\n') + b).join('');
}
// the markdown is built from a copy, so saving never disturbs a caret sitting in a fresh highlight
const toMd = () => { const c = ED.cloneNode(true); dropEmpty(c); return blocks(c).replace(/\n{3,}/g, '\n\n').trim() + '\n'; };

// ================= list, search and tags =================
function drawList() {
  const q = $('.nb-q').value.trim().toLowerCase();
  let list = notes.filter(n => (!q || n.name.toLowerCase().includes(q) || n.text.toLowerCase().includes(q)) && (!filterTag || tagsOf(n.text).includes(filterTag)));
  list.sort((a, b) => (pins.includes(b.name) - pins.includes(a.name)) || b.mtime - a.mtime);
  const mark = s => q ? esc(s).replace(new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`) : esc(s);
  const plain = s => s.replace(/\[\[([^\]|]*)(?:\|([^\]]*))?\]\]/g, (m, a, b) => b || a).replace(/[#*_>`~=\[\]\\]/g, '');
  $('.nb-list').innerHTML = list.map(n => {
    let line = n.text.split('\n').find(l => l.trim() && !/^#{1,6}\s/.test(l)) || '';
    if (q) { const i = n.text.toLowerCase().indexOf(q); if (i >= 0) line = (i > 30 ? '…' : '') + n.text.slice(Math.max(0, i - 30), i + 60).replace(/\n/g, ' '); }
    const folder = n.name.includes('/') ? n.name.slice(0, n.name.lastIndexOf('/')) + ' · ' : '';
    return `<button class="nb-item${n.name === cur ? ' on' : ''}" role="listitem" data-n="${esc(n.name)}"${n.name === cur ? ' aria-current="true"' : ''}><b>${pins.includes(n.name) ? '<i class="pin" aria-label="Pinned"></i>' : ''}${mark(base(n.name))}</b>
      <small>${mark(plain(line).slice(0, 90))}</small><small class="when">${esc(folder)}${ago(n.mtime)}</small></button>`;
  }).join('') || `<p class="sub" style="padding:8px">${q || filterTag ? 'No notes match.' : 'No notes yet.'}</p>`;
  const counts = {}; notes.forEach(n => new Set(tagsOf(n.text)).forEach(t => counts[t] = (counts[t] || 0) + 1));
  $('.nb-tags').innerHTML = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 14)
    .map(([t, c]) => `<button class="nb-tag${t === filterTag ? ' on' : ''}" data-t="${esc(t)}" aria-pressed="${t === filterTag}">#${esc(t)} ${c}</button>`).join('');
  $('.nb-count').textContent = `${notes.length} note${notes.length === 1 ? '' : 's'}`;
}
$('.nb-q').oninput = drawList;
$('.nb-list').onclick = e => { const b = e.target.closest('.nb-item'); if (b) { open(b.dataset.n); if (innerWidth < 720) setPanel('side', false); } };
$('.nb-tags').onclick = e => { const b = e.target.closest('.nb-tag'); if (b) { filterTag = filterTag === b.dataset.t ? null : b.dataset.t; drawList(); } };

// ================= open, save, rename =================
async function load(openName) {
  const a = api(); if (!a) { ED.innerHTML = '<p>The notebook works inside the Blackboard Buddy app.</p>'; return; }
  notes = await a.nb_list();
  drawList();
  const want = openName || cur || store('nbLast');
  await open(find(want) ? find(want).name : (notes.find(n => base(n.name) === 'Start here') || notes[0] || {}).name, true);
}
async function open(name, keepScroll) {
  if (!name) return;
  await flush();
  const n = find(name); if (!n) return;
  const same = cur === n.name; cur = n.name; store('nbLast', cur);
  TITLE.value = base(cur); TITLE.title = 'Click to rename: ' + cur;
  SRC.value = n.text;
  const top = $('.nb-scroll').scrollTop;
  await toEditor(n.text);
  $('.nb-scroll').scrollTop = same && keepScroll ? top : 0;
  status('Saved ' + ago(n.mtime));
  drawList(); info(); words();
  $('.nb-pin').setAttribute('aria-pressed', pins.includes(cur));
}
function changed() {
  if (!note()) return;
  status('Saving…', true); words();
  clearTimeout(saveT); saveT = setTimeout(save, 600);
}
async function save() {
  clearTimeout(saveT); saveT = 0;
  const n = note(), a = api(); if (!n || !a) return;
  n.text = raw ? SRC.value : toMd();
  const r = await a.nb_save(n.name, n.text, null);
  if (r.error) return status(r.error);
  n.mtime = r.mtime; status('Saved'); drawList(); info();
  if (!tracked.has(n.name)) { tracked.add(n.name); a.track('note'); }
}
async function flush() { if (saveT) await save(); }
async function rename() {
  const n = note(), a = api(), want = TITLE.value.trim();
  if (!n || !a || !want || want === base(n.name)) { TITLE.value = n ? base(n.name) : ''; return; }
  await flush();
  const full = n.name.includes('/') && !want.includes('/') ? n.name.slice(0, n.name.lastIndexOf('/') + 1) + want : want;
  const r = await a.nb_save(full, n.text, n.name);
  if (r.error) { status(r.error); TITLE.value = base(n.name); return; }
  pins = pins.map(p => p === n.name ? r.name : p); store('nbPins', JSON.stringify(pins));
  status('Renamed'); await load(r.name);
}
TITLE.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); ED.focus(); } if (e.key === 'Escape') { TITLE.value = base(cur || ''); ED.focus(); } };
TITLE.onchange = rename;

// ================= new notes, templates, daily note =================
const today = () => new Date().toLocaleDateString('en-CA');
const TEMPLATES = {
  blank: () => '',
  lecture: () => `**Module:** \n**Week:** \n**Date:** ${today()}\n\n## Key points\n\n- \n\n## Details\n\n\n## Questions to follow up\n\n- [ ] \n\n#lecture\n`,
  assignment: () => `**Module:** \n**Due:** \n\n## Brief\n\n\n## Plan\n\n- [ ] Read the brief and marking criteria\n- [ ] Research\n- [ ] Draft\n- [ ] Check against the criteria\n- [ ] Submit\n\n## Sources\n\n- \n\n#assignment\n`,
  meeting: () => `**Date:** ${today()}\n**Who:** \n\n## Agenda\n\n- \n\n## Notes\n\n\n## Actions\n\n- [ ] \n\n#meeting\n`,
};
async function create(name, text, folder) {
  const a = api(); if (!a) return;
  let n = name || 'Untitled', i = 2;
  const full = s => (folder ? folder + '/' : '') + s;
  if (!name) while (find(full(n))) n = `Untitled ${i++}`;
  if (!find(full(n))) {
    const r = await a.nb_save(full(n), text || '', null);
    if (r.error) return status(r.error);
    notes.push({name: r.name, text: text || '', mtime: r.mtime});
  }
  await open(full(n));
  if (!name) { TITLE.focus(); TITLE.select(); status('Type a name, then press Enter'); } else ED.focus();
}
const MENU = $('.nb-menu');
$('.nb-new').onclick = e => { e.stopPropagation(); MENU.hidden = !MENU.hidden; if (!MENU.hidden) MENU.querySelector('button').focus(); };
MENU.onclick = e => { const b = e.target.closest('button'); if (!b) return; MENU.hidden = true; create(null, TEMPLATES[b.dataset.t]()); };
MENU.onkeydown = e => {
  const items = [...MENU.querySelectorAll('button')], i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length].focus(); }
  if (e.key === 'Escape') { MENU.hidden = true; $('.nb-new').focus(); }
};
document.addEventListener('click', e => { if (!e.target.closest('.nb-newwrap')) MENU.hidden = true;
  if (!e.target.closest('.nb-tpick,[data-a=tools]')) closeTools(); if (!e.target.closest('.nb-ac,.nb-tools') && AC.classList.contains('picker')) hideAc(); });
$('.nb-today').onclick = () => create(today(), `# ${new Date().toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long'})}\n\n## To do\n\n- [ ] \n\n## Notes\n\n\n#daily\n`, 'Daily');
$('.nb-folder').onclick = () => api() && api().nb_open_folder();

// ================= toolbar =================
// holding the caret where it is: without this the editor blurs and loses its pending style
$('.nb-tools').addEventListener('mousedown', e => { if (e.target.closest('button')) e.preventDefault(); });
document.addEventListener('selectionchange', () => {
  const s = getSelection();
  if (s.rangeCount && ED.contains(s.anchorNode)) { lastRange = s.getRangeAt(0).cloneRange(); sync(); }
});
// Re-setting the selection wipes the pending style Chromium keeps on an empty caret, which
// is what stops Bold switching off again. So leave a selection that is already in the editor alone.
function restore() {
  const sel = getSelection();
  if (document.activeElement === ED && sel.rangeCount && ED.contains(sel.anchorNode)) return;
  if (document.activeElement !== ED) ED.focus();
  if (lastRange && ED.contains(lastRange.startContainer)) { sel.removeAllRanges(); sel.addRange(lastRange); }
}
const here = () => { const s = getSelection(); let n = s.anchorNode; if (!n || !ED.contains(n)) return null; return n.nodeType === 1 ? n : n.parentElement; };
function sync() {
  NB.querySelectorAll('.nb-tools [data-c]').forEach(b => {
    if (!/bold|italic|underline|strikeThrough|List/.test(b.dataset.c)) return;
    let on = false; try { on = document.queryCommandState(b.dataset.c); } catch (_) {}
    b.setAttribute('aria-pressed', on);
  });
  const el = here(); if (!el) return;
  $('[data-a=mark]').setAttribute('aria-pressed', !!el.closest('mark'));
  $('[data-a=code]').setAttribute('aria-pressed', !!el.closest('code') && !el.closest('pre'));
  $('[data-a=task]').setAttribute('aria-pressed', !!el.closest('ul.tasks'));
  $('.nb-style').value = el.closest('pre') ? 'pre' : el.closest('h1') ? 'h1' : el.closest('h2') ? 'h2' : el.closest('h3,h4') ? 'h3' : el.closest('blockquote') ? 'blockquote' : 'p';
  $('.nb-tbl').hidden = !el.closest('td,th');
}
// Bold and the rest keep a pending state on the caret. Rebuilding the DOM straight after
// throws that state away, so only the commands that change block structure get tidied up.
const INLINE_CMD = /^(bold|italic|underline|strikeThrough)$/;
const cmd = (c, v = null) => {
  restore();
  document.execCommand(c, false, v);
  if (!INLINE_CMD.test(c)) { flatten(ED); boxes(); }
  changed(); sync();
};
// Highlight and Code work like Bold: press once to switch on, press again to switch off,
// with or without anything selected. ZWSP is the empty marker that holds the caret inside
// a brand new tag; dropEmpty() clears those away again before the note is saved.
const ZWSP = '​';
function wrapInline(tag) {
  restore();
  const s = getSelection(), el = here(), inside = el && el.closest(tag);
  if (inside && ED.contains(inside)) {
    if (s.isCollapsed) {                        // switch off: step out and carry on in plain text
      const after = document.createTextNode(ZWSP);
      inside.after(after);
      const r = document.createRange(); r.setStart(after, 1); r.collapse(true);
      s.removeAllRanges(); s.addRange(r); lastRange = r.cloneRange();
    } else inside.replaceWith(...inside.childNodes);
  } else if (!s.rangeCount) {
    return;
  } else if (s.isCollapsed) {                   // switch on: whatever you type next is formatted
    const w = document.createElement(tag);
    w.appendChild(document.createTextNode(ZWSP));
    s.getRangeAt(0).insertNode(w);
    const r = document.createRange(); r.setStart(w.firstChild, 1); r.collapse(true);
    s.removeAllRanges(); s.addRange(r); lastRange = r.cloneRange();
  } else {
    const r = s.getRangeAt(0), w = document.createElement(tag); w.appendChild(r.extractContents()); r.insertNode(w);
    const nr = document.createRange(); nr.selectNodeContents(w); s.removeAllRanges(); s.addRange(nr);
  }
  changed(); sync();
}

// the caret markers are an editing aid only, so they never reach the saved file
function dropEmpty(root) {
  root.querySelectorAll('mark,code').forEach(el => {
    if (el.closest('pre')) return;
    if (!el.textContent.replace(/​/g, '').trim()) el.remove();
  });
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const hits = [];
  for (let n = walk.nextNode(); n; n = walk.nextNode()) if (n.nodeValue.includes(ZWSP)) hits.push(n);
  for (const n of hits) n.nodeValue = n.nodeValue.replace(/​/g, '');
}
function checklist() {
  restore();
  const el = here(); if (!el) return;
  const li = el.closest('li');
  if (li) {  // already a list item: switch this list between bullets and tick boxes
    let ul = li.parentElement;
    if (ul.tagName === 'OL') { const nu = document.createElement('ul'); nu.append(...ul.childNodes); ul.replaceWith(nu); ul = nu; }
    if (ul.classList.contains('tasks')) { ul.classList.remove('tasks'); ul.querySelectorAll(':scope > li > input[type=checkbox]').forEach(c => c.remove()); }
    else { ul.classList.add('tasks'); [...ul.children].forEach(x => { if (x.tagName === 'LI' && !x.querySelector(':scope > input[type=checkbox]')) addBox(x); }); }
    changed(); sync(); return;
  }
  // a plain line: wrap it in a checklist of its own (execCommand would merge it into the list above)
  const blk = el.closest('p,div,h1,h2,h3,h4,h5,h6,blockquote');
  if (!blk || blk === ED) return;
  const ul = document.createElement('ul'); ul.className = 'tasks';
  const nli = document.createElement('li');
  const kids = [...blk.childNodes];
  if (!(kids.length === 1 && kids[0].nodeType === 1 && kids[0].tagName === 'BR')) nli.append(...kids);
  ul.appendChild(nli); blk.replaceWith(ul);
  addBox(nli); flatten(ED); changed();
  const box = nli.querySelector(':scope > input[type=checkbox]');
  const r = document.createRange(); box ? r.setStartAfter(box) : r.selectNodeContents(nli); r.collapse(true);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r); lastRange = r.cloneRange(); sync();
}
function insertHTML(html) { restore(); document.execCommand('insertHTML', false, html); tidy(ED); changed(); }
function insertBlocks(...nodes) {  // put real block elements after the caret's block, never inside it
  restore();
  const el = here(); if (!el) return false;
  const host = hostBlock(el);
  if (!host) ED.append(...nodes);
  else { host.after(...nodes); if (!host.textContent.trim() && !host.querySelector('img,a,table')) host.remove(); }
  flatten(ED); changed(); return true;
}
function caretIn(el) {
  if (!el) return;
  ED.focus();
  const r = document.createRange(); r.selectNodeContents(el); r.collapse(true);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r); lastRange = r.cloneRange(); sync();
}
const cell = tag => `<${tag}><br></${tag}>`;
const FS = () => +(store('nbFs') || 17);
function setFs(v) { v = Math.max(13, Math.min(28, v)); store('nbFs', v); NB.style.setProperty('--fs', v + 'px'); }
setFs(FS());
// ---- which buttons live on the toolbar; the choice is kept per computer
const TPICK = $('.nb-tpick'), TLIST = $('.nb-tpick-l');
const tkey = b => (b.dataset.c ? 'c:' + b.dataset.c : 'a:' + b.dataset.a);
const toolsOff = () => { try { return new Set(JSON.parse(store('nbTools') || '[]')); } catch (_) { return new Set(); } };
const pickable = () => [...NB.querySelectorAll('.nb-tools .grp:not(.nb-tbl) button')].filter(b => b.dataset.a !== 'tools');
function applyTools() {
  const off = toolsOff();
  pickable().forEach(b => b.hidden = off.has(tkey(b)));
  NB.querySelectorAll('.nb-tools .grp:not(.nb-tbl)').forEach(g => {
    g.hidden = ![...g.children].some(c => !c.hidden);
  });
}
function drawTools() {
  const off = toolsOff();
  TLIST.innerHTML = '';
  for (const b of pickable()) {
    const row = document.createElement('label'), box = document.createElement('input'), name = document.createElement('span');
    row.className = 'nb-tpick-i';
    box.type = 'checkbox'; box.checked = !off.has(tkey(b));
    name.textContent = (b.getAttribute('aria-label') || b.textContent.trim() || b.title || '').replace(/\s*\(.*\)\s*$/, '');
    box.onchange = () => {
      const set = toolsOff();
      box.checked ? set.delete(tkey(b)) : set.add(tkey(b));
      store('nbTools', JSON.stringify([...set]));
      applyTools();
    };
    row.append(box, name);
    TLIST.append(row);
  }
}
function closeTools() { TPICK.hidden = true; $('[data-a=tools]').setAttribute('aria-pressed', 'false'); }
$('.nb-tpick-r').onclick = () => { store('nbTools', '[]'); applyTools(); drawTools(); };
applyTools();

const ACTIONS = {
  tools: () => {
    const btn = $('[data-a=tools]');
    if (!TPICK.hidden) return closeTools();
    drawTools();
    const bar = NB.querySelector('.nb-tools');   // offsets are plain CSS pixels, so a page zoom can't skew them
    TPICK.style.top = (bar.offsetTop + bar.offsetHeight + 6) + 'px';
    TPICK.hidden = false; btn.setAttribute('aria-pressed', 'true');
  },
  mark: () => wrapInline('mark'), code: () => wrapInline('code'),
  clear: () => { restore(); document.execCommand('removeFormat'); const el = here(); el && el.closest('mark,code') && el.closest('mark,code').replaceWith(...el.closest('mark,code').childNodes); document.execCommand('formatBlock', false, 'p'); changed(); sync(); },
  task: checklist,
  note: () => openPicker(),
  web: () => openPicker('web'),
  img: () => $('.nb-file').click(),
  table: () => {
    const t = document.createElement('table');
    t.innerHTML = `<thead><tr>${cell('th').repeat(3)}</tr></thead><tbody><tr>${cell('td').repeat(3)}</tr><tr>${cell('td').repeat(3)}</tr></tbody>`;
    const p = document.createElement('p'); p.innerHTML = '<br>';
    if (insertBlocks(t, p)) caretIn(t.querySelector('th'));
  },
  hr: () => {
    const p = document.createElement('p'); p.innerHTML = '<br>';
    if (insertBlocks(document.createElement('hr'), p)) caretIn(p);
  },
  row: () => {
    const tr = here()?.closest('tr'); if (!tr) return;
    const nr = document.createElement('tr'); nr.innerHTML = cell('td').repeat(tr.children.length);
    if (tr.parentElement.tagName === 'THEAD') { const t = tr.closest('table'); (t.tBodies[0] || t.appendChild(document.createElement('tbody'))).prepend(nr); } else tr.after(nr);
    changed();
  },
  col: () => { const c = here()?.closest('td,th'); if (!c) return; const i = [...c.parentElement.children].indexOf(c);
    c.closest('table').querySelectorAll('tr').forEach(tr => { const ref = tr.children[i]; const n = document.createElement(ref && ref.tagName === 'TH' ? 'th' : 'td'); n.innerHTML = '<br>'; ref ? ref.after(n) : tr.appendChild(n); }); changed(); },
  delrow: () => { const tr = here()?.closest('tr'); if (!tr) return; const t = tr.closest('table'); tr.remove(); if (!t.querySelector('tr')) t.remove(); changed(); sync(); },
  smaller: () => setFs(FS() - 1), bigger: () => setFs(FS() + 1),
  wide: () => { NB.classList.toggle('wide'); store('nbWide', NB.classList.contains('wide') ? '1' : ''); $('[data-a=wide]').setAttribute('aria-pressed', NB.classList.contains('wide')); },
  raw: async () => {
    await flush();
    raw = !raw; NB.classList.toggle('raw', raw); SRC.hidden = !raw; $('[data-a=raw]').setAttribute('aria-pressed', raw);
    NB.querySelectorAll('.nb-tools .grp:not(:last-of-type) button, .nb-style').forEach(b => b.disabled = raw);
    if (raw) { SRC.value = note() ? note().text : ''; SRC.focus(); }
    else if (note()) { await toEditor(note().text); ED.focus(); }
  },
  help: () => open(find('Start here') ? 'Start here' : (notes[0] || {}).name),
};
$('.nb-tools').addEventListener('mousedown', e => { if (e.target.closest('button')) e.preventDefault(); });  // keep the text selected
$('.nb-tools').onclick = e => {
  const b = e.target.closest('button'); if (!b || b.disabled) return;
  if (b.dataset.c) cmd(b.dataset.c); else if (ACTIONS[b.dataset.a]) ACTIONS[b.dataset.a]();
};
$('.nb-style').onchange = e => { cmd('formatBlock', e.target.value); };
if (store('nbWide')) ACTIONS.wide();
// arrow keys move along the toolbar (one Tab stop for the whole toolbar)
$('.nb-tools').addEventListener('keydown', e => {
  if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
  const items = [...NB.querySelectorAll('.nb-tools button:not([hidden]):not(:disabled), .nb-style')].filter(b => b.offsetParent);
  const i = items.indexOf(document.activeElement); if (i < 0) return;
  e.preventDefault(); items[(i + (e.key === 'ArrowRight' ? 1 : -1) + items.length) % items.length].focus();
});

// ================= top bar =================
function setPanel(which, on) {
  NB.classList.toggle('no-' + which, !on); store('nbHide' + which, on ? '' : '1');
  $('.nb-toggle-' + which).setAttribute('aria-pressed', on);
}
$('.nb-toggle-side').onclick = () => setPanel('side', NB.classList.contains('no-side'));
$('.nb-toggle-info').onclick = () => setPanel('info', NB.classList.contains('no-info'));
setPanel('side', !store('nbHideside') && innerWidth >= 720); setPanel('info', !store('nbHideinfo'));
$('.nb-pin').onclick = () => { pins = pins.includes(cur) ? pins.filter(p => p !== cur) : [...pins, cur]; store('nbPins', JSON.stringify(pins)); $('.nb-pin').setAttribute('aria-pressed', pins.includes(cur)); drawList(); };
$('.nb-del').onclick = async () => {
  if (!cur || !confirm(`Move “${base(cur)}” to the trash?\n\nYou can get it back from the .trash folder inside your Notebook folder.`)) return;
  clearTimeout(saveT); saveT = 0; await api().nb_delete(cur);
  notes = notes.filter(n => n.name !== cur); cur = null; drawList();
  if (notes.length) open(notes.slice().sort((a, b) => b.mtime - a.mtime)[0].name);
  else { ED.innerHTML = '<p><br></p>'; TITLE.value = ''; info(); }
  status('Moved to trash');
};
const askBtn = document.querySelector('.ask');
if (!askBtn) $('.nb-askbtn').hidden = true;
$('.nb-askbtn').onclick = async () => {
  await flush();
  const box = document.querySelector('.chat-in'); if (!askBtn || !box) return;
  askBtn.click();
  box.value = `About my note “${base(cur)}”:\n\n${(note() || {text: ''}).text.slice(0, 6000)}\n\nMy question: `;
  box.focus(); box.setSelectionRange(box.value.length, box.value.length); box.dispatchEvent(new Event('input'));
};

// ================= typing: shortcuts, auto-formatting, links =================
const AUTO = [[/^#$/, () => cmd('formatBlock', 'h1')], [/^##$/, () => cmd('formatBlock', 'h2')], [/^###$/, () => cmd('formatBlock', 'h3')],
  [/^[-*•]$/, () => cmd('insertUnorderedList')], [/^1[.)]$/, () => cmd('insertOrderedList')], [/^\[ ?\]$/, checklist], [/^>$/, () => cmd('formatBlock', 'blockquote')]];
ED.addEventListener('keydown', e => {
  if (!AC.hidden && !AC.classList.contains('picker') && acKey(e)) return;
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (mod && !e.shiftKey && '0123'.includes(k) && k) { e.preventDefault(); cmd('formatBlock', k === '0' ? 'p' : 'h' + k); return; }
  if (mod && e.shiftKey && (e.code === 'Digit8' || e.code === 'Digit7')) { e.preventDefault(); cmd(e.code === 'Digit8' ? 'insertUnorderedList' : 'insertOrderedList'); return; }
  if (mod && e.shiftKey && e.code === 'Digit9') { e.preventDefault(); checklist(); return; }
  if (mod && e.shiftKey && k === 'h') { e.preventDefault(); wrapInline('mark'); return; }
  if (mod && k === 'k') { e.preventDefault(); openPicker(); return; }
  if (mod && (k === 'b' || k === 'i' || k === 'u')) setTimeout(() => { changed(); sync(); });
  const el = here();
  if (e.key === 'Tab') {
    const td = el && el.closest('td,th');
    if (td) {
      e.preventDefault();
      const cells = [...td.closest('table').querySelectorAll('th,td')]; let i = cells.indexOf(td) + (e.shiftKey ? -1 : 1);
      if (i >= cells.length) { ACTIONS.row(); i = cells.length; }
      const target = [...td.closest('table').querySelectorAll('th,td')][Math.max(0, i)];
      const r = document.createRange(); r.selectNodeContents(target); r.collapse(false); getSelection().removeAllRanges(); getSelection().addRange(r);
      return;
    }
    if (el && el.closest('li')) { e.preventDefault(); cmd(e.shiftKey ? 'outdent' : 'indent'); return; }
    return;  // anywhere else, Tab moves to the next control as normal
  }
  if (e.key === ' ' && el) {
    const blk = el.closest('p,div,h1,h2,h3,h4,li');
    if (blk && blk !== ED && !blk.closest('li') && !blk.closest('pre')) {
      const text = blk.textContent;
      const hit = AUTO.find(([re]) => re.test(text));
      if (hit && getSelection().anchorOffset === text.length) {
        e.preventDefault(); blk.innerHTML = '<br>';
        const r = document.createRange(); r.setStart(blk, 0); r.collapse(true); getSelection().removeAllRanges(); getSelection().addRange(r); lastRange = r.cloneRange();
        hit[1](); return;
      }
    }
  }
  if (e.key === 'Enter' && !e.shiftKey && el) {
    const li = el.closest('li');
    if (li && !li.textContent.trim() && !li.querySelector('img,ul,ol')) {  // an empty item: step back out of the list
      e.preventDefault();
      const list = li.parentElement, p = document.createElement('p'); p.innerHTML = '<br>';
      let outer = list; while (outer.parentElement && outer.parentElement !== ED) outer = outer.parentElement;
      li.remove(); outer.after(p);
      if (!list.querySelector('li')) list.remove();
      flatten(ED); caretIn(p); changed(); return;
    }
    const blk = el.closest('p,div');
    if (blk && blk !== ED && /^(-{3,}|_{3,}|\*{3,})$/.test(blk.textContent.trim())) {
      e.preventDefault(); const p = document.createElement('p'); p.innerHTML = '<br>'; blk.replaceWith(document.createElement('hr'), p);
      const r = document.createRange(); r.setStart(p, 0); getSelection().removeAllRanges(); getSelection().addRange(r); changed(); return;
    }
  }
});
ED.addEventListener('input', () => { flatten(ED); boxes(); changed(); acCheck(); });
ED.addEventListener('paste', e => {
  const files = [...e.clipboardData.files].filter(f => f.type.startsWith('image/'));
  if (files.length) { e.preventDefault(); addImages(files); return; }
  setTimeout(() => { tidy(ED); changed(); });  // strip Word/web styling from pasted text
});
ED.addEventListener('click', e => {
  const wl = e.target.closest('a.wl');
  if (wl) { e.preventDefault(); find(wl.dataset.n) ? open(wl.dataset.n) : create(wl.dataset.n, ''); return; }
  const a = e.target.closest('a[href]');
  if (a && (e.ctrlKey || e.metaKey)) { e.preventDefault(); window.open(a.href, '_blank'); return; }
  const cb = e.target.closest('input[type=checkbox]');
  if (cb) { cb.toggleAttribute('checked', cb.checked); changed(); }
});
SRC.addEventListener('input', changed);
ED.addEventListener('blur', () => setTimeout(() => { if (!NB.contains(document.activeElement) || document.activeElement === TITLE) flush(); }, 200));

// ---- [[ autocomplete while typing, and the "Link note" picker
let acIdx = 0, acItems = [], acCtx = null;
function caretRect() {
  const s = getSelection(); if (!s.rangeCount) return null;
  const r = s.getRangeAt(0).cloneRange(); r.collapse(true);
  let rect = r.getBoundingClientRect();
  if (!rect.width && !rect.height) { const el = here(); rect = el ? el.getBoundingClientRect() : ED.getBoundingClientRect(); }
  return rect;
}
function place(rect) {
  const m = $('.nb-main').getBoundingClientRect();
  AC.style.left = Math.max(8, Math.min(rect.left - m.left, m.width - 290)) + 'px';
  AC.style.top = Math.min(rect.bottom - m.top + 6, m.height - 320) + 'px';
}
function acCheck() {
  const s = getSelection();
  if (!s.rangeCount || !s.isCollapsed || s.anchorNode.nodeType !== 3) return hideAc();
  const m = s.anchorNode.textContent.slice(0, s.anchorOffset).match(/\[\[([^\]\[\n|#]*)$/);
  if (!m) return hideAc();
  acCtx = {node: s.anchorNode, start: s.anchorOffset - m[0].length, end: s.anchorOffset};
  AC.classList.remove('picker'); fill(m[1]); if (acItems.length) { place(caretRect()); AC.hidden = false; } else hideAc();
}
function openPicker(mode = 'note') {
  restore();
  const s = getSelection();
  acCtx = {range: s.rangeCount ? s.getRangeAt(0).cloneRange() : null, alias: s.toString().trim(), mode};
  AC.classList.add('picker'); ACQ.value = '';
  ACQ.placeholder = mode === 'web' ? 'Paste a web address, then press Enter' : 'Find a note to link';
  AC.setAttribute('aria-label', mode === 'web' ? 'Add a web link' : 'Link a note');
  if (mode === 'web') ACL.innerHTML = '<p class="none" style="padding:6px 10px">Press Enter to add the link, Esc to cancel.</p>'; else fill('');
  place(caretRect() || ED.getBoundingClientRect()); AC.hidden = false; ACQ.focus();
}
function insertWeb(url) {
  url = url.trim(); if (!url) return;
  if (!/^(https?:|mailto:)/i.test(url)) url = 'https://' + url;
  const ctx = acCtx; hideAc(); ED.focus();
  if (ctx.range) { const s = getSelection(); s.removeAllRanges(); s.addRange(ctx.range); }
  if (getSelection().isCollapsed) document.execCommand('insertHTML', false, `<a href="${esc(url)}">${esc(url)}</a>&nbsp;`);
  else document.execCommand('createLink', false, url);
  tidy(ED); changed();
}
function fill(q) {
  q = q.toLowerCase();
  acItems = notes.filter(n => n.name !== cur && n.name.toLowerCase().includes(q))
    .sort((a, b) => (base(b.name).toLowerCase().startsWith(q) - base(a.name).toLowerCase().startsWith(q)) || b.mtime - a.mtime).slice(0, 8).map(n => n.name);
  if (q.trim() && !find(q)) acItems.push('\u0000' + q.trim());
  acIdx = 0; drawAc();
}
function drawAc() {
  ACL.innerHTML = acItems.map((n, i) => `<button role="option" aria-selected="${i === acIdx}" class="${i === acIdx ? 'on' : ''}" data-i="${i}">${n[0] === '\u0000' ? '+ New note “' + esc(n.slice(1)) + '”' : esc(base(n)) + (n.includes('/') ? `<small>${esc(n)}</small>` : '')}</button>`).join('')
    || '<p class="none" style="padding:6px 10px">Type a name to make a new note.</p>';
}
function hideAc() { AC.hidden = true; AC.classList.remove('picker'); }
function acPick(i) {
  const name = (acItems[i] || '').replace('\u0000', ''); if (!name) return;
  const ctx = acCtx; hideAc(); ED.focus();
  const s = getSelection(), r = document.createRange();
  if (ctx.node) {
    const after = ctx.node.textContent.slice(ctx.end, ctx.end + 2) === ']]' ? 2 : 0;
    r.setStart(ctx.node, ctx.start); r.setEnd(ctx.node, ctx.end + after);
  } else if (ctx.range) { r.setStart(ctx.range.startContainer, ctx.range.startOffset); r.setEnd(ctx.range.endContainer, ctx.range.endOffset); }
  s.removeAllRanges(); s.addRange(r);
  document.execCommand('insertHTML', false, chip(name, ctx.alias && ctx.alias !== name ? ctx.alias : '') + '&nbsp;');
  tidy(ED); changed();
}
function acKey(e) {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); acIdx = (acIdx + (e.key === 'ArrowDown' ? 1 : -1) + acItems.length) % Math.max(1, acItems.length); drawAc(); return true; }
  if (e.key === 'Enter' || e.key === 'Tab') { if (!acItems.length) return false; e.preventDefault(); acPick(acIdx); return true; }
  if (e.key === 'Escape') { e.preventDefault(); hideAc(); ED.focus(); return true; }
  return false;
}
ACQ.oninput = () => { if (acCtx && acCtx.mode !== 'web') fill(ACQ.value); };
ACQ.onkeydown = e => {
  if (acCtx && acCtx.mode === 'web') { if (e.key === 'Enter') { e.preventDefault(); insertWeb(ACQ.value); } else if (e.key === 'Escape') { e.preventDefault(); hideAc(); ED.focus(); } return; }
  acKey(e);
};
ACL.onmousedown = e => { e.preventDefault(); const b = e.target.closest('[data-i]'); if (b) acPick(+b.dataset.i); };

// ---- pictures: paste, drop or the Picture button
async function addImages(files) {
  const a = api(); if (!a) return;
  for (const f of files) {
    if (!f.type.startsWith('image/')) continue;
    status('Adding picture…', true);
    const url = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(f); });
    const res = await a.nb_image(url, f.name || 'pasted');
    if (res.error) { status(res.error); continue; }
    insertHTML(`<img src="${BB.vault}/${encodeURI(res.path)}" data-path="${esc(res.path)}" alt="${esc((f.name || 'picture').replace(/\.\w+$/, ''))}"><p><br></p>`);
  }
}
$('.nb-file').onchange = e => { addImages([...e.target.files]); e.target.value = ''; };
NB.addEventListener('dragover', e => { if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); NB.classList.add('drop'); } });
NB.addEventListener('dragleave', e => { if (!NB.contains(e.relatedTarget)) NB.classList.remove('drop'); });
NB.addEventListener('drop', e => {
  NB.classList.remove('drop'); if (!e.dataTransfer.files.length) return; e.preventDefault();
  const r = document.caretRangeFromPoint && document.caretRangeFromPoint(e.clientX, e.clientY); if (r && ED.contains(r.startContainer)) lastRange = r;
  addImages([...e.dataTransfer.files]);
});

// ================= right panel: links, outline, mentions =================
function lk(name, sub, missing) { return `<button class="lk${missing ? ' missing' : ''}" data-n="${esc(name)}">${esc(base(name))}${sub ? `<small>${esc(sub)}</small>` : ''}</button>`; }
function info() {
  const n = note();
  if (!n) { ['.nb-back', '.nb-out', '.nb-outline', '.nb-ment'].forEach(s => $(s).innerHTML = ''); return; }
  const me = [n.name.toLowerCase(), base(n.name).toLowerCase()];
  const clean = l => l.replace(/\[\[([^\]|]*)(?:\|([^\]]*))?\]\]/g, (m, a, b) => b || a).replace(/[#*_>`~=\\]/g, '').trim();
  const back = notes.filter(x => x !== n && linksOf(x.text).some(l => me.includes(l.toLowerCase())));
  $('.nb-back').innerHTML = back.map(x => lk(x.name, clean(x.text.split('\n').find(l => me.some(m => l.toLowerCase().includes('[[' + m))) || ''))).join('')
    || '<p class="none">No other notes link here yet.</p>';
  const out = [...new Set(linksOf(n.text))];
  $('.nb-out').innerHTML = out.map(l => lk(find(l) ? find(l).name : l, find(l) ? '' : 'Not made yet. Click to make it', !find(l))).join('') || '<p class="none">Use Link note to connect another note.</p>';
  const heads = [...ED.querySelectorAll('h1,h2,h3,h4')];
  $('.nb-outline').innerHTML = heads.map((h, i) => `<button class="lk" data-h="${i}" style="padding-left:${(+h.tagName[1] - 1) * 12 + 9}px">${esc(h.textContent)}</button>`).join('') || '<p class="none">Headings show up here.</p>';
  const word = base(n.name).toLowerCase();
  const ment = word.length > 2 ? notes.filter(x => x !== n && !back.includes(x) && codeless(x.text).toLowerCase().includes(word)) : [];
  $('.nb-ment').innerHTML = ment.slice(0, 8).map(x => `<div style="display:flex;align-items:center">${lk(x.name)}<button class="ghost" data-link="${esc(x.name)}" title="Turn the first mention into a link">Link</button></div>`).join('')
    || '<p class="none">None.</p>';
}
$('.nb-info').onclick = async e => {
  const L = e.target.closest('[data-link]');
  if (L) {
    const x = find(L.dataset.link), word = base(cur), re = new RegExp('(?<!\\[\\[)\\b' + word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'i');
    x.text = x.text.replace(re, m => `[[${cur}${m === word ? '' : '|' + m}]]`);
    const r = await api().nb_save(x.name, x.text, null); x.mtime = r.mtime; info(); drawList(); status('Linked'); return;
  }
  const h = e.target.closest('[data-h]');
  if (h) { const el = ED.querySelectorAll('h1,h2,h3,h4')[+h.dataset.h]; if (el) { el.scrollIntoView({behavior: 'smooth', block: 'start'}); const r = document.createRange(); r.selectNodeContents(el); r.collapse(false); ED.focus(); getSelection().removeAllRanges(); getSelection().addRange(r); } return; }
  const b = e.target.closest('.lk[data-n]'); if (b) find(b.dataset.n) ? open(b.dataset.n) : create(b.dataset.n, '');
};
function words() {
  const t = (raw ? SRC.value : ED.innerText).trim(), w = t ? t.split(/\s+/).length : 0;
  $('.nb-words').textContent = `${w} word${w === 1 ? '' : 's'} · ${Math.max(1, Math.round(w / 220))} min read`;
}

// ================= graph: every note a dot, every link a line =================
const G = $('.nb-graph'), CV = G.querySelector('canvas'), ctx = CV.getContext('2d');
let gNodes = [], gEdges = [], gScope = store('nbGraph') || 'all', cam = {x: 0, y: 0, k: 1}, drag = null, hover = null, alpha = 0, raf = 0;
function buildGraph() {
  const map = new Map(), edges = [];
  const add = (name, ghost) => { const k = name.toLowerCase(); if (!map.has(k)) map.set(k, {name, ghost, x: (Math.random() - .5) * 300, y: (Math.random() - .5) * 300, vx: 0, vy: 0, deg: 0}); return map.get(k); };
  notes.forEach(n => add(n.name, false));
  notes.forEach(n => linksOf(n.text).forEach(l => { const t = find(l); const b = add(t ? t.name : l, !t), a = map.get(n.name.toLowerCase()); if (a !== b) { edges.push([a, b]); a.deg++; b.deg++; } }));
  let nodes = [...map.values()];
  if (gScope === 'local' && cur) {
    const keep = new Set([map.get(cur.toLowerCase())]);
    for (let d = 0; d < 2; d++) edges.forEach(([a, b]) => { if (keep.has(a) || keep.has(b)) { keep.add(a); keep.add(b); } });
    nodes = nodes.filter(n => keep.has(n));
  }
  const set = new Set(nodes);
  gNodes = nodes; gEdges = edges.filter(([a, b]) => set.has(a) && set.has(b));
  cam = {x: 0, y: 0, k: 1}; alpha = 1;
}
function tick() {
  const N = gNodes;
  for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {
    const a = N[i], b = N[j]; let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy || 1; const f = 2600 / d2 * alpha, d = Math.sqrt(d2);
    dx /= d; dy /= d; a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
  }
  gEdges.forEach(([a, b]) => { const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, f = (d - 90) * .02 * alpha; a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f; });
  N.forEach(n => { if (n === drag?.node) return; n.vx -= n.x * .004 * alpha; n.vy -= n.y * .004 * alpha; n.vx *= .82; n.vy *= .82; n.x += n.vx; n.y += n.vy; });
  alpha = Math.max(alpha * .985, drag ? .3 : 0);
}
function draw() {
  const r = CV.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  if (CV.width !== Math.round(r.width * dpr)) { CV.width = r.width * dpr; CV.height = r.height * dpr; }
  const css = getComputedStyle(document.documentElement), fg = css.getPropertyValue('--text').trim(), mut = css.getPropertyValue('--muted').trim(), red = css.getPropertyValue('--red').trim();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, r.width, r.height);
  ctx.translate(r.width / 2 + cam.x, r.height / 2 + cam.y); ctx.scale(cam.k, cam.k);
  const near = new Set(); if (hover) gEdges.forEach(([a, b]) => { if (a === hover) near.add(b); if (b === hover) near.add(a); });
  ctx.lineWidth = 1 / cam.k;
  gEdges.forEach(([a, b]) => { ctx.strokeStyle = hover && (a === hover || b === hover) ? fg : mut; ctx.globalAlpha = hover && !(a === hover || b === hover) ? .12 : .45;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); });
  gNodes.forEach(n => {
    const me = cur && n.name.toLowerCase() === cur.toLowerCase(), rad = 5 + Math.min(8, Math.sqrt(n.deg) * 2);
    ctx.globalAlpha = n.ghost ? .4 : hover && n !== hover && !near.has(n) ? .25 : 1;
    ctx.fillStyle = me ? red : fg; ctx.beginPath(); ctx.arc(n.x, n.y, rad, 0, 7); ctx.fill();
    if (cam.k > .7 || me || n === hover || near.has(n) || gNodes.length < 25) {
      ctx.font = `${13 / Math.max(cam.k, .8)}px "Segoe UI", sans-serif`; ctx.textAlign = 'center'; ctx.fillStyle = n === hover || me ? fg : mut;
      ctx.fillText(base(n.name), n.x, n.y + rad + 15 / Math.max(cam.k, .8));
    }
  });
  ctx.globalAlpha = 1;
}
function loop() { if (G.hidden) return; if (alpha > .002) tick(); draw(); raf = requestAnimationFrame(loop); }
function at(e) { const r = CV.getBoundingClientRect(); return {x: (e.clientX - r.left - r.width / 2 - cam.x) / cam.k, y: (e.clientY - r.top - r.height / 2 - cam.y) / cam.k}; }
function hit(p) { return gNodes.find(n => Math.hypot(n.x - p.x, n.y - p.y) < 14 / Math.min(cam.k, 1)); }
CV.onpointerdown = e => { CV.setPointerCapture(e.pointerId); const p = at(e), n = hit(p); drag = {node: n, sx: e.clientX, sy: e.clientY, cx: cam.x, cy: cam.y, moved: false}; if (n) alpha = Math.max(alpha, .3); };
CV.onpointermove = e => {
  if (!drag) { const n = hit(at(e)); if (n !== hover) { hover = n; CV.style.cursor = n ? 'pointer' : ''; } return; }
  if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 3) drag.moved = true;
  if (drag.node) { const p = at(e); drag.node.x = p.x; drag.node.y = p.y; drag.node.vx = drag.node.vy = 0; }
  else { cam.x = drag.cx + e.clientX - drag.sx; cam.y = drag.cy + e.clientY - drag.sy; }
};
CV.onpointerup = () => {
  const d = drag; drag = null;
  if (d && d.node && !d.moved) { G.hidden = true; d.node.ghost ? create(d.node.name, '') : open(d.node.name); }
};
CV.onwheel = e => { e.preventDefault(); const k = Math.min(4, Math.max(.2, cam.k * (e.deltaY < 0 ? 1.12 : .89))); cam.x *= k / cam.k; cam.y *= k / cam.k; cam.k = k; };
function showGraph() {
  G.hidden = false; buildGraph();
  G.querySelectorAll('.nb-gscope button').forEach(b => { b.classList.toggle('on', b.dataset.s === gScope); b.setAttribute('aria-pressed', b.dataset.s === gScope); });
  for (let i = 0; i < 120; i++) tick();
  cancelAnimationFrame(raf); loop(); $('.nb-gclose').focus();
}
$('.nb-graph-btn').onclick = showGraph;
$('.nb-gclose').onclick = () => { G.hidden = true; $('.nb-graph-btn').focus(); };
G.querySelector('.nb-gscope').onclick = e => { const b = e.target.closest('button'); if (b) { gScope = b.dataset.s; store('nbGraph', gScope); showGraph(); } };

// ================= keys that work anywhere in the Notebook tab =================
addEventListener('keydown', e => {
  if (!document.getElementById('notebook')?.classList.contains('on')) return;
  if (e.key === 'Escape') { if (!G.hidden) { G.hidden = true; return; } if (!AC.hidden) { hideAc(); ED.focus(); return; } }
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase(), act = {n: () => create(null, ''), s: () => { save(); status('Saved'); }, f: () => { setPanel('side', true); $('.nb-q').focus(); $('.nb-q').select(); },
    g: () => G.hidden ? showGraph() : (G.hidden = true)}[k];
  if (act && !e.shiftKey) { e.preventDefault(); act(); }
});

// load once the app bridge is up, and again whenever the tab is opened (files may have changed outside the app)
if (window.pywebview && pywebview.api) load(); else addEventListener('pywebviewready', () => load());
new MutationObserver(() => { if (document.getElementById('notebook').classList.contains('on') && api()) flush().then(() => load()); })
  .observe(document.getElementById('notebook'), {attributes: true, attributeFilter: ['class']});
addEventListener('beforeunload', () => { if (saveT) save(); });
})();
"""
