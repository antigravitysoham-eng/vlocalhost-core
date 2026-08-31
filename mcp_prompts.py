"""Prompts — the tasks a user picks, not the ones a model guesses.

The pack already knows what artifact each kind of meeting owes: a sales call
owes a follow-up email, an incident review owes a postmortem, a design review
owes an ADR. Until now that knowledge was buried inside a tool description,
where the only way to reach it was for the model to decide on its own to call
the tool.

MCP prompts are the user-controlled primitive: the client shows them in its own
menu, usually as slash commands, and a person picks one. That is the right shape
for "draft the follow-up" — it is a thing somebody *decides* to do, not a thing
an assistant should infer.

Core supplies the registry and registers none of its own; a build with no
extensions lists no prompts. Same rule as every other registry here.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, NamedTuple, Optional

#: What ``prompts/get`` must return: a list of messages the client will send.
Messages = List[Dict[str, Any]]


class Argument(NamedTuple):
    """One argument a prompt accepts."""

    name: str
    description: str = ""
    required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "required": bool(self.required)}


class Prompt(NamedTuple):
    """A registered prompt.

    ``build(**arguments)`` returns either a plain string — turned into a single
    user message, which is what almost every prompt wants — or a list of
    message dicts for anything that needs more than one turn.
    """

    name: str
    build: Callable[..., Any]
    title: str = ""
    description: str = ""
    arguments: List[Argument] = []

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        if self.arguments:
            out["arguments"] = [a.to_dict() for a in self.arguments]
        return out


_REGISTRY: Dict[str, Prompt] = {}


def register_prompt(name: str,
                    build: Callable[..., Any],
                    *,
                    title: str = "",
                    description: str = "",
                    arguments: Optional[List[Argument]] = None) -> None:
    """Make a prompt available to any connected client."""
    key = (name or "").strip()
    if not key:
        raise ValueError("A prompt needs a non-empty name.")
    if not callable(build):
        raise TypeError(f"Prompt {key!r} needs a callable.")
    _REGISTRY[key] = Prompt(key, build, title, description, list(arguments or []))


def available_prompts() -> List[Prompt]:
    """Every registered prompt, name-sorted so a caching client sees a stable list."""
    try:
        from plugins import load

        load()
    except Exception:  # noqa: BLE001
        pass
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def get_prompt(name: str) -> Optional[Prompt]:
    available_prompts()
    return _REGISTRY.get(name)


def as_messages(built: Any) -> Messages:
    """Normalise whatever a prompt returned into MCP prompt messages."""
    if isinstance(built, list):
        return built
    return [{"role": "user",
             "content": {"type": "text", "text": str(built)}}]


__all__ = ["Argument", "Messages", "Prompt", "as_messages", "available_prompts",
           "get_prompt", "register_prompt"]
