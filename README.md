# Blackboard Buddy

Your UWE Blackboard in one clean app: this week's plan, timetable, deadlines,
announcements, AI assignment plans, AI study notes and grades.

## Open it

Double-click **Blackboard Buddy.exe**. Tip: right-click it → **Pin to Start** or
**Send to → Desktop (create shortcut)**.

- It checks Blackboard quietly in the background whenever it's been more than 6 hours.
- Press **↻** (top right) to check now. A bubble at the top shows what it's doing.
- **◐** changes the background.
- If Microsoft needs you to sign in again, a sign-in window pops up. Sign in, and it carries on.

Keyboard: `1`–`7` or the arrow keys switch tabs.

## Files

| What | Where |
|---|---|
| Study notes and assignment plans | `output\notes`, `output\plans` (Markdown, opens in any editor) |
| Calendar of deadlines | Deadlines tab → "Add to calendar" |
| Your Claude API key and settings | `.env` (keep it private) |

Don't delete the `_internal` folder; the app needs it.

## Cost

Each refresh only pays for new material. Same-day refreshes with nothing new are free.
Check your balance at platform.claude.com → Billing.

## Settings (`.env`)

`LOOKAHEAD_DAYS`, `NOTES_DAYS`, `NOTES_PER_RUN`, `PLANS_PER_RUN`, `AI_EFFORT`.

## Privacy

Your sign-in is stored only on this PC (in `%LOCALAPPDATA%\BlackboardBuddy`). Course
material is sent only to the Claude API to make notes. Your password is never stored.

## For developers

Source files are hidden in this folder (`app.py`, `main.py`, `blackboard.py`, `ai.py`).
Rebuild with `pip install -r requirements.txt` then PyInstaller (`--windowed --icon icon.ico app.py`).
