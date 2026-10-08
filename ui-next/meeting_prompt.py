"""The small window that offers to take notes when a call starts.

:mod:`meeting_detect` says *that* a call started; this decides what a person
sees. Two moments, one window:

* **A call started** -- "Zoom call started. Take notes?" with one button. It
  does not take focus: the person is joining a call, maybe typing a password,
  and a window that grabs the keyboard at that moment is how an offer becomes
  a nuisance. It waits twenty seconds, longer while the pointer is on it, and
  goes. "Not now" is not asked again for ten minutes; "Don't ask for Zoom" is
  never asked again, and Settings can undo it.
* **A call you are recording ended** -- "Saving in 20s", with "Keep
  recording". A recording nobody stopped is an hour of silence and whatever
  was said in the room afterwards; a recording stopped without warning is a
  meeting that ran over and lost its ending. A countdown with a cancel is the
  only answer that is safe both ways.

Where it sits: bottom-right, over the taskbar, where Windows puts its own
notifications, so it reads as the system talking rather than an ad.

**Made once, hidden, and shown without activation.** pywebview's own show
path calls ``Form.Show()`` and then focuses the webview, and ``focus=False``
does not survive that: the first build of this took the foreground from the
call it was announcing -- measured, the popup was the foreground window. So
the window is created at startup, while this app is the foreground anyway, and
every later appearance is ``SetWindowPos(... SWP_NOACTIVATE | SWP_SHOWWINDOW)``
straight to Win32, which cannot activate. It also means the offer appears the
instant the call is noticed rather than after a WebView2 cold start.
"""

import os
import pathlib
import platform
import threading

import config
import meeting_detect
import settings

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "prompt.html")

#: CSS pixels. The page is laid out for exactly this; see prompt.html.
WIDTH, HEIGHT = 372, 148
#: Gap from the work area's edge -- the same inset Windows gives its toasts.
MARGIN = 16

#: How long the offer waits, and how long the end countdown runs. Both are
#: shown on the window as a draining bar, so neither is a surprise.
OFFER_SECONDS = 20
SAVE_SECONDS = 20


class PromptApi:
    """What the small window can ask for. Every method is a button."""

    def __init__(self, owner):
        self._owner = owner

    def state(self):
        return self._owner.page_state()

    def take_notes(self):
        return self._owner.take_notes()

    def not_now(self):
        return self._owner.not_now()

    def never(self):
        return self._owner.never()

    def save_now(self):
        return self._owner.save_now()

    def keep_recording(self):
        return self._owner.keep_recording()

    def close(self):
        return self._owner.close()


