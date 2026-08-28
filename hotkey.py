"""A system-wide key that starts and stops a recording.

The point is the meeting you are already in. Reaching for the window, finding
it behind the call, and clicking Record costs the first thirty seconds of the
conversation -- which is where people say what the meeting is *about*. One key
that works while another app has focus is the difference between a transcript
that starts at the beginning and one that starts once you remembered.

**Why a chord and not just Ctrl+Shift.** Modifiers alone cannot be registered:
``RegisterHotKey`` takes a modifier mask *and* a virtual-key code, and rejects
a mask on its own. That is the API, not a preference. It is also the right
answer -- Ctrl+Shift is the opening of every Ctrl+Shift+X shortcut in every
application, so a bare pair would fire on Ctrl+Shift+T, Ctrl+Shift+Esc and the
rest, and Windows itself offers Ctrl+Shift as an input-switching chord.

**Why a function key by default.** A global hotkey outranks every application,
so whatever we take, nobody else can have. Ctrl+Shift+R would remove
hard-reload from every browser; Ctrl+Shift+M would take Teams' mute, which in a
meeting-notes app is close to malicious. F12 with both modifiers is bound by
almost nothing, so the default costs the user nothing they had.

**Windows only, and that is said out loud rather than hidden.** macOS needs an
Accessibility grant the app would have to talk the user through, and on Linux
the answer depends on the desktop. Both return a reason from :func:`start`
instead of failing silently, so the UI can explain itself. The cold-start half
of this feature -- pressing the key when the app is not running -- is a
shortcut hotkey instead, and lives in :mod:`shortcut`.
"""

import platform
import threading

#: ``RegisterHotKey`` modifier bits.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
#: Without this, holding the chord repeats at the keyboard's repeat rate and a
#: toggle would start and stop a recording many times a second.
MOD_NOREPEAT = 0x4000

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}

#: Virtual-key codes for the names a chord may use. Letters and digits are
#: their ASCII values, so only the ones that are not go here.
_KEYS = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
         "esc": 0x1B, "escape": 0x1B, "insert": 0x2D, "delete": 0x2E,
         "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
         "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
         "backspace": 0x08, "pause": 0x13, "printscreen": 0x2C}
for _n in range(1, 25):                      # F1..F24 are contiguous from 0x70
    _KEYS[f"f{_n}"] = 0x6F + _n

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
_HOTKEY_ID = 0xB01D                          # arbitrary, ours, per-thread


class ChordError(ValueError):
    """The chord could not be understood, with a reason a user can act on."""


def parse(chord: str) -> tuple:
    """``"ctrl+shift+f12"`` -> ``(mods, vk)``. Raises :class:`ChordError`.

    Deliberately strict about the one mistake worth catching: a chord of
    nothing but modifiers. That is the request people actually make, it looks
    entirely reasonable written down, and it cannot work -- so it earns a real
    explanation rather than a shrug.
    """
    parts = [p.strip().lower() for p in (chord or "").split("+") if p.strip()]
    if not parts:
        raise ChordError("No hotkey set.")

    mods, key = 0, None
    for part in parts:
        if part in _MODS:
            mods |= _MODS[part]
            continue
        if key is not None:
            raise ChordError(
                f"“{chord}” names two keys ({key} and {part}). A hotkey is any "
                f"number of modifiers and exactly one other key.")
        key = part

    if key is None:
        raise ChordError(
            f"“{chord}” is only modifiers. Windows needs a real key as well — "
            f"try {chord}+F12.")
    if not mods:
        raise ChordError(
            f"“{chord}” has no modifier. A bare key would fire while you were "
            f"typing; add Ctrl and Shift.")

    if key in _KEYS:
        vk = _KEYS[key]
    elif len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    else:
        raise ChordError(f"“{key}” is not a key this can register.")
    return mods | MOD_NOREPEAT, vk


def pretty(chord: str) -> str:
    """The chord as a human writes it: ``Ctrl+Shift+F12``."""
    nice = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt",
            "win": "Win", "super": "Super", "cmd": "Cmd"}
    out = []
    for part in (chord or "").split("+"):
        p = part.strip().lower()
        if not p:
            continue
        out.append(nice.get(p, p.upper() if len(p) <= 3 else p.capitalize()))
    return "+".join(out)


class Hotkey:
    """A registered chord, and the thread that listens for it.

    ``RegisterHotKey`` binds to the *calling thread* and its messages arrive in
    that thread's queue, so the registration and the message loop have to be
    the same thread. That is the whole reason this is a class with a thread in
    it rather than a function.

    ``on_press`` is called on that thread. It must not touch the UI directly --
    every caller here hands the work to its own event loop, the same way the
    audio callbacks already do.
    """

    def __init__(self, chord: str, on_press):
        self.chord = chord
        self.on_press = on_press
        self.error = ""
        self._thread = None
        self._tid = None
        self._ready = threading.Event()

    def start(self) -> bool:
        """Register and begin listening. False if it could not be done, with
        :attr:`error` saying why."""
        if platform.system() != "Windows":
            self.error = ("A system-wide hotkey is Windows-only in this "
                          "release. Recording still starts from the window "
                          "and from the tray.")
            return False
        try:
            parse(self.chord)
        except ChordError as e:
            self.error = str(e)
            return False

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="hotkey")
        self._thread.start()
        # Wait for the registration to succeed or fail, so a caller can report
        # "that chord is already taken" now rather than discovering it never
        # works. Bounded, because a hung registration must not hold up startup.
        self._ready.wait(timeout=3.0)
        return not self.error

    def _run(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        mods, vk = parse(self.chord)
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk):
            # The overwhelmingly likely cause is another application holding
            # the same chord. Windows does not say which, and there is no way
            # to ask, so the message says what the user can actually do.
            self.error = (f"{pretty(self.chord)} is already used by another "
                          f"application. Choose a different key in Settings.")
            self._ready.set()
            return
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception as e:  # noqa: BLE001 - never kill the loop
                        print(f"[hotkey] handler failed: {e}", flush=True)
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)

    def stop(self):
        """Unregister and let the thread end. Safe to call more than once."""
        if not self._tid:
            return
        try:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        except Exception:  # noqa: BLE001 - shutting down anyway
            pass
        self._tid = None


def start(chord: str, on_press):
    """Register ``chord``. Returns the :class:`Hotkey`, started or not.

    Never raises. A hotkey that cannot be registered is a smaller problem than
    an app that will not open, and the caller reads ``.error`` to say so.
    """
    key = Hotkey(chord, on_press)
    try:
        key.start()
    except Exception as e:  # noqa: BLE001
        key.error = str(e)
    return key
