#!/usr/bin/env python3
"""Measure what the transcript pipeline actually costs, end to end.

    python tools/bench_pipeline.py                       # the clean fixture
    python tools/bench_pipeline.py --fixture speech-cafe
    python tools/bench_pipeline.py --all                 # every fixture
    python tools/bench_pipeline.py --json out.json       # for diffing phases

Plays a fixture through the **real** :class:`audio_listener._Segmenter` and the
**real** transcriber, at real speed, and reports the number that matters: how
long after somebody stops talking their words appear.

That latency is three things stacked, and the bench separates them because they
have different fixes:

    endpoint   the VAD waiting out ``config.SILENCE_TIMEOUT_MS`` before it will
               admit the utterance has ended
    queue      time spent waiting for the model, which is busy with the last
               utterance — zero in a polite meeting, not zero in an argument
    decode     the model itself

Audio is fed on a wall clock, exactly as a microphone would deliver it, so the
queue term is real rather than assumed. Because the fixture carries ground
truth, "when the speaker stopped" is known to the sample instead of being
inferred from the thing being measured.

If the segmenter grows an ``on_partial`` callback, this picks it up
automatically and reports time-to-first-*partial* alongside time-to-final.
"""

import argparse
import json
import os
import queue
import re
import statistics
import sys
import threading
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
if CORE not in sys.path:
    sys.path.insert(0, CORE)

import config                                  # noqa: E402
from audio_listener import _Segmenter          # noqa: E402
from transcriber import build_transcriber      # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")


# --- text comparison ------------------------------------------------------

# Whisper writes "20th" and "2" where the script says "twentieth" and "two".
# Both are correct transcriptions, so counting them as errors would hide the
# errors that matter. Digits are folded to words before comparison.
_NUMBERS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten", "11": "eleven", "12": "twelve", "13": "thirteen",
    "14": "fourteen", "15": "fifteen", "16": "sixteen", "17": "seventeen",
    "18": "eighteen", "19": "nineteen", "20": "twenty", "30": "thirty",
    "40": "forty", "50": "fifty", "1st": "first", "2nd": "second",
    "3rd": "third", "4th": "fourth", "5th": "fifth", "20th": "twentieth",
}


def normalise(text):
    """Words only, lowercased, digits spelled out.

    Punctuation, case and digit-versus-word are formatting choices, not
    transcription errors.
    """
    words = re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()
    return [_NUMBERS.get(w, w) for w in words]


def levenshtein(ref, hyp):
    """Edit distance between two sequences (words or characters)."""
    if not ref:
        return len(hyp)
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(min(previous[j] + 1,              # deletion
                               current[j - 1] + 1,           # insertion
                               previous[j - 1] + (r != h)))  # substitution
        previous = current
    return previous[-1]


def char_error_rate(reference, hypothesis):
    """Edit distance over characters with spaces removed, as a percentage.

    Reported beside WER because word-level scoring punishes a compound split
    ("stand up" for "standup") as two errors when the model heard it perfectly.
    CER is the fairer read on whether the words were understood; WER is the
    fairer read on whether the transcript is clean. Both move together when
    something genuinely regresses, which is what these phases are watching for.
    """
    ref = "".join(normalise(reference))
    hyp = "".join(normalise(hypothesis))
    if not ref:
        return 0.0 if not hyp else 100.0
    return 100.0 * levenshtein(ref, hyp) / len(ref)


def word_error_rate(reference, hypothesis):
    """Standard WER: (substitutions + insertions + deletions) / reference words.

    Reported as a percentage; 0 is a perfect match and values above 100 are
    possible when the model invents more than it heard, which is exactly what
    happens on a music clip.
    """
    ref, hyp = normalise(reference), normalise(hypothesis)
    if not ref:
        return 0.0 if not hyp else 100.0
    return 100.0 * levenshtein(ref, hyp) / len(ref)


def trailing_silence_seconds(pcm_bytes, threshold=0.02):
    """How much silence a captured segment ends with.

    This *is* the endpoint delay, and measuring it here rather than against the
    fixture's ground truth matters: the VAD is free to split one spoken turn
    into two segments (it does), so matching the Nth segment to the Nth
    scripted utterance produces confident nonsense. A segment always ends at
    the instant it was flushed, and the silence it carries is exactly how long
    the VAD waited before conceding — no alignment required, and it works on a
    fixture with no ground truth at all.
    """
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if loud.size == 0:
        return len(audio) / config.SAMPLE_RATE
    return (len(audio) - loud[-1] - 1) / config.SAMPLE_RATE


