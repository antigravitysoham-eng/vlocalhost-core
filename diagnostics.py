"""Logging, crash capture, and a diagnostic report the user chooses to share.

When something goes wrong on someone else's machine you have nothing to work
from unless the app kept a record. This module keeps one, and makes it easy to
hand over — without breaking the promise the whole product rests on.

The rules that follow from that promise:

* **Nothing is ever sent automatically.** There is no telemetry, no crash
  endpoint, no network call anywhere in this file. The report is written to
  disk and shown to the user; sending it is an action only they can take.
* **The report is readable.** Plain text, short enough to scan before sharing.
  Someone who is careful about their data can check every line.
* **No meeting content, ever.** Not transcripts, not notes, not filenames, not
  calendar entries. Settings, versions, and the traceback — nothing else.
* **Paths are redacted.** Home directories collapse to ``~`` and the account
  name is replaced, because a support ticket should not leak who you are.

The log lives beside the other per-user files and is truncated when it gets
large; it is a debugging aid, not an archive.
"""

import os
import platform
import re
import sys
import traceback
from datetime import datetime

from version import __version__ as APP_VERSION

SUPPORT_URL = "https://vlocal.host/support/"

LOG_NAME = "vlocalhost.log"
REPORT_NAME = "vlocalhost-report.txt"

#: Truncate the log once it passes this, keeping the tail.
MAX_LOG_BYTES = 512 * 1024
#: How much of the log goes into a report.
REPORT_LOG_LINES = 120

_installed = False


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def log_path() -> str:
    from integrations import store

    return store.path_for(LOG_NAME)


def report_path() -> str:
    from integrations import store

    return store.path_for(REPORT_NAME)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
def redact(text: str) -> str:
    """Strip the obvious identifiers out of ``text``.

    Best effort by design: it removes what routinely leaks (home paths, the
    account name, email addresses) rather than pretending to anonymise. The
    user still sees the result before sharing it.
    """
    if not text:
        return ""
    home = os.path.expanduser("~")
    user = os.path.basename(home)
    out = text.replace(home, "~")
    if os.sep == "\\":                       # also catch forward-slash spellings
        out = out.replace(home.replace("\\", "/"), "~")
    if user and len(user) > 2:
        out = re.sub(re.escape(user), "<user>", out, flags=re.IGNORECASE)
    out = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", out)
    return out


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def _trim_log(path: str) -> None:
    try:
        if os.path.getsize(path) <= MAX_LOG_BYTES:
            return
        with open(path, "rb") as f:
            f.seek(-MAX_LOG_BYTES // 2, os.SEEK_END)
            tail = f.read()
        with open(path, "wb") as f:
            f.write(b"[log truncated]\n" + tail)
    except OSError:
        pass


def write(line: str) -> None:
    """Append one line to the log. Never raises."""
    try:
        path = log_path()
        _trim_log(path)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp}  {redact(str(line)).rstrip()}\n")
    except Exception:  # noqa: BLE001 - logging must never break the app
        pass


def _tail(lines: int = REPORT_LOG_LINES) -> str:
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).rstrip()
    except OSError:
        return "(no log yet)"


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
def _ollama_state() -> str:
    try:
        import config
        import requests

        r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        if not r.ok:
            return f"HTTP {r.status_code}"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        want = getattr(config, "OLLAMA_MODEL", "")
        has = any(n.split(":")[0] == want.split(":")[0] for n in names if n)
        return f"reachable, {want} {'present' if has else 'NOT PULLED'}"
    except Exception as e:  # noqa: BLE001
        return f"not reachable ({type(e).__name__})"


def _settings_summary() -> str:
    try:
        import config

        rows = [
            ("capture mode", getattr(config, "CAPTURE_MODE", "?")),
            ("whisper model", getattr(config, "WHISPER_MODEL", "?")),
            ("whisper compute", getattr(config, "WHISPER_COMPUTE", "?")),
            ("language", getattr(config, "WHISPER_LANGUAGE", "") or "auto"),
            ("ollama model", getattr(config, "OLLAMA_MODEL", "?")),
            ("calendar provider", getattr(config, "CALENDAR_PROVIDER", "") or "none"),
        ]
        return "\n".join(f"  {k:<20} {v}" for k, v in rows)
    except Exception as e:  # noqa: BLE001
        return f"  (settings unavailable: {e})"


