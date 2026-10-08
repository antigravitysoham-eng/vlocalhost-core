"""Notice a call starting, so the app can offer to take notes.

The hotkey solved the thirty seconds spent finding the window. This solves the
meeting nobody remembered to record at all. When another application opens the
microphone -- Zoom, Teams, a Meet tab -- the app offers, once, in a small
window that does not take focus from the call. One click records. Nothing is
recorded without that click.

**How a call is noticed, on Windows.** Windows keeps a per-app record of who
last opened the microphone, because the privacy indicator in the taskbar needs
one: ``HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone``. Each app
has ``LastUsedTimeStart`` and ``LastUsedTimeStop``, and a stop of zero means the
microphone is open *now*. Packaged apps (new Teams, WhatsApp) are subkeys by
package family; everything else sits under ``NonPackaged`` with the exe path's
backslashes written as ``#``. Reading it is a registry read -- no audio is
touched, no permission is asked, nothing leaves the machine.

It is exactly what the Windows privacy indicator shows. The other proven way
is enumerating WASAPI capture sessions (anarlog, the open-source note taker,
does that) -- equally good, but it is COM from Python for no extra signal.
Watching for zoom.exe is worse than either: it fires when Zoom opens, not when
a meeting does. The registry has one known trap -- an app that crashes holding
the microphone is left marked "in use" for good, new Teams most often -- so a
use only counts while the app's process is actually running.

**Why not every app, immediately.** Dictation tools open the microphone for
two seconds at a time all day. Known call apps are offered after a moment, a
browser after a little longer, and an app this module does not recognise only
once it has held the microphone long enough to be a conversation. Dictation
and other note takers are never offered. Each of these is a line in a table
below, not a heuristic.

**Windows only, and said out loud.** macOS reports the same thing through
CoreAudio (``kAudioDevicePropertyDeviceIsRunningSomewhere``), but it cannot say
*which* app, and nothing here has been run on a Mac. :func:`supported` returns
the reason rather than pretending.

This module decides nothing about the UI. :class:`Watcher` turns snapshots into
"a call started" and "that call ended", and the window decides what to show.
"""

import os
import platform
import re
import sys
import threading
import time
from dataclasses import dataclass

#: Where Windows records microphone use, per user.
_MIC_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
            r"\CapabilityAccessManager\ConsentStore\microphone")

#: What kind of thing is holding the microphone. The kind sets how long it has
#: to hold it before we ask -- see :data:`HOLD_SECONDS`.
CALL, BROWSER, OTHER = "call", "browser", "other"

#: Seconds of continuous use before the offer appears. Long enough that a
#: device test or a push-to-talk blip does not trip it; short enough that the
#: offer lands while people are still saying hello.
HOLD_SECONDS = {CALL: 3.0, BROWSER: 5.0, OTHER: 30.0}

#: After "Not now", the same app is not offered again for this long, even if it
#: lets go of the microphone and takes it back -- which Teams does on every
#: device switch, and a person who said no once should not be asked twice.
SNOOZE_SECONDS = 600.0

#: Seconds the microphone must stay released before a call counts as over.
#: Teams and Zoom briefly let go of the device when the user switches headset
#: mid-call; a shorter grace would end the meeting on every device change.
END_GRACE_SECONDS = 8.0

#: Known call apps: (lowercased exe name or package-family prefix, key, name).
#: ``key`` is what "Don't ask for this app" stores, so it must stay stable.
_CALL_APPS = (
    ("zoom.exe", "zoom", "Zoom"),
    ("cpthost.exe", "zoom", "Zoom"),
    ("zoom", "zoom", "Zoom"),                       # the Store package
    ("ms-teams.exe", "teams", "Microsoft Teams"),
    ("teams.exe", "teams", "Microsoft Teams"),
    ("msteams_", "teams", "Microsoft Teams"),
    ("slack.exe", "slack", "Slack"),
    ("91750d7e.slack_", "slack", "Slack"),
    ("webex.exe", "webex", "Webex"),
    ("ciscocollabhost.exe", "webex", "Webex"),
    ("atmgr.exe", "webex", "Webex"),
    ("discord.exe", "discord", "Discord"),
    ("skype.exe", "skype", "Skype"),
    ("microsoft.skypeapp_", "skype", "Skype"),
    ("5319275a.whatsappdesktop_", "whatsapp", "WhatsApp"),
    ("whatsapp.exe", "whatsapp", "WhatsApp"),
    ("gotomeeting.exe", "goto", "GoTo Meeting"),
    ("g2mcomm.exe", "goto", "GoTo Meeting"),
    ("ringcentral.exe", "ringcentral", "RingCentral"),
    ("bluejeans.exe", "bluejeans", "BlueJeans"),
    ("chime.exe", "chime", "Amazon Chime"),
    ("lark.exe", "lark", "Lark"),
    ("feishu.exe", "lark", "Lark"),
    ("telegram.exe", "telegram", "Telegram"),
    ("signal.exe", "signal", "Signal"),
    ("facetime", "facetime", "FaceTime"),
)

