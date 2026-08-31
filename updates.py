"""Telling the user an update exists, without becoming something that watches them.

Every other part of this product can say "nothing leaves the machine" without
qualification. This module is the one exception, so the shape of it matters more
than the code in it.

Two rules it is built around:

**Nothing here runs on a schedule.** There is no background thread, no timer, no
daemon and no call at startup. :func:`check_now` performs exactly one HTTP GET and
it is only ever reached from a button a human pressed. An install that is never
clicked never contacts anything, which is what makes the airplane-mode
demonstration honest.

**The reminder is local.** :func:`due_for_reminder` reads the system clock and a
stored date and returns a bool. It sends nothing. That is what nudges a user who
would otherwise never think about updates, and it costs zero network traffic.

The request itself carries no identifier: no version, no install id, no machine
name, no telemetry. It asks GitHub for the latest release tag and the comparison
happens here, on the user's machine. The only thing the other end learns is that
some IP asked a public API a public question -- the same thing it learns when you
open the releases page in a browser, which is the alternative this replaces.

stdlib ``urllib`` is used rather than ``requests`` on purpose: it keeps the
outbound surface of this file to a single, greppable ``urlopen`` that anyone
auditing the claim can find in one search.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.request

import config
import network
import version
from integrations import store

# Bookkeeping lives in its own file, deliberately not in settings.json.
# ``settings`` is the user's configuration and it refuses any key that is not a
# real ``config`` attribute -- correctly, because a settings file that accepts
# arbitrary names stops describing anything. These three are app state, not
# preferences: nobody edits them and nothing breaks if they are deleted.
_STATE_FILE = "update_state.json"

LAST_PROMPT_KEY = "last_prompt"
LAST_CHECK_KEY = "last_check"
LAST_SEEN_KEY = "last_seen_version"


def _state() -> dict:
    """Saved bookkeeping. A missing or corrupt file reads as empty."""
    try:
        with open(store.path_for(_STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(**changes) -> None:
    """Merge *changes* in. Never raises: losing bookkeeping is not worth a crash."""
    data = _state()
    data.update(changes)
    try:
        with open(store.path_for(_STATE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except OSError:
        pass

_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def current() -> str:
    """The running version, from the one place that holds it."""
    return version.__version__


def parse(tag: str):
    """``"v1.2.0-rc.1"`` -> ``(1, 2, 0, "rc.1")``, or ``None`` if unparseable.

    Returning ``None`` rather than raising matters: a malformed or unexpected tag
    upstream must not be able to crash a user's app. Everything downstream treats
    ``None`` as "cannot tell", which resolves to "do not claim an update exists".
    """
    m = _TAG.match((tag or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def is_newer(candidate: str, than: str) -> bool:
    """True only when *candidate* is a released version strictly newer than *than*.

    A pre-release is never newer. Somebody running 1.2.0 should not be told that
    1.3.0-rc.1 is available -- release candidates are opted into deliberately, and
    an update prompt is not an opt-in.
    """
    a, b = parse(candidate), parse(than)
    if a is None or b is None:
        return False
    if a[3]:                      # candidate is a pre-release
        return False
    return a[:3] > b[:3]


# --- the local half: a reminder that sends nothing ------------------------

def _today() -> _dt.date:
    return _dt.date.today()


def _stored_date(key: str):
    raw = _state().get(key)
    if not raw:
        return None
    try:
        return _dt.date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None            # corrupt value behaves as "never"


def due_for_reminder() -> bool:
    """Should the app nudge the user to check? Local clock only, no network.

    Deliberately *elapsed days since last prompt*, not a calendar date such as
    the last day of the month. A calendar trigger only fires if the app happens
    to be running that day -- a laptop that is shut over a weekend skips the
    whole period silently, and the user is never reminded again until the next
    one. Elapsed time cannot miss: whenever the app next starts, the debt is
    still owed.
    """
    days = int(getattr(config, "UPDATE_REMINDER_DAYS", 7) or 0)
    if days <= 0:
        return False           # 0 disables it; honoured forever, never re-asked
    last = _stored_date(LAST_PROMPT_KEY)
    if last is None:
        mark_reminded()        # first run starts the clock, does not prompt
        return False
    today = _today()
    if last > today:
        # Clock moved backwards, or the file was edited. Re-base rather than
        # prompting on every launch until the date catches up again.
        mark_reminded()
        return False
    return (today - last).days >= days


def mark_reminded() -> None:
    _write_state(**{LAST_PROMPT_KEY: _today().isoformat()})


def last_checked() -> str:
    """ISO date of the last successful check, or ``""`` if never."""
    d = _stored_date(LAST_CHECK_KEY)
    return d.isoformat() if d else ""


# --- the network half: one GET, only ever from a click --------------------

class CheckFailed(Exception):
    """Could not reach the release list. Never an error the user must act on."""


def check_now(timeout: float = 6.0) -> dict:
    """Ask for the latest released version. **Only call this from a user action.**

    Returns ``{"latest": "1.2.1", "current": "1.2.0", "update": True, "url": ...}``.

    Raises :class:`CheckFailed` when offline or unreachable. Being offline is not
    an error condition for this product -- callers show the message and carry on,
    the same rule the calendar integration follows.

    On a sealed install it raises :class:`network.Sealed` before any socket is
    opened. That is a different exception on purpose: offline means *could not*,
    sealed means *would not*, and a user who set the switch deserves to be told
    which one they are looking at.
    """
    if not network.allowed("update_check"):
        raise network.refuse("update_check")

    req = urllib.request.Request(
        config.UPDATE_API_URL,
        headers={
            # GitHub requires a User-Agent. This one names the app and nothing
            # else -- deliberately no version, no OS, no machine. The comparison
            # happens locally, so the far end never learns what is installed.
            "User-Agent": "Vlocalhost-Update-Check",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - offline is ordinary, not exceptional
        raise CheckFailed(str(exc)) from exc

    latest = str(payload.get("tag_name") or "").lstrip("v")
    if not parse(latest):
        raise CheckFailed(f"unrecognised release tag: {latest!r}")

    now = current()
    network.record("update_check")
    _write_state(**{
        LAST_CHECK_KEY: _today().isoformat(),
        LAST_SEEN_KEY: latest,
        LAST_PROMPT_KEY: _today().isoformat(),   # a check satisfies the reminder
    })
    return {
        "latest": latest,
        "current": now,
        "update": is_newer(latest, now),
        "url": payload.get("html_url") or config.UPDATE_RELEASES_URL,
        "notes": (payload.get("body") or "").strip(),
    }


def describe() -> str:
    """One line for the CLI and the GUI, without performing a check."""
    seen = _state().get(LAST_SEEN_KEY) or ""
    when = last_checked()
    if seen and is_newer(str(seen), current()):
        return f"{current()} installed - {seen} is available"
    if when:
        return f"{current()} installed - last checked {when}"
    return f"{current()} installed - never checked"