class MeetingPrompt:
    """Owns the watcher and the one small window. Built by the shell."""

    def __init__(self, api):
        self.api = api
        self.watcher = None
        self.window = None
        self._mode = None          # "offer" | "ended" | "recording"
        self._call = None
        self._from_offer = set()   # call keys a recording was started from
        self._return_to = 0        # the window that had focus when we appeared
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        """Begin watching, if the setting allows and the platform can."""
        if not getattr(config, "MEETING_PROMPT", True):
            return False
        ok, why = meeting_detect.supported()
        if not ok:
            print(f"[meeting] {why}", flush=True)
            return False
        if self.window is None:
            self._create()
        self.watcher = meeting_detect.Watcher(
            on_start=self._call_started, on_end=self._call_ended,
            ignore=meeting_detect.parse_ignore(
                getattr(config, "MEETING_PROMPT_IGNORE", "")))
        if not self.watcher.start():
            print(f"[meeting] {self.watcher.error}", flush=True)
            return False
        return True

    def stop(self):
        """The app is closing. The hidden window has to go too: pywebview only
        exits when *every* window is closed, and one nobody can see would keep
        the process alive forever."""
        if self.watcher is not None:
            self.watcher.stop()
        window, self.window = self.window, None
        self._mode = None
        if window is not None:
            threading.Thread(target=lambda: _quiet(window.destroy),
                             daemon=True).start()

    def settings_changed(self):
        """Settings moved. Restart the watcher so the new answer holds now;
        the window stays, hidden, for the next time."""
        if self.watcher is not None:
            self.watcher.stop()
        self.watcher = None
        self._hide()
        self.start()                  # makes a window only if there is none

    # -- detector events (watcher thread) ------------------------------------

    def _recording(self):
        eng = self.api._engine
        return bool(eng is not None and eng.is_listening)

    def _call_started(self, call):
        # Already recording -- the hotkey got there first, or a second app
        # joined the same meeting. Nothing to offer.
        if self._recording():
            return
        with self._lock:
            self._call = call
            self._show("offer")

    def _call_ended(self, call):
        with self._lock:
            if self._call is not None and self._call.key == call.key \
                    and self._mode == "offer":
                self._hide()              # the offer outlived its call
            if not self._recording():
                return
            if not getattr(config, "MEETING_AUTO_STOP", True):
                return
            # Only a call that could be what is being recorded. Any detected
            # call counts, not just ones started from the offer: a recording
            # begun with the record key during a Zoom call ends with it too.
            self._call = call
            self._show("ended")

    # -- the window ----------------------------------------------------------

    def page_state(self):
        call = self._call
        # The chord is named only if a front end actually registered it. This
        # window does not (the tray and the classic window do), and an offer
        # that advertises a key which does nothing is worse than silence.
        chord = ""
        key = getattr(self.api, "_hotkey", None)
        if key is not None and not getattr(key, "error", "")                 and getattr(config, "HOTKEY_ENABLED", True):
            import hotkey
            chord = hotkey.pretty(getattr(config, "HOTKEY", ""))
        return {
            "mode": self._mode,
            "app": call.app if call else "",
            "label": call.label if call else "",
            "kind": call.kind if call else "",
            "service": call.service if call else "",
            "chord": chord,
            "appearance": getattr(config, "APPEARANCE", "system"),
            "offer_seconds": OFFER_SECONDS,
            "save_seconds": SAVE_SECONDS,
            "auto_stop": bool(getattr(config, "MEETING_AUTO_STOP", True)),
        }

    def _create(self):
        import webview

        url = pathlib.Path(PAGE).resolve().as_uri()
        self.window = webview.create_window(
            "Vlocalhost — call",
            url=url,
            js_api=PromptApi(self),
            width=WIDTH, height=HEIGHT,
            resizable=False, frameless=True, easy_drag=False,
            on_top=True, focus=False, shadow=True, hidden=True,
            background_color="#17191F",
        )
        self.window.events.shown += self._dress

    def _hwnd(self):
        try:
            return self.window.native.Handle.ToInt32()
        except Exception:                                  # noqa: BLE001
            return 0

    def _dress(self):
        """Native finishing the page cannot do: round the corners (Windows
        11 draws them; 10 ignores the request and stays square), and keep the
        window out of the taskbar and Alt-Tab -- it is a notification, not an
        application window."""
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            import ctypes

            round_ = ctypes.c_int(2)                     # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(round_), 4)
            user32 = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        except Exception as e:                             # noqa: BLE001
            print(f"[meeting] dress: {e}", flush=True)

    def _show(self, mode):
        """Say the new words, then appear -- in that order, so the window never
        shows the last offer's text for a frame."""
        self._mode = mode
        window, hwnd = self.window, self._hwnd()
        if window is None or not hwnd:
            return
        import json
        detail = json.dumps({"type": "state", "payload": self.page_state()})
        _quiet(window.evaluate_js,
               f"window.dispatchEvent(new CustomEvent('vl',{{detail:{detail}}}))")

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        front = user32.GetForegroundWindow()
        if front and front != hwnd:
            self._return_to = front
        # Physical pixels on both sides: pywebview made this process
        # DPI-aware, so the work area and the window rect agree.
        area = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)
        try:
            scale = user32.GetDpiForWindow(hwnd) / 96.0
        except Exception:                                  # noqa: BLE001
            scale = 1.0
        # The size is set here, not trusted from create_window: pywebview sizes
        # the form *before* removing its frame, so a frameless window comes out
        # smaller by the border it no longer has -- 357x111 instead of 372x148.
        w, h, gap = int(WIDTH * scale), int(HEIGHT * scale), int(MARGIN * scale)
        HWND_TOPMOST = wintypes.HWND(-1)
        SWP_NOACTIVATE = 0x10
        user32.SetWindowPos(hwnd, HWND_TOPMOST, area.right - w - gap,
                            area.bottom - h - gap, w, h, SWP_NOACTIVATE)
        # ShowWindow, not SWP_SHOWWINDOW: only ShowWindow sends WM_SHOWWINDOW,
        # which is how WinForms -- and through it WebView2 -- learns the window
        # is visible. Without it the webview never paints and the offer is an
        # empty dark rectangle. SW_SHOWNOACTIVATE is the "never take focus".
        user32.ShowWindow(hwnd, 4)

    def _hide(self):
        """Go, and give the keyboard back.

        A click inside the page activates this window despite WS_EX_NOACTIVATE
        -- WebView2 takes focus for its own child window -- so after "Take
        notes" the call would have lost the keyboard. Measured: foreground was
        the call before the click and this window after it. So focus goes back
        to whatever had it when the offer appeared, before the window hides.
        """
        self._mode = None
        hwnd = self._hwnd()
        if hwnd:
            import ctypes
            user32 = ctypes.windll.user32
            back, self._return_to = self._return_to, 0
            if user32.GetForegroundWindow() == hwnd and back                     and user32.IsWindow(back):
                user32.SetForegroundWindow(back)
            user32.ShowWindow(hwnd, 0)                         # SW_HIDE
        window = self.window
        if window is not None:
            threading.Thread(target=lambda: _quiet(
                window.evaluate_js,
                "window.dispatchEvent(new CustomEvent('vl',"
                "{detail:{type:'hide',payload:{}}}))"), daemon=True).start()

    # -- buttons (bridge thread) ---------------------------------------------

    def take_notes(self):
        with self._lock:
            call = self._call
            if call is not None:
                self._from_offer.add(call.key)
            self._mode = "recording"
        # No title: the engine names the session from the calendar when there
        # is an event, which is a better name than "Zoom" could ever be.
        self.api.start()
        return {"ok": True}

    def not_now(self):
        with self._lock:
            if self._call is not None and self.watcher is not None:
                self.watcher.snooze(self._call.key)
            self._hide()
        return {"ok": True}

    def never(self):
        with self._lock:
            call = self._call
            if call is not None:
                keys = meeting_detect.parse_ignore(
                    getattr(config, "MEETING_PROMPT_IGNORE", ""))
                if call.key not in keys:
                    keys.append(call.key)
                settings.save(MEETING_PROMPT_IGNORE=", ".join(keys))
                if self.watcher is not None:
                    self.watcher.ignore.add(call.key)
            self._hide()
        return {"ok": True, "key": call.key if call else ""}

    def save_now(self):
        with self._lock:
            self._hide()
        if self._recording():
            self.api.stop()
            # The main window learns through the engine's state callback, but
            # the stop came from here, so say so there too.
            self.api.push("state", self.api._status_payload())
        return {"ok": True}

    def keep_recording(self):
        with self._lock:
            self._hide()
        return {"ok": True}

    def close(self):
        with self._lock:
            mode = self._mode
            self._hide()
        if mode == "offer":
            return self.not_now()
        return {"ok": True}


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception:                                      # noqa: BLE001
        pass
