"""Open the Aurora window from Core.

``ui-next/`` carries a hyphen, so it is not importable as a package and never
will be -- the name is the one the design has been called since it was drawn,
and renaming a directory to satisfy an import is the tail wagging the dog. This
module is the seam instead: it puts that directory on ``sys.path`` and hands
off, so :mod:`vlocalhost` can say ``import ui_shell`` and know nothing about
where the page lives.

It is deliberately thin. Everything real -- the window, the bridge, the page --
is in ``ui-next/``, and the only thing that belongs here is the one import
trick that directory's name forces on us.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "ui-next")


def available():
    """Can this machine open the Aurora window?

    Two ways it cannot: the page was left out of the build, or there is no
    pywebview. Both are answered without importing anything heavy, because
    :func:`vlocalhost.main` asks this on the way into every launch.
    """
    if not os.path.isfile(os.path.join(UI, "index.html")):
        return False, "the window's page is not in this build"
    try:
        import webview  # noqa: F401
    except Exception as e:                            # noqa: BLE001
        return False, f"pywebview is unavailable ({e})"
    return True, ""


def run(record_on_start=False, argv=()):
    """Open the window. Returns when it closes.

    Raises if the window cannot be opened, which is the contract
    :func:`vlocalhost.main` falls back on: it catches, says so, and tries the
    tkinter window next.
    """
    ok, why = available()
    if not ok:
        raise RuntimeError(why)

    # Last, so a module of ours never shadows one of Core's. Core is already on
    # the path by the time anything calls this -- it is how this module was
    # imported -- and `shell` and `api` import `config`, `settings` and
    # `engine` flat, exactly as every other Core module does.
    if UI not in sys.path:
        sys.path.append(UI)

    import shell

    args = list(argv)
    if record_on_start and "--record-on-start" not in args:
        args.append("--record-on-start")
    return shell.main(args)
