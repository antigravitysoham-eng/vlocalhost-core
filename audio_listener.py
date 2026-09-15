"""Audio capture with voice-activity detection.

Two things can be listened to, and a real meeting needs both:

  * your **microphone** — your side of the call
  * the **system output** (loopback) — everyone else, exactly as your speakers
    play them, with no bot joining the call and no per-platform integration

Each source runs its own VAD state machine, so silence is never transcribed and
an utterance is emitted the moment its speaker stops. Every utterance carries
the label of the source it came from, which is what lets the transcript say who
was talking — see ``docs/speaker-identification.md``.
"""

import collections
import platform
import queue
import threading

try:
    import sounddevice as sd
except OSError as e:  # PortAudio isn't installed (common on fresh macOS/Linux).
    _hint = {
        "Darwin": "Install PortAudio:  brew install portaudio",
        "Linux": "Install PortAudio:  sudo apt install libportaudio2   "
                 "(or your distro's equivalent, e.g. `dnf install portaudio`)",
    }.get(platform.system(), "Install the PortAudio library for your OS.")
    raise RuntimeError(
        f"Could not load the audio backend (PortAudio). {_hint}"
    ) from e

import numpy as np
import webrtcvad

import config

# --- Devices --------------------------------------------------------------
# PortAudio enumerates the sound hardware once, when it initialises, and never
# looks again. That is invisible until somebody connects a headset after the
# app is already open: it is simply not there, and the app records from the
# device that *was* the default at launch, or fails with a message about a
# missing loopback that sends people hunting for a driver they already have.
#
# Re-initialising PortAudio is the only way to make it look again. It is also
# the one thing that must never happen while a stream is open — every stream
# handle belongs to the terminated instance — so it is gated on nobody
# recording.

_open_streams = 0
_device_lock = threading.Lock()


def rescan_devices():
    """Make PortAudio enumerate the hardware again. True if it did.

    Refuses while any stream is open, because terminating PortAudio underneath
    a live recording would take the recording with it.

    **This is not a cheap read.** It tears down and re-creates the whole
    PortAudio library -- every host API on the machine, MME, DirectSound,
    WASAPI and WDM-KS -- which touches the audio stack globally rather than
    only this process.

    Confirmed on a live call: doing this and nothing else, opening no stream at
    all, distorted the user's voice for the people on the other end. It ran on
    every Start, which is exactly why "the moment I start recording" was the
    symptom, why the released build did it too, and why nothing about our own
    stream's format ever explained it. A call in a browser recalibrates its
    echo canceller against these endpoints and does not survive them being
    re-enumerated underneath it; a desktop client with its own audio stack
    shrugs it off. An always-on dictation tool on the same machine never
    disturbed the call because it never does this.

    So call it when the device list is genuinely wrong, not on the way into
    every recording. :func:`ensure_device_known` is the guarded version.
    """
    global _open_streams
    with _device_lock:
        if _open_streams:
            _note_rescan("refused - a stream is open")
            return False
        try:
            sd._terminate()
            sd._initialize()
            _note_rescan("RE-SCANNED - the audio stack was touched")
        except Exception as e:  # noqa: BLE001 - a stale list beats no audio
            print(f"[audio] could not re-scan devices: {e}", flush=True)
            _note_rescan(f"failed: {e.__class__.__name__}")
            return False
    return True


def _note_rescan(outcome):
    """Record that a re-scan was asked for, and by whom.

    This is the one call in the file that reaches past our own process, and the
    one already confirmed to distort a live call on its own. It is supposed to
    be rare now -- only when a device genuinely cannot be found -- and "supposed
    to be rare" is not something to take on trust when the symptom it causes is
    a user's voice breaking up for other people.

    Same shape as ``settings._note_change``: the caller, no values, one line.
    If this appears in the log on every Start, the guard is not holding and the
    device list is being rebuilt on the way into every recording after all.
    """
    try:
        import inspect
        import os

        frame = inspect.currentframe()
        chain = []
        # 0 = here, 1 = rescan_devices, 2+ = whoever wanted it.
        for _ in range(2):
            frame = frame.f_back if frame else None
        for _ in range(3):
            if not frame:
                break
            chain.append("%s:%s" % (os.path.basename(frame.f_code.co_filename),
                                    frame.f_code.co_name))
            frame = frame.f_back

        import diagnostics

        diagnostics.write("audio: device re-scan %s <- %s"
                          % (outcome, " <- ".join(chain) or "unknown"))
    except Exception:                              # noqa: BLE001
        pass                                       # never break capture to log


def ensure_device_known(spec):
    """Device indexes for ``spec``, re-scanning only if it cannot be found.

    The reason a re-scan existed at all is a headset plugged in mid-meeting: it
    is invisible until PortAudio looks again. That case is real, and it is also
    rare -- and the check for it is cheap, while the re-scan is not. So look
    first, and pay the cost only when the answer is "that device is not here".

    The common case -- the microphone that was there when the app opened is
    still there -- now touches nothing, which is what stops a recording from
    disturbing a call already in progress.
    """
    candidates = device_candidates(spec)
    if candidates:
        return candidates
    # The expensive branch. Worth naming the device that could not be found:
    # a saved INPUT_DEVICE index that no longer resolves would send every
    # single Start down here, which would look exactly like the guard not
    # working while in fact it is the setting that is stale.
    _note_rescan(f"needed - no candidate for {spec!r}")
    if rescan_devices():
        return device_candidates(spec)
    return candidates


