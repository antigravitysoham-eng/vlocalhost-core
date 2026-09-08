"""Rolling chunk summarisation, measured against the whole-transcript call.

The idea under test: instead of one model call after Stop over the whole
meeting, summarise the transcript in chunks *while the meeting runs*, then do
one small reduce at the end.

Two things are being measured and they are not the same thing:

  total    every second of model time the machine spends. Chunking makes this
           WORSE -- N chunk calls plus a reduce, against one call.
  after    the seconds between a user pressing Stop and their notes appearing.
           Only the reduce and the last unprocessed chunk land here, because
           everything else happened during the meeting. This is the number a
           user experiences, and it is the whole point.

Chunk output is deliberately structured facts rather than prose. A summary of
summaries goes mushy; a list of decisions and commitments composes.
"""
import os, re, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)

import summarizer
from summarizer import strip_timestamps, _notes_prompt

_CHUNK_PROMPT = """Below is one part of a longer meeting transcript. List only \
what this part actually contains, under exactly these four headings. One short \
line per item, no commentary, no preamble. Write "none" under a heading with \
nothing in it.

TOPICS
DECISIONS
ACTIONS
QUESTIONS

An ACTION is something a person committed to doing -- keep who said it and any \
day they named. A DECISION is what the group settled on. Do not invent a name \
or a date. Ignore garbled or repeated lines.

PART:
{part}
"""

_REDUCE_PROMPT = """Below are notes taken from consecutive parts of one meeting, \
in order. Merge them into a single set of meeting notes in Markdown with exactly \
these sections:

## Summary
A short paragraph (3-5 sentences) of what the meeting was about, as a whole.

## Key Discussion Points
- The main topics. Merge duplicates that appear in more than one part.

## Decisions
- What the group settled on. Write "None recorded." only if there are none.

## Action Items
- [ ] what was agreed — who said they would do it — when

Use only what is below. Do not invent anything. Never write the same line twice, \
and never write the words "Task", "owner" or "due date" as labels.

PARTS:
{parts}
"""


def chunks(transcript, words_per_chunk, overlap_words=40):
    """Split on line boundaries, never mid-utterance, with a little overlap."""
    lines = [l for l in strip_timestamps(transcript).splitlines() if l.strip()]
    out, cur, n = [], [], 0
    for line in lines:
        cur.append(line); n += len(line.split())
        if n >= words_per_chunk:
            out.append("\n".join(cur))
            tail, kept = [], 0
            for l in reversed(cur):
                tail.insert(0, l); kept += len(l.split())
                if kept >= overlap_words:
                    break
            cur, n = list(tail), kept
    if cur and (not out or len("\n".join(cur).split()) > overlap_words):
        out.append("\n".join(cur))
    return out


def rolling(eng, transcript, words_per_chunk):
    parts = chunks(transcript, words_per_chunk)
    during, facts = 0.0, []
    for i, part in enumerate(parts):
        t0 = time.time()
        facts.append("--- part %d ---\n%s" % (i + 1, eng._complete(
            _CHUNK_PROMPT.format(part=part), 300, 0).strip()))
        during += time.time() - t0
    t0 = time.time()
    notes = eng._complete(_REDUCE_PROMPT.format(parts="\n\n".join(facts)),
                          1024, getattr(eng, "NOTES_MIN_TOKENS", 0)).strip()
    reduce_secs = time.time() - t0
    # The last chunk cannot have been done during the meeting: it ends when the
    # meeting does. Charge it honestly to the after-Stop side.
    last = during / max(len(parts), 1)
    return notes, {"parts": len(parts), "during": during - last,
                   "after": last + reduce_secs, "total": during + reduce_secs}


WANT = ("## Summary", "## Key Discussion Points", "## Decisions", "## Action Items")


def sections(md):
    return sum(1 for w in WANT if w.lower() in md.lower())
