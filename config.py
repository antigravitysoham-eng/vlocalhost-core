"""Central configuration for the Meeting Notes Agent.

Everything is local/offline: mic -> VAD -> faster-whisper -> Ollama.
Tweak values here; no other file needs editing for normal use.
"""

# --- Audio capture -------------------------------------------------------
SAMPLE_RATE = 16000          # Hz. Whisper + webrtcvad both expect 16 kHz mono.
FRAME_MS = 30                # webrtcvad accepts 10, 20, or 30 ms frames.
CHANNELS = 1

# WHAT TO LISTEN TO. A video call has two ends: you (microphone) and everyone
# else (whatever your speakers are playing). Capturing both is what makes a
# meeting transcript complete.
#
#   "mic"    — your microphone only.
#   "both"   — your mic AND the system audio, each labelled. Recommended for
#              video calls. No bot joins the meeting; nothing is sent anywhere.
#   "system" — the system audio only (e.g. transcribing a recording or a
#              webinar you're only listening to).
#
# "both"/"system" need the loopback backend:  pip install soundcard
# Windows (WASAPI) and Linux (PulseAudio monitor) work out of the box. macOS
# has no OS-level loopback — install BlackHole and set INPUT_DEVICE below.
CAPTURE_MODE = "both"

# How each side is labelled in the transcript, e.g. "[10:04:12] You: ...".
LABEL_ME = "You"
LABEL_THEM = "Participants"

# Microphone to record from: None = the system default. Can be a device index
# or a name substring (see `python -m sounddevice` for the list). On macOS,
# point this at BlackHole to capture the far end.
INPUT_DEVICE = None

# --- Voice activity detection (VAD) --------------------------------------
# WHAT COUNTS AS SOMEBODY TALKING. This decides when an utterance ends, and so
# when a line can be transcribed at all — it sets the latency floor, and in a
# noisy room it decides whether the app works.
#
#   "silero" — the neural detector faster-whisper already ships (no extra
#              download, no extra dependency: the ONNX model is in
#              faster_whisper/assets and onnxruntime is already installed).
#              It knows what speech sounds like, so music, a fan or a café
#              stay silent.
#   "webrtc" — the older heuristic. Cheaper, and it has no model of speech:
#              measured on the bench fixtures it calls 100% of music and 82%
#              of café noise "speech", which means an utterance never ends and
#              nothing is transcribed until you press Stop. Kept as a fallback.
VAD_ENGINE = "silero"

# How sure Silero has to be, 0-1. Lower hears more and admits more noise.
# 0.5 measured best on the bench fixtures: it keeps 99-100% of non-speech
# quiet while catching as much speech as scoring the whole file at once.
VAD_THRESHOLD = 0.5

# Only used when VAD_ENGINE = "webrtc".
# 0 = least aggressive (more speech, more false positives)
# 3 = most aggressive (only clear speech). 2 is a good meeting default.
VAD_AGGRESSIVENESS = 2

# How much speech it takes to decide somebody has started talking. Judged over
# its own short window, separate from the silence timeout below: sharing one
# window makes the start of an utterance depend on how long the *end* of one
# takes to detect, which silently swallowed the opening words of every line.
VAD_ONSET_MS = 160
# ...and how much of that window has to be speech. 0.6 of 160 ms is 3 frames
# out of 5: fast enough not to clip a first syllable, strict enough that a
# single stray frame cannot open a segment. Blips shorter than
# MIN_UTTERANCE_MS are dropped at the other end anyway.
VAD_ONSET_RATIO = 0.6
# How much audio from just before the trigger to keep. The detector needs a
# moment to become sure, and this is what puts the first syllable back. It is
# prepended to the utterance and transcribed with it, so oversizing it costs
# decode time on silence.
VAD_LEAD_MS = 300

# End an utterance after this much continuous silence, then transcribe it.
SILENCE_TIMEOUT_MS = 800

# How often to show provisional words while somebody is still talking. This is
# what makes the transcript feel live: the finished line still arrives when the
# speaker stops, but you stop staring at nothing until then.
#
# It is a *floor*, not a promise. Provisional work is skipped whenever a
# finished utterance is waiting, and the next one never starts until as long
# has passed as the last one took — so on a slow machine this quietly stretches
# instead of stealing the model from the transcript that gets saved.
# Front ends that have nowhere to show it (the terminal, MCP) never ask for it
# and pay nothing.
PARTIAL_INTERVAL_MS = 500

# Which model draws the provisional words. None reuses the one above: no extra
# memory, no extra download.
#
# What "tiny" actually buys, measured on a 33-second monologue, is a smoother
# line rather than an earlier one: the first words appear at about the same
# moment either way (~1.6 s, set by the interval above plus one decode), but
# the text then refreshes every ~1.2 s instead of every ~2.4 s — 25 updates
# across the monologue against 8. It costs ~66 MB resident and one more model
# to fetch on first run, which is why it is not the default in a product whose
# promise is that it works offline as soon as it is installed.
#
# Making provisional text *shorter* is not a lever: Whisper pads every input to
# a 30-second window, so half a second of audio costs the same to decode as
# eight seconds (1834 ms against 1859 ms, measured). Model size is the only one.
PARTIAL_MODEL = None

# Ignore blips shorter than this (coughs, clicks) to avoid junk transcripts.
MIN_UTTERANCE_MS = 300

