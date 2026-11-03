# Blackboard Buddy: handoff notes

Paste this file into a new Claude Code session (any account) to carry on the work.
Last updated: 2 Nov 2026, 03:15.

## What it is
A Windows desktop app for Min (UWE Bristol, BSc Computer Science, year 2). It reads the
UWE Blackboard Ultra site (https://blackboard.uwe.ac.uk) and shows:
- This week: a countdown to the next deadline, the timetable and a short AI briefing.
- Deadlines, Updates, Plans, Notes, Grades and Modules.
It uses the Claude API (key in `.env`) to write the briefing, assignment plans and study notes.
The goal: Min should never need to open the Blackboard website.

## Where things are
- App folder: `C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy`
  - `Blackboard Buddy.exe` and `_internal\`: the built app.
  - `.env`: `ANTHROPIC_API_KEY`, `LOOKAHEAD_DAYS=45`.
  - `output\`: `dashboard.html`, `deadlines.ics`, `notes\`, `plans\`.
  - Hidden source files. Run `attrib -h <file>` before editing, then `attrib +h <file>` after:
    - `main.py`: sync, AI and dashboard HTML/CSS/JS.
    - `app.py`: pywebview window.
    - `blackboard.py`: Playwright/REST client.
    - `ai.py`: Claude calls.
    - `vault.py`: the Notebook tab (files, links, UI).
    - `make_icon.py`, `icon.ico`/`icon.png`, `data\` (snapshot, AI cache, run.log).
- `%LOCALAPPDATA%\BlackboardBuddy\`:
  - `venv\`: Python 3.14.
  - `browser-profile\`: Edge sign-in cookies.
  - `webview\`
  - `materials\`: downloaded module files. Kept out of OneDrive on purpose.
  - `b\`: PyInstaller build folder with `Blackboard Buddy.spec`. The short path avoids a Windows long-path error.
- Desktop shortcut: `Blackboard Buddy.lnk` points to the .exe, with its icon set to `icon.ico`.

## How it works
- **Sign-in:** Playwright drives Edge (`channel="msedge"`) with a persistent profile.
  - It tries headless first (Microsoft SSO cookies), then opens a visible window.
  - It calls Blackboard's REST API `/learn/api/public/...` with the session cookies.
  - Never type Min's password; Min signs in themself.
- **Syncs from the app:**
  - On start, if the last sync is more than 6 hours old.
  - In the background every 2 hours, without a window. If Blackboard has signed Min out,
    the app shows a "Signed out of Blackboard" prompt, and its Sign in button opens the window.
  - Manually with the â†» button.
- **Data rules:**
  - Only this year's modules (enrolled within 300 days).
  - Deadlines are gradebook columns plus calendar items of type `GradebookColumn`.
    `Course` calendar items are the timetable.
- **AI:**
  - Model `claude-opus-5-5` with adaptive effort and the server-side fallback beta.
  - Results are cached in `data\ai-cache.json`. The briefing key is `briefing:v2:<hash of inputs>`;
    bump `v2` when the prompt changes.
  - Notes: at most 8 new notes per sync, only for files changed in the last 14 days.
- **Materials:**
  - Every attachment of file and document items is saved under `materials\<module>\<section>\`.
  - Word and PowerPoint files also get a `.preview.html` text version for the in-app viewer.
  - Files are only re-downloaded when the item changes.
- **Modules tab:** rebuilds each module's outline from `parentId`, folding same-title wrappers.
  - Files open in a viewer: PDF, image or video inline; Word/PowerPoint as the preview, with "Open in app".
  - Panopto, LTI and submission links open Blackboard in the browser.

## Build and install
```bash
cd "/c/Users/minnk/OneDrive/Desktop/Blackboard Buddy"
python -c "import main,pathlib; s=main.load_json(main.SNAPSHOT,None); \
  main.build_dashboard(s, main.collect_deadlines(s), main.recent_announcements(s), \
  main.released_grades(s), main.rehydrate_dates(main.load_json(main.DATA_DIR/'ai-latest.json',{})))"
python build_bb9.py            # stages the sources, runs PyInstaller, copies over the app folder
```
`main.py` on its own does a full Blackboard sync (slow, signs in); the one-liner above just rebuilds
`output\dashboard.html` from the last sync. `python main.py --no-ai --no-open` does a real sync.

**Always build with `build_bb9.py`, never PyInstaller by hand.** A Python process gets a filtered view
of `%LOCALAPPDATA%\BlackboardBuddy\b\` and cannot see files it did not create itself, so PyInstaller
there fails with "Spec file not found" and the exe is silently never replaced (this cost a whole round).
`build_bb9.py` works because one Python process creates `%LOCALAPPDATA%\bb9`, copies every source and
the spec into it, builds, and copies `dist\Blackboard Buddy\*` back over the app folder. It deletes each
target first, because a hidden file cannot be opened for writing. It prints the exe's size and mtime -
**check those changed before believing a build landed.** Close the app first, or the copy fails.

Re-hide the sources afterwards and rebuild the share zip if you want to share it (Round 9l).

Editing the hidden sources - Python cannot open a hidden file for writing, so clear the flag first:
```bash
python -c "import ctypes
for n in ('main.py','app.py','ai.py','blackboard.py','vault.py','online.py'):
    ctypes.windll.kernel32.SetFileAttributesW(n, 0x80)"
```
`attrib -h a.py b.py` fails from the PowerShell tool ("Parameter format not correct"). To hide them
again, OR in `[System.IO.FileAttributes]::Hidden` with .NET, or call `SetFileAttributesW(n, 0x82)`.

## Design rules Min asked for
- Apple Ã— Nothing:
  - Light background `#efefef`, dark `#000`.
  - Full-page dot grid with no fade.
  - Doto dot-matrix font for headings, Space Mono for labels.
  - Glass cards, spring animations.
- Red `#d71921` is used only for urgency. A pulsing red dot marks the closest item
  (next deadline, its plan, the next class, a badge on the Deadlines tab).
  It hangs in the margin so the text stays aligned.
- Theme button: a plain light/dark toggle (no "auto"), with a circular reveal and spam-click guard.
- Intro: dot-matrix "HI MIN" plus a typed subtitle, about 5 seconds, cannot be skipped, flashes out.
- Briefing:
  - Under 200 words, professional tone.
  - Headings: Key Priorities, Deadlines, Updates, Schedule, Points to Note.
- Everything professional and minimal. Min dislikes walls of text and anything that looks "AI generic".
- App icon: black squircle with a white dot-matrix "B" and a red dot.

## Round 3 (2 Nov, 01:40): built, tested and installed
- **Top-left corner: account button**, which opens a sheet with:
  - Name, username and programme, last sync, and Claude spend this month (`data\spend.json`).
  - The activity graph.
  - **Settings**, saved in `data\settings.json`:
    - Background sync: off, 1, 2 or 6 hours.
    - New notes per sync: 0â€“15.
    - Change the API key.
    - Open the study files folder.
    - Sign out of Blackboard, which deletes `browser-profile`.
- **Bottom-right: Ask Claude** chat.
  - `Api.ask`, then `AI.chat` with `main.chat_context`.
  - The context block is prompt-cached, effort is low, and the first question costs about $0.10.
- **Activity graph**: GitHub-style, drawn as dots (`data\activity.json`, keyed by day).
  - Shows a whole year that scrolls sideways; the mouse wheel scrolls it while hovering.
    It opens scrolled to today.
  - â€¹ year â€º picker, going back to the earliest year in the data.
  - Layout toggle: Months (a block per month) or Year (one GitHub-style strip), remembered in `localStorage.heatLayout`.
  - Rows run Monday to Sunday. Today is ringed and future days are faded.
  - The data is permanent in `data\activity.json`; rebuilds never touch `data\`.
  - Ticks come from app opens, syncs, Claude questions and notes made.
  - Shown on This week and in Account.
- **Notes tab**: module cards like Modules.
  - Inside each: notes by section, plus "Not in your notes yet" files with **Make notes** buttons.
  - Those run `Api.make_notes` and then `main.make_notes_for`, which works from the downloaded files and needs no sign-in.
  - `run_ai` now keeps all cached notes, not only the last 14 days.
- **Updates**: each update has a Hide/Unhide button (`localStorage.hiddenUpdates`) and a "Show N hidden" toggle.
- **Red dot**: centred in the gutter. The timetable's first column has 30px of left padding.

## Round 4 (2 Nov, 02:00): sharing and updates, built and installed
- **Works for other people.**
  - First run asks for name, Blackboard address (prefilled UWE) and an optional Claude key.
  - These are saved to `.env` as `USER_NAME`, `BB_URL` and `ANTHROPIC_API_KEY` by `Api.setup`, using `main.set_env`.
  - `main.display_name()` uses `USER_NAME` first, then the Blackboard first name.
  - Data goes in `main.BASE`: next to the .exe, or `%LOCALAPPDATA%\BlackboardBuddy` if that folder is read-only.
  - `ai.SYSTEM` no longer mentions UWE.
- **Share zip:** `Desktop\Blackboard Buddy - share.zip` (69 MB).
  - Contains the `Blackboard Buddy\` folder: the exe, `_internal` and `READ ME FIRST.txt`.
  - No `.env`, data or notes. Never send the app folder itself, because it holds Min's key.
- **Auto-update:**
  - `main.VERSION` is `1.1.0`. `main.UPDATE_REPO` is a GitHub "owner/repo" and is **still empty, so update checks are off**. Fill it in, then rebuild.
  - `app.update_loop` checks GitHub `releases/latest` 20 s after start and then every 12 h.
  - If the tag is newer and the release has a .zip asset, it shows the "Update available" prompt (`bbUpdate`).
  - Account > Settings has a version row and "Check for updates".
  - "Update now" (`Api.install_update`, installed app only, github.com URLs only):
    1. Downloads and unzips to temp.
    2. A .bat waits for the app to close.
    3. robocopy copies the new files over the app folder (`.env`, `data` and `output` aren't in the zip, so they're kept).
    4. The app reopens.
  - **Release steps:** bump `VERSION`, build, make the share zip, then on GitHub create a Release tagged `v<VERSION>` with the zip attached. The first line of the release notes appears in the prompt.
  - The swap itself is untested until the first real release.

## Round 5 (2 Nov, 02:25): Notebook tab, built and installed
- New **Notebook** tab (after Notes): Min's own notes, Obsidian-style. Code is in hidden `vault.py`, imported as `notebook`.
  - It isn't named `notebook.py` because PyInstaller ships a hook for Jupyter's `notebook` package, which breaks the build.
- **Storage:** plain .md files in `<app folder>\Notebook\` (`main.BASE / "Notebook"`), pictures in `Notebook\attachments\`,
  deleted notes in `Notebook\.trash\`. The folder opens fine in Obsidian.
  - `Notebook\Start here.md` is the full user guide, written on first run along with an example note `Linking notes.md`.
- **Python** (`vault.py`):
  - `all_notes`, `save(name, text, old)`: with `old` set it renames the note and rewrites `[[old]]` links in every note.
  - `delete` moves the note to `.trash`.
  - `save_image(data_url)` writes to `attachments/`.
  - `render`: Python-Markdown with tables, fenced_code, sane_lists and nl2br. A list, quote or table may start straight under a text line, as in Obsidian.
  - Api wrappers in `app.py` are `nb_list`, `nb_save`, `nb_delete`, `nb_image`, `nb_render` and `nb_open_folder`.
- **UI** (`NB_HTML`/`NB_CSS`/`NB_JS` in `vault.py`, injected by `main.build_dashboard`):
  - Left column: search with highlights, tag chips (#tag), pinned notes first, + New with templates
    (Blank, Lecture, Assignment, Group meeting), Today (`Daily/YYYY-MM-DD`), Graph, Folder.
  - Middle: title (rename), Edit/Split/Read, toolbar (bold, italic, heading, lists, checklist, quote, code, `[[ ]]`, Image, Table,
    ✦ Ask, which sends the note to Ask Claude, Pin, Delete), autosave after 0.7 s, word count.
  - `[[` autocomplete (arrows/Enter, or "Create …"). Clicking a missing link creates that note.
  - Checkboxes tick from the preview. Enter continues lists, Tab indents. Paste, drop or button adds pictures.
  - Right column: Backlinks, Links, Outline (click to jump), Mentions (with a "Link" button). It hides below 1100 px.
  - Graph: force layout on a canvas, the current note red; drag, pan, zoom, click to open; "All notes" / "This note" (2 hops).
  - Shortcuts: Ctrl+N/S/F/G/E/B/I/K.
- Tested in Edge with a stubbed bridge against a temp vault (`%LOCALAPPDATA%\BlackboardBuddy\nbtest.py`): no JS errors,
  links/rename/backlinks/images/tasks/daily/trash all checked, dark, light and 900 px screenshots fine.
- The share zip was rebuilt with the Notebook (Desktop, about 69 MB).

## Round 6 (2 Nov, 03:00): Word-style Notebook editor, built and installed
Min asked for a Word-like editor (no typing `*`/`_`, a toolbar and Ctrl+B/I/U), the Notebook filling
the window, and something easier to use. That is now the editor in the app.

- `vault_word_wip.py` was promoted to `vault.py`. Its Playwright test is now `vault_test.py`.
  The Round 5 Markdown editor is kept as the hidden `vault_round5_md.py.bak` in case a rollback is wanted.
- **The test passes 30/30 with no page errors.** Run it from the app folder with the venv python:
  `"$LOCALAPPDATA/BlackboardBuddy/venv/Scripts/python" vault_test.py`.
  It needs `output\dashboard.html` to be current, so run `main.py --rebuild --no-open` first.
  Screenshots land in `%LOCALAPPDATA%\BlackboardBuddy\nbshots\`.

**What the editor has** (all in `vault.py`):
- A `contenteditable` editor that still saves plain Markdown files. Markdown becomes the editor's HTML
  through `nb_render` plus `prep()`/`tidy()`; the editor becomes Markdown through `toMd()`
  (`blocks`/`inline`/`list`/`table`).
- A Word-like toolbar: undo/redo, a style dropdown, B/I/U/S, highlight (`==x==`), code, clear;
  bullets/numbers/checklist, indent and outdent; Link note (a picker), Web link (an in-app box, no
  `prompt()`), Picture, Table (with +Row/+Column/-Row), Line; A-/A+ text size, Wide page, a Markdown
  view toggle and a ? (help) button.
- Typing shortcuts: `- `, `1. `, `[] `, `# `, `> ` and `---`+Enter. Ctrl+0-3 set headings;
  Ctrl+Shift+7/8/9 make lists; Ctrl+K links a note; `[[` opens the note autocomplete.
- Links to other notes appear as chips; clicking one opens the note.
- Full-window layout through `html:has(#notebook.on) header{display:none}`, with buttons to hide the
  notes list and the side panel.
- Accessibility: ARIA roles, `aria-pressed` states, an `aria-live` save status, arrow keys along the
  toolbar, visible focus rings, reduced motion respected.

**The bugs that were blocking it, and the fixes**
1. **Tab shortcuts stole keys from the editor** (`main.py`, the global `keydown` that switches tabs).
   It only ignored `input,textarea`, so in a `contenteditable` a typed "1" switched tab and the arrow
   keys moved along the tab bar. That is why the `[[` autocomplete and Ctrl+K looked broken: by then
   the Notebook tab was no longer on screen. It now ignores
   `input,textarea,[contenteditable="true"],#notebook`. **Keep that selector in mind when touching the
   tab bar** - the Notebook has its own keyboard model.
2. **Edge nested new blocks inside the `<p>` the caret was in**, giving `<p><ul>...</ul></p>`, which is
   invalid and saved as nothing. `flatten(root)` in `vault.py` lifts a `ul/ol/p/div/blockquote/pre/
   table/hr/h1-h6` back out to the top level, keeping the nodes themselves so the caret survives.
   It runs from `tidy()`, `cmd()`, `insertBlocks()` and the editor's `input` handler.
3. **`checklist()` swallowed the list above it.** `execCommand('insertUnorderedList')` on the line after
   a list merges into that list, so ticking "checklist" turned the bullets above into tasks. It now
   builds its own `<ul class="tasks">` by hand.
4. **Enter in an empty checklist item did not leave the list**, because the tick box counts as content.
   The Enter handler now steps out of the list when the item has no text.
5. **Tables and lines went in through `insertHTML`** and left the caret outside the table. `insertBlocks()`
   + `caretIn()` put them after the caret's block and drop the caret in the first cell.
6. **Tick boxes are now per item, not per list.** Markdown merges a checklist written straight after a
   bullet list into one list, so a list can be part bullets, part tasks. `boxes()` only fills in a new,
   empty item of a list that already has boxes; the toolbar's checklist button boxes every item itself;
   `toMd` reads the box off the item, never off the list's class; and the CSS hides the bullet per item.
   Adjacent lists of the same kind are written with no blank line between them, so saving, reopening and
   saving again gives the same file.
7. **Guide round-trip.** A literal `[]` inside bold saved as `\[\]`, so the guide now writes it as code.
   A line break inside a list item (an `<li>` with a `<br>` from nl2br) collapsed onto one line; `list()`
   now keeps it as a newline with the continuation indent.
- `render()` also turns `~~x~~` into `<del>` and `==x==` into `<mark>`, and escapes a `#tag` at the start
  of a line so it does not become a heading.

**Also done**
- `Notebook\Start here.md` and `Notebook\Linking notes.md` were rewritten with the new Word-style guide.
  They were still byte-identical to the Round 5 text, so nothing of Min's was overwritten. Note that
  `vault.init()` only writes those two files when the folder has no notes at all, so if the guide text
  changes again, an existing copy has to be replaced by hand.
- The share zip was rebuilt: `Desktop\Blackboard Buddy - share.zip`, 69.5 MB, exe + `_internal` +
  `READ ME FIRST.txt` only. Checked: no `.env`, no `data`, no notes.

## Round 7 (2 Nov, 03:15): Modules in light mode, built and installed
The open item was "check the Modules tab in light mode". Light mode turned out fine; the check found a
worse problem in **dark** mode instead.

- **Blackboard's own HTML carries hard-coded colours** - black text, blues, purples - pasted in by staff.
  On the dark theme that content was invisible: black on black, a contrast ratio of 1.02. Real text was
  affected ("Module Name: Artificial Intelligence II", staff names, link titles).
  `clean_body()` in `main.py` now strips colour from imported Blackboard HTML: `color`, `background`,
  `background-color` and `-webkit-text-fill-color` out of `style="..."`, plus the legacy `color=` and
  `bgcolor=` attributes. Everything else (other styles, hrefs, entities) is left alone. The helpers are
  `_TAG`, `_STYLE_ATTR`, `_COLOUR_ATTR`, `_COLOUR_DECL` and `_no_colour`, just above `clean_body`.
  This also keeps the design rule that red is only ever used for urgency.
- **The muted grey was just under the 4.5:1 contrast minimum** on the light page background
  (`#7b7b7b` gave 3.68:1 on `#efefef`). It is now `#6a6a6a`, in the dashboard CSS and in `PREVIEW_HTML`.
  Dark mode's `#8a8a8a` was already fine.
- Checked with a script that walks every text element of three modules with all 40 sections expanded and
  works out the real contrast ratio against the effective background:
  the hidden `contrast_test.py` in the app folder. Run it like the Notebook test, after a
  `main.py --rebuild --no-open`. It now reports **nothing below the minimum in either theme**, and
  the Notebook test still passes 30/30.
- **Module titles:** UWE writes some as "Artificial intelligence ii", so `short()` in `main.py` now puts a
  trailing `ii`/`iii`/`iv` into capitals. Nothing else is re-cased: "BSC (HONS) COMPUTER SCIENCE" is the
  programme's own official title, and title-casing it would give "Bsc (Hons)". The lookbehind needs a
  space before the numeral, so "Hawaii" is left alone.
- Built, installed and running. The share zip was rebuilt again (69.5 MB).

Worth knowing when testing the dashboard with Playwright:
- The Modules tab's section id is **`courses`**, not `modules`.
- Module cards are `#courses button.course[data-open]`; the Notes tab has look-alike `.course` buttons
  earlier in the document, so always scope the selector to `#courses`.
- Stub `api.state()` as `async () => ({})`. Returning `null` throws
  "Cannot read properties of null (reading 'filter')", which looks like a real page error but is not.

## Status
Round 7 is done: the Modules tab checked in both themes, built, installed and running.
Round 6 (the Word-style Notebook editor) is done and tested 30/30. Before that, in Round 5 and earlier:
- Modules tab rebuilt in-app, all materials downloaded (44 files, 136 MB), file viewer.
- Notes grouped by module then section, with type tags and module filter chips. Plans grouped by module.
- Background sync with an in-app sign-in prompt. Account sheet, settings, Ask Claude, activity graph.

Open items / ideas:
- The Notebook has only been driven by Playwright in Edge against `output\dashboard.html`, never clicked
  through by hand in the pywebview window. Worth Min trying: type a note, make a checklist, add a table.
- At a window narrower than about 700 px the notes list covers the editor until a note is clicked, and
  the tab bar itself overflows. That is the whole app, not just the Notebook, and a desktop window is
  rarely that small.
- Pages that are only an embedded video (e.g. OS "Introduction") show an "open on Blackboard" note.
  Some pages with no text and no iframe may still be hidden.
- `main.UPDATE_REPO` is still empty, so update checks are off. Fill in the GitHub "owner/repo", then
  rebuild. The update swap itself is untested until the first real release.
## Round 8 (2 Nov, 03:55): own title bar, Claude accounts, customising, built and installed

Min asked for: a resizable Ask Claude panel, togglable Notebook buttons, the Windows title bar gone,
a model/effort/usage view, sound effects, "make everything customizable", and a Claude sign-in that
can be switched between accounts.

**No claude.ai login exists for third-party apps.** Anthropic publishes no OAuth for a subscription
account, so "sign in" means an API key. The app now verifies the key with Anthropic before saving it,
keeps several accounts, and leaves the AI features off until one is signed in.

- **Window** (`app.py`): `frameless=True, easy_drag=False` on both `create_window` calls, with
  `DRAG_REGION_DIRECT_TARGET_ONLY` on. Frameless kills WinForms' resize borders, so `_restore_resizing()`
  puts `WS_THICKFRAME|WS_MAXIMIZEBOX|WS_MINIMIZEBOX` back via ctypes and `SWP_FRAMECHANGED`.
  The page draws `.titlebar` (a drag strip carrying the app name) and `.wctl` (minimise / maximise / close),
  served by `Api.win_minimize/win_maximize/win_close`. `.refresh` and `.theme` moved left to clear them.
- **Resizable panels** (`main.py`, `EXTRA_JS`): `.sh-grip` on a sheet's inner edge. The drag is
  **delta-based** (`w0 + (x0 - clientX)`), not `innerWidth - clientX`, so it still works under interface
  zoom. Widths are kept per sheet in `localStorage['sheetW:<name>']`; double-click resets.
- **Claude accounts**: `data/accounts.json` = `{"active": id, "list": [{id, label, key, hint}]}`.
  `main.accounts/active_account/active_key/signed_in/new_ai`. `Api.account_add` validates with
  `anthropic.Anthropic(api_key=key).models.list(limit=1)` and **never returns a key to the page**.
  `main.adopt_env_key()` (called from `on_start`) turns the old `.env` key into the first account.
- **Model, effort, usage**: `ai.MODELS` holds four models with per-million prices; `AI.__init__`
  takes `model`/`api_key`; `_track` prices cache reads separately. `main.spend_summary()` feeds the
  usage list. Changing the model writes to `settings.json` via `save_settings`.
- **Settings redesign**: the account sheet now has four sections - General / Claude / Look / Tabs.
- **Look**: highlight colour, interface size (`html.style.zoom`), dot grid, animations, opening screen,
  sounds, volume. Kept in `localStorage.look` and applied by a small script in `<head>` **before the
  first paint**, so there's no flash of the standard look. `.sub` is an ALL-CAPS label style - full
  sentences use the new `.note` class instead.
- **Tabs**: tick to show, drag to reorder, stored in `localStorage.tabsPref`. Hiding the tab you're
  looking at moves you off it first. The number and arrow keys skip hidden tabs (`vis()` in the nav script).
- **Sounds**: `SFX` synthesises every sound with WebAudio (no audio files in the build). One capture-phase
  click listener picks the sound from the button's classes.
- **Notebook** (`vault.py`):
  - Highlight and Code are now real toggles - press once to switch on with nothing selected, press again
    to switch off, exactly like Bold. A zero-width space holds the caret inside a brand-new tag;
    `dropEmpty()` strips those, and `toMd()` runs on a **clone** so saving never disturbs the caret.
  - A "..." button opens a chooser for which toolbar buttons show at all (`localStorage.nbTools`).
    Groups that end up empty hide themselves; the table group is left out; arrow-key nav already skips hidden.
- Tests: `vault_test.py` 44/44, `contrast_test.py` clean in both themes, and a settings-panel run
  (scratch script `nbdbg/set.py`) covering accounts, model change, colour, zoom, tab hide/reorder and reload.

### Round 8b (2 Nov, 04:10): fixes Min found in the real window

- **Resizing was broken.** `_restore_resizing()` used `GetActiveWindow()`, which is **per-thread** -
  `on_start` runs on its own thread, so it got the wrong handle and the style was never changed.
  Now `_hwnd()` asks WinForms (`webview.windows[0].native.Handle`) and falls back to
  `FindWindowW(None, "Blackboard Buddy")`, retrying for 5s, and logs whether WS_THICKFRAME took.
  Verified on the running window: THICKFRAME, MAXIMIZEBOX and MINIMIZEBOX are all set.
- **Bold and friends would not switch off.** Two causes, both in `vault.py`:
  1. `cmd()` ran `flatten(ED); boxes()` after every command, and rebuilding the DOM throws away the
     pending style Chromium keeps on an empty caret. Those now run only for non-inline commands.
  2. `restore()` always did `removeAllRanges(); addRange(lastRange)`, and **re-setting the selection
     also clears the pending style**, so each press switched the format on again. It now leaves a
     selection alone when it is already inside the editor, and a `mousedown` handler on `.nb-tools`
     calls `preventDefault()` so pressing a button never blurs the editor in the first place.
  Covered by 7 new checks; `vault_test.py` is 51/51.
- **Refresh and theme buttons** moved down out of the title bar to `top:66px`, right edge.
- **Hover animations** on the window buttons: a pill that grows from the middle, the minimise line
  drops, the close cross turns red and spins 90 degrees. Refresh spins on hover; the theme button
  uses a growing ring, because the script owns its inline `transform`.
- **Custom pointer**: a sharp arrow as an SVG data-URI cursor, pale on dark and dark on light via
  `--cur` / `--cur-p`, with a red dot added for anything clickable. Text fields keep the I-beam.
  It is a Look switch (`cursor` in `localStorage.look`), applied before the first paint.

### Round 8c (2 Nov, 04:15): resizing for real, and a pointer worth looking at

- **Resizing, second attempt.** WS_THICKFRAME was confirmed set on the live window and resizing
  *still* didn't work: with `FormBorderStyle = None` the web view covers the whole window, so the
  frame never gets the mouse. Now the page draws its own edges (`.redge`, eight strips, `z-index:60`,
  5px sides and 9px corners) and a press calls `Api.win_resize_edge(1-8)`, which does
  `ReleaseCapture()` + `PostMessageW(hwnd, WM_SYSCOMMAND, SC_SIZE + direction)`. Windows then runs
  its own resize loop, so it is smooth and needs no polling. The style bits stay for snapping.
- **`main.log_quiet()`** added: writes to `data/run.log` only. `main.log()` notifies LISTENERS, which
  is what put "Resize border on (window ...)" in Min's status bar.
- **Refresh** turns once per press (`.refresh.turn` + a one-shot animation re-triggered by removing
  the class and reading `offsetWidth`) instead of spinning on hover.
- **Pointer** is now a choice, not a switch: **Ring** (default), **Dot**, **Arrow** or the Windows one,
  under Settings -> Look. Each shape carries its own hot spot inside the `--cur` / `--cur-p` value,
  built by a small Python helper so the three shapes x two themes x normal/pressable are generated,
  not hand-written. An old boolean `cursor` value migrates (`true` -> ring, `false` -> off).

### Round 8d (2 Nov, 04:18): the Live pointer

Min wanted the pointer animated. A CSS `cursor:` image cannot animate, so **Live** hides Windows'
pointer (`html.cur-live,html.cur-live *{cursor:none!important}`) and the page draws two elements:
`.cur-dot` tracks the mouse exactly and `.cur-ring` eases toward it at 0.22 a frame, so it stretches
behind fast movement and settles when you stop. The rAF loop stops itself once they meet.

| state | ring |
|---|---|
| idle | 30px, thin, 50% |
| over anything pressable | 46px, red |
| held down | 20px, thicker, solid |
| over a text field | a 3x24px bar, dot hidden |

- Live is now the default (`LOOK_DEF.cursor`), with Ring / Dot / Arrow / Windows still in Settings -> Look.
  `curStyle()` migrates an old boolean. `fancy-cursor` is **not** added for Live, since there is no image.
- The resize edges deliberately keep Windows' own arrows, so `.redge` is left out of `PRESSABLE`.
- With animations off, the ease is 1, so the ring sits exactly on the dot.

### Round 8e (2 Nov, 04:33): the white screen, resizing (third attempt), zoom, more pointers

- **White screen on startup.** Turning **Animations** off hid the opening screen
  (`html.no-anim .intro{display:none}`) but the script still ran, and `intro-on` hides
  `header, main, nav` for about five seconds. The result was a blank pale page every launch.
  Two fixes: the head script now sets `sessionStorage.introSeen` when **either** `intro` or `anim`
  is off, and `intro()` has a 7s watchdog that calls `finish()` if `intro-on` is somehow still on.
  Worth knowing: every earlier Playwright run set `introSeen='1'`, so the startup path was never
  being tested. Test the intro path when touching anything that hides the page.
- **Resizing, third attempt.** `SC_SIZE` via `PostMessage` did not work either - the browser control
  holds the mouse, so Windows' own resize loop never sees it. The page now drives it: pointer capture
  on the edge strip, `ev.screenX/Y` deltas times `devicePixelRatio`, and `Api.win_set_bounds()`
  (`SetWindowPos`), at most one call per frame, minimum 720x480. `Api.win_bounds()` and
  `Api.work_area()` back it.
- **Snap layouts**, since a frameless window gets none: hover Maximise for ~0.45s (or right-click it)
  and a menu of halves, quarters, top half and full appears. `Api.win_snap()` works off
  `MonitorFromWindow` + `GetMonitorInfoW` rcWork, so it respects the taskbar and the right monitor.
- **Interface size above 100% broke two things.** Mouse coordinates arrive unzoomed but a measured
  `getBoundingClientRect()` does not scale the way you would expect under a root `zoom`.
  The live pointer now divides by `uiZoom()`; the sheet grip, the snap menu and the notebook's
  button chooser were rewritten to anchor off `offsetWidth` / computed insets / `offsetTop`, which
  are plain CSS pixels and need no correction at all. Verified exact at 85%, 100% and 130%.
  **Rule of thumb: never place a fixed element from a measured rect while a root zoom is possible.**
- **More pointer shapes**: dashed ring over anything draggable (title bar, tab rows, the panel grip),
  a wide pill over sliders, a dotted dim ring over disabled controls, and a spinning red arc while
  the app is syncing (`html.busy`, set by `bbStatus`).
- **Claude controls in Ask Claude**: the panel now has a bar with the account (click to switch, or
  jump to Settings), model, effort and the month's spend, kept in step with the Settings copies.

### Round 8f (2 Nov, 04:40): tab dragging, native dark widgets, resize pointer

- **Dragging a tab left the pointer behind.** It used the browser's own drag and drop, and a native
  drag suppresses `pointermove` for its whole duration, so the drawn pointer simply froze.
  The tab rows now reorder with plain pointer events. The live pointer also follows
  `drag` / `dragover` / `dragenter` now, so dropping a picture into a note behaves too.
- **And the drag died after one frame.** `row.setPointerCapture()` plus `list.insertBefore(row, ...)`
  do not mix: moving the captured element through the DOM releases the capture. The listeners live
  on `document` instead. **Never capture the pointer on an element you are about to move.**
- **The list and the tab bar disagreed.** `applyTabs()` appended the saved order (pushing those tabs
  to the end) while `drawTabs()` always rendered `bbTabs` in its original order. Both now read one
  `tabOrder()`: saved order first, then anything new.
- **White select menus and scrollbars** on the dark theme: Windows draws those itself and was never
  told the theme. Added `color-scheme` on `:root`, switched with `data-theme`.
- **The pointer shows resizing.** At a window edge the ring lies down into a 40x13 bar turned to
  match the direction (0, 90, 45 or -45 degrees), driven by `ring.dataset.turn` folded into the
  transform. `html.cur-live .redge{cursor:none}` stops Windows drawing a second one.

### Round 8g (2 Nov, 04:54): the blank window on restart

Min reported the app stuck on a black or white screen after closing and reopening. **I could not
reproduce it** - six clean launches (kill-and-start, and close-button-then-start) all rendered, the
live window inspected over CDP was fine, and every stored Look / Tabs combination renders in
Playwright. Ruled out: cloud-only OneDrive files (all 297 are local), the process failing to exit
(it does), and a stuck `intro-on`.

Attaching a debugger to the real window is the technique worth remembering:
```powershell
[Environment]::SetEnvironmentVariable("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS","--remote-debugging-port=9333","Process")
Start-Process "...\Blackboard Buddy.exe"
```
then `p.chromium.connect_over_cdp("http://127.0.0.1:9333")` and inspect the page for real.

What was fixed anyway, in order of how likely it is to be the cause:

1. **The window's own background was `#F5F5F7`.** That is what fills the frame while WebView2
   starts, which on a dark setup is exactly a "white screen" for as long as the engine takes.
   `start_colour()` now reads `settings["theme"]`, written by `Api.note_theme()` whenever the page
   loads or the theme is toggled, so the empty window matches.
2. **Two copies at once.** `only_one_copy()` holds a named mutex; a second launch raises the first
   window and exits. Two copies would fight over the WebView2 data folder and the loser opens blank.
   This also explains a fast close-then-reopen, when the first process has not finished exiting.
3. **A backstop that nothing else can break.** The head script now defines `bbMarks` and an
   `unhide()` that strips `intro-on`, fired by a 6s timer *and* by `window.onerror`. It is the
   first script on the page, so it survives any later failure. Verified by throwing inside the
   opening screen on purpose: the page appears at once instead of hanging.
4. **Startup tracing.** `Api.note_start()` writes the marks to `data/run.log`
   (`page start: head | intro | ready | dom`). If it happens again, that line says how far it got.

### Still open after Round 8
- Min should confirm the real window looks right - Playwright can't see native chrome, and
  computer-use can't resolve "Blackboard Buddy" by name.
- The share zip has **not** been rebuilt for Round 8.
- `python main.py --rebuild` needs the dependencies; this round they were installed into
  `%LOCALAPPDATA%\Python\pythoncore-3.14-64` with `pip install -r requirements.txt`.

- Keep this file updated after every change (Min asked for a handoff before usage runs out).
- Possibly rename "HI MIN" / "LOADING YOUR WEEK" to something more professional (Min was asked).
- Remind Min:
  - Change their UWE password (it was pasted in chat once).
  - The SDGP project choice is due Fri 6 Nov.
- Spend so far: about $3 of $24.41 API credit. Round 6 used no API calls.


## Round 8h - the blank window, found

**The app came up as a blank white (or black) window on every launch.** It was not WebView2,
not OneDrive, not a second copy, and not the opening screen. It was a CSS class name clash:

- The drawn "live" pointer is two elements classed `cur-ring` and `cur-dot`, hidden until wanted:
  `.cur-ring,.cur-dot{display:none;...}`.
- The pointer **style** setting puts `cur-<style>` on `<html>`.
- So choosing the **Ring** or **Dot** pointer made `<html>` match that rule and the whole document
  got `display:none`. The window then showed nothing but its own background colour - white on the
  light theme, black on the dark one - and it stuck, because the choice is saved in `localStorage`.

The user's saved value was `cursor: "ring"`, which is why it happened on every launch after they
changed the pointer, and why it never showed up in a browser test: the tests all ran on the
default pointer.

**Fix:** the two drawn elements are now `.pointer-ring` / `.pointer-dot`, which nothing can collide
with. The root style classes (`cur-live`, `cur-ring`, `cur-dot`, `cur-arrow`) are untouched, so a
saved setting still works and nobody has to re-pick their pointer. Added as a backstop:
`html{display:block!important}` - nothing landing on `<html>` can hide the document again.

**Test:** `curtest.py` loads the page once per pointer style x theme with the setting written to
`localStorage` and **no `introSeen` shortcut**, then asserts `<html>`/`<body>` are not `display:none`
and the header and main have height. It fails on the old page for `ring` and `dot` in both themes.
Keep it: a test that skips the opening screen cannot see a startup-only fault.

### The build was not actually rebuilding
`python -m PyInstaller --noconfirm "Blackboard Buddy.spec"` in `%LOCALAPPDATA%\BlackboardBuddy\b\`
fails with **"Spec file not found"** even though the file is there: Python processes get a filtered
view of that folder and can only see what they themselves created (`os.listdir` shows just `build`
and `dist`; `glob` finds no `.spec`). Files copied in by Bash or PowerShell are invisible to it, so
the build reads nothing and the exe is never replaced.

**Work around it by staging the build folder from Python itself:**
```python
# creates the folder, copies the sources and writes the spec - all from one python process
dst = pathlib.Path(r"C:\Users\minnk\AppData\Local\bb9"); dst.mkdir(exist_ok=True)
for n in ('main.py','app.py','ai.py','blackboard.py','vault.py','icon.ico','icon.png'):
    shutil.copy2(src / n, dst / n)
(dst / "Blackboard Buddy.spec").write_text(SPEC, encoding='utf-8')
```
then `cd` there and run PyInstaller, and copy `dist\Blackboard Buddy\*` over the app folder with
`shutil.copy2` (again from Python, for the same reason). 295 files, exe ~15.2 MB.
**Check the exe's size or timestamp changed before believing a build landed.**

## Round 9 (2 Nov, 05:30): Ask Claude warning, startup sound, richer sound effects, built and installed
- **Ask Claude warning.** The first time Ask Claude is opened, a popup (`.askwarn`, placed after the sign-in
  modal so `.signin:not(.update)` still finds sign-in first) says it is paid per question from the API
  credit (~$0.10 on Opus), shows this month's spend, and has "Don't show this again" (`localStorage.askWarned`).
- **Startup sound.** `boot` plays once per launch (`sessionStorage.bootSound`), `flash` when the intro finishes.
  `app.py run()` adds `--autoplay-policy=no-user-gesture-required` to `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`
  so it can play before any click.
- **Sound redesign.** `SFX` in `main.py` EXTRA_JS is now layered: `tone` (two detuned oscillators, optional
  pitch glide), `bell` (tone + inharmonic overtones) and `noise` (filtered noise for clicks/whooshes), all via
  stereo panning, a generated 1.6 s convolver reverb and a compressor. Sounds live in the `SOUNDS` table
  (same names as before: tap tab on off open close send reply done error boot flash). Still no audio files;
  still gated by `BB.settings.sounds` / `volume`. Test: scratchpad `sfx_test.py <dashboard.html>` measures
  each sound's peak through an analyser (all > 0, muted = 0).
- **Pointer.** Live pointer is the default (`LOOK_DEF.cursor = 'live'`); Min's copy is on it.
- Build this round: `build_bb9.py` (deletes each target before copying, as hidden files can't be overwritten),
  254 files, exe ~13.8 MB. The share zip has NOT been rebuilt since Round 8.

### Round 9b (2 Nov, 05:35): Nothing-style sounds, built and installed
Min asked for the sounds to be "more like Nothing style". The `SOUNDS` table is now dry and precise:
`pip` (a short pure sine, 2 ms attack, little reverb) and `tick` (a 12 ms high-passed noise click), laid out on
a fixed time grid, like a dot matrix lighting up. Toggles are two-step pips (up for on, down for off), open/close
are three-step arpeggios, done is a four-pip run, error is a square-wave double beep, and boot counts in six ticks,
then four pips, then a held chord. Reverb send is down to .1 and the master level is `volume/100 * .25`. The unused
`bell` voice was removed. Peaks measured at volume 60 are 0.02-0.08, muted 0. Exe built 05:35, 254 files.

### Round 9c (2 Nov, 05:39): scroll sound, built and installed
Scrolling ticks like a wheel with notches. A capture-phase, passive `scroll` listener (an IIFE right after
`window.SFX = SFX;`) keeps each scroller's position in a WeakMap and plays `scroll` (an 8 ms high tick plus a
faint pip) once per 56 px travelled, at most every 30 ms. Ticks are 12% higher going up, up to 18% higher and
louder when scrolling fast, with a little random pitch so they don't sound mechanical. `edge` (a low, soft thud) plays
when a scroller hits its top or bottom, at most every 400 ms. To support this, `SFX.play(name, k, lvl)` now takes an
optional pitch multiplier and level. Test: scratchpad `scroll_test.py <dashboard.html>` (wheel down gave 11 ticks
and 1 edge, up gave 12 ticks and 1 edge). Exe built 05:39, 254 files.

### Round 9d (2 Nov, 05:47): app needed several clicks to open; steady scroll tick
**Cause, two parts (`app.py`):**
1. `only_one_copy()` found "another copy" with `FindWindowW(None, "Blackboard Buddy")`. That also matches a
   **File Explorer window open on the app folder** (same name), so a launch raised Explorer and quit. `_hwnd()` had
   the same fallback, so the resize border and window buttons could act on Explorer too.
2. After the X is pressed, the old process takes a few seconds to shut down and still holds the mutex. A new launch
   in that gap bowed out with nothing on screen, so every other click did nothing.

**Fix:** `_find_window(pid=None)` enumerates windows titled "Blackboard Buddy" and keeps only those owned by this
program's exe (or, with `pid`, by that process). `_hwnd()` falls back to `_find_window(os.getpid())`. In
`only_one_copy()`, if the other copy has a visible window it is raised as before; otherwise the new copy waits up to
20 s on the mutex (`WaitForSingleObject`, accepting WAIT_ABANDONED) and opens normally.
**Test:** scratchpad `launch_test3.ps1` (closes with the X and reopens 1 s later, five times): every reopen showed a
window (about 5 s, waiting for the old copy; a cold start takes 0.9 s), and a click while open only raised the existing copy.
To make reopening faster, the remaining time is the old process's own shutdown plus `wait_for_profile`.

**Scroll:** Min wanted the tick consistent, so the pitch and level no longer change with speed or direction, and the random variation is gone.
Exe built 05:45, 254 files.

### Round 9e (2 Nov, 05:52): This week shows upcoming assignments instead of the timetable
Min asked to swap the timetable for upcoming assignments. In `main.py` (dashboard build, after the Notes tab),
`tt_html` is replaced by `up_html`: an "Upcoming assignments · N to hand in" table (Left / Due / Assignment /
Module) with the first six of `upcoming` (unsubmitted, due in the future, soonest first). The next one gets the
red `near` dot, and there is an "and N more in Deadlines" line when there are more than six. It reuses the `.tt` table
styling and `days_left()` pills. `timetable()` is kept, because Ask Claude's context still includes the next 14
days of classes. Exe built 05:52, 254 files.

### Round 9f (2 Nov, 05:58): Note map on This week (Obsidian-style, automatic)
A "Note map" card under Activity on This week: every note is a dot, and the lines are made automatically.
- **Data:** the dashboard build embeds `<script type="application/json" id="map-data">` with the current modules
  (not the "-BSc..." programme) and the study notes (`ai_results["notes"]`, title plus first 6000 chars). Notebook notes
  are read live with `pywebview.api.nb_list()` on `pywebviewready` and whenever the This week tab is clicked,
  so new ones show without a sync. The guide notes "Start here" and "Linking notes" are left out.
- **Connections** (JS IIFE at the end of EXTRA_JS):
  1. a study note joins its module (solid line);
  2. a Notebook note joins the module its folder or opening text names (solid line);
  3. Notebook `[[links]]` (solid line);
  4. shared topics: TF-IDF over words of 4+ letters (stopwords dropped, plural -s stripped), cosine similarity, and each
     note joins its two closest notes when similarity >= 0.12 (dotted line).
- **Look:** modules are rings with mono uppercase labels, study notes are dots, Notebook notes are diamonds. Hover lights a note and its
  neighbours in red with labels, drag moves dots, click opens the note (study note: Notes tab > module > that card
  opened and scrolled to; Notebook note: Notebook tab and the vault's global `open(name)`). Uses a force layout and auto-fits to
  the 380 px canvas. It redraws on theme change and resize, without wheel-zoom, so page scrolling isn't hijacked.
- **Test:** scratchpad `map_test.py <dashboard.html>`, which stubs two linked Notebook notes, screenshots light/dark and clicks a dot.
  Real app: 12 study notes, 8 connections. The page error "drawAccounts ... reading 'length'" in tests is from the stub
  returning {} for the accounts list, which is harmless. Exe built 05:57, 254 files.

### Round 9g (2 Nov, 06:12): map pop-up, Done buttons, choosing the study files folder, Blackboard in-app
- **Map pop-up.** While a note is open (a study-note card in Notes, or a Notebook note, via the vault's global `cur`),
  a round `.map-btn` sits above Ask Claude. It opens `.map-pop`, a small copy of the Note map sharing the same nodes and
  layout (`makeView()` draws both). The current note is red with a pulsing ring, and neighbours are named on hover.
  Clicking a dot goes to that note and the pop-up follows. Esc, ✕ or clicking outside closes it. `sync()` runs on
  clicks, `toggle` events and every 1 s; for a Notebook note made since the last build, it re-reads `nb_list` (at most every 3 s).
- **Done.** `DONE = data/done.json` holds "course|title" keys (`done_key()`, `set_done()`). `build_dashboard` leaves
  done items out of `upcoming`, so the red dots (nav, countdown, tables, plan card), the countdown and Upcoming move on.
  Deadlines rows get a "Mark done" / "✓ Done" button (the row dims and the task is struck through), and so do Plans cards (absolute-positioned
  in the summary, with the › moved to the far right). The click is captured on the document: the page flips at once and plays `done`, then
  `Api.set_done` saves, runs `main.run(sync=False, use_ai=False)` and the page reloads at the same scroll
  (`sessionStorage.keepScroll`). Tested: ticking the only open assignment took all four red dots away; unticking brought them back.
- **Study files folder.** Settings > General shows where they are, with Open folder and Choose folder… buttons. `Api.choose_materials`
  uses the pywebview folder dialog; a non-empty pick gets a "Blackboard study files" subfolder. `main.move_materials` moves the files,
  rewrites the snapshot's `local` paths (so nothing re-downloads), saves `materials_dir` in settings and reloads. `main.MATERIALS`
  reads that setting at import. `open_materials` now returns a message instead of letting Explorer pop an error.
  About Min's "Location is not available" error: the folder existed. It was the copy of the app *started from a Claude
  session*, whose AppData writes are sandboxed, so Explorer couldn't see them. Opened normally, it works.
- **Blackboard in the app.** Clicking any http link in the dashboard calls `Api.open_inside(url, title)`, which opens or reuses a
  second pywebview window (`viewer`, closed with the main one); Shift or Ctrl+click still goes to the browser. LTI items
  (Panopto etc.) now link to `/webapps/blackboard/execute/blti/launchLink?course_id=..&content_id=..&from_ultra=true`
  instead of the module page. The app's WebView2 profile is not signed in to Blackboard (the sync's sign-in uses a separate
  Playwright profile), so the first link asks for UWE sign-in once. Verified: the link reaches login.microsoftonline.com.
  Exe built 06:12, 254 files.
- **Testing note:** launching the exe from a Claude session runs it sandboxed. Don't sign in or choose folders in that copy.

### Round 9h (2 Nov, 06:16): map pop-up follows the window size
`.map-pop` width is `min(clamp(300px, 34vw, 620px), 100vw - 44px)` and `.mp-cv` height is `min(clamp(200px, 42vh, 560px),
100vh - 260px)`, so the pop-up stays on screen. The pulse loop redraws every frame, so the canvas picks up the new size by itself.
Measured with scratchpad `popsize_test.py`: 1180x860 gave 401x435, 1920x1080 gave 620x528, and 600x560 gave 300x309; all fully on screen.
Exe built 06:16.

### Round 9i (2 Nov, 06:19): sticky (magnetic) live pointer
In the live pointer IIFE (`main.py` EXTRA_JS, "the live pointer"):
- **Buttons.** `stick(t, off)` runs on every pointermove. For the nearest `STICKY` element (`button, a.ghost, a.btn, a.file, .swatch,
  [role=button]`) that is at most 240x72 px and not in the title bar, a resize edge or the Notebook editor, the ring heads 55% of the way to its
  centre (`tx/ty`). The element leans toward the mouse by 20% of the offset, capped at ±6 px, using the CSS `translate` property
  (so it adds to existing `transform` hover effects). Leans live in `pulled` and spring back to 0 in `frame()`. The centre is measured
  minus the current lean, to avoid feedback. With "reduce animation" on, there is no lean and the ring snaps.
- **Map dots.** The note map's `V.hit` now picks the *nearest* dot within radius+12 px and publishes its screen position as
  `window.bbStick`. Over a canvas, `frame()` moves the ring 80% onto that point and turns it red (`hot`).
- The dot always stays on the real mouse. `away()` releases everything when the mouse leaves the window.
- Test: scratchpad `magnet_test.py <dashboard.html>`. Mouse 14 px off a nav tab: ring halfway in, tab leaned 2.8 px,
  back to '' after leaving. Mouse 8 px off a map dot: ring within 2 px of it, red. Exe built 06:19.

### Round 9j (2 Nov, 06:26): stronger map magnet, labels that don't overlap, readable dropdowns
- **Magnet.** Map dots catch the pointer within radius+22 px (was +12). The ring sits exactly on the dot, and the small pointer dot
  is drawn 45% of the way there (the click still uses the real mouse, which is inside the catch radius).
- **Labels.** `makeView().draw` collects labels (priority: the hovered/current note, then its neighbours, then modules), then
  places each in the first free spot: under, over, right, left, or one line further under/over. Dots count as taken space.
  Labels that fit nowhere are dropped, except priority 0. Each has a 75% `--bg` backing. Neighbour labels truncate at 28
  characters (22 in the pop-up), the focused one at 40 (30). Test: scratchpad `labels_test.py` hooks `fillText`, hovers all 16 dots: up to
  11 labels at once, 0 overlapping pairs.
- **Dropdowns.** Windows drew `<select>` lists grey-on-white in dark mode (Ask Claude's model picker). Global CSS:
  `select option{background-color:var(--bg);color:var(--text)}` and `:checked` inverted. Note: `--card` isn't defined everywhere
  options are, so don't use it there. Exe built 06:26.

### Round 9k (2 Nov, 06:33): note map names say what's in the note; briefing crash fixed
- **BUG FIXED (important):** in `run_ai`, the briefing cache key line had a comment pasted into the middle of it:
  `bkey = "briefing:v2:" + hashlib  # bump v2 ....sha1(...)`, i.e. str + module, which raises TypeError (not caught by
  the anthropic excepts). Every sync that reached the AI step crashed before writing the briefing, plans or new notes. It now
  reads `bkey = "briefing:v2:" + hashlib.sha1(...).hexdigest()` with the comment on the line above.
- **Topic titles.** `main.note_topics(notes)` sets `n["topic"]` on each study note: `ai.AI.note_topic(title, md)` asks
  **Haiku 5.5, effort low** (whatever model Settings picks) for a 2-6 word title, cached in ai-cache as `topic:<sha1 of the note>`.
  A trailing "revision notes"/"notes" is trimmed. With no API key or on error, it falls back to `heading_topic(n)`: the first non-numbered
  heading minus "Revision notes", the module code and the module name. It's called at the end of `run_ai` and in `make_notes_for`.
  The 12 existing notes were back-filled once, for $0.005.
- The Note map (and its pop-up) labels study notes with `topic`; `title` (the Blackboard name) is still used to find and
  open the card in the Notes tab. Exe built 06:32.

### Round 9l (2 Nov, 06:36): share zip rebuilt
`Desktop\Blackboard Buddy - share.zip`, 74.5 MB, 255 files, made by scratchpad `make_share.py` straight from
`%LOCALAPPDATA%\bb9\dist\Blackboard Buddy` (the clean build, never the app folder) plus `READ ME FIRST.txt`.
The script checks for `.env`, data, notes, output and the strings sk-ant- / minnk / USER_NAME=, and found none.
`READ ME FIRST.txt` now says updates come as a new zip (`UPDATE_REPO` is still empty, so auto-update is off), drops the
timetable and mentions the API cost. Rebuild the zip after every exe build you want to share.

### Round 10 (2 Nov, 08:13): Online tab (shared live board), corner buttons, pointer after reload
- **New `online.py`** (hidden like the other sources; added to `build_bb9.py`). Tab "Online" after Notebook: `ON_HTML/ON_CSS/ON_JS`,
  full-bleed like the Notebook. A pan/zoom board (drag empty space = pan, wheel = zoom at the pointer, Shift+drag = box select)
  with blocks: note (sanitised rich text), sticky, big text, code (highlight.js 11.9.0 from cdnjs, language guess), terminal
  output (ANSI stripped; prompts/errors/warnings coloured), checklist (tick, assign to a member), picture (paste/drop, shrunk to
  ≤450 KB JPEG), link (opens in the in-app viewer), pen drawing, arrows between blocks. Paste sorts text into link/terminal/code/note
  (`sniff`). Drop files: images, code files, logs. Undo/redo, Ctrl+D, Ctrl+C/V of blocks, Delete, arrow keys, F = fit, 0 = 100%.
  Keys are caught on `window` (capture) while the tab is open so the app's tab keys don't fire; `#online` added to the global key guard.
- **Live sync = Supabase** (supabase-js 2.45.4 UMD from jsdelivr, loaded on first use). Anonymous sign-in, groups with 8-char
  invite codes, RLS so only members read/write a group's blocks. Blocks are upserted (debounced 600 ms; soft delete via
  `deleted=true` because realtime can't filter deletes). Live typing/moving and cursors go over a broadcast channel; presence
  shows who's online and which block someone is typing in (others can't type there at the same time). Failed saves retry every 5 s.
- **Not set up yet:** `online.DEFAULT_URL/DEFAULT_KEY` are empty. Until filled (or typed into the tab's Connect card, saved as
  `online_url/online_key` in settings via `Api.online_config`), the tab works as a board on this computer only
  (`data/online-board.json` via `Api.ol_local_load/ol_local_save`).
  Setup: supabase.com project (London) → SQL Editor → run `Online setup.sql` (in the app folder, = `online.SETUP_SQL`) →
  Authentication → enable **anonymous sign-ins** → put Project URL + anon key in `online.py` defaults, rebuild, re-zip.
- Tested in Edge with a fake Supabase (scratchpad `fake_supabase.js`, `online_test.py`): 36/36 checks, including two pages live
  (join by code, live typing, typing lock, move, cursors, task assignment, delete, offline then recovery) and HTML injection stripped.
  NOT yet tested against a real Supabase project.
- **Corner buttons:** in the app (`.app`), theme and refresh now sit in the title bar left of the window buttons (top 4px,
  right 142/176px, 30px) instead of at top 66px where they covered the Notebook toolbar, the Online menu and narrow pages.
- **Pointer after reload:** the live pointer saves its position in `sessionStorage.ptr` on `pagehide` and starts there, instead of
  sitting in the top-left corner until the mouse moves. Exe built 08:12.

### Round 10b (2 Nov, 08:27): Online is live on Min's Supabase project
- Supabase org "Blackboard Buddy" (Free), project **blackboard-buddy**, ref `pbsfruwusfxwaafeypgj`, region Europe. Min holds the DB password.
- Setup SQL run in the SQL editor (now also grants table rights to `authenticated` and revokes `anon`, so it doesn't depend on
  "Automatically expose new tables"). Anonymous sign-ins switched on (Authentication → Sign In / Providers).
- `online.DEFAULT_URL/DEFAULT_KEY` = the project URL and the legacy **anon** JWT (public by design; service_role never read).
- Real-server test (scratchpad `online_real_test.py`, two separate browser profiles + an outsider): 12/12 pass: create/join by code,
  live typing + label, saved in DB across reload, live move, cursors, pasted Java, outsider reads 0 rows and its insert is refused,
  no-session REST call gets 401.
- Left behind by the test: group "Test group (delete me)" (id 02e95419-1a26-4e59-bcf5-98c38274448c) and 3 anonymous test users.
  Claude didn't hard-delete them; the clean-up query is in an Untitled query tab in the SQL editor for Min to run.
- READ ME FIRST mentions the Online tab. Exe and share zip rebuilt 08:27 (74.6 MB, checked clean).

### Round 11 (2 Nov, ~09:15): coding on the Online board ("like VS Code")
- New block **proj** (tool K, folder icon): a multi-file project. Board card shows the file list, a peek of the main file and the
  last 8 output lines. Code blocks got `▶ Run` / `Open` buttons and the same output strip. Dropping 2+ code files makes a project.
- Double-click / Open on code or proj opens the **editor overlay** (`IDE` in online.py): Monaco 0.45.0 from cdnjs (the VS Code
  editor; textarea fallback offline), explorer (new / rename on double-click / delete / ▶ = run-from file), tabs, starting
  templates (Python, Web page, JavaScript, Java, Empty), Output + Preview panel, input box for `input()` (`|` = new line),
  status bar. Ctrl+Enter / F5 run, Ctrl+S save, Esc closes.
- **Running:** Python via Pyodide 0.26.4 in a Web Worker (multi-file imports work, user-only tracebacks); JS in a worker with
  console capture; HTML projects inline their CSS/JS into a sandboxed iframe (`allow-scripts allow-modals`) whose console shows in
  Output. 30 s / 90 s timeouts and a Stop button kill the worker. Other languages: no Run, use VS Code.
- **Open in VS Code:** `Api.ol_vscode(title, files)` writes to `Documents\Blackboard Buddy projects\<name>` and opens Code.exe
  (else the folder). Paths with `..` or any anchor (`C:\`, `\x`, `C:x`) are skipped.
- **Live:** whoever opens it first edits; others get the editor read-only, updating as they type, with an Edit button once free.
  Run output is shared on the block.
- Bugs found by tests and fixed: the board's key listener stopped keys reaching Monaco (Ctrl+Enter etc.); Chromium never paints a
  sandboxed iframe created while hidden, so each preview makes a fresh frame; `/abs.py` would have escaped the export folder;
  the editor's ✕ sat under the browser-mode corner button.
- Tests (scratchpad `coding_test.py`): 21/21 incl. file:// page and two-person live; `online_test.py` 36/36 (now forces no
  server, since a real one is built in); `vs_test.py` path safety. Exe + share zip rebuilt, sources re-hidden.

### Round 12 (2 Nov, ~09:40): more coding tools in the Online editor
- **Files:** explorer is a folder tree (fold, `+` new file inside, × delete folder, double-click rename; renaming to `a/b.py` moves).
  New folder = `folder/.keep` (hidden everywhere via `isKeep`). Upload button → picker: files or a whole folder (`webkitdirectory`);
  dropping folders walks them (`webkitGetAsEntry`). Text only, ≤400 KB each, ≤1.5 MB per project, ≤200 per go; skips pictures and
  node_modules/.git/venv/build folders.
- **Board links:** a project file can be `{n, link: blockId}` (no `t`). `filesOf()` reads the text from the block each time
  (`blockText`: note → plain text, checklist → `[x] item` lines, terminal → no ANSI, link → URL). Read-only in the editor with a bar:
  Go to block / Make it a normal file. Board card shows linked files in italics. Replace-all skips links.
- **Python:** each run starts in an empty /home/pyodide; `loadPackagesFromImports` + micropip for other imports (notes in Output,
  not saved to the board); matplotlib → PNGs in Output (`[chart]` on the board); non-GUI warning hidden. Console tab (Ctrl+`)
  runs Python in the last run's globals (`_bb_repl`, 'single' mode prints values), ↑/↓ history.
- **JavaScript:** `require('./stats')` (CommonJS, cached, .json too) and `readFile(name)` between project files. Template updated.
- **Template** "Python + data": CSV + bar chart.
- **Easier coding:** Python snippets (def, forr, ifmain, csv, plot…); autocomplete offers names defined in the project's other
  files; traceback lines are links and the failing line gets a red marker; Search tab (Ctrl+Shift+F) with replace-all;
  picker for Ctrl+P (files, fuzzy), Ctrl+Shift+P / ⋯ (≈30 commands with their shortcuts); word wrap (Alt+Z), minimap, text size
  (Ctrl+= / Ctrl+-) saved in `localStorage.bbIde`; drag the panel's top edge to resize. Editor-wide keys go through `IDE.keys`
  from the board's capture-phase key handler.
- **Ask AI** (Ctrl+I / AI tab): quick buttons (explain error / file, find bugs, what next) or free text; selected lines are
  included. `Api.code_ask(history, files, current, output)` → `AI.code_help` (files cached in the first message, effort low,
  `CODE_SYSTEM`), counted in spend. Answers rendered by a small DOM-only markdown (no innerHTML) with Copy / Insert at cursor.
- `Api.ol_vscode(..., how="folder")` = "Save to computer" (opens the folder instead of VS Code).
- Tests (scratchpad): `features_test.py` 36/36, `coding_test.py` 21/21, `online_test.py` 36/36, `ai_test.py` (message shape).

### Round 13 (2 Nov, ~09:55): right-click menu on the Online board
Min asked to "right click on small little widows or open space to edit or see other options".
In `online.py`, after the ••• menu:
- **`.ol-ctx`**, one element in `.ol`, sharing the ••• menu's look (`.ol-menu,.ol-ctx` selectors).
  `ctxBuild(items)` takes a list where each entry is `{label, key, go, warn}`, `{head}`, `{rule}` or
  `{colours: fn}` (a row of the five sticky colours). A `false` entry is skipped, so the lists read as
  plain conditions. `ctxOpen(cx, cy)` puts it at the pointer and then clamps it inside the board, so a
  right-click in the very corner still shows the whole menu (tested 12 px from the corner). It is
  `max-height:calc(100% - 12px);overflow:auto` for the case where it is taller than the board.
- **On a block** (`blockItems`): the kind as a heading, then Open in the editor / Run it (code and
  projects), Edit the text or Edit the list, Add an item, Open the link, Copy the text, then Copy,
  Duplicate, Bring to the front, the colour row for stickies, Show just this one, and Delete in red.
  Right-clicking a block outside the selection selects it; right-clicking one inside the selection acts
  on all of them and the menu says "N blocks".
- **On empty space** (`spaceItems`): Put here - note, sticky, big text, code, project, checklist,
  terminal output, link, picture - then Paste here, Select everything, Show everything, Back to 100%
  and Save the board as a file. Whatever it adds lands where the click was and opens for typing.
- **Paste here** can't read a paste event, so it calls `navigator.clipboard.readText()`. If that is
  refused it says "Press Ctrl+V to paste here" rather than looking broken. It understands
  `bb-online:` copies the same way the paste handler does.
- Closes on Escape (before clearing the selection), on a click anywhere else, and on scrolling the
  board. **Scrolling with the pointer over the menu does not close it**, so a long menu can be
  scrolled; the pointer sits on the menu the instant it opens, which is why the listener is on the
  document with a `.ol-ctx` guard rather than on the board.
- **Inside text being typed** (`input, textarea, [contenteditable], .ol-ide`) the event is left alone,
  so Windows' own menu still gives copy, paste and spelling. The Monaco editor keeps its own menu.
- Test: scratchpad `ctx_test.py` (34/34, no page errors). It forces local mode the way `online_test.py`
  does - stub `window.pywebview`, a `BB` setter that blanks `BB.online`, then click `.ol-card .alt`.
  It needs `clipboard-read`/`clipboard-write` permissions for Paste here. Two notes on writing it:
  the `small` heading is upper-cased by CSS, so compare case-insensitively; and near an edge the menu
  is clamped, so don't assert it opens exactly under the pointer in both directions.
  `online_test.py`, `coding_test.py` and `features_test.py` all still pass.
- Exe built 09:58 with `build_bb9.py`, 295 files, 15.27 MB. **Share zip not rebuilt this round.**

### Round 14 (2 Nov, ~10:20): joining lines, boxes, find, lining up, locking, ready-made layouts
Min asked to "connect notes and codes and other stuff with a line or other illustrations", then for
"other cool and useful stuff" in the Online tab. All of it is in `online.py`.

**Lines between blocks.** The `arrow` kind now carries its own look in `data`: `ends`
(`to` / `both` / `none` / `from`), `form` (`curve` / `straight` / `elbow`), `dash`, `thick`, `color`
and `label`. `linkPath(a, c, form)` returns the path **and** the point halfway along it - for a
curve that is the real midpoint of the quadratic, `(p1 + 2q + p2) / 4`, not the midpoint of the
bounding box. `drawLinks` draws the label as an SVG `<text>` with a `<rect>` of background behind it,
sized from `getBBox()` after the text is in the document. One marker (`#ol-head`) serves both ends,
because it is `orient="auto-start-reverse"`, and it is `fill="context-stroke"` so a coloured line
gets a matching head for free.

**Starting a line without the tool.** Right-click a block → "Draw a line from here", then click the
other block; a line follows the mouse in between (`linkFrom`, `startLink`, `stopLink`, `linkClick`,
and a `pointermove` on the board). The pointerdown handler checks `linkFrom` first and swallows that
click. Esc stops it.

**Boxes and circles** - the new `shape` kind (tool O): `data.form` box/ellipse, `color`, `fill`,
`dash` and a name in `data.text` (the generic `.ol-c` edit path, so no new editing code). Drawn with
`::after` so there is no real box in the way, and **`pointer-events:none` on the block with four
`.ol-srim` strips** down the sides - that way the inside of a frame stays usable: you can still drag
blocks in it and right-click bare board through it. New shapes go to the back (`toBack`, which walks
`z` down past 0; `.ol-world` is transformed, so a negative `z` stays inside it and does not fall
behind the board).

**Find on the board** (Ctrl+F, `.ol-find`): `searchable(b)` gathers whatever text a block holds -
title, text, label, url, note HTML as plain text, checklist items, project file names - and Enter
walks the matches, centring each without changing the zoom. The key is caught **before** the
"are we typing" bail-out, or it never fires; and because the box is a real input, Ctrl+A inside it
does not select every block.

**Lining things up:** `tidy(ids, how)` - left / top / middle / centre / right / bottom, and
`row` / `column` which spread the selection out with an even 28 px gap. It leaves out arrows (they
follow what they join) and locked blocks. Shows in the block menu only when more than one is picked.

**Locking:** `data.lock`. `locked(b)` guards both `startEdit` and the move-drag (the block still
selects, it just will not move), the resize handle is hidden and a padlock shows in the corner.

**Ready-made layouts:** `LAYOUTS` + `dropLayout(key, p)` - "To do · Doing · Done" (three frames,
two stickies) and "Revision plan" (a shaded frame, a checklist, a note, a sticky, joined with a
straight line and a dashed right-angled one labelled "then"). In the right-click menu on empty space
and in the ••• menu. They double as a demonstration of the new pieces.

**Testing:** scratchpad `lines_test.py`, 50/50, no page errors. `online_test.py`, `coding_test.py`,
`features_test.py` and `ctx_test.py` all still pass. Three things worth knowing if you extend it:
- To click a line in a test, **do not use the hit path's bounding-box centre** - on a curve that
  point is not on the curve. Use `getPointAtLength(len / 2)` and map it with `getScreenCTM()`.
- Menu labels carry a ✓, so run the tests with `PYTHONIOENCODING=utf-8` or printing dies on cp1252.
- Once a few blocks are down, a fixed "empty space" coordinate is no longer empty. `lines_test.py`
  has `rclick_bare()`, which scans with `elementFromPoint` for board with nothing on it.
- `window.bbOnline` now also exposes `paint, place, put, drawLinks, startEdit, tidy, dropLayout`.

Exe built 10:19 with `build_bb9.py`, 295 files, 15.27 MB. **Share zip is now two rounds behind.**
Left on Min's board: four blocks from checking it in the real app (two notes, a line, a frame) -
deleting them needed a permission Claude does not have, so Min can select them and press Delete.

### Round 15 (2 Nov, ~10:55): sharp zoom, renaming, groups that move together, drag-select, board switcher

**Blurry text when zoomed in - found by measuring, not guessing.** `zoomAt` leaves `S.view.x/y` on a
fraction of a pixel (it works out `sx - (sx - v.x) * nk / v.k`), and a fractional `translate()` makes
the browser resample every glyph. Measured with scratchpad `blur_probe3.py`, which screenshots a slab
of text and averages the step between neighbouring pixels - crisp edges jump in one pixel, a resampled
one smears over several:

| offset | at 2.5x | at 2.0x |
|---|---|---|
| whole pixels | 14.22 | 15.85 |
| half a pixel off | 12.81 | 12.66 |

**Fix:** `applyView` rounds the translate to whole **device** pixels (`Math.round(n * dpr) / dpr`),
while `S.view` keeps the exact value so panning and zooming still add up. After it, the fractional
cases measure the same as the whole-pixel ones. Confirmed in the real app over CDP: a view set to
`-321.37, -205.81` draws as `matrix(2.4, 0, 0, 2.4, -321, -206)`.
`will-change: transform` was **not** the cause (toggling it changed nothing measurable), but text in a
layer the browser holds ready to move loses some sharpening, so it is now only on while the board is
actually moving - `.ol-world.moving`, added in `applyView` and taken off 180 ms after the last change.
Things that did *not* turn out to matter: the device scale factor (tried 1, 1.5 and 2).

**Renaming** - `TITLED` says which kinds carry a name and where it lives (`title` for code, project,
terminal, checklist and link; `text` for a box). "Rename…" in the right-click menu, or **F2** with one
block picked. A note or sticky has no separate name - its text is its content - and the menu says so
rather than silently overwriting it.

**A box moves what is in it.** `inside(shape)` is every block whose centre is within it;
`withInsides(ids)` widens the dragged set, so picking a frame up takes its contents along and leaves
everything else. Locked blocks and lines are left out (lines follow what they join anyway).

**Drag to pick out several.** Dragging bare board with the select tool now draws the marquee; Shift
still adds to what is already picked. Moving the board is the hand tool, the middle button, Alt-drag,
or **holding space** (`spaceDown`, with `.ol-board.grab`; a `blur` listener clears it, or the hand
sticks after alt-tabbing). The empty-board hint and the two tool tooltips were reworded to match.

**Switching board.** `myGroups()` already returned every group you are in, so joining several always
worked - what was missing was the switch. The group name in the top bar is now a button
(`showBoards`) listing **On this computer** first, then every group with its code, a tick on the one
you are on, and a way to join or start another. It reuses the right-click menu's `ctxBuild`/`ctxOpen`.
It signs in on demand (`knownGroups` calls `connect()` if needed) and, when the server cannot be
reached, still lists the offline board and says so instead of failing.

**On "a separate offline version":** read as *the offline board is one of the boards you can switch
to*, which is what was built - `useLocal()` keeps it in `data/online-board.json`, quite separate from
any group, and it is now always one click away. If what was wanted is a **build of the app with no
online features at all**, that is a different job and has not been done.

**Testing:** scratchpad `board2_test.py`, 21/21 - including the sharpness measurement as a standing
test, that a frame carries its contents while an outside block stays put, and that space-drag still
pans. All five other suites still pass (`online_test`, `coding_test`, `features_test`, `ctx_test`,
`lines_test`). Note for anyone extending it: `.ol-h` headers are upper-cased by CSS, so `inner_text`
gives "GRAPH SEARCH" - compare case-insensitively, the same trap as the menu headings.

Exe built 10:57 with `build_bb9.py`, 295 files, 15.28 MB. **Share zip is now three rounds behind.**

## Round 16 - the switcher made visible, writing correction, and the window

**The board switcher is a real control now.** The group name in the top bar became `.ol-switch`: a
bordered pill holding the state dot, the board's name and a `▾`, which inverts while its menu is
open. `aria-haspopup="menu"`, `aria-expanded` and an `aria-label` on the button; `role="menu"` on
`.ol-ctx` and `role="menuitem"` on its items. **Ctrl+K** opens it from anywhere on the board, arrow
keys walk the list, Escape closes it and hands focus back. The offline board reads **Offline board /
only here**, and the three different situations now say different things: "You are not in a group
yet", "Can't reach the server", "No group server set up yet" (the last one offers to set one up
rather than to join).

