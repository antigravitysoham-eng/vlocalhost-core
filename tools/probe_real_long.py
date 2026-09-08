"""One realistic long meeting, end to end, printed in full.

The synthetic filler transcript is useful for measuring where a context window
runs out and useless for judging notes: it repeats eight sentences, so thin
notes from it are the right answer. This uses the nine shipped scenarios with
chatter between them -- real decisions and real commitments, spread through a
meeting far too long for one call.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE); sys.path.insert(0, HERE)

import config, summarizer, rolling
from summarizer import _notes_prompt
from probe_rolling_run import long_meeting, recall

engine_name = sys.argv[1] if len(sys.argv) > 1 else "llamacpp"
pad = int(sys.argv[2]) if len(sys.argv) > 2 else 700

config.SUMMARY_ENGINE = engine_name
summarizer._engine = None; summarizer._engine_key = None

text, keep = long_meeting(9, pad)
print("engine=%s  meeting=%d words" % (engine_name, len(text.split())), flush=True)

eng = summarizer.engine(); eng.load()
print("budget=%d  prompt=%d tokens  -> %s"
      % (rolling.budget(eng), rolling.count_tokens(eng, _notes_prompt(text)),
         "one call" if rolling.fits(eng, _notes_prompt(text)) else "rolling"), flush=True)

seen = []
t0 = time.time()
notes = summarizer.summarize(text, on_progress=lambda d, n: seen.append((d, n)))
secs = time.time() - t0
hits, tot = recall(notes, keep)
print("\n%.1fs  parts=%s  recall=%d/%d\n%s\n%s"
      % (secs, seen[-1][1] if seen else 1, hits, tot, "=" * 60, notes))
