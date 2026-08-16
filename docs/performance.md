# Performance and memory

Everything runs on your CPU, so the honest answer to "how heavy is it" is a
measurement, not an adjective. These are real numbers from this codebase.

**Test machine:** AMD Ryzen (Zen 3) 6 cores / 12 threads, 14.8 GB RAM, Windows 11,
Python 3.12, `int8` on CPU. Full app: engine, **both** capture sources open
(microphone *and* system audio), real speech through the pipeline.

Re-measure on your own hardware — **Settings → Benchmark this machine**, or:

```python
import performance; print(performance.benchmark())   # (peak_MB, seconds_for_10s_audio)
```

## Memory

| Profile | Model | Idle (model freed) | While recording | Peak |
|---|---|---|---|---|
| **Light** | `tiny` | ~90 MB | ~226 MB | **~250 MB** |
| **Balanced** *(default)* | `base` | ~91 MB | ~337 MB | **~347 MB** |
| **Accurate** | `small` | ~95 MB | ~687 MB | **~734 MB** |

Bare Python with the app's imports and no model loaded is **~29 MB**; a
fully idle app that has already recorded once sits at **~90 MB**.

**So: budget ~350 MB peak on the default profile, ~250 MB on Light.**

Whisper dominates the footprint. Everything else — VAD, audio buffers, the Tk
window, the MCP server — is tens of megabytes. Ollama runs as a **separate
process** and is not counted here; it only runs when you stop a recording, and
`llama3.2` needs roughly 2 GB of its own while summarizing.

## Speed

Measured as how long the model takes to transcribe 10 seconds of audio:

| Profile | Time for 10 s | Verdict |
|---|---|---|
| Light (`tiny`) | fastest | large headroom on weak CPUs |
| Balanced (`base`) | **2.2 s → 4.5× real time** | keeps up comfortably |
| Accurate (`small`) | ~10 s+ | roughly real time; lagged behind live speech even on 6 cores |

Transcription is **asynchronous** — utterances queue up and drain during
pauses, so falling briefly behind delays a line appearing but never drops audio.
Sustained lag on a long meeting is the thing to avoid, which is why `small` is
not the default.

## Latency — how long until words appear

Speed is not latency. The model can run at 4× real time and still leave a line
sitting invisible for two seconds, because a line cannot be transcribed until
the VAD accepts that the speaker has stopped.

Measured with `tools/bench_pipeline.py`, which replays a fixture through the
real segmenter and the real model at real speed. The metric is the one a user
would describe: **from a turn ending to its words on screen.**

**v1.1.1 baseline — webrtcvad @ 2, one shared hysteresis window:**

| Fixture | Wait (median) | Wait (worst) | Segments from 6 turns | WER | CER |
|---|---|---|---|---|---|
| `speech-clean` | 1.74 s | 1.85 s | 8 | 7.2% | 0.3% |
| `speech-cafe` (10 dB SNR) | **16.6 s** | **30.8 s** | **1** | 8.7% | 1.6% |
| `speech-music` (5 dB SNR) | **16.7 s** | **30.9 s** | **1** | 7.2% | 0.3% |
| `music-only` | — | — | 1 (no text) | 0 invented words | — |

The second and third rows are the finding. `webrtcvad` reads steady background
noise as speech — measured frame by frame it called **100% of music** and 82%
of café noise speech — so the window never went quiet, the utterance never
ended, and the whole meeting arrived as **one segment when recording stopped**.
Not a slow transcript: no transcript until you press Stop, and proportionally
worse on a real meeting than on a 32-second clip. It is also why those rows
show a *lower* per-segment decode cost — one long segment is cheaper to decode
than eight short ones, which flatters every metric except the only one that
counts.

Accuracy was never the problem. Whisper handled café noise at 1.6% CER and
invented nothing over music with no speech under it. The gate in front of it
was the problem.

**v1.2.0 — Silero VAD, separate onset and release windows:**

| Fixture | Wait (median) | Wait (worst) | Segments from 6 turns | WER | CER |
|---|---|---|---|---|---|
| `speech-clean` | **1.68 s** | 1.84 s | 8 | 7.2% | 0.3% |
| `speech-cafe` (10 dB SNR) | **1.66 s** | 1.73 s | 8 | 7.2% | **0.3%** |
| `speech-music` (5 dB SNR) | **1.52 s** | 1.62 s | 8 | 7.2% | 0.3% |
| `music-only` | — | — | 2 (no text) | 0 invented words | — |

A noisy room now behaves like a quiet one — **16.6 s to 1.66 s**, and café CER
improves from 1.6% to 0.3% because the transcript is no longer one 32-second
block. Silero also turned out to be *cheaper*: across three runs each it decoded
at 251–256 ms per second of speech against webrtcvad's 313–512 ms, and its
worst-case wait was 1.71 s against 27.2 s. The VAD itself costs 0.55 ms per
32 ms frame, under 2% of the frame it judges.

Two details that are easy to get wrong, both measured:

- **Silero needs context.** Its recurrent state is zeroed on every call, so
  feeding one frame at a time drops recall from 86% to 67%. It is fed a rolling
  256 ms window and only the newest probability is used, which restores 99.3%
  agreement with scoring the whole file at once. Larger windows measured no
  better.
- **Onset and release need separate windows.** Sharing one — the original
  design — meant an utterance could only begin once 90% of the last 800 ms read
  as speech. webrtcvad is generous enough for that to fire instantly; Silero is
  selective enough that it fired late, after the rolling buffer had already
  discarded the opening words. Whole phrases went missing. Onset is now judged
  over its own 160 ms window with a 300 ms lead-in buffer prepended.

