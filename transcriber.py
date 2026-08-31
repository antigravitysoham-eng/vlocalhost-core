"""Local speech-to-text — bring your own voice model.

By default this runs faster-whisper locally, but the engine is pluggable: point
``config.WHISPER_MODEL`` at any model name or a local folder, or attach a fully
custom engine via ``config.CUSTOM_TRANSCRIBER``. Use :func:`build_transcriber`
to get the configured engine; the rest of the app never hardcodes a backend.

Any engine only needs two methods:
    load(self)                    -> warm up (optional)
    transcribe(self, pcm_bytes)   -> str
"""

import importlib

import numpy as np

import config
import languages
import network

# Common single-word hallucinations Whisper emits on near-silent/noise audio.
# Whisper does this in other languages too — these are the frequent ones.
_HALLUCINATIONS = {
    "you", "thank you.", "thanks for watching!", "bye.", ".",
    "ご視聴ありがとうございました", "字幕by索兰娅", "請不吝點贊 訂閱",
    "धन्यवाद", "gracias.", "merci.",
}


def build_transcriber():
    """Return the configured speech-to-text engine.

    Uses ``config.CUSTOM_TRANSCRIBER`` ("module:attr") when set — letting users
    attach their own model — otherwise the built-in faster-whisper engine.
    """
    spec = getattr(config, "CUSTOM_TRANSCRIBER", None)
    if spec:
        engine = _load_custom(spec)
        if not hasattr(engine, "transcribe"):
            raise TypeError(
                f"CUSTOM_TRANSCRIBER {spec!r} must provide a .transcribe(pcm_bytes) "
                "method returning text."
            )
        return engine
    return FasterWhisperTranscriber()


def _load_custom(spec):
    """Resolve "module.path:attr" to an instance (calls the attr if callable)."""
    if ":" not in spec:
        raise ValueError(
            f'CUSTOM_TRANSCRIBER must look like "module.path:ClassName", got {spec!r}.'
        )
    module_name, attr = spec.split(":", 1)
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if callable(obj) else obj


def _build_model(name):
    """Construct a faster-whisper model, honouring Sealed Mode.

    ``WhisperModel`` reaches huggingface.co for anything it cannot find on
    disk. That is the right default, and it is also the one download a sealed
    install must not perform.

    So the local copy is always tried first, with ``local_files_only``. The
    ordinary case — a model already downloaded — is then provably offline, and
    the timestamp the network report shows is a real one: it moves when a file
    is actually fetched and never when a cached model is simply loaded. A
    "last used: never" that quietly meant "loaded from cache this morning"
    would be worse than showing nothing at all.

    Only if the model is genuinely absent does this fall through to a fetch,
    and on a sealed install it does not fall through: the library's own message
    talks about repositories and offline flags, while the person reading it set
    a switch called Sealed Mode, so it is replaced with one that names the
    cause and both ways out.
    """
    from faster_whisper import WhisperModel

    def build(local_only):
        return WhisperModel(
            name,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE,
            cpu_threads=getattr(config, "WHISPER_CPU_THREADS", 0),
            local_files_only=local_only,
        )

    try:
        return build(True)
    except Exception as exc:
        if not network.allowed("model_download"):
            raise RuntimeError(
                f"The speech model {name!r} is not on this machine, and this "
                f"install is sealed so it cannot be downloaded. Either point "
                f"WHISPER_MODEL at a model folder you already have, or turn "
                f"off Sealed Mode long enough to fetch it once."
            ) from exc

    model = build(False)
    network.record("model_download")
    return model


