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
    """
    global _open_streams
    with _device_lock:
        if _open_streams:
            return False
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:  # noqa: BLE001 - a stale list beats no audio
            print(f"[audio] could not re-scan devices: {e}", flush=True)
            return False
    return True


#: Host APIs to offer, best first, per platform. One physical microphone is
#: exposed once *per API*, so listing them all shows four of everything: this
#: machine reports its mic array under MME, DirectSound, WASAPI and WDM-KS, and
#: the MME copy has its name cut to 31 characters mid-word. Picking one API is
#: what makes the list read like the hardware instead of the driver stack.
#:
#: The order is measured, not assumed. WASAPI and WDM-KS are the modern APIs
#: and the obvious first choice, and both **refuse to open at 16 kHz** — they
#: hand back the device's native 48 kHz or nothing (PaErrorCode -9997 /
#: -9996). Whisper and webrtcvad both need 16 kHz mono, so an entry a user
#: cannot record from is worse than one with an abbreviated name. DirectSound
#: resamples, and reports full names; MME resamples, and truncates. Hence this
#: order. Re-measure before changing it:
#:     python -c "import sounddevice as sd; ..."  (see docs/models.md)
_PREFERRED_HOST_APIS = {
    "Windows": ("Windows DirectSound", "MME"),
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


class _Segmenter:
    """Turns a stream of fixed-size frames into utterances.

    Speech has to fill most of a rolling window before capture starts, and the
    window has to go mostly quiet before it ends — that hysteresis is what stops
    a cough starting a segment or a mid-sentence breath ending one.
    """

    def __init__(self, on_utterance, label):
        self.on_utterance = on_utterance
        self.label = label
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        self.frame_size = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)
        self.bytes_per_frame = self.frame_size * 2  # int16 = 2 bytes/sample

        self._padding = max(1, int(config.SILENCE_TIMEOUT_MS / config.FRAME_MS))
        self._min_frames = int(config.MIN_UTTERANCE_MS / config.FRAME_MS)
        self._ring = collections.deque(maxlen=self._padding)
        self._triggered = False
        self._voiced = []

    def feed(self, frame):
        """Consume one frame of raw 16-bit mono PCM."""
        if len(frame) != self.bytes_per_frame:
            return  # partial block — the VAD only accepts exact frame sizes
        is_speech = self.vad.is_speech(frame, config.SAMPLE_RATE)

        if not self._triggered:
            self._ring.append((frame, is_speech))
            if sum(1 for _, s in self._ring if s) > 0.9 * self._ring.maxlen:
                self._triggered = True
                self._voiced.extend(f for f, _ in self._ring)
                self._ring.clear()
        else:
            self._voiced.append(frame)
            self._ring.append((frame, is_speech))
            if sum(1 for _, s in self._ring if not s) > 0.9 * self._ring.maxlen:
                self.flush()

    def flush(self):
        """Emit whatever has been captured, if it's long enough to be speech."""
        voiced, self._voiced = self._voiced, []
        self._triggered = False
        self._ring.clear()
        if len(voiced) >= self._min_frames:
            self.on_utterance(b"".join(voiced), self.label)


class MicListener:
    """Your microphone, via PortAudio. Works on every platform."""

    kind = "microphone"

    def __init__(self, on_utterance, label=None, device=None):
        """on_utterance(pcm_bytes, label) per detected speech segment."""
        # label="" means single-source capture: nothing to distinguish, so the
        # transcript stays unlabelled.
        self.label = config.LABEL_ME if label is None else label
        self.device = device if device is not None else config.INPUT_DEVICE
        self._seg = _Segmenter(on_utterance, self.label)
        self._q = queue.Queue()
        self._running = False
        self._stream = None
        self._worker = None

    def _callback(self, indata, frames, time_info, status):
        # Runs on a high-priority audio thread — hand off and return fast.
        self._q.put(bytes(indata))

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
            try:
                return sd.RawInputStream(
                    samplerate=config.SAMPLE_RATE,
                    blocksize=self._seg.frame_size,
                    dtype="int16",
                    channels=config.CHANNELS,
                    device=device,
                    callback=self._callback,
                )
            except Exception as e:  # noqa: BLE001 - try the next API's copy
                last = e
                if device is not None:
                    print(f"[audio] device {device} would not open at "
                          f"{config.SAMPLE_RATE} Hz ({e}); trying another",
                          flush=True)
        raise last if last is not None else RuntimeError(
            "No audio input device could be opened.")

    def start(self):
        global _open_streams
        if self._running:
            return
        # Look at the hardware as it is *now*, not as it was when the app
        # opened. A headset plugged in during the meeting is the common case,
        # and before this it was simply invisible.
        rescan_devices()
        candidates = device_candidates(self.device)
        if not candidates:
            # Nothing on this machine matches. Raise the message that names
            # what *is* plugged in, rather than a PortAudio error code.
            resolve_device(self.device)

        self._running = True
        try:
            self._stream = self._open(candidates)
            self._stream.start()
        except Exception:
            # Nothing was opened, so nothing is holding the device list open.
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

    def __init__(self, on_utterance, label=None):
        self.label = config.LABEL_THEM if label is None else label
        self._seg = _Segmenter(on_utterance, self.label)
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
        # Name the device and say what to do. "No loopback device available"
        # on its own sends people hunting for a driver, when the usual causes
        # are a playback device that changed under the app (a headset
        # connecting) or another program holding it exclusively.
        try:
            import soundcard as sc

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
        started = []
        for listener in self.listeners:
            try:
                listener.start()
                started.append(listener)
            except Exception as e:  # noqa: BLE001
                # One source failing (no loopback device, say) must not take the
                # meeting down — record what we can and say what we lost.
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


def build_listener(on_utterance):
    """The capture pipeline described by ``config.CAPTURE_MODE``.

    ``"mic"``  — your microphone only (the original behaviour).
    ``"both"`` — your microphone *and* the meeting audio from your speakers,
                 each labelled, so the transcript shows who spoke.
    """
    mode = (getattr(config, "CAPTURE_MODE", "mic") or "mic").lower()
    if mode not in ("mic", "both", "system"):
        raise ValueError(
            f"CAPTURE_MODE must be 'mic', 'both', or 'system' — got {mode!r}.")
    if mode == "mic":
        return MicListener(on_utterance, label="")
    if mode == "system":
        return LoopbackListener(on_utterance, label="")
    return MultiListener([MicListener(on_utterance),
                          LoopbackListener(on_utterance)])


# Back-compat: earlier code constructed AudioListener directly with a callback
# taking only the PCM bytes.
class AudioListener(MicListener):
    def __init__(self, on_utterance, **kwargs):
        super().__init__(lambda pcm, label: on_utterance(pcm), **kwargs)
