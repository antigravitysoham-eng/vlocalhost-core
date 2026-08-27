# Vlocalhost Core — Meeting Notes

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

A meeting note-taker that runs **entirely on your machine**. It listens to both
sides of the call, transcribes whoever is speaking, and writes clean, structured
notes when you stop. No bot joins your meeting. Nothing is uploaded.

```
your mic ─────┐
              ├─▶ voice activity detection ─▶ faster-whisper ─▶ transcript
system audio ─┘        (silence ignored)         (local)            │
   (everyone else)                                                  │
                                              Ollama (local) ───────┘──▶ notes.md
```

- **Both ends** — your microphone *and* the meeting audio from your speakers,
  labelled `You` and `Participants`.
- **100 languages** — English out of the box; switch to Hindi, Bengali,
  Marathi, Gujarati, Tamil, Telugu, Kannada, Malayalam, Punjabi, Urdu, French,
  Spanish, Japanese, Chinese and more, or auto-detect per utterance.
- **Light** — ~350 MB while recording, ~90 MB idle, 4.5× real time on a
  6-core CPU. [Measured.](docs/performance.md)
- **Silence is ignored** — it only transcribes real speech.
- **Private by construction** — see below.
- **Four front ends** — a window, a tray icon, a terminal, and an MCP server
  that lets Claude or Cursor drive it.

Runs on **Windows, macOS, and Linux**. Free forever, AGPL-3.0.

## Private by construction, not by promise

Plenty of tools promise not to upload your meetings. This one ships without the
ability to.

Core contains **no calendar or email providers at all** — not disabled ones, not
ones behind a setting. The interface for them exists; no implementation is
installed. There is no code path in this repository that can send meeting
content anywhere. The only outbound traffic is the one-time model download and a
call to Ollama on `127.0.0.1`.

You do not have to trust a toggle. You can read `requirements.txt`.

## 1. Prerequisites

- **Python 3.9+**
- **Ollama** for summarization — <https://ollama.com/download>
  ```bash
  ollama pull llama3.2      # or any model you like; set it in the app
  ```
  Make sure Ollama is running (`ollama serve`, or just launch the app).
- **PortAudio** — the microphone backend. Bundled on Windows; install it on the others:

  | OS | Command |
  |---|---|
  | **Windows** | Nothing to do — bundled with the `sounddevice` wheel. |
  | **macOS** | `brew install portaudio` |
  | **Linux (Debian/Ubuntu)** | `sudo apt install libportaudio2` |
  | **Linux (Fedora/RHEL)** | `sudo dnf install portaudio` |

- **Linux tray icon (optional)** — `sudo apt install gir1.2-appindicator3-0.1`.
  Without it the app still works and falls back to terminal mode.

## 2. Install

