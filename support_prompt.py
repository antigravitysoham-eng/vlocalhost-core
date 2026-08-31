"""A quiet ask for a tip when the user closes the window.

Part of Core, not Pro. The ask is for the free product — the people who install
it and never pay a rupee are exactly the audience — and shipping it in Pro meant
it reached nobody: the installer bundles Core alone, so no released copy of the
app had ever shown it.

It still goes through ``gui.register_exit_hook`` rather than being wired into
the close path, so it stays one self-contained file that can be deleted without
touching the window code.

Rules this follows, because a donation prompt that ignores them is spam:

* It never appears to anyone who has already paid. A paid package declares
  itself through Core's ``entitlement`` registry and this returns before it
  has even counted the close. Core still learns nothing about who that
  package is or what it sells.
* It never appears before the notes are saved. The hook runs after shutdown.
* It appears on every close, including the first. This is a deliberate product
  decision, taken for launch and easily reversed: raise
  :data:`CLOSES_BEFORE_FIRST_ASK` to wait a few closes, and
  :data:`DAYS_BETWEEN_ASKS` above zero to space them out. Both gates are still
  here and still honoured.
* Every failure path is silent. A tip prompt must not be the reason an app
  refuses to quit.

State lives beside the other per-user files via Core's ``store``, not in
``settings``, whose EDITABLE list is Core's business and has no Pro keys.
"""

import json
import os
import platform
import tkinter as tk
from tkinter import ttk

import entitlement
from integrations import store

_FILE = "support_prompt.json"

UPI_ID = "mitrasoham@ybl"
PAYEE = "Soham Mitra"

#: Closes to sit through before the first ask, and days between asks after.
#: 1 and 0 mean "every close, starting with the first". The gates below are
#: unchanged, so restoring a quieter cadence is a two-number edit.
CLOSES_BEFORE_FIRST_ASK = 1
DAYS_BETWEEN_ASKS = 0

# Core's palette. Duplicated rather than imported so this module keeps working
# if Core reorganises its brand tokens.
INK = "#090C12"
PANEL = "#0E1220"
EDGE = "#1C2333"
AMBER = "#FFB43D"
CYAN = "#38E1CE"
PAPER = "#EAEEF4"
MUTED = "#7E8AA0"

_MONO = ("Cascadia Code", 9) if platform.system() == "Windows" else ("Menlo", 11)
_BODY = ("Segoe UI", 10) if platform.system() == "Windows" else ("Helvetica", 12)
_HEAD = ("Segoe UI", 15, "bold") if platform.system() == "Windows" \
    else ("Helvetica", 17, "bold")