*Two traps worth knowing.* The app draws its own pointer, so `cursor` is `none` everywhere - never
assert `cursor: pointer` in a test. And the board's window-level keydown handler runs in the capture
phase and stops everything that is not Escape, so a listener on a menu inside it never fires. The
menu's keys now live in `ctxKeys(ev)`, which that handler calls first while a menu is open.

**Writing correction** (`Writing` in `online.py`, exported as `window.bbWriting`). It never guesses:
it only changes words it has been told are wrong, so a name, a module or a bit of jargon it has never
met is left exactly as typed. About 230 known misspellings - ordinary ones, missing apostrophes
(`dont`, `im`, `youre`) and the ones that come up writing code (`retrun`, `lenght`, `funtion`) - plus
six rules: the same word twice, a lone `i`, a space before a comma, two spaces, and a sentence
starting small. **UK spelling is deliberately safe**: `colour`, `realise`, `behaviour` and the rest
are not in the list and never will be.

- **As you type**: in a note, sticky, big text, box name or checklist, finishing a known typo with a
  space fixes it, through `document.execCommand('insertText')` so **Ctrl+Z still takes it back**.
  Known typos get a wavy underline through the CSS Custom Highlight API (`::highlight(bb-typo)`),
  which touches no DOM and so cannot disturb the caret. Off switch: right-click the board →
  "Fix typos as I type", kept in `localStorage` under `bbFixTyping`.
