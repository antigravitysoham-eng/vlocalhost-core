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
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import config
import engine as engine_mod
import notes_panel
import updates
import languages
import performance
import settings
from integrations import UPGRADE_URL, available_providers, provider_label, store
# The brand tokens and fonts. They live in `theme` rather than here so the
# notes panel can paint with them without importing the window that builds it.
from theme import (AMBER, AMBER_DEEP, BODY, CARD, CYAN, DANGER, EDGE, INK,
                   MONO, MUTED, PANEL, PAPER, TITLE)

#: Whether Settings offers the built-in note writer.
#:
#: False until chunked summaries are good enough to stand behind. The engine
#: itself is complete and tested -- see ``summarizer.py`` and ``rolling.py`` --
#: and it is reachable through ``--set SUMMARY_ENGINE=llamacpp`` for anyone
#: developing against it. What is not ready is the answer it gives on a long
#: meeting: below about 4 500 words it is fine, and above that the notes are
#: assembled from parts that measurably lose detail against a single call.
#:
#: One flag rather than deleting the rows, so turning the feature on is a
#: one-line change and a rebuild rather than an archaeology exercise.
SHOW_BUILTIN_NOTES_ENGINE = False

#: Returned by the update worker when the install is sealed, so the result
#: handler can tell "you switched this off" apart from "we could not reach it".
#: A sentinel object rather than a string, because the third possible result is
#: a dict and any stand-in value could one day collide with a real one.
_SEALED = object()

#: Callables run just before the window is destroyed, in registration order.
#: Core registers one of its own (the tip prompt, in :func:`run`); optional
#: packages add theirs from their ``register()``.
_exit_hooks = []


def register_exit_hook(hook):
    """Register *hook* to run just before the window closes.

    ``hook`` is called with the root window and may open a dialog of its
    own; the close waits for it. It runs *after* the engine has shut down
    and the notes are on disk, so nothing a hook does can cost a recording,
    and a hook that raises is reported and skipped — quitting must always
    succeed.

    This is an extension point. Core registers the tip prompt through it; a
    package that wants a word with the user on the way out registers its own
    from ``register()``.
    """
    _exit_hooks.append(hook)


_extra_tabs = []


def register_tab(title, build):
    """Add a tab to the window. Called by an installed package's register().

    ``build(app, frame)`` fills it. Core supplies the notebook and nothing else:
    it does not know what the tab is for, and a build with nothing installed
    shows the three tabs it has always had.

    This is the same rule as every other registry here. A screen that exists to
    set up a paid capability is part of that capability, not part of the
    recorder -- and putting it in Core would mean the free build carrying a
    shopfront for something it does not have.
    """
    _extra_tabs.append((title, build))


