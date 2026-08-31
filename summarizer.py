"""Turn a raw transcript into structured meeting notes — bring your own model.

The transcript may be in any language, or several — lines are tagged with the
speaker and the detected language code. ``config.NOTES_LANGUAGE`` decides
whether the notes come back in English or in whatever was spoken.

Like :mod:`transcriber`, the engine is pluggable and the rest of the app never
hardcodes one. ``config.SUMMARY_ENGINE`` picks a built-in; ``CUSTOM_SUMMARIZER``
attaches anything else. Use :func:`build_summarizer` to get the configured
engine.

Any engine needs two methods, and may add a third:
    summarize(self, transcript)  -> Markdown notes
    title(self, transcript)      -> a short title, or "" if unavailable
    load(self)                   -> warm up (optional)

Everything above that line -- the prompts, the language rule, the timestamp
handling, the plain-text rendering -- belongs to the *product*, not to any one
engine, so it stays here and every backend gets it for free. A backend's whole
job is to turn a prompt into text.

Why this shape, when Ollama was the only engine for a year: because "bring your
own model" is the claim on the website, and a claim that rests on one process
being installed is one process away from being untrue. Ollama stays a
first-class backend and the default; it is simply no longer the only door.
"""

import importlib
import re

import requests

import config
import languages

# "[10:04:22] " or "[10:04] " at the head of a transcript line.
_TS = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", re.MULTILINE)
# The same marker anywhere in a line, for scrubbing what the model returns.
_TS_ANY = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")


def strip_timestamps(text: str) -> str:
    """Remove ``[HH:MM:SS]`` markers from a transcript, or from model output.

    The summary used to come back full of timestamps, which made the notes
    read as a second copy of the transcript rather than a summary of it. The
    cause was not the model misbehaving. It was handed a timestamp on every
    line and told the transcript was timestamped, so it mirrored the format
    back -- exactly what a language model is supposed to do. The fix is to
    stop showing it something we do not want returned.

    Speaker labels are deliberately kept: "You:" and "Them:" are what make an
    action item attributable to somebody.
    """
    return _TS.sub("", text or "")


def scrub_timestamps(text: str) -> str:
    """Belt and braces for the model's output.

    The instruction alone is not reliable across the range of local models
    people run -- a 3B model will cheerfully ignore it, and the failure is
    silent and ugly. Cheap to do, so do it.
    """
    return _TS_ANY.sub("", text or "")

_LANGUAGE_RULE = """
The transcript may be in any language, and may switch between languages. Lines \
may be tagged with the speaker and a language code, like "You (hi):". \
Those tags are metadata — never copy them into the notes. \
{directive}
"""

_PROMPT = """You are a meeting notes assistant. Below is a raw transcript of a \
meeting captured from a microphone. It may contain transcription errors, filler \
words, and incomplete sentences. Produce clean, professional meeting notes in \
Markdown with exactly these sections:

## Summary
A short paragraph (3-5 sentences) capturing what the meeting was about.

## Key Discussion Points
- Bullet points of the main topics discussed.

## Decisions
- Any decisions that were made. Write "None recorded." if there were none.

## Action Items
- [ ] Task — owner (if mentioned) — due date (if mentioned)
Write "None recorded." if there were none.

Only use information present in the transcript. Do not invent details.

Do not write clock times such as 10:04 unless a time was actually spoken as part of a decision or a deadline. These are notes, not a log. The timestamped record already exists in the transcript file saved beside this one, and repeating it here would make the two files copies of each other.
{language_rule}
TRANSCRIPT:
{transcript}
"""


_TITLE_PROMPT = """Give a short, descriptive title for the meeting described by \
the transcript below. Use 3 to 6 words. Respond with ONLY the title text — no \
quotes, no "Title:" label, no trailing punctuation, no explanation. \
Write the title in English even if the transcript is in another language, and \
use only plain ASCII letters — the title becomes a file name.
Name what the meeting was ABOUT. Never use the words "transcript", "recording", "notes", "summary" or "audio" — those describe the file, not the conversation, and they end up duplicated in the file name.

TRANSCRIPT:
{transcript}
"""