#: Host APIs to offer, best first, per platform. One physical microphone is
#: exposed once *per API*, so listing them all shows four of everything: this
#: machine reports its mic array under MME, DirectSound, WASAPI and WDM-KS, and
#: the MME copy has its name cut to 31 characters mid-word. Picking one API is
#: what makes the list read like the hardware instead of the driver stack.
#:
#: The order is measured, not assumed. WASAPI and WDM-KS are the modern APIs
#: and the obvious first choice, and both refuse to open at 16 kHz — they hand
#: back the device's native rate or nothing (PaErrorCode -9997 / -9996).
#:
#: That used to put WASAPI last and land every recording on MME. It is why a
#: call in a browser broke up while this app recorded, and why the same call in
#: a desktop client did not: MME and DirectSound reach the device through a
#: compatibility layer that puts a sample-rate conversion on the endpoint, and
#: a browser's capture — WebRTC, on WASAPI at the native rate, with its own
#: echo canceller and a tight clock — does not survive that. A desktop client
#: with its own audio stack shrugs it off. The app was the one behaving badly.
#:
#: :meth:`MicListener._rates_to_try` now opens at whatever the device actually
#: runs at and reduces to 16 kHz in software, so WASAPI opens cleanly and is
#: preferred. Nothing is asked of the endpoint, so nothing else capturing from
#: it is disturbed — whichever program that happens to be.
#:
#: To re-measure: `python vlocalhost.py --devices` lists what each API offers,
#: and sd.check_input_settings(device=i, samplerate=r) says what each accepts.
_PREFERRED_HOST_APIS = {
    "Windows": ("Windows WASAPI", "Windows DirectSound", "MME"),
    "Darwin": ("Core Audio",),
    "Linux": ("PulseAudio", "ALSA"),
}

#: Routing endpoints rather than hardware. They work, but they are named after
#: the driver plumbing and picking one tells a user nothing about which
#: microphone they just chose.
_PSEUDO_DEVICES = ("microsoft sound mapper", "primary sound capture driver",
                   "sysdefault", "default", "pulse")


def input_devices(refresh=False):
    """Microphones this machine can record from.

    ``[{"index", "name", "default"}]``, the default device first.
    """
    if refresh:
        rescan_devices()
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception as e:  # noqa: BLE001 - no audio backend is not a crash
        print(f"[audio] could not list input devices: {e}", flush=True)
        return []

    def usable(api_index):
        return [i for i, d in enumerate(devices)
                if d.get("hostapi") == api_index
                and d.get("max_input_channels", 0) >= 1
                and not (d.get("name") or "").strip().lower().startswith(
                    _PSEUDO_DEVICES)]

    chosen, indices = None, []
    for wanted in _PREFERRED_HOST_APIS.get(platform.system(), ()):
        for api_index, api in enumerate(host_apis):
            if api.get("name") == wanted and usable(api_index):
                chosen, indices = api_index, usable(api_index)
                break
        if chosen is not None:
            break
    if chosen is None:
        # An API this build has never seen. Fall back to whichever one
        # PortAudio itself defaults to, then to everything.
        try:
            fallback = sd.default.hostapi
        except Exception:  # noqa: BLE001
            fallback = None
        if fallback is not None and usable(fallback):
            chosen, indices = fallback, usable(fallback)
        else:
            indices = [i for i, d in enumerate(devices)
                       if d.get("max_input_channels", 0) >= 1]

    # The default *for the chosen API*. sd.default.device points into whichever
    # API PortAudio picked, which is MME on Windows — a different index for the
    # same microphone, under a truncated name.
    default_index = None
    if chosen is not None:
        default_index = host_apis[chosen].get("default_input_device")
    if default_index is None or default_index not in indices:
        default_index = indices[0] if indices else None

    found = [{"index": i, "name": (devices[i].get("name") or "").strip(),
              "default": i == default_index}
             for i in indices]
    found.sort(key=lambda d: (not d["default"], d["name"].lower()))
    return found


def _same_microphone(a, b):
    """Whether two device names denote the same hardware.

    MME truncates names to 31 characters, so the same microphone is
    "Microphone Array (AMD Audio Dev" there and "Microphone Array (AMD Audio
    Device)" under DirectSound. One being a prefix of the other is the
    reliable signal; the length floor stops two devices sharing a generic
    first word from being merged.
    """
    a, b = a.strip().lower(), b.strip().lower()
    if a == b:
        return True
    shortest = min(len(a), len(b))
    return shortest >= 8 and (a.startswith(b) or b.startswith(a))


