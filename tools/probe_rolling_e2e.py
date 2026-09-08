"""End to end through summarizer.summarize(), the way the app calls it.

Not through rolling directly: the point is that a caller asks for notes and
gets notes, without knowing or caring which path answered.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE); sys.path.insert(0, HERE)

import config, summarizer, rolling
from summarizer import _notes_prompt
from probe_context import transcript

WANT = ("## Summary", "## Key Discussion Points", "## Decisions", "## Action Items")

def sections(md):
    return sum(1 for w in WANT if w.lower() in md.lower())

def run(engine_name, targets):
    config.SUMMARY_ENGINE = engine_name
    summarizer._engine = None; summarizer._engine_key = None
    print("== %s ==" % engine_name, flush=True)
    for target in targets:
        text, words = transcript(target)
        seen = []
        t0 = time.time()
        try:
            notes = summarizer.summarize(
                text, on_progress=lambda d, n: seen.append((d, n)))
            secs = time.time() - t0
            eng = summarizer.engine()
            path = "rolling(%d parts)" % seen[-1][1] if seen else "one call"
            print("  %5d words  %7.1fs  %-18s sections=%d/4  chars=%d"
                  % (words, secs, path, sections(notes), len(notes)), flush=True)
        except Exception as e:
            print("  %5d words  RAISED %s: %s"
                  % (words, type(e).__name__, str(e)[:100]), flush=True)

if __name__ == "__main__":
    eng = sys.argv[1] if len(sys.argv) > 1 else "llamacpp"
    targets = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [400, 9000]
    run(eng, targets)
