"""First-run setup — pick a speech model, decide about summaries, and go.

Runs automatically the first time the app starts with no saved settings, and
any time afterwards from Settings, or::

    python vlocalhost.py --setup

**Why this lives in the app and not in the installer.** Every question here is
the same question on Windows, macOS and Linux, but the installers are a batch
file, a shell script and a shell script — three languages, none of them
testable, none of them able to show a progress bar. Asking here means one
implementation, in the toolkit the app already draws with, that can be re-run
whenever somebody installs Ollama later or swaps their microphone.

It also puts the questions in the right order. The old installer ran
``ollama pull`` before the user had ever seen the app, so a two-gigabyte
download blocked somebody who had not yet decided they wanted the product.

**Ollama is reached over HTTP, never by running the ``ollama`` command.** The
CLI is frequently not on PATH even on machines where the server is running
perfectly — a ``where ollama`` check reports "not installed" and sends the user
off to reinstall something they already have. The server on 127.0.0.1 is the
honest test.

Nothing here is mandatory. Every step can be skipped, and skipping leaves a
working app: recording and transcription never needed Ollama, only the written
summary does.
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import config
import languages
import performance
import settings

# Core's palette, so the wizard and the window are visibly one product.
INK = "#090C12"
PANEL = "#0E1220"
EDGE = "#1C2333"
AMBER = "#FFB43D"
CYAN = "#38E1CE"
PAPER = "#EAEEF4"
MUTED = "#7E8AA0"
DANGER = "#E8624F"

import platform as _platform

_MONO = ("Cascadia Code", 9) if _platform.system() == "Windows" else ("Menlo", 11)
_BODY = ("Segoe UI", 10) if _platform.system() == "Windows" else ("Helvetica", 12)
_HEAD = ("Segoe UI", 17, "bold") if _platform.system() == "Windows" \
    else ("Helvetica", 19, "bold")

#: What we offer to fetch when there is no usable model installed.
DEFAULT_LLM = "llama3.2"

#: What the language picker opens on. Deliberately a constant rather than
#: whatever ``config`` currently holds: setup is the place where somebody is
#: told what the app assumes, and the answer should not change depending on
#: what a previous run left behind. Anyone who wants another language picks it
#: here, or in Settings afterwards.
DEFAULT_LANGUAGE = "en"

#: Long enough for a busy machine, short enough that nobody thinks we hung.
PROBE_TIMEOUT = 2.5

#: Where somebody who has never heard of Ollama has to go.
OLLAMA_SITE = "https://ollama.com/download"

#: How often to look again while the user is off installing it, in ms. The
#: whole point is that they leave this window open, install Ollama in another
#: one, and come back to find the step has moved on by itself -- so it has to
#: be often enough to feel immediate and cheap enough to run all day. The probe
#: is one HTTP call to a loopback address that is not listening.
WATCH_EVERY = 4000


def install_hint() -> tuple:
    """(what to do, the command to type) for installing Ollama on this OS.

    The old screen said "install it from ollama.com" and stopped there, which
    is a fine instruction for somebody who already knows what Ollama is and no
    instruction at all for the person actually reading it. Three sentences,
    picked by platform, cost nothing and remove the guess.
    """
    system = _platform.system()
    if system == "Windows":
        return ("Download it from ollama.com. The Windows installer needs no "
                "administrator rights and starts the server for you.", "")
    if system == "Darwin":
        return ("Download it from ollama.com. Open the .dmg, drag Ollama into "
                "Applications, and launch it once.", "")
    return ("Install it from a terminal:",
            "curl -fsSL https://ollama.com/install.sh | sh")


# ---------------------------------------------------------------------------
# Ollama, over HTTP
# ---------------------------------------------------------------------------
def ollama_models(url: str = "") -> tuple:
    """Return ``(reachable, [model names])`` for the Ollama server.

    Never raises: an unreachable server is the ordinary case, not an error.
    """
    import requests

    base = (url or getattr(config, "OLLAMA_URL", "")).rstrip("/")
    if not base:
        return False, []
    try:
        resp = requests.get(f"{base}/api/tags", timeout=PROBE_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001 - connection, timeout, HTTP, bad JSON
        return False, []
    names = []
    for entry in payload.get("models") or []:
        name = (entry or {}).get("name") or (entry or {}).get("model") or ""
        if name:
            names.append(name)
    return True, sorted(names)


def default_notes_dir() -> str:
    """Where notes go when nobody has chosen anything: the per-user data
    folder, which no install or upgrade ever touches."""
    from integrations import store

    return os.path.join(store.data_dir(), "notes")


def folder_problem(path: str) -> str:
    """Why ``path`` can't hold notes, or "" if it can.

    Checked by writing, not by inspecting permissions: a network share, a
    read-only mount and a folder owned by another user all look fine until
    something actually tries.
    """
    probe = os.path.join(path, ".vlocalhost-write-test")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
    except OSError as e:
        return f"Notes can't be saved there: {e.strerror or e}"
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return ""


def _base_name(model: str) -> str:
    """``llama3.2:latest`` -> ``llama3.2``, so tags don't defeat a match."""
    return model.split(":", 1)[0]


