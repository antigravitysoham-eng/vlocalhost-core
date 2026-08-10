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

# Only these may be changed from the UI. Each name is a ``config`` attribute.
EDITABLE = (
    "CAPTURE_MODE",
    "CALENDAR_PROVIDER",
    "AUTO_START_FROM_CALENDAR",
    "EMAIL_SUMMARY_TO_ATTENDEES",
    "EMAIL_SUMMARY_TO_SELF",
    "POST_NOTES_TO_EVENT",
    "WHISPER_MODEL",
    "WHISPER_BEAM_SIZE",
    "WHISPER_COMPUTE",
    "WHISPER_CPU_THREADS",
    "RELEASE_MODEL_WHEN_IDLE",
    "WHISPER_LANGUAGE",
    "WHISPER_TASK",
    "NOTES_LANGUAGE",
    "OLLAMA_MODEL",
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
