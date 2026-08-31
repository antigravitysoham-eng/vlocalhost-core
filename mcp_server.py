"""Vlocalhost.AI — MCP server.

Exposes the app over the Model Context Protocol so any MCP client (Claude Code,
Claude Desktop, Cursor, Windsurf, or your own agent) can start a recording,
watch the transcript, search past meetings, and see what's next on your
calendar — while the audio, the transcription, and the summarization all stay
on this machine.

Speaks JSON-RPC 2.0 over stdio with no extra dependencies, so it runs on the
same Python as the app itself.

    python mcp_server.py            # normally launched by the MCP client

Register it (Claude Desktop's claude_desktop_config.json, Cursor's
.cursor/mcp.json, or `claude mcp add`):

    {"mcpServers": {"vlocalhost": {
        "command": "python", "args": ["<path>/mcp_server.py"]}}}
"""

import json
import os
import sys
import threading

# Protocol messages own the real stdout. The app prints diagnostics with plain
# print(), so send everything else to stderr — one stray line would corrupt the
# stream and the client would drop the connection.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config          # noqa: E402
import mcp_protocol    # noqa: E402
import engine as engine_mod  # noqa: E402
import settings        # noqa: E402

SERVER_NAME = "vlocalhost"
SERVER_VERSION = "1.1.0"
PROTOCOL_VERSION = mcp_protocol.LATEST
SUPPORTED_PROTOCOLS = set(mcp_protocol.VERSIONS)

_engine = None
_engine_lock = threading.Lock()


