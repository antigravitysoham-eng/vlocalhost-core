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
import engine as engine_mod  # noqa: E402
import settings        # noqa: E402

SERVER_NAME = "vlocalhost"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

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
    return _text(*[f"{i['modified']}  {i['kind']:<10}  {i['name']}" for i in items])


def tool_read_note(name):
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

    lines = [f"App is using: {config.CALENDAR_PROVIDER or 'no account (local only)'}"]
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
]

_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------
def _send(message):
    _PROTOCOL_OUT.write(json.dumps(message) + "\n")
    _PROTOCOL_OUT.flush()


def _result(request_id, payload):
    _send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def _error(request_id, code, message):
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})


def handle_initialize(params):
    asked = params.get("protocolVersion")
    return {
        "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Vlocalhost.AI records and transcribes meetings entirely on this "
            "machine. Use start_recording / stop_recording to run a session, "
            "live_transcript to follow along, and search_notes / read_note to "
            "look back at past meetings. email_notes is the only tool that "
            "sends anything off the device — always confirm recipients first."
        ),
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


def dispatch(message):
    """Handle one JSON-RPC message. Returns nothing; replies are written out."""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    # Notifications have no id and must never be answered.
    if request_id is None:
        return

    if method == "initialize":
        _result(request_id, handle_initialize(params))
    elif method == "ping":
        _result(request_id, {})
    elif method == "tools/list":
        _result(request_id, {"tools": [
            {k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]})
    elif method == "tools/call":
        _result(request_id, handle_tools_call(params))
    elif method in ("resources/list", "resources/templates/list"):
        _result(request_id, {"resources": [], "resourceTemplates": []})
    elif method == "prompts/list":
        _result(request_id, {"prompts": []})
    else:
        _error(request_id, -32601, f"Method not found: {method}")


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
