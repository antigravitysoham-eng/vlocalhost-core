"""Where this machine's MCP server can be wired, and the exact config to paste.

The window needs a list of assistants and, for each, the block of JSON that goes
in that assistant's own config file. Core knows one entry — the generic one,
built from its own server path, which is all any MCP client actually needs.
Anything more specific (which file, on which platform, under which key) is
knowledge about other vendors' products, and that belongs to whatever registers
it rather than to the recorder.

So this is a registry, and Core seeds it with the one host it can honestly
describe: *any client at all*.

An extension registers the rest from its ``register()``::

    from mcp_hosts import register_host

    register_host("claude_code", "Claude Code",
                  where="run this once",
                  snippet='claude mcp add vlocalhost -- "..." "..."',
                  note="Local. Nothing is exposed to the network.")

Nothing here reaches the network or writes to anybody else's config file. The
user copies a block and pastes it themselves, which is the only version of this
that does not involve us editing files belonging to another application.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, NamedTuple, Optional


class Host(NamedTuple):
    """One place the server can be wired up."""

    key: str
    name: str
    where: str          # the file or action the snippet goes into
    snippet: str        # exactly what the user pastes
    note: str = ""      # one line about what this host can and cannot do
    local: bool = True  # False for hosts that can only reach an HTTPS endpoint


_REGISTRY: Dict[str, Host] = {}


def server_args() -> list:
    """How to launch this build's MCP server, extensions included.

    Naming ``mcp_server.py`` directly works for a plain Core install and
    silently drops every installed extension for anyone running through a
    launcher -- the server comes up without the capabilities the rest of the app
    has. So the launcher wins when there is one.
    """
    launcher = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if launcher.endswith(".py") and os.path.isfile(launcher):
        return [launcher, "--mcp"]
    return [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "mcp_server.py")]


def server_path() -> str:
    """Absolute path of this build's MCP server script."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "mcp_server.py")


def standard_block() -> str:
    """The config almost every MCP client accepts, with this machine's paths."""
    return json.dumps(
        {"mcpServers": {"vlocalhost": {"command": sys.executable,
                                       "args": server_args()}}}, indent=2)


def register_host(key: str, name: str, *, where: str, snippet: str,
                  note: str = "", local: bool = True) -> None:
    """Add a destination. Registering the same key twice replaces it."""
    slug = (key or "").strip()
    if not slug:
        raise ValueError("A host needs a non-empty key.")
    _REGISTRY[slug] = Host(slug, name, where, snippet, note, local)


def _seed() -> None:
    """Core's own entry: the generic block, which is all MCP requires."""
    if "generic" in _REGISTRY:
        return
    register_host(
        "generic", "Any MCP client",
        where="your client's MCP config",
        snippet=standard_block(),
        note="The standard shape. Claude Desktop, Cursor and most others take "
             "this verbatim; a few want it under a different key.")


def available_hosts() -> List[Host]:
    """Every destination. The generic entry is always first, then the rest."""
    try:
        from plugins import load

        load()
    except Exception:  # noqa: BLE001 - a broken loader means core only
        pass
    _seed()
    rest = sorted((h for k, h in _REGISTRY.items() if k != "generic"),
                  key=lambda h: h.name)
    return [_REGISTRY["generic"]] + rest


def get_host(key: str) -> Optional[Host]:
    available_hosts()
    return _REGISTRY.get(key)


__all__ = ["Host", "available_hosts", "get_host", "register_host",
           "server_path", "standard_block"]
