"""Discovery of optional feature packages.

Vlocalhost Core is the complete recording engine: capture, transcription,
summarization, notes on disk, the window, the tray, the terminal and the MCP
server. It is the whole product for anyone who just wants to record their own
meetings privately.

Paid extensions ship as separate importable packages that are simply absent
from a core install. If one is present, this module calls its ``register()``
once at startup and it wires itself into the app's extension points — today
that means the calendar/email provider registry in :mod:`integrations`.

This is the *only* place in the core that knows a paid build exists, and it
knows nothing beyond the package name. Core code must never import an
extension directly or branch on whether one is loaded; it asks the relevant
registry what is available and behaves accordingly. That rule is what keeps
the two repositories from drifting into a fork.
"""

import importlib
import sys
from typing import List

#: Optional packages to look for, in load order.
PLUGIN_MODULES = ("vlocalhost_pro",)

_loaded: List[str] = []
_done = False


def load() -> List[str]:
    """Import and register every optional package that is installed.

    Safe to call repeatedly — the work happens once. Returns the names that
    registered successfully. A plugin that raises is reported and skipped:
    a broken extension must never take down local recording.
    """
    global _done
    if _done:
        return _loaded
    _done = True

    for module_name in PLUGIN_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue  # not installed — the ordinary case for a core build
        register = getattr(module, "register", None)
        if not callable(register):
            print(f"[plugins] {module_name} has no register() — skipped.",
                  file=sys.stderr, flush=True)
            continue
        try:
            register()
        except Exception as e:  # noqa: BLE001 - never let a plugin break startup
            print(f"[plugins] {module_name} failed to load: {e}",
                  file=sys.stderr, flush=True)
            continue
        _loaded.append(module_name)

    return _loaded


def loaded() -> List[str]:
    """Optional packages that registered successfully this run."""
    return list(_loaded)


def edition() -> str:
    """'Pro' or 'Core' — for the About box and diagnostics only.

    Never gate behaviour on this. Ask the relevant registry what it has
    instead, so a build with some extensions and not others still works.
    """
    load()
    return "Pro" if "vlocalhost_pro" in _loaded else "Core"