- **On demand**: **F7**, or right-click → "Check the writing…" on one block, or "Check the writing on
  the board…" on empty space. The `.ol-fix` panel lists each find with how many times it happens, a
  Fix for each and a Fix everything.
- **A note keeps its formatting.** Its words live inside html, so `overText` stitches the text
  between the tags together with `\u0001`, fixes it in one go and puts it back. That seam is why the
  "capital after a full stop" rule anchors on `^\u0001*` - without it every tag would start a new
  sentence and `<i>definately</i>` came back as `<i>Definitely</i>`.
- Code, projects and terminal output are deliberately out of scope (`FIELD` has no entry for them).

**Found in passing:** the revision layout built its checklist items with `{text: …}` while everything
else uses `{t: …}`, so those three items came up blank. Fixed.

**Still true and still odd:** a *big text* block stores what `innerText` gives back, and CSS
upper-cases that kind, so its text is saved shouting. Harmless today, but it means the original
casing is gone for good - worth deciding on before anything else reads that field.

**The window.** Two things the app was getting wrong on Windows:
- *It opened wherever Windows felt like.* `centre_window()` in `app.py` now puts it in the middle of
  the work area of the monitor **the mouse is on**, called from `_restore_resizing` once the window
  exists, and shrinks it to fit a screen smaller than itself.