class FasterWhisperTranscriber:
    """The built-in engine. Loads a faster-whisper model (by name or local path)
    once and reuses it for every utterance.

    The model is multilingual; ``WHISPER_LANGUAGE`` decides what it listens
    for. It ships pinned to ``"en"``, and any of the 100 supported codes can be
    chosen instead. Set it to ``None`` and the language is detected **per
    utterance**, so a bilingual meeting transcribes correctly as it switches
    between speakers; the detected code is left on :attr:`last_language` for
    the caller to label the line with.
    """

    def __init__(self):
        self._model = None
        self._partial_model = None
        self.last_language = None       # ISO code detected for the last utterance
        self.last_language_prob = 0.0   # how sure Whisper was (0-1)

    def precheck(self):
        """Validate the model/language pairing. Cheap: a string comparison.

        Split out of :meth:`load` so the recorder can refuse an impossible
        combination *before* it opens the microphone, while still building the
        model in the background. Loading is what costs seconds; this costs
        nothing, and it is the failure a user can actually do something about.
        """
        warning = languages.check(config.WHISPER_MODEL, config.WHISPER_LANGUAGE)
        if warning:
            raise RuntimeError(warning)

    def load(self):
        """Load the model. Expensive, and deliberately not on the start path.

        Measured on a mid-range Windows laptop, every single time the recorder
        starts, because RELEASE_MODEL_WHEN_IDLE hands the memory back between
        sessions: 0.5 s for tiny, 0.8 s for base, 2.1 s for small. That used to
        run before the microphone opened, which is the whole reason pressing
        Record felt slow. NoteTaker.start now warms it on a background thread
        while audio is already being captured.
        """
        if self._model is None:
            self.precheck()
            # WHISPER_MODEL may be a name, a HF repo id, or a local model folder.
            self._model = _build_model(config.WHISPER_MODEL)
        return self._model

    def load_partial(self):
        """The model used for provisional text, or the main one.

        Decoding cost with Whisper is close to **constant** regardless of how
        much audio you hand it — every input is padded to a 30-second mel
        window, so 0.5 s measured 1834 ms against 8 s at 1859 ms on this
        machine. Provisional text therefore cannot be made cheap by keeping it
        short; the only lever is a smaller model.

        ``config.PARTIAL_MODEL = "tiny"`` is that lever. It buys a smoother
        line rather than an earlier one — first words land at about the same
        moment, then refresh roughly twice as often — for 66 MB resident and
        one extra first-run download. Left as None it reuses whatever the main
        model is, which costs nothing and needs nothing downloaded.
        """
        wanted = getattr(config, "PARTIAL_MODEL", None)
        if not wanted or wanted == config.WHISPER_MODEL:
            return self.load()
        if self._partial_model is None:
            self._partial_model = _build_model(wanted)
        return self._partial_model

    def unload(self):
        """Drop the model and give the memory back — a few hundred MB.

        Safe to call any time; the next transcribe() reloads it.
        """
        if self._model is None and self._partial_model is None:
            return False
        self._model = None
        self._partial_model = None
        import gc

        gc.collect()
        return True

    def transcribe(self, pcm_bytes, partial=False):
        """pcm_bytes: raw 16-bit mono PCM at config.SAMPLE_RATE. Returns text.

        ``partial=True`` means this is a provisional read of an utterance still
        being spoken, headed for a live display and not for the saved
        transcript. It skips timestamping, which is work nobody will look at,
        and it does not disturb :attr:`last_language` — a half-finished
        sentence is a poor sample to label the finished line with, and the
        final pass is moments away.
        """
        model = self.load_partial() if partial else self.load()
        # int16 PCM -> float32 in [-1, 1], which is what whisper expects.
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        segments, info = model.transcribe(
            audio,
            # None = detect this utterance's language on its own.
            language=languages.normalize(config.WHISPER_LANGUAGE),
            task=getattr(config, "WHISPER_TASK", "transcribe"),
            beam_size=getattr(config, "WHISPER_BEAM_SIZE", 1),
            # Each utterance is independent, so carrying context between them
            # only invites the model to invent continuity that isn't there.
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            without_timestamps=partial,
        )
        if not partial:
            self.last_language = getattr(info, "language", None)
            self.last_language_prob = (
                getattr(info, "language_probability", 0.0) or 0.0)

        kept = []
        for seg in segments:
            # Drop segments Whisper itself is unsure are speech.
            if getattr(seg, "no_speech_prob", 0.0) > 0.6:
                continue
            if getattr(seg, "avg_logprob", 0.0) < -1.0:
                continue
            kept.append(seg.text.strip())

        text = " ".join(kept).strip()
        # Filter lone hallucination phrases (e.g. a noise blip -> "You").
        if text.lower() in _HALLUCINATIONS:
            return ""
        return text


# Back-compat: earlier code imported `Transcriber` directly.
Transcriber = FasterWhisperTranscriber
