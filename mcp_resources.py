"""Meetings as MCP resources — attachable, rather than fetched through a tool.

A tool call is the wrong shape for "here is a document". It makes the model
choose a function, invent arguments, and read prose back; and it means the
client's own UI — the paperclip, the @-mention, the resource picker — has
nothing to show. Notes are the most literal example of a resource there is, and
this server has been serving them through ``read_note`` because it never
declared the capability.

This module is the registry behind ``resources/list``, ``resources/read`` and
``resources/templates/list``. Core registers its own notes, because those are
Core's data and reading them is already free. An extension registers whatever
else it can derive.

Two things this deliberately does not do:

**It does not enumerate an unbounded archive.** ``resources/list`` returns a
recent window and a *template* covering everything, rather than four thousand
entries a client would have to page through. The template is the honest way to
say "any meeting, by name".

**It does not read a file the caller did not name.** Every provider resolves a
URI it minted itself, and Core's own provider goes through ``engine.read_note``,
which already refuses to escape the notes folder.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, NamedTuple, Optional

#: The scheme every provider here mints URIs under. RFC 3986 compliant, and
#: distinct enough that a client will not confuse one for a file path.
SCHEME = "vlocalhost"


class Resource(NamedTuple):
    """One resource, in the shape ``resources/list`` needs."""

    uri: str
    name: str
    title: str = ""
    description: str = ""
    mime_type: str = "text/plain"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"uri": self.uri, "name": self.name,
                               "mimeType": self.mime_type}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        return out


class Template(NamedTuple):
    """A parameterised family of resources, per RFC 6570."""

    uri_template: str
    name: str
    title: str = ""
    description: str = ""
    mime_type: str = "text/plain"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"uriTemplate": self.uri_template,
                               "name": self.name, "mimeType": self.mime_type}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        return out


class Provider(NamedTuple):
    """Somewhere resources come from.

    ``list_resources()`` returns what is worth advertising right now.
    ``read(uri)`` returns text for a URI this provider minted, or None if the
    URI is not its business — the server then tries the next provider and, if
    none claims it, answers "not found" rather than guessing.
    """

    key: str
    list_resources: Callable[[], List[Resource]]
    read: Callable[[str], Optional[str]]
    templates: Callable[[], List[Template]]


_PROVIDERS: Dict[str, Provider] = {}


def register_provider(key: str,
                      list_resources: Callable[[], List[Resource]],
                      read: Callable[[str], Optional[str]],
                      templates: Optional[Callable[[], List[Template]]] = None
                      ) -> None:
    """Add a source of resources. Registering the same key twice replaces it."""
    name = (key or "").strip()
    if not name:
        raise ValueError("A resource provider needs a non-empty key.")
    _PROVIDERS[name] = Provider(name, list_resources, read,
                                templates or (lambda: []))


def _providers() -> List[Provider]:
    try:
        from plugins import load

        load()  # extensions register on first ask, not at import time
    except Exception:  # noqa: BLE001 - a broken loader means "core only"
        pass
    return [_PROVIDERS[k] for k in sorted(_PROVIDERS)]


def list_resources() -> List[Dict[str, Any]]:
    """Everything worth advertising, from every provider.

    A provider that raises is skipped rather than allowed to empty the list —
    one broken extension must not make the archive invisible.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for provider in _providers():
        try:
            items = provider.list_resources() or []
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            if item.uri in seen:
                continue
            seen.add(item.uri)
            out.append(item.to_dict())
    return out


def list_templates() -> List[Dict[str, Any]]:
    """Every parameterised family, from every provider."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for provider in _providers():
        try:
            items = provider.templates() or []
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            if item.uri_template in seen:
                continue
            seen.add(item.uri_template)
            out.append(item.to_dict())
    return out


def read(uri: str) -> Optional[Dict[str, Any]]:
    """Contents for ``uri``, or None if no provider claims it.

    None is a real answer here and the server turns it into ``-32602``. The
    specification is explicit that an empty ``contents`` array must not be used
    for a missing resource, because it cannot be told apart from a resource that
    exists and is empty.
    """
    target = (uri or "").strip()
    if not target:
        return None
    for provider in _providers():
        try:
            text = provider.read(target)
        except Exception:  # noqa: BLE001 - a failing provider is not the answer
            continue
        if text is not None:
            return {"uri": target, "mimeType": _mime_for(target), "text": text}
    return None


def _mime_for(uri: str) -> str:
    if uri.endswith("/pack") or uri.endswith(".json"):
        return "application/json"
    if uri.endswith(".md"):
        return "text/markdown"
    return "text/plain"


__all__ = ["Provider", "Resource", "SCHEME", "Template", "list_resources",
           "list_templates", "read", "register_provider"]