def _state() -> dict:
    try:
        with open(store.path_for(_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    target = store.path_for(_FILE)
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except OSError:
        pass  # a tip prompt is never worth surfacing an error for


def _now_days() -> float:
    """Whole days since the epoch. Coarse on purpose — this is a cadence."""
    import time
    return time.time() / 86400.0


def _asset(name: str):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)


def maybe_show(root) -> None:
    """Exit hook. Decides whether to ask, then asks. Never raises."""
    try:
        # Somebody who bought the paid build has already answered this ask, and
        # asking them anyway is the one version of it that is genuinely rude.
        # Checked before the close is counted, so that uninstalling the paid
        # package doesn't leave a free install owing a backlog of asks.
        if entitlement.is_paid():
            return
        state = _state()
        # Nothing sets this any more — the "Don't ask again" tickbox is gone.
        # It is still honoured for anyone who ticked it while it existed;
        # taking the box away is no reason to start asking them again.
        if state.get("never"):
            return
        closes = int(state.get("closes", 0)) + 1
        state["closes"] = closes
        if closes < CLOSES_BEFORE_FIRST_ASK:
            _write(state)
            return
        last = float(state.get("last_asked", 0) or 0)
        today = _now_days()
        if last and today - last < DAYS_BETWEEN_ASKS:
            _write(state)
            return
        state["last_asked"] = today
        _write(state)
        _dialog(root)
    except Exception:  # noqa: BLE001 - closing the app always wins
        pass


def _dialog(root) -> None:
    win = tk.Toplevel(root)
    win.title("Enjoying Vlocalhost?")
    win.configure(bg=INK)
    win.resizable(False, False)
    win.transient(root)

    outer = tk.Frame(win, bg=INK, padx=26, pady=22)
    outer.pack(fill="both", expand=True)

    tk.Label(outer, text="● ON-DEVICE · FREE FOREVER", font=_MONO,
             bg=INK, fg=CYAN).pack(anchor="w")
    tk.Label(outer, text="Liked Vlocalhost?\nBuy me an Americano", font=_HEAD,
             bg=INK, fg=PAPER, justify="left").pack(anchor="w", pady=(8, 6))
    tk.Label(outer,
             text=("It runs entirely on your machine, so it costs me nothing to\n"
                   "give away — and it stays free. If it saved you a meeting's\n"
                   "worth of typing, a coffee back is lovely. Entirely optional."),
             font=_BODY, bg=INK, fg=MUTED, justify="left").pack(anchor="w")

    card = tk.Frame(outer, bg=PANEL, highlightbackground=EDGE,
                    highlightthickness=1, padx=16, pady=14)
    card.pack(fill="x", pady=(16, 12))

    # The QR is optional: a missing or unreadable file must not break the quit.
    photo = None
    try:
        photo = tk.PhotoImage(file=_asset("upi-qr.png"))
        # PhotoImage has no resampling; subsample is the cheap way down.
        if photo.width() > 190:
            photo = photo.subsample(max(1, round(photo.width() / 170)))
    except Exception:  # noqa: BLE001
        photo = None
    if photo is not None:
        label = tk.Label(card, image=photo, bg=PANEL, bd=0)
        label.image = photo  # keep a reference or Tk garbage-collects it
        label.pack()
        tk.Label(card, text="scan with any UPI app · pay what you like",
                 font=_MONO, bg=PANEL, fg=MUTED).pack(pady=(9, 0))

    idrow = tk.Frame(card, bg=PANEL)
    idrow.pack(fill="x", pady=(12, 0))
    # Label the handle: on its own "mitrasoham@ybl" reads like an email address.
    tk.Label(idrow, text="UPI ID:", font=_MONO, bg=PANEL,
             fg=MUTED).pack(side="left", padx=(0, 8))
    tk.Label(idrow, text=UPI_ID, font=_MONO, bg=PANEL, fg=AMBER).pack(side="left")

    copied = tk.StringVar(value="copy")
    cta = tk.StringVar(value="Buy me an Americano")

    def _restore(var, text):
        """Put a button's label back, if the dialog is still there to hear it."""
        try:
            var.set(text)
        except Exception:  # noqa: BLE001
            pass  # closed already, or the interpreter is on its way out

    def _copy_id(var, done, idle, ms):
        """Put the UPI ID on the clipboard, and say so on *var* for a moment."""
        try:
            root.clipboard_clear()
            root.clipboard_append(UPI_ID)
            var.set(done)
        except Exception:  # noqa: BLE001
            var.set("copy failed")
        try:
            win.after(ms, lambda: _restore(var, idle))
        except Exception:  # noqa: BLE001
            pass

    ttk.Button(idrow, textvariable=copied, style="TButton",
               command=lambda: _copy_id(copied, "copied", "copy", 1600),
               ).pack(side="right")

    row = tk.Frame(outer, bg=INK)
    row.pack(fill="x")

    def close():
        try:
            win.grab_release()
        except Exception:  # noqa: BLE001
            pass
        win.destroy()

    # The heading makes the ask; this makes it one click. Copying is all it can
    # honestly do — a upi:// link has no handler on a desktop, so a button that
    # opened one would silently do nothing on the machines this app runs on.
    ttk.Button(row, textvariable=cta,
               command=lambda: _copy_id(cta, "UPI ID copied — thank you",
                                        "Buy me an Americano", 2200),
               ).pack(side="left")
    ttk.Button(row, text="Close", command=close).pack(side="right")

    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Escape>", lambda _e: close())

    # Centre on the parent before showing, so it doesn't flash at 0,0.
    win.update_idletasks()
    try:
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
    except Exception:  # noqa: BLE001
        pass

    try:
        win.grab_set()
    except Exception:  # noqa: BLE001
        pass
    win.wait_window()
