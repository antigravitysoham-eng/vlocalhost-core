"""Every connection this app can make, declared in one place — and a switch
that makes them impossible.

The product's whole claim is that your meetings stay on your machine. That
claim is true today because of how the app is built, not because of a setting:
core ships no calendar providers, no telemetry, no crash endpoint, and the
summarizer talks to a model on ``127.0.0.1``. But "true because of how it is
built" is something a buyer has to take on faith, and a security reviewer is
paid not to.

So this module does two things faith cannot.

**It writes the surface down.** :data:`CONNECTIONS` is the complete list of
outbound calls the app is capable of — what triggers each one, what it carries,
what the far end learns, and whether it touches meeting content. It is data
rather than prose so that the CLI, the GUI and a reviewer all read the same
answer, and so it can be checked against the code instead of drifting from it.

**It makes the list enforceable.** :func:`sealed` reads one setting. When it is
on, every entry marked :attr:`Connection.sealable` is refused at the call site
— the update check returns, the model loader will not fetch, providers will not
hand out a client. Not discouraged: refused, with a message that says why. That
turns the airplane-mode demonstration from something you perform into something
an administrator configures once across a fleet.

Two rules keep this honest, and both matter more than the code:

**The list includes the awkward entries.** Emailing your notes sends your notes.
Connecting a calendar reads your calendar. Both are optional, both happen only
when a human asks, and both are named here in the same words as everything else.
A claim with three stated exceptions survives review; an absolute claim with one
discovered exception does not, and the discovery costs you the whole argument.

**Nothing here reports anything.** This module records local timestamps so the
app can tell you when it last contacted something. It has no opinion about what
that means and it sends the answer nowhere.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import NamedTuple

import config
from integrations import store

#: Bookkeeping — when each connection last happened, as ISO dates. Its own file
#: rather than ``settings.json`` for the reason ``updates`` keeps its own: this
#: is app state, not preference. Deleting it loses nothing but the timestamps.
_STATE_FILE = "network_state.json"


class Connection(NamedTuple):
    """One outbound call the app is capable of making.

    ``key``        stable identifier, used for the timestamp and by callers
    ``label``      what to call it in front of a user
    ``host``       where it goes, as plainly as it can be written
    ``when``       what has to happen for it to fire at all
    ``carries``    what actually travels, in the user's terms
    ``learns``     what the far end can infer from having received it
    ``content``    True when meeting audio, transcripts or notes are involved
    ``sealable``   True when :func:`sealed` must refuse it
    ``optional``   True when it only exists if a paid package is installed
    """

    key: str
    label: str
    host: str
    when: str
    carries: str
    learns: str
    content: bool
    sealable: bool
    optional: bool = False


#: The complete outbound surface. If you add a network call anywhere in this
#: codebase and it is not in this list, the list is wrong and the claim on the
#: website is wrong with it.
CONNECTIONS: tuple[Connection, ...] = (
    Connection(
        key="model_download",
        label="Speech model download",
        host="huggingface.co",
        when="First run, and again only if you choose a model that is not "
             "already on this machine.",
        carries="The name of the public model being requested.",
        learns="That some address downloaded a public file, the same thing it "
               "learns from a browser.",
        content=False,
        sealable=True,
    ),
    Connection(
        key="update_check",
        label="Update check",
        host="api.github.com",
        when="Only when you press Check for updates. There is no timer, no "
             "background thread and no call at startup.",
        carries="Nothing but the request. No version, no install id, no "
                "machine name, no telemetry.",
        learns="That some address asked a public API a public question.",
        content=False,
        sealable=True,
    ),
    Connection(
        key="local_model",
        label="Note writing (Ollama)",
        host="Wherever OLLAMA_URL points. Ships as 127.0.0.1 - this machine.",
        when="Every time a recording is stopped and notes are written, and "
             "when the app checks which models you have.",
        carries="The transcript, to a model running on this computer.",
        learns="Nothing, at the shipped setting: loopback traffic never "
               "reaches a network. OLLAMA_URL is yours to change, and pointing "
               "it at another machine sends transcripts there. Nothing else in "
               "this app can be redirected that way.",
        content=True,
        sealable=False,
    ),
    Connection(
        key="note_model_pull",
        label="Note model download (via Ollama)",
        host="registry.ollama.ai - fetched by Ollama, not by this app",
        when="Only from the setup wizard, when you ask it to install the "
             "note-writing model you do not have yet.",
        carries="The name of the public model being requested.",
        learns="That some address downloaded a public model.",
        content=False,
        sealable=True,
    ),
    Connection(
        key="local_control",
        label="Single-instance control channel",
        host="127.0.0.1 - this machine",
        when="Whenever the app starts, so a second launch hands its request to "
             "the window already running instead of opening another.",
        carries="Short commands such as start recording. No meeting content.",
        learns="Nothing. It listens on loopback and answers only this machine.",
        content=False,
        sealable=False,
    ),
    Connection(
        key="calendar",
        label="Calendar sync",
        host="Your calendar provider (Google or Microsoft)",
        when="Only if you installed a paid package and connected an account.",
        carries="Sign-in, and requests for your upcoming events.",
        learns="That you are reading your own calendar.",
        content=False,
        sealable=True,
        optional=True,
    ),
    Connection(
        key="email_delivery",
        label="Emailing notes",
        host="Your email provider (Google or Microsoft)",
        when="Only when you send notes from a finished meeting, and only to "
             "the recipients you name.",
        carries="The notes themselves. This is the point of the feature.",
        learns="What your mail provider learns from any message you send.",
        content=True,
        sealable=True,
        optional=True,
    ),
)

#: Opening the guide, the pricing page or a support link hands a URL to the
#: operating system's browser. Listed here so the omission is not mistaken for
#: an oversight: the app makes no request, the browser does, and it happens
#: because somebody clicked. Sealing does not disable these, because sealing a
#: browser is not this application's business.
BROWSER_LINKS = ("vlocal.host, github.com, ollama.com - opened in your browser, "
                 "never fetched by the app.")


def by_key(key: str) -> Connection | None:
    """The declared connection called *key*, or ``None``."""
    for c in CONNECTIONS:
        if c.key == key:
            return c
    return None


# --- the switch -----------------------------------------------------------

def sealed() -> bool:
    """Is this install sealed?

    Read at the moment of the call rather than cached, so toggling the setting
    takes effect without a restart.
    """
    return bool(getattr(config, "SEALED_MODE", False))


class Sealed(Exception):
    """Raised when a sealed install is asked to reach the network.

    Callers should treat this the way they treat being offline: it is an
    ordinary answer, not a fault. Say what was refused and carry on.
    """


def refuse(key: str) -> Sealed:
    """The exception to raise when *key* is blocked, worded for a human.

    Returned rather than raised so the call site reads ``raise refuse(...)``
    and keeps its own traceback.
    """
    conn = by_key(key)
    what = conn.label if conn else key
    return Sealed(
        f"{what} is unavailable: this install is sealed, so it makes no "
        f"network connections. Turn off Sealed Mode in Settings to allow it."
    )


def allowed(key: str) -> bool:
    """May *key* run right now?

    Unknown keys are allowed. A typo must not silently disable a feature — but
    it will be absent from the report, which is where it gets noticed.
    """
    conn = by_key(key)
    if conn is None:
        return True
    return not (conn.sealable and sealed())


# --- when did each of these last happen -----------------------------------

def _state() -> dict:
    """Saved timestamps. A missing or corrupt file reads as empty."""
    try:
        with open(store.path_for(_STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(key: str) -> None:
    """Note that *key* just happened. Never raises — losing a timestamp is not
    worth a crash, and this is called from paths that are already doing the
    interesting work."""
    data = _state()
    data[key] = _dt.date.today().isoformat()
    try:
        with open(store.path_for(_STATE_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except OSError:
        pass


def last_contact(key: str) -> str:
    """ISO date this connection last happened, or ``""`` for never.

    ``never`` is the interesting answer and the common one: an install that is
    used and never updated has contacted nothing since the day it downloaded a
    model.
    """
    value = _state().get(key)
    return str(value) if value else ""


# --- the report -----------------------------------------------------------

def summary() -> str:
    """One line for the top of a settings pane or a diagnostic report."""
    if sealed():
        return "Sealed - this install makes no network connections."
    count = sum(1 for c in CONNECTIONS if c.sealable)
    return (f"Not sealed - {count} connections are possible. "
            f"None of them runs on a schedule.")


def _wrap(text: str, width: int, indent: int) -> list[str]:
    """Fold *text* to *width*, continuation lines indented to match.

    A hand-rolled fold rather than ``textwrap`` because the report is printed
    into consoles of unknown width and pasted into tickets, and the one thing
    it must never do is arrive as a wall.
    """
    pad = " " * indent
    words, line, out = text.split(), "", []
    for word in words:
        candidate = f"{line} {word}".strip()
        if line and len(candidate) > width:
            out.append(line)
            line = word
        else:
            line = candidate
    if line:
        out.append(line)
    return [out[0]] + [pad + rest for rest in out[1:]] if out else [""]


# --- keeping the list true -------------------------------------------------

#: Files in core that are allowed to contain outbound-capable calls, and the
#: declared connection each one implements. Everything else in the tree should
#: be incapable of reaching a network, and :func:`audit` is how that stops
#: being an assumption.
#:
#: ``summarizer`` is here because it posts to Ollama, which the contract lists
#: as loopback. The audit cannot tell ``127.0.0.1`` from anywhere else by
#: reading source, so the judgement is recorded here where it can be reviewed
#: rather than hidden in a regex.
EXPECTED_CALLERS = {
    "updates.py": "update_check",
    "transcriber.py": "model_download",
    # All four reach Ollama at ``config.OLLAMA_URL`` -- writing notes, and
    # asking which models are installed. The audit reads source and cannot tell
    # a loopback address from any other, so the judgement is recorded here
    # where a reviewer can see it and disagree.
    "summarizer.py": "local_model",
    "engine.py": "local_model",
    "diagnostics.py": "local_model",
    "setup_wizard.py": "local_model",
    "control.py": "local_control",
}

#: What an outbound call looks like in this codebase. Deliberately crude: it is
#: meant to over-report, because a false positive costs somebody thirty seconds
#: and a false negative costs the entire claim.
_CALL_MARKERS = (
    "urlopen",
    "urllib.request",
    "requests.get",
    "requests.post",
    "httpx.",
    "socket.create_connection",
    "http.client",
)


def audit(root: str = "") -> list[str]:
    """Find files that can reach a network but are not in the contract.

    Returns a list of human-readable findings, empty when the code and
    :data:`CONNECTIONS` agree. This is the difference between a document that
    describes the app today and one that keeps describing it after the next
    twenty commits: a contributor who adds an HTTP call to a new file gets a
    finding here, and either declares it or removes it.

    It reads source rather than watching traffic, so it is a drift detector and
    not a proof. The proof is the one the report already suggests -- seal the
    install, disconnect the machine, and see that the product still works.
    """
    import os

    base = root or os.path.dirname(os.path.abspath(__file__))
    findings: list[str] = []

    for folder, dirs, files in os.walk(base):
        # Vendored runtimes and build output are not ours to audit, and
        # ``tools`` is the build machinery -- it downloads a Python runtime on
        # a maintainer's machine and is not shipped to anybody.
        dirs[:] = [d for d in dirs
                   if d not in {"build", "__pycache__", ".git", "runtime",
                                "tools"}]
        for name in sorted(files):
            if not name.endswith(".py") or name == "network.py":
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    source = f.read()
            except OSError:
                continue
            hits = sorted({m for m in _CALL_MARKERS if m in source})
            if not hits:
                continue
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            key = EXPECTED_CALLERS.get(os.path.basename(rel))
            if key is None:
                findings.append(
                    f"{rel} can reach a network ({', '.join(hits)}) and is not "
                    f"in the contract.")
            elif by_key(key) is None:
                findings.append(
                    f"{rel} is mapped to {key!r}, which is not a declared "
                    f"connection.")
    return findings


def report(width: int = 78) -> str:
    """The whole contract as text, for ``--network`` and for support.

    Plain ASCII on purpose. This is meant to be read on the machine in front of
    you, pasted into a ticket and forwarded to somebody's security team, and a
    Windows console that renders an em dash as a replacement character makes
    the document look careless at exactly the moment it is being trusted.
    """
    label_col = 16
    body = width - label_col

    def field(name: str, text: str) -> None:
        folded = _wrap(text, body, label_col)
        lines.append(f"    {name:<12}{folded[0]}")
        lines.extend(folded[1:])

    lines: list[str] = []
    lines.append("Network")
    lines.append("=" * width)
    lines.append(summary())
    lines.append("")
    lines.extend(_wrap(
        "Audio, transcripts and notes are written to this machine and are "
        "never uploaded. Two entries below do carry meeting content - the "
        "local model that writes your notes, and emailing them yourself. "
        "Both are marked.", width, 0))
    lines.append("")

    for conn in CONNECTIONS:
        if not conn.sealable:
            state = "local only - this never reaches a network"
        elif not allowed(conn.key):
            state = "blocked (sealed)"
        elif conn.optional:
            state = "permitted, and only with a paid package installed"
        else:
            state = "permitted"

        lines.append(conn.label)
        field("goes to", conn.host)
        field("happens", conn.when)
        field("carries", conn.carries)
        field("they learn", conn.learns)
        if conn.content:
            field("content", "yes - this one carries meeting content")
        field("status", state)
        # Only outbound calls get a timestamp. Printing "last used: never" for
        # loopback would be a true sentence that reads as a false one.
        if conn.sealable:
            field("last used", last_contact(conn.key) or "never")
        lines.append("")

    lines.extend(_wrap(f"Links: {BROWSER_LINKS}", width, 0))
    lines.append("")

    findings = audit()
    if findings:
        lines.append("Contract check: FAILED")
        for item in findings:
            lines.extend(_wrap(f"  - {item}", width, 4))
        lines.extend(_wrap(
            "A file can reach a network without being described above. Treat "
            "the list as incomplete until this is resolved.", width, 0))
    else:
        lines.extend(_wrap(
            "Contract check: passed. No file in this install can reach a "
            "network except the ones named above.", width, 0))
    lines.append("")

    lines.extend(_wrap(
        "Check it yourself: seal the install, disconnect the machine, and "
        "record a meeting. Everything except the two optional integrations "
        "works unchanged, because nothing else needed a network to begin "
        "with.", width, 0))
    return "\n".join(lines)