#: Browsers. The call inside one is named from the window title when it can be.
_BROWSERS = (
    ("chrome.exe", "chrome", "Chrome"),
    ("msedge.exe", "edge", "Edge"),
    ("firefox.exe", "firefox", "Firefox"),
    ("brave.exe", "brave", "Brave"),
    ("opera.exe", "opera", "Opera"),
    ("vivaldi.exe", "vivaldi", "Vivaldi"),
    ("arc.exe", "arc", "Arc"),
    ("thebrowsercompany.arc_", "arc", "Arc"),
)

#: Never offered. Dictation opens the microphone constantly and is not a
#: meeting; another note taker recording is somebody else's recording; and the
#: system's own voice features are not calls.
_NEVER = (
    "wispr flow.exe", "dragon.exe", "natspeak.exe", "voiceaccess",
    "soundrec", "microsoft.windowssoundrecorder_", "voicerecorder",
    "meetily.exe", "granola.exe", "otter", "fireflies", "krisp.exe",
    "microsoft.windows.cortana_", "microsoftwindows.client.cbs_",
    "nvidia broadcast.exe", "obs64.exe", "audacity.exe",
)

#: Window titles that name a call in a browser tab: (pattern, service name).
_CALL_TITLES = (
    (re.compile(r"^Meet\s*[-–—:]|Google Meet", re.I), "Google Meet"),
    (re.compile(r"Microsoft Teams", re.I), "Microsoft Teams"),
    (re.compile(r"Zoom (Meeting|Workplace)|zoom\.us", re.I), "Zoom"),
    (re.compile(r"\bWebex\b", re.I), "Webex"),
    (re.compile(r"\bWhereby\b", re.I), "Whereby"),
    (re.compile(r"Jitsi Meet", re.I), "Jitsi"),
    (re.compile(r"\bhuddle\b", re.I), "Slack huddle"),
    (re.compile(r"\bDiscord\b", re.I), "Discord"),
    (re.compile(r"\bGather\b", re.I), "Gather"),
)


@dataclass(frozen=True)
class MicUse:
    """One application holding the microphone right now."""

    key: str      #: stable id -- what "Don't ask for this app" stores
    app: str      #: the name a person would say: "Zoom", "Chrome"
    kind: str     #: CALL, BROWSER or OTHER
    source: str   #: the exe path or package family, for the log


@dataclass
class Call:
    """A use that has lasted long enough to be offered."""

    key: str
    app: str
    kind: str
    service: str = ""   #: "Google Meet", when a browser's title says so
    since: float = 0.0  #: monotonic time the microphone was first seen open

    @property
    def label(self):
        """How the offer names it: "Zoom", "Google Meet in Chrome"."""
        if self.service and self.service != self.app:
            return f"{self.service} in {self.app}"
        return self.app


def supported():
    """``(True, "")``, or ``(False, why)`` in words a Settings row can show."""
    if platform.system() != "Windows":
        return False, ("Noticing calls is Windows-only in this release. The "
                       "record key and the Record button still work.")
    return True, ""


