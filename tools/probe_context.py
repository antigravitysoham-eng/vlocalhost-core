"""Does a long meeting actually reach the model, or is it silently truncated?

Two markers are planted in one transcript: a distinctive fact in the FIRST
minute and another in the LAST. Both are unmissable if the model saw them.
Which markers come back tells you where the prompt was cut.

Nothing here is a timing test. It is a correctness test with one question:
for a meeting of length N, do the notes describe the whole meeting?
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)

import summarizer, config

HEAD = "The budget code for this project is ALPHA-7741."
TAIL = "The budget code has changed to ZULU-9920."

FILLER = [
    "We should look at the ingest worker retry counts again this week.",
    "The staging database snapshot is still a week behind production.",
    "Nobody has picked up the flaky integration test on the payments path.",
    "Support saw three tickets about the export button since Tuesday.",
    "The marketing page copy still says the old pricing tier.",
    "We agreed to leave the onboarding email sequence alone for now.",
    "Someone needs to check whether the nightly backup actually restores.",
    "The design review slipped because two people were out.",
]

def transcript(words_target):
    lines = ["[09:00:00] You: " + HEAD]
    n = len(HEAD.split())
    i = 0
    while n < words_target:
        who = "You" if i % 2 else "Participants"
        s = FILLER[i % len(FILLER)]
        lines.append("[09:%02d:%02d] %s: %s" % ((i // 60) % 60, i % 60, who, s))
        n += len(s.split()); i += 1
    lines.append("[10:59:59] You: " + TAIL)
    return "\n".join(lines), n

def probe(eng, name):
    print("== %s ==" % name, flush=True)
    for target in (400, 1500, 3000, 6000):
        text, words = transcript(target)
        try:
            t0 = time.time(); out = eng.summarize(text); secs = time.time() - t0
            head = "ALPHA-7741" in out or "ALPHA" in out
            tail = "ZULU-9920" in out or "ZULU" in out
            print("  %5d words  %6.1fs  head=%-5s tail=%-5s  chars=%d"
                  % (words, secs, head, tail, len(out)), flush=True)
        except Exception as e:
            print("  %5d words  RAISED %s: %s" % (words, type(e).__name__, str(e)[:120]), flush=True)

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "ollama"
    if which == "ollama":
        probe(summarizer.OllamaSummarizer(), "ollama / " + config.OLLAMA_MODEL)
    elif which == "llamacpp":
        probe(summarizer.LlamaCppSummarizer(), "llama.cpp / qwen2.5-1.5b (n_ctx=8192)")
    elif which == "ctranslate2":
        probe(summarizer.CTranslate2Summarizer(), "ctranslate2 / qwen2.5-1.5b")
