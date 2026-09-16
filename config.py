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

# Which library records the microphone.
#
#   "soundcard"  WASAPI directly, the same path system audio already uses
#   "portaudio"  the historical path, via sounddevice
#   "auto"       soundcard on Windows, PortAudio elsewhere
#
# This is not a preference about code. A bare PortAudio input stream -- no
# voice detection, no model, none of this application -- distorted a user's
# voice for the other people on a Teams call in Chrome, reproducibly, and
# stopped the moment the stream closed. An always-on dictation tool on the same
# machine never did, and it is an Electron app: it captures through Chromium's
# audio engine, which is what the call itself uses.
#
# soundcard is not a new dependency; it is what records system audio today, on
# every recording, and has done so without this problem.
MIC_BACKEND = "auto"
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
# "tiny", not "base", and the reason is not accuracy.
#
# `base` running on this machine distorted the user's own voice for the other
# people on a browser call, for as long as a recording was running. Measured on
# a live call, everything else held equal:
#
#     no speech model at all            clean
#     base, 12 threads                  distorted
#     base, 4 threads                   WORSE -- the load stretches, not shrinks
#     base, below-normal priority       no change
#     base + OMP_WAIT_POLICY=PASSIVE    better, still audible
#     tiny                              clean
#
# Six other explanations were tested on that call and eliminated: the device
# re-scan, the sample-rate conversion, the channel remix, the loopback stream,
# the mic backend, and the thread count. Capture is innocent in every form --
# opening the microphone and discarding the audio disturbs nothing. It is the
# decode that starves the call, and priority not helping while fewer threads
# made it worse points at memory bandwidth rather than CPU time.
#
# So the default is the model that does not spoil the meeting it is recording.
# A worse transcript is a cost the user can see and correct; sounding broken to
# everyone else is one they cannot, and they will not know it is us.
#
# Set "base" or "small" for a better transcript when nothing else needs the
# machine -- recording a room, or a call in a desktop client rather than a
# browser. The real fix is a cheaper architecture: a streaming transducer costs
# a fraction of an encoder-decoder per second of audio. See the notes on
# Parakeet in the release-and-security policy.
WHISPER_MODEL = "tiny"
# Which revision of a Hugging Face model to fetch. Unpinned, you get whatever
# is at the head of the repo on the day you install -- so two machines running
# "base" can hold different weights, and a change upstream arrives without
# anyone deciding to take it. Pinning makes the download reproducible and is
# the difference between "we ship Whisper" and "we ship this Whisper" in a
# security review.
#
# Applies only to models fetched by name or repo id. A local folder is already
# pinned by virtue of being a folder. Set to None to track the head again.
WHISPER_REVISION = {
    "tiny":  None,
    "base":  "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    "small": None,
}

# Where a bundled copy of the models lives, for an installer that ships them.
# When set, this is used as the Hugging Face cache, so a first run finds the
# weights already present and never reaches the network. Empty means the
# ordinary per-user cache.
BUNDLED_MODELS_DIR = ""

# "int8" is fast on CPU. Use "float16" if you have a good GPU + CUDA.
WHISPER_COMPUTE = "int8"
WHISPER_DEVICE = "cpu"       # "cpu" or "cuda"

# 1 = greedy decoding: markedly faster and, on short meeting utterances, barely
# less accurate. Raise to 5 for the best transcription at 2-3x the CPU cost.
WHISPER_BEAM_SIZE = 1

# Threads for the speech model. 0 lets CTranslate2 choose (usually all cores).
# Set 2 on a small machine to leave the rest of the system responsive.
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

# --- Appearance ----------------------------------------------------------
# "system", "light" or "dark".
#
# `dark-mode.md` warns against an app-specific appearance setting -- "they may
# think your app is broken because it doesn't respond to their systemwide
# appearance choice" -- and that is why this shipped following the OS and
# nothing else. The guidance assumes a platform whose users know where the
# system toggle is. On Windows most do not, and the request came back twice.
#
# "system" is the default, so the guidance still holds for anyone who never
# touches it: the app follows the OS out of the box. The other two are an
# override for somebody who has decided, which is a different thing from an
# app that ignores the OS by default.
APPEARANCE = "system"