def _packages() -> str:
    names = ("sounddevice", "soundcard", "faster_whisper", "numpy",
             "requests", "pystray", "PIL", "webrtcvad")
    out = []
    for name in names:
        try:
            mod = __import__(name)
            out.append(f"  {name:<20} {getattr(mod, '__version__', 'installed')}")
        except Exception:  # noqa: BLE001
            out.append(f"  {name:<20} MISSING")
    return "\n".join(out)


def build_report(error: str = "") -> str:
    """A plain-text report the user can read, then paste or attach."""
    try:
        import plugins

        edition = plugins.edition()
    except Exception:  # noqa: BLE001
        edition = "Core"

    parts = [
        "Vlocalhost.AI diagnostic report",
        "=" * 46,
        f"generated     {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"app           {APP_VERSION} ({edition})",
        f"python        {platform.python_version()} ({platform.architecture()[0]})",
        f"os            {platform.system()} {platform.release()} ({platform.machine()})",
        f"ollama        {_ollama_state()}",
        "",
        "Settings",
        _settings_summary(),
        "",
        "Packages",
        _packages(),
    ]
    if error:
        parts += ["", "Error", redact(error).rstrip()]
    parts += ["", f"Log (last {REPORT_LOG_LINES} lines)", redact(_tail())]
    parts += ["", "-" * 46,
              "No meeting audio, transcripts or notes are included.",
              "Paths and account names have been redacted."]
    return "\n".join(parts) + "\n"


def save_report(error: str = "") -> str:
    """Write the report next to the log. Returns the path."""
    path = report_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_report(error))
    except OSError as e:
        raise OSError(f"could not write {path}: {e}") from e
    return path


def open_support() -> None:
    import webbrowser

    try:
        webbrowser.open(SUPPORT_URL)
    except Exception:  # noqa: BLE001
        print(f"Support: {SUPPORT_URL}", flush=True)


GUIDE_URL = "https://vlocal.host/install/guide/"

# The summaries guide has no page of its own on the site yet. Its subject is
# described, and the PDF named, in the installation guide's first-run section,
# so that anchor is the honest fallback rather than the top of a long page.
SUMMARIES_URL = GUIDE_URL + "#first-run"

#: Every document the build ships, keyed by the name the rest of the app uses.
#: Each entry is (file name in docs/, the friendlier name ``build_bundle.py``
#: gives it at the root of the download, where to go when neither exists, and
#: what to call it in a message).
#:
#: A table rather than a function per document: there are two now and the
#: second one arrived by copy-and-paste pressure, which is exactly how the
#: fallback chain would have come to differ between them.
DOCS = {
    "guide": (
        "vlocalhost-installation-guide.pdf",
        "Installation Guide.pdf",
        GUIDE_URL,
        "installation guide",
    ),
    "summaries": (
        "vlocalhost-summaries-setup.pdf",
        "Summaries Setup.pdf",
        SUMMARIES_URL,
        "summaries setup guide",
    ),
}


def doc_path(kind: str = "guide") -> "str | None":
    """The shipped PDF for ``kind``, if this copy has it.

    Three places, because the build puts it in more than one: ``docs/`` beside
    the app, and the root of the bundle where somebody opening the folder will
    see it. A checkout run from source has the first and not the second; the
    macOS bundle has both, one level further up.
    """
    filename, visible, _url, _label = DOCS[kind]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "docs", filename),
        os.path.join(os.path.dirname(here), visible),
        os.path.join(os.path.dirname(here), filename),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def open_doc(kind: str = "guide") -> bool:
    """Open a shipped guide, falling back to the web.

    Returns True if a local file was opened. The fallback matters: running from
    source there is no PDF, and a Help button that does nothing is worse than
    one that goes to the web.
    """
    import subprocess
    import webbrowser

    _filename, _visible, url, label = DOCS[kind]
    path = doc_path(kind)
    if not path:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            print(f"{label.capitalize()}: {url}", flush=True)
        return False

    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606  (documented Windows API)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:  # noqa: BLE001
        # No PDF reader, or no desktop session. The file is still on disk and
        # naming it is more use than a silent failure.
        print(f"{label.capitalize()}: {path}", flush=True)
        return False
    return True


# The installation guide by its own name, because that is what every caller
# outside this module already asks for, and because ``--guide`` on the command
# line has meant this one thing since 1.0.
def guide_path() -> "str | None":
    return doc_path("guide")


def open_guide() -> bool:
    return open_doc("guide")