- *Windows' own Snap did nothing.* The page was moving the window itself with `win_set_bounds`, so
  Windows never saw a drag and none of its snapping could fire. The title strip now hands the drag
  over (`win_drag` → `WM_NCLBUTTONDOWN` with `HTCAPTION`), which gives back the zones at the screen
  edges, Snap Assist and drag-to-the-top-to-maximise. It only starts once the mouse has moved 4 px,
  or a double-click to maximise would be swallowed. The maximise button no longer keeps its own count
  of the window state - `win_state` asks Windows (`IsZoomed`), and the page re-reads it on resize, so
  snapping from outside the app leaves the button telling the truth. The app's own layout menu gained
  a `⊞` button that opens **Windows' own chooser** (Win+Z).

**Testing:** `write_test.py`, 40/40 - that it fixes what it knows, leaves UK spelling and ordinary
words alone, keeps a note's bold and italic, fixes a checklist item, refuses to touch code, fixes as
you type and stops when switched off. All seven other suites still pass: `switch_test` 26/26,
`ctx_test` 34/34, `lines_test` 50/50, `board2_test` 21/21, `vault_test` 51/51, and `online_test`,
`coding_test`, `features_test`. `board2_test` and `switch_test` needed their own wording updated for
the new switcher, not the code.

The window work is **not** covered by a test - it is all native Windows behaviour. It needs trying in
the real app: open it and see where it lands, then drag the strip to the top and to a side.