**What was measured and deliberately *not* built:** a spectral music/noise gate
and a denoise pass, both planned. With Silero in place café CER is already 0.3%
— identical to clean audio, so there is nothing for denoise to recover — and
`music-only` already produces zero words. Both would have been code carrying
risk for a gain the fixtures say does not exist. If a real-world clip ever shows
otherwise, the bench is how that gets decided.

## Provisional text — what you see while somebody is still talking

A line cannot be transcribed until the speaker stops, so the wait above has a
floor no amount of tuning removes. What removes the *feeling* of it is showing
provisional words mid-sentence and replacing them with the real line a moment
later.

Measured from the instant a speaker **starts** talking, which is when a person
starts waiting:

| Fixture | Provisional | Finished line | Text arrives sooner by |
|---|---|---|---|
| `speech-clean` (3.8 s turns) | **2.11 s** | 5.92 s | **3.8 s** |
| `speech-cafe` | 1.92 s | 5.90 s | 4.0 s |
| `speech-music` | 2.14 s | 5.82 s | 3.7 s |
| `speech-monologue` (33 s turn) | **1.54 s** | 35.63 s | **34.1 s** |
| `music-only` | none emitted | — | — |

The monologue row is the one that matters. Someone explaining something for
half a minute used to watch an empty panel for 18 seconds before the first
finished line appeared. Now words appear at 1.5 s and keep updating.

**The cost, stated plainly:** the finished line lands about **0.4 s later**
(1.68 s → ~2.1 s after a turn ends), because provisional and final decoding
share one model and a final cannot interrupt a partial already running. Text
3.8 s sooner against a final 0.4 s later is a trade worth making, but it is a
trade.

Two rules keep it from being worse than that, and they are the whole design:

- **Finals always win.** A waiting partial is discarded the moment a finished
  utterance is queued, and a decoded partial is thrown away if a final landed
  while it was decoding. Provisional text is decoration; the saved transcript
  is the product.
- **Partials wait for themselves.** The next one cannot start until as long has
  passed as the last one took, capping provisional work at half the model's
  time. On a slow machine the feature stretches out and eventually stops
  refreshing rather than starving the transcript.

Front ends with nowhere to display it — the terminal, MCP — never pass an
``on_partial``, and then no provisional audio is buffered or decoded at all.

**Why provisional text cannot simply be made cheaper:** Whisper pads every
input to a 30-second mel window, so decode cost is nearly independent of how
much audio you hand it — measured at **1834 ms for 0.5 s** of audio against
**1859 ms for 8 s**. Sending shorter partials saves nothing. The only lever is
a smaller model, which is what `PARTIAL_MODEL` is. Setting it to `"tiny"` buys
a *smoother* line rather than an earlier one: first words at about the same
moment, then refreshing every ~1.2 s instead of ~2.4 s (25 updates across the
monologue against 8), for ~66 MB resident and one more first-run download.

Regenerate the fixtures and re-measure with:

```
python tools/make_fixtures.py
python tools/bench_pipeline.py --all --repeat 2 --json result.json
python tools/bench_pipeline.py --fixture speech-cafe --vad webrtc   # compare
```

`--repeat` matters: a laptop under load varies enough to fake a result in
either direction, and the first comparison this work produced was wrong for
exactly that reason.

## What makes it light

- **`base` multilingual, not `small`.** Half the RAM and ~4× the speed of
  `small`, while still covering all 100 languages.
- **Greedy decoding** (`WHISPER_BEAM_SIZE = 1`). Beam search at width 5 costs
  2–3× the CPU for very little gain on short meeting utterances.
- **`int8` quantization** — roughly a quarter the memory of float32.
- **The model is freed when you stop** (`RELEASE_MODEL_WHEN_IDLE`). An app left
  open all day idles at ~90 MB instead of holding ~340 MB. Reloading costs 2–5 s
  at the next recording.
- **Silence is never transcribed.** The VAD gate means a quiet meeting costs
  almost nothing — CPU scales with speech, not wall-clock time.
- **`condition_on_previous_text=False`** — each utterance is independent, which
  is both faster and stops the model inventing continuity.
- **Lazy imports.** faster-whisper, `soundcard`, and the Google/Microsoft client
  libraries load only when actually used.

## Tuning for a minimal machine

```python
# config.py
WHISPER_MODEL = "tiny"        # or use Settings → Light
WHISPER_BEAM_SIZE = 1
WHISPER_CPU_THREADS = 2       # leave cores for the rest of the system
CAPTURE_MODE = "mic"          # one stream instead of two
RELEASE_MODEL_WHEN_IDLE = True
```

`WHISPER_CPU_THREADS` is the one to reach for on a 2-core machine: unrestricted,
CTranslate2 will take every core and make the desktop feel sluggish while it
transcribes.

## Rough floor

| Resource | Minimum | Comfortable |
|---|---|---|
| RAM (app only) | 512 MB free | 1 GB free |
| RAM (with Ollama summarizing) | 3 GB free | 4 GB+ |
| CPU | 2 cores, Light profile | 4 cores, Balanced |
| Disk | ~200 MB (`tiny`) | ~1 GB (`base` + Ollama model) |

The summarization step, not the transcription, sets the real memory floor. If
RAM is genuinely scarce, point `OLLAMA_MODEL` at something small
(`qwen2.5:1.5b`, `llama3.2:1b`) — or skip Ollama entirely and keep the
transcript, which is saved regardless of whether summarization succeeds.
