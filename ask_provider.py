"""The extension point behind the Ask screen.

Core has no way to answer a question across meetings: that needs facts
extracted from each meeting, with citations, and Core extracts none. An
extension can register a provider here, and the Ask screen uses it. With
nothing registered -- the free build -- the screen says so and does nothing.

Same rule as every other registry in Core (``integrations``, ``actions``,
``mcp_tools``): Core asks whether something is registered and never which
product registered it.

A provider is any object with three methods:

    status()          -> {"indexed": int, "total": int}
    index_next()      -> {"done": str | None, "failed": str | None, "remaining": int}
                         extract one more meeting; called repeatedly by the UI
    ask(question)     -> {"head": str, "facts": [fact, ...], "source": str}
                         or None when nothing answers it.
                         fact = {"role", "text", "who", "due", "meeting", "at"}

Answers must be assembled from cited facts, never written by a model: every
fact names the meeting and the second it came from.
"""

from typing import Any, Optional

_PROVIDER: Optional[Any] = None


def register(provider: Any) -> None:
    for name in ("status", "index_next", "ask"):
        if not callable(getattr(provider, name, None)):
            raise TypeError(f"An ask provider needs a {name}() method.")
    global _PROVIDER
    _PROVIDER = provider


def get() -> Optional[Any]:
    return _PROVIDER


def available() -> bool:
    return _PROVIDER is not None


__all__ = ["available", "get", "register"]