## Round 17 - the window, moving about the board, groups, and never losing work

**Dragging the window back - and Windows' own snapping with it.** Round 16 replaced pywebview's
drag region with a `win_drag` call into the app, which the installed exe did not have yet, so the
window could not be moved at all. Three things were wrong underneath, and all three are worth
knowing:
1. `WM_NCLBUTTONDOWN` with `HTCAPTION` cannot work from the API thread - `ReleaseCapture` only
   releases the *calling* thread's capture, and the capture belongs to the browser.
2. `WM_SYSCOMMAND` / `SC_MOVE` is dropped by WinForms itself on a window with no border of its own,
   whatever style bits it has. (`WS_SYSMENU` was missing too and is now set alongside
   `WS_THICKFRAME`, `WS_MAXIMIZEBOX` and `WS_MINIMIZEBOX`, but it was not the cause.)
3. What does work is **WebView2's own draggable regions**: `app-region: drag` on `.titlebar`,
   `no-drag` on its buttons, which are off unless the runtime is asked for them -
   `--enable-features=msWebView2EnableDraggableRegions`, added in `run()`. The browser then hands
   the drag to the window and Windows does the rest.

   `CSS.supports('app-region', 'drag')` is **true whether or not the engine acts on it**, so it is
   useless as a test. The app says which mode it is in (`win_drag_mode` -> `browser` / `app`) and
   the page starts in `app` mode - moving the window itself, which always works - and only steps
   aside once the app says `browser`. The double-click to maximise is handed over with it, or both
   sides would fire and undo each other.

   Measured in the real app: a 420 px sideways drag moves it 420 px, and dragging to the top edge
   maximises it (`2000,500 -> -7,-7`, `IsZoomed` true) - that is Windows' own snap, which is what
   brings the layouts bar with it. The window buttons still work (`no-drag`).

