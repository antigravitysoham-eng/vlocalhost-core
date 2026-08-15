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

## Latency, which is not the same as speed

How fast the model runs and how long you wait to see a word are different
numbers. A line cannot appear until the speaker pauses long enough to end the
utterance (`SILENCE_TIMEOUT_MS`, 800 ms) and the model has then decoded it. On
an M-series Mac with `base`, that decode is **~400–550 ms for anything up to
about 8 s of audio** — there is a fixed encoder cost per pass, so a 2 s
utterance costs nearly what an 8 s one does. Only past ~10 s does length begin
to dominate (16 s → ~1.1 s).

The consequence is that a long sentence shows nothing at all until it ends.
Speak for six seconds and the first text arrives in the seventh.

**`LIVE_PARTIALS`** (on by default) fills that gap: while someone is still
talking, the utterance so far is transcribed every `PARTIAL_INTERVAL_MS` and
shown as dimmed provisional text, which the finished line then replaces.
Measured on the same machine against a 16 s spoken paragraph:

| | first text on screen | line committed after speaker stops |
|---|---|---|
| Partials off | +6.5 s | 0.8–1.1 s |
| Partials on | **+1.8 s**, refreshing every ~0.7 s | 0.6–1.3 s |

The right-hand column barely moves — partials don't make the transcript
faster, they make the wait visible. Nothing provisional is ever saved: the
committed line is transcribed from the whole utterance, so the file on disk is
identical either way.

The cost is another decode pass per interval, which is why it is a switch. Two
passes never run at once, a stale preview is dropped rather than run late, and
a finished utterance always takes priority — so the worst case is CPU spent,
not a line arriving later. Turn it off (Settings, or `LIVE_PARTIALS = False`)
on a machine that is already working hard.

`SILENCE_TIMEOUT_MS` looks like the latency knob and mostly isn't: halving it
to 400 ms moved the committed line by about 0.1 s, and split the same paragraph
into twice as many lines, because the pauses inside a sentence started ending
it.

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
