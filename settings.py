"""User settings the app can change at runtime (from the GUI).

``config.py`` holds the defaults and the advanced knobs you edit by hand.
Anything the in-app UI can toggle is persisted as JSON in the per-user config
directory and overlaid onto :mod:`config` at startup — so the rest of the app
keeps reading ``config.X`` and never needs to know a UI exists.

    <config dir>/settings.json

Delete that file to go back to the defaults in ``config.py``.
"""

import json
import os

import config
from integrations import store

_FILE = "settings.json"

# Only these may be changed from the UI or the command line. Each name is a
# ``config`` attribute.
#
# Anything a user is *told* to configure has to be in here. A setting that only
# exists as a line in ``config.py`` is not really configurable: the installer
# unpacks a fresh copy of that file on every update, so hand edits are silently
# reverted the first time somebody upgrades.
EDITABLE = (
    "CAPTURE_MODE",
    "INPUT_DEVICE",
    "VAD_ENGINE",
    "VAD_THRESHOLD",
    "SILENCE_TIMEOUT_MS",
    "PARTIAL_INTERVAL_MS",
    "PARTIAL_MODEL",
    "CALENDAR_PROVIDER",
    "AUTO_START_FROM_CALENDAR",
    "EMAIL_SUMMARY_TO_ATTENDEES",
    "EMAIL_SUMMARY_TO_SELF",
    "POST_NOTES_TO_EVENT",
    "WHISPER_MODEL",
    "WHISPER_BEAM_SIZE",
    "WHISPER_COMPUTE",
    "WHISPER_CPU_THREADS",
    "WHISPER_DEVICE",
    "RELEASE_MODEL_WHEN_IDLE",
    "WHISPER_LANGUAGE",
    "WHISPER_TASK",
    "NOTES_LANGUAGE",
    "OLLAMA_URL",
    "OLLAMA_MODEL",
    "OUTPUT_DIR",
    # "module:ClassName" of a speech engine to use instead of faster-whisper.
    # This imports and runs code the user names, so the Settings tab keeps it
    # behind an Advanced disclosure and says so plainly. It is no more
    # privileged than config.py was — same user, same machine — but it is
    # worth being explicit about.
    "CUSTOM_TRANSCRIBER",
    # How often the local, network-free update reminder fires. 0 turns it off.
    "UPDATE_REMINDER_DAYS",
)


def path() -> str:
    """Absolute path of the settings file."""
    return store.path_for(_FILE)


def load() -> dict:
    """Saved overrides only. A missing or corrupt file reads as no overrides."""
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in EDITABLE}


def apply() -> dict:
    """Overlay the saved overrides onto :mod:`config`. Returns what was applied.

    Call this once at startup, before anything reads ``config``.
    """
    saved = load()
    for key, value in saved.items():
        setattr(config, key, value)
    return saved


def current() -> dict:
    """Effective value of every editable setting (defaults + any overrides)."""
    return {k: getattr(config, k, None) for k in EDITABLE}


def save(**changes) -> dict:
    """Persist ``changes`` and apply them to :mod:`config` immediately."""
    unknown = [k for k in changes if k not in EDITABLE]
    if unknown:
        raise KeyError(f"Not user-editable: {', '.join(sorted(unknown))}")

    data = load()
    data.update(changes)
    for key, value in changes.items():
        setattr(config, key, value)

    # Write via a temp file so a crash mid-write can't corrupt the settings.
    target = path()
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, target)
    return data


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_EMPTY = ("", "none", "null", "default")


def coerce(key: str, raw: str):
    """Turn a command-line string into the type this setting expects.

    The shape is taken from whatever ``config`` currently holds, so the rule
    stays right if a default changes. ``none`` clears a setting back to the
    default; a bare number becomes an int where the current value is one (or
    is unset, as with a microphone index).
    """
    current = getattr(config, key, None)
    text = raw.strip()

    if isinstance(current, bool):
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{key} expects true or false, not {raw!r}")

    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{key} expects a whole number, not {raw!r}") from None

    if isinstance(current, float):
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{key} expects a number, not {raw!r}") from None

    if text.lower() in _EMPTY:
        return None

    # INPUT_DEVICE defaults to None but takes an index or a name substring.
    if current is None and text.isdigit():
        return int(text)

    return text