**Opening centred needed a wait.** Centring as soon as the window exists lands it off by however
much WinForms resizes it afterwards (measured: off by 83, -128). `_centre_when_settled` watches the
size until it stops changing, then centres. Measured after: off by 0, 0.

**The layout menu was appearing on hover** and covering the page. It now opens only on purpose:
holding the maximise button for 550 ms, or right-clicking it. A press that opens the menu no longer
also maximises.

**Moving about the board.** Left-drag on bare board draws a selection box (as it always did); the
**right button dragged** moves the board. A right-click that does not move still opens the menu -
`rightFrom` records where the button went down, the window-level `pointermove` only starts panning
past 5 px, and `contextmenu` swallows the menu when it moved. No pointer capture is taken, so a
right-click still lands on whatever it was over.

**Boxes.** Picking the box or circle tool now **draws one out at the size you want**; a plain click
still gives the usual 400x280. (`drag.kind === 'make'`, using the same marquee rectangle.) A box
also offers **Tidy what is in here** - `tidyInside` lays its contents out in rows inside it, in the
order they are already in, clearing the box's own name and growing the box if they will not fit -
and **Pick out what is in here**.

**Saving, and not losing it.** Three layers:
- Every change is **mirrored into browser storage the instant it happens** (`Local.mirror`), and the
  file is written a moment later. On the way back in, `Local.load` compares the mirror's timestamp
  against the file's (`ol_local_saved_at`) and takes the mirror when it is newer - which is what
  makes closing the app a quarter-second after typing safe.
