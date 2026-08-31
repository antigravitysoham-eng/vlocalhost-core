"""Extra MCP tools, registered by whatever is installed.

Core's MCP server already turns every registered *action* into a tool — see
``mcp_server._action_tools`` and :mod:`actions`. That covers anything shaped
like "do this to one saved meeting", which is what an action is.

It does not cover everything an assistant wants to ask. "What do I still owe
Mike?" is not a question about one meeting; it is a question about all of them,
and it takes arguments an action has no way to declare. Rather than widen
:class:`actions.Action` until it can express both — and make every registrant
carry fields it does not use — this is a second, plainer registry for tools
that bring their own schema.

Core supplies the mechanism and learns nothing else. It does not know what the
tools do, cannot call one it was not given, and a build with nothing installed
has an empty registry and advertises no extra tools at all. Same rule as
:mod:`integrations` and :mod:`actions`, for the same reason: it is what keeps
the free build honestly free of the paid one rather than merely switched off.

An extension registers from its ``register()``::

    from mcp_tools import register_tool

    register_tool(
        "open_action_items", run,
        description="Everything still owed, across every indexed meeting.",
        input_schema={"type": "object", "properties": {
            "owner": {"type": "string", "description": "Filter to one person"}}})

``run`` is called with the tool's arguments as keyword arguments and returns
text. Raising is fine — the server reports the failure to the model rather than
dying, which is the behaviour every other tool already gets.
"""

from typing import Any, Callable, Dict, List, NamedTuple, Optional

#: An input schema for a tool that takes nothing.
NO_ARGUMENTS: Dict[str, Any] = {"type": "object", "properties": {}}


class McpTool(NamedTuple):
    """One registered tool, in the shape ``tools/list`` needs."""

    name: str
    run: Callable[..., str]
    description: str
    input_schema: Dict[str, Any]


_REGISTRY: Dict[str, McpTool] = {}


def register_tool(name: str,
                  run: Callable[..., str],
                  *,
                  description: str = "",
                  input_schema: Optional[Dict[str, Any]] = None) -> None:
    """Make ``name`` available to any connected MCP client.

    Registering the same name twice replaces the first, so a package reloaded
    during development does not accumulate duplicates — and ``tools/list`` can
    never advertise two tools the client cannot tell apart.
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("An MCP tool needs a non-empty name.")
    if not callable(run):
        raise TypeError(f"Tool {key!r} needs a callable.")
    _REGISTRY[key] = McpTool(key, run, description or key,
                             input_schema or dict(NO_ARGUMENTS))


def available_tools() -> List[McpTool]:
    """Every registered tool, name-sorted. Empty in a build with no extensions."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def get_tool(name: str) -> Optional[McpTool]:
    """One registered tool, or None."""
    return _REGISTRY.get(name)


__all__ = ["NO_ARGUMENTS", "McpTool", "available_tools", "get_tool",
           "register_tool"]
