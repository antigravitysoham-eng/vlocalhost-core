"""The extension point behind the Records screen.

Core records conversations and writes notes. Turning each conversation into a
record in somebody else's format -- their sheet's columns, their system's
fields -- is not something Core does. An extension can register a provider
here; the window then shows a Records screen and routes it to the provider.
With nothing registered -- the free build -- the screen does not exist at all,
because a screen that only says "not available" is an advert, not a feature.

Same rule as ``ask_provider`` and every other registry in Core: Core asks
whether something is registered and never which product registered it.

A provider is any object with a ``call(op, args)`` method returning a JSON-able
dict, for the operations in ``OPS``. Core passes the page's request through and
never interprets it, except ``try`` and ``anyway``: there Core reads the
meeting's transcript itself (it owns the notes folder) and hands the provider
the text.

Optionally ``on_window(notify)``: called once when the window is up, with a
``notify(payload)`` that sends a "records" event to the page. A provider uses it
to start any background work that only makes sense while the app is open.
"""

from typing import Any, Callable, Optional

#: What the page may ask for. Anything else is refused before it reaches the
#: provider, so a page bug cannot call into code it was never meant to reach.
OPS = ("overview", "templates", "create", "contract", "save", "approve",
       "records", "release", "try", "anyway", "remove")

_PROVIDER: Optional[Any] = None


def register(provider: Any) -> None:
    if not callable(getattr(provider, "call", None)):
        raise TypeError("A records provider needs a call(op, args) method.")
    global _PROVIDER
    _PROVIDER = provider


def get() -> Optional[Any]:
    return _PROVIDER


def available() -> bool:
    return _PROVIDER is not None


def window_started(notify: Callable[[dict], None]) -> None:
    """Tell the provider the window is up. Never raises: a provider that
    cannot start its background work must not stop the window opening."""
    p = _PROVIDER
    hook = getattr(p, "on_window", None) if p is not None else None
    if callable(hook):
        try:
            hook(notify)
        except Exception as e:  # noqa: BLE001
            print(f"[records] provider did not start: {e}", flush=True)


__all__ = ["OPS", "available", "get", "register", "window_started"]
