"""Saved notes, read back as structure instead of a wall of text.

The summariser asks the model for four fixed sections (see ``_PROMPT`` in
:mod:`summarizer`) and :func:`summarizer.to_plain_text` renders them with
uppercase headings, ``  - `` bullets and ``  [ ] `` task boxes. That shape is
stable enough to read back, and reading it back is what lets the window show a
meeting as a summary, a list of decisions and a list of owned actions rather
than as a paragraph nobody scans.

Two formats, because a notes folder outlives a release:

* the plain text written today  — ``ACTION ITEMS`` / ``  [ ] ...``
* the Markdown written before it — ``## Action Items`` / ``- [ ] ...``

No tkinter in here. The window is one caller; the point of keeping this
separate is that the parsing can be tested without one, and that anything else
which grows a need to read a saved meeting (a meetings list, an export) gets
the same answer as the window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

#: Headings we recognise, however they were written. The key is what the
#: section is called here; the values are matched case-insensitively against
#: the heading text with punctuation stripped.
_SECTIONS = {
    "summary": ("summary",),
    "points": ("key discussion points", "discussion points", "key points",
               "topics discussed"),
    "decisions": ("decisions", "decisions made"),
    "actions": ("action items", "actions", "next steps", "tasks"),
}

#: A heading in either format: "## Action Items", "ACTION ITEMS", "Action Items:".
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*$")

_BULLET = re.compile(r"^\s*[-*+•]\s+(.*)$")
_TASK = re.compile(r"^\s*(?:[-*+]\s+)?\[([ xX])\]\s*(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")

#: "what — who — when". The prompt asks for an em dash and small models oblige
#: most of the time; the spaced hyphen is the common near miss. Never a bare
#: hyphen, which would cut "re-run the installer" in half.
_PART = re.compile(r"\s+[—–]\s+|\s+-\s+")

#: The model was told to write this rather than leave a section empty.
_NONE = re.compile(r"^(none|none recorded|n/?a|nothing|no .*recorded)\.?$", re.I)


@dataclass
class Item:
    """One bullet, or one action item broken into its parts.

    ``owner`` and ``due`` are empty for anything that is not an action, and
    routinely empty for actions too -- the prompt explicitly tells the model to
    leave them out rather than invent a name or a day, so a missing owner is
    the system working.
    """

    text: str
    owner: str = ""
    due: str = ""
    done: bool = False

    def line(self) -> str:
        """The item as one readable string, parts rejoined."""
        return " — ".join([p for p in (self.text, self.owner, self.due) if p])


@dataclass
class Notes:
    """A saved meeting, in the shape the window draws."""

    title: str = ""
    summary: str = ""
    points: List[Item] = field(default_factory=list)
    decisions: List[Item] = field(default_factory=list)
    actions: List[Item] = field(default_factory=list)
    #: Anything before the first heading we recognised. A model that ignored
    #: the format still said something, and dropping it silently would be worse
    #: than showing it.
    preamble: str = ""

    def is_empty(self) -> bool:
        return not (self.summary or self.points or self.decisions
                    or self.actions or self.preamble)


def _heading_key(text: str):
    """Which section a heading names, or None if it names nothing we know."""
    plain = text.strip().strip(":").strip().lower()
    plain = re.sub(r"\s+", " ", plain)
    for key, names in _SECTIONS.items():
        if plain in names:
            return key
    return None


def _looks_like_heading(line: str):
    """A heading in either format, as (key, matched) -- or (None, False).

    Plain text loses the ``##``, so an all-caps line standing on its own is the
    only signal left. Requiring it to also *name* a known section is what stops
    a shouted sentence in the transcript from splitting the notes in two.
    """
    stripped = line.strip()
    if not stripped:
        return None, False

    md = _MD_HEADING.match(line)
    if md:
        return _heading_key(md.group(2)), True

    # Uppercase, no sentence punctuation, short: what to_plain_text emits.
    if stripped == stripped.upper() and len(stripped) <= 40:
        key = _heading_key(stripped)
        if key:
            return key, True
    return None, False


def _split_action(text: str) -> Item:
    """Break "what — who — when" into its three parts.

    Any of the three may be missing. Two parts are read as *what* and *who*,
    because an owner is the part a small model drops last and a due date the
    part it drops first.
    """
    parts = [p.strip() for p in _PART.split(text) if p.strip()]
    if len(parts) >= 3:
        return Item(text=parts[0], owner=parts[1], due=" — ".join(parts[2:]))
    if len(parts) == 2:
        return Item(text=parts[0], owner=parts[1])
    return Item(text=text.strip())


def parse(text: str, title: str = "") -> Notes:
    """Read saved notes into :class:`Notes`. Never raises.

    ``text`` is the file as written, heading line and all. Unparseable input
    comes back as ``preamble``, so the caller always has something to show --
    notes that exist and did not fit the format still beat an empty panel.
    """
    notes = Notes(title=title)
    lines = (text or "").splitlines()

    # The file opens with "<title>\n====" (plain) or "# <title>" (Markdown).
    # Skip it: the caller already knows the title and repeating it in the panel
    # wastes the one line of vertical space the summary needs.
    start = 0
    if lines:
        first = lines[0].strip()
        if len(lines) > 1 and set(lines[1].strip()) in ({"="}, {"-"}) and first:
            if not notes.title:
                notes.title = first
            start = 2
        elif first.startswith("# "):
            if not notes.title:
                notes.title = first[2:].strip()
            start = 1

    section = None
    buckets = {"summary": [], "points": [], "decisions": [], "actions": [],
               None: []}

    for line in lines[start:]:
        key, is_heading = _looks_like_heading(line)
        if is_heading:
            # A heading we do not recognise still ends the previous section --
            # otherwise an unexpected "## Risks" lands inside Decisions.
            section = key
            continue
        if not line.strip():
            buckets[section].append("")
            continue
        buckets[section].append(line)

    notes.preamble = "\n".join(buckets[None]).strip()
    notes.summary = _as_prose(buckets["summary"])
    for key in ("points", "decisions", "actions"):
        setattr(notes, key, _as_items(buckets[key], action=key == "actions"))
    return notes


def _as_prose(lines: List[str]) -> str:
    """A section meant to read as a paragraph.

    to_plain_text indents body text by two spaces; keeping that indent here
    would show up as a ragged left edge in a proportional font.
    """
    out = []
    for line in lines:
        stripped = line.strip()
        bullet = _BULLET.match(line)
        if bullet:
            stripped = bullet.group(1).strip()
        out.append(stripped)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _as_items(lines: List[str], action: bool = False) -> List[Item]:
    """A section meant to read as a list.

    Continuation lines are folded into the item above them: a model that wraps
    one long action across two lines meant one action, and showing the tail as
    a second bullet with no owner reads as a second task nobody owns.
    """
    items: List[Item] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _NONE.match(stripped):
            continue          # "None recorded." is an answer, not an item

        task = _TASK.match(line)
        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if task:
            body, done = task.group(2).strip(), task.group(1).lower() == "x"
        elif bullet:
            body, done = bullet.group(1).strip(), False
        elif numbered:
            body, done = numbered.group(1).strip(), False
        elif items:
            items[-1].text = f"{items[-1].text} {stripped}".strip()
            continue
        else:
            body, done = stripped, False

        if not body or _NONE.match(body):
            continue
        item = _split_action(body) if action else Item(text=body)
        item.done = done
        items.append(item)
    return items


def read(path: str, title: str = "") -> Notes:
    """Parse a saved notes file. A file that cannot be read yields empty Notes."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse(f.read(), title=title)
    except OSError as e:
        print(f"[notes] cannot read {path}: {e}", flush=True)
        return Notes(title=title)


__all__ = ["Item", "Notes", "parse", "read"]
