"""Speaking two MCP revisions at once, without pretending to speak either badly.

The `2026-07-28` revision is not a polish pass on `2025-06-18`. It removes the
`initialize` handshake entirely and makes the protocol stateless: every request
carries its own protocol version and client capabilities in ``_meta``, servers
must implement ``server/discover``, every result carries a ``resultType``, and
list results must carry cache hints.

Meanwhile the clients people actually have — Claude Desktop, Claude Code,
Cursor — speak `2025-06-18` and send ``initialize``. A server that jumps to the
new revision alone is a server nobody can connect to; a server that stays put
is one that stops working as clients move.

So this module owns exactly one job: **decide which revision a given request is
speaking, and shape the reply to match.** Everything version-dependent lives
here rather than being sprinkled through the handlers as ``if`` statements,
because the failure mode of getting that wrong is a client that connects, lists
tools, and then silently misreads every answer.

Two rules it is built on:

**Per-request, then per-session, then floor.** The new revision puts the version
on each request. The old one agrees it once at ``initialize``. Both are honoured,
in that order, and an unrecognised version is answered with an error rather than
a guess.

**Never add a field a client did not ask for.** ``resultType`` and the cache
hints are required at `2026-07-28` and unknown at `2025-06-18`. Emitting them
unconditionally is the kind of thing that works in testing and breaks a strict
client in the field.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

#: Every revision this server can answer, newest first. Order matters: the
#: first entry is what ``server/discover`` and an unversioned request get.
VERSIONS: Tuple[str, ...] = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

LATEST = VERSIONS[0]

#: What an old client that never negotiated is assumed to speak. The oldest
#: revision we support, because assuming *less* of a client than it can do
#: costs a feature, and assuming more costs the connection.
FLOOR = VERSIONS[-1]

#: The revision that introduced statelessness, ``resultType`` and cache hints.
#: Everything gated in this module is gated on being at least this.
STATELESS_FROM = "2026-07-28"

# -- `_meta` keys defined by the specification -------------------------------
META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# -- Error codes -------------------------------------------------------------
# `2026-07-28` partitions the JSON-RPC server-error range and renumbers the
# codes it introduced. -32002 for "resource not found" became -32602 to line up
# with plain JSON-RPC, and clients are told to accept the old one, so both are
# recorded here rather than only the new one.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022
LEGACY_RESOURCE_NOT_FOUND = -32002


def _rank(version: str) -> int:
    """How new a version is. Higher is newer; unknown sorts oldest."""
    try:
        return len(VERSIONS) - VERSIONS.index(version)
    except ValueError:
        return -1


def is_known(version: str) -> bool:
    return version in VERSIONS


def is_at_least(version: str, floor: str = STATELESS_FROM) -> bool:
    """True if ``version`` is ``floor`` or newer.

    Compared by position in :data:`VERSIONS` rather than by parsing the date,
    so a revision we have never heard of is treated as older than everything we
    have — which is the safe direction. Claiming a new field is understood by a
    client we cannot place is how you corrupt a stream.
    """
    return _rank(version) >= _rank(floor) > 0


def negotiate(asked: Optional[str]) -> str:
    """The version to speak with a client that asked for ``asked``.

    An exact match wins. Anything else falls back to the newest revision we
    support and lets the client decide whether it can live with that — which is
    what the old handshake was for, and what ``server/discover`` replaces.
    """
    if asked and is_known(asked):
        return asked
    return LATEST


def request_version(message: Dict[str, Any], session_default: str = FLOOR) -> str:
    """Which revision one request is speaking.

    ``_meta`` first, because at `2026-07-28` that is the only place it appears.
    Then whatever was agreed at ``initialize`` for a client still using the
    handshake. Then the floor, which keeps an unversioned request answerable
    instead of rejected.
    """
    params = message.get("params")
    meta = (params or {}).get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict):
        asked = meta.get(META_PROTOCOL)
        if isinstance(asked, str) and asked:
            return asked
    return session_default


def client_info(message: Dict[str, Any]) -> Dict[str, Any]:
    """Who is asking, per ``_meta``. Empty when the client did not say.

    Used for the audit log and for per-client scope. A client that declines to
    identify itself is not refused — it is recorded as unknown, which is a fact
    worth having rather than a reason to break the connection.
    """
    params = message.get("params")
    meta = (params or {}).get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict):
        info = meta.get(META_CLIENT_INFO)
        if isinstance(info, dict):
            return info
    return {}


def shape(payload: Dict[str, Any], version: str) -> Dict[str, Any]:
    """Add what the negotiated revision requires of every result.

    At `2026-07-28` that is ``resultType``. Earlier revisions have never heard
    of it, and the spec tells clients to read its absence as ``"complete"``, so
    adding it there would be noise at best.
    """
    if is_at_least(version) and "resultType" not in payload:
        payload = dict(payload)
        payload["resultType"] = "complete"
    return payload


def cacheable(payload: Dict[str, Any], version: str, *,
              ttl_ms: int = 300_000, scope: str = "private") -> Dict[str, Any]:
    """Add the cache hints `2026-07-28` requires on every list result.

    ``scope`` defaults to ``"private"`` and should stay that way for anything
    derived from a user's meetings. ``"public"`` invites shared intermediaries
    to hold the response, which is a reasonable thing to allow for a public API
    and an unreasonable thing to allow for somebody's meeting archive.
    """
    if not is_at_least(version):
        return payload
    out = dict(payload)
    out.setdefault("ttlMs", int(ttl_ms))
    out.setdefault("cacheScope", scope)
    return out


def server_meta(name: str, version_string: str,
                existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ``_meta`` a `2026-07-28` server should put on each result."""
    meta = dict(existing or {})
    meta[META_SERVER_INFO] = {"name": name, "version": version_string}
    return meta


__all__ = [
    "FLOOR", "INTERNAL_ERROR", "INVALID_PARAMS", "LATEST",
    "LEGACY_RESOURCE_NOT_FOUND", "META_CLIENT_CAPS", "META_CLIENT_INFO",
    "META_PROTOCOL", "META_SERVER_INFO", "METHOD_NOT_FOUND", "STATELESS_FROM",
    "UNSUPPORTED_PROTOCOL_VERSION", "VERSIONS", "cacheable", "client_info",
    "is_at_least", "is_known", "negotiate", "request_version", "server_meta",
    "shape",
]
