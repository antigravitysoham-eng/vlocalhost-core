"""Provider-agnostic calendar/email interface.

The rest of the app talks to a ``CalendarProvider`` and never needs to know
whether it's backed by Google or Microsoft. Both providers translate their
native event objects into the common :class:`Event` shape below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class CredentialSetup:
    """How the UI should ask for a provider's own OAuth *app* credentials.

    Providers differ in what they need — one wants a JSON file downloaded from
    a console, another wants an id pasted from a portal. Rather than teach the
    window about each one, the provider describes the prompt and the window
    renders it. ``kind`` is:

    ``"file"``  — show a file picker; the chosen path is passed back.
    ``"text"``  — show a single-line input; the typed string is passed back.

    Either way the result goes to :meth:`CalendarProvider.save_client_credentials`.
    """

    kind: str                       # "file" | "text"
    title: str                      # dialog title
    prompt: str                     # what the user is being asked for
    file_types: List[tuple] = field(  # ("JSON", "*.json") pairs, kind="file"
        default_factory=lambda: [("All files", "*.*")])
    help_text: str = ""             # numbered steps for the setup guide


@dataclass
class Event:
    """A single calendar event, normalized across providers."""

    id: str
    title: str
    start: datetime               # timezone-aware, local time
    end: datetime                 # timezone-aware, local time
    attendees: List[str] = field(default_factory=list)  # email addresses
    organizer: Optional[str] = None                      # email address
    join_url: Optional[str] = None                       # Meet/Teams/Zoom link
    description: str = ""

    @property
    def is_meeting(self) -> bool:
        """A 'real' meeting worth auto-recording: has other people or a call link."""
        others = [a for a in self.attendees if a and a != self.organizer]
        return bool(self.join_url) or len(others) >= 1


class CalendarProvider(ABC):
    """Common interface implemented by GoogleProvider and MicrosoftProvider."""

    name: str = "base"

    # -- auth ---------------------------------------------------------------
    @abstractmethod
    def authenticate(self, interactive: bool = True, on_prompt=None) -> None:
        """Obtain and cache credentials. With interactive=False, only load a
        previously cached token and raise if none exists (no browser popup).

        ``on_prompt(message)`` — optional callback for sign-in instructions the
        user must act on (e.g. the Microsoft device code). Defaults to printing
        to stdout; the GUI passes a callback that shows them in the window.
        """

    @abstractmethod
    def is_authenticated(self) -> bool:
        """True if we have usable cached credentials (no network call)."""

    @abstractmethod
    def disconnect(self) -> bool:
        """Forget the cached account: delete the stored token and reset state.
        Returns True if something was actually removed. The OAuth *client*
        credentials the user supplied are kept — only the sign-in is dropped."""

    def has_client_credentials(self) -> bool:
        """True once the user has supplied their own OAuth app credentials.
        Until then, connecting can't even be attempted."""
        return True

    def credential_setup(self) -> Optional[CredentialSetup]:
        """Describe how to collect this provider's OAuth app credentials, or
        None if it needs none. See :class:`CredentialSetup`."""
        return None

    def save_client_credentials(self, value: str) -> str:
        """Persist credentials gathered per :meth:`credential_setup`.

        ``value`` is the chosen file path (kind="file") or the typed string
        (kind="text"). Returns a one-line confirmation to show the user, and
        raises ValueError if the input isn't what this provider expected.
        """
        raise NotImplementedError

    # -- calendar -----------------------------------------------------------
    @abstractmethod
    def list_events(self, start: datetime, end: datetime) -> List[Event]:
        """Return events on the primary calendar within [start, end]."""

    @abstractmethod
    def update_event_description(self, event_id: str, notes_markdown: str) -> None:
        """Append/replace the event's description/body with the meeting notes."""

    # -- mail ---------------------------------------------------------------
    @abstractmethod
    def send_email(self, to: List[str], subject: str, body_markdown: str) -> None:
        """Send an email (notes summary) to the given recipients."""

    # -- convenience --------------------------------------------------------
    def current_event(self, now: datetime, grace_minutes: int = 5) -> Optional[Event]:
        """The event happening right now (started within the last ``grace``
        minutes and not yet ended), or None. Used to attach context to notes."""
        from datetime import timedelta

        window_start = now - timedelta(minutes=grace_minutes)
        for ev in self.list_events(window_start, now + timedelta(minutes=1)):
            if ev.start <= now <= ev.end and ev.is_meeting:
                return ev
        return None