def extra_tabs():
    """Registered tabs, in registration order. Empty in a core-only build."""
    return list(_extra_tabs)


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

        self.engine = engine_mod.build(on_line=self._line_from_worker,
                                       on_partial=self._interim_from_worker)
        # Notes are written after the stop returns, so they arrive here rather
        # than as the return value of anything. See _notes_update.
        self.engine.on_notes(self._notes_update)

        root.title("Vlocalhost.AI — Meeting Notes")
        # Wider than it was. The Record tab now holds two panes side by side --
        # the transcript and the notes column -- and at the old 940 the notes
        # were squeezed to about 340px, which wraps an action item's owner onto
        # its own line. The sash is draggable either way.
        root.geometry("1180x720")
        root.minsize(900, 600)
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
        self.root.after(1200, self._update_nudge)
        self.root.after(1000, self._tick)
        self._refresh_connections()
        self._check_loopback()
        self._show_language_warning()
        self._refresh_profile()
        # Before the first probe returns, so the screen never opens showing rows
        # that belong to an engine this install is not running.
        self._show_notes_engine_rows()
        self._start_engine_check()
        self._start_remote_control()

    # -- the hotkey and the second launch ------------------------------------
    def _start_remote_control(self):
        """Listen for the record hotkey, and for a second launch asking to
        toggle.

        Both arrive on threads that are not Tk's, so both go through the same
        queue every background worker in this file already uses. Calling
        ``_toggle_record`` directly from either would be a cross-thread Tk call
        -- which does not raise, it corrupts.

        Started after the widgets exist. A press that arrived before the record
        button was built would find a handler referring to nothing.
        """
        import control
        import hotkey as hotkey_mod

        self._hotkey = None
        self._control = None

        self._hotkey_at = 0.0

        def toggle_from_elsewhere():
            self._ui_q.put(self._toggle_record)

        self._control = control.Server({
            control.TOGGLE: toggle_from_elsewhere,
            control.START: lambda: self._ui_q.put(self._start_if_idle),
            control.STOP: lambda: self._ui_q.put(self._stop_if_recording),
        })
        if not self._control.start():
            # The app is entirely usable without it; only the second press of
            # the hotkey suffers, and it degrades to opening a second window
            # rather than to anything dangerous.
            print(f"[control] {self._control.error}", flush=True)

        if not getattr(config, "HOTKEY_ENABLED", True):
            return

        # The app is the only owner. An earlier version put the chord on a
        # desktop shortcut as well, so the key would also work with the app
        # closed -- and that could never have worked: Explorer binds a
        # shortcut's hotkey the moment the file exists and holds it, so the
        # app's own registration lost every time. One owner, and it is this
        # one; the key works whenever the app is open or in the tray.
        self._hotkey = hotkey_mod.start(config.HOTKEY, self._hotkey_pressed)
        if self._hotkey.error:
            # Said once, in the status bar, not in a dialog: a hotkey that
            # could not be registered is a disappointment, not an emergency,
            # and a modal on startup would be worse than the problem.
            print(f"[hotkey] {self._hotkey.error}", flush=True)
            self._ui_q.put(lambda: self.status_left.configure(
                text=self._hotkey.error))

    def _hotkey_pressed(self):
        """A press arrived. Record that it did, then do the work.

        The timestamp is the point. "Nothing happened" is the failure people
        actually report, and from the outside a chord the OS never delivered
        looks exactly like a chord the app ignored. Settings shows when the
        last press landed, which separates the two without a support thread.
        """
        self._hotkey_at = time.monotonic()
        self._ui_q.put(self._note_hotkey)
        self._ui_q.put(self._toggle_record)

    def _note_hotkey(self):
        label = getattr(self, "hotkey_label", None)
        if label is not None and label.winfo_exists():
            self._show_hotkey_state()

    def _start_if_idle(self):
        if not self.engine.is_listening:
            self._toggle_record()

    def _stop_if_recording(self):
        if self.engine.is_listening:
            self._toggle_record()

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
        # A dropdown has more states than `configure` reaches, and the one the
        # app uses most -- state="readonly", for a list you pick from rather
        # than type into -- is not among them. Left alone, clam paints those in
        # its own pale palette: the language, the notes engine and the model
        # picker all rendered as near-white text on near-white ground, so the
        # current selection could not be read at all. The `map` below is what
        # actually fixes it; `configure` only covers the editable case.
        s.configure("TCombobox", fieldbackground=INK, background=EDGE,
                    foreground=PAPER, arrowcolor=AMBER,
                    selectbackground=INK, selectforeground=PAPER)
        s.map("TCombobox",
              fieldbackground=[("readonly", INK), ("disabled", PANEL)],
              foreground=[("readonly", PAPER), ("disabled", MUTED)],
              # A readonly combobox draws its value as *selected* text, so
              # without these two the highlight repaints it in system colours
              # the moment it takes focus and it disappears again.
              selectbackground=[("readonly", INK), ("focus", INK)],
              selectforeground=[("readonly", PAPER), ("focus", PAPER)],
              background=[("active", EDGE), ("readonly", EDGE)],
              arrowcolor=[("disabled", MUTED)])

        # The open list is a plain Tk listbox owned by Tk, not a ttk widget, so
        # no style reaches it -- it has to be set through the option database
        # before the first one is created.
        for option, value in (
                ("*TCombobox*Listbox.background", PANEL),
                ("*TCombobox*Listbox.foreground", PAPER),
                ("*TCombobox*Listbox.selectBackground", AMBER),
                ("*TCombobox*Listbox.selectForeground", INK),
                ("*TCombobox*Listbox.font", BODY)):
            self.root.option_add(option, value)

        # Tk binds the mouse wheel on a combobox to *change its value*. On a
        # settings page that also scrolls, both happen at once: the page moves
        # and whichever dropdown passed under the pointer silently takes a new
        # value -- and these save on change, so scrolling past the speech model
        # rewrites it to something the user never chose. Found by doing exactly
        # that: a scroll turned "base" into "small" and wrote it to disk.
        #
        # Replace the class binding with a no-op rather than returning "break".
        # The page scroll is a bind_all handler, which runs after the class
        # binding, so swallowing the event would fix the value and break the
        # scrolling instead.
        self.root.bind_class("TCombobox", "<MouseWheel>", lambda _e: None)

    def _header(self):
        bar = ttk.Frame(self.root, style="Ink.TFrame", padding=(20, 14, 20, 6))
        bar.pack(fill="x")
        # The real mark, from the same asset the taskbar and the tray use, so the
        # three cannot drift apart again. The hand-drawn version below is only a
        # fallback for a build with no assets folder -- it approximates the tile
        # and the bars, and it is deliberately not a second source of truth.
        mark = None
        try:
            from PIL import Image, ImageTk

            here = os.path.dirname(os.path.abspath(__file__))
            img = Image.open(os.path.join(here, "assets", "vlocalhost.png"))
            img = img.convert("RGBA").resize((34, 34), Image.LANCZOS)
            self._mark_img = ImageTk.PhotoImage(img)   # keep a ref or Tk drops it
            mark = tk.Label(bar, image=self._mark_img, bg=INK, bd=0)
        except Exception:  # noqa: BLE001 - never fail to draw a window over an icon
            mark = tk.Canvas(bar, width=34, height=34, bg=INK, highlightthickness=0)
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

        # Version and the update check live in the header because that is where
        # someone looks when they are asking "what am I running?". Nothing here
        # contacts anything until the button is pressed -- see updates.py.
        box = ttk.Frame(bar, style="Ink.TFrame")
        box.pack(side="right", padx=(0, 18))
        self.ver_line = tk.Label(box, text=f"v{updates.current()}", bg=INK,
                                 fg=MUTED, font=MONO)
        self.ver_line.pack(anchor="e")
        self.check_btn = tk.Button(
            box, text="Check for updates", command=self._check_updates,
            bg=INK, fg=AMBER, activebackground=INK, activeforeground=AMBER_DEEP,
            relief="flat", bd=0, cursor="hand2", font=(MONO[0], 9, "underline"),
            highlightthickness=0, padx=0, pady=0)
        self.check_btn.pack(anchor="e")

    def _tabs(self):
        # Give installed packages their chance to register before the
        # notebook is built. Everything else in here loads them as a side
        # effect of asking a registry a question; the tab list is asked
        # first, so it has to ask for itself.
        try:
            import plugins

            plugins.load()
        except Exception as e:  # noqa: BLE001 - a plugin never blocks the window
            print(f"[gui] plugins: {e}", flush=True)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(6, 0))
        self.tab_record = ttk.Frame(nb, padding=18)
        self.tab_conn = ttk.Frame(nb, padding=18)
        self.tab_set = ttk.Frame(nb)
        nb.add(self.tab_record, text="Record")
        nb.add(self.tab_conn, text="Connections")
        # Anything an installed package registered. A core-only build has an
        # empty registry and shows the three tabs it always had.
        self.extra_tabs = {}
        for title, build in extra_tabs():
            frame = ttk.Frame(nb, padding=18)
            nb.add(frame, text=title)
            self.extra_tabs[title] = frame
            try:
                build(self, frame)
            except Exception as e:  # noqa: BLE001 - a bad tab is not a dead window
                print(f"[gui] tab {title!r} failed: {e}", flush=True)
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

    # -- Updates -------------------------------------------------------------

    def _update_nudge(self):
        """If the local reminder is due, say so quietly. Sends nothing.

        A label change, never a modal. Interrupting somebody with a dialog they
        did not ask for -- to tell them to go and ask a question -- would be a
        worse thing to do than the problem it solves.
        """
        try:
            if updates.due_for_reminder():
                self.ver_line.config(text=f"v{updates.current()} · check due",
                                     fg=AMBER)
                updates.mark_reminded()
        except Exception:  # noqa: BLE001 - a nudge must never break startup
            pass

    def _check_updates(self):
        """The one place in the app that reaches the network, on a click."""
        self.check_btn.config(state="disabled", text="Checking…")

        def worker():
            import network

            try:
                result = updates.check_now()
            except network.Sealed:
                # Reported separately from offline on purpose. The user chose
                # this; saying "offline" would send them to check their wifi.
                self.root.after(0, self._update_done, _SEALED)
            except updates.CheckFailed:
                self.root.after(0, self._update_done, None)
            except Exception:  # noqa: BLE001
                self.root.after(0, self._update_done, None)
            else:
                self.root.after(0, self._update_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _update_done(self, result):
        self.check_btn.config(state="normal", text="Check for updates")
        if result is _SEALED:
            self.ver_line.config(text=f"v{updates.current()} · sealed", fg=MUTED)
            return
        if result is None:
            # Offline is not an error. Same rule as the calendar integration.
            self.ver_line.config(text=f"v{updates.current()} · offline", fg=MUTED)
            return
        if not result["update"]:
            self.ver_line.config(text=f"v{updates.current()} · up to date",
                                 fg=CYAN)
            return
        self.ver_line.config(text=f"v{updates.current()} → {result['latest']}",
                             fg=AMBER)
        nl = chr(10)
        if messagebox.askyesno(
                "Update available",
                f"You are running {result['current']}.{nl}"
                f"{result['latest']} is available.{nl}{nl}"
                "Open the release page to download it?"):
            import webbrowser

            webbrowser.open(result["url"])

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

        # The two headings, outside the panes rather than inside them. They are
        # the collapse controls, so they have to stay reachable when the pane
        # they name is not on screen -- a control that disappears with the
        # thing it hides cannot bring it back.
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(6, 4))
        self._panes = {}
        self.transcript_toggle = self._pane_toggle(bar, "transcript",
                                                   "LIVE TRANSCRIPT")
        self.notes_toggle = self._pane_toggle(bar, "notes",
                                              "SUMMARY & NEXT STEPS")

        # Two panes: the words on the left, what they came to on the right.
        # They are side by side rather than one after the other because they
        # are now live at the same time -- the notes column is summarising the
        # last meeting while the transcript fills with the next one.
        self.split = ttk.PanedWindow(parent, orient="horizontal")
        self.split.pack(fill="both", expand=True)

        left = ttk.Frame(self.split)
        right = ttk.Frame(self.split)
        self.split.add(left, weight=3)
        self.split.add(right, weight=2)
        # index is where each goes back when it is expanded again; a pane
        # forgotten and re-added lands wherever it is inserted, and the
        # transcript belongs on the left whichever order they were hidden in.
        self._panes["transcript"] = {"frame": left, "index": 0, "open": True}
        self._panes["notes"] = {"frame": right, "index": 1, "open": True}

        wrap = tk.Frame(left, bg=EDGE, padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        self.transcript = tk.Text(wrap, bg=INK, fg=PAPER, insertbackground=AMBER,
                                  font=MONO, wrap="word", relief="flat",
                                  padx=14, pady=12, height=12, width=40)
        self.transcript.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrap, command=self.transcript.yview)
        scroll.pack(side="right", fill="y")
        self.transcript.configure(yscrollcommand=scroll.set, state="disabled")
        self.transcript.tag_configure("hint", foreground=MUTED)
        # Provisional text, shown while the sentence is still being spoken and
        # replaced by the real line a moment later. Dimmed so it reads as "not
        # final yet" rather than as transcript somebody could quote.
        self.transcript.tag_configure("interim", foreground=MUTED,
                                      font=(MONO[0], MONO[1], "italic"))
        self._say("Silence is ignored — lines appear when someone speaks.", "hint")

        # One card per meeting stopped this session: its own progress line, its
        # own notes, its own next-step buttons. See :mod:`notes_panel`.
        self.notes_column = notes_panel.NotesColumn(
            self._scrollable(right),
            submit=lambda fn: threading.Thread(target=fn, daemon=True).start(),
            to_ui=self._ui_q.put,
            open_path=self._open_saved,
            set_clipboard=self._set_clipboard)

    # -- collapsing a pane ---------------------------------------------------
    def _pane_toggle(self, parent, key, label):
        """The heading for one pane, which is also its collapse control."""
        button = tk.Button(
            parent, text=f"▾ {label}", command=lambda: self._toggle_pane(key),
            bg=PANEL, fg=MUTED, activebackground=PANEL, activeforeground=PAPER,
            relief="flat", bd=0, cursor="hand2", font=MONO,
            highlightthickness=0, padx=0, pady=2, anchor="w")
        button.pack(side="left", padx=(0, 22))
        return button

    def _toggle_pane(self, key):
        """Hide or restore one side of the split.

        Collapsing the transcript gives the whole window to the notes, which is
        what you want while reading them; collapsing the notes gives it all to
        the words, which is what you want while a meeting is running. Neither
        is a mode -- both panes keep working either way, and a hidden pane is
        only hidden.
        """
        pane = self._panes[key]
        other = self._panes["notes" if key == "transcript" else "transcript"]
        if pane["open"] and not other["open"]:
            # Refuse rather than leave an empty tab. Open the other one first
            # and the second collapse does what was asked.
            self._toggle_pane("notes" if key == "transcript" else "transcript")

        if pane["open"]:
            self.split.forget(pane["frame"])
            pane["open"] = False
        else:
            # insert() by position, so the transcript returns to the left even
            # if it was the one hidden first.
            where = 0 if pane["index"] == 0 or not other["open"] else "end"
            self.split.insert(where, pane["frame"],
                              weight=3 if key == "transcript" else 2)
            pane["open"] = True
        self._paint_toggles()

    def _paint_toggles(self):
        for key, button in (("transcript", self.transcript_toggle),
                            ("notes", self.notes_toggle)):
            open_ = self._panes[key]["open"]
            label = button.cget("text")[2:]
            button.configure(text=("▾ " if open_ else "▸ ") + label,
                             fg=MUTED if open_ else AMBER)

    def _say(self, text, tag=None):
        self.transcript.configure(state="normal")
        self._drop_interim()          # a real line always replaces the guess
        self.transcript.insert("end", text + "\n", tag or ())
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _drop_interim(self):
        """Remove the provisional line, if one is showing. Caller holds the
        widget in its editable state."""
        span = self.transcript.tag_ranges("interim")
        if span:
            self.transcript.delete(span[0], span[1])

    def _show_interim(self, text, label=""):
        """Words the speaker is still saying: dim, and overwritten in place.

        Always the last thing in the widget, because :meth:`_say` deletes it
        before appending, so the finished line lands where the guess was.
        """
        if not self.engine.is_listening:
            return  # a partial that finished decoding after Stop
        self.transcript.configure(state="normal")
        self._drop_interim()
        who = f"{label}: " if label else ""
        self.transcript.insert("end", f"  {who}{text}\n", ("interim",))
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _line_from_worker(self, line):
        """Called from the transcription thread — hop onto the Tk thread."""
        self._ui_q.put(lambda: self._say(line))

    def _interim_from_worker(self, text, label=""):
        """Provisional text, from the transcription thread."""
        self._ui_q.put(lambda: self._show_interim(text, label))

    def _clear_interim(self):
        self.transcript.configure(state="normal")
        self._drop_interim()
        self.transcript.configure(state="disabled")

    def _toggle_record(self):
        if self._busy:
            return
        self._busy = True
        self.record_btn.configure(state="disabled")
        if self.engine.is_listening:
            self._set_state("Saving…", "Writing the transcript.")
            threading.Thread(target=self._stop_worker, daemon=True).start()
        else:
            self._set_state("Starting…", "Opening the microphone.")
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
        # The model may still be building on a background thread. Say so, rather
        # than let an empty transcript for the first second read as a failure.
        warming = "" if getattr(self.engine, "model_ready", lambda: True)()             else " Transcribing shortly — the speech model is still loading."
        self._say((f"Recording “{title}”." if title else "Recording.") + warming,
                  "hint")
        self.record_btn.configure(text="■ Stop & save", style="Stop.TButton",
                                  state="normal")
        self._set_state("● Recording", title or "Manual session — no calendar event.")

    def _start_failed(self, err):
        self._busy = False
        self.record_btn.configure(state="normal")
        self._set_state("Ready to record", "Could not start.")
        # A failed Start used to leave nothing behind but a dialog the user
        # closed. The log recorded that the app launched and which settings
        # moved, and nothing at all about a recording that never began -- so a
        # report of "no playback device" could not be tied to a capture mode,
        # a device, or an exception type afterwards. That happened, and the
        # only way to investigate was to try to provoke it again live.
        #
        # The device state this depends on is exactly the kind that changes
        # while nobody is looking: headsets connect, a call takes the default
        # endpoint, Windows switches the communications device. Which is why
        # the moment matters and reconstructing it later does not work.
        try:
            import diagnostics

            diagnostics.write(
                "start failed: mode=%s device=%r -- %s: %s"
                % (getattr(config, "CAPTURE_MODE", "?"),
                   getattr(config, "INPUT_DEVICE", None),
                   type(err).__name__, err))
        except Exception:                          # noqa: BLE001 - logging a
            pass                                   # failure must not add one
        messagebox.showerror("Could not start recording", str(err))

    def _stop_worker(self):
        # defer_notes: come back as soon as the words are on disk. Summarising
        # is one to two minutes of local model, and it used to happen inside
        # this call -- which is why the record button stayed dead for all of
        # it. Now it happens on the engine's notes worker and reports into the
        # card that _stopped opens, and the next meeting can start immediately.
        result = self.engine.stop_and_save(defer_notes=True)
        self._ui_q.put(lambda: self._stopped(result))

    def _stopped(self, result):
        self._busy = False
        # Anything still showing as provisional was superseded by the flush
        # that Stop performed; leaving it would put unfinished words at the
        # bottom of a transcript the user is about to read as final.
        self._clear_interim()
        self.record_btn.configure(text="● Start recording", style="Accent.TButton",
                                  state="normal")
        self.timer_label.configure(text="00:00")

        if not result.get("transcript"):
            self._set_state("Ready to record",
                            result.get("error") or "Nothing saved.")
            return

        self._set_state("Ready to record",
                        "Saved. Summarizing on the right — you can start the "
                        "next meeting now.")
        self._say("— saved, summarizing in the background —", "hint")
        # The card itself is opened by _notes_on_ui, on the engine's "queued"
        # announcement, so that a meeting the calendar stopped on its own gets
        # one too rather than only the ones stopped by this button.

    # -- notes arriving from the background worker ---------------------------
    def _notes_update(self, job, state, result):
        """Called by the engine's notes worker, mostly not on the Tk thread."""
        self._ui_q.put(lambda: self._notes_on_ui(job, state, result))

    def _notes_on_ui(self, job, state, result):
        if state == "queued":
            self.notes_column.add(job, title=(result or {}).get("title") or "",
                                  transcript_path=(result or {}).get("transcript") or "",
                                  duration=(result or {}).get("recorded") or 0)
            self.notes_column.renumber()
            return
        card = self.notes_column.card(job)
        if card is None:
            return
        if state == "running":
            card.set_running()
        elif state == "done":
            card.set_done(result, actions_for=self._available_actions)
        elif state == "failed":
            card.set_failed(result.get("error") or "The summary step failed.",
                            transcript_path=result.get("transcript"))
        self.notes_column.renumber()
        self._refresh_status_right()

    @staticmethod
    def _available_actions(meeting):
        import actions as actions_mod

        return actions_mod.available_actions(meeting)

    def _open_saved(self, path):
        """Open one saved file, or its folder if the file has gone."""
        if path and os.path.exists(path):
            try:
                if platform.system() == "Windows":
                    os.startfile(path)  # noqa: S606 - a file this app wrote
                elif platform.system() == "Darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
                return
            except OSError as e:
                print(f"[gui] open {path}: {e}", flush=True)
        open_folder(engine_mod.notes_dir())

    def _set_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

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
        # config.py ships "both" — mic and meeting audio. Falling back to
        # anything else here would let the Settings tab disagree with the
        # documented default about which sources a recording uses.
        self.capture_var = tk.StringVar(
            value=current.get("CAPTURE_MODE") or config.CAPTURE_MODE or "both")
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
        # Which microphone. INPUT_DEVICE has always been settable — from the
        # command line, and by editing config.py — but never from here, so a
        # user with two microphones had to change the Windows default or open
        # a terminal to record from the right one.
        mic_row = ttk.Frame(capture)
        mic_row.pack(anchor="w", fill="x", pady=(10, 0))
        ttk.Label(mic_row, text="Microphone").pack(side="left")
        self.mic_var = tk.StringVar()
        self.mic_box = ttk.Combobox(mic_row, textvariable=self.mic_var,
                                    width=46, state="readonly")
        self.mic_box.pack(side="left", padx=(10, 6))
        self.mic_box.bind("<<ComboboxSelected>>", lambda _e: self._mic_changed())
        ttk.Button(mic_row, text="Refresh", width=9,
                   command=lambda: self._refresh_mics(rescan=True, announce=True)
                   ).pack(side="left")
        self._mic_choices = {}
        self._refresh_mics()

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

        keys = ttk.LabelFrame(parent, text="Record without leaving the meeting",
                              style="Card.TLabelframe", padding=14)
        keys.pack(fill="x", pady=(14, 0))
        ttk.Label(keys, text="One key starts recording and transcribing, and "
                             "the same key stops and saves \u2014 while the call, "
                             "not this window, has focus. It works when the app "
                             "is closed too: the shortcut starts it recording.",
                  style="Muted.TLabel", wraplength=820,
                  justify="left").pack(anchor="w")

        row = ttk.Frame(keys)
        row.pack(anchor="w", pady=(10, 0))
        self.hotkey_on_var = tk.BooleanVar(
            value=bool(current.get("HOTKEY_ENABLED", True)))
        ttk.Checkbutton(row, text="Enabled", variable=self.hotkey_on_var,
                        command=self._save_hotkey).pack(side="left")
        self.hotkey_var = tk.StringVar(value=current.get("HOTKEY") or "")
        ttk.Entry(row, textvariable=self.hotkey_var, width=22).pack(
            side="left", padx=(12, 0))
        ttk.Button(row, text="Apply", command=self._save_hotkey).pack(
            side="left", padx=(6, 0))
        self.hotkey_label = ttk.Label(keys, text="", style="Muted.TLabel",
                                      wraplength=820, justify="left")
        self.hotkey_label.pack(anchor="w", pady=(8, 0))
        self._show_hotkey_state()

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
        ttk.Label(models, text="English by default · 100 languages to pick from",
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

        # Who writes the summaries. Two plain choices, because that is the whole
        # decision for almost everybody: use what the app can install itself, or
        # use the Ollama you already have. The engine names -- llamacpp,
        # ctranslate2 -- are a fact about the runtime, not a question a person
        # should have to answer, so they live under Advanced.
        import notes_model as _notes_model
        import summarizer as _summarizer

        ttk.Label(models, text="Summaries by").grid(row=5, column=0, sticky="w")
        choice = ttk.Frame(models)
        choice.grid(row=5, column=1, sticky="w", padx=10, pady=4)
        self.notes_choice_var = tk.StringVar(
            value=("ollama" if (current.get("SUMMARY_ENGINE") or "ollama")
                   == "ollama" else "builtin"))
        # "Built in" is written, tested and not offered. It works, and only up
        # to about half an hour of speech: past roughly 4 500 words the prompt
        # exceeds the model's context window and the meeting comes back with a
        # transcript and no notes at all. `rolling.py` removes that ceiling by
        # summarising in parts, and the parts are measurably worse than one call
        # -- on the same meeting run both ways, 2 of 17 known facts recovered
        # against 5, with "we are about halfway through the agenda" filed as a
        # discussion point.
        #
        # A headline feature that quietly produces nothing after thirty minutes
        # is worse than one that is not there yet, so the row stays hidden until
        # the chunked path is good enough to stand behind. Everything below it
        # still works: SUMMARY_ENGINE and NOTES_MODEL remain editable through
        # --set, so this is hidden from the screen, not removed from the build.
        if SHOW_BUILTIN_NOTES_ENGINE:
            ttk.Radiobutton(choice, text="Built in", value="builtin",
                            variable=self.notes_choice_var,
                            command=self._notes_choice_changed).pack(side="left")
        ttk.Radiobutton(choice, text="Ollama", value="ollama",
                        variable=self.notes_choice_var,
                        command=self._notes_choice_changed).pack(
            side="left", padx=(14, 0) if SHOW_BUILTIN_NOTES_ENGINE else (0, 0))
        self.notes_status = ttk.Label(models, text="checking the notes engine...",
                                      style="Muted.TLabel", wraplength=300,
                                      justify="left")
        self.notes_status.grid(row=5, column=2, sticky="w")

        # The download, offered only when it is the piece that is missing.
        get_label = ttk.Label(models, text="Notes model")
        get_label.grid(row=6, column=0, sticky="w")
        get = ttk.Frame(models)
        get.grid(row=6, column=1, sticky="w", padx=10, pady=4)
        self.get_model_btn = ttk.Button(
            get, text="Download (%d MB)" % _notes_model.DEFAULT.size_mb,
            command=self._download_notes_model)
        self.get_model_btn.pack(side="left")
        self.get_model_bar = ttk.Progressbar(get, mode="determinate",
                                             maximum=1000, length=140)
        # The terms, before the download rather than after it. One line, and a
        # link, because "Apache-2.0" means nothing to most people and the point
        # is that they can go and look.
        self._licence_link(get, _notes_model.DEFAULT.licence,
                           _notes_model.DEFAULT.licence_url).pack(side="left",
                                                                  padx=(10, 0))
        self.get_progress = tk.StringVar(
            value="%s - one download, nothing else to install"
                  % _notes_model.DEFAULT.label)
        get_hint = ttk.Label(models, textvariable=self.get_progress,
                             style="Muted.TLabel", wraplength=300,
                             justify="left")
        get_hint.grid(row=6, column=2, sticky="w")
        self._download_rows = (get_label, get, get_hint)

        # The same row, in its other state: the model is here and the only
        # thing left to offer is getting rid of it. A download this size should
        # be removable from the screen that put it there, not by hunting for a
        # folder.
        have_label = ttk.Label(models, text="Notes model")
        have_label.grid(row=7, column=0, sticky="w")
        have = ttk.Frame(models)
        have.grid(row=7, column=1, sticky="w", padx=10, pady=4)
        self.remove_model_btn = ttk.Button(have, text="Remove download",
                                           command=self._remove_notes_model)
        self.remove_model_btn.pack(side="left")
        self.have_model_var = tk.StringVar(value="")
        have_hint = ttk.Label(models, textvariable=self.have_model_var,
                              style="Muted.TLabel", wraplength=300,
                              justify="left")
        have_hint.grid(row=7, column=2, sticky="w")
        self._remove_rows = (have_label, have, have_hint)

        ollama_url_label = ttk.Label(models, text="Ollama server")
        ollama_url_label.grid(row=8, column=0, sticky="w")
        self.ollama_url_var = tk.StringVar(value=current.get("OLLAMA_URL"))
        ollama_url_entry = ttk.Entry(models, textvariable=self.ollama_url_var,
                                     width=36)
        ollama_url_entry.grid(row=8, column=1, sticky="w", padx=10, pady=4)
        ollama_url_hint = ttk.Label(models,
                                    text="another machine on your LAN works too",
                                    style="Muted.TLabel")
        ollama_url_hint.grid(row=8, column=2, sticky="w")

        summ_label = ttk.Label(models, text="Summarization")
        summ_label.grid(row=9, column=0, sticky="w")
        summ = ttk.Frame(models)
        summ.grid(row=9, column=1, sticky="w", padx=10, pady=4)
        self.ollama_var = tk.StringVar(value=current.get("OLLAMA_MODEL"))
        # A combobox, not an entry: typing a model name that isn't installed is
        # the single most common way to end up with no summaries and no clue why.
        self.ollama_box = ttk.Combobox(summ, textvariable=self.ollama_var,
                                       width=26)
        self.ollama_box.pack(side="left")
        ttk.Button(summ, text="List...", width=8,
                   command=self._list_ollama_models).pack(side="left", padx=(6, 0))
        self.ollama_label = ttk.Label(models, text="", style="Muted.TLabel")
        self.ollama_label.grid(row=9, column=2, sticky="w")

        self.advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(models, text="Advanced - pick the engine and the model "
                                     "file yourself",
                        variable=self.advanced_var,
                        command=self._show_notes_engine_rows).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(8, 2))

        engine_label = ttk.Label(models, text="Engine")
        engine_label.grid(row=11, column=0, sticky="w")
        self.notes_engine_var = tk.StringVar(
            value=(current.get("SUMMARY_ENGINE") or "ollama"))
        engine_box = ttk.Combobox(models, textvariable=self.notes_engine_var,
                                  width=34, state="readonly",
                                  values=_summarizer.engine_names())
        engine_box.grid(row=11, column=1, sticky="w", padx=10, pady=4)
        engine_box.bind("<<ComboboxSelected>>",
                        lambda _e: self._notes_engine_changed())
        engine_hint = ttk.Label(models, text="which runtime writes the notes",
                                style="Muted.TLabel")
        engine_hint.grid(row=11, column=2, sticky="w")

        notes_model_label = ttk.Label(models, text="Model file")
        notes_model_label.grid(row=12, column=0, sticky="w")
        notes_model_row = ttk.Frame(models)
        notes_model_row.grid(row=12, column=1, sticky="w", padx=10, pady=4)
        self.notes_model_var = tk.StringVar(value=current.get("NOTES_MODEL") or "")
        ttk.Entry(notes_model_row, textvariable=self.notes_model_var,
                  width=26).pack(side="left")
        ttk.Button(notes_model_row, text="Browse...", width=8,
                   command=self._browse_notes_model).pack(side="left", padx=(6, 0))
        notes_model_hint = ttk.Label(
            models, text="leave blank to use the downloaded or bundled copy",
            style="Muted.TLabel", wraplength=300, justify="left")
        notes_model_hint.grid(row=12, column=2, sticky="w")

        # Rows that belong to one choice and mislead under the other.
        self._ollama_rows = (ollama_url_label, ollama_url_entry, ollama_url_hint,
                             summ_label, summ, self.ollama_label)
        self._advanced_rows = (engine_label, engine_box, engine_hint,
                               notes_model_label, notes_model_row,
                               notes_model_hint)

        ttk.Label(models, text="Custom engine").grid(row=13, column=0, sticky="w")
        self.custom_stt_var = tk.StringVar(
            value=current.get("CUSTOM_TRANSCRIBER") or "")
        ttk.Entry(models, textvariable=self.custom_stt_var, width=36).grid(
            row=13, column=1, sticky="w", padx=10, pady=4)
        ttk.Label(models, text="advanced — \"module:ClassName\"; runs code you "
                               "name. Blank = faster-whisper.",
                  style="Muted.TLabel").grid(row=13, column=2, sticky="w")

        buttons = ttk.Frame(models)
        buttons.grid(row=14, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(buttons, text="Save models", command=self._save_models).pack(
            side="left")
        ttk.Button(buttons, text="Re-check engine",
                   command=self._start_engine_check
                   ).pack(side="left", padx=8)
        ttk.Button(buttons, text="Run setup again",
                   command=self._rerun_setup).pack(side="left")
        ttk.Label(models, text="A model or language change applies to the next "
                               "recording.", style="Muted.TLabel").grid(
            row=15, column=0, columnspan=3, sticky="w", pady=(8, 0))

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

        helpbox = ttk.LabelFrame(parent, text="Help", style="Card.TLabelframe",
                                 padding=14)
        helpbox.pack(fill="x", pady=(14, 0))
        ttk.Label(helpbox, text="Both guides ship with the app, so they work "
                                "with no internet. The installation guide "
                                "covers each platform, first run and what "
                                "every error message means. The summaries "
                                "guide covers both ways to get a notes model "
                                "— the one-button download and Ollama — and "
                                "why a meeting can save a transcript and no "
                                "notes.",
                  style="Muted.TLabel", wraplength=820, justify="left").pack(
            anchor="w")
        help_buttons = ttk.Frame(helpbox)
        help_buttons.pack(anchor="w", pady=(10, 0))
        ttk.Button(help_buttons, text="Installation guide",
                   command=self._open_guide).pack(side="left")
        ttk.Button(help_buttons, text="Summaries setup guide",
                   command=self._open_summaries_guide).pack(side="left",
                                                            padx=8)
        ttk.Button(help_buttons, text="Support page",
                   command=self._open_support).pack(side="left")

        ttk.Label(parent, text=f"Settings file: {settings.path()}",
                  style="Mono.TLabel").pack(anchor="w", pady=(14, 0))

    # diagnostics is imported where it is used, as everywhere else in this
    # file — it pulls in platform and traceback, and startup does not need it.
    def _open_doc(self, kind):
        """Open a bundled PDF, or say where it went if that is not possible."""
        import diagnostics

        label = diagnostics.DOCS[kind][3]
        if diagnostics.open_doc(kind):
            self.status_left.configure(text=f"Opened the {label}.")
        else:
            path = diagnostics.doc_path(kind)
            # A file exists but nothing would open it — name it, so the user
            # can reach it themselves rather than press the button again.
            self.status_left.configure(
                text=f"Guide: {path}" if path
                else f"No {label} in this copy — opened the online one.")

    def _open_guide(self):
        self._open_doc("guide")

    def _open_summaries_guide(self):
        self._open_doc("summaries")

    def _open_support(self):
        import diagnostics

        diagnostics.open_support()

    def _show_hotkey_state(self):
        """Say what the hotkey is doing, in the one place someone would look.

        Three things worth distinguishing, because the fix differs for each:
        switched off, a chord that cannot be registered, and a chord another
        application already holds.
        """
        import hotkey as hotkey_mod

        if not self.hotkey_on_var.get():
            self.hotkey_label.configure(
                text="Off. Recording still starts from the button above and "
                     "from the tray.", style="Muted.TLabel")
            return
        handle = getattr(self, "_hotkey", None)
        if handle is not None and handle.error:
            self.hotkey_label.configure(text=handle.error, style="Bad.TLabel")
            return
        try:
            chord = hotkey_mod.pretty(self.hotkey_var.get())
            hotkey_mod.parse(self.hotkey_var.get())
        except hotkey_mod.ChordError as e:
            self.hotkey_label.configure(text=str(e), style="Bad.TLabel")
            return
        seen = getattr(self, "_hotkey_at", 0.0)
        if seen:
            ago = max(0, int(time.monotonic() - seen))
            when = "just now" if ago < 3 else f"{ago}s ago"
            note = f" \u2014 last pressed {when}."
        else:
            note = ". Press it to check it reaches the app."
        self.hotkey_label.configure(
            text=f"\u2713 {chord} starts and stops a recording{note}",
            style="Good.TLabel")

    def _save_hotkey(self):
        """Save the chord and re-register it, without a restart.

        Re-registering live is the whole reason ``Hotkey.stop`` releases the
        chord: somebody trying to find a combination their machine has free
        should be able to try three of them in ten seconds, not restart twice.
        """
        import hotkey as hotkey_mod

        chord = self.hotkey_var.get().strip()
        enabled = bool(self.hotkey_on_var.get())
        if enabled and chord:
            try:
                hotkey_mod.parse(chord)
            except hotkey_mod.ChordError as e:
                # Refused before saving: a stored chord that cannot work would
                # come back broken on the next launch with no clue why.
                self.hotkey_label.configure(text=str(e), style="Bad.TLabel")
                return

        settings.save(HOTKEY=chord or config.HOTKEY, HOTKEY_ENABLED=enabled)
        settings.apply()

        handle = getattr(self, "_hotkey", None)
        if handle is not None:
            handle.stop()
            self._hotkey = None
        if enabled:
            self._hotkey = hotkey_mod.start(config.HOTKEY,
                                            self._hotkey_pressed)
        self._hotkey_at = 0.0        # a new chord has not been pressed yet
        self._show_hotkey_state()
        self.status_left.configure(text="Hotkey saved.")

    def _toggle(self, key):
        settings.save(**{key: self.vars[key].get()})
        self.status_left.configure(text=f"Saved: {key.lower().replace('_', ' ')}")

    def _capture_changed(self):
        settings.save(CAPTURE_MODE=self.capture_var.get())
        self.status_left.configure(text="Capture source saved — "
                                        "applies to the next recording.")
        self._check_loopback()

    _MIC_DEFAULT = "System default"

    def _refresh_mics(self, rescan=False, announce=False):
        """Fill the microphone list from the hardware attached right now.

        ``rescan`` makes PortAudio enumerate again, which is the only way a
        headset connected after the app opened becomes visible.
        """
        try:
            from audio_listener import input_devices

            devices = input_devices(refresh=rescan)
        except Exception as e:  # noqa: BLE001 - no PortAudio is not a crash
            self._mic_choices = {self._MIC_DEFAULT: None}
            self.mic_box.configure(values=[self._MIC_DEFAULT], state="disabled")
            self.mic_var.set(self._MIC_DEFAULT)
            print(f"[audio] microphone list unavailable: {e}", flush=True)
            return

        default_name = next((d["name"] for d in devices if d["default"]), "")
        label_default = (f"{self._MIC_DEFAULT}  ({default_name})"
                         if default_name else self._MIC_DEFAULT)
        # Stored as None, so the setting keeps following the system default
        # rather than pinning today's default device by name.
        choices = {label_default: None}
        for device in devices:
            choices[device["name"]] = device["name"]

        saved = getattr(config, "INPUT_DEVICE", None)
        if saved in (None, ""):
            selected = label_default
        else:
            selected = next((label for label, value in choices.items()
                             if value is not None
                             and str(value).lower() == str(saved).lower()), None)
            if selected is None:
                # Configured but not plugged in. Show it rather than silently
                # snapping back to the default — the setting is still in force,
                # and Start will say so plainly.
                selected = f"{saved}  — not connected"
                choices[selected] = saved

        self._mic_choices = choices
        self.mic_box.configure(values=list(choices), state="readonly")
        self.mic_var.set(selected)
        if announce and hasattr(self, "status_left"):
            self.status_left.configure(
                text=f"Found {len(devices)} microphone(s).")

    def _mic_changed(self):
        settings.save(INPUT_DEVICE=self._mic_choices.get(self.mic_var.get()))
        self.status_left.configure(text="Microphone saved — applies to the "
                                        "next recording.")

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

    @staticmethod
    def _language_ok_text(code):
        """What to say when the model/language pairing is sound. A pinned
        language gets no per-line tag, so promising one would be a lie."""
        if languages.normalize(code) is None:
            return ("✓ Detected language is shown on each line, so a mistake "
                    "is visible.")
        return (f"✓ Everything is transcribed as {languages.name_for(code)}. "
                "Pick Auto-detect for a meeting that switches languages.")

    def _show_language_warning(self):
        """Check the saved model/language pairing without re-saving it."""
        warning = languages.check(config.WHISPER_MODEL, config.WHISPER_LANGUAGE)
        self.lang_warning.configure(
            text=("⚠ " + warning) if warning
            else self._language_ok_text(config.WHISPER_LANGUAGE),
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
            text=("⚠ " + warning) if warning else self._language_ok_text(code),
            style="Bad.TLabel" if warning else "Good.TLabel")
        self._refresh_status_right()

    def _licence_link(self, parent, label, url):
        """A small underlined licence name that opens the terms in a browser.

        Matches the "Check for updates" link rather than inventing a second
        kind of link: the app already has one way of saying "this is clickable
        and it leaves the window".
        """
        import webbrowser

        widget = tk.Button(
            parent, text=label, bg=PANEL, fg=MUTED, activebackground=PANEL,
            activeforeground=AMBER, relief="flat", bd=0, cursor="hand2",
            font=(MONO[0], 8, "underline"), highlightthickness=0,
            padx=0, pady=0,
            command=lambda: webbrowser.open(url))
        return widget

    def _show_notes_engine_rows(self):
        """Show only the rows that mean something right now.

        Three things decide the shape of this section, and none of them should
        need explaining to the person reading it:

        * Ollama's server and model rows do nothing under the built-in engine --
          editing them leaves the notes byte-identical -- so they go away.
        * The download button is only useful while the model is missing. Once
          the weights are here, or the install shipped with them, it is noise.
        * Engine and model-file are for somebody who has a reason. Off by
          default, and nothing above depends on them being visible.
        """
        import notes_model

        builtin = self.notes_choice_var.get() == "builtin"
        for widget in self._ollama_rows:
            widget.grid_remove() if builtin else widget.grid()
        show_download = builtin and not notes_model.present()
        for widget in self._download_rows:
            widget.grid() if show_download else widget.grid_remove()

        # Offer removal only for a copy we downloaded. A bundled model belongs
        # to the installer and a path the user set belongs to them -- deleting
        # either from here would be taking something that is not ours.
        ours = notes_model.source() == "downloaded by this app"
        for widget in self._remove_rows:
            widget.grid() if (builtin and ours) else widget.grid_remove()
        if builtin and ours:
            self.have_model_var.set(
                "%s · %d MB in your user folder"
                % (notes_model.DEFAULT.label,
                   notes_model.downloaded_size_mb()))
        for widget in self._advanced_rows:
            widget.grid() if self.advanced_var.get() else widget.grid_remove()

    def _notes_choice_changed(self):
        """Turn the two-way choice into the engine setting behind it.

        "Built in" means whichever embedded engine ships with this release, so
        the pairing lives with the model spec rather than being spelled out in
        the UI. Anyone who picked a different embedded engine under Advanced
        keeps it -- switching to "Built in" from ctranslate2 should not quietly
        move them to llamacpp.
        """
        import notes_model
        import summarizer

        if self.notes_choice_var.get() == "ollama":
            engine = "ollama"
        elif summarizer.needs_model(self.notes_engine_var.get()):
            engine = self.notes_engine_var.get()
        else:
            engine = notes_model.DEFAULT.engine
        self.notes_engine_var.set(engine)
        settings.save(SUMMARY_ENGINE=engine)
        self._show_notes_engine_rows()
        self._refresh_status_right()
        self._start_engine_check()

    def _notes_engine_changed(self):
        """The Advanced engine picker. Keeps the plain choice above in step."""
        import summarizer

        engine = self.notes_engine_var.get().strip() or "ollama"
        settings.save(SUMMARY_ENGINE=engine)
        self.notes_choice_var.set(
            "builtin" if summarizer.needs_model(engine) else "ollama")
        self._show_notes_engine_rows()
        self._refresh_status_right()
        self._start_engine_check()

    def _remove_notes_model(self):
        """Delete the model this app downloaded, after asking.

        Only our own copy, and only with a confirmation: it is a gigabyte and
        getting it back means downloading it again. The engine cache is dropped
        afterwards so the next summary does not try to load a file that is no
        longer there.
        """
        import notes_model

        size = notes_model.downloaded_size_mb()
        if not messagebox.askyesno(
                "Remove the notes model?",
                "This deletes %s (%d MB) from your user folder.\n\n"
                "Summaries stop working until you download it again or "
                "switch to Ollama. Your recordings, transcripts and saved "
                "notes are not touched."
                % (notes_model.DEFAULT.label, size)):
            return

        removed = notes_model.remove()
        import summarizer
        summarizer._engine = summarizer._engine_key = None
        self.status_left.configure(
            text="Notes model removed." if removed
            else "Nothing to remove.")
        self._show_notes_engine_rows()
        self._refresh_status_right()
        self._start_engine_check()

    def _download_notes_model(self):
        """Fetch the built-in model, showing progress, without freezing the UI.

        The work happens on a worker thread and every widget update is posted
        back through the same queue the rest of this window uses, because
        touching a tkinter variable from a worker raises "main thread is not in
        main loop" -- occasionally, and only on someone else's machine.
        """
        import notes_model

        self.get_model_btn.configure(state="disabled")
        self.get_model_bar.pack(side="left", padx=(8, 0))
        self.get_model_bar.configure(value=0)

        def report(fraction, note):
            def apply():
                if fraction is None:
                    self.get_model_bar.configure(mode="indeterminate")
                else:
                    self.get_model_bar.configure(mode="determinate",
                                                 value=int(fraction * 1000))
                self.get_progress.set(note)
            self._ui_q.put(apply)

        def worker():
            problem = notes_model.download(report)

            def done():
                self.get_model_btn.configure(state="normal")
                self.get_model_bar.pack_forget()
                if problem:
                    self.get_progress.set(problem)
                    self.status_left.configure(text="The download did not finish.")
                else:
                    self.get_progress.set("Ready.")
                    self.status_left.configure(text="Notes model installed.")
                # Rebuild the engine against what is now on disk, and let the
                # rows re-decide whether the download button still belongs.
                import summarizer
                summarizer._engine = summarizer._engine_key = None
                self._show_notes_engine_rows()
                self._refresh_status_right()
                self._start_engine_check()
            self._ui_q.put(done)

        threading.Thread(target=worker, daemon=True).start()

    def _browse_notes_model(self):
        """Find the weights for an embedded engine.

        A folder for CTranslate2 and a file for llama.cpp, because that is what
        each one actually loads -- offering the wrong dialog is how a user ends
        up with a path the engine will refuse.
        """
        if self.notes_engine_var.get() == "ctranslate2":
            chosen = filedialog.askdirectory(
                parent=self.root,
                title="Select a CTranslate2 model folder")
        else:
            chosen = filedialog.askopenfilename(
                parent=self.root, title="Select a .gguf model file",
                filetypes=[("GGUF model", "*.gguf"), ("All files", "*.*")])
        if chosen:
            self.notes_model_var.set(chosen)
            settings.save(NOTES_MODEL=chosen)
            self._show_notes_engine_rows()
            self._refresh_status_right()
            self._start_engine_check()

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
        self.notes_engine_var.set(current.get("SUMMARY_ENGINE") or "ollama")
        self.notes_model_var.set(current.get("NOTES_MODEL") or "")
        self.notes_choice_var.set(
            "ollama" if (current.get("SUMMARY_ENGINE") or "ollama") == "ollama"
            else "builtin")
        self._show_notes_engine_rows()
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
            SUMMARY_ENGINE=self.notes_engine_var.get().strip() or "ollama",
            NOTES_MODEL=self.notes_model_var.get().strip(),
        )
        self.status_left.configure(text="Model settings saved.")
        self._show_notes_engine_rows()
        self._refresh_status_right()
        self._start_engine_check()

    def _start_engine_check(self):
        """Probe the notes engine on a worker, reading Tk state before leaving.

        The read has to happen here. Tcl interpreters are not thread-safe, and
        ``notes_choice_var.get()`` used to be the first statement *inside* the
        worker -- it raised outright the first time this screen was driven from
        a test harness, and in the running app it was a data race that happened
        to work. Every UI write in ``_check_ollama`` already goes through
        ``self._ui_q`` for exactly this reason; the one read had slipped past
        the rule.

        One helper rather than the same two lines at eight call sites, because
        the first version of this fix passed the value at one of them and left
        the other seven defaulting to "ollama" -- which would have hidden the
        "not downloaded yet" message for the built-in engine.
        """
        try:
            choice = self.notes_choice_var.get()
        except Exception:                          # noqa: BLE001
            choice = "ollama"                      # before the row is built
        threading.Thread(target=self._check_ollama, args=(choice,),
                         daemon=True).start()

    def _check_ollama(self, choice="ollama"):
        """Report on whatever writes the notes -- not necessarily Ollama.

        ``check_ollama`` has been an alias for ``check_notes_engine`` since the
        engine became pluggable, so this line was already telling the truth
        while sitting beside an Ollama-only control that was not. It now sits on
        the notes-engine row, where what it says matches what it is next to.
        """
        import notes_model

        # A model nobody has downloaded yet is not a fault. A red cross beside
        # a Download button reads as "something is broken" when the honest
        # message is "this step has not been done", and the button next to it
        # is already the answer -- so say that instead, quietly.
        if choice is None:                        # a caller that has no value
            choice = "ollama"                     # to give cannot be on a thread
        if choice == "builtin" and not notes_model.present():
            waiting = "Not downloaded yet - about %d MB." % (
                notes_model.DEFAULT.size_mb,)
            self._ui_q.put(lambda: self.notes_status.configure(
                text=waiting, style="Muted.TLabel"))
            return

        ok, detail = engine_mod.check_ollama()
        self._ui_q.put(lambda: self.notes_status.configure(
            text=("✓ " if ok else "✗ ") + detail,
            style="Good.TLabel" if ok else "Bad.TLabel"))

    def _copy_mcp(self):
        """Point the assistant at whatever script launched this process.

        Naming mcp_server.py directly would work for a plain Core install and
        silently drop every installed extension for anyone running through a
        launcher — the MCP server would come up without the providers the rest
        of the app has.
        """
        import mcp_hosts

        # One source of truth: the Assistants screen an extension may add shows
        # the same block, so the two can never disagree about how to launch us.
        blob = mcp_hosts.standard_block()
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
        import summarizer

        provider = config.CALENDAR_PROVIDER or "local only"
        lang = config.WHISPER_LANGUAGE or "auto"
        # The notes model comes from the engine, not from OLLAMA_MODEL, so this
        # line stays true for whoever is actually writing the notes.
        self.status_right.configure(
            text=f"{config.WHISPER_MODEL} · {lang} · {summarizer.model_label()} "
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
        # Summaries still being written. The transcripts are already on disk,
        # so nothing said is at risk -- but a summary somebody waited two
        # minutes for and then lost by closing the window is its own kind of
        # broken promise, so ask rather than decide for them.
        waiting = self.engine.pending_notes()
        if waiting and not messagebox.askyesno(
                "Still summarizing",
                f"{waiting} meeting{'s are' if waiting > 1 else ' is'} still "
                f"being summarized.\n\nWait for "
                f"{'them' if waiting > 1 else 'it'} to finish and quit?\n\n"
                f"The transcript{'s are' if waiting > 1 else ' is'} already "
                f"saved either way."):
            return
        self.status_left.configure(text="Saving and closing…")
        self.root.update_idletasks()
        # Before the engine, so a hotkey pressed during a slow save cannot
        # queue a toggle against an engine that is shutting down. Both are
        # best-effort: neither may stand between the user and a closed window.
        for name in ("_hotkey", "_control"):
            handle = getattr(self, name, None)
            if handle is not None:
                try:
                    handle.stop()
                except Exception as e:  # noqa: BLE001
                    print(f"[gui] {name}: {e}", flush=True)
        def waiting_for(n):
            self.status_left.configure(
                text=f"Finishing {n} summar{'ies' if n > 1 else 'y'}…")
            self.root.update_idletasks()

        try:
            self.engine.shutdown(on_wait=waiting_for)
        except Exception as e:  # noqa: BLE001 - never block the quit
            print(f"[gui] shutdown: {e}", flush=True)
        for hook in _exit_hooks:
            try:
                hook(self.root)
            except Exception as e:  # noqa: BLE001 - never block the quit
                name = getattr(hook, "__name__", repr(hook))
                print(f"[gui] exit hook {name}: {e}", flush=True)
        self.root.destroy()


def run(record_on_start: bool = False):
    """Open the window. Returns when the user closes it.

    ``record_on_start`` is the hotkey's cold-start path: the key was pressed
    with nothing running, so the window opens and begins recording without
    waiting to be asked twice.
    """
    # The tip prompt is Core's own — it asks on behalf of the free product —
    # but it stays an ordinary hook, registered here rather than wired into
    # the close path, so deleting the module is all it takes to remove it.
    # A failure must never keep the window from opening.
    try:
        import support_prompt

        register_exit_hook(support_prompt.maybe_show)
    except Exception as e:  # noqa: BLE001
        print(f"[gui] support prompt unavailable: {e}", flush=True)

    root = tk.Tk()
    app = App(root)
    if record_on_start:
        # after(), not a direct call: the window has not been drawn yet, and
        # the engine's start path reports progress into widgets that do not
        # exist until Tk has run once. The delay is what makes the hotkey feel
        # like one action rather than a window that appears and then reacts.
        root.after(120, app._start_if_idle)
    # Auto-record from the calendar if the user turned it on.
    if config.AUTO_START_FROM_CALENDAR:
        # defer_notes, like the button: a meeting the calendar ends by itself
        # gets a card that summarises in the background, rather than locking
        # the app up for two minutes at the exact moment the next call starts.
        app.engine.start_scheduler(
            on_message=lambda m: app._ui_q.put(lambda: app._log_auth(f"[calendar] {m}")),
            defer_notes=True)
    root.mainloop()


if __name__ == "__main__":
    run()
