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
# 0 = least aggressive (more speech, more false positives)
# 3 = most aggressive (only clear speech). 2 is a good meeting default.
VAD_AGGRESSIVENESS = 2

# End an utterance after this much continuous silence, then transcribe it.
#
# This looks like the latency knob and mostly isn't. Measured on an M-series
# Mac with the "base" model, halving it to 400 ms moved the finished line from
# 1.06 s to 0.95 s after the speaker stopped — the decode dominates — while
# splitting the same paragraph into four lines instead of two, because the
# pauses inside a sentence started ending it. LIVE_PARTIALS below is what
# actually removes the wait. Lower this only if you want lines to commit in
# smaller pieces, and know that is what you are choosing.
SILENCE_TIMEOUT_MS = 800

# How much speech has to arrive before capture begins. Separate from the
# silence timeout so that shortening one doesn't make the other trigger-happy —
# they were a single value, which meant a quicker cut-off also let a cough
# start a segment.
SPEECH_TRIGGER_MS = 240

# Audio kept from *before* the trigger fired, so the first syllable isn't
# clipped off the front of the utterance that gets transcribed.
PREROLL_MS = 300

# Ignore blips shorter than this (coughs, clicks) to avoid junk transcripts.
MIN_UTTERANCE_MS = 300

# --- Live (partial) transcription ----------------------------------------
# Transcribe what has been said *so far*, while the person is still speaking,
# and show it as provisional text that the finished line replaces. Waiting for
# the pause is what makes transcription feel slow; this fills the wait.
#
# It costs real CPU: another pass over the utterance every interval, on top of
# the final one. The encoder cost is roughly fixed per pass rather than
# proportional to length (~400 ms for "base" on an M-series Mac, ~200 ms for
# "tiny"), which is what makes it affordable at all. Turn it off on a machine
# that is already working hard, or when running on battery.
LIVE_PARTIALS = True

# How often to refresh the provisional text. Setting this below the time one
# pass takes doesn't make it faster — passes are never queued up, and a stale
# one is dropped rather than run late — it just spends more CPU.
PARTIAL_INTERVAL_MS = 700

# Only ever re-transcribe the last N seconds for a provisional line. A long
# uninterrupted monologue would otherwise get slower to preview the longer it
# ran. The final line is always transcribed whole, so nothing is lost from the
# saved transcript — only the preview is clipped.
PARTIAL_MAX_SECONDS = 12

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