def device_candidates(spec):
    """Every device index that could be ``spec``, best first.

    A picked microphone exists under several host APIs, and only some of them
    will open at 16 kHz. Returning the alternatives lets a recording start on
    the MME copy when the DirectSound one will not open, instead of failing at
    a device the user can plainly see is plugged in.
    """
    if spec is None or spec == "":
        return [None]

    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        return [spec]

    order = _PREFERRED_HOST_APIS.get(platform.system(), ())

    def rank(index):
        name = host_apis[devices[index].get("hostapi")].get("name", "")
        return order.index(name) if name in order else len(order)

    inputs = [i for i, d in enumerate(devices)
              if d.get("max_input_channels", 0) >= 1]
    if isinstance(spec, int) and not isinstance(spec, bool):
        if spec not in inputs:
            return []
        wanted = (devices[spec].get("name") or "").strip()
        matches = [spec] + [i for i in inputs if i != spec
                            and _same_microphone(devices[i].get("name") or "",
                                                 wanted)]
        return sorted(matches, key=rank)

    needle = str(spec).strip().lower()
    exact = [i for i in inputs
             if _same_microphone(devices[i].get("name") or "", needle)]
    if exact:
        return sorted(exact, key=rank)
    loose = [i for i in inputs
             if needle in (devices[i].get("name") or "").strip().lower()]
    return sorted(loose, key=rank)


def resolve_device(spec, devices=None):
    """Turn ``config.INPUT_DEVICE`` into an index PortAudio will accept.

    ``None`` means the system default. An int is an index and a string is a
    name substring — the setting has always accepted both. Names are resolved
    here rather than handed to PortAudio because a re-scan renumbers the
    devices: the index that was saved last week may now be somebody else's
    webcam, whereas the name still identifies the hardware.

    Raises RuntimeError naming what *is* connected when a configured device
    is not, which is the difference between a user fixing it and filing a bug.
    """
    if spec is None or spec == "":
        return None

    # One matching path, shared with the recording path. Matching on the
    # curated list instead would reject an index saved by an older build
    # (`--set INPUT_DEVICE=9`) even when that exact microphone is present under
    # another host API, which is a working setup broken by an upgrade.
    candidates = device_candidates(spec)
    if candidates:
        return candidates[0]

    found = input_devices() if devices is None else devices
    # Plain ASCII on purpose: this text reaches a Windows console through
    # MultiListener.start, and cp1252 cannot encode an arrow or a curly quote.
    # A UnicodeEncodeError while reporting a missing microphone would replace a
    # fixable problem with a confusing one.
    available = ", ".join(d["name"] for d in found) or "none"
    what = (f"Microphone {spec}" if isinstance(spec, int)
            else f'The microphone "{spec}"')
    raise RuntimeError(
        f"{what} is not connected. Reconnect it, or pick another in "
        f"Settings, under 'What to listen to'. Available: {available}")


# --- Voice activity detection --------------------------------------------
# Two detectors behind one interface. The difference between them is not a
# tuning preference; it decides whether the app works in a room with a fan in
# it. See docs/performance.md for the measurements.


class _WebRtcDetector:
    """The original: WebRTC's VAD, an energy and spectral-shape heuristic.

    Cheap, dependency-light, and honest about clean speech. It has no model of
    what speech *is*, so anything with sustained energy reads as talking:
    measured against the bench fixtures it labels **100% of music** and 82% of
    café noise as speech. Kept as the fallback, because a machine where the
    Silero model will not load still has to record.
    """

    name = "webrtc"

    def __init__(self):
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        # webrtcvad accepts 10, 20 or 30 ms and nothing else.
        self.frame_ms = config.FRAME_MS
        self.frame_size = int(config.SAMPLE_RATE * self.frame_ms / 1000)

    def is_speech(self, frame):
        return self.vad.is_speech(frame, config.SAMPLE_RATE)

    def reset(self):
        pass


class _SileroDetector:
    """Silero v6, the neural VAD that faster-whisper already ships.

    No new dependency: the ONNX model sits in ``faster_whisper/assets`` and
    ``onnxruntime`` is already in the bundle. On the same fixtures it keeps
    99–100% of non-speech quiet, including music, where webrtcvad keeps none.

    **It needs context.** ``SileroVADModel.__call__`` zeroes its recurrent
    state on every call, so feeding one 32 ms frame at a time asks the model
    to judge each frame in isolation — which drops recall from 86% to 67% and
    is the opposite of what a recurrent detector is for. Feeding a rolling
    window of the last :data:`CONTEXT_FRAMES` frames and taking the newest
    probability restores it to 99.3% agreement with scoring the whole file at
    once. Eight frames is where that curve flattens; sixteen and thirty-two
    measured no better and cost proportionally more.
    """

    name = "silero"

    #: Silero v6 is trained on 512-sample windows at 16 kHz. Not adjustable.
    WINDOW = 512
    #: How much history each decision sees. 8 x 32 ms = 256 ms, ~0.55 ms of CPU
    #: per frame, which is under 2% of the frame's own duration.
    CONTEXT_FRAMES = 8

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        if config.SAMPLE_RATE != 16000:
            raise RuntimeError(
                f"Silero VAD is a 16 kHz model and SAMPLE_RATE is "
                f"{config.SAMPLE_RATE}.")
        self._model = get_vad_model()
        self.frame_size = self.WINDOW
        self.frame_ms = 1000 * self.WINDOW / config.SAMPLE_RATE  # 32.0
        self.threshold = float(getattr(config, "VAD_THRESHOLD", 0.5))
        self._context = collections.deque(maxlen=self.CONTEXT_FRAMES)

    def is_speech(self, frame):
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        self._context.append(samples)
        window = np.concatenate(self._context)
        # The newest probability: the earlier frames in the window are there to
        # give this one its history, not to be re-judged.
        return float(np.asarray(self._model(window)).ravel()[-1]) >= self.threshold

    def reset(self):
        self._context.clear()


