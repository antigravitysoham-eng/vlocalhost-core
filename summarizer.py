"""Turn a raw transcript into structured meeting notes using a local Ollama model.

The transcript may be in any language, or several — lines are tagged with the
speaker and the detected language code. ``config.NOTES_LANGUAGE`` decides
whether the notes come back in English or in whatever was spoken.
"""

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


def generate_title(transcript: str) -> str:
    """Return a short human title for the meeting, or '' if Ollama is unreachable.

    Used to name the saved files. Never raises — naming falls back to a
    timestamp when the model can't be reached.
    """
    prompt = _TITLE_PROMPT.format(transcript=transcript[:4000])
    try:
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return ""
    return resp.json().get("response", "").strip()


def summarize(transcript: str) -> str:
    """Return Markdown notes, or raise RuntimeError if Ollama is unreachable.

    Timestamps are stripped on the way in and scrubbed on the way out, so the
    notes stay a summary rather than becoming a second transcript.
    """
    prompt = _PROMPT.format(transcript=strip_timestamps(transcript),
                            language_rule=_language_rule())
    try:
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=600,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            "Could not reach Ollama. Is it running? Start it with `ollama serve` "
            f"and pull the model with `ollama pull {config.OLLAMA_MODEL}`."
        ) from e
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama returned an error: {e} — {resp.text}") from e

    return scrub_timestamps(resp.json().get("response", "").strip())