def pull_model(name: str, url: str, progress) -> str:
    """Stream a model pull, reporting progress. Returns "" on success.

    ``progress`` is called with (fraction, status text); fraction is None while
    the server is doing something it cannot measure.

    A sealed install refuses. The request itself is loopback -- it goes to
    Ollama on this machine -- but Ollama answers it by downloading from its own
    registry, so allowing it while sealed would mean causing an internet
    connection the app had promised not to make. Proxying a download through
    another process does not make it a local operation, and the whole value of
    the switch is that it does not have that kind of exception.
    """
    import requests

    import network

    if not network.allowed("note_model_pull"):
        return str(network.refuse("note_model_pull"))

    base = (url or getattr(config, "OLLAMA_URL", "")).rstrip("/")
    try:
        resp = requests.post(f"{base}/api/pull",
                             json={"model": name, "stream": True},
                             stream=True, timeout=(10, 600))
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("error"):
                return str(event["error"])
            done = event.get("completed")
            total = event.get("total")
            status = event.get("status") or "working"
            if isinstance(done, int) and isinstance(total, int) and total > 0:
                progress(done / total, f"{status} — {done // 1048576} of "
                                       f"{total // 1048576} MB")
            else:
                progress(None, status)
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------
class Wizard:
    """A short sequence of questions that ends in a saved settings file."""

    def __init__(self, master=None):
        self._own_root = master is None
        self.root = tk.Tk() if self._own_root else tk.Toplevel(master)
        self.root.title("Set up Vlocalhost.AI")
        self.root.configure(bg=INK)
        self.root.resizable(False, False)
        if not self._own_root:
            self.root.transient(master)

        self.completed = False
        self.choices = {}
        self._pull_queue = queue.Queue()
        self._probe_queue = queue.Queue()
        self._watch_job = None

        self.body = tk.Frame(self.root, bg=INK, padx=30, pady=26)
        self.body.pack(fill="both", expand=True)

        self.nav = tk.Frame(self.root, bg=INK, padx=30)
        self.nav.pack(fill="x", pady=(0, 22))
        self.back_btn = ttk.Button(self.nav, text="Back", command=self._back)
        self.next_btn = ttk.Button(self.nav, text="Next", command=self._next)
        self.back_btn.pack(side="left")
        self.next_btn.pack(side="right")

        self.steps = [self._step_welcome, self._step_language, self._step_speech,
                      self._step_summaries, self._step_done]
        self.index = 0
        self._render()

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Escape>", lambda _e: self._close())

    # -- frame plumbing ----------------------------------------------------
    def _clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _heading(self, title, blurb=""):
        tk.Label(self.body, text=f"STEP {self.index + 1} OF {len(self.steps)}",
                 font=_MONO, bg=INK, fg=CYAN).pack(anchor="w")
        tk.Label(self.body, text=title, font=_HEAD, bg=INK, fg=PAPER,
                 justify="left").pack(anchor="w", pady=(8, 6))
        if blurb:
            tk.Label(self.body, text=blurb, font=_BODY, bg=INK, fg=MUTED,
                     justify="left", wraplength=520).pack(anchor="w")

    def _render(self):
        self._clear()
        self.steps[self.index]()
        self.back_btn.configure(state="normal" if self.index else "disabled")
        last = self.index == len(self.steps) - 1
        self.next_btn.configure(text="Finish" if last else "Next")

    def _remember(self):
        """Hold on to this page's answers: leaving it destroys its widgets, and
        a rebuilt page must come back with what the user already chose."""
        for name in ("notes_dir", "language", "profile", "custom_model"):
            var = getattr(self, name, None)
            if var is not None:
                self.choices[name] = var.get()

    def _back(self):
        if self.index:
            self._remember()
            self.index -= 1
            self._render()

    def _next(self):
        if self.index == len(self.steps) - 1:
            self._close()
            return
        self._remember()
        self.index += 1
        self._render()

    # -- steps -------------------------------------------------------------
    def _step_welcome(self):
        from integrations import store

        self._heading(
            "Everything stays on this machine",
            "A few quick questions and you're recording. Every answer can be "
            "changed later in Settings.")

        self.notes_dir = tk.StringVar(
            value=self.choices.get("notes_dir") or store.notes_dir())
        card = tk.Frame(self.body, bg=PANEL, highlightbackground=EDGE,
                        highlightthickness=1, padx=16, pady=14)
        card.pack(fill="x", pady=(18, 0))
        tk.Label(card, text="Your notes will be saved to", font=_BODY,
                 bg=PANEL, fg=MUTED).pack(anchor="w")
        tk.Label(card, textvariable=self.notes_dir, font=_MONO, bg=PANEL,
                 fg=AMBER, wraplength=500,
                 justify="left").pack(anchor="w", pady=(4, 0))

        row = tk.Frame(card, bg=PANEL)
        row.pack(anchor="w", pady=(12, 0))
        ttk.Button(row, text="Choose folder…",
                   command=self._browse_notes).pack(side="left")
        ttk.Button(row, text="Use the default",
                   command=self._default_notes).pack(side="left", padx=(8, 0))

        self.notes_note = tk.Label(
            card, text="Updating or reinstalling the app never touches this "
                       "folder. Point it at a synced folder and your notes "
                       "follow you between machines.",
            font=_BODY, bg=PANEL, fg=MUTED, wraplength=500, justify="left")
        self.notes_note.pack(anchor="w", pady=(12, 0))

    def _browse_notes(self):
        """Choose the notes folder. A folder that can't be written to is
        refused here, where it is one dialog away from being fixed, rather
        than at the end of a meeting nobody can save."""
        chosen = filedialog.askdirectory(
            parent=self.root, title="Choose where meeting notes are saved",
            mustexist=False)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        problem = folder_problem(chosen)
        if problem:
            self.notes_note.configure(text=problem, fg=DANGER)
            return
        self.notes_dir.set(chosen)
        self.notes_note.configure(
            text="New notes are written here. Anything already saved stays "
                 "where it is — move those yourself if you want them together.",
            fg=MUTED)

    def _default_notes(self):
        self.notes_dir.set(default_notes_dir())
        self.notes_note.configure(
            text="Back to the standard location, inside your per-user data "
                 "folder.", fg=MUTED)

    @staticmethod
    def _language_label(code) -> str:
        """The picker's label for a config value ("en" -> "English")."""
        wanted = code or languages.AUTO
        for value, label in languages.choices():
            if value == wanted:
                return label
        return languages.name_for(code)

    def _step_language(self):
        self._heading(
            "What language are your meetings in?",
            "Pinning the language is faster and more accurate than detecting "
            "it: meeting speech comes in short bursts, which is exactly where "
            "detection guesses wrong.")

        self._lang_labels = {label: code for code, label in languages.choices()}
        self.language = tk.StringVar(value=self.choices.get(
            "language", self._language_label(DEFAULT_LANGUAGE)))
        ttk.Combobox(self.body, textvariable=self.language, state="readonly",
                     width=34,
                     values=[label for _, label in languages.choices()]).pack(
            anchor="w", pady=(20, 0))

        card = tk.Frame(self.body, bg=PANEL, highlightbackground=EDGE,
                        highlightthickness=1, padx=16, pady=14)
        card.pack(fill="x", pady=(20, 0))
        tk.Label(card, text="Meetings that switch languages",
                 font=_BODY, bg=PANEL, fg=PAPER).pack(anchor="w")
        tk.Label(card, text="Choose “Auto-detect” instead. Each line is then "
                            "worked out on its own and tagged with the language "
                            "it was heard as, so a wrong guess is visible rather "
                            "than silent.",
                 font=_BODY, bg=PANEL, fg=MUTED, wraplength=500,
                 justify="left").pack(anchor="w", pady=(4, 0))

    def _step_speech(self):
        self._heading(
            "How accurate should transcription be?",
            "This trades accuracy against how hard your machine has to work. "
            "The model downloads once, the first time you record.")

        self.profile = tk.StringVar(value=self.choices.get("profile", "balanced"))
        for key in ("light", "balanced", "accurate"):
            spec = performance.PROFILES[key]
            row = tk.Frame(self.body, bg=INK)
            row.pack(fill="x", pady=(12, 0))
            tk.Radiobutton(
                row, text=f"{spec['label']}  ·  {spec['WHISPER_MODEL']}",
                variable=self.profile, value=key, font=_BODY, bg=INK, fg=PAPER,
                selectcolor=PANEL, activebackground=INK, activeforeground=PAPER,
                bd=0, highlightthickness=0).pack(anchor="w")
            tk.Label(row, text=f"    {spec['summary']} · ~{spec['ram_mb']} MB · "
                               f"{spec['accuracy']}",
                     font=_BODY, bg=INK, fg=MUTED, wraplength=500,
                     justify="left").pack(anchor="w")

        own = tk.Frame(self.body, bg=INK)
        own.pack(fill="x", pady=(18, 0))
        self.custom_model = tk.StringVar(value=self.choices.get("custom_model", ""))
        tk.Label(own, textvariable=self.custom_model, font=_MONO, bg=INK,
                 fg=AMBER, wraplength=380, justify="left").pack(side="left")
        ttk.Button(own, text="Use my own model folder…",
                   command=self._browse_model).pack(side="right")

    def _browse_model(self):
        chosen = filedialog.askdirectory(
            parent=self.root, title="Select a converted Whisper model folder")
        if chosen:
            self.custom_model.set(chosen)

    def _step_summaries(self):
        self._heading(
            "Written summaries (optional)",
            "Recording and transcription work without this. Summaries are "
            "written by a second model running locally, through Ollama.")

        self.url = tk.StringVar(value=getattr(config, "OLLAMA_URL", ""))
        self.llm = tk.StringVar(value=getattr(config, "OLLAMA_MODEL", DEFAULT_LLM))
        self.status = tk.StringVar(value="Looking for Ollama…")
        self.progress_text = tk.StringVar(value="")

        card = tk.Frame(self.body, bg=PANEL, highlightbackground=EDGE,
                        highlightthickness=1, padx=16, pady=14)
        card.pack(fill="x", pady=(18, 0))
        tk.Label(card, textvariable=self.status, font=_BODY, bg=PANEL,
                 fg=PAPER, wraplength=500, justify="left").pack(anchor="w")

        self.model_row = tk.Frame(card, bg=PANEL)
        self.model_row.pack(fill="x", pady=(12, 0))

        self.bar = ttk.Progressbar(card, mode="determinate", maximum=1000)
        self.progress_label = tk.Label(card, textvariable=self.progress_text,
                                       font=_MONO, bg=PANEL, fg=MUTED)

        self.root.after(60, self._probe_ollama)

    @staticmethod
    def _alive(widget) -> bool:
        """True if a widget is still on screen.

        Both the probe and the download poller are ``after`` callbacks, so they
        can arrive after the user has moved on or closed the window and the
        widgets they were going to update no longer exist.
        """
        try:
            return bool(widget.winfo_exists())
        except Exception:  # noqa: BLE001 - interpreter already gone
            return False

    def _cancel_watch(self):
        """Drop a pending automatic re-probe.

        Called before every probe, so a user pressing *Check now* while the
        timer is still pending ends up with one chain of callbacks rather than
        two, each halving the interval of the last.
        """
        if self._watch_job is not None:
            try:
                self.root.after_cancel(self._watch_job)
            except Exception:  # noqa: BLE001 - already fired, or window gone
                pass
            self._watch_job = None

    def _probe_ollama(self):
        """Ask the server what it has, off the Tk thread.

        This used to call ``ollama_models`` inline, which was fine when it
        happened once on entering the step. It now also runs on a timer, and a
        blocking 2.5-second call every four seconds would leave the window
        unable to redraw for most of its life.
        """
        if not self._alive(self.model_row):
            return
        self._cancel_watch()
        url = self.url.get()

        def worker():
            self._probe_queue.put(ollama_models(url))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(120, self._drain_probe)

    def _drain_probe(self):
        """Tk is single-threaded: the worker probes, the main loop draws."""
        if not self._alive(self.model_row):
            return
        try:
            reachable, models = self._probe_queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self._drain_probe)
            return
        self._show_ollama(reachable, models)

    def _open_ollama_site(self):
        import webbrowser

        try:
            webbrowser.open(OLLAMA_SITE)
        except Exception:  # noqa: BLE001 - no browser, or no desktop session
            print(f"Ollama: {OLLAMA_SITE}", flush=True)

    def _open_summaries_guide(self):
        """The shipped PDF that covers this entire step in full.

        diagnostics is imported here rather than at module scope, as everywhere
        else in the app: it pulls in platform and traceback, and setup does not
        need either of them to draw a window.
        """
        try:
            import diagnostics

            diagnostics.open_doc("summaries")
        except Exception as e:  # noqa: BLE001 - a help button must not throw
            print(f"[setup] could not open the summaries guide: {e}", flush=True)

    def _show_ollama(self, reachable, models):
        """Draw the result of a probe."""
        if not self._alive(self.model_row):
            return
        for child in self.model_row.winfo_children():
            child.destroy()

        if not reachable:
            self.status.set(
                "Ollama isn't running on this machine.\n\n"
                "Everything else works — you'll get a full transcript of "
                "every meeting. Only the written summary needs Ollama.")
            what, command = install_hint()
            tk.Label(self.model_row, text=what, font=_BODY, bg=PANEL, fg=PAPER,
                     wraplength=500, justify="left").pack(anchor="w")
            if command:
                tk.Label(self.model_row, text=command, font=_MONO, bg=PANEL,
                         fg=AMBER, wraplength=500,
                         justify="left").pack(anchor="w", pady=(4, 0))
            tk.Label(self.model_row,
                     text="Leave this window open. It notices by itself when "
                          "Ollama starts, and carries on from there.",
                     font=_BODY, bg=PANEL, fg=MUTED, wraplength=500,
                     justify="left").pack(anchor="w", pady=(8, 0))

            row = tk.Frame(self.model_row, bg=PANEL)
            row.pack(anchor="w", pady=(10, 0))
            ttk.Button(row, text="Open ollama.com",
                       command=self._open_ollama_site).pack(side="left")
            ttk.Button(row, text="Setup guide",
                       command=self._open_summaries_guide).pack(
                side="left", padx=(8, 0))
            ttk.Button(row, text="Check now",
                       command=self._probe_ollama).pack(side="left", padx=(8, 0))
            ttk.Button(row, text="Skip",
                       command=self._next).pack(side="left", padx=(8, 0))

            # The whole reason this is no longer a dead end: installing Ollama
            # happens in another window, and the user should not have to know
            # to come back here and press anything when it is done.
            self._watch_job = self.root.after(WATCH_EVERY, self._probe_ollama)
            return

        usable = [m for m in models if _base_name(m)]
        if usable:
            self.status.set("Ollama is running. Choose the model that writes "
                            "your summaries:")
            preferred = next(
                (m for m in usable if _base_name(m) == _base_name(self.llm.get())),
                usable[0])
            self.llm.set(preferred)
            ttk.Combobox(self.model_row, textvariable=self.llm, values=usable,
                         state="readonly", width=32).pack(side="left")
            ttk.Button(self.model_row, text="Refresh",
                       command=self._probe_ollama).pack(side="left", padx=(10, 0))
        else:
            self.status.set(
                f"Ollama is running but has no models yet. "
                f"{DEFAULT_LLM} is about 2 GB.")
            ttk.Button(self.model_row, text=f"Download {DEFAULT_LLM}",
                       command=self._start_pull).pack(side="left")
            ttk.Button(self.model_row, text="Skip",
                       command=self._next).pack(side="left", padx=(10, 0))

    def _start_pull(self):
        for child in self.model_row.winfo_children():
            child.destroy()
        self.bar.pack(fill="x", pady=(12, 4))
        self.progress_label.pack(anchor="w")
        self.progress_text.set("starting…")
        self.next_btn.configure(state="disabled")

        url = self.url.get()

        def worker():
            error = pull_model(DEFAULT_LLM, url,
                               lambda f, t: self._pull_queue.put(("tick", f, t)))
            self._pull_queue.put(("done", error, ""))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(120, self._drain_pull)

    def _drain_pull(self):
        """Tk is single-threaded: the worker posts, the main loop renders."""
        if not self._alive(self.bar):
            return          # window closed mid-download; the thread is a daemon
        try:
            while True:
                kind, a, b = self._pull_queue.get_nowait()
                if kind == "tick":
                    if a is None:
                        self.bar.configure(mode="indeterminate")
                        self.bar.start(12)
                    else:
                        self.bar.stop()
                        self.bar.configure(mode="determinate", value=a * 1000)
                    self.progress_text.set(b)
                else:
                    self.bar.stop()
                    self.bar.pack_forget()
                    self.progress_label.pack_forget()
                    self.next_btn.configure(state="normal")
                    if a:
                        self.status.set(f"That download didn't finish: {a}\n\n"
                                        f"You can skip this and set it up later "
                                        f"from Settings.")
                    else:
                        self.llm.set(DEFAULT_LLM)
                    self._probe_ollama()
                    return
        except queue.Empty:
            pass
        self.root.after(120, self._drain_pull)

    def _step_done(self):
        self._heading("You're set up",
                      "Press Finish, then Start recording. Everything here is "
                      "in Settings if you change your mind.")
        summary = tk.Frame(self.body, bg=PANEL, highlightbackground=EDGE,
                           highlightthickness=1, padx=16, pady=14)
        summary.pack(fill="x", pady=(18, 0))
        for label, value in self._pending().items():
            row = tk.Frame(summary, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=_BODY, bg=PANEL, fg=MUTED,
                     width=18, anchor="w").pack(side="left")
            tk.Label(row, text=value, font=_MONO, bg=PANEL, fg=AMBER,
                     anchor="w", wraplength=340, justify="left").pack(side="left")

    # -- results -----------------------------------------------------------
    def _language_code(self):
        """The chosen language code, or None for auto-detect. Absent when the
        user closed the wizard before the language step ever drew."""
        var = getattr(self, "language", None)
        label = var.get() if var is not None else self.choices.get("language", "")
        if not label:
            return ""
        return getattr(self, "_lang_labels", {}).get(label, languages.AUTO)

    def _notes_choice(self) -> str:
        """The notes folder on screen, or "" if step one never drew."""
        var = getattr(self, "notes_dir", None)
        chosen = var.get() if var is not None else self.choices.get("notes_dir", "")
        return os.path.normpath(chosen.strip()) if chosen.strip() else ""

    def _pending(self) -> dict:
        """What Finish will save, in human terms."""
        out = {}
        notes = self._notes_choice()
        if notes:
            out["Notes folder"] = notes
        code = self._language_code()
        if code:
            out["Spoken language"] = languages.name_for(
                languages.normalize(code))
        custom = getattr(self, "custom_model", None)
        custom = custom.get().strip() if custom else ""
        if custom:
            out["Speech model"] = custom
        else:
            key = getattr(self, "profile", None)
            key = key.get() if key else "balanced"
            out["Speech model"] = performance.PROFILES[key]["WHISPER_MODEL"]
        llm = getattr(self, "llm", None)
        out["Summaries"] = llm.get() if llm and llm.get() else "off for now"
        return out

    def _save(self):
        """Write the choices. A failure here must not trap the user."""
        changes = {}
        notes = self._notes_choice()
        if notes:
            # The default is stored as the plain name config.py ships, not as
            # an absolute path: a profile that moves between machines (or a
            # user whose account is renamed) should follow the data folder
            # rather than point at a path that no longer exists.
            changes["OUTPUT_DIR"] = ("notes"
                                     if notes == os.path.normpath(default_notes_dir())
                                     else notes)
        code = self._language_code()
        if code:
            # normalize() turns the "auto" the picker speaks into the None the
            # transcriber wants.
            changes["WHISPER_LANGUAGE"] = languages.normalize(code)
        custom = getattr(self, "custom_model", None)
        custom = custom.get().strip() if custom else ""
        if custom:
            changes["WHISPER_MODEL"] = custom
        elif getattr(self, "profile", None):
            spec = performance.PROFILES[self.profile.get()]
            changes["WHISPER_MODEL"] = spec["WHISPER_MODEL"]
            changes["WHISPER_BEAM_SIZE"] = spec["WHISPER_BEAM_SIZE"]
            changes["WHISPER_COMPUTE"] = spec["WHISPER_COMPUTE"]
        llm = getattr(self, "llm", None)
        if llm and llm.get().strip():
            changes["OLLAMA_MODEL"] = llm.get().strip()
        url = getattr(self, "url", None)
        if url and url.get().strip():
            changes["OLLAMA_URL"] = url.get().strip()

        if not changes:
            return
        try:
            settings.save(**changes)
            self.completed = True
        except Exception as e:  # noqa: BLE001
            print(f"[setup] could not save settings: {e}", flush=True)

    def _close(self):
        self._save()
        try:
            self.root.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    def run(self) -> bool:
        """Show the wizard and block until it closes. True if settings saved."""
        self.root.update_idletasks()
        try:
            self.root.grab_set()
        except Exception:  # noqa: BLE001
            pass
        if self._own_root:
            self.root.mainloop()
        else:
            self.root.wait_window()
        return self.completed


# ---------------------------------------------------------------------------
def needed() -> bool:
    """True when nobody has been through setup on this machine yet."""
    return not os.path.isfile(settings.path())


def run(master=None) -> bool:
    """Show the wizard. Never raises — setup is not worth blocking the app."""
    try:
        return Wizard(master).run()
    except Exception as e:  # noqa: BLE001
        print(f"[setup] wizard unavailable: {e}", flush=True)
        return False