def _language_rule() -> str:
    """The instruction that decides what language the notes come back in."""
    setting = (getattr(config, "NOTES_LANGUAGE", "en") or "en").lower()
    if setting == "same":
        directive = ("Write the notes in the same language the meeting was "
                     "conducted in. If several were used, choose the dominant "
                     "one and keep the whole set of notes in it.")
    else:
        directive = (f"Write the notes in {languages.name_for(setting)}, "
                     "translating as needed, no matter what language was "
                     "spoken. Keep names, products and quoted phrases as they "
                     "were said.")
    return _LANGUAGE_RULE.format(directive=directive)


def _notes_prompt(transcript: str) -> str:
    """The notes prompt, timestamps stripped on the way in."""
    return _PROMPT.format(transcript=strip_timestamps(transcript),
                          language_rule=_language_rule())


def _title_prompt(transcript: str) -> str:
    """The title prompt, from the opening of the meeting only."""
    # 1200 characters, not 4000. Naming a meeting needs the opening minutes,
    # not the whole thing, and the cost of this call scales with what it is
    # given: measured on llama3.2 over a 7k-character transcript, the title
    # took 49.7s from 4000 characters, 15.3s from 1500 and 8.0s from 800 --
    # for a *better* title, because a short excerpt gives the model less to
    # ramble about. This call sits directly between a user pressing Stop and
    # their notes appearing, so it is worth being cheap.
    return _TITLE_PROMPT.format(transcript=transcript[:1200])


# --- the engines ----------------------------------------------------------

class OllamaSummarizer:
    """Notes from a model served by Ollama on ``config.OLLAMA_URL``.

    The default, and the only engine until now. It is kept first-class rather
    than deprecated: Ollama is a model *manager* as well as a runtime, and
    ``ollama pull mistral`` is a better experience than "find a GGUF" for
    anyone who already has it installed.
    """

    name = "ollama"

    def model_label(self) -> str:
        return config.OLLAMA_MODEL

    def status(self):
        """(ok, detail) — never raises, because a health check that throws is
        worse than one that says it does not know."""
        try:
            resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=4)
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception as e:                    # noqa: BLE001
            return False, (f"not reachable at {config.OLLAMA_URL} "
                           f"({e.__class__.__name__})")

        want = config.OLLAMA_MODEL
        # Ollama reports "llama3.2:latest" for a model pulled as "llama3.2".
        if any(n == want or n.split(":")[0] == want.split(":")[0] for n in names):
            return True, f"{want} ready"
        if names:
            return False, f"running, but {want} isn't pulled (ollama pull {want})"
        return False, f"running, but no models pulled (ollama pull {want})"

    def _generate(self, prompt: str, timeout: int) -> str:
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    def title(self, transcript: str) -> str:
        try:
            return self._generate(_title_prompt(transcript), 120)
        except requests.exceptions.RequestException:
            return ""

    def summarize(self, transcript: str) -> str:
        try:
            return self._generate(_notes_prompt(transcript), 600)
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                "Could not reach Ollama. Is it running? Start it with "
                f"`ollama serve` and pull the model with `ollama pull "
                f"{config.OLLAMA_MODEL}`."
            ) from e
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Ollama returned an error: {e}") from e


_ENGINES = {"ollama": OllamaSummarizer}


def _load_custom(spec):
    """Resolve "module.path:attr" to an instance (calls the attr if callable)."""
    if ":" not in spec:
        raise ValueError(
            f'CUSTOM_SUMMARIZER must look like "module.path:ClassName", got {spec!r}.'
        )
    module_name, attr = spec.split(":", 1)
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if callable(obj) else obj


def build_summarizer():
    """Return the configured notes engine.

    ``CUSTOM_SUMMARIZER`` wins when set — it imports and runs code the user
    named, which is no more privileged than editing config.py was, but is worth
    being explicit about. Otherwise ``SUMMARY_ENGINE`` selects a built-in.
    """
    spec = getattr(config, "CUSTOM_SUMMARIZER", None)
    if spec:
        engine = _load_custom(spec)
        for method in ("summarize", "title"):
            if not hasattr(engine, method):
                raise TypeError(
                    f"CUSTOM_SUMMARIZER {spec!r} must provide a "
                    f".{method}(transcript) method returning text."
                )
        return engine

    name = (getattr(config, "SUMMARY_ENGINE", "ollama") or "ollama").lower()
    engine_cls = _ENGINES.get(name)
    if engine_cls is None:
        raise ValueError(
            f"Unknown SUMMARY_ENGINE {name!r}. Installed: "
            f"{', '.join(sorted(_ENGINES))} - or set CUSTOM_SUMMARIZER."
        )
    return engine_cls()