# --- fixture loading ------------------------------------------------------

def load_fixture(name):
    """(float32 audio, ground truth or None) for a fixture stem."""
    wav = os.path.join(FIXTURES, name + ".wav")
    if not os.path.isfile(wav):
        raise SystemExit(f"No fixture {name!r} — run tools/make_fixtures.py first.")
    with wave.open(wav, "rb") as w:
        if w.getframerate() != config.SAMPLE_RATE or w.getnchannels() != 1:
            raise SystemExit(f"{wav}: expected {config.SAMPLE_RATE} Hz mono")
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16)

    truth = None
    meta = os.path.join(FIXTURES, name + ".json")
    if os.path.isfile(meta):
        with open(meta, encoding="utf-8") as f:
            truth = json.load(f)
    elif name.startswith("speech-"):
        # The noisy mixes share the clean clip's timeline by construction.
        base = os.path.join(FIXTURES, "speech-clean.json")
        if os.path.isfile(base):
            with open(base, encoding="utf-8") as f:
                truth = json.load(f)
    return audio, truth


# --- the run --------------------------------------------------------------

class Run:
    """One pass of a fixture through segmenter and transcriber."""

    def __init__(self, transcriber, realtime=True):
        self.transcriber = transcriber
        self.realtime = realtime
        self.finals = []          # dicts, in the order the model produced them
        self.partials = []        # (wall_time, text) if the segmenter supports it
        self._q = queue.Queue()
        self._partial_slot = None
        self._partial_lock = threading.Lock()
        self._next_partial_at = 0.0
        self._running = True
        self._t0 = None
        self._worker = threading.Thread(target=self._decode_loop, daemon=True)

    # -- callbacks the segmenter drives -----------------------------------
    def _on_utterance(self, pcm, label):
        # Wall time at which the VAD conceded the utterance was over. Everything
        # before this instant is endpoint delay; everything after is the model.
        self._q.put((pcm, label, time.perf_counter()))

    def _on_partial(self, pcm, label):
        # A slot, not a queue — the same rule NoteTaker uses, because a bench
        # that let partials pile up FIFO would measure a backlog the product
        # cannot have and report a latency nobody will experience.
        with self._partial_lock:
            self._partial_slot = (pcm, label, time.perf_counter())

    def _take_partial(self):
        """NoteTaker's two guards: finals win, and back off by the last cost."""
        if not self._q.empty():
            with self._partial_lock:
                self._partial_slot = None
            return None
        if time.perf_counter() < self._next_partial_at:
            return None
        with self._partial_lock:
            item, self._partial_slot = self._partial_slot, None
        return item

    def _run_partial(self):
        item = self._take_partial()
        if item is None:
            return
        pcm, label, queued_at = item
        started = time.perf_counter()
        try:
            text = self.transcriber.transcribe(pcm, partial=True)
        except Exception as e:                            # noqa: BLE001
            print(f"  [partial decode error] {e}")
            return
        done = time.perf_counter()
        self._next_partial_at = done + (done - started)
        if text and self._q.empty():
            self.partials.append({
                "at": done - self._t0,
                "audio_to": queued_at - self._t0,
                "decode_ms": 1000 * (done - started),
                "text": text,
            })

    # -- the model, on its own thread, exactly as NoteTaker runs it -------
    def _decode_loop(self):
        while self._running or not self._q.empty():
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                self._run_partial()
                continue
            pcm, label, flushed_at = item[0], item[1], item[2]
            started = time.perf_counter()
            try:
                text = self.transcriber.transcribe(pcm)
            except Exception as e:                        # noqa: BLE001
                print(f"  [decode error] {e}")
                continue
            done = time.perf_counter()
            span = len(pcm) / 2 / config.SAMPLE_RATE
            endpoint = trailing_silence_seconds(pcm)
            self.finals.append({
                "label": label,
                "text": text,
                "audio_seconds": span,
                # Where this segment sits in the fixture, so it can be named
                # against the script without being *aligned* to it.
                "audio_from": flushed_at - self._t0 - span,
                "audio_to": flushed_at - self._t0,
                "endpoint_ms": 1000 * endpoint,
                "queue_ms": 1000 * (started - flushed_at),
                "decode_ms": 1000 * (done - started),
                # What the user waits: the VAD holding on, the model being
                # busy, and the model working.
                "latency_ms": 1000 * (endpoint + (done - flushed_at)),
                "ready_at": done - self._t0,
            })

    def play(self, audio):
        """Feed the fixture through the segmenter at the speed of real life."""
        seg = _Segmenter(self._on_utterance, label="You")
        # Phase 2 adds partial hypotheses; pick them up the moment they exist
        # rather than needing this file changed again.
        if hasattr(seg, "on_partial"):
            seg.on_partial = self._on_partial

        frame = seg.frame_size
        self._t0 = time.perf_counter()
        self._worker.start()

        for start in range(0, len(audio) - frame + 1, frame):
            if self.realtime:
                due = self._t0 + (start + frame) / config.SAMPLE_RATE
                delay = due - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            seg.feed(audio[start:start + frame].tobytes())

        seg.flush()
        self._running = False
        self._worker.join(timeout=120)
        return self.finals


