"""Vlocalhost.AI — desktop window.

A small Tk interface over :class:`engine.AppEngine`: press record and watch the
transcript appear, connect a Google or Outlook account, and flip the delivery
settings — without editing any Python. Tk ships with Python, so this adds no
dependency and looks the same on Windows, macOS, and Linux.

    python vlocalhost.py            # opens this window
"""

import json
import os
import platform
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import config
import engine as engine_mod
import languages
import performance
import settings
from integrations import UPGRADE_URL, available_providers, provider_label, store

# --- Brand tokens ---------------------------------------------------------
INK = "#090C12"       # window ground
PANEL = "#0E1220"     # cards / surfaces
EDGE = "#1C2333"      # hairline borders
AMBER = "#FFB43D"     # the one accent
AMBER_DEEP = "#E08A17"
CYAN = "#38E1CE"      # "on-device / live" — semantic only
PAPER = "#EAEEF4"
MUTED = "#7E8AA0"
DANGER = "#E8624F"

#: Callables run just before the window is destroyed, in registration order.
#: Populated only by optional packages — the core registers none of its own.
_exit_hooks = []


def register_exit_hook(hook):
    """Register *hook* to run just before the window closes.

    ``hook`` is called with the root window and may open a dialog of its
    own; the close waits for it. It runs *after* the engine has shut down
    and the notes are on disk, so nothing a hook does can cost a recording,
    and a hook that raises is reported and skipped — quitting must always
    succeed.

    This is an extension point. The core ships no hooks; a package that
    wants a word with the user on the way out registers one from its
    ``register()``.
    """
    _exit_hooks.append(hook)


MONO = ("Cascadia Code", 9) if platform.system() == "Windows" else ("Menlo", 11)
BODY = ("Segoe UI", 10) if platform.system() == "Windows" else ("Helvetica", 12)
TITLE = ("Segoe UI", 20, "bold") if platform.system() == "Windows" \
    else ("Helvetica", 22, "bold")