# One engine, reused. Rebuilt when the settings that choose it change, so a
# switch in the UI takes effect without a restart -- and so an engine that has
# to load a model from disk pays that cost once rather than per meeting.
_engine = None
_engine_key = None


def engine():
    """The live engine, built on first use and cached until the config moves."""
    global _engine, _engine_key
    key = (getattr(config, "CUSTOM_SUMMARIZER", None),
           getattr(config, "SUMMARY_ENGINE", "ollama"))
    if _engine is None or key != _engine_key:
        _engine = build_summarizer()
        _engine_key = key
    return _engine


# --- what the rest of the app calls ----------------------------------------

# ``status`` and ``model_label`` are optional on an engine: the required
# contract stays the two methods that make notes, so somebody can write a
# working backend in ten lines. These fall back to something honest when a
# minimal engine does not implement them, rather than refusing to run it.

def status():
    """(ok, detail) for whatever engine is configured. Never raises."""
    try:
        eng = engine()
    except Exception as e:                        # noqa: BLE001
        return False, f"notes engine unavailable ({e.__class__.__name__})"
    probe = getattr(eng, "status", None)
    if probe is None:
        return True, f"{model_label()} (this engine reports no status)"
    try:
        return probe()
    except Exception as e:                        # noqa: BLE001
        return False, f"status check failed ({e.__class__.__name__})"


def model_label() -> str:
    """What to show a user when naming the notes model. Never raises."""
    try:
        eng = engine()
    except Exception:                             # noqa: BLE001
        return "unavailable"
    label = getattr(eng, "model_label", None)
    try:
        return label() if label else getattr(eng, "name", "custom")
    except Exception:                             # noqa: BLE001
        return getattr(eng, "name", "custom")

def generate_title(transcript: str) -> str:
    """Return a short human title for the meeting, or '' if unavailable.

    Used to name the saved files. Never raises — naming falls back to a
    timestamp when the model can't be reached.
    """
    try:
        return engine().title(transcript)
    except Exception:  # noqa: BLE001 - a title is never worth failing a meeting
        return ""


def to_plain_text(md: str) -> str:
    """Render the model's Markdown as plain text.

    The summary is saved as .txt and the same string is used for the email body
    and the calendar description. None of those three render Markdown: on
    Windows a .md file often has no default program at all, and an attendee
    reading the email just sees literal "## Summary" and "- [ ]" characters.

    This is not a general Markdown implementation and does not need to be. The
    model is asked for four fixed sections and produces headings, bullets and
    task boxes; those are what this handles.
    """
    out = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        # Inline emphasis and code ticks, which carry no meaning in plain text.
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", line)
        line = line.replace("`", "")

        stripped = line.strip()
        if not stripped:
            out.append("")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            text = heading.group(2).strip()
            # A blank line before a heading, but never a leading one.
            if out and out[-1] != "":
                out.append("")
            out.append(text.upper() if len(heading.group(1)) >= 2 else text)
            continue

        task = re.match(r"^[-*+]\s+\[([ xX])\]\s*(.*)$", stripped)
        if task:
            box = "x" if task.group(1).lower() == "x" else " "
            out.append("  [%s] %s" % (box, task.group(2).strip()))
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            out.append("  - %s" % bullet.group(1).strip())
            continue

        numbered = re.match(r"^(\d+[.)])\s+(.*)$", stripped)
        if numbered:
            out.append("  %s %s" % (numbered.group(1), numbered.group(2).strip()))
            continue

        out.append("  %s" % stripped)

    # Collapse runs of blank lines and trim the ends.
    text, blank = [], False
    for line in out:
        if line == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        text.append(line)
    return "\n".join(text).strip()


def summarize(transcript: str) -> str:
    """Return Markdown notes, or raise RuntimeError if the engine is unreachable.

    Timestamps are stripped on the way in and scrubbed on the way out, so the
    notes stay a summary rather than becoming a second transcript. The scrub
    happens here rather than in each engine: it is a property of the notes we
    want, not of the model that wrote them, and a backend author should not
    have to know about it to be correct.
    """
    return scrub_timestamps(engine().summarize(transcript))