def classify(source):
    """``MicUse`` for an exe path or package family, or None to ignore it."""
    lowered = source.lower()
    base = os.path.basename(lowered.replace("#", "\\"))
    for needle in _NEVER:
        if needle in base or lowered.startswith(needle):
            return None
    for table, kind in ((_CALL_APPS, CALL), (_BROWSERS, BROWSER)):
        for needle, key, name in table:
            if base == needle or base.startswith(needle):
                return MicUse(key, name, kind, source)
    # Unknown. Name it after the exe, which is what the user sees in Task
    # Manager, and key it the same way so "Don't ask" holds across updates.
    stem = os.path.splitext(base)[0] or base
    stem = re.sub(r"_[a-z0-9]{13}$", "", stem)      # a package's publisher hash
    stem = stem.split(".")[-1] if "." in stem else stem
    name = stem.replace("-", " ").replace("_", " ").strip().title() or source
    return MicUse(re.sub(r"[^a-z0-9]+", "", stem.lower()) or "app", name,
                  OTHER, source)


def _long(path):
    """The long form of a path, lowercased for comparison.

    Windows records the exe path *as it was launched*, and a program started
    through an 8.3 name (``C:/Users/SOHAMM~1/...``) is recorded that way,
    while the process list may report either. Compared raw, a real call
    looked stale and was dropped -- found by testing, not by reasoning. Only
    a path with a ``~`` can be short, so only those pay for the system call.
    """
    if "~" not in path:
        return os.path.normcase(os.path.abspath(path))
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetLongPathNameW(path, buf, 1024):
            path = buf.value
    except Exception:                                     # noqa: BLE001
        pass
    return os.path.normcase(os.path.abspath(path))


def _own_paths():
    """Exe paths that are this app. Our own recording opens the microphone
    too, and offering to record the recording would be absurd."""
    folder = os.path.dirname(sys.executable)
    return {_long(os.path.join(folder, sibling))
            for sibling in (os.path.basename(sys.executable), "python.exe",
                            "pythonw.exe", "vlocalhost.exe")}


def _running_exes():
    """Lowercased exe paths of running processes, or None if unknowable.

    The registry is left with a stop time of zero when an app crashes holding
    the microphone, so a stale entry would read as a call forever. Checking the
    exe is actually running removes that. psutil is already in the bundle; if
    it is missing the check is skipped rather than failing every read.
    """
    try:
        import psutil
    except Exception:                                     # noqa: BLE001
        return None
    running = set()
    for proc in psutil.process_iter(["exe"]):
        exe = proc.info.get("exe")
        if exe:
            running.add(_long(exe))
    return running


def _package_running(family, running):
    """Is some process from this Store package running? A package runs from
    ``WindowsApps/<Name>_<Version>_<Arch>_<Res>_<PublisherId>/``, so its
    family name ``<Name>_<PublisherId>`` is enough to find it."""
    if "_" not in family:
        return True                             # not a family name; trust it
    name, pub = family.lower().rsplit("_", 1)
    marker = "\\windowsapps\\" + name + "_"
    return any(marker in exe and pub in exe for exe in running)


def mic_users_windows():
    """Every app with the microphone open right now, as ``MicUse``."""
    import winreg

    found = []
    own = _own_paths()
    running = None

    def open_now(key):
        try:
            start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
            stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
        except OSError:
            return False
        return bool(start) and not stop

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_KEY)
    except OSError:
        return found
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            name = winreg.EnumKey(root, i)
            if name == "NonPackaged":
                continue
            with winreg.OpenKey(root, name) as sub:
                if not open_now(sub):
                    continue
            use = classify(name)
            if not use:
                continue
            if running is None:
                running = _running_exes() or set()
            if running and not _package_running(name, running):
                continue                         # crashed holding it: stale
            found.append(use)
        try:
            loose = winreg.OpenKey(root, "NonPackaged")
        except OSError:
            return found
        with loose:
            for i in range(winreg.QueryInfoKey(loose)[0]):
                name = winreg.EnumKey(loose, i)
                with winreg.OpenKey(loose, name) as sub:
                    if not open_now(sub):
                        continue
                exe = _long(name.replace("#", "\\"))
                if exe in own:
                    continue
                if running is None:
                    running = _running_exes() or set()
                if running and exe not in running:
                    continue                     # crashed holding it: stale
                use = classify(name.replace("#", "\\"))
                if use:
                    found.append(use)
    return found


