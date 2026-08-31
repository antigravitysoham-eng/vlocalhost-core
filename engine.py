"""The shared session engine.

One object owns a recording session: the mic pipeline, the connected
calendar/email provider, and the meeting currently being recorded. The GUI, the
tray, and the MCP server all drive this same object, so they can never disagree
about what is happening — and only one of them can hold the microphone.

    engine = AppEngine(on_line=print)
    engine.start()            # blocks while the model loads
    ...
    result = engine.stop_and_save()
"""

import json
import os
import platform
import threading
import time
from datetime import datetime, timedelta

import config
import settings
from integrations import store
from notetaker import NoteTaker

_LOCK_FILE = "recording.lock"


def notes_dir() -> str:
    """Absolute path of the folder transcripts and notes are written to."""
    return store.notes_dir()


# --- One microphone, one recorder ----------------------------------------
# The window, the tray, and the MCP server are separate processes that can all
# record. Only one may hold the mic, so whoever starts first takes a lock file
# and the others report who has it instead of fighting over the device.

def _pid_alive(pid: int) -> bool:
    """True if a process with this id is still running (no third-party deps)."""
    if pid == os.getpid():
        return True
    if platform.system() == "Windows":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def active_recorder():
    """Details of the process currently recording, or None. Clears stale locks."""
    path = store.path_for(_LOCK_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid == os.getpid() or _pid_alive(pid):
        return info
    # The owner died without cleaning up (crash, kill) — the lock is stale.
    try:
        os.remove(path)
    except OSError:
        pass
    return None


def _take_lock(front_end: str):
    holder = active_recorder()
    if holder and int(holder["pid"]) != os.getpid():
        raise RuntimeError(
            f"Vlocalhost is already recording in another window "
            f"({holder.get('front_end', 'unknown')}, pid {holder['pid']}). "
            "Stop that session first — only one can use the microphone.")
    with open(store.path_for(_LOCK_FILE), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "front_end": front_end,
                   "since": datetime.now().isoformat(timespec="seconds")}, f)


def _release_lock():
    holder = active_recorder()
    if holder and int(holder["pid"]) == os.getpid():
        try:
            os.remove(store.path_for(_LOCK_FILE))
        except OSError:
            pass


def check_ollama():
    """(ok, detail) for the local summarization model — never raises."""
    import requests

    try:
        resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=4)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception as e:  # noqa: BLE001 - a status check must not throw
        return False, f"not reachable at {config.OLLAMA_URL} ({e.__class__.__name__})"

    want = config.OLLAMA_MODEL
    # Ollama reports "llama3.2:latest" for a model pulled as "llama3.2".
    if any(n == want or n.split(":")[0] == want.split(":")[0] for n in names):
        return True, f"{want} ready"
    if names:
        return False, f"running, but {want} isn't pulled (ollama pull {want})"
    return False, f"running, but no models pulled (ollama pull {want})"


def _named(title):
    """A stand-in event that only carries a name, so a manually started session
    can still be titled (by the GUI or an MCP client) instead of being named by
    the model. Has no id or attendees, so nothing is emailed or posted back."""
    if not title or not title.strip():
        return None
    from integrations import Event

    now = datetime.now().astimezone()
    return Event(id="", title=title.strip(), start=now, end=now)