```bash
git clone https://github.com/antigravitysoham-eng/vlocalhost-core.git
cd vlocalhost-core
python -m venv .venv

# Activate the virtual environment:
.venv\Scripts\activate          # Windows (PowerShell / cmd)
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

> The first run downloads the Whisper model (~145 MB for the default `base`). Cached after that.
>
> **macOS**: the first run will prompt for **microphone permission** — allow it, then re-run.

## 3. Run

```bash
python vlocalhost.py
```

The window opens on **Record** — press **● Start recording**, talk, then
**■ Stop & save**. Lines appear live as people speak. Everything else (capture
source, models, languages) lives in the other tabs; you never edit Python.

Other front ends, same engine:

```bash
python vlocalhost.py --tray      # system-tray icon only
python vlocalhost.py --no-tray   # terminal, Ctrl+C to finish
python vlocalhost.py --devices   # what this machine can record
python vlocalhost.py --mcp       # MCP server on stdio (see §7)
```

Every meeting produces **two files** in `notes/`, named after the meeting. They
are deliberately different documents, not two copies of the same one:

- `<date>_<meeting-title>-transcript.txt` — **the transcript.** The full record, every line
  stamped `[HH:MM:SS]`, in whatever languages were spoken. This is the evidence.
- `<date>_<meeting-title>-summary.txt` — **the summary.** Summary / Key Points /
  Decisions / Action Items, and **no timestamps** — clock times are stripped
  before the model sees the transcript and scrubbed again from what it returns.
  A time only appears here if somebody actually said one, as a deadline or a
  next meeting.

If the summary read like a second copy of the transcript in an older build, that
is what this fixes: the model was being handed a timestamp on every line and
told the transcript was timestamped, so it mirrored the format straight back.

## 4. Recording both sides of a call

A microphone only carries **your** half of a video call. Vlocalhost also
captures your **system audio** — everyone else, exactly as your speakers play
them — and labels each line:

```
[10:04:12] You: I think we should push the launch to Friday.
[10:04:19] Participants: Agreed, but the vendor contract has to close first.
```

Choose the source under **Settings → What to listen to**:

| Mode | Records | Use for |
|---|---|---|
| **Both** *(default)* | mic + system audio | video calls |
| Mic only | your microphone | in-person meetings |
| System only | system audio | webinars you're only listening to |

| OS | System audio | Setup |
|---|---|---|
| Windows | WASAPI loopback | none — works out of the box |
| Linux | PulseAudio monitor | none on PulseAudio/PipeWire |
| macOS | no OS-level loopback | install [BlackHole](https://existential.audio/blackhole/), route output into it, set `INPUT_DEVICE` in `config.py` |

Run `python vlocalhost.py --devices` to see what your machine supports.

Splitting `Participants` into individual named speakers is the next step —
the approach, the trade-offs, and the effort are laid out in
[`docs/speaker-identification.md`](docs/speaker-identification.md).

> **Consent:** recording other people is a legal question that varies by
> jurisdiction, and many places require everyone to agree. Tell people.

## 5. Languages

Vlocalhost transcribes **100 languages**. It listens for **English** unless you
say otherwise: pinning the language is faster and more accurate than detecting
it, and meeting utterances are short enough that detection does get things
wrong. Pick another under **Settings → Spoken language**, or choose
**Auto-detect** — that detects the language **per utterance**, so a meeting that
switches between them transcribes correctly as it goes, and each line is tagged
with what was detected:

```
[10:04:12] You (en): Can you send the vendor analysis?
[10:04:19] Participants (hi): हाँ, मैं आज शाम तक भेज दूँगा।
```

Covered, among others: **Hindi, Bengali, Marathi, Gujarati, Tamil, Telugu,
Kannada, Malayalam, Punjabi, Urdu, Assamese, Sanskrit, Sindhi, Nepali, Sinhala**,
plus French, Spanish, Japanese, Chinese, Cantonese, German, Portuguese, Arabic,
Korean and more.

**Not supported by Whisper at any model size:** Irish (Gaeilge), Scottish
Gaelic, **Odia**, Konkani, Maithili, Bhojpuri, Manipuri, Dogri, Kashmiri,
Santali, Bodo. The app refuses these rather than pretending.

Two things worth knowing:

- **The `.en` models are English-only.** Given Hindi they don't error, they
  hallucinate English. The app **blocks** that pairing instead of producing
  confident nonsense.
- **Mixing languages inside one sentence** (Hinglish — *"deployment ka status
  kya hai"*) doesn't work. Whisper picks one language per utterance. Switching
  *between* utterances is handled correctly.

Two more switches in **Settings**: translate everything to English as it's
transcribed, and always write the notes in English whatever was spoken.

## 6. How light is it?

Measured on a 6-core Ryzen with both capture sources running:

| Profile | Model | While recording | Peak | Speed |
|---|---|---|---|---|
| **Light** | `tiny` | ~226 MB | ~250 MB | large headroom |
| **Balanced** *(default)* | `base` | ~337 MB | **~347 MB** | **4.5× real time** |
| **Accurate** | `small` | ~687 MB | ~734 MB | ~real time; can lag |

Idle — window open, nothing recording — is **~90 MB**, because the model is
freed when you stop. Pick a profile under **Settings → Performance**, or press
**Benchmark this machine** to measure your own hardware.

Ollama is a separate process and only runs when you stop a recording; it needs
~2 GB of its own while summarizing. Full detail, and how to tune for a 2-core
machine, in [`docs/performance.md`](docs/performance.md).

## 7. Connect an AI assistant (MCP)

The app ships an **MCP server**, so Claude Code, Claude Desktop, Cursor, or any
other MCP client can start a recording, follow the live transcript and search
past meetings — while the audio and the models stay local.

Register it (**Settings → Copy MCP config** fills in the right paths):

```json
{
  "mcpServers": {
    "vlocalhost": {
      "command": "python",
      "args": ["/full/path/to/mcp_server.py"]
    }
  }
}
```

- **Claude Code** — `claude mcp add vlocalhost -- python /full/path/to/mcp_server.py`
- **Claude Desktop** — `claude_desktop_config.json`
- **Cursor** — `.cursor/mcp.json`

Then restart the client and ask it to *"start recording this meeting"* or
*"what did we decide about the vendor contract?"*

| Tool | What it does |
|---|---|
| `start_recording` / `stop_recording` | run a session; stop returns the notes |
| `recording_status` | live state, model health |
| `live_transcript` | the transcript so far |
| `list_notes` / `read_note` / `search_notes` | your saved meetings |
| `connection_status` | which models and providers are ready |
| `upcoming_meetings` | *needs a calendar provider — see §9* |
| `email_notes` | *needs a mail provider — see §9* |

Only one process can hold the microphone: if the window is already recording,
the MCP server says so instead of fighting over the device.

## 8. Tuning

Most settings live in the **Settings** tab and are saved to your config folder.
The advanced knobs are in `config.py`:

| Setting | What it does |
|---|---|
| `CAPTURE_MODE` | `"both"` / `"mic"` / `"system"` — what to listen to. |
| `LABEL_ME`, `LABEL_THEM` | How each side is named in the transcript. |
| `INPUT_DEVICE` | Microphone index or name substring; `None` = default. |
| `WHISPER_MODEL` | Accuracy vs speed: `tiny` → `base` → `small` → `medium`. A `.en` suffix means English-only. **Bring your own model:** a name, a Hugging Face repo id, or a **local folder** — any size, any language. |
| `CUSTOM_TRANSCRIBER` | Attach a custom STT engine (`"module:ClassName"` with `.transcribe(pcm_bytes)`) and bypass faster-whisper entirely. |
| `WHISPER_LANGUAGE` | `"en"` by default. Another ISO code like `"hi"`, or `None` to detect per utterance. |
| `WHISPER_TASK` | `"transcribe"` keeps the spoken language; `"translate"` outputs English. |
| `WHISPER_BEAM_SIZE` | `1` = greedy (fast). `5` = slower, marginally better. |
| `WHISPER_CPU_THREADS` | `0` = all cores. Set `2` on a small machine. |
| `RELEASE_MODEL_WHEN_IDLE` | Free the model's RAM between recordings. |
| `NOTES_LANGUAGE` | `"en"` forces English notes; `"same"` keeps the spoken language. |
| `VAD_AGGRESSIVENESS` | 0–3. Higher = only transcribe clear speech. |
| `SILENCE_TIMEOUT_MS` | How long a pause ends an utterance (default 800 ms). |
| `OLLAMA_MODEL` | Any model you've pulled, e.g. `llama3.1`, `mistral`, `qwen2.5`. |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE` | Set `cuda` / `float16` for GPU acceleration. |