# --- Transcription: bring your own voice model ---------------------------
# The speech-to-text engine. Default is faster-whisper, running fully local.
# There is NO model limitation — attach the model you want, three ways:
#
#   1. Any faster-whisper model NAME below:
#        tiny(.en) base(.en) small(.en) medium(.en) large-v3, or a
#        fine-tuned CTranslate2 model repo id from Hugging Face.
#   2. A LOCAL model FOLDER: set WHISPER_MODEL to a path to your own converted
#        model directory — any size, any language, fully offline, no download.
#        e.g. WHISPER_MODEL = r"C:\models\my-whisper-large"
#   3. A completely custom engine: see CUSTOM_TRANSCRIBER below.
# NOTE ON LANGUAGES: models ending in ".en" are English-ONLY. Given other
# languages they don't error — they hallucinate English. Drop the ".en" for the
# multilingual weights ("tiny" "base" "small" "medium" "large-v3"), which cover
# 100 languages including Hindi, Bengali, Marathi, Gujarati, Tamil, Telugu,
# Kannada, Malayalam, Punjabi, Urdu, French, Spanish, Japanese and Chinese.
# "base" is the default: multilingual, keeps up with live speech on modest
# hardware, ~340 MB. See performance.py for the measured profiles — "tiny" for
# weak machines, "small" when you need the accuracy and have the cores.
WHISPER_MODEL = "base"
# "int8" is fast on CPU. Use "float16" if you have a good GPU + CUDA.
WHISPER_COMPUTE = "int8"
WHISPER_DEVICE = "cpu"       # "cpu" or "cuda"

# 1 = greedy decoding: markedly faster and, on short meeting utterances, barely
# less accurate. Raise to 5 for the best transcription at 2-3x the CPU cost.
WHISPER_BEAM_SIZE = 1

# Threads for the speech model. 0 lets CTranslate2 choose (usually all cores).
# Set 2 on a small machine to leave the rest of the system responsive.
WHISPER_CPU_THREADS = 0

# Unload the speech model when you stop recording, returning a few hundred MB
# to the system. The next recording pays the load cost again (2-5 s).
# Turn off if you start and stop recordings constantly.
RELEASE_MODEL_WHEN_IDLE = True

# Spoken language. "en" out of the box: most meetings are in one known
# language, and pinning it is both faster and more accurate than detection,
# which is unreliable on the short utterances a meeting is made of. Pin another
# code (e.g. "hi") from Settings, or set None to detect the language per
# utterance — right for a meeting that switches between languages mid-sentence.
# See languages.py for the list.
WHISPER_LANGUAGE = "en"

# "transcribe" keeps the original language. "translate" renders any language
# into English (Whisper does this natively, still offline).
WHISPER_TASK = "transcribe"

# Tag lines with the detected language, e.g. "[10:04] You (hi): ...".
# Only applies when auto-detecting; it makes a misdetection visible.
SHOW_DETECTED_LANGUAGE = True

# Language for the generated notes: "en" forces English notes from any spoken
# language, "same" writes them in whatever was spoken.
NOTES_LANGUAGE = "en"

# Attach your OWN speech-to-text engine and bypass faster-whisper entirely.
# Set to "module.path:ClassName" (or "module.path:factory") pointing at an
# object that implements:
#     load(self)                      -> warm up (optional)
#     transcribe(self, pcm_bytes)     -> str   (16-bit mono PCM @ SAMPLE_RATE)
# Example:  CUSTOM_TRANSCRIBER = "my_engine:MyWhisper"
# Leave as None to use faster-whisper with the settings above.
CUSTOM_TRANSCRIBER = None

# --- Summarization (Ollama, runs locally) --------------------------------
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"    # pull first:  ollama pull llama3.2

# --- Output --------------------------------------------------------------
# Where transcripts and summaries are written. A plain name lands inside your
# per-user data folder — NOT next to this file, so updating or reinstalling the
# app can never touch your notes. Run `python vlocalhost.py --paths` to see the
# real location. Set an absolute path to keep notes somewhere else entirely,
# e.g. a synced folder:  OUTPUT_DIR = r"D:\Dropbox\Meetings"
OUTPUT_DIR = "notes"

# --- Calendar / email integration (optional) -----------------------------
# Core ships no providers, so the settings below do nothing on their own —
# they are the contract an installed provider plugs into. See README §9 and
# integrations/base.py to write one, or run:
#     python vlocalhost.py --connect
# to list whatever this build has.
#
# Which provider to use for calendar + email, or None to stay entirely local.
CALENDAR_PROVIDER = None     # None, or a registered provider name

# Automatically start listening when a calendar meeting begins, and stop +
# save when it ends. Requires a connected provider.
AUTO_START_FROM_CALENDAR = False
CALENDAR_POLL_SECONDS = 60   # how often to check the calendar for a live meeting
AUTO_START_GRACE_MINUTES = 2 # begin this many minutes before the scheduled start

# After a meeting is saved, email the notes to the event's attendees.
EMAIL_SUMMARY_TO_ATTENDEES = False
# Also send a copy to yourself even if you're the only attendee.
EMAIL_SUMMARY_TO_SELF = True

# After a meeting is saved, write the notes back into the calendar event.
POST_NOTES_TO_EVENT = False
