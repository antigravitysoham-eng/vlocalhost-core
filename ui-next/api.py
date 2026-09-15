"""Everything the window may ask this machine to do.

One class, one instance, handed to pywebview as ``js_api``. Every public method
here is reachable from the page as ``window.pywebview.api.<name>(...)``, and
every argument and return value crosses as JSON — so nothing here may hand back
an object the page cannot read. Dataclasses get flattened on the way out; paths
go as strings.

The other direction is :meth:`push`. The engine's callbacks arrive on worker
threads — a transcribed line from the mic pipeline, a notes job from the
summariser — and are forwarded into the page as one ``vl`` CustomEvent carrying
a ``type``. One channel rather than a global function per event, because the
page is a single listener either way and the alternative is a list of window
globals that all have to exist before the first event can fire.

**Nothing in this file talks to the network, and nothing in it decides policy.**
It is a translation layer: the rules about what may be recorded, summarised or
sent live in Core, and this asks Core rather than reimplementing any of it.
"""

import os
import platform
import re
import subprocess
import threading

import audio_listener
import config
import diagnostics
import engine as engine_mod
import mcp_hosts
import notes_view
import performance as perf
import settings
import updates


class Api:
    """The page's view of this machine."""

    def __init__(self):
        self._window = None
        self._engine = None
        self._lock = threading.Lock()
        #: Lines transcribed during the session currently on screen. The engine
        #: keeps the authoritative transcript; this is only what the page has
        #: already been told about, so a reload can be replayed into it.
        self._lines = []

    # -- wiring --------------------------------------------------------------

    def attach(self, window):
        """Called once by the shell, after the window exists."""
        self._window = window

    @property
    def engine(self):
        """Built on first use.

        ``launching.md`` wants the window up immediately, and constructing an
        AppEngine reaches for the notes directory and the calendar provider.
        Neither is needed to draw a screen, so neither happens until something
        actually asks.
        """
        with self._lock:
            if self._engine is None:
                self._engine = engine_mod.AppEngine(
                    on_line=self._line,
                    on_state=self._state,
                    on_partial=self._partial,
                    on_level=self._level,
                    front_end="window",
                )
                self._engine.on_notes(self._notes)
            return self._engine

    def push(self, kind, payload=None):
        """Send one event to the page.

        Called from whichever thread the engine happens to be on. The window
        may be closing, and evaluate_js on a dead window raises — a recording
        being torn down is not a reason to print a traceback, so it is
        swallowed deliberately rather than by accident.
        """
        if self._window is None:
            return
        import json
        detail = json.dumps({"type": kind, "payload": payload or {}})
        try:
            self._window.evaluate_js(
                "window.dispatchEvent(new CustomEvent('vl',"
                f"{{detail:{detail}}}))")
        except Exception:
            pass

    # -- engine callbacks ----------------------------------------------------

    def _line(self, text):
        self._lines.append(text)
        self.push("line", {"text": text})

    def _partial(self, text, label=""):
        self.push("partial", {"text": text, "who": label})

    def _level(self, dbfs, speech, label=""):
        """Input level, about twelve times a second, per source.

        The throttle lives in the segmenter rather than here — see
        ``audio_listener._Segmenter.on_level``. This only forwards, so the
        capture thread's share of drawing a meter is one dict and one
        evaluate_js.
        """
        self.push("level", {"db": round(dbfs, 1), "speech": speech,
                            "who": label})

    def _state(self, status):
        self.push("state", self._status_payload(status))

    def _notes(self, job, state, result):
        """A deferred summary changed state.

        ``result`` carries only the transcript while the notes are still being
        written, and is None on "running" — so the page is told the state and
        whatever exists, and asks for the rest when there is any.
        """
        payload = {"job": job, "state": state}
        if isinstance(result, dict):
            payload["title"] = result.get("title")
            payload["error"] = result.get("error")
            for key in ("transcript", "summary", "notes"):
                path = result.get(key)
                payload[key] = os.path.basename(path) if path else None
        payload["pending"] = self.engine.pending_notes()
        self.push("notes", payload)

    # -- status --------------------------------------------------------------

    def _status_payload(self, status=None):
        eng = self._engine
        if eng is None:
            # Same keys, so the page never has to ask which shape it got. The
            # engine is not built until something needs it, and drawing a
            # screen does not.
            return {"listening": False, "elapsed": 0, "pending": 0,
                    "title": None, "lines": 0,
                    "notes_dir": engine_mod.notes_dir(),
                    "whisper_model": getattr(config, "WHISPER_MODEL", ""),
                    "notes_model": getattr(config, "OLLAMA_MODEL", ""),
                    "model_ready": False,
                    "sealed": bool(getattr(config, "SEALED_MODE", False))}
        status = status or eng.status()
        return {
            "listening": bool(status.get("listening")),
            "elapsed": int(status.get("elapsed_seconds") or 0),
            "title": status.get("meeting"),
            "lines": int(status.get("lines") or 0),
            "pending": eng.pending_notes(),
            "notes_dir": status.get("notes_dir"),
            "whisper_model": status.get("whisper_model"),
            "notes_model": status.get("ollama_model"),
            # A method on AppEngine, not a property -- unlike is_listening
            # and elapsed just above it. Without the call this returns a
            # bound method, and pywebview fails to serialise the reply.
            "model_ready": eng.model_ready(),
            "sealed": bool(getattr(config, "SEALED_MODE", False)),
        }

    def status(self):
        """Cheap enough for the page to call on every screen change."""
        return self._status_payload()

    # -- recording -----------------------------------------------------------

    def start(self, title=None):
        """Begin a recording.

        :meth:`AppEngine.start` blocks while the speech model loads — seconds
        on a cold start, and on this thread that is a frozen window. So it runs
        on its own thread and the page is told twice: once that the attempt
        began, and again when the microphone is actually open.
        ``loading.md``: something appears immediately, and people can keep
        working.
        """
        def run():
            try:
                self.engine.start(title=title or None)
            except Exception as e:
                self.push("error", {"where": "start", "message": str(e)})
                return
            self.push("state", self._status_payload())

        self._lines = []
        threading.Thread(target=run, daemon=True, name="vl-start").start()
        return {"starting": True}

    def stop(self):
        """Stop, save the words, and let the summary be written behind us.

        ``defer_notes=True`` is the whole reason the notes column exists: the
        transcript is on disk before this returns, and the summary arrives
        later as a "notes" event. The next meeting can start immediately.
        """
        result = self.engine.stop_and_save(defer_notes=True)
        return {
            "error": result.get("error"),
            "title": result.get("title"),
            "job": result.get("job"),
            "pending": bool(result.get("pending")),
            "recorded": result.get("recorded"),
            "transcript": (os.path.basename(result["transcript"])
                           if result.get("transcript") else None),
        }

    def transcript(self):
        """The session's words as the engine holds them, not as the page saw them."""
        if self._engine is None:
            return {"text": ""}
        return {"text": self._engine.transcript()}

    # -- the library ---------------------------------------------------------

    def library(self, limit=200):
        """Saved meetings, newest first — one row per meeting, not per file.

        ``list_notes`` reports files, and a meeting is two of them:
        ``<base>-transcript.txt`` and ``<base>-summary.txt``. Grouping belongs
        here rather than in the page, because the page should never have to
        know a naming convention to count what it is showing.
        """
        rows = {}
        for item in self.engine.list_notes(limit=0):
            name = item["name"]
            base = name
            for suffix in ("-transcript.txt", "-summary.txt", "-summary.md",
                           "-notes.md"):
                if name.endswith(suffix):
                    base = name[: -len(suffix)]
                    break
            row = rows.setdefault(base, {
                "base": base, "title": _title_from(base),
                "transcript": None, "summary": None,
                "modified": item["modified"], "bytes": 0,
            })
            row[item["kind"] if item["kind"] == "notes" else "transcript"] = name
            if item["kind"] == "notes":
                row["summary"] = name
            row["bytes"] += item["bytes"]
            row["modified"] = max(row["modified"], item["modified"])

        out = sorted(rows.values(), key=lambda r: r["modified"], reverse=True)
        return out[:limit] if limit else out

    def meeting(self, base):
        """One meeting, parsed into the shape the page draws.

        The summariser writes four fixed sections and ``notes_view`` reads them
        back, so the window can show a summary and lists of decisions and owed
        actions instead of a wall of text.

        **No timestamps.** Core's prompt forbids clock times, so nothing here
        can carry the second a thing was said. The citation that Aurora is
        built around comes from a Context Pack, which is Pro's and is not
        extracted unless somebody asks for it — so a meeting without one shows
        the claim and omits the second, rather than printing a plausible time
        nobody said.
        """
        directory = engine_mod.notes_dir()
        summary = None
        for suffix in ("-summary.txt", "-summary.md", "-notes.md"):
            candidate = os.path.join(directory, base + suffix)
            if os.path.isfile(candidate):
                summary = candidate
                break

        title = _title_from(base)
        if summary is None:
            return {"base": base, "title": title, "summary": "",
                    "facts": [], "missing": True}

        notes = notes_view.read(summary, title=title)
        facts = []
        for item in notes.decisions:
            facts.append({"role": "Decided", "text": item.text})
        for item in notes.actions:
            facts.append({"role": "Owed", "text": item.text,
                          "who": item.owner, "due": item.due,
                          "done": item.done})
        for item in notes.points:
            facts.append({"role": "Discussed", "text": item.text})
        return {
            "base": base,
            "title": notes.title or title,
            "summary": notes.summary or notes.preamble,
            "facts": facts,
            "missing": False,
            # The page states this rather than quietly drawing fewer marks:
            # a design built on citations has to say when it has none.
            "cited": False,
        }

    # -- settings ------------------------------------------------------------

    def settings_get(self):
        """Every editable setting and its effective value."""
        values = settings.current()
        return {"values": {k: _jsonable(v) for k, v in values.items()},
                "editable": list(settings.EDITABLE),
                "path": settings.path()}

    def settings_set(self, changes):
        """Write one or more settings, and say what actually landed.

        Values arrive from JSON, so a number may be a number already or a
        string from a text field. ``settings.coerce`` only knows how to read a
        string, and it takes the expected type from whatever ``config`` holds
        now — so strings go through it and everything else is passed as is.
        """
        if not isinstance(changes, dict):
            return {"error": "changes must be an object"}
        clean = {}
        for key, value in changes.items():
            if key not in settings.EDITABLE:
                return {"error": f"{key} is not an editable setting"}
            try:
                clean[key] = (settings.coerce(key, value)
                              if isinstance(value, str) else value)
            except ValueError as e:
                return {"error": str(e)}
        try:
            saved = settings.save(**clean)
        except Exception as e:
            return {"error": str(e)}
        return {"saved": {k: _jsonable(v) for k, v in (saved or clean).items()}}

    def devices(self):
        """Microphones this machine can offer, for the input picker.

        ``entering-data.md``: offer choices instead of requiring text entry.
        A failure here is not fatal — the page falls back to "System default",
        which is what an unset INPUT_DEVICE already means.
        """
        try:
            found = audio_listener.input_devices()
        except Exception as e:
            return {"error": str(e), "devices": []}
        out = []
        for device in found or []:
            if isinstance(device, dict):
                out.append({"index": device.get("index"),
                            "name": device.get("name") or str(device)})
            else:
                out.append({"index": None, "name": str(device)})
        return {"devices": out}

    # -- the machine ---------------------------------------------------------

    def open_notes_folder(self):
        """Show the notes folder in the system's file manager."""
        return self._reveal(engine_mod.notes_dir())

    def open_config_folder(self):
        """Show the folder holding the settings file and the log."""
        return self._reveal(os.path.dirname(diagnostics.log_path()))

    def _reveal(self, directory):
        """Show a folder in the system file manager. Never raises."""
        try:
            os.makedirs(directory, exist_ok=True)
            system = platform.system()
            if system == "Windows":
                os.startfile(directory)                     # noqa: S606
            elif system == "Darwin":
                subprocess.Popen(["open", directory])
            else:
                subprocess.Popen(["xdg-open", directory])
        except Exception as e:
            return {"error": str(e)}
        return {"opened": directory}

    # -- parity with the shipping tkinter window -----------------------------
    #
    # Everything below was in the Settings and Connections tabs and would have
    # been lost by the swap. None of it is reimplemented: each call goes to the
    # Core module the old tab called, so the two windows cannot drift apart or
    # disagree about what this machine is.

    def version(self):
        """The running version and when it last looked for a newer one.

        No network. :meth:`check_updates` is the one that reaches out, and it
        is a separate call so that only a button can cause it.
        """
        try:
            return {"version": updates.current(),
                    "last_checked": updates.last_checked(),
                    "line": updates.describe()}
        except Exception as e:
            return {"error": str(e)}

    def check_updates(self, timeout=8):
        """Ask whether a newer version exists. **A user action, never automatic.**

        This is the only method in this file that causes a network request, and
        it is here because the shipping window has the button and dropping it
        would be a regression. ``updates.check_now`` says to call it only from
        a user action; that rule survives the port. Nothing calls this on a
        timer, at launch, or when a screen becomes visible.
        """
        try:
            found = updates.check_now(timeout)
        except Exception as e:
            return {"error": str(e)}
        if not found:
            return {"current": True, "version": updates.current()}
        return {"current": False, "latest": found}

    def performance(self):
        """Which performance profile is live, and what it is expected to cost."""
        try:
            name = perf.current()
            return {"profile": name,
                    "describes": perf.describe(name),
                    "ram_mb": perf.estimate_ram_mb()}
        except Exception as e:
            return {"error": str(e)}

    def benchmark(self):
        """Measure this machine. Slow on purpose -- it transcribes real audio.

        Returns peak RAM and the seconds needed for ten seconds of speech, so
        the number reads as "faster than real time" rather than as a score.
        """
        try:
            peak_mb, seconds = perf.benchmark()
        except Exception as e:
            return {"error": str(e)}
        return {"peak_mb": peak_mb, "seconds": seconds,
                "realtime": round(10.0 / seconds, 2) if seconds else None}

    def engine_check(self):
        """Is the notes engine reachable, and what can it run?

        The same call the old tab's "Re-check engine" button made.
        """
        try:
            return {"result": engine_mod.check_notes_engine()}
        except Exception as e:
            return {"error": str(e)}

    def mcp(self):
        """The assistant-connection block, and every host that accepts it.

        What the Connections tab showed. It reads paths off this machine and
        composes text; it connects to nothing and sends nothing.
        """
        def field(host, name):
            if isinstance(host, dict):
                return host.get(name)
            return getattr(host, name, None)

        try:
            hosts = []
            for h in (mcp_hosts.available_hosts() or []):
                hosts.append({
                    "key": field(h, "key"),
                    "name": field(h, "name"),
                    # Where the user has to paste it, and the block to paste --
                    # a host carries its own snippet because a few clients want
                    # the standard shape under a different key.
                    "where": field(h, "where") or "",
                    "snippet": field(h, "snippet") or "",
                    "note": field(h, "note") or "",
                    "local": bool(field(h, "local")),
                })
            return {"hosts": hosts, "config": mcp_hosts.standard_block()}
        except Exception as e:
            return {"error": str(e), "hosts": [], "config": ""}

    def support(self, kind=""):
        """Open a shipped guide, or the support page.

        ``kind`` picks a document; empty opens support. The fallback to the web
        when a shipped copy is missing is Core's decision, not this file's.
        """
        try:
            if kind:
                diagnostics.open_doc(kind)
            else:
                diagnostics.open_support()
        except Exception as e:
            return {"error": str(e)}
        return {"opened": kind or "support"}

    def save_report(self):
        """Write a redacted diagnostic report, then reveal it.

        Redaction is ``diagnostics.redact``'s job rather than this one's, so
        what gets stripped is decided in one place for both windows.
        """
        try:
            path = diagnostics.save_report(None)
        except Exception as e:
            return {"error": str(e)}
        self._reveal(os.path.dirname(path))
        return {"path": path}

    # -- first run -----------------------------------------------------------
    #
    # The same five questions the tkinter wizard asks, asked by this window
    # instead. Not a new flow and not a shorter one: `setup_wizard.options()`
    # supplies the choices and `setup_wizard.commit()` writes them, so both
    # windows ask the same things and save the same settings. What changed is
    # what it looks like, and nothing else.

    def setup_needed(self):
        """Has anybody been through setup on this machine?"""
        import setup_wizard

        try:
            return {"needed": bool(setup_wizard.needed())}
        except Exception as e:                       # noqa: BLE001
            # Never let a broken check trap somebody behind a wizard. "Not
            # needed" is the safer wrong answer: the app runs on its defaults
            # and Settings can reach every one of them.
            return {"needed": False, "error": str(e)}

    def setup_options(self):
        """Everything needed to draw the five steps."""
        import setup_wizard

        try:
            return setup_wizard.options()
        except Exception as e:                       # noqa: BLE001
            return {"error": str(e)}

    def setup_commit(self, choices=None):
        """Write the choices, then make them live in this process.

        ``settings.apply()`` is the part that is easy to forget: this window is
        already running and the engine has not been built yet, so without it
        the first recording would use the config this process started with
        rather than what was just chosen.
        """
        import setup_wizard

        try:
            saved = setup_wizard.commit(dict(choices or {}))
            settings.apply()
        except Exception as e:                       # noqa: BLE001
            return {"error": str(e)}
        return {"saved": saved}

    def ollama_probe(self, url=""):
        """Is Ollama there, and what does it already have?

        Its own method rather than part of :meth:`setup_options` because the
        answer changes while the step is on screen -- somebody installs Ollama
        in another window and presses the button again.
        """
        import setup_wizard

        try:
            reachable, models = setup_wizard.ollama_models(url or "")
            hint, command = setup_wizard.install_hint()
        except Exception as e:                       # noqa: BLE001
            return {"error": str(e), "reachable": False, "models": []}
        return {"reachable": bool(reachable), "models": list(models),
                "hint": hint, "command": command}

    def pull_model(self, name="", url=""):
        """Download a model into Ollama, reporting progress to the page.

        On a worker thread: it is most of a gigabyte over somebody's
        connection and the window has to stay alive. Progress arrives as
        ``pull`` events, the same channel everything else uses.
        """
        import setup_wizard

        def run():
            def progress(done, total, status=""):
                self.push("pull", {"done": done, "total": total,
                                   "status": status})

            try:
                error = setup_wizard.pull_model(name, url or "", progress)
            except Exception as e:                   # noqa: BLE001
                error = str(e)
            self.push("pull", {"done": 0, "total": 0, "finished": True,
                               "error": error or ""})

        threading.Thread(target=run, daemon=True, name="vl-pull").start()
        return {"started": True}

    def choose_folder(self):
        """Open the system folder picker. Empty path means cancelled."""
        if self._window is None:
            return {"path": ""}
        try:
            import webview

            # `FileDialog.FOLDER` on 6.2.1, falling back to the old constant
            # on anything older. They are the same value (20), but the bare
            # `webview.FOLDER_DIALOG` prints a deprecation warning on every
            # call and is slated for removal -- and this is called from the
            # setup steps, so that warning lands on somebody's first run.
            folder = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
            picked = self._window.create_file_dialog(
                folder if folder is not None else webview.FOLDER_DIALOG)
        except Exception as e:                       # noqa: BLE001
            return {"error": str(e), "path": ""}
        if not picked:
            return {"path": ""}
        first = picked[0] if isinstance(picked, (list, tuple)) else picked
        return {"path": str(first)}

    def shutdown(self):
        """Finish what is being written, then let the window go.

        A summary in flight is a meeting somebody already sat through. Dropping
        it to close a window faster is the wrong trade, so this waits.
        """
        if self._engine is None:
            return {"ok": True}
        try:
            self._engine.shutdown(
                on_wait=lambda n: self.push(
                    "closing", {"pending": n}))
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}


#: A stem begins with the date it was recorded, and sometimes a time after it:
#: "2026-09-14_meeting-with-the-team" and "2026-09-14_1402_pricing-review" are
#: both real. Both are stripped, because the row already shows when it was.
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[_-]?(?:\d{3,6}[_-]?)?")


def _title_from(base):
    """A readable title from a file stem."""
    stem = _STAMP.sub("", base)
    stem = stem.replace("-", " ").replace("_", " ").strip()
    if not stem:
        return base
    return stem[:1].upper() + stem[1:]


def _jsonable(value):
    """Settings hold ints, floats, bools, strings and None. Anything else is text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
