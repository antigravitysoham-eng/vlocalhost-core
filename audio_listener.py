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

    def start(self):
        if self._running:
            return
        self._running = True
        self._stream = sd.RawInputStream(
            samplerate=config.SAMPLE_RATE,
            blocksize=self._seg.frame_size,
            dtype="int16",
            channels=config.CHANNELS,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
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
        try:
            import soundcard as sc

            sc.get_microphone(str(sc.default_speaker().name),
                              include_loopback=True)
        except Exception as e:  # noqa: BLE001 - no output device, etc.
            return False, f"No loopback device available ({e})."
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