# --- reporting ------------------------------------------------------------

def report(name, run, truth, audio_seconds):
    """Print the table, return the machine-readable summary."""
    finals = [f for f in run.finals if f["text"]]
    utterances = (truth or {}).get("utterances") or []

    print(f"\n=== {name} ===")
    print(f"  audio {audio_seconds:.1f}s · {len(run.finals)} segment(s) from the "
          f"VAD · {len(finals)} with text")
    if utterances:
        print(f"  ground truth: {len(utterances)} utterance(s)")
        if len(finals) != len(utterances):
            print(f"  ** segment count differs from ground truth — the VAD is "
                  f"splitting or merging turns **")

    def turn_of(f):
        """Which scripted turn(s) this segment covers, for reading the table."""
        hits = [str(n) for n, u in enumerate(utterances, 1)
                if u["start_seconds"] < f["audio_to"]
                and u["end_seconds"] > f["audio_from"]]
        return ",".join(hits) or "-"

    latencies = [f["latency_ms"] / 1000 for f in finals]
    print(f"\n  {'#':>2}  {'turn':>5}  {'endpoint':>9}  {'queue':>7}  "
          f"{'decode':>8}  {'latency':>8}  text")
    for i, f in enumerate(finals):
        print(f"  {i + 1:>2}  {turn_of(f):>5}  {f['endpoint_ms']:8.0f}ms  "
              f"{f['queue_ms']:6.0f}ms  {f['decode_ms']:7.0f}ms  "
              f"{f['latency_ms']:7.0f}ms  {f['text'][:44]}")

    summary = {"fixture": name, "audio_seconds": round(audio_seconds, 2),
               "segments": len(run.finals), "with_text": len(finals)}

    if latencies:
        summary["latency_ms"] = {
            "first": round(1000 * latencies[0]),
            "median": round(1000 * statistics.median(latencies)),
            "worst": round(1000 * max(latencies)),
        }
        summary["endpoint_ms_median"] = round(
            statistics.median(f["endpoint_ms"] for f in finals))
        decode_total = sum(f["decode_ms"] for f in finals)
        speech_total = sum(f["audio_seconds"] for f in finals) or 1.0
        summary["decode_ms_per_audio_second"] = round(decode_total / speech_total)
        summary["realtime_factor"] = round(1000 * speech_total / decode_total, 1)
        print(f"\n  latency   first {summary['latency_ms']['first']}ms · "
              f"median {summary['latency_ms']['median']}ms · "
              f"worst {summary['latency_ms']['worst']}ms")
        print(f"  endpoint  {summary['endpoint_ms_median']}ms median "
              f"(config.SILENCE_TIMEOUT_MS is {config.SILENCE_TIMEOUT_MS}ms)")
        print(f"  decode    {summary['decode_ms_per_audio_second']}ms per second "
              f"of speech ({summary['realtime_factor']}x real time)")

    # The number a user would actually report. Per-segment latency flatters a
    # VAD that never endpoints: swallow six turns into one segment emitted at
    # the end of the meeting and every segment still looks fast, because there
    # was only one and it was measured from its own tail. This asks the other
    # question — for each thing that was *said*, how long until it was on
    # screen — and a VAD that refuses to cut has nowhere to hide.
    if utterances and finals:
        waits = []
        for turn in utterances:
            for f in finals:
                if f["audio_to"] >= turn["end_seconds"]:
                    waits.append(f["ready_at"] - turn["end_seconds"])
                    break
        if waits:
            summary["turn_wait_ms"] = {
                "median": round(1000 * statistics.median(waits)),
                "worst": round(1000 * max(waits)),
            }
            print(f"  wait      {summary['turn_wait_ms']['median']}ms median "
                  f"from a turn ending to its words appearing · worst "
                  f"{summary['turn_wait_ms']['worst']}ms")

    if run.partials:
        summary["partials"] = len(run.partials)
        # The question provisional text exists to answer: once you start
        # talking, how long until anything at all is on screen? Measured from
        # each turn's *start*, unlike the final-line wait above, which is
        # measured from its end.
        firsts = []
        for turn in utterances:
            for p in run.partials:
                if p["audio_to"] >= turn["start_seconds"]:
                    if p["audio_to"] <= turn["end_seconds"] + 1.0:
                        firsts.append(p["at"] - turn["start_seconds"])
                    break
        if firsts:
            summary["first_words_ms"] = {
                "median": round(1000 * statistics.median(firsts)),
                "worst": round(1000 * max(firsts)),
            }
            print(f"  first words {summary['first_words_ms']['median']}ms "
                  f"median after a speaker starts · worst "
                  f"{summary['first_words_ms']['worst']}ms "
                  f"({len(run.partials)} provisional updates)")
        else:
            print(f"  partials  {len(run.partials)}, none inside a turn")
    else:
        print("  partials  none (the segmenter emits finals only)")

    if utterances:
        reference = " ".join(u["text"] for u in utterances)
        hypothesis = " ".join(f["text"] for f in finals)
        summary["wer"] = round(word_error_rate(reference, hypothesis), 1)
        summary["cer"] = round(char_error_rate(reference, hypothesis), 1)
        print(f"  accuracy  WER {summary['wer']}% · CER {summary['cer']}%")
        print(f"\n  heard: {hypothesis[:300]}")
    elif "music-only" in name:
        # No speech in the file, so any word at all is a false positive.
        words = sum(len(normalise(f["text"])) for f in finals)
        summary["false_words"] = words
        print(f"  accuracy  {words} hallucinated word(s) — the correct answer "
              f"is 0")
        if finals:
            print(f"\n  invented: {' | '.join(f['text'][:60] for f in finals)}")

    summary["utterances"] = finals
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", default="speech-clean", help="fixture stem")
    ap.add_argument("--all", action="store_true", help="run every fixture")
    ap.add_argument("--fast", action="store_true",
                    help="feed as fast as possible — decode and endpoint stay "
                         "honest, queue contention does not")
    ap.add_argument("--json", default="", help="write the summary here")
    ap.add_argument("--vad", default="", choices=["", "silero", "webrtc"],
                    help="override config.VAD_ENGINE for this run")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each fixture N times and report every run — a "
                         "laptop under load varies enough to fake a result")
    args = ap.parse_args()

    if args.vad:
        config.VAD_ENGINE = args.vad

    names = (["speech-clean", "speech-cafe", "speech-music", "music-only",
              "speech-monologue"]
             if args.all else [args.fixture])

    print(f"model {config.WHISPER_MODEL} · {config.WHISPER_COMPUTE} on "
          f"{config.WHISPER_DEVICE} · beam {config.WHISPER_BEAM_SIZE}")
    print(f"vad aggressiveness {config.VAD_AGGRESSIVENESS} · "
          f"silence timeout {config.SILENCE_TIMEOUT_MS}ms · "
          f"frame {config.FRAME_MS}ms")

    transcriber = build_transcriber()
    if hasattr(transcriber, "load"):
        loading = time.perf_counter()
        transcriber.load()
        print(f"model loaded in {time.perf_counter() - loading:.1f}s")

    results = []
    for name in names:
        audio, truth = load_fixture(name)
        for attempt in range(max(1, args.repeat)):
            label = name if args.repeat < 2 else f"{name} (run {attempt + 1})"
            run = Run(transcriber, realtime=not args.fast)
            run.play(audio)
            results.append(report(label, run, truth,
                                  len(audio) / config.SAMPLE_RATE))

    if args.json:
        payload = {
            "settings": {
                "whisper_model": config.WHISPER_MODEL,
                "whisper_compute": config.WHISPER_COMPUTE,
                "whisper_device": config.WHISPER_DEVICE,
                "beam_size": config.WHISPER_BEAM_SIZE,
                "vad_aggressiveness": config.VAD_AGGRESSIVENESS,
                "silence_timeout_ms": config.SILENCE_TIMEOUT_MS,
                "frame_ms": config.FRAME_MS,
                "realtime": not args.fast,
            },
            "results": results,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
