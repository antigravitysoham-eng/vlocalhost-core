"""Whole-transcript vs rolling chunks, on a long meeting built from real fixtures.

The ten shipped scenarios concatenated make one long multi-topic meeting whose
contents are known: every fixture's ``must_find`` strings are unarguably in the
text. Recall against that list is the quality measure, and it is the measure
that matters here -- a summary that is fast because it only saw the last ten
minutes is not fast, it is wrong.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
ROOT = os.path.dirname(CORE)
sys.path.insert(0, CORE); sys.path.insert(0, ROOT)

import summarizer
from probe_rolling import rolling, chunks, sections
from vlocalhost_pro.next_actions.scenarios import SCENARIOS

#: Chatter between the real topics, so a long meeting is long the way a real one
#: is -- mostly filler with the substance spread thinly through it. Without this
#: the nine fixtures come to 1266 words, which no engine has trouble with, and
#: the test would prove nothing about length.
_FILLER = [
    "Anyway, before we move on, did anyone look at the dashboard this morning.",
    "I had a quick look, nothing jumped out at me.",
    "Right. And the other thing was the calendar invite for next week.",
    "I will forward it, I think half the room is missing from it.",
    "Sure. Sorry, go on, you were saying about the earlier point.",
    "No it is fine, I had lost my train of thought anyway.",
    "Can everyone still hear me, my connection dropped for a second there.",
    "You are fine, we can hear you.",
]


def long_meeting(n, pad_words=0):
    keep = [s for s in SCENARIOS if s.key != "no_content"][:n]
    if not pad_words:
        return "\n".join(s.transcript for s in keep), keep
    parts, i = [], 0
    for s in keep:
        parts.append(s.transcript)
        done = 0
        while done < pad_words:
            line = _FILLER[i % len(_FILLER)]
            parts.append("[00:00:00] %s: %s"
                         % ("You" if i % 2 else "Participants", line))
            done += len(line.split()); i += 1
    return "\n".join(parts), keep

def recall(notes, keep):
    low = notes.lower()
    hits = tot = 0
    for s in keep:
        for m in s.must_find:
            tot += 1
            hits += 1 if m.lower() in low else 0
    return hits, tot

def run(engine_name, n_fixtures, sizes, pad=0):
    cls = summarizer._ENGINES[engine_name]
    eng = cls(); eng.load()
    text, keep = long_meeting(n_fixtures, pad)
    words = len(text.split())
    print("== %s | %d fixtures, %d words ==" % (engine_name, n_fixtures, words), flush=True)

    try:
        t0 = time.time(); notes = eng.summarize(text); whole = time.time() - t0
        h, t = recall(notes, keep)
        print("  whole transcript   after=%6.1fs total=%6.1fs  sections=%d/4  recall=%d/%d"
              % (whole, whole, sections(notes), h, t), flush=True)
    except Exception as e:
        # Not a caveat to note and move past: this is the user getting no notes
        # at all for a meeting they recorded. Print it where the comparison is.
        print("  whole transcript   FAILED  %s: %s"
              % (type(e).__name__, str(e)[:90]), flush=True)

    for size in sizes:
        notes, m = rolling(eng, text, size)
        h, t = recall(notes, keep)
        print("  rolling %3d words  after=%6.1fs total=%6.1fs  sections=%d/4  recall=%d/%d  parts=%d"
              % (size, m["after"], m["total"], sections(notes), h, t, m["parts"]), flush=True)

if __name__ == "__main__":
    eng = sys.argv[1] if len(sys.argv) > 1 else "llamacpp"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    pad = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    sizes = [int(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 else [300, 500, 800]
    run(eng, n, sizes, pad)
