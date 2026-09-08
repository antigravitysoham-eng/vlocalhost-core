"""A long meeting whose content is never repeated, for judging duplication.

The earlier harness reached its word count by running the nine scenarios four
times over, so the same decision really was taken four times and notes that
listed it four times were not obviously wrong. Any conclusion about the merge
drawn from that is worthless.

This says each thing once. Length comes from filler, and the filler is forty
distinct lines rather than eight repeated ones, so it cannot manufacture
duplicates either. What is left in the notes is duplication the chunking
actually caused.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE); sys.path.insert(0, HERE)

import config, summarizer, rolling
from summarizer import _notes_prompt
from probe_rolling_run import recall
from vlocalhost_pro.next_actions.scenarios import SCENARIOS

CHATTER = [
    "Sorry, I was on mute there for a second.",
    "Can you scroll back up, I missed the top of that.",
    "My camera keeps freezing, I will leave it off if that is alright.",
    "Someone is typing quite loudly, if you could mute that would help.",
    "I have a hard stop at the hour, just so everyone knows.",
    "Do we need [Participant] for this bit or can they drop?",
    "I will drop off after this item if that is fine with everyone.",
    "Give me one second, my laptop is asking to restart again.",
    "Is the recording running? I want to catch up later if so.",
    "I cannot find the link, can someone paste it in the chat.",
    "Let me share my screen, tell me when you can see it.",
    "That is a bit small on my end, can you zoom in.",
    "We are about halfway through the agenda, so we are doing alright.",
    "Shall we park that and come back to it at the end.",
    "I think we lost [Participant], they may be rejoining.",
    "Right, where were we before that interruption.",
    "I have not had a chance to read the doc yet, apologies.",
    "Can we get that circulated before the next one.",
    "There is an echo, I think two of us are in the same room.",
    "Sorry, you go first, I keep talking over you.",
    "I will put the notes in the shared folder afterwards.",
    "Does anyone have anything before we move on.",
    "Just so I am clear, that is the same thing we discussed last week.",
    "I am going to grab a coffee, keep going without me.",
    "The office wifi is being difficult this morning.",
    "Can we check the time on this, I think we are running over.",
    "I will follow up in writing so nothing gets lost.",
    "That reminds me, we never closed out the thing from Tuesday.",
    "Let me pull the numbers up, one moment.",
    "Sorry, could you repeat the last part, my connection dipped.",
    "Are we all seeing the same version of this.",
    "I think that is a separate conversation, honestly.",
    "Good, that is one less thing to worry about.",
    "I will take that offline with [Participant] afterwards.",
    "Is everyone happy to keep going for another ten minutes.",
    "That was not on my copy of the agenda.",
    "Let us make sure we capture that one properly.",
    "I missed the beginning, can someone summarise where we got to.",
    "I have a note to check that but have not done it yet.",
    "Fine by me, no objections here.",
]


def fair(pad_words=380):
    keep = [s for s in SCENARIOS if s.key != "no_content"]
    parts, i = [], 0
    for s in keep:
        parts.append(s.transcript)
        done = 0
        while done < pad_words:
            line = CHATTER[i % len(CHATTER)]
            parts.append("[00:00:00] %s: %s"
                         % ("You" if i % 2 else "Participants", line))
            done += len(line.split()); i += 1
    return "\n".join(parts), keep


engine_name = sys.argv[1] if len(sys.argv) > 1 else "llamacpp"
config.SUMMARY_ENGINE = engine_name
summarizer._engine = None; summarizer._engine_key = None

pad = int(sys.argv[2]) if len(sys.argv) > 2 else 380
text, keep = fair(pad)
print("engine=%s  meeting=%d words  (each topic said once, 40 distinct filler lines)"
      % (engine_name, len(text.split())), flush=True)

eng = summarizer.engine()
if callable(getattr(eng, "load", None)):
    eng.load()
print("prompt=%d tokens, budget=%d -> %s"
      % (rolling.count_tokens(eng, _notes_prompt(text)), rolling.budget(eng),
         "one call" if rolling.fits(eng, _notes_prompt(text)) else "rolling"),
      flush=True)

seen = []
t0 = time.time()
notes = summarizer.summarize(text, on_progress=lambda d, n: seen.append((d, n)))
hits, tot = recall(notes, keep)
print("\n%.1fs  parts=%s  recall=%d/%d\n%s\n%s"
      % (time.time() - t0, seen[-1][1] if seen else 1, hits, tot, "=" * 60, notes))
