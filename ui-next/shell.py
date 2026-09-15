"""The desktop window: Aurora, in a native frame, driven by Core's engine.

This is the shell the prototype was always aiming at. It opens a real OS window
— WebView2 on Windows, WKWebView on macOS, GTK or Qt on Linux — points it at
``index.html`` on disk, and hands it :class:`api.Api` as the bridge back to
Python.

**Why a webview and not a widget toolkit.** Aurora is drawn in HTML: rounded
surfaces, a backdrop-blurred chrome bar, a gradient wash, real control over
type. tkinter can do none of it and Qt would mean rewriting all of it in QML.
The webview renders the design as designed, and — unlike Electron — bundles no
browser: WebView2 and WKWebView are already on the machine.

**Why pywebview and not Tauri.** Tauri's backend is Rust and this one is
Python: Whisper, Ollama, the audio pipeline, the MCP server, ``engine.py``.
Tauri would mean a Rust shell plus a frozen Python sidecar plus IPC between
them — three moving parts where this has one process and one language.

Nothing in ``core/`` is modified to support this. The window is another front
end onto the same :class:`AppEngine` the tray and the MCP server already drive,
which is what makes it impossible for them to disagree about who holds the
microphone.

    python run_ui.py            # from the repository root
"""

import os
import pathlib
import sys
import threading

import webview
import webview.menu as menu

import api as api_mod

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")

#: Big enough for the two-column shell to keep its 212px sidebar and still
#: leave the meter a full measure. Below `min_size` the sidebar collapses to a
#: scrolling row — see the 860px breakpoint in app.css — so the window is
#: allowed to go there rather than being clamped above it.
WIDTH, HEIGHT = 1180, 820
MIN_SIZE = (680, 560)


def _menu(api):
    """A native menu bar.

    `designing-for-macos.md` wants every command reachable from the menu bar,
    and a webview supplies none of its own — so the commands the page owns are
    named here as well. The accelerators are the platform's: Command-comma
    opens settings, which `settings.md` calls the way people expect to get
    there.

    Each item drives the *page*, not the engine directly, so the window and the
    menu can never show different things: the menu asks the page to do what the
    button would have done.
    """
    def page(script):
        return lambda: _run(api, script)

    return [
        menu.Menu("File", [
            menu.MenuAction("Start / stop recording", page("vl.menu('record')")),
            menu.MenuSeparator(),
            menu.MenuAction("Open notes folder", page("vl.menu('folder')")),
        ]),
        menu.Menu("View", [
            menu.MenuAction("Recording", page("vl.menu('screen:record')")),
            menu.MenuAction("Library", page("vl.menu('screen:library')")),
            menu.MenuSeparator(),
            menu.MenuAction("Ask", page("vl.menu('screen:ask')")),
            menu.MenuAction("Assistants", page("vl.menu('screen:assistants')")),
            menu.MenuAction("Settings", page("vl.menu('screen:settings')")),
        ]),
    ]


def _run(api, script):
    """Fire a page command from a menu click, without letting it raise.

    Off the calling thread on purpose, for the same reason :func:`_closing`
    touches nothing: a menu action is delivered on the GUI thread, and
    ``evaluate_js`` waits there for a webview that needs that very thread to
    answer. One deadlock of this shape already shipped -- every window close
    hung -- and a menu item is not worth risking a second.
    """
    window = api._window
    if window is None:
        return

    def fire():
        try:
            window.evaluate_js(script)
        except Exception:                              # noqa: BLE001
            pass

    threading.Thread(target=fire, daemon=True, name="vl-menu").start()


#: The shutdown worker, so :func:`main` can wait for it after the GUI is gone.
_shutdown = None


def _closing(api):
    """Window close: let the window go, and finish the writing behind it.

    **Nothing here may call into the page.** This runs on the GUI thread, and
    ``evaluate_js`` blocks that thread until the webview has run the script --
    which the webview cannot do, because running it needs the same thread. The
    result is a deadlock, and Windows paints a deadlocked window as "Not
    Responding": every close appeared to hang the app.

    It was also pointless. Returning True closes the window, so a message
    pushed to the page here is drawn on a page that is going away. The wait
    that matters is the one below, and it happens after the GUI has gone.
    """
    global _shutdown
    _shutdown = threading.Thread(target=api.shutdown, daemon=True,
                                 name="vl-shutdown")
    _shutdown.start()
    return True


def main(argv=None):
    """Open the window. Returns when it closes."""
    if not os.path.isfile(INDEX):
        sys.exit(f"the page is missing: {INDEX}")

    # Overrides onto config before anything reads it — the same call Core's own
    # entry point makes, for the same reason.
    import settings
    settings.apply()

    # A **file:// URL**, not a bare path, and the difference is not cosmetic.
    #
    # Handed a plain filesystem path, pywebview serves it over its bundled
    # bottle server and the window ends up on http://127.0.0.1:<random>/ --
    # measured, not assumed. So the app was opening a listening socket to draw
    # itself, while the comment below said it did not and the pill in the
    # corner said "0 bytes out". Nothing left the machine, but a local port was
    # open for the life of every session, and the claim in the code was false.
    #
    # Passing an explicit file:// URL loads the identical page -- same node
    # count, same computed styles -- and opens nothing. `as_uri()` does the
    # percent-encoding this path needs, which is what makes it survive the
    # spaces in "Meeting Notes Agent".
    url = pathlib.Path(INDEX).resolve().as_uri()

    api = api_mod.Api()
    window = webview.create_window(
        "Vlocalhost AI",
        url=url,
        js_api=api,
        width=WIDTH, height=HEIGHT,
        min_size=MIN_SIZE,
        # The page paints its own ground from --paper, and the two must agree
        # or a resize flashes white on a dark appearance before the first frame
        # lands. This is Aurora's *light* --paper, so on a machine set to dark
        # there is a brief pale flash before the page paints. Known, and left
        # alone deliberately: matching it means reading the OS appearance
        # (a registry key on Windows, `defaults` on macOS), and new
        # platform-specific code is not a thing to add on the way into a
        # release. It is the same value the file has always carried.
        background_color="#FBFAFD",
    )
    api.attach(window)
    window.events.closing += lambda: _closing(api)

    debug = bool(argv and "--debug" in argv)

    # The hotkey's cold start. On Windows every press of the shortcut launches
    # a new process, so "start recording" arrives as an argument rather than as
    # a click, and the window has to honour it or the key silently does
    # nothing. It has to run *after* the GUI loop is up -- pywebview calls this
    # on its own thread once the window exists -- because the engine's first
    # callback needs a window to push into.
    start_now = bool(argv and "--record-on-start" in argv)

    def begin():
        try:
            api.start()
        except Exception as e:                      # noqa: BLE001
            api.push("error", {"message": f"Could not start recording: {e}"})

    # No `http_server` here, and the file:// URL above is the half that makes
    # that mean something -- omitting this argument alone did not, which is the
    # bug the comment on `url` records. The page is local files and asks for
    # nothing over a socket; an app whose pill reads "0 bytes out" should not
    # open a port to draw itself.
    webview.start(begin if start_now else None,
                  menu=_menu(api), debug=debug, private_mode=True)

    # The window is gone; the summary being written when it closed is not. That
    # thread is a daemon, so without this the interpreter would exit out from
    # under it and a meeting somebody sat through would lose its notes. Bounded
    # rather than open-ended: a stuck model must not leave a process nobody can
    # see holding the microphone lock.
    if _shutdown is not None:
        _shutdown.join(timeout=120)
        if _shutdown.is_alive():
            print("[shell] still writing notes after 120s; exiting anyway",
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
