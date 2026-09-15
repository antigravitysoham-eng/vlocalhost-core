"""The notes column: what the window shows once a meeting has stopped.

One card per meeting recorded this session, newest on top, each one writing its
own progress. The card exists from the moment Stop is pressed -- before there
are any notes to put in it -- because the thing it is really reporting is *that
the summary is being written*, and that takes one to two minutes on a local
model. Before this, that minute was a disabled button and a single line reading
"Saving...", and the notes themselves were never shown in the app at all: they
were a file path, and you opened Explorer to read what you had just waited for.

Three things follow from summarising in the background:

* the card has states, not just content -- queued, summarising, done, failed;
* several cards can be in different states at once, because the next meeting
  starts while the last one is still being written;
* the next-step actions belong *to a card*, not to the window, since "the
  meeting you just finished" is no longer a single thing.

The last one is also why an action's output is rendered here rather than
dropped on the clipboard unseen. Pasting a meeting into an assistant that is
not on this machine is an upload. The user should be able to read it first.
"""

import time
import tkinter as tk
from tkinter import ttk

import notes_view
from theme import (AMBER, CARD, CYAN, DANGER, EDGE, INK, MONO, MUTED, PANEL,
                   PAPER, PLANE_FAR, PLANE_MID, PLANE_NEAR)

#: How the "still working" line escalates. Summarising a real meeting is slow
#: -- around two minutes for 7,000 characters on llama3.2 -- and one unchanging
#: line for all of it reads as a hang. Moved here from the window's old global
#: "Saving..." ticker, which could only ever describe one meeting at a time.
_WAITING = (
    (20, "Summarizing with the local model."),
    (75, "Summarizing with the local model. This takes a minute or two."),
    (10 ** 9, "Still summarizing. Long meetings take longer; a smaller "
              "model finishes sooner."),
)


def _hint(seconds):
    for limit, text in _WAITING:
        if seconds < limit:
            return text
    return _WAITING[-1][1]


def mmss(seconds):
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def how_long(seconds):
    """A recording's length, in units rather than as a clock reading.

    "23:32" beside a wall-clock time reads as a second wall-clock time -- the
    card header showed "00:07 · 23:32" and both halves looked like the hour.
    "23m" cannot be mistaken for one.
    """
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds) // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class NotesColumn:
    """The stack of cards, newest first.

    ``submit(fn)`` runs work off the Tk thread and ``to_ui(fn)`` marshals a
    result back onto it -- the window already owns both, and a widget that
    reached for its own threads would be a second answer to a question the app
    has settled.
    """

    def __init__(self, parent, *, submit, to_ui, open_path, set_clipboard):
        self.parent = parent
        self.submit = submit
        self.to_ui = to_ui
        self.open_path = open_path
        self.set_clipboard = set_clipboard
        self._cards = {}          # job id -> NotesCard, in arrival order

        self.empty = ttk.Label(
            parent,
            text="Notes appear here when a recording stops.\n\n"
                 "Summarizing runs in the background — you can start the next "
                 "meeting straight away.",
            style="Muted.TLabel", justify="left", wraplength=320)
        self.empty.pack(anchor="w", pady=(4, 0))
        # add="+", not a plain bind. The frame handed in is the one inside
        # gui._scrollable, which has already bound <Configure> to recompute the
        # scroll region -- and a second plain bind() *replaces* that rather
        # than joining it, so the column would stop scrolling as cards were
        # added and the older ones would be unreachable.
        parent.bind("<Configure>", self._rewrap, add="+")

    def _rewrap(self, event):
        width = max(220, event.width - 24)
        self.empty.configure(wraplength=width)
        for card in self._cards.values():
            card.rewrap(width)

    def add(self, job, *, title="", transcript_path="", duration=0):
        """Open a card for a meeting that has just stopped."""
        self.empty.pack_forget()
        card = NotesCard(self, job, title=title,
                         transcript_path=transcript_path, duration=duration)
        self._cards[job] = card
        # Newest on top. Every card is re-packed, in reverse arrival order,
        # because pack() appends: packing only the new one puts it *below* the
        # stack, and packing the new one first and then the rest in arrival
        # order interleaves them wrongly the moment there are three.
        for existing in reversed(list(self._cards.values())):
            existing.frame.pack_forget()
            existing.frame.pack(fill="x", pady=(0, 10))
        self.renumber()
        return card

    def card(self, job):
        return self._cards.get(job)

    def renumber(self):
        """Tell each waiting card how many meetings are in front of it."""
        ahead = 0
        for card in self._cards.values():        # insertion order = stop order
            if card.state == "queued":
                card.set_ahead(ahead)
            if card.state in ("queued", "running"):
                ahead += 1