class AppEngine:
    """Recording session + integrations, shared by every front end."""

    def __init__(self, on_line=None, on_state=None, front_end="app",
                 on_partial=None):
        """on_line(text)  — a newly transcribed line (background thread).
        on_state(status) — called whenever recording starts/stops.
        on_partial(text, label) — provisional words while somebody is still
        talking, for a front end that can show and then replace them. Leave it
        unset (the terminal and MCP do) and no provisional decoding happens at
        all.
        front_end        — which UI this is, named in the 'already recording'
        message another process sees."""
        self._on_line = on_line or (lambda text: None)
        self._on_state = on_state or (lambda status: None)
        self._on_partial = on_partial
        self.front_end = front_end

        self._provider = None
        self._provider_error = ""
        self._provider_loaded = False
        self._notetaker = None
        self._scheduler = None
        self._lock = threading.Lock()

        self.event = None          # the meeting being recorded, if known
        self.started_at = None     # float epoch seconds
        self.last_result = None    # the most recent stop_and_save() result

    # -- lazy pieces ---------------------------------------------------------
    @property
    def notetaker(self) -> NoteTaker:
        """Built on first use — constructing it is cheap, the model loads later."""
        if self._notetaker is None:
            self._notetaker = NoteTaker(on_line=self._on_line,
                                        on_partial=self._on_partial,
                                        provider=self.provider)
        return self._notetaker

    @property
    def provider(self):
        """The connected calendar/email provider, or None. Never raises."""
        if not self._provider_loaded:
            self._provider_loaded = True
            self._provider, self._provider_error = self._build_provider()
        return self._provider

    @staticmethod
    def _build_provider():
        """(provider, error). A provider that is configured but not signed in
        yet counts as an error — the app still runs, just without delivery."""
        from integrations import get_provider

        try:
            provider = get_provider(config.CALENDAR_PROVIDER)
        except Exception as e:  # noqa: BLE001 - bad name or missing packages
            return None, str(e)
        if provider is None:
            return None, ""
        try:
            provider.authenticate(interactive=False)
        except Exception as e:  # noqa: BLE001 - not signed in yet
            return None, str(e)
        return provider, ""

    def reload_provider(self):
        """Re-read the provider setting (after the user connects or switches)."""
        self._provider_loaded = False
        self._provider = None
        self._provider_error = ""
        provider = self.provider
        if self._notetaker is not None:
            self._notetaker.provider = provider
        return provider

    @property
    def provider_error(self) -> str:
        self.provider  # force the lazy build so the error is populated
        return self._provider_error

    # -- recording -----------------------------------------------------------
    def current_event(self):
        """The meeting happening right now per the calendar, or None."""
        provider = self.provider
        if provider is None:
            return None
        try:
            return provider.current_event(datetime.now().astimezone())
        except Exception:  # noqa: BLE001 - calendar trouble must not block recording
            return None

    def start(self, event=None, title=None):
        """Load the model and start listening. Blocks (model load can take a
        few seconds) — call it on a worker thread from a UI.

        ``title`` names the session when there's no calendar event to name it.
        Raises RuntimeError if another Vlocalhost process holds the microphone.
        """
        with self._lock:
            if self.is_listening:
                return self.status()
            _take_lock(self.front_end)
            self.event = event or self.current_event() or _named(title)
            self.started_at = time.time()
        try:
            self.notetaker.start()
        except Exception:
            # The mic or model failed — don't leave a lock nobody owns.
            _release_lock()
            self.event = None
            self.started_at = None
            raise
        status = self.status()
        self._on_state(status)
        return status

    def stop_and_save(self):
        """Stop listening, write the files, and deliver per settings.

        Returns a result dict: title, transcript, summary, notes, delivered,
        error. ``error`` is set when there was nothing to save or the summary
        step failed; the transcript is still written in the latter case.
        """
        with self._lock:
            if not self.is_listening:
                return {"error": "Not recording.", "title": None,
                        "transcript": None, "summary": None, "notes": None,
                        "delivered": ""}
            event = self.event

        nt = self.notetaker
        nt.stop()
        _release_lock()
        paths, err = nt.save(event=event)
        delivered = nt.deliver(event, paths) if paths else ""

        self.event = None
        self.started_at = None
        self.release_model()
        result = {
            "title": (paths or {}).get("title"),
            "transcript": (paths or {}).get("transcript"),
            "summary": (paths or {}).get("summary"),
            "notes": (paths or {}).get("notes"),
            "delivered": delivered,
            "error": err if paths else (err or "Nothing was transcribed."),
        }
        self.last_result = result
        self._on_state(self.status())
        return result

    def release_model(self):
        """Hand the speech model's memory back while idle (see
        ``config.RELEASE_MODEL_WHEN_IDLE``). Never unloads mid-recording."""
        if not getattr(config, "RELEASE_MODEL_WHEN_IDLE", True):
            return False
        if self.is_listening or self._notetaker is None:
            return False
        transcriber = self._notetaker.transcriber
        return bool(getattr(transcriber, "unload", lambda: False)())

    def model_ready(self) -> bool:
        """True if the speech model is already resident.

        The window asks so it can say "transcribing shortly" instead of showing
        an empty transcript that looks like a failure. False is not an error --
        it just means the model is still building while audio is recorded.
        """
        if self._notetaker is None:
            return False
        return getattr(self._notetaker.transcriber, "_model", True) is not None

    @property
    def is_listening(self) -> bool:
        return self._notetaker is not None and self._notetaker.listening

    @property
    def elapsed(self) -> int:
        """Seconds recorded so far (0 when idle)."""
        return int(time.time() - self.started_at) if self.started_at else 0

    def transcript(self) -> str:
        return self._notetaker.transcript_text() if self._notetaker else ""

    def status(self) -> dict:
        """A snapshot safe to show in a UI or hand to an MCP client."""
        transcript = self.transcript()
        provider = self.provider
        return {
            "listening": self.is_listening,
            "elapsed_seconds": self.elapsed,
            "meeting": self.event.title if self.event else None,
            "lines": len([ln for ln in transcript.splitlines() if ln.strip()]),
            "provider": config.CALENDAR_PROVIDER,
            "provider_connected": provider is not None,
            "provider_error": self._provider_error,
            "whisper_model": config.WHISPER_MODEL,
            "ollama_model": config.OLLAMA_MODEL,
            "notes_dir": notes_dir(),
        }

    # -- calendar ------------------------------------------------------------
    def upcoming_events(self, hours: int = 12):
        """Meetings between now and ``hours`` from now. [] if not connected."""
        provider = self.provider
        if provider is None:
            return []
        now = datetime.now().astimezone()
        try:
            return provider.list_events(now, now + timedelta(hours=hours))
        except Exception:  # noqa: BLE001
            return []

    def start_scheduler(self, on_message=None):
        """Watch the calendar and record meetings automatically."""
        if self._scheduler is not None or self.provider is None:
            return False
        from scheduler import CalendarScheduler

        say = on_message or (lambda m: print(f"[calendar] {m}", flush=True))

        def _begin(ev):
            say(f"meeting started: {ev.title}")
            self.start(ev)

        def _end(ev):
            say(f"meeting ended: {ev.title}")
            self.stop_and_save()

        self._scheduler = CalendarScheduler(self.provider, _begin, _end,
                                            on_error=say)
        self._scheduler.start()
        return True

    def stop_scheduler(self):
        if self._scheduler is not None:
            self._scheduler.stop()
            self._scheduler = None

    # -- saved notes ---------------------------------------------------------
    def list_notes(self, limit: int = 20):
        """Saved meetings, newest first: name, files, size, modified time."""
        directory = notes_dir()
        if not os.path.isdir(directory):
            return []
        items = []
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            items.append({
                "name": name,
                "path": path,
                # "-notes.md" is the pre-1.2.0 name for a summary. Files
                # already on disk keep working -- renaming the output is
                # not a reason to stop recognising what a user saved last
                # month.
                "kind": ("notes"
                         if name.endswith(("-summary.txt", "-summary.md",
                                           "-notes.md"))
                         else "transcript"),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds"),
                "bytes": stat.st_size,
            })
        items.sort(key=lambda i: i["modified"], reverse=True)
        return items[:limit] if limit else items

    def read_note(self, name: str) -> str:
        """Contents of one saved file. ``name`` must be inside the notes dir."""
        directory = os.path.realpath(notes_dir())
        path = os.path.realpath(os.path.join(directory, name))
        # Never let a crafted name (../../secrets) escape the notes folder.
        if os.path.commonpath([directory, path]) != directory:
            raise ValueError("Note name must be a file inside the notes folder.")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"No such note: {name}")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def search_notes(self, query: str, limit: int = 20):
        """Case-insensitive search across saved notes and transcripts."""
        needle = query.lower().strip()
        if not needle:
            return []
        hits = []
        for item in self.list_notes(limit=0):
            try:
                text = self.read_note(item["name"])
            except (OSError, ValueError):
                continue
            lowered = text.lower()
            if needle not in lowered:
                continue
            # A little context around the first match, for a useful preview.
            at = lowered.index(needle)
            start, end = max(0, at - 120), min(len(text), at + 200)
            hits.append({**item, "excerpt": text[start:end].replace("\n", " ")})
            if len(hits) >= limit:
                break
        return hits

    # -- shutdown ------------------------------------------------------------
    def shutdown(self):
        """Stop everything, saving an in-progress session rather than losing it."""
        self.stop_scheduler()
        result = None
        if self.is_listening:
            result = self.stop_and_save()
        elif self._notetaker is not None and self._notetaker.has_unsaved():
            paths, err = self._notetaker.save(event=self.event)
            delivered = self._notetaker.deliver(self.event, paths) if paths else ""
            result = {"title": (paths or {}).get("title"), "error": err,
                      "delivered": delivered,
                      "transcript": (paths or {}).get("transcript"),
                      "summary": (paths or {}).get("summary"),
                      "notes": (paths or {}).get("notes")}
        return result


def build(on_line=None, on_state=None, on_partial=None) -> AppEngine:
    """Apply saved user settings, then return a ready engine."""
    settings.apply()
    return AppEngine(on_line=on_line, on_state=on_state, on_partial=on_partial)