- `saveEverything()` on `pagehide`, `beforeunload`, `blur`, on losing the screen, and every two
  minutes. `Sync.flushAll()` sends everything a group board still has waiting.
- **A copy on this computer, for both boards**, in `data/backups`: `<name>-latest.json` always, plus
  dated copies no closer than five minutes apart, twenty kept per board. The group board lives on
  the server, so this is the only thing on the laptop if the server goes away. Right-click the board
  for "Back one up now" and "Put back an older copy…". Putting back a **group** copy opens it on the
  offline board instead of writing over what the group has.
- Both the board file and the backups are written through `_write_safely` (write beside, then
  `os.replace`), so a copy killed mid-write cannot leave half a board behind.

**Deliberately not done:** `window.events.closing` is **not** wired to `save_on_close`. That event
is raised on the window's own thread and `evaluate_js` waits for that same thread, so the app would
hang on the way out. The mirror covers the same gap and is tested. `save_on_close` is left in place
for a caller with a thread of its own, with the warning on it.

**Testing:** two new suites in the project folder - `group_test.py` 16/16 (looking about, drawing a
box out, tidying what is in one) and `save_test.py` 18/18 (the mirror beating the file, recovery on
the next open, a stale mirror *not* winning, the backups, putting one back, right-drag panning).
All nine other suites pass. The window work is checked by scripts in the scratchpad that drive the
real app with real mouse input: `realdrag.py`, `realdrag2.py`, `btncheck.py`, `where.py`.


