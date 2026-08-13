"""Rescue notes written by versions that kept them beside the source.

Until v1.0.5 the app wrote transcripts and summaries into a ``notes`` folder
next to ``notetaker.py`` — that is, *inside* the application folder. Installers
unpack each release into its own versioned directory and delete it before
reinstalling, so that arrangement lost meetings two different ways:

* reinstalling the same version deleted every note outright;
* upgrading left them behind in the previous version's folder, where the new
  build could not see them, so the app opened with an empty history.

:func:`run` moves anything it finds into the permanent per-user data directory
(see :mod:`integrations.store`). It runs once at startup, before any UI, and is
built to be dull:

* **Idempotent.** Folders already dealt with are recorded and skipped.
* **Never destructive.** A name collision keeps both files rather than
  overwriting; an identical file is simply dropped from the source.
* **Cross-volume safe.** Copy-then-delete, because an install on ``D:`` and a
  profile on ``C:`` cannot be ``os.rename``'d between.
* **Silent on failure.** Losing a migration is bad; refusing to start the app
  is worse. Every problem is logged and swallowed.
"""

import glob
import json
import os
import shutil

from integrations import store

#: Written to the data directory so a scan happens once per legacy folder.
_STATE = "migrated.json"

#: Version-stamped install directories the Windows/macOS/Linux installers make.
_INSTALL_GLOB = "vlocalhost-core-*"


def _state_path() -> str:
    return os.path.join(store.data_dir(), _STATE)


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    target = _state_path()
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except OSError:
        pass  # we will simply rescan next launch; harmless


def _key(path: str) -> str:
    """A stable identity for a folder, case-insensitively on Windows."""
    return os.path.normcase(os.path.abspath(path))


def legacy_dirs() -> list:
    """Every folder an older version might have left notes in.

    Two places: this build's own directory (which is where the running copy
    would have written them before this change), and any sibling versioned
    install left behind by an upgrade.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    found = [os.path.join(here, "notes")]

    # <install root>/vlocalhost-core-1.0.4/notes, etc. — siblings of this build.
    parent = os.path.dirname(here)
    for sibling in glob.glob(os.path.join(parent, _INSTALL_GLOB)):
        found.append(os.path.join(sibling, "notes"))

    target = _key(store.notes_dir())
    unique = []
    for path in found:
        if not os.path.isdir(path) or _key(path) == target:
            continue
        if _key(path) not in [_key(p) for p in unique]:
            unique.append(path)
    return unique


def _same_file(a: str, b: str) -> bool:
    """True if two files hold identical bytes. Notes are small; just read them."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _free_name(directory: str, filename: str) -> str:
    """``filename``, or ``name-migrated-2.ext``… — never an existing path."""
    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    for n in range(2, 1000):
        candidate = os.path.join(directory, f"{stem}-migrated-{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
    raise OSError(f"no free filename for {filename}")


def _move_one(source: str, target_dir: str) -> str:
    """Move one file in. Returns 'moved', 'duplicate', or 'failed'."""
    name = os.path.basename(source)
    existing = os.path.join(target_dir, name)
    if os.path.exists(existing) and _same_file(source, existing):
        try:
            os.remove(source)          # already migrated on an earlier run
        except OSError:
            pass
        return "duplicate"
    try:
        # copy2 then remove, not move: the install and the profile are often
        # on different volumes, and copy2 keeps the original timestamps so the
        # notes list stays in the order the user remembers.
        destination = _free_name(target_dir, name)
        shutil.copy2(source, destination)
        os.remove(source)
        return "moved"
    except OSError:
        return "failed"


def run(quiet: bool = False) -> dict:
    """Move any stranded notes into the permanent data directory.

    Returns a summary dict. Never raises.
    """
    summary = {"moved": 0, "duplicate": 0, "failed": 0, "folders": []}
    try:
        state = _load_state()
        done = set(state.get("folders", []))
        target_dir = store.notes_dir()

        for folder in legacy_dirs():
            if _key(folder) in done:
                continue
            try:
                entries = sorted(os.listdir(folder))
            except OSError:
                continue

            for name in entries:
                source = os.path.join(folder, name)
                if not os.path.isfile(source):
                    continue
                summary[_move_one(source, target_dir)] += 1

            summary["folders"].append(folder)
            done.add(_key(folder))
            # Tidy up, but only if we emptied it. A leftover file means
            # something failed, and that is worth leaving visible.
            try:
                os.rmdir(folder)
            except OSError:
                pass

        if summary["folders"]:
            state["folders"] = sorted(done)
            _save_state(state)
            if not quiet and (summary["moved"] or summary["failed"]):
                print(f"[migrate] moved {summary['moved']} note file(s) into "
                      f"{target_dir}", flush=True)
                if summary["failed"]:
                    print(f"[migrate] {summary['failed']} file(s) could not be "
                          f"moved and were left where they were.", flush=True)
    except Exception as e:  # noqa: BLE001 - never block startup over a migration
        print(f"[migrate] skipped: {e}", flush=True)
    return summary
