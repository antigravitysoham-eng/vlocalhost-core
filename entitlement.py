"""Whether this copy has been paid for — asked of the install, not assumed.

Core cannot see its own extensions and must not go looking. What it can do is
leave somewhere for one to say *money already changed hands*, and read the
answer back. That is the whole of this module: a one-word registry with a
default of :data:`FREE`.

It exists because one question in the app is not a question about features.
"Which calendar providers are installed" is answered by
:mod:`integrations`; "what can I do with a finished meeting" by :mod:`actions`.
Those registries are the right place for every *capability* decision and this
module must never be used for one. But the tip prompt asks the user for a
coffee, and the only thing it needs to know is whether it is talking to
somebody who already bought the paid build. No feature registry answers that,
and inferring it from "are there providers installed" would be wrong the first
time anyone ships a package that is paid for and registers no provider.

Nothing here validates anything, and that is deliberate. :func:`declare` is an
assertion made by whatever package is installed alongside Core, not a licence
check — a build that ships the paid package *is* the paid product. If a
licence file ever gates the tier, it gates it on the paid package's side and
this module still only reports the result.
"""

from __future__ import annotations

#: The default, and what a core-only build always reports.
FREE = "free"

_tier = FREE
_label = ""


def declare(tier: str, label: str = "") -> None:
    """Record that this install is ``tier`` — called by an optional package.

    ``tier`` is a short lowercase word (``"pro"``, ``"team"``); ``label`` is
    how to name it to a human. Called from a package's ``register()``, so it
    has happened by the time anything reads it — see :func:`tier`.

    Last caller wins, which is what you want if a build somehow carries two
    paid packages: the one that loads last is the one the user bought most
    recently. An empty or whitespace-only tier is ignored rather than raising,
    because nothing in Core is worth breaking over this.
    """
    global _tier, _label
    cleaned = (tier or "").strip().lower()
    if not cleaned:
        return
    _tier = cleaned
    _label = (label or cleaned.title()).strip()


def tier() -> str:
    """This install's tier: :data:`FREE`, or whatever a package declared.

    Loads the optional packages first, because the honest answer depends on
    them and the caller has no reason to know that. ``plugins.load()`` does its
    work once and is cheap every time after, so this stays a plain read.
    """
    try:
        import plugins

        plugins.load()
    except Exception:  # noqa: BLE001 - a broken plugin loader means "free"
        pass
    return _tier


def label() -> str:
    """How to name this tier to a human. Empty string for a free install."""
    tier()
    return _label


def is_paid() -> bool:
    """True if any installed package has declared this a paid build."""
    return tier() != FREE


__all__ = ["FREE", "declare", "is_paid", "label", "tier"]