def build_detector():
    """The detector named by ``config.VAD_ENGINE``, or the one that works.

    Falls back to webrtcvad rather than refusing to record: a missing model
    file or a faster-whisper that moved its internals should cost accuracy,
    never the meeting.
    """
    wanted = str(getattr(config, "VAD_ENGINE", "silero") or "silero").lower()
    if wanted not in ("silero", "webrtc"):
        print(f"[audio] unknown VAD_ENGINE {wanted!r}; using silero", flush=True)
        wanted = "silero"
    if wanted == "silero":
        try:
            return _SileroDetector()
        except Exception as e:  # noqa: BLE001
            print(f"[audio] Silero VAD unavailable ({e}); falling back to "
                  f"webrtcvad, which does not reject music or steady noise.",
                  flush=True)
    return _WebRtcDetector()


class _Segmenter:
    """Turns a stream of fixed-size frames into utterances.

    Speech has to fill a short window before capture starts, and a longer
    window has to go quiet before it ends. That hysteresis is what stops a
    cough starting a segment or a mid-sentence breath ending one.

    **Starting and stopping use different windows, and that matters.** They
    used to share one, sized for the silence timeout: an utterance began only
    once 90% of the last 800 ms read as speech. webrtcvad flags speech
    generously enough for that to fire almost immediately, so it was invisible.
    Silero is more selective — the property that makes it ignore music — and
    against the same rule it took long enough to trigger that the rolling
    window had already discarded the opening words. Whole phrases went missing
    from the transcript ("Last thing," "I'll update the"), which read as a
    transcription error and was really a buffering one.

    So onset is judged over its own short window, and every frame is kept in a
    lead-in buffer regardless, so whenever the trigger fires the audio before
    it is still there to prepend.

    The frame size comes from the detector, not from a constant: webrtcvad
    takes 10/20/30 ms frames and Silero takes 512 samples (32 ms), and
    everything downstream — the capture blocksize, the windows, the minimum
    utterance — is derived from whichever is in use.
    """

    def __init__(self, on_utterance, label, on_partial=None, on_level=None):
        self.on_utterance = on_utterance
        #: Called with the audio captured *so far*, while somebody is still
        #: talking, so a UI can show provisional text instead of nothing. Left
        #: None by the terminal and MCP front ends, which have nowhere to put
        #: it — and then no partial is ever built or decoded, so they pay
        #: nothing for a feature they cannot use.
        self.on_partial = on_partial
        #: ``on_level(dbfs, speech, label)`` — how loud the room is, and whether
        #: the detector called this frame speech. Same opt-in shape as
        #: ``on_partial``: a front end with no meter passes nothing, and then
        #: none of the arithmetic below ever runs.
        #:
        #: It reports the *peak* since the last call rather than the level at
        #: one instant. A meter sampled on a timer misses the syllable that
        #: falls between two samples and so reads quiet during exactly the
        #: speech it exists to show. Throttling belongs here for the same
        #: reason: this is the only place that knows the frame rate, and a
        #: callback crossing a JS bridge fifty times a second is a cost the
        #: audio thread should not be paying.
        self.on_level = on_level
        self._level_peak = 0.0
        self._level_speech = False
        self._level_count = 0
        self.label = label
        self.detector = build_detector()
        self.frame_size = self.detector.frame_size
        self.bytes_per_frame = self.frame_size * 2  # int16 = 2 bytes/sample

        frame_ms = self.detector.frame_ms
        self._padding = max(1, int(config.SILENCE_TIMEOUT_MS / frame_ms))
        self._min_frames = int(config.MIN_UTTERANCE_MS / frame_ms)
        # ~12 level reports a second. Fast enough that the meter moves with the
        # voice, slow enough that the bridge is not the bottleneck. Whole
        # frames, so it stays locked to the audio rather than to a clock.
        self._level_every = max(1, int(80 / frame_ms))
        onset_ms = getattr(config, "VAD_ONSET_MS", 160)
        self._onset_frames = max(1, int(onset_ms / frame_ms))
        self._onset_ratio = float(getattr(config, "VAD_ONSET_RATIO", 0.6))

        # Raw frames kept while idle, so the run-up to a trigger is never lost.
        # Only as much as the onset can plausibly lag by: this audio is
        # prepended to every utterance and then transcribed, so a buffer sized
        # for the silence timeout would staple most of a second of silence to
        # the front of every line and pay to decode it.
        lead_frames = max(self._onset_frames,
                          int(getattr(config, "VAD_LEAD_MS", 300) / frame_ms))
        self._lead = collections.deque(maxlen=lead_frames)
        self._partial_every = max(
            1, int(getattr(config, "PARTIAL_INTERVAL_MS", 500) / frame_ms))
        self._since_partial = 0
        self._onset = collections.deque(maxlen=self._onset_frames)
        self._release = collections.deque(maxlen=self._padding)
        self._triggered = False
        self._voiced = []

    def _level(self, frame, is_speech):
        """Accumulate loudness, and report the peak every few frames.

        Only reached when a front end asked for it. Everything here is cheap —
        one RMS over 512-odd samples — but it runs on the capture thread, so it
        is kept to arithmetic and never touches the consumer more than about
        twelve times a second.

        Errors are swallowed on purpose. A meter is decoration; a listener that
        raises into the capture thread would take the recording down with it,
        and losing the meeting to save the meter is the wrong way round.
        """
        samples = np.frombuffer(frame, dtype=np.int16)
        if samples.size:
            rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
            self._level_peak = max(self._level_peak, rms)
        self._level_speech = self._level_speech or is_speech
        self._level_count += 1
        if self._level_count < self._level_every:
            return

        peak, speech = self._level_peak, self._level_speech
        self._level_peak, self._level_speech, self._level_count = 0.0, False, 0
        # dBFS against a full-scale int16. Floored at -60, which is the bottom
        # of the meter: below that the number is dithering noise and a scale
        # that keeps going just makes the quiet end twitch.
        dbfs = -60.0 if peak <= 0 else max(-60.0, 20.0 * np.log10(peak / 32768.0))
        try:
            self.on_level(float(dbfs), bool(speech), self.label)
        except Exception:                                      # noqa: BLE001
            pass

    def feed(self, frame):
        """Consume one frame of raw 16-bit mono PCM."""
        if len(frame) != self.bytes_per_frame:
            return  # partial block — the VAD only accepts exact frame sizes
        is_speech = self.detector.is_speech(frame)
        if self.on_level is not None:
            self._level(frame, is_speech)

        if not self._triggered:
            self._lead.append(frame)
            self._onset.append(is_speech)
            if (len(self._onset) == self._onset.maxlen
                    and sum(self._onset) >= self._onset_ratio * self._onset.maxlen):
                self._triggered = True
                # Everything still buffered, oldest first: the speaker's first
                # syllable is in here, several frames before the detector was
                # willing to call it speech.
                self._voiced.extend(self._lead)
                self._lead.clear()
                self._onset.clear()
                self._release.clear()
                self._since_partial = 0
        else:
            self._voiced.append(frame)
            self._release.append(is_speech)
            # Provisional text while the speaker is still going. Emitted on a
            # frame count rather than a clock so it stays tied to the audio,
            # and only when somebody is listening for it.
            self._since_partial += 1
            if self.on_partial and self._since_partial >= self._partial_every:
                self._since_partial = 0
                self.on_partial(b"".join(self._voiced), self.label)
            # 90% quiet, not perfectly quiet. Demanding every frame in the
            # window be silent means one stray flag restarts the whole
            # timeout, which costs real latency at the end of every sentence
            # and buys nothing Silero's false-positive rate needs.
            if (len(self._release) == self._release.maxlen
                    and sum(self._release) <= 0.1 * self._release.maxlen):
                self.flush()

    def flush(self):
        """Emit whatever has been captured, if it's long enough to be speech."""
        voiced, self._voiced = self._voiced, []
        self._triggered = False
        self._lead.clear()
        self._onset.clear()
        self._release.clear()
        self._since_partial = 0
        # The next utterance is a new one; its first frames should not be
        # judged against the tail of the last.
        self.detector.reset()
        if len(voiced) >= self._min_frames:
            self.on_utterance(b"".join(voiced), self.label)


