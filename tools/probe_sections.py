"""Why does the notes call sometimes stop after ## Summary?

One variable at a time, greedy, over the ten shipped fixtures plus variants of
one of them. Prints the number of required headings each run produced.
"""
import os, re, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
ROOT = os.path.dirname(CORE)
sys.path.insert(0, CORE); sys.path.insert(0, ROOT)

import summarizer
from vlocalhost_pro.next_actions.scenarios import SCENARIOS

WANT = ("## Summary", "## Key Discussion Points", "## Decisions", "## Action Items")

def sections(md):
    low = md.lower()
    return sum(1 for w in WANT if w.lower() in low)

def tag(t):                       # add a language tag to every speaker label
    return re.sub(r"^(\[[\d:]+\]\s*)?(You|Them|Participants)(:)",
                  lambda m: (m.group(1) or "") + m.group(2) + " (en)" + m.group(3),
                  t, flags=re.M)

def untag(t):
    return re.sub(r"\s*\((?:en|hi|es|fr|de)\)\s*:", ":", t)

eng = summarizer.LlamaCppSummarizer(); eng.load()
d = {s.key: s.transcript for s in SCENARIOS}

print("== ten shipped fixtures, as-is (no language tag) vs with (en) added ==")
for k, t in d.items():
    row = []
    for label, text in (("plain", untag(t)), ("(en)", tag(t))):
        t0 = time.time(); out = eng.summarize(text); secs = time.time() - t0
        row.append("%s=%d/4 %.1fs" % (label, sections(out), secs))
    print("  %-20s %s" % (k, "   ".join(row)))