def window_titles_windows():
    """Titles of visible top-level windows. Used only to name a browser call,
    and read on this machine only -- nothing is stored or logged."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(each, 0)
    return titles


def service_from_titles(titles):
    """The call service a window title names, or ``""``."""
    for title in titles:
        for pattern, name in _CALL_TITLES:
            if pattern.search(title):
                return name
    return ""


class Watcher:
    """Snapshots in, "call started" and "call ended" out.

    The decisions are all in :meth:`tick`, which takes the time and the list of
    apps holding the microphone and returns what changed. That is the part
    worth testing, and it can be tested with no registry, no clock and no
    thread. :meth:`start` adds the thread.
    """

    def __init__(self, on_start=None, on_end=None, ignore=(),
                 probe=None, titles=None, interval=1.5):
        self.on_start = on_start or (lambda call: None)
        self.on_end = on_end or (lambda call: None)
        self.ignore = {k.strip().lower() for k in ignore if k and k.strip()}
        self.probe = probe or mic_users_windows
        self.titles = titles or window_titles_windows
        self.interval = interval
        self.error = ""
        self._seen = {}      # key -> (MicUse, first seen)
        self._gone = {}      # key -> first moment it was missing
        self._live = {}      # key -> Call, offered and not yet ended
        self._snoozed = {}   # key -> not offered again before this moment
        self._now = 0.0
        self._stop = threading.Event()
        self._thread = None

    # -- the decisions -------------------------------------------------------

    def tick(self, now, uses):
        """Advance to ``now`` given the apps holding the mic. Returns a list of
        ``("start" | "end", Call)``."""
        self._now = now
        events = []
        present = {}
        for use in uses:
            present.setdefault(use.key, use)

        for key, use in present.items():
            self._gone.pop(key, None)
            if key not in self._seen:
                self._seen[key] = (use, now)
            if key in self._live or key in self.ignore:
                continue
            if now < self._snoozed.get(key, 0.0):
                continue
            first = self._seen[key][1]
            if now - first >= HOLD_SECONDS[use.kind]:
                call = Call(use.key, use.app, use.kind, since=first)
                if use.kind == BROWSER:
                    try:
                        call.service = service_from_titles(self.titles())
                    except Exception:                     # noqa: BLE001
                        call.service = ""
                self._live[key] = call
                events.append(("start", call))

        for key in list(self._seen):
            if key in present:
                continue
            gone_at = self._gone.setdefault(key, now)
            if now - gone_at < END_GRACE_SECONDS:
                continue
            self._seen.pop(key, None)
            self._gone.pop(key, None)
            call = self._live.pop(key, None)
            if call is not None:
                events.append(("end", call))
        return events

    def snooze(self, key, seconds=SNOOZE_SECONDS):
        """"Not now": let this call go, and do not offer this app again soon.

        The call stays tracked until it ends, so "call ended" still arrives --
        a recording started later from the record key is still offered a stop.
        """
        self._snoozed[key] = self._now + seconds

    def live(self):
        """Calls offered and still going, oldest first."""
        return sorted(self._live.values(), key=lambda c: c.since)

    # -- the thread ----------------------------------------------------------

    def start(self):
        """Begin watching. False, with :attr:`error` set, if it cannot."""
        ok, why = supported()
        if not ok:
            self.error = why
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="meeting-watch")
        self._thread.start()
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                events = self.tick(time.monotonic(), self.probe())
            except Exception as e:                        # noqa: BLE001
                # A failed read is not a reason to stop watching, and a
                # traceback every 1.5s would bury the log.
                if not self.error:
                    print(f"[meeting] {e}", flush=True)
                self.error = str(e)
                events = []
            for kind, call in events:
                try:
                    (self.on_start if kind == "start" else self.on_end)(call)
                except Exception as e:                    # noqa: BLE001
                    print(f"[meeting] handler failed: {e}", flush=True)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


def parse_ignore(value):
    """The saved "don't ask" list, as keys. Stored as a comma-separated string
    so it survives settings.json and a text field alike."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [i.strip().lower() for i in items if i and i.strip()]


if __name__ == "__main__":
    # A probe for a human: open a call and watch it appear.
    ok, why = supported()
    if not ok:
        sys.exit(why)
    for use in mic_users_windows():
        print(f"{use.kind:8} {use.key:12} {use.app:20} {use.source}")
    print("titles name:", service_from_titles(window_titles_windows()) or "-")
