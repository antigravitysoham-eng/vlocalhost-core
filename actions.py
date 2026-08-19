"""What can be done with a meeting once it has been saved.

Core writes a transcript and notes and stops there. Anything further — drafting
the follow-up, handing the meeting to another assistant, filing a ticket — is a
paid capability, and Core must not know what any of it is.

So this is a registry and nothing else. Core asks what is available and offers
it; an installation with no extensions has an empty registry and shows nothing.
There is no "if pro" anywhere, which is the rule that keeps the two
repositories from drifting into a fork (see :mod:`plugins`).

An extension registers with:

    actions.register_action(
        "context_pack", build_pack,
        label="Copy for an assistant",
        description="Meeting facts, ready to paste into Claude or ChatGPT.")

and ``run`` is called with a :class:`Meeting` describing what was just saved.
"""

from typing import Callable, Dict, List, NamedTuple, Optional


class Meeting(NamedTuple):
    """One saved meeting, as handed to an action.

    Everything an action could need without going back to disk. ``notes`` is
    None when summarization failed — the transcript is always written, so an
    action that only needs the words still works.
    """

    title: str
    transcript: str
    notes: Optional[str] = None
    transcript_path: str = ""
    notes_path: str = ""
    started_at: Optional[str] = None


class Action(NamedTuple):
    """A registered thing that can be done with a meeting."""

    name: str
    run: Callable[[Meeting], object]
    label: str
    description: str
    #: False when the action needs summarized notes and there are none. Actions
    #: that read only the transcript stay available on a machine with no Ollama.
    needs_notes: bool = False


_REGISTRY: Dict[str, Action] = {}


def register_action(name: str,
                    run: Callable[[Meeting], object],
                    *,
                    label: str,
                    description: str = "",
                    needs_notes: bool = False) -> None:
    """Make ``name`` available to :func:`available_actions`.

    Registering the same name twice replaces the first, so a plugin reloaded
    during development does not accumulate duplicates.
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("An action needs a non-empty name.")
    if not callable(run):
        raise TypeError(f"Action {key!r} needs a callable to run.")
    _REGISTRY[key] = Action(key, run, label or key.replace("_", " ").title(),
                            description, needs_notes)


def available_actions(meeting: Optional[Meeting] = None) -> List[Action]:
    """Registered actions, in registration order.

    Pass the meeting to filter out actions that cannot run against it — an
    action needing notes is dropped when summarization produced none, rather
    than being offered and then failing.
    """
    from plugins import load

    load()  # extensions register on first ask, not at import time
    actions = list(_REGISTRY.values())
    if meeting is not None and not (meeting.notes or "").strip():
        actions = [a for a in actions if not a.needs_notes]
    return actions


def get_action(name: str) -> Optional[Action]:
    from plugins import load

    load()
    return _REGISTRY.get(name.strip().lower())


def run_action(name: str, meeting: Meeting):
    """Run one action by name. Raises KeyError if nothing registered it."""
    action = get_action(name)
    if action is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"No action named {name!r}. Registered: {known}")
    return action.run(meeting)


__all__ = ["Action", "Meeting", "available_actions", "get_action",
           "register_action", "run_action"]
