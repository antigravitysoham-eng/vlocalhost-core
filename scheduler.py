"""Auto-start recording around calendar meetings.

Polls the connected calendar. When a meeting is in progress it fires
``on_start(event)``; when that meeting ends it fires ``on_stop(event)``. The
app wires these to NoteTaker.start / stop-and-save so recording tracks your
calendar without any clicks.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

import config
from integrations.base import CalendarProvider, Event


class CalendarScheduler:
    def __init__(self, provider: CalendarProvider,
                 on_start: Callable[[Event], None],
                 on_stop: Callable[[Event], None],
                 on_error: Optional[Callable[[str], None]] = None):
        self.provider = provider
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_error = on_error or (lambda msg: None)

        self._active: Optional[Event] = None  # event currently being recorded
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self):
        while self._running:
            try:
                self._tick(datetime.now().astimezone())
            except Exception as e:  # noqa: BLE001 - never let polling kill the app
                self.on_error(f"Calendar check failed: {e}")
            # Sleep in small slices so stop() is responsive.
            waited = 0.0
            while self._running and waited < config.CALENDAR_POLL_SECONDS:
                threading.Event().wait(0.5)
                waited += 0.5

    def _tick(self, now: datetime):
        grace = timedelta(minutes=config.AUTO_START_GRACE_MINUTES)
        # Look at events spanning a little before now to a little after.
        events = self.provider.list_events(now - timedelta(hours=1),
                                            now + timedelta(hours=1))
        live = next((ev for ev in events
                     if ev.is_meeting and (ev.start - grace) <= now < ev.end), None)

        if self._active is not None:
            # Recording — has the active meeting ended (or been replaced)?
            if live is None or live.id != self._active.id or now >= self._active.end:
                ended = self._active
                self._active = None
                self.on_stop(ended)
                # If a different meeting is live right now, start it too.
                if live is not None and (self._active is None):
                    self._active = live
                    self.on_start(live)
        elif live is not None:
            self._active = live
            self.on_start(live)