def get_engine():
    """The shared session engine, built on first use."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = engine_mod.build()
            _engine.front_end = "MCP client"
        return _engine


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def _text(*lines):
    return "\n".join(str(line) for line in lines if line is not None)


def tool_start_recording(title=None):
    eng = get_engine()
    if eng.is_listening:
        return _text(f"Already recording (started {eng.elapsed}s ago).",
                     f"Meeting: {eng.event.title if eng.event else 'untitled'}")
    holder = engine_mod.active_recorder()
    if holder:
        return _text("Cannot start — another Vlocalhost session holds the "
                     f"microphone ({holder.get('front_end')}, pid {holder['pid']}, "
                     f"since {holder.get('since')}).")
    eng.start(title=title)
    name = eng.event.title if eng.event else None
    return _text("Recording. The mic is live and silence is ignored.",
                 f"Meeting: {name}" if name else
                 "No title given — the local model will name it on save.",
                 "Call stop_recording when the meeting ends.")


def tool_stop_recording():
    eng = get_engine()
    if not eng.is_listening:
        return "Not recording — nothing to stop."
    result = eng.stop_and_save()
    if not result.get("transcript"):
        return _text("Stopped.", result.get("error") or "Nothing was transcribed.")
    lines = [f"Saved “{result['title']}”.",
             f"Transcript: {result['transcript']}"]
    if result.get("summary"):
        lines.append(f"Notes: {result['summary']}")
    if result.get("delivered"):
        lines.append(f"Delivered: {result['delivered']}")
    if result.get("error"):
        lines.append(f"Summary step failed: {result['error']}")
    if result.get("notes"):
        lines += ["", "--- NOTES ---", result["notes"]]
    return _text(*lines)


def tool_recording_status():
    eng = get_engine()
    status = eng.status()
    ollama_ok, ollama_detail = engine_mod.check_ollama()
    lines = [
        f"Recording: {'yes' if status['listening'] else 'no'}",
        f"Elapsed: {status['elapsed_seconds']}s" if status["listening"] else None,
        f"Meeting: {status['meeting']}" if status["meeting"] else None,
        f"Lines transcribed: {status['lines']}",
        f"Speech model: {status['whisper_model']}",
        f"Summary model: {status['ollama_model']} ({ollama_detail})",
        f"Calendar/email: {status['provider'] or 'not connected'}"
        + ("" if status["provider_connected"] else
           f" — {status['provider_error'] or 'no account linked'}"),
        f"Notes folder: {status['notes_dir']}",
    ]
    holder = engine_mod.active_recorder()
    if holder and int(holder["pid"]) != os.getpid():
        lines.append(f"Note: another session is recording ({holder['front_end']}).")
    return _text(*lines)


def tool_live_transcript():
    eng = get_engine()
    transcript = eng.transcript()
    if not transcript.strip():
        return ("Nothing transcribed yet."
                if eng.is_listening else "Not recording.")
    return transcript


def tool_list_notes(limit=20):
    items = get_engine().list_notes(limit=int(limit))
    if not items:
        return "No saved meetings yet."

    import mcp_policy

    hidden = 0
    if mcp_policy.enabled():
        kept = mcp_policy.filter_notes(items)
        hidden = len(items) - len(kept)
        items = kept

    rows = [f"{i['modified']}  {i['kind']:<10}  {i['name']}" for i in items]
    if hidden:
        # Said out loud, because a list that silently shrank would let an
        # assistant answer from half an archive and sound certain about it.
        rows.append(f"({hidden} older meeting(s) withheld -- "
                    + mcp_policy.describe() + ")")
    if not rows:
        return ("Every saved meeting is outside the window assistants may "
                "read. " + mcp_policy.describe())
    return _text(*rows)


def _scope_check(name, path=""):
    """None if allowed, otherwise the refusal to return instead of content."""
    import mcp_policy

    ok, reason = mcp_policy.allows(name, path)
    return None if ok else "Refused: " + name + chr(10) + reason


def tool_read_note(name):
    refusal = _scope_check(name)
    if refusal:
        return refusal
    return get_engine().read_note(name)


def tool_search_notes(query, limit=10):
    hits = get_engine().search_notes(query, limit=int(limit))
    if not hits:
        return f"No saved meeting mentions {query!r}."
    return _text(*[f"{h['name']} ({h['modified']})\n    …{h['excerpt']}…"
                   for h in hits])


def tool_upcoming_meetings(hours=12):
    eng = get_engine()
    if eng.provider is None:
        return ("No calendar connected. Open the Vlocalhost window → "
                "Connections to link a Google or Outlook account.")
    events = eng.upcoming_events(hours=int(hours))
    if not events:
        return f"Nothing on the calendar in the next {hours} hours."
    lines = []
    for ev in events:
        when = ev.start.strftime("%a %H:%M") + ev.end.strftime("–%H:%M")
        who = f" · {len(ev.attendees)} attendee(s)" if ev.attendees else ""
        call = " · has a call link" if ev.join_url else ""
        lines.append(f"{when}  {ev.title}{who}{call}")
    return _text(*lines)


def tool_email_notes(note, to, subject=None):
    eng = get_engine()
    if eng.provider is None:
        return "No email account connected — link one in the Vlocalhost window."
    if isinstance(to, str):
        to = [to]
    recipients = [address.strip() for address in to if address and address.strip()]
    if not recipients:
        return "No recipients given."
    body = eng.read_note(note)
    eng.provider.send_email(recipients, subject or f"Meeting notes: {note}", body)
    return f"Sent {note} to {', '.join(recipients)}."


def tool_connection_status():
    from integrations import available_providers, get_provider

    import mcp_policy

    lines = [f"App is using: {config.CALENDAR_PROVIDER or 'no account (local only)'}",
             mcp_policy.describe()]
    names = available_providers()
    if not names:
        lines.append("No calendar/email providers are installed — this build "
                     "records, transcribes and summarizes entirely offline.")
    for name in names:
        try:
            provider = get_provider(name)
        except Exception as e:  # noqa: BLE001 - optional packages missing
            lines.append(f"{name}: unavailable — {e}")
            continue
        if not provider.has_client_credentials():
            lines.append(f"{name}: no OAuth app credentials yet "
                         "(add them in the Vlocalhost window → Connections)")
        elif provider.is_authenticated():
            lines.append(f"{name}: connected")
        else:
            lines.append(f"{name}: credentials present, not signed in")
    ok, detail = engine_mod.check_ollama()
    lines.append(f"ollama: {'ok' if ok else 'problem'} — {detail}")
    lines.append(f"config folder: {settings.path()}")
    return _text(*lines)


TOOLS = [
    {
        "name": "start_recording",
        "description": "Start recording and transcribing the meeting happening "
                       "now on this machine. Audio never leaves the device. If a "
                       "calendar is connected, the current meeting names the "
                       "session automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "Optional name for the session. Leave "
                                         "empty to use the calendar event, or "
                                         "let the local model name it on save."},
            },
        },
        "handler": tool_start_recording,
    },
    {
        "name": "stop_recording",
        "description": "Stop recording, write the transcript and the summarized "
                       "notes to disk, deliver them per the user's settings, and "
                       "return the notes.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_stop_recording,
    },
    {
        "name": "recording_status",
        "description": "Whether a recording is running, for how long, which "
                       "meeting, and the health of the local models and the "
                       "calendar connection.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_recording_status,
    },
    {
        "name": "live_transcript",
        "description": "The transcript of the recording in progress so far.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_live_transcript,
    },
    {
        "name": "list_notes",
        "description": "List saved meeting transcripts and notes, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer",
                                     "description": "How many to return (default 20)."}},
        },
        "handler": tool_list_notes,
    },
    {
        "name": "read_note",
        "description": "Read one saved transcript or notes file by its file name "
                       "(as returned by list_notes or search_notes).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string",
                                    "description": "File name inside the notes folder."}},
            "required": ["name"],
        },
        "handler": tool_read_note,
    },
    {
        "name": "search_notes",
        "description": "Full-text search across every saved meeting, returning "
                       "the matching files with a snippet of context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to look for."},
                "limit": {"type": "integer", "description": "Max hits (default 10)."},
            },
            "required": ["query"],
        },
        "handler": tool_search_notes,
    },
    {
        "name": "upcoming_meetings",
        "description": "Meetings on the connected calendar between now and a few "
                       "hours out, with attendee counts and whether they have a "
                       "call link.",
        "inputSchema": {
            "type": "object",
            "properties": {"hours": {"type": "integer",
                                     "description": "How far ahead to look (default 12)."}},
        },
        "handler": tool_upcoming_meetings,
    },
    {
        "name": "email_notes",
        "description": "Email a saved notes file to people, using the account "
                       "connected in the app. This sends data off the machine, so "
                       "confirm the recipients with the user first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "File name to send."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "Recipient email addresses."},
                "subject": {"type": "string", "description": "Optional subject."},
            },
            "required": ["note", "to"],
        },
        "handler": tool_email_notes,
    },
    {
        "name": "connection_status",
        "description": "Which calendar/email accounts are set up and connected, "
                       "and whether the local summarization model is reachable.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_connection_status,
    },
    {
        "name": "mcp_access_log",
        "description": "What has read your meetings through MCP: which client, "
                       "which tool, when. Local only, never sent anywhere.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer",
                      "description": "How many recent entries (default 50)."}}},
        "handler": lambda limit=50: __import__("mcp_audit").summary(limit),
    },
]

def _action_tools():
    """One MCP tool per registered action.

    Core does not know what these are. An extension registers an action (see
    :mod:`actions`) and it appears here automatically, so a paid capability
    reaches Claude Desktop, Claude Code and Cursor without this file naming it
    — and a Core install adds nothing, because the registry is empty.

    The meeting is chosen the same way the CLI chooses it: by file name, or the
    most recent recording when the client does not say. A model asking "draft
    the follow-up from my last meeting" should not have to look the name up
    first.
    """
    import actions as actions_mod

    tools = []
    for action in actions_mod.available_actions():
        def run(note=None, _action=action):
            from _actions_cli import _load

            return _action.run(_load(note or ""))

        tools.append({
            "name": action.name,
            "description": action.description or action.label,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "note": {"type": "string",
                             "description": "File name of a saved meeting, "
                                            "e.g. 2026-08-19_standup.txt. "
                                            "Omit for the most recent one."},
                },
            },
            "handler": run,
        })
    return tools


def _registered_tools():
    """Tools an extension registered with its own schema.

    The companion to :func:`_action_tools`. An action is "do this to one saved
    meeting" and needs no schema beyond a note name; anything that asks a
    question across meetings, or takes arguments of its own, arrives here
    instead. Core still names none of them -- see :mod:`mcp_tools`.
    """
    import mcp_tools
    from plugins import load

    load()  # extensions register on first ask, not at import time
    tools = []
    for entry in mcp_tools.available_tools():
        tools.append({
            "name": entry.name,
            "description": entry.description,
            "inputSchema": entry.input_schema,
            "handler": entry.run,
        })
    return tools


TOOLS += _action_tools()
TOOLS += _registered_tools()
TOOLS.sort(key=lambda t: t["name"])
_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------
def _send(message):
    _PROTOCOL_OUT.write(json.dumps(message) + "\n")
    _PROTOCOL_OUT.flush()


def _result(request_id, payload, version=None):
    """Reply, shaped for whichever revision the request was speaking."""
    body = mcp_protocol.shape(payload, version or _session["version"])
    _send({"jsonrpc": "2.0", "id": request_id, "result": body})


def _error(request_id, code, message, data=None):
    body = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    _send({"jsonrpc": "2.0", "id": request_id, "error": body})


#: What a client agreed to at ``initialize``. At 2026-07-28 there is no
#: handshake and every request carries its own version, so this is only ever
#: consulted for clients still using the old lifecycle.
_session = {"version": mcp_protocol.FLOOR, "client": {}}


def _capabilities():
    """What this server can do. Resources and prompts are always declared:
    an empty list is a valid answer and is what a core-only build gives for
    prompts, whereas withholding the capability would stop a client asking at
    all."""
    return {
        "tools": {"listChanged": False},
        "resources": {},
        "prompts": {"listChanged": False},
    }


INSTRUCTIONS = (
    "Vlocalhost.AI records and transcribes meetings entirely on this machine. "
    "Use start_recording / stop_recording to run a session, live_transcript to "
    "follow along, and search_notes / read_note to look back. Meetings are also "
    "available as resources under vlocalhost://meeting/... — prefer attaching "
    "one of those over calling a tool to fetch text. email_notes is the only "
    "tool that sends anything off this device: always confirm recipients first."
)


def handle_discover(params, version):
    """``server/discover`` — mandatory at 2026-07-28, harmless before it.

    Replaces the ``initialize`` handshake. A client may call it up front to
    pick a version, or use it on stdio as a probe to find out whether the
    server is new enough to talk to statelessly.
    """
    return {
        "protocolVersions": list(mcp_protocol.VERSIONS),
        "capabilities": _capabilities(),
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def handle_initialize(params):
    """The pre-2026-07-28 handshake. Kept because it is what shipped clients use.

    Claude Desktop, Claude Code and Cursor all speak 2025-06-18 today. Dropping
    this in favour of the new lifecycle would be standards-compliant and would
    disconnect every user, which is not a trade worth making until the clients
    move.
    """
    asked = params.get("protocolVersion")
    agreed = asked if mcp_protocol.is_known(asked) else mcp_protocol.LATEST
    _session["version"] = agreed
    info = params.get("clientInfo")
    if isinstance(info, dict):
        _session["client"] = info
    return {
        "protocolVersion": agreed,
        "capabilities": _capabilities(),
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def handle_tools_call(params):
    name = params.get("name")
    tool = _BY_NAME.get(name)
    if tool is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True}
    arguments = params.get("arguments") or {}
    try:
        text = tool["handler"](**arguments)
        is_error = False
    except TypeError as e:  # bad/missing arguments from the client
        text, is_error = f"Invalid arguments for {name}: {e}", True
    except Exception as e:  # noqa: BLE001 - report failures to the model, don't die
        text, is_error = f"{name} failed: {e}", True
    return {"content": [{"type": "text", "text": text or "(no output)"}],
            "isError": is_error}


def handle_prompts_get(params):
    """Build one prompt. Raises LookupError for a name we do not have."""
    import mcp_prompts

    name = params.get("name")
    prompt = mcp_prompts.get_prompt(name)
    if prompt is None:
        raise LookupError(f"Unknown prompt: {name}")
    arguments = params.get("arguments") or {}
    built = prompt.build(**arguments)
    return {"description": prompt.description or prompt.title or prompt.name,
            "messages": mcp_prompts.as_messages(built)}


def _first_argument(params):
    """A short, non-secret hint of what a call was about, for the log.

    The first string argument only. Logging every argument would put transcript
    text into a second file, which is the opposite of the point.
    """
    arguments = params.get("arguments") or {}
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return ""


def _audit(kind, name, message, detail="", allowed=True):
    """Record an access, if it is the kind worth recording. Never raises."""
    try:
        import mcp_audit

        if kind == "tool" and name not in mcp_audit.CONTENT_TOOLS:
            return
        client = mcp_protocol.client_info(message) or _session["client"]
        mcp_audit.record(kind, name or "", client=client, detail=detail,
                         allowed=allowed)
    except Exception:  # noqa: BLE001 - bookkeeping never breaks an answer
        pass


def dispatch(message):
    """Handle one JSON-RPC message. Returns nothing; replies are written out."""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    # Notifications have no id and must never be answered.
    if request_id is None:
        return

    version = mcp_protocol.request_version(message, _session["version"])
    if not mcp_protocol.is_known(version):
        _error(request_id, mcp_protocol.UNSUPPORTED_PROTOCOL_VERSION,
               f"Unsupported protocol version: {version}",
               {"supported": list(mcp_protocol.VERSIONS)})
        return

    # Cache hints are per-list and deliberately short. A tool list is stable
    # within a run; a meeting list is not, because a recording finishing changes
    # it. Both are "private": nothing derived from somebody's meetings should
    # sit in a shared intermediary.
    def listing(payload, ttl_ms):
        return mcp_protocol.cacheable(payload, version, ttl_ms=ttl_ms,
                                      scope="private")

    try:
        if method == "server/discover":
            _result(request_id, handle_discover(params, version), version)
        elif method == "initialize":
            _result(request_id, handle_initialize(params), version)
        elif method == "ping":
            # Removed at 2026-07-28, answered anyway. It costs nothing and an
            # older client that pings and gets an error concludes the server
            # is broken.
            _result(request_id, {}, version)
        elif method == "tools/list":
            _result(request_id, listing({"tools": [
                {k: v for k, v in tool.items() if k != "handler"}
                for tool in TOOLS]}, 600_000), version)
        elif method == "tools/call":
            _audit("tool", params.get("name"), message,
                   detail=_first_argument(params))
            _result(request_id, handle_tools_call(params), version)
        elif method == "resources/list":
            import mcp_resources

            _result(request_id,
                    listing({"resources": mcp_resources.list_resources()},
                            60_000), version)
        elif method == "resources/templates/list":
            import mcp_resources

            _result(request_id,
                    listing({"resourceTemplates": mcp_resources.list_templates()},
                            600_000), version)
        elif method == "resources/read":
            import mcp_resources

            uri = params.get("uri")
            found = mcp_resources.read(uri)
            _audit("resource", uri or "", message, allowed=found is not None)
            if found is None:
                # -32602 at 2026-07-28, and clients are told to accept the old
                # -32002 too. An empty contents array is explicitly forbidden
                # here: it cannot be told apart from a resource that exists and
                # happens to be empty.
                _error(request_id, mcp_protocol.INVALID_PARAMS,
                       "Resource not found", {"uri": uri})
            else:
                _result(request_id,
                        listing({"contents": [found]}, 60_000), version)
        elif method == "prompts/list":
            import mcp_prompts

            _result(request_id, listing({"prompts": [
                p.to_dict() for p in mcp_prompts.available_prompts()]},
                600_000), version)
        elif method == "prompts/get":
            _result(request_id, handle_prompts_get(params), version)
        else:
            _error(request_id, mcp_protocol.METHOD_NOT_FOUND,
                   f"Method not found: {method}")
    except LookupError as e:
        _error(request_id, mcp_protocol.INVALID_PARAMS, str(e))
    except TypeError as e:
        _error(request_id, mcp_protocol.INVALID_PARAMS, str(e))
    except Exception as e:  # noqa: BLE001 - a bad request must not end the session
        _error(request_id, mcp_protocol.INTERNAL_ERROR, f"{method} failed: {e}")


def main():
    settings.apply()
    print(f"[{SERVER_NAME}] MCP server ready on stdio.", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            dispatch(message)
        except Exception as e:  # noqa: BLE001 - one bad call must not end the session
            print(f"[{SERVER_NAME}] {e}", file=sys.stderr, flush=True)
            if message.get("id") is not None:
                _error(message["id"], -32603, str(e))

    # stdin closed: the client went away. Don't lose an in-progress recording.
    if _engine is not None:
        _engine.shutdown()


if __name__ == "__main__":
    main()