# ---------------------------------------------------------------------------
# crash capture
# ---------------------------------------------------------------------------
def _dialog(summary: str, path: str) -> bool:
    """Offer the report in a window. False if no window was possible."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:  # noqa: BLE001
        return False
    try:
        win = tk.Tk() if not tk._default_root else tk.Toplevel()
        win.title("Vlocalhost hit a problem")
        win.configure(bg="#090C12")
        frame = tk.Frame(win, bg="#090C12", padx=24, pady=20)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Something went wrong", bg="#090C12", fg="#EAEEF4",
                 font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tk.Label(frame, text=summary[:300], bg="#090C12", fg="#E8624F",
                 font=("Consolas", 9), justify="left", wraplength=460
                 ).pack(anchor="w", pady=(6, 10))
        tk.Label(frame,
                 text=("A diagnostic report has been saved. It contains your\n"
                       "settings and this error — no meeting content.\n\n"
                       + redact(path)),
                 bg="#090C12", fg="#7E8AA0", font=("Segoe UI", 9),
                 justify="left").pack(anchor="w")

        row = tk.Frame(frame, bg="#090C12")
        row.pack(fill="x", pady=(16, 0))

        def show_folder():
            folder = os.path.dirname(path)
            try:
                if platform.system() == "Windows":
                    os.startfile(folder)  # noqa: S606
                elif platform.system() == "Darwin":
                    import subprocess

                    subprocess.Popen(["open", folder])
                else:
                    import subprocess

                    subprocess.Popen(["xdg-open", folder])
            except Exception:  # noqa: BLE001
                pass

        def copy_it():
            try:
                win.clipboard_clear()
                win.clipboard_append(build_report(summary))
            except Exception:  # noqa: BLE001
                pass

        ttk.Button(row, text="Get help", command=open_support).pack(side="left")
        ttk.Button(row, text="Copy report", command=copy_it).pack(side="left", padx=8)
        ttk.Button(row, text="Show file", command=show_folder).pack(side="left")
        ttk.Button(row, text="Close", command=win.destroy).pack(side="right")

        win.update_idletasks()
        win.mainloop() if not tk._default_root or win is tk._default_root else win.wait_window()
        return True
    except Exception:  # noqa: BLE001
        return False


def report_crash(exc_type, exc_value, exc_tb) -> None:
    """Log an unhandled exception, save a report, and tell the user."""
    summary = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    write("UNHANDLED " + summary.replace("\n", " | ")[:2000])
    try:
        path = save_report(summary)
    except Exception:  # noqa: BLE001
        path = "(report could not be written)"
    if not _dialog(f"{exc_type.__name__}: {exc_value}", path):
        print("\n" + "-" * 60, flush=True)
        print(summary, flush=True)
        print(f"A diagnostic report was saved to:\n  {path}", flush=True)
        print(f"Please send it to us: {SUPPORT_URL}", flush=True)
        print("-" * 60 + "\n", flush=True)


def tk_exception(_widget, exc_type, exc_value, exc_tb) -> None:
    """Hook for ``root.report_callback_exception``."""
    report_crash(exc_type, exc_value, exc_tb)


def setup() -> None:
    """Start logging and route unhandled exceptions here. Safe to call twice."""
    global _installed
    if _installed:
        return
    _installed = True

    write(f"--- start {APP_VERSION} on {platform.system()} "
          f"{platform.release()} python {platform.python_version()} ---")

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        report_crash(exc_type, exc_value, exc_tb)

    sys.excepthook = hook

    if hasattr(sys, "excepthook") and hasattr(sys, "version_info") \
            and sys.version_info >= (3, 8):
        try:
            import threading

            def thread_hook(args):
                if issubclass(args.exc_type, KeyboardInterrupt):
                    return
                report_crash(args.exc_type, args.exc_value, args.exc_traceback)

            threading.excepthook = thread_hook
        except Exception:  # noqa: BLE001
            pass


def run_diagnose() -> int:
    """``--diagnose``: write the report and show the user where it is."""
    setup()
    try:
        path = save_report()
    except Exception as e:  # noqa: BLE001
        print(f"Could not write the report: {e}", flush=True)
        return 1
    print(build_report(), flush=True)
    print("-" * 46, flush=True)
    print(f"Saved to:  {path}", flush=True)
    print(f"Log file:  {log_path()}", flush=True)
    print(f"\nSend it to us at {SUPPORT_URL}", flush=True)
    print("Read it first — it is plain text and yours to check.", flush=True)
    return 0
