"""Meeting Notes Agent — core orchestration.

Wires the mic listener -> transcriber -> summarizer together and manages the
live transcript. Used by both the tray app (app.py) and the CLI (--no-tray).
"""

import os
import queue
import re
import threading
import time
from datetime import datetime

import config
from audio_listener import build_listener
from integrations import store
from transcriber import build_transcriber
from summarizer import summarize, generate_title


def _slugify(text, fallback="meeting"):
    """Turn a free-text meeting title into a safe filename fragment."""
    first = (text.strip().splitlines() or [""])[0]
    first = first.strip().strip('"\'')
    first = re.sub(r"(?i)^title[:\-\s]+", "", first)  # drop a stray "Title:" label
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    return slug[:60].strip("-") or fallback


def _unique(path):
    """Return `path`, or path-2, path-3, ... so we never overwrite a file."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root}-{i}{ext}"):
        i += 1
    return f"{root}-{i}{ext}"


class NoteTaker:
    def __init__(self, on_line=None, provider=None, on_partial=None):
        """on_line(text) is called for each newly transcribed line (for live UI).
        on_partial(text, label) is called with provisional text while somebody
        is still speaking — pass it only if you have somewhere to show it.
        provider: optional CalendarProvider for naming/email/post-back."""
        self.on_line = on_line or (lambda text: None)
        self.on_partial = on_partial
        self.provider = provider

        self.transcriber = build_transcriber()  # faster-whisper, or your own engine
        # Mic, system audio, or both — see config.CAPTURE_MODE.
        self.listener = build_listener(
            self._on_utterance,
            on_partial=self._on_partial if on_partial else None)

        self._utt_q = queue.Queue()
        # Provisional audio waits in a single slot, not a queue. A newer
        # partial makes an older one worthless, so the newest simply replaces
        # whatever was waiting — which also means a slow machine cannot build
        # a backlog of stale text to grind through.
        self._partial_slot = None
        self._partial_lock = threading.Lock()
        self._next_partial_at = 0.0
        self._transcript = []
        self._lock = threading.Lock()
        self._worker = None
        self.listening = False
        self._dirty = False  # True when there is a transcript not yet saved

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self.listening:
            return
        if hasattr(self.transcriber, "load"):
            self.transcriber.load()  # warm up the model before the mic opens (optional)
        with self._lock:
            self._transcript = []
        self._dirty = False
        self.listening = True
        self._worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._worker.start()
        self.listener.start()

    def stop(self):
        """Stop listening. Returns the full transcript text."""
        if not self.listening:
            return self.transcript_text()
        self.listening = False
        self.listener.stop()          # flushes any in-progress utterance
        # Whatever was mid-sentence has just been flushed as a real utterance,
        # so any provisional copy of it is now worse than what is coming.
        with self._partial_lock:
            self._partial_slot = None
        if self._worker is not None:
            self._worker.join(timeout=30)  # let the queue drain
            self._worker = None
        return self.transcript_text()

    # -- pipeline -------------------------------------------------------------
    def _on_utterance(self, pcm_bytes, label=None):
        """One detected speech segment, tagged with the source it came from."""
        self._utt_q.put((pcm_bytes, label))

    def _on_partial(self, pcm_bytes, label=None):
        """Audio for an utterance still in progress. Newest wins."""
        with self._partial_lock:
            self._partial_slot = (pcm_bytes, label)

    def _take_partial(self):
        """The waiting partial, if it is worth spending the model on.

        Two guards. A partial is dropped outright when a finished utterance is
        queued, because the saved transcript is the product and provisional
        text is decoration. And the next one is not started until as long has
        passed as the last one took, so on a machine where a partial costs
        400 ms the feature settles at half the model's time instead of
        occupying all of it — the slow-laptop case degrades to today's
        behaviour rather than to a locked-up one.
        """
        if not self._utt_q.empty():
            with self._partial_lock:
                self._partial_slot = None
            return None
        if time.monotonic() < self._next_partial_at:
            return None
        with self._partial_lock:
            item, self._partial_slot = self._partial_slot, None
        return item

    def _run_partial(self):
        """Decode the waiting partial, if there is one worth decoding."""
        item = self._take_partial()
        if item is None:
            return
        pcm, label = item
        started = time.monotonic()
        try:
            text = self.transcriber.transcribe(pcm, partial=True)
        except TypeError:
            # A custom CUSTOM_TRANSCRIBER predating the partial argument. It
            # still works, it just cannot be asked to hurry.
            text = self.transcriber.transcribe(pcm)
        except Exception as e:  # noqa: BLE001 - provisional text is expendable
            print(f"[partial transcribe error] {e}")
            return
        self._next_partial_at = time.monotonic() + (time.monotonic() - started)
        # A final may have landed while this was decoding, in which case the
        # real line is already on screen and this is stale.
        if text and self._utt_q.empty():
            self.on_partial(text, label or "")

    def _transcribe_loop(self):
        while self.listening or not self._utt_q.empty():
            try:
                pcm, label = self._utt_q.get(timeout=0.05)
            except queue.Empty:
                self._run_partial()
                continue
            try:
                text = self.transcriber.transcribe(pcm)
            except Exception as e:  # noqa: BLE001 - keep listening on a bad chunk
                print(f"[transcribe error] {e}")
                continue
            if text:
                ts = datetime.now().strftime("%H:%M:%S")
                # Only name the speaker when we're capturing more than one
                # source — a mic-only transcript has nobody to distinguish.
                who = label or ""
                # When auto-detecting, show what language this line was heard
                # as, so a misdetection is visible instead of silent.
                if (config.SHOW_DETECTED_LANGUAGE
                        and config.WHISPER_LANGUAGE in (None, "auto")):
                    detected = getattr(self.transcriber, "last_language", None)
                    if detected:
                        who = f"{who} ({detected})" if who else f"({detected})"
                line = f"[{ts}] {who}: {text}" if who else f"[{ts}] {text}"
                with self._lock:
                    self._transcript.append(line)
                self._dirty = True
                self.on_line(line)

    # -- output ---------------------------------------------------------------
    def transcript_text(self):
        with self._lock:
            return "\n".join(self._transcript)

    def has_unsaved(self):
        """True if there's transcript content that hasn't been written to disk."""
        return self._dirty and bool(self.transcript_text().strip())

    def save(self, event=None):
        """Name the meeting, then write the transcript (.txt) and summary (.md).
        Files are named ``<date>_<meeting-title>``. When ``event`` is given, its
        calendar title is used instead of asking the model to invent one.
        Returns (paths, error); ``paths['title']`` is the human meeting name and
        ``paths['notes']`` is the summary Markdown (for emailing/posting back)."""
        transcript = self.transcript_text()
        if not transcript.strip():
            return None, "Nothing was transcribed — no notes to save."

        out_dir = store.notes_dir()
        date = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # Prefer the real calendar title; otherwise ask the model to name it,
        # falling back to a timestamp if Ollama is unreachable.
        title = (event.title if event and event.title else "") or generate_title(transcript)
        base = f"{date}_{_slugify(title)}" if title else f"meeting_{stamp}"

        # Transcript is a plain .txt file named with the meeting name.
        transcript_path = _unique(os.path.join(out_dir, f"{base}.txt"))
        header = title or "Meeting Transcript"
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(f"{header}\n{'=' * len(header)}\nSaved: {stamp}\n\n"
                    f"{transcript}\n")

        summary_error = None
        notes = None
        summary_path = _unique(os.path.join(out_dir, f"{base}-notes.md"))
        try:
            notes = summarize(transcript)
            with open(summary_path, "w", encoding="utf-8") as f:
                # No timestamp in the heading. The date is already in the
                # filename, and a summary that opens with a clock time reads
                # like a log entry rather than a set of notes.
                f.write(f"# {title or 'Meeting Notes'}\n\n{notes}\n")
        except Exception as e:  # noqa: BLE001
            summary_error = str(e)
            summary_path = None

        self._dirty = False  # written to disk; don't re-save on quit
        return ({"transcript": transcript_path, "summary": summary_path,
                 "title": title or base, "notes": notes}, summary_error)

    def deliver(self, event, paths):
        """Best-effort: email the notes to attendees and/or write them back onto
        the calendar event, according to config. Returns a status string (or '').
        Never raises — delivery failures shouldn't lose the saved notes."""
        if self.provider is None or not paths or not paths.get("notes"):
            return ""
        notes = paths["notes"]
        title = paths.get("title") or "Meeting"
        results = []

        if config.EMAIL_SUMMARY_TO_ATTENDEES:
            recipients = list(event.attendees) if event else []
            if config.EMAIL_SUMMARY_TO_SELF and event and event.organizer:
                if event.organizer not in recipients:
                    recipients.append(event.organizer)
            recipients = [r for r in dict.fromkeys(recipients) if r]
            if recipients:
                try:
                    self.provider.send_email(
                        recipients, f"Meeting notes: {title}", notes)
                    results.append(f"emailed {len(recipients)} attendee(s)")
                except Exception as e:  # noqa: BLE001
                    results.append(f"email failed ({e})")

        # A manually named session has no real event id — nothing to post onto.
        if config.POST_NOTES_TO_EVENT and getattr(event, "id", None):
            try:
                self.provider.update_event_description(event.id, notes)
                results.append("posted to calendar event")
            except Exception as e:  # noqa: BLE001
                results.append(f"event update failed ({e})")

        return "; ".join(results)