## 9. Extending Core: calendar and email providers

Core defines a `CalendarProvider` interface and a registry, and ships **no**
implementations. If one is registered, the app gains the ability to name notes
from the calendar event, start and stop recording around meetings, email the
summary to attendees, and post the notes back onto the event.

Writing your own is a normal Python package:

```python
# my_provider/__init__.py
from integrations import register_provider
from integrations.base import CalendarProvider

class MyProvider(CalendarProvider):
    name = "mine"
    ...   # see integrations/base.py for the interface

def register():
    register_provider("mine", MyProvider, label="My calendar")
```

Add its import name to `PLUGIN_MODULES` in `plugins.py` and the Connections tab,
the `--connect` command and the MCP tools all pick it up — no changes to Core.
`integrations/base.py` documents every method, including how a provider
describes its own credential prompt so the window stays generic.

**Vlocalhost Pro** is the commercial edition that ships ready-made Google
(Calendar + Gmail) and Microsoft (Outlook Calendar + Mail) providers, plus
automation, team and enterprise features, built on exactly this extension point
— <https://vlocalhost.ai/pricing>. Core is not a trial and does not expire; if
you only want to record your own meetings privately, you are already done.

## Notes & limits

- Accuracy depends on mic quality and the Whisper model size. Bump the model if
  the transcript is rough.
- Everyone on the far end currently shares one label — see
  [`docs/speaker-identification.md`](docs/speaker-identification.md).
- Summarization needs Ollama running; if it isn't, the transcript is still saved
  and the app tells you the summary step failed.

## Files

| File | Role |
|---|---|
| `vlocalhost.py` | Entry point — window, tray, terminal, MCP. |
| `gui.py` | The desktop window. |
| `engine.py` | Shared session engine every front end drives. |
| `mcp_server.py` | MCP server (stdio, no extra dependencies). |
| `notetaker.py` | Capture → transcribe → summarize; saves the files. |
| `audio_listener.py` | Mic + system-audio capture and VAD segmentation. |
| `transcriber.py` | faster-whisper wrapper (or your own engine). |
| `summarizer.py` | Ollama wrapper + notes prompt. |
| `config.py` | Defaults and advanced settings. |
| `languages.py` | Supported languages + the English-only-model guard. |
| `performance.py` | Light/Balanced/Accurate profiles and the benchmark. |
| `settings.py` | Settings the app writes at runtime. |
| `scheduler.py` | Auto-records around meetings, when a calendar provider exists. |
| `plugins.py` | Discovers optional packages at startup. |
| `integrations/` | The provider interface and registry — no providers included. |

## Contributing

Pull requests are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md)
first — it covers the CLA and the one architectural rule that keeps this
codebase from forking in two.

Security issues: [`SECURITY.md`](SECURITY.md), never a public issue.

## Licence

Vlocalhost Core is free software under the **GNU Affero General Public License
v3.0** — see [`LICENSE`](LICENSE). You may use, study, modify and redistribute
it; if you distribute a modified version, or run one as a network service, you
must publish your changes under the same licence.

The **name and logo are not covered** by that licence. If you fork and
distribute this, rename it — see [`TRADEMARK.md`](TRADEMARK.md).