def _to_mono_16k(pcm, channels, factor):
    """Device audio -> the mono 16 kHz the rest of the pipeline expects.

    Both conversions happen here rather than being asked of the endpoint.
    Windows will happily mix channels and resample on the device for you, but
    on a shared microphone that converter is imposed on every program capturing
    from it, and a browser's call audio does not survive it.

    Channels are averaged, not picked: this is a microphone *array*, and one
    element on its own is quieter and off-axis.

    Rate reduction averages each group of ``factor`` samples rather than taking
    every nth. Dropping samples folds everything above the new Nyquist back
    into the band as aliasing, which a speech model hears as consonants nobody
    said. Only exact integer ratios are used (48000 -> 16000 is 3), so there is
    no resampling error to accumulate.
    """
    samples = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        usable = (samples.size // channels) * channels
        if usable == 0:
            return b""
        samples = samples[:usable].reshape(-1, channels).astype(np.int32).mean(axis=1)
    else:
        samples = samples.astype(np.int32)
    if factor > 1:
        usable = (samples.size // factor) * factor
        if usable == 0:
            return b""
        samples = samples[:usable].reshape(-1, factor).mean(axis=1)
    return samples.astype(np.int16).tobytes()


def _use_soundcard_mic():
    """True when the microphone should be recorded through soundcard."""
    choice = (getattr(config, "MIC_BACKEND", "auto") or "auto").lower()
    if choice == "soundcard":
        return True
    if choice == "portaudio":
        return False
    if platform.system() != "Windows":
        return False
    try:
        import soundcard  # noqa: F401
    except Exception:                              # noqa: BLE001
        return False
    return True


class SoundcardMicListener:
    """The microphone via WASAPI, the same way system audio is captured.

    Deliberately the same shape as :class:`LoopbackListener`, because that path
    has recorded every meeting this app has ever saved without disturbing
    anything else on the machine. The only difference is which endpoint it
    opens.

    Why this exists at all: see config.MIC_BACKEND. PortAudio's input stream,
    on its own, breaks a browser call's outgoing audio on at least one common
    laptop audio stack.
    """

    kind = "microphone"

    def __init__(self, on_utterance, label=None, device=None, on_partial=None,
                 on_level=None):
        self.label = config.LABEL_ME if label is None else label
        self.device = device if device is not None else config.INPUT_DEVICE
        self._seg = _Segmenter(on_utterance, self.label, on_partial=on_partial,
                                on_level=on_level)
        self._running = False
        self._worker = None

    def _pick(self, sc):
        """The configured microphone, or the default one.

        Matched by name because soundcard has no notion of PortAudio's indexes,
        and the setting the user chose is a name.
        """
        want = (self.device or "").strip()
        if want:
            for m in sc.all_microphones(include_loopback=False):
                if m.name == want or want in m.name or m.name in want:
                    return m
        return sc.default_microphone()

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False
        if self._worker is not None:
            self._worker.join(timeout=3)
            self._worker = None

    def _loop(self):
        import soundcard as sc

        initialized = LoopbackListener._init_com()
        frame = self._seg.frame_size
        # Bigger gulps than one VAD frame: asking WASAPI for 32 ms at a time
        # cannot keep up and it reports dropped audio. Same reason as loopback.
        chunk = frame * 8
        try:
            mic = self._pick(sc)
            with mic.recorder(samplerate=config.SAMPLE_RATE, channels=1,
                              blocksize=chunk) as rec:
                print(f"[audio] microphone via soundcard/WASAPI: {mic.name}",
                      flush=True)
                while self._running:
                    block = rec.record(numframes=chunk)
                    mono = block[:, 0] if block.ndim > 1 else block
                    pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                    for start in range(0, len(pcm) - frame + 1, frame):
                        self._seg.feed(pcm[start:start + frame].tobytes())
        except Exception as e:  # noqa: BLE001
            print(f"[audio] microphone stopped: {e}", flush=True)
        finally:
            self._seg.flush()
            if initialized:
                import ctypes

                ctypes.windll.ole32.CoUninitialize()


class MicListener:
    """Your microphone, via PortAudio. Works on every platform."""

    kind = "microphone"

    def __init__(self, on_utterance, label=None, device=None, on_partial=None,
                 on_level=None):
        """on_utterance(pcm_bytes, label) per detected speech segment."""
        # label="" means single-source capture: nothing to distinguish, so the
        # transcript stays unlabelled.
        self.label = config.LABEL_ME if label is None else label
        self.device = device if device is not None else config.INPUT_DEVICE
        self._seg = _Segmenter(on_utterance, self.label, on_partial=on_partial,
                                on_level=on_level)
        self._q = queue.Queue()
        self._running = False
        self._stream = None
        self._worker = None
        # What the device is actually opened as, when that differs from the
        # mono 16 kHz the pipeline wants. Both set by _open().
        self._decimate = 1
        self._channels = config.CHANNELS

    def _callback(self, indata, frames, time_info, status):
        # Runs on a high-priority audio thread — hand off and return fast.
        if self._channels > 1 or self._decimate > 1:
            self._q.put(_to_mono_16k(bytes(indata), self._channels,
                                     self._decimate))
            return
        self._q.put(bytes(indata))

    @staticmethod
    def _formats_to_try(device):
        """(rate, channels, decimate) triples, best first.

        The device's own format comes first, exactly as it runs: its rate and
        its channel count. Every difference between what is asked for and what
        the device is becomes a converter Windows puts on the *endpoint*, and
        an endpoint converter is imposed on every program capturing from that
        microphone -- which is why a Teams call in Chrome broke up while this
        app recorded, and why Wispr Flow, which holds the same microphone all
        day, does not disturb it.

        The old behaviour asked for 16 kHz mono on a 48 kHz stereo array: two
        conversions, and 16 kHz is refused by WASAPI and WDM-KS outright, so it
        also pushed the whole thing onto MME's compatibility path.

        Mono 16 kHz is still tried last, because a device that offers only that
        must still work.
        """
        target = config.SAMPLE_RATE
        want_ch = config.CHANNELS
        options = []
        native_rate = native_ch = 0
        try:
            info = sd.query_devices(device) if device is not None else \
                sd.query_devices(kind="input")
            native_rate = int(info.get("default_samplerate") or 0)
            native_ch = int(info.get("max_input_channels") or 0)
        except Exception:                          # noqa: BLE001
            pass

        def add(rate, channels):
            if rate and channels and rate % target == 0:
                triple = (rate, channels, rate // target)
                if triple not in options:
                    options.append(triple)

        add(native_rate, native_ch)                # exactly what the device is
        add(native_rate, want_ch)                  # right rate, we mix down
        add(48000, native_ch)
        add(48000, want_ch)
        options.append((target, want_ch, 1))       # last resort
        return options

    def _open(self, candidates):
        """Open the first candidate that will actually take our format.

        The same microphone appears under several host APIs and they do not
        agree on what they will accept: WASAPI and WDM-KS refuse 16 kHz
        outright. Trying the alternatives turns "Invalid sample rate" on a
        device the user can see plugged in into a recording that simply
        starts.
        """
        last = None
        for device in candidates:
            for rate, channels, decimate in self._formats_to_try(device):
                try:
                    stream = sd.RawInputStream(
                        samplerate=rate,
                        blocksize=self._seg.frame_size * decimate,
                        dtype="int16",
                        channels=channels,
                        device=device,
                        callback=self._callback,
                    )
                except Exception as e:  # noqa: BLE001 - try the next option
                    last = e
                    continue
                self._decimate = decimate
                self._channels = channels
                if decimate > 1 or channels != config.CHANNELS:
                    print(f"[audio] capturing at {rate} Hz / {channels} ch — "
                          f"the device's own format — and converting to "
                          f"{config.SAMPLE_RATE} Hz mono in software",
                          flush=True)
                return stream
            if device is not None:
                print(f"[audio] device {device} would not open ({last}); "
                      f"trying another", flush=True)
        raise last if last is not None else RuntimeError(
            "No audio input device could be opened.")

    def start(self):
        global _open_streams
        if self._running:
            return
        # A headset plugged in during the meeting is invisible until PortAudio
        # looks again -- but looking again resets the machine's audio stack and
        # disturbs anything already capturing, a call in a browser above all.
        # See rescan_devices. So try what we already know about first.
        candidates = ensure_device_known(self.device)
        if not candidates:
            # Nothing on this machine matches. Raise the message that names
            # what *is* plugged in, rather than a PortAudio error code.
            resolve_device(self.device)

        self._running = True
        try:
            self._stream = self._open(candidates)
            self._stream.start()
        except Exception:
            # The cached enumeration can be stale -- indexes shift when devices
            # come and go, and the unconditional re-scan this replaced was also
            # what kept them fresh. So the re-scan still happens, just here,
            # where the alternative is not recording at all rather than a call
            # that sounds slightly worse.
            self._stream = None
            retry = []
            if rescan_devices():
                retry = device_candidates(self.device)
            if not retry:
                self._running = False
                raise
            try:
                self._stream = self._open(retry)
                self._stream.start()
            except Exception:
                self._running = False
                self._stream = None
                raise
        with _device_lock:
            _open_streams += 1
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self):
        global _open_streams
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            with _device_lock:
                _open_streams = max(0, _open_streams - 1)
        if self._worker is not None:
            self._worker.join(timeout=2)
            self._worker = None

    def _loop(self):
        while self._running:
            try:
                self._seg.feed(self._q.get(timeout=0.1))
            except queue.Empty:
                continue
        self._seg.flush()  # don't lose an utterance in progress


class LoopbackListener:
    """Everyone else on the call — captured from what your speakers play.

    Uses the ``soundcard`` package: WASAPI loopback on Windows and a PulseAudio
    monitor source on Linux, both without any virtual cable. macOS has no
    OS-level loopback, so there you install a virtual device (BlackHole) and
    point ``config.INPUT_DEVICE`` at it instead.
    """

    kind = "system audio"

    def __init__(self, on_utterance, label=None, on_partial=None, on_level=None):
        self.label = config.LABEL_THEM if label is None else label
        self._seg = _Segmenter(on_utterance, self.label, on_partial=on_partial,
                                on_level=on_level)
        self._running = False
        self._worker = None

    @staticmethod
    def available():
        """(ok, reason) — whether loopback capture can run on this machine."""
        try:
            import soundcard  # noqa: F401
        except ImportError:
            return False, ("The 'soundcard' package isn't installed. "
                           "Run:  pip install soundcard")
        if platform.system() == "Darwin":
            return False, ("macOS has no built-in loopback. Install a virtual "
                           "device such as BlackHole, route the call's audio "
                           "into it, and set INPUT_DEVICE in config.py.")
        # WASAPI is COM, and COM is per-thread. soundcard initialises it at
        # module import, on whichever thread imports it first -- so this passed
        # in every isolated test, where that thread happened to be this one,
        # and failed in the running app, where Settings had already imported
        # soundcard on another thread and the import here is a no-op. Then the
        # first WASAPI call returns CO_E_NOTINITIALIZED and this function
        # blames the hardware for it.
        initialized = LoopbackListener._init_com()
        try:
            return LoopbackListener._probe()
        finally:
            if initialized:
                import ctypes

                ctypes.windll.ole32.CoUninitialize()

    @staticmethod
    def _probe():
        """The endpoint checks themselves. COM must already be up.

        Split out so the initialise/uninitialise pair around it stays balanced
        no matter which branch returns.
        """
        import soundcard as sc

        # Name the device and say what to do. "No loopback device available"
        # on its own sends people hunting for a driver, when the usual causes
        # are a playback device that changed under the app (a headset
        # connecting) or another program holding it exclusively.
        try:
            speaker = sc.default_speaker()
        except Exception as e:  # noqa: BLE001
            return False, ("Windows reports no playback device, so there is "
                           f"nothing to capture ({e}). Plug in or enable a "
                           "speaker or headset, then press Start again. Your "
                           "microphone still records on its own — choose "
                           "“My microphone only”.")
        try:
            sc.get_microphone(str(speaker.name), include_loopback=True)
        except Exception as e:  # noqa: BLE001
            return False, (f"Could not tap “{speaker.name}” for meeting audio "
                           f"({e}). This usually means the playback device "
                           "changed while the app was open, or another program "
                           "has it exclusively. Reconnect it and press Start "
                           "again, or record your microphone only.")
        return True, "ready"

    def start(self):
        if self._running:
            return
        ok, reason = self.available()
        if not ok:
            raise RuntimeError(reason)
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False
        if self._worker is not None:
            self._worker.join(timeout=3)
            self._worker = None

    @staticmethod
    def _init_com():
        """WASAPI is COM, and COM is per-thread. Without this the recorder
        raises CO_E_NOTINITIALIZED (0x800401f0) on any thread but the first."""
        if platform.system() != "Windows":
            return False
        import ctypes

        COINIT_MULTITHREADED = 0x0
        # S_OK (0) or S_FALSE (1) both mean COM is usable on this thread.
        result = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        return result in (0, 1)

    def _loop(self):
        import soundcard as sc

        initialized = self._init_com()
        frame = self._seg.frame_size
        # Read in bigger gulps than the 30 ms VAD frame — asking WASAPI for one
        # tiny frame at a time can't keep up and it reports dropped audio.
        chunk = frame * 8
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(str(speaker.name), include_loopback=True)
            with mic.recorder(samplerate=config.SAMPLE_RATE, channels=1,
                              blocksize=chunk) as rec:
                while self._running:
                    block = rec.record(numframes=chunk)
                    # soundcard hands back float32 [-1, 1]; the VAD and Whisper
                    # both want 16-bit PCM.
                    mono = block[:, 0] if block.ndim > 1 else block
                    pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                    # Hand the segmenter exactly one VAD frame at a time.
                    for start in range(0, len(pcm) - frame + 1, frame):
                        self._seg.feed(pcm[start:start + frame].tobytes())
        except Exception as e:  # noqa: BLE001 - keep the mic alive if this dies
            print(f"[loopback] stopped: {e}", flush=True)
        finally:
            self._seg.flush()
            if initialized:
                import ctypes

                ctypes.windll.ole32.CoUninitialize()


class MultiListener:
    """Runs several sources at once and merges their utterances."""

    def __init__(self, listeners):
        self.listeners = listeners

    def start(self):
        """Open each source in turn.

        Deliberately sequential. Opening the microphone and the WASAPI loopback
        on parallel threads saves about 180 ms, and costs system audio
        entirely: COM is per-thread, `soundcard` initialises it itself in the
        thread it runs on, and a second CoInitializeEx from a worker thread
        conflicts with it -- measured as 0x800401f0 and then 0x100000001, with
        the loopback dropping out of the recording and only the microphone
        surviving. Losing the other half of a meeting is not worth 180 ms.

        The latency that mattered was never here; it was the speech model
        loading before the microphone opened. See NoteTaker.start.
        """
        started = []
        for listener in self.listeners:
            try:
                listener.start()
                started.append(listener)
            except Exception as e:  # noqa: BLE001
                # One source failing (no loopback device, say) must not take the
                # meeting down -- record what we can and say what we lost.
                print(f"[audio] {listener.kind} unavailable: {e}", flush=True)
        if not started:
            raise RuntimeError("No audio source could be opened.")
        self.listeners = started

    def stop(self):
        for listener in self.listeners:
            listener.stop()

    @property
    def sources(self):
        return [listener.kind for listener in self.listeners]


def build_listener(on_utterance, on_partial=None, on_level=None):
    """The capture pipeline described by ``config.CAPTURE_MODE``.

    ``"mic"``  — your microphone only (the original behaviour).
    ``"both"`` — your microphone *and* the meeting audio from your speakers,
                 each labelled, so the transcript shows who spoke.

    ``on_partial`` is optional and opt-in: pass it and each source will also
    report the audio captured so far while somebody is still speaking.
    """
    mode = (getattr(config, "CAPTURE_MODE", "mic") or "mic").lower()
    if mode not in ("mic", "both", "system"):
        raise ValueError(
            f"CAPTURE_MODE must be 'mic', 'both', or 'system' — got {mode!r}.")
    # Which library records the microphone. See config.MIC_BACKEND: a bare
    # PortAudio input stream disturbs a browser call's outgoing audio on some
    # laptop audio stacks, and soundcard -- already used for system audio on
    # every recording -- does not go through PortAudio at all.
    mic_class = SoundcardMicListener if _use_soundcard_mic() else MicListener

    # Which path a recording actually took, written down at the moment it is
    # chosen. "Is it even using the backend I set" has been guesswork twice in
    # this investigation, and guessing about it is how a theory gets tested
    # against the wrong code.
    try:
        import diagnostics

        diagnostics.write("audio: start mode=%s mic=%s backend=%s"
                          % (mode, mic_class.__name__,
                             getattr(config, "MIC_BACKEND", "auto")))
    except Exception:                              # noqa: BLE001
        pass

    if mode == "mic":
        return mic_class(on_utterance, label="", on_partial=on_partial,
                         on_level=on_level)
    if mode == "system":
        return LoopbackListener(on_utterance, label="", on_partial=on_partial,
                                on_level=on_level)
    return MultiListener([mic_class(on_utterance, on_partial=on_partial,
                                    on_level=on_level),
                          LoopbackListener(on_utterance, on_partial=on_partial,
                                           on_level=on_level)])


# Back-compat: earlier code constructed AudioListener directly with a callback
# taking only the PCM bytes.
class AudioListener(MicListener):
    def __init__(self, on_utterance, **kwargs):
        super().__init__(lambda pcm, label: on_utterance(pcm), **kwargs)
