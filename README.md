<div align="center">

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
·  ███  ███ 
·  █  █ █  █   B L A C K B O A R D
·  ███  ███
·  █  █ █  █   B U D D Y
·  ███  ███
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

### Your whole degree. One window.

A desktop app that reads your university Blackboard,
writes your notes, and gets you to actually revise them.

`Windows` · `macOS` · `v1.2.0`

</div>

---

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   01 — WHY
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

Blackboard knows everything about your course and tells you none of it.
Deadlines sit three clicks deep. Lecture slides pile up unread.
Nothing connects to anything.

This app signs in, reads all of it, and gives it back to you as
one page you'd actually want to open.

<br>

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   02 — WHAT IT DOES
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

### This week

Everything due, everything new, in the order it matters.
Open it in the morning and you know what today is.

### Notes that write themselves

New lecture material becomes a study note, in plain English,
in your words rather than the slide's.
Assignments become a plan with dates on it.

### Study

Your notes turn into cards. Cards turn into revision.

| | |
|---|---|
| **Spaced repetition** | Cards come back exactly when you're about to forget them |
| **Quiz** | Multiple choice, against the clock |
| **Match** | Six pairs, best time per week |
| **Type it** | Write the term. Near enough counts |
| **Blurt** | Empty the page from memory, then see what you missed |
| **Focus** | 25 on, 5 off, and it keeps count |

XP, levels, daily challenges and twelve badges, because a streak
is a better reason to open an app than guilt is.

### The board

An infinite canvas. Notes, sticky notes, checklists, code that runs,
terminal output, pictures, links, drawing, and boxes to hold it all.
Works on its own, or shared with your group in real time.

### Notebook

A vault for everything that isn't Blackboard's.
Markdown, folders, search, and a map of how your notes link up.

<br>

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   03 — GETTING IT RUNNING
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

Double-click **Blackboard Buddy.exe**. That's the install.

On first run it asks for three things: your name, your university's
Blackboard address, and a Claude API key from
[console.anthropic.com](https://console.anthropic.com).

Then it signs you in through your own browser — the app never sees
your password — and reads your modules.

```
↻          check Blackboard now
◐          light or dark
1 – 7      jump between tabs
```

It checks on its own every few hours. You can set that to anything
from 15 minutes to once a week.

<br>

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   04 — WHERE YOUR THINGS LIVE
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

| | |
|---|---|
| Notes and plans | `output/notes`, `output/plans` — Markdown, open them anywhere |
| Boards, cards, profile | `data/` |
| Backups | `data/backups` — kept automatically, twenty deep |
| Your key and settings | `.env` — never leaves this computer |
| The app itself | `_internal/` — leave it alone |

<br>

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   05 — PRIVACY
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

Your sign-in stays on this computer.
Your password is never stored, never typed into this app.
Course material goes to the Claude API to be turned into notes,
and nowhere else. No account, no telemetry, no server of ours.

<br>

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   06 — COST
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

A refresh only pays for material it hasn't read before.
Nothing new means nothing spent.
`AI_EFFORT` in `.env` trades money for depth: `low`, `medium`, `high`.

<br>

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
   07 — SPECIFICATION
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

| | |
|---|---|
| Language | Python 3.12+ |
| Window | pywebview — WebView2 on Windows, WebKit on macOS |
| Interface | HTML, CSS and JavaScript generated from Python. No build step |
| Intelligence | Claude, via the Anthropic API |
| Shared boards | Supabase — realtime, row-level security |
| Packaging | PyInstaller |
| Platforms | Windows 10/11, macOS 13+ (Apple silicon and Intel) |

```
pip install -r requirements.txt
python app.py              # run it
python build_bb9.py        # build and install it, Windows
python build_mac.py        # build it, macOS only
```

Source files ship hidden on disk. Un-hide one before editing:

```python
import ctypes; ctypes.windll.kernel32.SetFileAttributesW("main.py", 0x80)
```

`HANDOFF.md` is the long version — every round of work, what changed and why.

<br>

<div align="center">

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

Built for a Computer Science degree at UWE Bristol,
then built properly.

```
· · · · · · · · · · · · · · · · · · · · · · · · · ·
```

</div>
