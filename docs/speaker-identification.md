# Capturing both ends, and knowing who spoke

Two separate problems get confused with each other. Solve them in order.

1. **Capture** — hearing everyone, not just yourself.
2. **Attribution** — putting a name against each line.

Stage 1 and stage 2 below are **built and working today**. Stages 3 and 4 are the
proposed path, with the trade-offs that matter for a local-first product.

---

## Stage 1 — Capture both ends *(shipped)*

A video call has two audio paths, and a microphone only carries one of them:

| Path | What it holds | How to capture it |
|---|---|---|
| Microphone | you, in the room | PortAudio (`sounddevice`) |
| System output (loopback) | **everyone else**, exactly as your speakers play them | WASAPI loopback / PulseAudio monitor (`soundcard`) |

Set `CAPTURE_MODE = "both"` (the default) and the app opens **both**, each with
its own VAD state machine, and merges the utterances into one transcript.

Why loopback rather than a meeting bot: no bot joins the call, no per-platform
Zoom/Teams/Meet integration to build and maintain, nothing is uploaded, and it
works on any call — including one nobody gave you permission to add a bot to.

**Platform support**

| OS | Mechanism | Setup |
|---|---|---|
| Windows | WASAPI loopback | none — works out of the box |
| Linux | PulseAudio `.monitor` source | none on PulseAudio/PipeWire |
| macOS | no OS-level loopback exists | install [BlackHole](https://existential.audio/blackhole/), send output to it, set `INPUT_DEVICE` |

Check the machine you're on:

```bash
python vlocalhost.py --devices
```

> **Consent.** Recording the far end of a call is a legal question, not a
> technical one, and it varies by jurisdiction — many places require every party
> to agree. Tell people they're being recorded.

---

## Stage 2 — Attribution by source *(shipped)*

Because the two streams stay separate all the way through the VAD, every line
already knows which side it came from:

```
[10:04:12] You: I think we should push the launch to Friday.
[10:04:19] Participants: Agreed, but the vendor contract has to close first.
```

This is exact — not a guess. There is no clustering to get wrong, and it costs
nothing extra. Rename the sides with `LABEL_ME` / `LABEL_THEM` in `config.py`.

**The limit:** everyone on the far end shares one label. For a 1:1 that is the
complete answer. For a six-person call it tells you *"not you"* and no more.

---

## Stage 3 — Split the far end into speakers *(proposed)*

Turn `Participants` into `Speaker 1`, `Speaker 2`, `Speaker 3` by clustering
voices — diarization.

**Recommended: embeddings + online clustering.** The app already slices audio
into utterances at natural speech boundaries, which is the hard part of
diarization. Each utterance needs a voice fingerprint, then utterances that
sound alike get grouped:

1. Embed each far-end utterance with a speaker-encoder — `speechbrain`'s
   ECAPA-TDNN, or `resemblyzer` (lighter, ~15 MB, CPU-friendly). Output is a
   192–256 dim vector.
2. Cluster online with cosine similarity: compare against known centroids, and
   if the best match is below a threshold (~0.75 for ECAPA), start a new
   speaker. Update the centroid with a running mean.
3. Label the line with the resulting cluster id.

Roughly 150 lines against the existing pipeline, no re-architecture, still fully
offline, and it runs per-utterance so latency stays hidden behind transcription.

| Option | Accuracy | Cost | Verdict |
|---|---|---|---|
| **Resemblyzer + online clustering** | good | ~15 MB, fast on CPU | **start here** |
| SpeechBrain ECAPA + clustering | better | ~80 MB, slower | upgrade path |
| `pyannote.audio` 3.x full pipeline | best | ~500 MB, gated HF token, GPU-ish, needs whole-file audio | too heavy, and the token requirement breaks "install and go" |
| WhisperX | best-in-class alignment | wraps pyannote, batch-only | not viable for live capture |

**Known failure modes, worth stating up front:** two people talking over each
other land in one utterance and get one label; a speaker on a bad connection can
split into two clusters; and the count of speakers is discovered, never known.
Diarization is heuristic in a way stage 2 is not — keep the `You` /
`Participants` split underneath it as ground truth so a clustering mistake can
never make your own words look like someone else's.

Suggested config surface:

```python
DIARIZE_PARTICIPANTS = False   # split the far end into Speaker 1/2/3
DIARIZE_THRESHOLD = 0.75       # lower = fewer, broader speakers
```

---

## Stage 4 — Put real names on the speakers *(proposed)*

Cluster ids are still not names. Three ways to close that gap, cheapest first:

1. **Calendar attendees + the transcript.** The event already gives the attendee
   list. Feed it to the local Ollama model along with the diarized transcript
   and let it map clusters to names from self-introductions and direct address
   ("thanks, Priya"). Free, offline, no enrolment — but it guesses, so render
   the result as *likely* and let the user correct it.
2. **Voice enrolment.** Keep a small local profile store: once a cluster is
   named, save its centroid to the config folder and match it in later meetings.
   The second meeting with the same colleague is then labelled automatically.
   This is the strongest option, and it means storing voiceprints — make it
   opt-in, keep it on-device, and let people delete a profile.
3. **Per-participant streams.** Zoom/Teams/Meet APIs can deliver separate audio
   per participant, which makes attribution exact. It also means a bot in the
   call, cloud credentials, and a per-platform integration to maintain — the
   opposite of this product's premise. Reserve it for an enterprise tier.

---

## Suggested order

| Step | Work | Payoff |
|---|---|---|
| ~~Loopback capture~~ | ~~done~~ | you hear the whole meeting at all |
| ~~You vs. Participants~~ | ~~done~~ | correct two-sided transcript, zero guessing |
| Resemblyzer diarization | ~2 days | Speaker 1/2/3 on the far end |
| Ollama name-mapping from attendees | ~1 day | real names, no enrolment |
| Voice profile store | ~3 days | names that persist across meetings |

Stage 2 already covers a 1:1 completely, so stage 3 only pays off for group
calls — size the work against how many of those your users actually run.
