"""``--actions`` and ``--do`` — run whatever the registry offers, from a terminal.

Core has no idea what these actions are. It lists what registered and runs the
one you name against a saved meeting, so a free install prints "no actions
available" and an install with an extension prints that extension's.

    python vlocalhost.py --actions
    python vlocalhost.py --do copy_for_assistant
    python vlocalhost.py --do copy_redacted 2026-08-14_standup.txt
"""

import os
import sys

import actions as actions_mod
import engine as engine_mod


def _newest_transcript():
    """The most recent saved transcript, or None."""
    for item in engine_mod.AppEngine().list_notes(limit=0):
        if item["kind"] == "transcript":
            return item
    return None


def _load(name: str = ""):
    """Build a Meeting from a saved note, by name or the newest one."""
    app = engine_mod.AppEngine()
    if name:
        item = next((i for i in app.list_notes(limit=0) if i["name"] == name), None)
        if item is None:
            # LookupError, not SystemExit. This function is called by the MCP
            # server as well as the CLI, and SystemExit inherits from
            # BaseException -- so the server's `except Exception` let it
            # through and the whole process died when a client asked for a
            # meeting that did not exist. A wrong file name should be an error
            # message, not a dropped connection.
            raise LookupError(f"No saved meeting called {name!r}. "
                              f"Run --actions to see what is available.")
    else:
        item = _newest_transcript()
        if item is None:
            raise LookupError("No saved meetings yet — record one first.")

    transcript = app.read_note(item["name"])
    title = os.path.splitext(item["name"])[0]
    # The summary sits beside the transcript. Try the current name first,
    # then the pre-1.2.0 one, so meetings saved by an older build still
    # resolve. The transcript may itself carry the "-transcript" suffix
    # now, which has to come off before either summary name is built.
    notes = None
    stem = os.path.splitext(item["name"])[0]
    if stem.endswith("-transcript"):
        stem = stem[: -len("-transcript")]
    for candidate in (stem + "-summary.txt", stem + "-summary.md",
                      stem + "-notes.md"):
        try:
            notes = app.read_note(candidate)
            break
        except (FileNotFoundError, ValueError):
            continue

    return actions_mod.Meeting(
        title=title, transcript=transcript, notes=notes,
        transcript_path=item["path"], started_at=item["modified"][:10])


def run_actions() -> int:
    """``--actions`` — what can be done with a saved meeting here."""
    available = actions_mod.available_actions()
    if not available:
        print("No actions available.\n"
              "Actions come from a paid extension; this is a Core install.",
              flush=True)
        return 0

    newest = _newest_transcript()
    print("Actions\n" + "-" * 34, flush=True)
    for a in available:
        print(f"  {a.name}", flush=True)
        print(f"      {a.label} — {a.description}", flush=True)
    if newest:
        print(f"\nMost recent meeting: {newest['name']}", flush=True)
    print(f"\nRun one with:  python vlocalhost.py --do "
          f"{available[0].name}", flush=True)
    return 0


def run_do(argv) -> int:
    """``--do NAME [meeting]`` — run one action and print what it returns."""
    rest = [a for a in argv[argv.index("--do") + 1:] if not a.startswith("--")]
    if not rest:
        print("Usage: python vlocalhost.py --do <action> [meeting-file]",
              file=sys.stderr, flush=True)
        return 2

    name, meeting_file = rest[0], (rest[1] if len(rest) > 1 else "")
    action = actions_mod.get_action(name)
    if action is None:
        known = ", ".join(a.name for a in actions_mod.available_actions())
        print(f"No action called {name!r}."
              f"{' Available: ' + known if known else ' None are installed.'}",
              file=sys.stderr, flush=True)
        return 1

    try:
        meeting = _load(meeting_file)
    except LookupError as e:
        print(e, file=sys.stderr, flush=True)
        return 1
    print(f"[{action.label}] {meeting.title}", file=sys.stderr, flush=True)
    # The result goes to stdout and the labels to stderr, so the useful part
    # can be piped straight to the clipboard:  ... --do copy_for_assistant | clip
    result = action.run(meeting)
    print(result if isinstance(result, str) else repr(result), flush=True)
    return 0
