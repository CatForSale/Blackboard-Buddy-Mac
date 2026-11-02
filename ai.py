"""Claude-powered summaries, plans and study notes."""

import base64
import io
import json

import anthropic

MODEL = "claude-opus-5-5"

# The models Min can pick in Settings. Prices are US dollars per million tokens:
# (input, output, cache read). A cheaper model costs less but writes shorter, blunter notes.
MODELS = {
    "claude-opus-5-5":   {"label": "Opus 5.5",   "note": "Best writing. The default.",          "in": 4.00,  "out": 20.00, "cache": 0.20},
    "claude-sonnet-5-5": {"label": "Sonnet 5.5", "note": "Half the price, still strong.",       "in": 2.00,  "out": 10.00, "cache": 0.20},
    "claude-haiku-5-5":  {"label": "Haiku 5.5",  "note": "Cheapest and fastest.",               "in": 0.10,  "out": 0.50,  "cache": 0.10},
    "claude-fable-5-1":  {"label": "Fable 5.1",  "note": "The most capable, and the dearest.",  "in": 10.00, "out": 50.00, "cache": 1.00},
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

SYSTEM = (
    "You are a study assistant for a university student. "
    "You work from material pulled from their Blackboard account. "
    "Write in clear, friendly British English, in Markdown. Be concrete and practical: "
    "name the actual modules, deadlines and tasks. Never invent deadlines, marks or "
    "requirements that are not in the material; if something is unclear, say so and "
    "tell the student to check with the module leader."
)

CODE_SYSTEM = (
    "You help a university student with code in a group project they share on a whiteboard app. "
    "Answer briefly and concretely, in plain British English. When code should change, show only the changed part "
    "in a fenced code block with its language, and say which file it goes in. No intro, no filler. "
    "Where the code runs: Python runs in the browser with Pyodide (numpy, pandas and matplotlib work, other "
    "pure-Python packages install themselves; no sockets, threads or subprocess; the only files are the project's). "
    "JavaScript runs in a Web Worker with console.log, require('./file') and readFile('name'). Web pages run in a "
    "sandboxed frame. Other languages run in VS Code on the student's computer."
)


class AI:
    def __init__(self, effort="medium", model=None, api_key=None):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.effort = effort if effort in EFFORTS else "medium"
        self.model = model if model in MODELS else MODEL
        self.cost = 0.0  # US dollars spent by this run
        self.tokens_in = 0
        self.tokens_out = 0

    def _track(self, usage):
        price = MODELS[self.model]
        read = usage.cache_read_input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        fresh = usage.input_tokens + written  # writing to the cache is charged at the input rate
        self.tokens_in += fresh + read
        self.tokens_out += usage.output_tokens
        self.cost += (fresh * price["in"] + read * price["cache"] + usage.output_tokens * price["out"]) / 1_000_000

    def _ask(self, content, max_tokens=16000, messages=None, system=SYSTEM):
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=messages or [{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        self._track(msg.usage)
        if msg.stop_reason == "refusal":
            return "_Claude declined to process this item._"
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if msg.stop_reason == "max_tokens":
            text += "\n\n_(Cut short: output limit reached.)_"
        return text

    # ---------- features ----------

    def weekly_briefing(self, today, deadlines, announcements, grades, calendar):
        payload = json.dumps({
            "today": today, "upcoming_deadlines": deadlines,
            "recent_announcements": announcements, "recently_released_grades": grades,
            "calendar": calendar,
        }, indent=1, default=str)
        return self._ask(
            "Here is everything from my Blackboard for the coming weeks:\n\n"
            f"```json\n{payload}\n```\n\n"
            "Write my weekly briefing. Keep it short and scannable: under 200 words in total, "
            "no intro or sign-off, no explanations, fragments are fine. Professional tone: "
            "no slang, jokes, emoji or casual asides. Use exactly these headings:\n"
            "1. **Key Priorities**: 3 bullets, each under 12 words.\n"
            "2. **Deadlines**: a table of what's due in the next 14 days "
            "(module, task, due, days left), most urgent first. Short names.\n"
            "3. **Updates**: only announcements that change something I do; one line each, "
            "at most 4.\n"
            "4. **Schedule**: one line per day for the next 7 days, under 8 words each; "
            "skip empty days.\n"
            "5. **Points to Note**: at most 3 bullets, each under 12 words.")

    def assignment_plan(self, course_name, assignment, brief_blocks, today):
        intro = (
            f"Module: {course_name}\nToday: {today}\n"
            f"Assignment details from Blackboard:\n```json\n"
            f"{json.dumps(assignment, indent=1, default=str)}\n```\n"
            "Any attached brief documents follow.")
        content = [{"type": "text", "text": intro}, *brief_blocks, {"type": "text", "text": (
            "Make me an assignment plan with these sections:\n"
            "1. **What's being asked**: in plain English.\n"
            "2. **Requirements checklist**: word count, format, referencing style, "
            "submission method, learning outcomes and marking criteria, as `- [ ]` items.\n"
            "3. **Step-by-step plan**: steps with suggested dates working back from "
            "the deadline, leaving buffer time.\n"
            "4. **How to score highly**: based on the marking criteria if given.\n"
            "5. **Starting points**: an outline or structure and research directions.\n"
            "6. **Questions to ask the tutor**: anything ambiguous.")}]
        return self._ask(content)

    def study_notes(self, course_name, item_path, doc_blocks):
        content = [
            {"type": "text", "text": f"Module: {course_name}\nMaterial: {item_path}\n"
                                     "The course material follows."},
            *doc_blocks,
            {"type": "text", "text": (
                "Turn this into revision notes with these sections:\n"
                "1. **Summary**: one paragraph.\n"
                "2. **Key concepts**: headings with clear explanations, formulas and "
                "definitions; keep any examples from the material.\n"
                "3. **Flashcards**: 8-15 cards as a Markdown table with Question and "
                "Answer columns.\n"
                "4. **Practice questions**: 5 exam-style questions.\n"
                "5. **Model answers**: short answers to those questions.\n"
                "6. **Further reading**: only things the material itself mentions.")},
        ]
        return self._ask(content)

    def note_topic(self, title, notes_md):
        """A few words saying what a set of notes covers, for the note map."""
        text = self._ask(
            f"These are revision notes made from a Blackboard item called {title!r}:\n\n{notes_md[:12000]}\n\n"
            "Write a title of 2 to 6 words that tells a student what these notes are about, like "
            "\"Particle swarm optimisation\" or \"Bakery sales prediction brief\". Name the topic, not the "
            "file, the module or the week, and don't say \"revision notes\". Reply with the title only.",
            max_tokens=40, system="You write short, specific titles for study notes.")
        return text.splitlines()[0].strip().strip("\"'*#. ")[:60] if text else ""

    def grade_feedback(self, grades):
        return self._ask(
            "These are my released marks and tutor feedback:\n\n"
            f"```json\n{json.dumps(grades, indent=1, default=str)}\n```\n\n"
            "Summarise how I'm doing per module, what the feedback says I do well, "
            "and the top 3-5 concrete things to improve next time. Keep it encouraging.")

    def chat(self, history, context_json):
        """A conversation about the student's Blackboard data. The data block is cached, so
        follow-up questions cost a fraction of the first."""
        first = {"role": "user", "content": [
            {"type": "text", "text": f"My Blackboard data, for reference:\n```json\n{context_json}\n```",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": history[0]["content"]}]}
        return self._ask(None, max_tokens=4000, messages=[first, *history[1:]], system=SYSTEM + (
            " You are answering questions inside the student's Blackboard Buddy app. Answer concisely "
            "and in a professional tone; use short lists and tables where they help."))

    def code_help(self, history, files, current="", output=""):
        """Questions about a code project in the Online tab's editor. The files are cached like chat()'s data."""
        project = "\n\n".join(f"### {f.get('n')}\n```\n{str(f.get('t', ''))[:20000]}\n```" for f in files[:40])[:80000]
        extra = f"\n\nOpen file: {current}" if current else ""
        if output:
            extra += f"\n\nOutput of the last run:\n```\n{output[-4000:]}\n```"
        first = {"role": "user", "content": [
            {"type": "text", "text": f"The project's files:\n\n{project or '(no files yet)'}", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": history[0]["content"] + (extra if len(history) == 1 else "")}]}
        if len(history) > 1:
            history = [*history[:-1], {"role": "user", "content": history[-1]["content"] + extra}]
        return self._ask(None, max_tokens=4000, messages=[first, *history[1:]], system=CODE_SYSTEM)

    def answer(self, question, context_json):
        return self._ask(
            "Here is my Blackboard data:\n\n"
            f"```json\n{context_json}\n```\n\nQuestion: {question}")


# ---------- turning downloaded files into Claude content blocks ----------

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 150_000


def file_to_blocks(file_name, data):
    """Converts a downloaded file into content blocks Claude can read, or [] if unsupported."""
    name = file_name.lower()
    try:
        if name.endswith(".pdf") and len(data) <= MAX_PDF_BYTES:
            return [{"type": "document", "title": file_name, "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.b64encode(data).decode()}}]
        text = None
        if name.endswith(".pptx"):
            from pptx import Presentation
            prs = Presentation(io.BytesIO(data))
            slides = []
            for i, slide in enumerate(prs.slides, 1):
                parts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
                if slide.has_notes_slide:
                    parts.append("Speaker notes: " + slide.notes_slide.notes_text_frame.text)
                slides.append(f"--- Slide {i} ---\n" + "\n".join(p for p in parts if p.strip()))
            text = "\n\n".join(slides)
        elif name.endswith(".docx"):
            from docx import Document
            text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
        elif name.endswith((".txt", ".md")):
            text = data.decode("utf-8", errors="replace")
        if text and text.strip():
            return [{"type": "text", "text": f"=== {file_name} ===\n{text[:MAX_TEXT_CHARS]}"}]
    except Exception:
        pass
    return []
