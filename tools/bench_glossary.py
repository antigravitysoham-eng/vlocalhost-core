#!/usr/bin/env python3
"""Does handing the recogniser your vocabulary actually help, and what does it cost?

    python tools/bench_glossary.py

Transcribes the same audio three ways -- no glossary, the real one, and a
deliberately bloated one -- and counts how often each expected term is heard.

**The second number is the one that decides the feature.** Biasing is a trade:
too few terms and a name still comes out wrong, too many and the model starts
hearing them in audio that does not contain them. A feature that swaps a
mangled word for an invented one is worse than no feature, because a wrong
name a reader can see is honest and a plausible one is not.

So this also transcribes a *control* clip that contains none of the terms, and
counts how many turn up anyway. That is the hallucination rate, and it is the
reason `GLOSSARY_MAX_TERMS` exists.
"""

import io
import os
import re
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)
sys.path.insert(0, HERE)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config                                              # noqa: E402
import settings                                            # noqa: E402

settings.apply()

import bench_pipeline as bp                                # noqa: E402
import transcriber as tr                                   # noqa: E402

#: The terms from a real meeting this app got wrong. Every one of these came
#: back mangled on 14 September: "risk of suspense" for risk and compliance,
#: "air attacks" for AI attacks, "MSI financial service" for BFSI, "CSOT" for
#: CISO. They are the case for the feature, so they are what it is tested on.
REAL = ["Helius", "BFSI", "CISO", "Chanakya", "Kairo", "Hyderabad",
        "DBS Bank", "CMMC", "Abridge", "Vlocalhost"]

#: Plausible-sounding terms from a world this audio does not contain. If these
#: start appearing, the bias is inventing rather than recognising.
DECOYS = ["Zephyrine", "Malbrook", "Quantivo", "Farrowgate", "Oslen",
          "Bracknell", "Tyrelle", "Kappadine", "Verrano", "Silbex"]

FIXTURE = os.path.join(HERE, "fixtures", "speech-clean.wav")
TRUTH = ("Good morning everyone. Thanks for joining the stand up. "
         "Let's start with the release.")


def audio():
    with wave.open(FIXTURE, "rb") as w:
        return w.readframes(w.getnframes())


def run(label, terms):
    config.GLOSSARY = "\n".join(terms)
    config.GLOSSARY_MAX_TERMS = 200          # the cap is what we are testing
    engine = tr.build_transcriber()
    pcm = audio()
    t0 = time.time()
    heard = engine.transcribe(pcm) or ""
    secs = time.time() - t0

    decoys = [d for d in DECOYS if re.search(rf"\b{re.escape(d)}\b", heard, re.I)]
    wer = bp.word_error_rate(TRUTH, heard[:len(TRUTH) + 40])
    print(f"  {label:26} {secs:5.1f}s   decoys heard: {len(decoys):2}"
          f"   WER on known text: {wer:5.1f}%")
    if decoys:
        print(f"    {'':26} invented: {', '.join(decoys)}")
    return heard, len(decoys), secs


print("=" * 74)
print("GLOSSARY BENCHMARK — does biasing help, and what does it cost?")
print("=" * 74)
print(f"model    : {config.WHISPER_MODEL}")
print(f"audio    : {os.path.basename(FIXTURE)} — contains NONE of the terms")
print()
print("This fixture is the control. Any term heard here was invented, because")
print("none of them are in the audio. Fewer is better; zero is the bar.")
print()

base, base_decoys, base_secs = run("no glossary", [])
real, real_decoys, real_secs = run(f"real ({len(REAL)} terms)", REAL)
big = REAL + DECOYS + [f"Placeholder{i}" for i in range(180)]
bloat, bloat_decoys, bloat_secs = run(f"bloated ({len(big)} terms)", big)

print()
print("-" * 74)
print(f"cost of biasing      : {real_secs - base_secs:+.1f}s on a "
      f"{base_secs:.1f}s decode")
print(f"invented terms       : {base_decoys} none -> {real_decoys} real -> "
      f"{bloat_decoys} bloated")
print()
if real_decoys == 0:
    print("VERDICT: a real-sized glossary invented nothing. Safe to ship.")
else:
    print("VERDICT: the glossary put words into audio that did not contain")
    print("         them. Lower GLOSSARY_MAX_TERMS before shipping this.")
if bloat_decoys > real_decoys:
    print(f"         The cap matters: {len(big)} terms invented "
          f"{bloat_decoys - real_decoys} more than {len(REAL)} did.")
print()
print("NOTE: this measures the *cost* of biasing, not the benefit. The benefit")
print("      needs audio that actually contains the jargon -- record thirty")
print("      seconds saying these terms and run it again to see them fixed.")
