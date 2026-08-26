"""A named Windows mutex, held for the life of the process.

This exists for the installer, not for the app. ``vlocalhost.iss`` declares

    AppMutex=Vlocalhost.AI.Running

and Inno checks for that name before it starts replacing files. Without it an
upgrade over a running copy hits locked files, and Inno's fallback is to ask for
a reboot -- a genuinely bad experience for something that lives in the tray and
is therefore running approximately always.

``AppMutex`` on its own does nothing: it is the *installer* half of a handshake,
and it only works if the application actually creates a mutex by that name. That
is the half this module is.

No-op on macOS and Linux, where the packaging formats do not have the problem.
"""

from __future__ import annotations

import sys

#: Must match ``AppMutex`` in ``installer/windows/vlocalhost.iss`` exactly.
MUTEX_NAME = "Vlocalhost.AI.Running"

# Module-level so the handle lives as long as the interpreter does. A local
# would be garbage-collected the moment hold() returned, closing the mutex and
# leaving the installer to believe nothing is running -- which is the whole bug
# this is meant to prevent, reintroduced silently.
_handle = None


def hold() -> bool:
    """Create the mutex. Returns True if this process now holds a handle.

    Never raises. A failure here must not stop the app from starting: the cost
    of failing is a worse upgrade experience later, which is not a reason to
    refuse to run now.
    """
    global _handle
    if not sys.platform.startswith("win"):
        return False
    if _handle is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        create = ctypes.windll.kernel32.CreateMutexW
        create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        # bInitialOwner=False: we only need the name to exist while we run, not
        # ownership. A second instance opening the same name is fine and
        # expected -- this is not a single-instance lock, and using it as one
        # would break the perfectly reasonable "tray plus a CLI run" case.
        _handle = create(None, False, MUTEX_NAME)
        return bool(_handle)
    except Exception:  # noqa: BLE001 - best effort, by design
        _handle = None
        return False
