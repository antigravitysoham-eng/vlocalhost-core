"""A record of what an assistant read, kept on the same disk as the meetings.

Wiring an MCP client to this app hands it the recorder and the archive. That is
the point, and it is also the moment the product acquires a question it has
never had to answer before: *what did it actually look at?*

Before this, the honest answer was "no idea". A config file in some editor could
read every meeting ever recorded and leave no trace. That was defensible while
connecting was a thing one person did to their own machine on purpose. It stops
being defensible the moment the product advertises wiring up four assistants.

So: an append-only line-per-event log, written locally, readable with the same
tools as everything else, and **never sent anywhere**. It is not telemetry
turned inward — nothing here leaves the machine, and the file is the user's to
read or delete.

Deliberately in Core rather than behind the paid tier. Knowing what read your
meetings is not a premium feature; it is the thing that makes the free feature
safe to use.

Format is JSON Lines, at ``data_dir()/mcp-access.log``:

    {"at": "2026-08-29T14:32:01", "client": "claude-code", "kind": "tool",
     "name": "search_notes", "detail": "budget", "allowed": true}

Every write is best-effort. A log that cannot be written must never be the
reason a tool call fails -- the recording and the answer matter more than the
bookkeeping about them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from integrations import store

FILE = "mcp-access.log"

#: Above this the log is trimmed to the newest half on the next write. Bounded
#: so an always-on assistant cannot quietly fill a disk, and generous enough
#: that a month of ordinary use is never trimmed.
MAX_LINES = 5000

#: Tools that read meeting content, and so are worth a line. Recorder controls
#: (start/stop/status) are not: they change nothing about who saw what, and
#: logging them would bury the entries that matter.
CONTENT_TOOLS = {
    "read_note", "search_notes", "list_notes", "live_transcript",
    "copy_for_assistant", "copy_redacted", "context_pack_json",
    "open_action_items", "search_decisions", "open_questions",
    "meeting_timeline", "brief_me", "stale_commitments", "person_view",
    "digest", "index_meetings",
}


#: Overrides the log location. Exists so a test that drives the real server in
#: a subprocess does not write into the user's actual archive -- which it was
#: doing, and which is exactly the kind of thing an access log must never do.
ENV_OVERRIDE = "VLOCALHOST_MCP_LOG"


def path() -> str:
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    return override or os.path.join(store.data_dir(), FILE)


def _client_name(info: Optional[Dict[str, Any]]) -> str:
    if not isinstance(info, dict):
        return "unknown"
    name = str(info.get("name") or "").strip()
    return name or "unknown"


def record(kind: str, name: str, *, client: Optional[Dict[str, Any]] = None,
           detail: str = "", allowed: bool = True) -> None:
    """Append one line. Never raises, never blocks an answer."""
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "client": _client_name(client),
        "kind": kind,
        "name": name,
        "detail": (detail or "")[:200],
        "allowed": bool(allowed),
    }
    try:
        target = path()
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim(target)
    except OSError:
        pass  # bookkeeping is never worth failing a tool call for


def _trim(target: str) -> None:
    """Keep the file bounded, cheaply and only when it has actually grown."""
    try:
        if os.path.getsize(target) < 400 * MAX_LINES:
            return
        with open(target, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_LINES:
            return
        keep = lines[len(lines) // 2:]
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, target)
    except OSError:
        pass


def entries(limit: int = 50) -> List[Dict[str, Any]]:
    """The most recent events, newest last. Unreadable lines are skipped."""
    try:
        with open(path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-max(1, int(limit)):]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def summary(limit: int = 50) -> str:
    """What has read your meetings, for a human or a model to read back."""
    items = entries(limit)
    if not items:
        return ("Nothing has been read through MCP yet, or the log has been "
                f"cleared.\nThe log lives at {path()} and is never sent "
                "anywhere.")

    by_client: Dict[str, int] = {}
    refused = 0
    for item in items:
        by_client[item.get("client", "unknown")] = \
            by_client.get(item.get("client", "unknown"), 0) + 1
        if not item.get("allowed", True):
            refused += 1

    out = [f"Last {len(items)} access(es) through MCP:", ""]
    for client, count in sorted(by_client.items(), key=lambda kv: -kv[1]):
        out.append(f"  {client}: {count}")
    if refused:
        out.append(f"  ({refused} refused or not found -- a URI that "
                   f"named nothing counts here too)")
    out += ["", "Most recent:"]
    for item in items[-12:]:
        mark = " " if item.get("allowed", True) else "REFUSED "
        detail = f" {item.get('detail')}" if item.get("detail") else ""
        out.append(f"  {item.get('at', '')}  {mark}{item.get('client')} → "
                   f"{item.get('name')}{detail}")
    out += ["", f"Full log: {path()}", "Nothing here is ever sent anywhere."]
    return "\n".join(out)


def clear() -> int:
    """Delete the log. Returns how many entries were removed."""
    count = len(entries(MAX_LINES))
    try:
        os.remove(path())
    except OSError:
        return 0
    return count


__all__ = ["CONTENT_TOOLS", "FILE", "MAX_LINES", "clear", "entries", "path",
           "record", "summary"]