# --- Who the notes are for -----------------------------------------------
# Optional, and empty out of the box. Asked once on first run and changeable
# in Settings; both are free for the user to leave blank, and everything works
# unchanged when they do.
#
# These reach the model as a **system message**, not as part of the prompt --
# `/api/generate` takes a `system` field, and a persona belongs in that slot
# rather than buried in the instructions. Nothing is baked into a derived
# model: a change here applies to the very next meeting with nothing to
# rebuild, and there is one source of truth instead of a settings file and a
# model in Ollama's store that can drift apart.
#
# **They change emphasis and wording, never content.** The fidelity rules in
# summarizer.py sit after the persona and stay absolute -- everything said goes
# in whether or not it looks relevant, and nothing that was not said is added.
# That ordering is deliberate: a small model follows the last instruction it
# read, and USER_CONTEXT is text a person typed, which means it may ask for
# something the notes must not do.

#: Their area of work: "Sales", "Customer Success", "Finance", "HR", ...
#: A plain string, not an enum -- "Other" is a real answer and so is anything
#: they type.
USER_FIELD = ""

#: True once the app has asked who the notes are for -- answered or waved
#: away. Its own flag rather than "are the fields empty", because leaving them
#: empty is a real answer and somebody who chose that must not be asked again
#: on every launch.
USER_ASKED = False

#: How they want the notes to read: "Short and blunt", "Full detail",
#: "Decisions and owners only", "Formal", "Casual" -- or anything they type.
#: A separate key from USER_CONTEXT rather than a sentence folded into it, so
#: Settings can show which one is chosen instead of guessing from prose.
USER_TONE = ""

#: Anything else, in their own words. Optional and usually empty: the two
#: fields above cover most of it in two clicks, and asking somebody to write a
#: paragraph during setup is how a setup step gets skipped. Quoted to the model
#: as *theirs*, never merged into the instructions, so it reads as information
#: about a person rather than as a command.
USER_CONTEXT = ""

# --- Summarization -------------------------------------------------------
# Which engine writes the notes. "ollama" is the default and, for now, the only
# built-in: it runs a model on this machine and is a model manager as well as a
# runtime, which is most of why it is worth keeping even once others exist.
#
# The setting exists ahead of a second engine on purpose. "Bring your own model"
# is a claim the product makes, and a claim that rests on one particular process
# being installed is one process away from being untrue. Everything above the
# engine -- the prompts, the language rule, the timestamp handling -- lives in
# summarizer.py and every backend inherits it.
SUMMARY_ENGINE = "ollama"

# Where the weights are, for the engines that run a model in this process
# ("ctranslate2" or "llamacpp"). A .gguf file for llamacpp, a converted model
# folder for ctranslate2. Unused by "ollama", which manages its own models.
#
# Empty until an embedded engine is chosen. Both are written and neither is
# enabled: which one we ship is a measurement (5-build/run_model_bench.py),
# not a preference, and CTranslate2 starts ahead on a technicality worth
# stating -- faster-whisper already runs on it, so it adds no dependency, no
# licence and no platform matrix.
NOTES_MODEL = ""

# "module.path:ClassName" of a notes engine to use instead of the built-in --
# an embedded llama.cpp, an LM Studio or llama-server endpoint, a model you
# converted yourself. It needs two methods, summarize(transcript) and
# title(transcript); see summarizer.py.
#
# This imports and runs code you name. That is no more privileged than editing
# this file was -- same user, same machine -- but it is worth saying plainly,
# and it is the one setting that can send a transcript somewhere we do not
# control, because a custom engine can point anywhere you point it.
CUSTOM_SUMMARIZER = None

# Where Ollama listens, and which model it should use. Only read by the
# "ollama" engine. The URL is a setting rather than a constant because some
# people run Ollama on another machine -- which does mean transcripts leave
# this one, so `--network` names it explicitly rather than assuming loopback.
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"    # pull first:  ollama pull llama3.2

