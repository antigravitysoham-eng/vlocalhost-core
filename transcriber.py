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
        self.last_language = None       # ISO code detected for the last utterance
        self.last_language_prob = 0.0   # how sure Whisper was (0-1)

    def load(self):
        """Load the model. Call once up front so the first utterance isn't slow."""
        if self._model is None:
            from faster_whisper import WhisperModel

            warning = languages.check(config.WHISPER_MODEL,
                                      config.WHISPER_LANGUAGE)
            if warning:
                # Refuse rather than transcribe nonsense — see languages.check.
                raise RuntimeError(warning)

            # WHISPER_MODEL may be a name, a HF repo id, or a local model folder.
            self._model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE,
                cpu_threads=getattr(config, "WHISPER_CPU_THREADS", 0),
            )
        return self._model

    def unload(self):
        """Drop the model and give the memory back — a few hundred MB.

        Safe to call any time; the next transcribe() reloads it.
        """
        if self._model is None:
            return False
        self._model = None
        import gc

        gc.collect()
        return True

    def transcribe(self, pcm_bytes):
        """pcm_bytes: raw 16-bit mono PCM at config.SAMPLE_RATE. Returns text."""
        model = self.load()
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
        )
        self.last_language = getattr(info, "language", None)
        self.last_language_prob = getattr(info, "language_probability", 0.0) or 0.0

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
