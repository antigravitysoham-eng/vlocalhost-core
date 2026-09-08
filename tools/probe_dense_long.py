"""A long meeting that is mostly substance, which is what a real one is.

The padded fixture meeting is 80% small talk by volume, and notes drawn from it
are mostly about small talk -- which is the right answer to the wrong question.
Real meetings are not four fifths filler. This repeats the nine scenarios with
only a little chatter between them, so a part contains real decisions and real
commitments and the notes can be judged on whether it found them.

Repetition also tests the merge: the same decision arriving from four different
parts must come out as one line, not four.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE); sys.path.insert(0, HERE)

import config, summarizer, rolling
from summarizer import _notes_prompt
from probe_rolling_run import _FILLER, recall
from vlocalhost_pro.next_actions.scenarios import SCENARIOS


def dense(rounds=4, pad_words=60):
    keep = [s for s in SCENARIOS if s.key != "no_content"]
    parts, i = [], 0
    for _ in range(rounds):
        for s in keep:
            parts.append(s.transcript)
            done = 0
            while done < pad_words:
                line = _FILLER[i % len(_FILLER)]
                parts.append("[00:00:00] %s: %s"
                             % ("You" if i % 2 else "Participants", line))
                done += len(line.split()); i += 1
    return "\n".join(parts), keep


engine_name = sys.argv[1] if len(sys.argv) > 1 else "llamacpp"
config.SUMMARY_ENGINE = engine_name
summarizer._engine = None; summarizer._engine_key = None

text, keep = dense()
filler_words = sum(len(l.split()) for l in text.splitlines()
                   if any(f in l for f in _FILLER))
print("engine=%s  meeting=%d words  filler=%d%%"
      % (engine_name, len(text.split()),
         100 * filler_words // len(text.split())), flush=True)

eng = summarizer.engine()
# load() is optional in the engine contract -- Ollama has none.
if callable(getattr(eng, 'load', None)):
    eng.load()
print("prompt=%d tokens, budget=%d -> %s"
      % (rolling.count_tokens(eng, _notes_prompt(text)), rolling.budget(eng),
         "one call" if rolling.fits(eng, _notes_prompt(text)) else "rolling"),
      flush=True)

seen = []
t0 = time.time()
notes = summarizer.summarize(text, on_progress=lambda d, n: seen.append((d, n)))
secs = time.time() - t0
hits, tot = recall(notes, keep)
print("\n%.1fs  parts=%s  recall=%d/%d\n%s\n%s"
      % (secs, seen[-1][1] if seen else 1, hits, tot, "=" * 60, notes))
