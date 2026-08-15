"""Meeting Notes Agent — core orchestration.

Wires the mic listener -> transcriber -> summarizer together and manages the
live transcript. Used by both the tray app (app.py) and the CLI (--no-tray).
"""

import os
import queue
import re
import threading
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
        on_partial(text) is called with provisional text while someone is still
        speaking; the next on_line replaces it. Front ends that cannot show
        provisional text simply leave it None, and no partial work is done.
        provider: optional CalendarProvider for naming/email/post-back."""
        self.on_line = on_line or (lambda text: None)
        self.on_partial = on_partial
        self.provider = provider

        self.transcriber = build_transcriber()  # faster-whisper, or your own engine
        # Mic, system audio, or both — see config.CAPTURE_MODE.
        self.listener = build_listener(
            self._on_utterance,
            self._on_partial_audio if on_partial else None)

        self._utt_q = queue.Queue()
        # Only ever the most recent provisional segment. A queue would let
        # previews pile up behind each other and arrive describing speech that
        # finished seconds ago, which is worse than not showing them at all.
        self._pending_partial = None
        self._partial_lock = threading.Lock()
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
        with self._partial_lock:
            self._pending_partial = None  # don't preview audio we've stopped for
        if self._worker is not None:
            self._worker.join(timeout=30)  # let the queue drain
            self._worker = None
        return self.transcript_text()

    # -- pipeline -------------------------------------------------------------
    def _on_utterance(self, pcm_bytes, label=None):
        """One detected speech segment, tagged with the source it came from."""
        self._utt_q.put((pcm_bytes, label))

    def _on_partial_audio(self, pcm_bytes, label=None):
        """A segment that is still being spoken. Latest one wins."""
        if not getattr(config, "LIVE_PARTIALS", True):
            return
        with self._partial_lock:
            self._pending_partial = (pcm_bytes, label)

    def _take_partial(self):
        with self._partial_lock:
            pending, self._pending_partial = self._pending_partial, None
        return pending

    def _next_job(self):
        """(pcm, label, is_final) for the transcriber, or None when idle.

        Finished utterances always win: a preview is worth nothing once the
        real line is ready, and letting one delay the other would trade the
        latency we are trying to remove for latency somewhere else.
        """
        try:
            pcm, label = self._utt_q.get(timeout=0.05)
        except queue.Empty:
            pending = self._take_partial()
            return (*pending, False) if pending else None
        # Whatever preview was waiting describes audio this line now covers.
        self._take_partial()
        return pcm, label, True

    def _format(self, text, label):
        """A transcript line: timestamp, who spoke, and the words."""
        ts = datetime.now().strftime("%H:%M:%S")
        # Only name the speaker when we're capturing more than one source —
        # a mic-only transcript has nobody to distinguish.
        who = label or ""
        # When auto-detecting, show what language this line was heard as, so a
        # misdetection is visible instead of silent.
        if (config.SHOW_DETECTED_LANGUAGE
                and config.WHISPER_LANGUAGE in (None, "auto")):
            detected = getattr(self.transcriber, "last_language", None)
            if detected:
                who = f"{who} ({detected})" if who else f"({detected})"
        return f"[{ts}] {who}: {text}" if who else f"[{ts}] {text}"

    def _transcribe_loop(self):
        while self.listening or not self._utt_q.empty():
            job = self._next_job()
            if job is None:
                continue
            pcm, label, is_final = job
            try:
                text = self.transcriber.transcribe(pcm)
            except Exception as e:  # noqa: BLE001 - keep listening on a bad chunk
                print(f"[transcribe error] {e}")
                continue
            if not text:
                continue
            if not is_final:
                # Provisional: shown, never recorded. The finished line that
                # follows is the one that reaches the transcript and the file.
                self.on_partial(self._format(text, label))
                continue
            line = self._format(text, label)
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
                f.write(f"# {title or 'Meeting Notes'} — {stamp}\n\n{notes}\n")
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