## Round 18 - dressing blocks up, the little map, finding things, and boxes that size themselves

**How a block looks.** Every block carries its own look in its data, so it travels with the block
and survives a save. `dress(b, d)` in `online.py` turns that data into CSS variables on the node
(`--tf` font, `--tc` writing colour, `--bgc` colour behind it, `--fs2` size, `--caps`), and the
stylesheet lays those over the usual look of each kind. `lookMenu(ids, at)` is the menu behind
**How it looks...** (Ctrl L) in a block's own menu: bigger and smaller text, five fonts, bold,
slanted, ALL CAPITALS for big text, left/centre/right, the colour of the writing, the colour
behind it, the colour of a sticky, and "Back to how it started". It works on however many blocks
are picked out at once.

**More colours everywhere.** `PAL` is now 24 (a bright row, the same hues deeper, then greys),
`STICKY` 12, and a new `PAPER` 12 of soft paper shades for what sits behind writing. The palette
rows wrap, so they still fit the menu.

**Lines remember.** A line's menu has **Use this look for new lines**, which keeps its ends, its
shape, dashed and colour in `olLine`; `add('arrow', ...)` starts from that. There is a way back.

**Far out, blocks keep their shape.** Below 45% the board gets a `far` class: each block hides its
contents and shows its name instead, at `calc(13px / var(--k))` so the name is always about 13px on
screen however far out you are. The name is clipped to three lines inside the block, a to-do shows
what is still to do, code shows its first line, and a box stays an outlined box with its name at the
top rather than turning into a blank white card. The earlier measurement is why this exists: a PIL
sharpness probe across five zoom levels with and without a `will-change` layer hint gave identical
numbers, so the text was not being rendered badly, it was simply too small to read - no rendering
tweak could have helped.

**The little map.** `.ol-map` is a 260x170 canvas in the bottom-left corner drawing every block and
a frame round the part you are looking at. Click or drag it to go there. **M** puts it up and away,
and it is remembered in `olMap`.

**One search box, in the corner.** The old bar that dropped from the top is gone; `.ol-hunt` sits
in the corner and is always there. Typing searches as you go, Enter walks the matches, Escape
clears it, Ctrl F puts the cursor in it. It now looks inside checklists too - those lines are kept
under `t`, and the search was reading `text`, so it had never found one.

**Boxes size themselves.** `needs(shape)` is how big a box has to be to hold what is in it,
`growToFit(shape)` lets it out to that (never pulls it in, so things put side by side stay put),
and `regrowBoxes(ids)` runs at the end of a drag over every box that gained something. Dragging a
block into a box grows the box round it, and the resize handle will not pull a box in smaller than
its contents. Tidying uses the same path.

**On GitHub.** The folder is a git repository now, `main`, one commit. `.gitignore` keeps `.env`,
`data/`, `Notebook/`, `output/`, `_internal/` and the built exe on this laptop - only source goes
up. `upload.py` commits and pushes in one go, refuses outright if a `.env` ever reaches the staged
list, and `build_bb9.py` calls it after every build, so a build is an upload. It needs a remote:
make the repository on GitHub, then `git remote add origin ...` once.

**Testing:** a new suite, `far_test.py` 24/24 - the far-out view (names shown, contents out of the
way, size on screen, a box still a box), the map (drawn, the frame, clicking to travel, M), the
corner search (including checklists), and boxes sizing themselves both ways. All eight suites pass:
`vault_test` 51, `ctx_test` 34, `lines_test` 50, `board2_test` 21, `write_test` 40, `group_test` 16,
`save_test` 18, `far_test` 24.

### Round 19 (3 Nov, 00:06): pointer while busy; longer background-check options
- **Pointer bug:** while the app was busy (sync / writing notes) `html.busy .pointer-ring` ran `animation: spin`, whose
  `transform: rotate()` replaced the inline `transform` the pointer script uses to put the ring on the mouse, so the ring
  jumped to the window's top-left and spun there (likely also the old "cursor goes to a corner on refresh"). The arc is now a
  `::after` with its own `ptr-spin` animation; the ring keeps following. Test: scratchpad `busy_ptr_test.py`
  (light + dark, idle + busy, ring and dot within 3 px of the mouse).
- **Check Blackboard in the background** now also offers 12 hours, once a day, every 2 / 3 days, once a week
  (`sync_hours` 12/24/48/72/168; `background_loop` already handles any number).
- Testing note: a running app rewrites `output/dashboard.html` from its own (older) code after a sync, so browser tests
  should use a copy of the freshly built page.