class NotesCard:
    """One meeting: its progress, then its notes, then what to do with them."""

    def __init__(self, column, job, *, title="", transcript_path="", duration=0):
        self.column = column
        self.job = job
        self.state = "queued"
        self.title = title
        self.transcript_path = transcript_path
        self.notes_path = ""
        self.notes_text = ""
        self.duration = duration
        self.started = time.monotonic()
        self._timer = None
        self._wrapped = []        # labels that re-wrap with the column
        self._meeting = None

        self.frame = tk.Frame(column.parent, bg=CARD, highlightbackground=EDGE,
                              highlightthickness=1, padx=14, pady=12)

        head = tk.Frame(self.frame, bg=CARD)
        head.pack(fill="x")
        self.title_label = tk.Label(
            head, text=title or "Untitled meeting", bg=CARD, fg=PAPER,
            font=("Segoe UI", 11, "bold"), anchor="w", justify="left")
        self.title_label.pack(side="left", fill="x", expand=True)
        # When it was recorded and for how long. Both, because two meetings in
        # an afternoon look identical in a list otherwise -- and the length is
        # the quickest way to tell the real call from the two minutes of it you
        # recorded by accident.
        stamp = time.strftime("%H:%M")
        if duration:
            stamp += "  ·  " + how_long(duration)
        self.clock = tk.Label(head, text=stamp, bg=CARD, fg=MUTED, font=MONO)
        self.clock.pack(side="right")

        self.status = tk.Label(self.frame, text="", bg=CARD, fg=AMBER,
                               font=MONO, anchor="w", justify="left")
        self.status.pack(fill="x", pady=(4, 0))
        self.hint = tk.Label(self.frame, text="", bg=CARD, fg=MUTED,
                             anchor="w", justify="left")
        self.hint.pack(fill="x")
        self._wrapped.append(self.hint)

        # Everything below the status line: empty until the notes land.
        self.body = tk.Frame(self.frame, bg=CARD)
        self.body.pack(fill="x")

        self.footer = tk.Frame(self.frame, bg=CARD)
        self.footer.pack(fill="x", pady=(8, 0))
        self._link(self.footer, "Open transcript",
                   lambda: self.column.open_path(self.transcript_path))

        self.set_ahead(0)

    # -- small painting helpers ---------------------------------------------
    def _link(self, parent, text, command):
        link = tk.Button(parent, text=text, command=command, bg=CARD, fg=AMBER,
                         activebackground=CARD, activeforeground=PAPER,
                         relief="flat", bd=0, cursor="hand2", padx=0, pady=0,
                         font=(MONO[0], 9, "underline"), highlightthickness=0)
        link.pack(side="left", padx=(0, 12))
        return link

    def _plane(self, ground, label, collapsed=False):
        """One plane of the stack: its own ground tone, label, and fold.

        Returns the frame to fill. Planes are packed in order, so "further
        back" is simply "further down" -- which is the whole of what the flat
        fallback is allowed to do, and enough, because the tone ramp keeps the
        ordering readable when the labels are skimmed rather than read.

        The label folds the plane away. A long meeting's summary is the one
        part of this card that is genuinely long, and it sits between the
        things you act on and the buttons that act on them -- so being able to
        shut it is the difference between a card you scroll and a card you
        read. ``collapsed`` starts it shut.
        """
        plane = tk.Frame(self.body, bg=ground, padx=12, pady=10)
        plane.pack(fill="x", pady=(8, 0))

        head = tk.Button(
            plane, bg=ground, fg=MUTED, activebackground=ground,
            activeforeground=PAPER, relief="flat", bd=0, cursor="hand2",
            font=(MONO[0], 8, "bold"), anchor="w", padx=0, pady=0,
            highlightthickness=0)
        head.pack(fill="x", pady=(0, 5))

        body = tk.Frame(plane, bg=ground)
        body._ground = ground           # children have to match it
        state = {"open": not collapsed}

        def paint():
            head.configure(text=("▾ " if state["open"] else "▸ ") + label)

        def fold():
            state["open"] = not state["open"]
            if state["open"]:
                body.pack(fill="x")
            else:
                body.pack_forget()
            paint()

        head.configure(command=fold)
        paint()
        if state["open"]:
            body.pack(fill="x")
        return body

    def _para(self, parent, text, fg=PAPER):
        label = tk.Label(parent, text=text, bg=self._ground(parent), fg=fg,
                         anchor="w", justify="left")
        label.pack(fill="x")
        self._wrapped.append(label)
        return label

    @staticmethod
    def _ground(widget):
        """The plane colour a child should paint itself against."""
        return getattr(widget, "_ground", CARD)

    def rewrap(self, width):
        for label in self._wrapped:
            label.configure(wraplength=max(200, width - 40))
        self.title_label.configure(wraplength=max(140, width - 110))

    # -- states --------------------------------------------------------------
    def set_ahead(self, ahead):
        """Waiting its turn. Notes are written one meeting at a time."""
        if self.state != "queued":
            return
        self.status.configure(text="◷ Queued", fg=MUTED)
        self.hint.configure(
            text=("Waiting for %d meeting%s ahead of it — notes are written "
                  "one at a time." % (ahead, "" if ahead == 1 else "s"))
            if ahead else "Waiting to start.")

    def set_running(self):
        self.state = "running"
        self.started = time.monotonic()
        self._tick()

    def _tick(self):
        if self.state != "running":
            return
        secs = int(time.monotonic() - self.started)
        self.status.configure(text="⟳ Summarizing…  %s" % mmss(secs), fg=AMBER)
        self.hint.configure(text=_hint(secs))
        self._timer = self.frame.after(1000, self._tick)

    def _stop_timer(self):
        if self._timer is not None:
            try:
                self.frame.after_cancel(self._timer)
            except Exception:  # noqa: BLE001 - already fired, or window gone
                pass
            self._timer = None

    def set_failed(self, error, transcript_path=""):
        """No notes. The words are still saved, and that is the point to make."""
        self._stop_timer()
        self.state = "failed"
        self.transcript_path = transcript_path or self.transcript_path
        self.status.configure(text="⚠ No summary", fg=DANGER)
        self.hint.configure(
            text=f"{error}\n\nThe transcript was saved either way — the words "
                 f"are not lost, and the notes can be made again later.")

    def set_done(self, result, actions_for=None):
        """The notes arrived. Draw them, and offer what comes next.

        ``actions_for(meeting)`` is the window's view of the action registry.
        A build with nothing registered returns an empty list and no next-step
        row is drawn at all, which is the same thing the old single row did.
        """
        self._stop_timer()
        self.state = "done"
        self.title = result.get("title") or self.title
        self.transcript_path = result.get("transcript") or self.transcript_path
        self.notes_path = result.get("summary") or ""
        self.notes_text = result.get("notes") or ""

        self.title_label.configure(text=self.title or "Untitled meeting")
        # No "took 1:47". How long the model spent is this program's business,
        # and the reader has moved on to the notes.
        self.status.configure(text="✓ Notes ready", fg=CYAN)
        delivered = result.get("delivered") or ""
        self.hint.configure(text=delivered or "")
        if not delivered:
            self.hint.pack_forget()

        for child in self.body.winfo_children():
            child.destroy()

        notes = notes_view.parse(self.notes_text, title=self.title)
        if notes.is_empty():
            self._para(self.body, "The model returned no notes for this "
                                  "meeting.", fg=MUTED)
        else:
            self._draw(notes)

        if self.notes_path:
            self._link(self.footer, "Open notes",
                       lambda: self.column.open_path(self.notes_path))

        if actions_for is not None:
            self._offer_next(actions_for)
        # winfo_width answers 1 for a widget Tk has not laid out yet, which
        # would wrap every label to the minimum. Fall back to a sane column.
        width = self.column.parent.winfo_width()
        self.rewrap(width if width > 50 else 360)

    def _draw(self, notes):
        """Planes, nearest first: what was decided, what is owed, then the prose.

        The order is the Spatial direction's, and its second rule: on one
        meeting, depth is *certainty*, not time. A decision is nearest because
        it is settled; the summary sits furthest back because it is the softest
        thing on the card -- a paragraph about the meeting rather than anything
        you can check or act on.

        Which is also why the notes come first and the summary after them. It
        inverts the order the file is written in, and the file is not the point
        -- nobody opens this to read prose, they open it to find what they now
        owe somebody.
        """
        drew = False

        # -- NOTES: the checkable half ------------------------------------
        if notes.decisions:
            plane = self._plane(PLANE_NEAR, "DECIDED")
            for item in notes.decisions:
                self._bullet(plane, item.text, cite=item.due)
            drew = True

        if notes.actions:
            plane = self._plane(PLANE_MID, "OWED")
            for item in notes.actions:
                self._action(plane, item)
            drew = True

        # -- SUMMARY: the prose half, underneath --------------------------
        summary = notes.summary or notes.preamble
        if summary:
            plane = self._plane(PLANE_FAR, "SUMMARY")
            self._para(plane, summary, fg=PAPER if not drew else MUTED)
            drew = True

        # Discussed is furthest of all: skimmed, never acted on. It shares the
        # summary's ground rather than earning a step of its own, because a
        # fourth tone stops reading as distance and starts reading as stripes.
        if notes.points:
            # Folded shut to begin with. It is the furthest plane and the one
            # nobody acts on; open by default it pushes the next-step buttons
            # off the bottom of a card whose useful half is already read.
            plane = self._plane(PLANE_FAR, "DISCUSSED", collapsed=True)
            for item in notes.points:
                self._bullet(plane, item.text, fg=MUTED)

    def _bullet(self, parent, text, fg=PAPER, cite=""):
        ground = self._ground(parent)
        row = tk.Frame(parent, bg=ground)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="·", bg=ground, fg=MUTED, font=MONO).pack(
            side="left", anchor="n", padx=(2, 6))
        label = tk.Label(row, text=text, bg=ground, fg=fg, anchor="w",
                         justify="left")
        label.pack(side="left", fill="x", expand=True)
        self._wrapped.append(label)
        # The citation, when there is one. Amber, because in this palette amber
        # means "checkable" -- a timestamp is the thing that lets you go and
        # check it. Core's notes carry none (its prompt forbids clock times);
        # they arrive with an extracted pack.
        if cite:
            tk.Label(row, text=cite, bg=ground, fg=AMBER, font=MONO).pack(
                side="right", anchor="n", padx=(8, 0))

    def _action(self, parent, item):
        """One commitment, with whoever owns it and whenever it is due.

        "Owed", in the design's word, which is better than "action item"
        because it says who is out of pocket. The box is a checkbox the user
        can tick, and ticking it changes nothing on disk: it is a reading aid
        for working through the list on the call after this one. The saved file
        stays the record of what was *said*, which is not the same thing as
        what has since been done.
        """
        ground = self._ground(parent)
        row = tk.Frame(parent, bg=ground)
        row.pack(fill="x", pady=2)
        var = tk.BooleanVar(value=item.done)
        box = tk.Checkbutton(row, variable=var, bg=ground, fg=PAPER,
                             activebackground=ground, selectcolor=INK,
                             highlightthickness=0, bd=0, padx=0, pady=0)
        box.pack(side="left", anchor="n")

        text = tk.Frame(row, bg=ground)
        text.pack(side="left", fill="x", expand=True, padx=(4, 0))
        label = tk.Label(text, text=item.text, bg=ground, fg=PAPER, anchor="w",
                         justify="left")
        label.pack(fill="x")
        self._wrapped.append(label)

        # Only when the transcript actually said so. The prompt tells the model
        # to leave the owner and the date out rather than invent them, so an
        # action with neither is the system working, not a gap to paper over.
        meta = "  ·  ".join(p for p in (item.owner, item.due) if p)
        if meta:
            tk.Label(text, text=meta, bg=ground, fg=CYAN, font=MONO,
                     anchor="w").pack(fill="x")

    # -- next steps ----------------------------------------------------------
    def _offer_next(self, actions_for):
        """A button per registered action, scoped to this meeting.

        Core knows nothing about what any of them are (see :mod:`actions`); it
        asks the registry and draws what comes back. Nothing registered, and
        this whole section is absent.
        """
        try:
            import actions as actions_mod

            self._meeting = actions_mod.Meeting(
                title=self.title, transcript=self._read_transcript(),
                notes=self.notes_text, transcript_path=self.transcript_path,
                notes_path=self.notes_path)
            available = actions_for(self._meeting)
        except Exception as e:  # noqa: BLE001 - an extension never breaks a save
            print(f"[actions] unavailable: {e}", flush=True)
            return
        if not available:
            return

        self.next_plane = self._plane(PLANE_MID, "NEXT STEPS")
        buttons = tk.Frame(self.next_plane, bg=PLANE_MID)
        buttons.pack(fill="x")
        self._next_buttons = []
        for action in available:
            button = ttk.Button(buttons, text=action.label,
                                command=lambda a=action: self._run_next(a))
            button.pack(side="left", padx=(0, 6), pady=(2, 0))
            self._next_buttons.append(button)

        self.next_status = tk.Label(self.next_plane, text="", bg=PLANE_MID,
                                    fg=MUTED, anchor="w", justify="left")
        self.next_status.pack(fill="x", pady=(6, 0))
        self._wrapped.append(self.next_status)
        self.preview = None

    def _read_transcript(self):
        """The words, from the file this card was written from.

        Not from the recorder: by the time anyone presses one of these buttons
        the recorder may well be part-way through the *next* meeting, and
        asking it would quietly hand out the wrong transcript. The file is the
        record.
        """
        try:
            with open(self.transcript_path, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            return self.notes_text
        # Drop the "<title>\n====\nSaved: ...\n\n" header the file opens with.
        _, sep, rest = body.partition("\n\n")
        return (rest if sep else body).strip()

    def _run_next(self, action):
        """Run one action off the Tk thread, then show what it produced.

        Extraction is another local model call -- tens of seconds -- so running
        it inline would freeze the window mid-click and look like a crash.
        """
        for button in self._next_buttons:
            button.configure(state="disabled")
        self.next_status.configure(text=f"{action.label}…  working", fg=AMBER)

        def work():
            try:
                output = action.run(self._meeting)
            except Exception as e:  # noqa: BLE001
                self.column.to_ui(lambda err=e: self._next_failed(action, err))
                return
            self.column.to_ui(lambda text=output: self._next_ready(action, text))

        self.column.submit(work)

    def _next_failed(self, action, error):
        for button in self._next_buttons:
            button.configure(state="normal")
        self.next_status.configure(text=f"{action.label} failed: {error}",
                                   fg=DANGER)

    def _next_ready(self, action, output):
        """Show the result. Do not put it anywhere until the user says so.

        This used to go straight onto the clipboard with a word count, because
        pasting a meeting into an assistant that is not on this machine is an
        upload and the count was the honest disclosure. Showing the text is the
        better version of the same honesty: the user can read what leaves
        before it leaves.
        """
        for button in self._next_buttons:
            button.configure(state="normal")
        if not isinstance(output, str) or not output.strip():
            self.next_status.configure(
                text="Nothing to copy — the meeting had no decisions or "
                     "actions in it.", fg=MUTED)
            return

        if self.preview is None:
            wrap = tk.Frame(self.next_plane, bg=EDGE, padx=1, pady=1)
            wrap.pack(fill="x", pady=(6, 0))
            self.preview = tk.Text(wrap, bg=PANEL, fg=PAPER, font=MONO,
                                   wrap="word", relief="flat", height=8,
                                   padx=10, pady=8, insertbackground=AMBER)
            self.preview.pack(side="left", fill="both", expand=True)
            bar = ttk.Scrollbar(wrap, command=self.preview.yview)
            bar.pack(side="right", fill="y")
            self.preview.configure(yscrollcommand=bar.set)

            self.copy_row = tk.Frame(self.next_plane, bg=PLANE_MID)
            self.copy_row.pack(fill="x", pady=(6, 0))
            ttk.Button(self.copy_row, text="Copy to clipboard",
                       command=self._copy).pack(side="left")

        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", output)
        self.preview.configure(state="disabled")
        self._output = output
        self.next_status.configure(
            text=f"{action.label} — {len(output.split())} words. Read it, then "
                 f"copy. Nothing has been sent.", fg=CYAN)

    def _copy(self):
        self.column.set_clipboard(self._output)
        self.next_status.configure(
            text=f"Copied — {len(self._output.split())} words, ready to paste. "
                 f"Nothing was sent.", fg=CYAN)


__all__ = ["NotesCard", "NotesColumn"]
