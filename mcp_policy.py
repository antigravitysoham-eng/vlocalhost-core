"""Whether a given meeting may be served to a connected assistant.

Core's MCP server hands out meetings. Whether it should hand out *all* of them
is a policy question, and policy is not the recorder's business — a free install
is one person wiring their own machine to their own assistant, and it has always
served everything.

So Core asks, and does not answer. With nothing registered every meeting is
allowed, which is exactly what every install already does. Something that wants
a narrower rule registers a filter:

    from mcp_policy import register_filter

    def only_recent(name, path):
        return (True, "") if fresh(name) else (False, "Outside the window.")

    register_filter("scope", only_recent)

A filter returns ``(allowed, reason)``. The reason is not decoration: a refusal
that reads as "not found" would let an assistant answer "you have no
commitments" from half an archive and sound certain about it, so the caller is
expected to say what was withheld rather than quietly withhold it.

**Filters fail open.** One that raises is skipped. A bug in a policy must not be
able to make somebody's own meetings unreadable on their own machine.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

#: ``(name, path) -> (allowed, reason)``
Filter = Callable[[str, str], Tuple[bool, str]]

_FILTERS: Dict[str, Filter] = {}


def register_filter(key: str, fn: Filter) -> None:
    """Add a policy. Registering the same key twice replaces it."""
    slug = (key or "").strip()
    if not slug:
        raise ValueError("A policy filter needs a non-empty key.")
    if not callable(fn):
        raise TypeError(f"Policy {slug!r} needs a callable.")
    _FILTERS[slug] = fn


def _filters() -> List[Filter]:
    try:
        from plugins import load

        load()
    except Exception:  # noqa: BLE001 - a broken loader means "no policy"
        pass
    return [_FILTERS[k] for k in sorted(_FILTERS)]


def enabled() -> bool:
    """True if anything is narrowing what assistants may read."""
    return bool(_filters())


def allows(name: str, path: str = "") -> Tuple[bool, str]:
    """Whether one meeting may be served, and why not if it may not.

    Every registered filter must agree. The first refusal wins and carries its
    own reason, so the message a user sees comes from whichever policy actually
    stopped it rather than from a generic denial here.
    """
    for fn in _filters():
        try:
            allowed, reason = fn(name, path)
        except Exception:  # noqa: BLE001 - a broken policy is not a refusal
            continue
        if not allowed:
            return False, reason or "Withheld by policy."
    return True, ""


def filter_notes(items):
    """Drop entries a policy refuses from a ``list_notes``-shaped list."""
    if not enabled():
        return list(items)
    return [i for i in items
            if allows(i.get("name", ""), i.get("path", ""))[0]]


def describe() -> str:
    """One line about what assistants may read, for status output.

    Each filter may supply its own ``describe`` attribute; those are joined.
    With nothing registered this states the free build's actual behaviour rather
    than implying a limit exists.
    """
    lines = []
    for fn in _filters():
        text = getattr(fn, "describe", None)
        if callable(text):
            try:
                said = text()
            except Exception:  # noqa: BLE001
                said = ""
            if said:
                lines.append(said)
    if not lines:
        return "Assistants may read every saved meeting. No limit is set."
    return " ".join(lines)


__all__ = ["Filter", "allows", "describe", "enabled", "filter_notes",
           "register_filter"]