def open_folder(path):
    """Reveal a folder in the OS file manager."""
    os.makedirs(path, exist_ok=True)
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # noqa: S606 - a known local directory
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def mmss(seconds):
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class App:
    def __init__(self, root):
        self.root = root
        self._ui_q = queue.Queue()   # work marshalled back onto the Tk thread
        self._busy = False           # a start/stop is in flight

        self.engine = engine_mod.build(on_line=self._line_from_worker)

        root.title("Vlocalhost.AI — Meeting Notes")
        root.geometry("940x660")
        root.minsize(820, 560)
        root.configure(bg=INK)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Tk swallows exceptions raised inside callbacks and prints them to a
        # console the user does not have. Route them into the crash report.
        try:
            import diagnostics

            root.report_callback_exception = diagnostics.tk_exception
        except Exception:  # noqa: BLE001
            pass

        self._style()
        self._header()
        self._tabs()
        self._statusbar()

        self.root.after(80, self._pump)
        self.root.after(1000, self._tick)
        self._refresh_connections()
        self._check_loopback()
        self._show_language_warning()
        self._refresh_profile()
        threading.Thread(target=self._check_ollama, daemon=True).start()

    # -- theme ---------------------------------------------------------------
    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")  # the only built-in theme that honours our colors
        s.configure(".", background=PANEL, foreground=PAPER, font=BODY,
                    borderwidth=0, focuscolor=PANEL)
        s.configure("TFrame", background=PANEL)
        s.configure("Ink.TFrame", background=INK)
        s.configure("TLabel", background=PANEL, foreground=PAPER)
        s.configure("Ink.TLabel", background=INK, foreground=PAPER)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("MutedInk.TLabel", background=INK, foreground=MUTED)
        s.configure("Mono.TLabel", background=PANEL, foreground=MUTED, font=MONO)
        s.configure("Big.TLabel", background=PANEL, foreground=PAPER,
                    font=(BODY[0], 15, "bold"))
        s.configure("Live.TLabel", background=PANEL, foreground=CYAN, font=MONO)
        s.configure("Bad.TLabel", background=PANEL, foreground=DANGER)
        s.configure("Good.TLabel", background=PANEL, foreground=CYAN)

        s.configure("TNotebook", background=INK, borderwidth=0, tabmargins=(12, 8, 0, 0))
        s.configure("TNotebook.Tab", background=INK, foreground=MUTED,
                    padding=(18, 9), font=BODY)
        s.map("TNotebook.Tab", background=[("selected", PANEL)],
              foreground=[("selected", PAPER)])

        s.configure("TButton", background=EDGE, foreground=PAPER, padding=(14, 7))
        s.map("TButton", background=[("active", "#28324A"), ("disabled", "#141A28")],
              foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=AMBER, foreground=INK,
                    padding=(22, 11), font=(BODY[0], 11, "bold"))
        s.map("Accent.TButton", background=[("active", AMBER_DEEP),
                                            ("disabled", "#5A4A28")],
              foreground=[("disabled", "#20242E")])
        s.configure("Stop.TButton", background=DANGER, foreground=INK,
                    padding=(22, 11), font=(BODY[0], 11, "bold"))
        s.map("Stop.TButton", background=[("active", "#C74B39")])

        s.configure("TCheckbutton", background=PANEL, foreground=PAPER)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("TRadiobutton", background=PANEL, foreground=PAPER)
        s.map("TRadiobutton", background=[("active", PANEL)])
        s.configure("Card.TLabelframe", background=PANEL, bordercolor=EDGE,
                    borderwidth=1, relief="solid")
        s.configure("Card.TLabelframe.Label", background=PANEL, foreground=AMBER,
                    font=(BODY[0], 10, "bold"))
        s.configure("TEntry", fieldbackground=INK, foreground=PAPER,
                    insertcolor=AMBER, bordercolor=EDGE)
        s.configure("TCombobox", fieldbackground=INK, background=EDGE,
                    foreground=PAPER, arrowcolor=AMBER)

    def _header(self):
        bar = ttk.Frame(self.root, style="Ink.TFrame", padding=(20, 14, 20, 6))
        bar.pack(fill="x")
        mark = tk.Canvas(bar, width=34, height=34, bg=INK, highlightthickness=0)
        # The brand mark, drawn small: amber tile holding a dark waveform.
        mark.create_rectangle(2, 2, 32, 32, fill=AMBER, outline="")
        for x, h in ((9, 6), (14, 11), (19, 8), (24, 4)):
            mark.create_rectangle(x, 17 - h, x + 3, 17 + h, fill=INK, outline="")
        mark.pack(side="left", padx=(0, 12))

        name = ttk.Frame(bar, style="Ink.TFrame")
        name.pack(side="left")
        ttk.Label(name, text="Vlocalhost.AI", style="Ink.TLabel",
                  font=TITLE).pack(anchor="w")
        ttk.Label(name, text="Meeting notes that never leave your machine",
                  style="MutedInk.TLabel").pack(anchor="w")

        self.badge = tk.Label(bar, text="● on-device", bg=INK, fg=CYAN, font=MONO)
        self.badge.pack(side="right")

    def _tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(6, 0))
        self.tab_record = ttk.Frame(nb, padding=18)
        self.tab_conn = ttk.Frame(nb, padding=18)
        self.tab_set = ttk.Frame(nb)
        nb.add(self.tab_record, text="Record")
        nb.add(self.tab_conn, text="Connections")
        nb.add(self.tab_set, text="Settings")
        self._build_record(self.tab_record)
        self._build_connections(self.tab_conn)
        # Settings is taller than the window, so it scrolls.
        self._build_settings(self._scrollable(self.tab_set))

    @staticmethod
    def _scrollable(parent):
        """Return a frame inside ``parent`` that scrolls vertically."""
        canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=18)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def resize(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # Match the inner frame to the canvas so wraplength works.
            canvas.itemconfigure(window, width=canvas.winfo_width())

        inner.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def wheel(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        # Bind only while the pointer is over this tab, so the wheel doesn't
        # hijack scrolling elsewhere in the app.
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _statusbar(self):
        bar = ttk.Frame(self.root, style="Ink.TFrame", padding=(20, 8))
        bar.pack(fill="x", side="bottom")
        self.status_left = ttk.Label(bar, text="Ready.", style="MutedInk.TLabel")
        self.status_left.pack(side="left")
        self.status_right = ttk.Label(bar, text="", style="MutedInk.TLabel",
                                      font=MONO)
        self.status_right.pack(side="right")

    # -- Record tab ----------------------------------------------------------
    def _build_record(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")

        left = ttk.Frame(top)
        left.pack(side="left", fill="x", expand=True)
        self.state_label = ttk.Label(left, text="Ready to record",
                                     style="Big.TLabel")
        self.state_label.pack(anchor="w")
        self.meeting_label = ttk.Label(
            left, text="Press record, or connect a calendar to start automatically.",
            style="Muted.TLabel")
        self.meeting_label.pack(anchor="w", pady=(2, 0))

        self.timer_label = ttk.Label(top, text="00:00", style="Live.TLabel",
                                     font=(MONO[0], 24, "bold"))
        self.timer_label.pack(side="right", padx=(0, 16))

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(16, 12))
        self.record_btn = ttk.Button(controls, text="● Start recording",
                                     style="Accent.TButton",
                                     command=self._toggle_record)
        self.record_btn.pack(side="left")
        ttk.Button(controls, text="Open notes folder",
                   command=lambda: open_folder(engine_mod.notes_dir())
                   ).pack(side="left", padx=8)
        ttk.Button(controls, text="Report a problem",
                   command=self._report_problem).pack(side="left")

        ttk.Label(parent, text="LIVE TRANSCRIPT", style="Mono.TLabel").pack(
            anchor="w", pady=(6, 4))
        wrap = tk.Frame(parent, bg=EDGE, padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        self.transcript = tk.Text(wrap, bg=INK, fg=PAPER, insertbackground=AMBER,
                                  font=MONO, wrap="word", relief="flat",
                                  padx=14, pady=12, height=12)
        self.transcript.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrap, command=self.transcript.yview)
        scroll.pack(side="right", fill="y")
        self.transcript.configure(yscrollcommand=scroll.set, state="disabled")
        self.transcript.tag_configure("hint", foreground=MUTED)
        self._say("Silence is ignored — lines appear when someone speaks.", "hint")

        self.result_label = ttk.Label(parent, text="", style="Muted.TLabel",
                                      wraplength=820, justify="left")
        self.result_label.pack(anchor="w", pady=(10, 0))

    def _say(self, text, tag=None):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text + "\n", tag or ())
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _line_from_worker(self, line):
        """Called from the transcription thread — hop onto the Tk thread."""
        self._ui_q.put(lambda: self._say(line))

    def _toggle_record(self):
        if self._busy:
            return
        self._busy = True
        self.record_btn.configure(state="disabled")
        if self.engine.is_listening:
            self._set_state("Saving…", "Summarizing with the local model.")
            threading.Thread(target=self._stop_worker, daemon=True).start()
        else:
            self._set_state("Starting…", "Loading the speech model.")
            threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        try:
            self.engine.start()
        except Exception as e:  # noqa: BLE001 - surface mic/model failures in the UI
            self._ui_q.put(lambda err=e: self._start_failed(err))
            return
        self._ui_q.put(self._started)

    def _started(self):
        self._busy = False
        title = self.engine.event.title if self.engine.event else None
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._say(f"Recording “{title}”." if title else "Recording.", "hint")
        self.record_btn.configure(text="■ Stop & save", style="Stop.TButton",
                                  state="normal")
        self._set_state("● Recording", title or "Manual session — no calendar event.")
        self.result_label.configure(text="")

    def _start_failed(self, err):
        self._busy = False
        self.record_btn.configure(state="normal")
        self._set_state("Ready to record", "Could not start.")
        messagebox.showerror("Could not start recording", str(err))

    def _stop_worker(self):
        result = self.engine.stop_and_save()
        self._ui_q.put(lambda: self._stopped(result))

    def _stopped(self, result):
        self._busy = False
        self.record_btn.configure(text="● Start recording", style="Accent.TButton",
                                  state="normal")
        self._set_state("Ready to record", "Saved. Press record to start another.")
        self.timer_label.configure(text="00:00")

        if not result.get("transcript"):
            self.result_label.configure(text=result.get("error") or "Nothing saved.",
                                        style="Bad.TLabel")
            return
        parts = [f"Saved “{result['title']}” → {os.path.basename(result['transcript'])}"]
        if result.get("summary"):
            parts.append(os.path.basename(result["summary"]))
        if result.get("delivered"):
            parts.append(result["delivered"])
        if result.get("error"):
            parts.append(f"summary failed: {result['error']}")
        self.result_label.configure(text="  ·  ".join(parts), style="Muted.TLabel")
        self._say("— saved —", "hint")

    def _set_state(self, state, detail=None):
        self.state_label.configure(text=state)
        if detail is not None:
            self.meeting_label.configure(text=detail)

    # -- Connections tab -----------------------------------------------------
    def _build_connections(self, parent):
        self.use_provider = tk.StringVar(value=config.CALENDAR_PROVIDER or "")
        self.cards = {}
        names = available_providers()

        if not names:
            self._build_no_connections(parent)
            return

        ttk.Label(parent, text="Connect an account so meetings name themselves, "
                              "recording can start on its own, and notes reach "
                              "attendees.", style="Muted.TLabel",
                  wraplength=840, justify="left").pack(anchor="w")
        ttk.Label(parent, text="Only the summary you choose to send ever leaves "
                              "this machine.", style="Muted.TLabel").pack(
            anchor="w", pady=(2, 14))

        row = ttk.Frame(parent)
        row.pack(fill="x")
        for index, name in enumerate(names):
            self.cards[name] = self._provider_card(row, name, index, len(names))

        ttk.Label(parent, text="SIGN-IN", style="Mono.TLabel").pack(
            anchor="w", pady=(16, 4))
        wrap = tk.Frame(parent, bg=EDGE, padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        self.auth_log = tk.Text(wrap, bg=INK, fg=PAPER, font=MONO, wrap="word",
                                relief="flat", padx=14, pady=12, height=7)
        self.auth_log.pack(fill="both", expand=True)
        self.auth_log.insert("end", "Set up your credentials, then press Connect.\n")
        self.auth_log.configure(state="disabled")

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Open config folder",
                   command=lambda: open_folder(store.config_dir())).pack(side="left")
        ttk.Button(actions, text="Copy sign-in text",
                   command=self._copy_auth_log).pack(side="left", padx=8)
        ttk.Button(actions, text="Setup guide", command=self._show_setup_help
                   ).pack(side="left")

    def _build_no_connections(self, parent):
        """Shown when the build has no calendar/email providers installed.

        Not an error state — it's the honest one. Everything that makes the app
        useful on its own already works; this tab is where the optional,
        network-touching conveniences would appear.
        """
        ttk.Label(parent, text="No accounts to connect — and nothing to leak.",
                  style="Big.TLabel", wraplength=840, justify="left").pack(
            anchor="w")
        ttk.Label(parent,
                  text="This build ships no calendar or email providers, so "
                       "there is no code path here that can reach the network. "
                       "Recording, transcription, summaries and your notes on "
                       "disk are unaffected — they never needed an account.",
                  style="Muted.TLabel", wraplength=840, justify="left").pack(
            anchor="w", pady=(6, 16))

        card = ttk.LabelFrame(parent, text="Available in Vlocalhost Pro",
                              style="Card.TLabelframe", padding=14)
        card.pack(fill="x")
        for line in ("Name each note from the calendar event it belongs to",
                     "Start and stop recording on its own when a meeting runs",
                     "Email the summary to the attendees",
                     "Post the notes back onto the calendar event"):
            ttk.Label(card, text=f"·  {line}", style="Muted.TLabel").pack(
                anchor="w", pady=1)
        ttk.Label(card, text=UPGRADE_URL, style="Mono.TLabel").pack(
            anchor="w", pady=(12, 0))

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(16, 0))
        ttk.Button(actions, text="Open config folder",
                   command=lambda: open_folder(store.config_dir())).pack(side="left")

    def _provider_card(self, parent, name, index=0, total=1):
        card = ttk.LabelFrame(parent, text=provider_label(name),
                              style="Card.TLabelframe", padding=14)
        left = 0 if index == 0 else 10
        right = 0 if index == total - 1 else 10
        card.pack(side="left", fill="both", expand=True, padx=(left, right))

        status = ttk.Label(card, text="checking…", style="Muted.TLabel")
        status.pack(anchor="w")
        creds = ttk.Label(card, text="", style="Mono.TLabel", wraplength=360,
                          justify="left")
        creds.pack(anchor="w", pady=(4, 10))

        buttons = ttk.Frame(card)
        buttons.pack(anchor="w")
        setup = ttk.Button(buttons, text="1 · Add credentials",
                           command=lambda: self._setup_credentials(name))
        setup.pack(side="left")
        connect = ttk.Button(buttons, text="2 · Connect",
                             command=lambda: self._connect(name))
        connect.pack(side="left", padx=6)
        disconnect = ttk.Button(buttons, text="Disconnect",
                                command=lambda: self._disconnect(name))
        disconnect.pack(side="left")

        use = ttk.Radiobutton(card, text="Use this account", value=name,
                              variable=self.use_provider,
                              command=self._provider_changed)
        use.pack(anchor="w", pady=(10, 0))
        return {"status": status, "creds": creds, "connect": connect,
                "disconnect": disconnect, "setup": setup}

    def _provider_for(self, name):
        """A provider instance for status/auth, or (None, error)."""
        from integrations import get_provider

        try:
            return get_provider(name), ""
        except Exception as e:  # noqa: BLE001 - missing optional packages
            return None, str(e)

    def _refresh_connections(self):
        for name, card in self.cards.items():
            provider, err = self._provider_for(name)
            if provider is None:
                card["status"].configure(text="unavailable", style="Bad.TLabel")
                card["creds"].configure(text=err)
                for key in ("connect", "disconnect", "setup"):
                    card[key].configure(state="disabled")
                continue
            has_creds = provider.has_client_credentials()
            connected = has_creds and provider.is_authenticated()
            card["status"].configure(
                text="● Connected" if connected else "○ Not connected",
                style="Good.TLabel" if connected else "Muted.TLabel")
            card["creds"].configure(
                text="OAuth app credentials found." if has_creds
                else "Step 1 needed — add your own OAuth app credentials.")
            card["setup"].configure(state="normal")
            card["connect"].configure(state="normal" if has_creds else "disabled",
                                      text="2 · Reconnect" if connected
                                      else "2 · Connect")
            card["disconnect"].configure(state="normal" if connected else "disabled")
        self._refresh_status_right()

    def _setup_credentials(self, name):
        """Collect a provider's own OAuth app credentials.

        The provider describes the prompt it needs (a file, or a line of text)
        and validates whatever comes back, so this window never has to know
        which service it is talking to.
        """
        provider, err = self._provider_for(name)
        if provider is None:
            messagebox.showerror("Unavailable", err)
            return

        spec = provider.credential_setup()
        if spec is None:
            self._log_auth(f"{provider_label(name)} needs no credentials — "
                           "press Connect.")
            return

        if spec.kind == "file":
            value = filedialog.askopenfilename(
                title=spec.title, filetypes=list(spec.file_types))
        else:
            value = simpledialog.askstring(spec.title, spec.prompt,
                                           parent=self.root)
        if not value:
            return

        try:
            self._log_auth(provider.save_client_credentials(value))
        except Exception as e:  # noqa: BLE001 - bad file, bad id, unwritable dir
            messagebox.showerror("Could not save credentials", str(e))
            return
        self._refresh_connections()

    def _connect(self, name):
        provider, err = self._provider_for(name)
        if provider is None:
            messagebox.showerror("Unavailable", err)
            return
        self.cards[name]["connect"].configure(state="disabled")
        self._log_auth(f"\nConnecting {name}…")

        def worker():
            try:
                provider.authenticate(
                    interactive=True,
                    on_prompt=lambda msg: self._ui_q.put(
                        lambda m=msg: self._log_auth(m)))
            except Exception as e:  # noqa: BLE001 - report any auth failure
                self._ui_q.put(lambda err=e: self._connect_done(name, str(err)))
                return
            self._ui_q.put(lambda: self._connect_done(name, None))

        threading.Thread(target=worker, daemon=True).start()

    def _connect_done(self, name, error):
        if error:
            self._log_auth(f"✗ {error}")
        else:
            self._log_auth(f"✓ Connected {name}.")
            if not self.use_provider.get():
                # First account connected — make it the one the app uses.
                self.use_provider.set(name)
                self._provider_changed()
        self._refresh_connections()

    def _disconnect(self, name):
        provider, err = self._provider_for(name)
        if provider is None:
            return
        if not messagebox.askyesno(
                "Disconnect", f"Forget the signed-in {name} account?\n\n"
                              "Your OAuth app credentials are kept, so you can "
                              "reconnect with one click."):
            return
        try:
            provider.disconnect()
            self._log_auth(f"Disconnected {name}.")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Could not disconnect", str(e))
        if self.use_provider.get() == name:
            self.use_provider.set("")
            self._provider_changed()
        self._refresh_connections()

    def _provider_changed(self):
        value = self.use_provider.get() or None
        settings.save(CALENDAR_PROVIDER=value)
        self.engine.reload_provider()
        self._log_auth(f"App is now using: {value or 'no account (local only)'}")
        self._refresh_status_right()

    def _log_auth(self, message):
        self.auth_log.configure(state="normal")
        self.auth_log.insert("end", message.strip() + "\n")
        self.auth_log.see("end")
        self.auth_log.configure(state="disabled")

    def _copy_auth_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.auth_log.get("1.0", "end").strip())
        self.status_left.configure(text="Sign-in text copied to the clipboard.")

    def _show_setup_help(self):
        """Each installed provider supplies its own console walkthrough."""
        sections = []
        for name in available_providers():
            provider, _ = self._provider_for(name)
            spec = provider.credential_setup() if provider else None
            if spec and spec.help_text:
                sections.append(f"{provider_label(name).upper()}\n"
                                f"{spec.help_text}")
        if not sections:
            messagebox.showinfo(
                "One-time credential setup",
                "No providers in this build need credentials.")
            return
        messagebox.showinfo(
            "One-time credential setup",
            "This app is yours to distribute, so each user brings their own "
            "OAuth app — no shared cloud service in the middle.\n\n"
            + "\n\n".join(sections))

    # -- Settings tab --------------------------------------------------------
    def _build_settings(self, parent):
        current = settings.current()

        capture = ttk.LabelFrame(parent, text="What to listen to",
                                 style="Card.TLabelframe", padding=14)
        capture.pack(fill="x")
        self.capture_var = tk.StringVar(value=current.get("CAPTURE_MODE") or "mic")
        for value, text in (
            ("both", "My microphone and the meeting audio  —  both sides of a "
                     "video call, labelled “You” and “Participants”"),
            ("mic", "My microphone only  —  in-person meetings"),
            ("system", "Meeting audio only  —  a webinar or a call I'm just "
                       "listening to"),
        ):
            ttk.Radiobutton(capture, text=text, value=value,
                            variable=self.capture_var,
                            command=self._capture_changed).pack(anchor="w", pady=2)
        self.loopback_label = ttk.Label(capture, text="", style="Muted.TLabel",
                                        wraplength=820, justify="left")
        self.loopback_label.pack(anchor="w", pady=(8, 0))

        auto = ttk.LabelFrame(parent, text="While a meeting is on your calendar",
                              style="Card.TLabelframe", padding=14)
        auto.pack(fill="x", pady=(14, 0))
        self.vars = {}
        for key, text in (
            ("AUTO_START_FROM_CALENDAR",
             "Start and stop recording automatically around scheduled meetings"),
            ("EMAIL_SUMMARY_TO_ATTENDEES",
             "Email the notes to attendees when a meeting is saved"),
            ("EMAIL_SUMMARY_TO_SELF", "Always send a copy to me"),
            ("POST_NOTES_TO_EVENT", "Write the notes back onto the calendar event"),
        ):
            var = tk.BooleanVar(value=bool(current.get(key)))
            self.vars[key] = var
            ttk.Checkbutton(auto, text=text, variable=var,
                            command=lambda k=key: self._toggle(k)).pack(anchor="w",
                                                                       pady=2)
        ttk.Label(auto, text="These need a connected account (Connections tab).",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

        speed = ttk.LabelFrame(parent, text="Performance",
                               style="Card.TLabelframe", padding=14)
        speed.pack(fill="x", pady=14)
        self.profile_var = tk.StringVar(value=performance.current())
        row = ttk.Frame(speed)
        row.pack(anchor="w")
        for name in ("light", "balanced", "accurate"):
            profile = performance.PROFILES[name]
            ttk.Radiobutton(row, text=f"{profile['label']}  (~{profile['ram_mb']} MB)",
                            value=name, variable=self.profile_var,
                            command=self._profile_changed).pack(side="left",
                                                                padx=(0, 18))
        self.profile_label = ttk.Label(speed, text="", style="Muted.TLabel",
                                       wraplength=820, justify="left")
        self.profile_label.pack(anchor="w", pady=(8, 0))

        self.release_var = tk.BooleanVar(
            value=bool(current.get("RELEASE_MODEL_WHEN_IDLE", True)))
        ttk.Checkbutton(speed, text="Free the model's memory between recordings "
                                    "(idles at ~60 MB, costs a few seconds to "
                                    "start)", variable=self.release_var,
                        command=lambda: settings.save(
                            RELEASE_MODEL_WHEN_IDLE=self.release_var.get())
                        ).pack(anchor="w", pady=(8, 0))
        ttk.Button(speed, text="Benchmark this machine",
                   command=self._run_benchmark).pack(anchor="w", pady=(10, 0))

        models = ttk.LabelFrame(parent, text="Models (all local)",
                                style="Card.TLabelframe", padding=14)
        models.pack(fill="x", pady=(0, 14))

        ttk.Label(models, text="Spoken language").grid(row=0, column=0, sticky="w")
        self._lang_labels = {label: code for code, label in languages.choices()}
        self.lang_var = tk.StringVar(
            value=languages.name_for(current.get("WHISPER_LANGUAGE"))
            if current.get("WHISPER_LANGUAGE") else "Auto-detect (any language)")
        lang = ttk.Combobox(models, textvariable=self.lang_var, width=34,
                            state="readonly",
                            values=[label for _, label in languages.choices()])
        lang.grid(row=0, column=1, sticky="w", padx=10, pady=4)
        lang.bind("<<ComboboxSelected>>", lambda _e: self._language_changed())
        ttk.Label(models, text="100 languages; pin one if you know it",
                  style="Muted.TLabel").grid(row=0, column=2, sticky="w")

        ttk.Label(models, text="Speech-to-text").grid(row=1, column=0, sticky="w")
        speech = ttk.Frame(models)
        speech.grid(row=1, column=1, sticky="w", padx=10, pady=4)
        self.whisper_var = tk.StringVar(value=current.get("WHISPER_MODEL"))
        whisper = ttk.Combobox(speech, textvariable=self.whisper_var, width=26,
                               values=["tiny", "base", "small", "medium",
                                       "large-v3", "tiny.en", "base.en",
                                       "small.en", "medium.en"])
        whisper.pack(side="left")
        whisper.bind("<<ComboboxSelected>>", lambda _e: self._language_changed())
        ttk.Button(speech, text="Folder…", width=8,
                   command=self._browse_model).pack(side="left", padx=(6, 0))
        ttk.Label(models, text="“.en” = English only · or a HF id / local folder",
                  style="Muted.TLabel").grid(row=1, column=2, sticky="w")

        self.lang_warning = ttk.Label(models, text="", style="Muted.TLabel",
                                      wraplength=780, justify="left")
        self.lang_warning.grid(row=2, column=0, columnspan=3, sticky="w",
                               pady=(2, 6))

        self.translate_var = tk.BooleanVar(
            value=(current.get("WHISPER_TASK") == "translate"))
        ttk.Checkbutton(models, text="Translate speech to English as it is "
                                     "transcribed", variable=self.translate_var,
                        command=self._language_changed).grid(
            row=3, column=0, columnspan=3, sticky="w")

        self.notes_english_var = tk.BooleanVar(
            value=(current.get("NOTES_LANGUAGE") or "en") != "same")
        ttk.Checkbutton(models, text="Always write the notes in English, "
                                     "whatever was spoken",
                        variable=self.notes_english_var,
                        command=self._language_changed).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Label(models, text="Ollama server").grid(row=5, column=0, sticky="w")
        self.ollama_url_var = tk.StringVar(value=current.get("OLLAMA_URL"))
        ttk.Entry(models, textvariable=self.ollama_url_var, width=36).grid(
            row=5, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(models, text="another machine on your LAN works too",
                  style="Muted.TLabel").grid(row=5, column=2, sticky="w")

        ttk.Label(models, text="Summarization").grid(row=6, column=0, sticky="w")
        summ = ttk.Frame(models)
        summ.grid(row=6, column=1, sticky="w", padx=10, pady=4)
        self.ollama_var = tk.StringVar(value=current.get("OLLAMA_MODEL"))
        # A combobox, not an entry: typing a model name that isn't installed is
        # the single most common way to end up with no summaries and no clue why.
        self.ollama_box = ttk.Combobox(summ, textvariable=self.ollama_var,
                                       width=26)
        self.ollama_box.pack(side="left")
        ttk.Button(summ, text="List…", width=8,
                   command=self._list_ollama_models).pack(side="left", padx=(6, 0))
        self.ollama_label = ttk.Label(models, text="checking Ollama…",
                                      style="Muted.TLabel")
        self.ollama_label.grid(row=6, column=2, sticky="w")

        ttk.Label(models, text="Custom engine").grid(row=7, column=0, sticky="w")
        self.custom_stt_var = tk.StringVar(
            value=current.get("CUSTOM_TRANSCRIBER") or "")
        ttk.Entry(models, textvariable=self.custom_stt_var, width=36).grid(
            row=7, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(models, text="advanced — \"module:ClassName\"; runs code you "
                               "name. Blank = faster-whisper.",
                  style="Muted.TLabel").grid(row=7, column=2, sticky="w")

        buttons = ttk.Frame(models)
        buttons.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(buttons, text="Save models", command=self._save_models).pack(
            side="left")
        ttk.Button(buttons, text="Re-check Ollama",
                   command=lambda: threading.Thread(target=self._check_ollama,
                                                    daemon=True).start()
                   ).pack(side="left", padx=8)
        ttk.Button(buttons, text="Run setup again",
                   command=self._rerun_setup).pack(side="left")
        ttk.Label(models, text="A model or language change applies to the next "
                               "recording.", style="Muted.TLabel").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))

        mcp = ttk.LabelFrame(parent, text="Connect an AI assistant (MCP)",
                             style="Card.TLabelframe", padding=14)
        mcp.pack(fill="x")
        ttk.Label(mcp, text="Claude, Cursor, and other MCP clients can drive this "
                            "app — start a recording, search your notes, check "
                            "what's next on your calendar.",
                  style="Muted.TLabel", wraplength=820, justify="left").pack(
            anchor="w")
        ttk.Button(mcp, text="Copy MCP config", command=self._copy_mcp).pack(
            anchor="w", pady=(10, 0))

        ttk.Label(parent, text=f"Settings file: {settings.path()}",
                  style="Mono.TLabel").pack(anchor="w", pady=(14, 0))

    def _toggle(self, key):
        settings.save(**{key: self.vars[key].get()})
        self.status_left.configure(text=f"Saved: {key.lower().replace('_', ' ')}")

    def _capture_changed(self):
        settings.save(CAPTURE_MODE=self.capture_var.get())
        self.status_left.configure(text="Capture source saved — "
                                        "applies to the next recording.")
        self._check_loopback()

    def _check_loopback(self):
        """Say plainly whether the far end of a call can be captured here."""
        from audio_listener import LoopbackListener

        if self.capture_var.get() == "mic":
            self.loopback_label.configure(
                text="Only your side of a video call will be recorded.",
                style="Muted.TLabel")
            return
        ok, reason = LoopbackListener.available()
        self.loopback_label.configure(
            text=("✓ System audio can be captured — no bot joins your call, "
                  "nothing is uploaded." if ok else f"✗ {reason}"),
            style="Good.TLabel" if ok else "Bad.TLabel")

    def _profile_changed(self):
        """Apply a performance preset and reflect it in the model pickers."""
        name = self.profile_var.get()
        settings.save(**performance.values(name))
        self.whisper_var.set(config.WHISPER_MODEL)
        self.profile_label.configure(text=performance.describe(name),
                                     style="Muted.TLabel")
        self._show_language_warning()
        self._refresh_status_right()

    def _refresh_profile(self):
        name = performance.current()
        self.profile_var.set(name)
        self.profile_label.configure(text=performance.describe(name))

    def _run_benchmark(self):
        """Measure RAM and speed here rather than quoting someone else's laptop."""
        if self.engine.is_listening:
            messagebox.showinfo("Busy", "Stop the recording first — the "
                                        "benchmark loads its own model.")
            return
        self.profile_label.configure(text="Benchmarking… this takes a few seconds.")

        def worker():
            try:
                peak, seconds = performance.benchmark()
            except Exception as e:  # noqa: BLE001
                self._ui_q.put(lambda err=e: self.profile_label.configure(
                    text=f"Benchmark failed: {err}", style="Bad.TLabel"))
                return
            ratio = 10.0 / seconds if seconds else 0
            self._ui_q.put(lambda: self.profile_label.configure(
                text=f"On this machine: ~{peak:.0f} MB peak · "
                     f"{seconds:.1f}s to transcribe 10s of audio "
                     f"({ratio:.1f}× real time). "
                     + ("Comfortable." if ratio >= 1.5 else
                        "Tight — consider the Light profile."),
                style="Good.TLabel" if ratio >= 1.5 else "Bad.TLabel"))

        threading.Thread(target=worker, daemon=True).start()

    def _show_language_warning(self):
        """Check the saved model/language pairing without re-saving it."""
        warning = languages.check(config.WHISPER_MODEL, config.WHISPER_LANGUAGE)
        self.lang_warning.configure(
            text=("⚠ " + warning) if warning else
            "✓ Detected language is shown on each line, so a mistake is visible.",
            style="Bad.TLabel" if warning else "Good.TLabel")

    def _language_changed(self):
        """Save the language choices and warn about a combination that would
        silently produce nonsense (an English-only model on another language)."""
        code = self._lang_labels.get(self.lang_var.get(), languages.AUTO)
        model = self.whisper_var.get().strip()
        settings.save(
            WHISPER_LANGUAGE=languages.normalize(code),
            WHISPER_MODEL=model,
            WHISPER_TASK="translate" if self.translate_var.get() else "transcribe",
            NOTES_LANGUAGE="en" if self.notes_english_var.get() else "same",
        )
        warning = languages.check(model, code)
        self.lang_warning.configure(
            text=("⚠ " + warning) if warning else
            "✓ Detected language is shown on each line, so a mistake is visible.",
            style="Bad.TLabel" if warning else "Good.TLabel")
        self._refresh_status_right()

    def _browse_model(self):
        """Point the speech engine at a converted model folder on disk."""
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Select a converted CTranslate2 / Whisper model folder")
        if chosen:
            self.whisper_var.set(chosen)
            self._language_changed()

    def _list_ollama_models(self):
        """Fill the dropdown with what this Ollama actually has installed."""
        # Read the variable here, on the Tk thread. Touching a tkinter variable
        # from a worker raises "main thread is not in main loop".
        url = self.ollama_url_var.get().strip()

        def worker():
            import setup_wizard

            reachable, names = setup_wizard.ollama_models(url)

            def apply():
                if not reachable:
                    self.ollama_label.configure(
                        text="✗ No Ollama at that address.", style="Bad.TLabel")
                    return
                self.ollama_box.configure(values=names)
                if names:
                    self.ollama_label.configure(
                        text=f"✓ {len(names)} model(s) installed",
                        style="Good.TLabel")
                else:
                    self.ollama_label.configure(
                        text="Ollama is running but has no models yet.",
                        style="Bad.TLabel")
            self._ui_q.put(apply)

        threading.Thread(target=worker, daemon=True).start()

    def _rerun_setup(self):
        """Re-open the first-run wizard, then reflect whatever it saved."""
        import setup_wizard

        setup_wizard.run(self.root)
        settings.apply()
        current = settings.current()
        self.whisper_var.set(current.get("WHISPER_MODEL") or "")
        self.ollama_var.set(current.get("OLLAMA_MODEL") or "")
        self.ollama_url_var.set(current.get("OLLAMA_URL") or "")
        self._refresh_profile()
        self._show_language_warning()
        self._refresh_status_right()
        self.status_left.configure(text="Setup finished.")

    def _save_models(self):
        self._language_changed()   # persists the model too, and re-checks it
        settings.save(
            OLLAMA_MODEL=self.ollama_var.get().strip(),
            OLLAMA_URL=self.ollama_url_var.get().strip() or config.OLLAMA_URL,
            # Blank means "use faster-whisper", which is None, not "".
            CUSTOM_TRANSCRIBER=self.custom_stt_var.get().strip() or None,
        )
        self.status_left.configure(text="Model settings saved.")
        self._refresh_status_right()
        threading.Thread(target=self._check_ollama, daemon=True).start()

    def _check_ollama(self):
        ok, detail = engine_mod.check_ollama()
        self._ui_q.put(lambda: self.ollama_label.configure(
            text=("✓ " if ok else "✗ ") + detail,
            style="Good.TLabel" if ok else "Bad.TLabel"))

    def _copy_mcp(self):
        """Point the assistant at whatever script launched this process.

        Naming mcp_server.py directly would work for a plain Core install and
        silently drop every installed extension for anyone running through a
        launcher — the MCP server would come up without the providers the rest
        of the app has.
        """
        launcher = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        if launcher.endswith(".py") and os.path.isfile(launcher):
            args = [launcher, "--mcp"]
        else:
            args = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "mcp_server.py")]
        blob = json.dumps({"mcpServers": {"vlocalhost": {
            "command": sys.executable, "args": args}}}, indent=2)
        self.root.clipboard_clear()
        self.root.clipboard_append(blob)
        messagebox.showinfo(
            "MCP config copied",
            "Paste this into your assistant's MCP settings "
            "(Claude Desktop: claude_desktop_config.json; Cursor: "
            ".cursor/mcp.json), then restart it:\n\n" + blob)

    # -- housekeeping --------------------------------------------------------
    def _pump(self):
        """Run work queued by background threads on the Tk thread."""
        while True:
            try:
                job = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                job()
            except Exception as e:  # noqa: BLE001 - one bad update must not kill the UI
                print(f"[gui] {e}", flush=True)
        self.root.after(80, self._pump)

    def _tick(self):
        if self.engine.is_listening:
            self.timer_label.configure(text=mmss(self.engine.elapsed))
            self.badge.configure(text="● recording", fg=DANGER)
        else:
            self.badge.configure(text="● on-device", fg=CYAN)
        self.root.after(1000, self._tick)

    def _refresh_status_right(self):
        provider = config.CALENDAR_PROVIDER or "local only"
        lang = config.WHISPER_LANGUAGE or "auto"
        self.status_right.configure(
            text=f"{config.WHISPER_MODEL} · {lang} · {config.OLLAMA_MODEL} "
                 f"· {provider}")

    def _report_problem(self):
        """Write a diagnostic report, show it, and offer to open support."""
        import diagnostics

        try:
            path = diagnostics.save_report()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Report", f"Could not write the report:\n{e}")
            return
        if messagebox.askyesno(
                "Report a problem",
                f"A diagnostic report has been saved:\n\n{path}\n\n"
                "It lists your settings, versions and recent log lines — no "
                "meeting audio, transcripts or notes. Read it before sending.\n\n"
                "Open the support page now?"):
            diagnostics.open_support()
        open_folder(os.path.dirname(path))

    def _on_close(self):
        if self.engine.is_listening and not messagebox.askyesno(
                "Still recording",
                "A recording is in progress. Stop, save the notes, and quit?"):
            return
        self.status_left.configure(text="Saving and closing…")
        self.root.update_idletasks()
        try:
            self.engine.shutdown()
        except Exception as e:  # noqa: BLE001 - never block the quit
            print(f"[gui] shutdown: {e}", flush=True)
        for hook in _exit_hooks:
            try:
                hook(self.root)
            except Exception as e:  # noqa: BLE001 - never block the quit
                name = getattr(hook, "__name__", repr(hook))
                print(f"[gui] exit hook {name}: {e}", flush=True)
        self.root.destroy()


def run():
    """Open the window. Returns when the user closes it."""
    root = tk.Tk()
    app = App(root)
    # Auto-record from the calendar if the user turned it on.
    if config.AUTO_START_FROM_CALENDAR:
        app.engine.start_scheduler(
            on_message=lambda m: app._ui_q.put(lambda: app._log_auth(f"[calendar] {m}")))
    root.mainloop()


if __name__ == "__main__":
    run()
