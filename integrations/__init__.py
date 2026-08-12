"""Calendar/email integrations — the interface and the registry.

The core app never imports a concrete provider. It asks :func:`get_provider`
for one by name, and provider implementations register themselves by calling
:func:`register_provider` at import time.

That indirection is what keeps the product honest about its own promise: a
core build ships **no** providers at all, so there is no code path that can
reach the network on your behalf. Everything is local because nothing else is
installed — not because a setting says so.

Optional feature packages (see :mod:`plugins`) add providers by registering
them::

    from integrations import register_provider

    def register():
        register_provider("google", GoogleProvider,
                          label="Google — Calendar + Gmail",
                          requires="google-api-python-client google-auth-oauthlib")

Ask :func:`available_providers` what is installed rather than assuming any
particular name exists; in a core-only build the answer is an empty list.
"""

from typing import Callable, Dict, List, NamedTuple, Optional

from .base import CalendarProvider, Event

#: Where to point someone whose build has no providers installed.
UPGRADE_URL = "https://antigravitysoham-eng.github.io/vlocalhost-ai/pricing/"


class _Registration(NamedTuple):
    """One registered provider: how to build it and what it needs."""

    factory: Callable[[], CalendarProvider]
    label: str
    requires: str  # pip package names, for the "not installed" message


_REGISTRY: Dict[str, _Registration] = {}
_plugins_loaded = False


def register_provider(name: str,
                      factory: Callable[[], CalendarProvider],
                      *,
                      label: Optional[str] = None,
                      requires: str = "") -> None:
    """Make ``name`` available to :func:`get_provider`.

    ``factory`` is called with no arguments to build the provider, and is only
    called on demand — so a provider whose third-party packages are missing
    costs nothing until someone actually selects it.
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("A provider needs a non-empty name.")
    _REGISTRY[key] = _Registration(factory, label or key.title(), requires)


def _ensure_plugins_loaded() -> None:
    """Give optional feature packages a chance to register, exactly once.

    Imported here rather than at module scope so that :mod:`plugins` can import
    from this module without a circular import at startup.
    """
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True  # set first: a failing loader must not retry forever
    try:
        import plugins
    except ImportError:
        return
    plugins.load()


def available_providers() -> List[str]:
    """Names of every installed provider, sorted. Empty in a core-only build."""
    _ensure_plugins_loaded()
    return sorted(_REGISTRY)


def provider_label(name: str) -> str:
    """Human-readable name for a provider, for menus and window text."""
    _ensure_plugins_loaded()
    entry = _REGISTRY.get((name or "").lower())
    return entry.label if entry else (name or "").title()


def get_provider(name: Optional[str]) -> Optional[CalendarProvider]:
    """Return a provider instance for ``name``, or None if disabled.

    None/'none' means "stay local" and is not an error. An unknown name raises
    ValueError; a known provider whose packages are missing raises RuntimeError
    with the install command.
    """
    _ensure_plugins_loaded()
    if not name or name.lower() == "none":
        return None

    key = name.lower()
    entry = _REGISTRY.get(key)
    if entry is None:
        if not _REGISTRY:
            raise RuntimeError(
                f"No calendar or email providers are installed in this build, "
                f"so {name!r} is unavailable. Recording, transcription and "
                f"summaries all work without one. Calendar auto-start and "
                f"emailing notes are Vlocalhost Pro features — {UPGRADE_URL}"
            )
        raise ValueError(
            f"Unknown calendar provider: {name!r} "
            f"(installed: {', '.join(sorted(_REGISTRY))}, or 'none')."
        )

    try:
        return entry.factory()
    except ImportError as e:
        hint = f"\n  pip install {entry.requires}" if entry.requires else ""
        raise RuntimeError(
            f"The {key} integration needs extra packages.{hint}"
        ) from e


__all__ = [
    "CalendarProvider",
    "Event",
    "UPGRADE_URL",
    "available_providers",
    "get_provider",
    "provider_label",
    "register_provider",
]