# How much context to ask Ollama for. This used to go unsent, which meant the
# ceiling was whatever the user's Ollama defaulted to -- 16 384 on the version
# measured here, 4 096 on older ones -- and Ollama does not refuse a prompt
# that is too long, it quietly drops the front of it. So the same hour-long
# meeting could produce whole notes on one machine and notes describing only
# its second half on another, with nothing saying which had happened.
#
# Naming it makes the ceiling ours. A meeting that does not fit is summarised
# in parts by `rolling.py` rather than being silently cut.
#
# 16384 and not 8192, which is where this started and which was wrong. The
# number decides when a meeting stops being summarised in one call and starts
# being summarised in parts, and parts are measurably the worse of the two --
# on one meeting run both ways, one call recovered 5 of 17 known facts and
# chunking 2, and only the chunked notes carried lines like "we are about
# halfway through the agenda". Chunking is what happens when the alternative is
# nothing, not a thing to reach for early.
#
#     num_ctx    one call up to
#       8 192    ~3 400 words   ~22 minutes of speech
#      16 384    ~8 200 words   ~54 minutes of speech
#
# At 8192 an ordinary hour-long meeting would take the worse path. At 16384 the
# common case stays on the better one and chunking is reserved for the genuinely
# long meeting, where what it replaces is Ollama quietly dropping the first half.
#
# It costs memory, and it is not a new cost: Ollama 0.33 already loads 16384 by
# default, so this asks for what a current install was giving anyway. Lower it
# on a machine that cannot spare the RAM -- the notes stay correct, more of them
# just go through `rolling.py`.
OLLAMA_NUM_CTX = 16384

# Words per part when a meeting is too long to summarise in one call. Measured
# on an 8 755-word meeting: 300 words cost 527s of model time for no benefit,
# 500 cost 209s, 800 cost 187s. Below about 800 the extra parts cost more than
# they buy, because every part is another line the final merge has to read.
NOTES_CHUNK_WORDS = 800

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


# --- Updates -------------------------------------------------------------
# Nothing here runs on a schedule and nothing here contacts anything on its
# own. `updates.py` holds the whole of it, and the only outbound call in the
# module is reached from a button a human pressed.
#
# How many days may pass before the app reminds you to check. The reminder is
# purely local -- it compares the system clock against a stored date and sends
# nothing at all. Set to 0 to switch it off; that choice is honoured forever.
#
# Seven days rather than monthly on purpose: the published security policy
# commits to a 72-hour fix for anything that could put meeting content off the
# machine, and a monthly nudge cannot deliver a 72-hour promise.
UPDATE_REMINDER_DAYS = 7

# Where "Check for updates" looks, and where the button sends you. Public,
# unauthenticated, and the same page a browser would open.
UPDATE_API_URL = ("https://api.github.com/repos/"
                  "antigravitysoham-eng/vlocalhost-core/releases/latest")
UPDATE_RELEASES_URL = ("https://github.com/"
                       "antigravitysoham-eng/vlocalhost-core/releases/latest")

# --- Sealed Mode ----------------------------------------------------------
# Refuse every outbound connection this app is capable of making.
#
# Nothing here is needed to record, transcribe or write notes: all three
# already happen on this machine. What sealing removes is the small number of
# optional calls that remain -- the update check, a first-run model download,
# and the calendar and email integrations a paid package adds. See
# :mod:`network` for the full list, which is the same list the app prints when
# you ask it.
#
# It exists because "we do not upload your meetings" is a sentence somebody
# else has to believe. Sealed, that claim stops being a statement of intent and
# becomes a property of the install, testable by pulling the network cable and
# carrying on working. An administrator can set it once across a fleet; a
# hospital or a bank can set it and stop asking.
#
# Off by default: most people want the update check, and the speech model has
# to arrive somehow the first time.
SEALED_MODE = False

# --- The record hotkey ---------------------------------------------------
# One key that starts and stops a recording while another window has focus,
# because the first thirty seconds of a meeting is where people say what it is
# about, and that is exactly the time spent finding the window.
#
# Any number of modifiers plus exactly one real key. Modifiers on their own
# cannot be registered -- Windows needs a virtual-key code as well -- and a
# bare key would fire while you were typing, so both are refused with an
# explanation rather than silently ignored.
#
# Not a function key, and that is the whole lesson of the first attempt. F12
# looked ideal -- almost nothing is bound to Ctrl+Shift+F12 -- and it fails for
# a reason no amount of software testing finds: on most laptops the function
# row is media-locked, so pressing F12 without Fn sends volume or brightness
# and never produces an F12 at all. The app listens correctly and nothing
# arrives. A synthetic keypress injects the virtual-key directly and sails
# straight past the Fn layer, so the tests pass while the user's finger does
# nothing.
#
# Space has no Fn layer anywhere, is unmissable by touch, and Ctrl+Shift+Space
# is bound globally by very little. Ctrl+Shift+R was the other candidate and
# would have taken hard-reload from every browser -- a system-wide hotkey
# outranks every application, so whatever this takes, nothing else can have.
HOTKEY = "ctrl+shift+space"

# Turn the hotkey off without clearing the chord you chose.
HOTKEY_ENABLED = True
