"""Meeting Notes Agent — core orchestration.

Wires the mic listener -> transcriber -> summarizer together and manages the
live transcript. Used by both the tray app (app.py) and the CLI (--no-tray).
"""

import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import config
from audio_listener import build_listener
from integrations import store
from transcriber import build_transcriber
from summarizer import summarize, generate_title, to_plain_text


#: Words that describe the file rather than the conversation. A local model
#: asked for a meeting title will sometimes answer "ptacotty meeting
#: transcript", and the saved pair then reads "...-transcript-transcript.txt"
#: and "...-transcript-summary.md" -- which is how a perfectly good summary
#: came to be reported as missing. The title prompt asks for these to be left
#: out; this is here because a small local model will ignore that.
_ARTEFACT_WORDS = re.compile(
    r"(?i)\b(transcripts?|transcription|recordings?|audio|notes?|summary|minutes)\b")


def _clean_title(text):
    """Drop artefact words from a model-generated title, keeping it readable.

    Same words as :data:`_ARTEFACT_WORDS`, applied to the human-facing title
    rather than the slug. Returns the original if nothing would survive -- a
    meeting genuinely called "Notes" keeps its name.
    """
    first = (text or "").strip().splitlines()
    first = first[0].strip().strip(chr(34) + chr(39)) if first else ""
    first = re.sub(r"(?i)^title[:\-\s]+", "", first)
    stripped = _ARTEFACT_WORDS.sub(" ", first)
    stripped = re.sub(r"\s+", " ", stripped).strip(" -,:")
    stripped = re.sub(r"(?i)^(of|the|a|an|for|on|with|about|from)\s+", "", stripped)
    stripped = re.sub(r"(?i)\s+(of|the|a|an|for|on|with|about|from)$", "", stripped)
    return stripped if re.search(r"[A-Za-z0-9]", stripped) else first


def _slugify(text, fallback="meeting"):
    """Turn a free-text meeting title into a safe filename fragment."""
    first = (text.strip().splitlines() or [""])[0]
    first = first.strip().strip('"\'')
    stripped = _ARTEFACT_WORDS.sub(" ", first)
    # Keep the stripped version only if something survives. "Transcript" alone
    # is a poor title, but it beats an empty one.
    if re.search(r"[A-Za-z0-9]", stripped):
        first = stripped
    first = re.sub(r"(?i)^title[:\-\s]+", "", first)  # drop a stray "Title:" label
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")
    # Removing an artefact word can leave the connector that pointed at it --
    # "recording of standup" becomes "of-standup". Drop those from either end.
    slug = re.sub(r"^(of|the|a|an|for|on|with|about|from)-", "", slug)
    slug = re.sub(r"-(of|the|a|an|for|on|with|about|from)$", "", slug)
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


@dataclass
class Pending:
    """A meeting whose words are saved and whose notes are not yet written.

    Handed from :meth:`NoteTaker.save_transcript` to :meth:`NoteTaker.write_notes`,
    across a thread and quite possibly after the *next* meeting has already
    started recording. So it carries everything the notes step needs and holds
    no reference to live recorder state -- the recorder has moved on.
    """

    transcript: str
    transcript_path: str
    base: str
    title: str
    titled: bool          # True when a calendar event named it: no model call
    stamp: str
    out_dir: str
    event: object = None


class NoteTaker:
    def __init__(self, on_line=None, provider=None, on_partial=None,
                 on_level=None):
        """on_line(text) is called for each newly transcribed line (for live UI).
        on_partial(text, label) is called with provisional text while somebody
        is still speaking — pass it only if you have somewhere to show it.
        provider: optional CalendarProvider for naming/email/post-back."""
        self.on_line = on_line or (lambda text: None)
        self.on_partial = on_partial
        #: Input level for a meter, if the front end has one. Passed straight
        #: down: nothing here reads it, so there is nothing here to keep in
        #: step with it.
        self.on_level = on_level
        self.provider = provider

        self.transcriber = build_transcriber()  # faster-whisper, or your own engine
        # Mic, system audio, or both — see config.CAPTURE_MODE.
        self.listener = build_listener(
            self._on_utterance,
            on_partial=self._on_partial if on_partial else None,
            on_level=on_level)

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
        """Open the microphone now; build the speech model alongside it.

        The order here is the whole of the start latency. Loading the model
        first meant the microphone did not open until it finished -- 0.5 to 2
        seconds on a warm cache and several on a cold one, every time, because
        the model is released while idle. Nothing about capturing audio needs a
        neural network, so it no longer waits for one.

        What still happens up front is the *check*: an English-only model asked
        for another language is refused before recording starts, because that
        is a mistake the user can fix and a recording made under it is wasted.

        The queue between the two is unbounded, so audio captured while the
        model is still building is transcribed when it arrives rather than
        dropped. The first line appears at the same moment it always did; the
        difference is that the meeting is now being recorded while you wait for
        it.
        """
        if self.listening:
            return
        precheck = getattr(self.transcriber, "precheck", None)
        if callable(precheck):
            precheck()  # cheap, and raises before anything is recorded

        with self._lock:
            self._transcript = []
        self._dirty = False
        self.listening = True
        self._worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._worker.start()
        self.listener.start()
        self._warm_model()

    def _warm_model(self):
        """Build the speech model on a background thread, if it has one.

        A failure here cannot be raised at the caller -- recording has already
        started by the time it happens -- so it is written into the transcript
        itself. That is where somebody looking for their missing text will
        actually be looking, and an empty transcript with no explanation is the
        worse outcome.
        """
        load = getattr(self.transcriber, "load", None)
        if not callable(load):
            return

        def warm():
            try:
                load()
            except Exception as e:  # noqa: BLE001 - never kill the recording
                print(f"[model] could not load: {e}", file=sys.stderr, flush=True)
                self.on_line(f"[model] could not load: {e} "
                             f"-- audio is still being recorded and saved.")

        threading.Thread(target=warm, daemon=True).start()

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

    def save_transcript(self, event=None):
        """Write the words to disk and nothing else. Returns ``(Pending, error)``.

        Split out of :meth:`save` because the two halves cost wildly different
        amounts. This half is file I/O -- milliseconds. The other half is one
        or two local model calls and takes a minute or more on a real meeting.
        A caller that needs the microphone back immediately (the window, so the
        next meeting can start) runs this, returns, and hands the ``Pending``
        to :meth:`write_notes` on a background worker.
        """
        transcript = self.transcript_text()
        if not transcript.strip():
            return None, "Nothing was transcribed — no notes to save."

        out_dir = store.notes_dir()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # A calendar event names the meeting for free. Without one the name has
        # to come from the model, and that is a call we are deliberately not
        # making yet -- so the file takes a timestamp now and its real name
        # later, in _rename_to_title.
        titled = bool(event and event.title)
        if titled:
            title = event.title
            base = f"{datetime.now().strftime('%Y-%m-%d')}_{_slugify(title)}"
        else:
            title = ""
            base = f"meeting_{stamp}"

        transcript_path = _unique(os.path.join(out_dir, f"{base}-transcript.txt"))
        header = title or "Meeting Transcript"
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(f"{header}\n{'=' * len(header)}\nSaved: {stamp}\n\n"
                    f"{transcript}\n")

        # The words are on disk, which is the whole of what _dirty guards. An
        # unwritten summary is not unsaved work in that sense: it can be made
        # again from this file, and the audio it came from cannot.
        self._dirty = False
        return Pending(transcript=transcript, transcript_path=transcript_path,
                       base=base, title=title, titled=titled, stamp=stamp,
                       out_dir=out_dir, event=event), None

    def write_notes(self, pending):
        """Name the meeting, summarise it, write the summary beside the
        transcript. The slow half of :meth:`save`.

        Takes the :class:`Pending` from :meth:`save_transcript` and returns the
        same ``(paths, error)`` pair :meth:`save` has always returned.
        """
        transcript = pending.transcript
        base = pending.base
        title = pending.title

        if not pending.titled:
            # Clean the title once, so the heading inside the file and the name
            # on disk agree. Stripping only in _slugify left a file called
            # "...-ptacotty-meeting-summary.txt" whose first line read
            # "ptacotty meeting transcript", which is the same confusion one
            # layer down. A calendar title is the user's own words and is left
            # alone. An unreachable model returns "", and the meeting keeps the
            # timestamp name it already has.
            title = _clean_title(generate_title(transcript))
            if title:
                base = self._rename_to_title(pending, title)

        summary_error = None
        notes = None
        summary_path = _unique(os.path.join(pending.out_dir, f"{base}-summary.txt"))
        try:
            # A meeting too long for the model's context window is summarised in
            # parts, and that takes minutes rather than seconds. Say so as it
            # goes: a long silence is indistinguishable from a hang.
            def progress(done, total):
                if total > 1:
                    self.on_line(f"[notes] summarising part {done} of {total}...")

            notes = to_plain_text(summarize(transcript, on_progress=progress))
            with open(summary_path, "w", encoding="utf-8") as f:
                # No timestamp in the heading. The date is already in the
                # filename, and a summary that opens with a clock time reads
                # like a log entry rather than a set of notes.
                #
                # Written as plain text, not Markdown. Nothing that reads this
                # renders Markdown -- not Notepad, not the email body, not the
                # calendar description -- so the syntax was only ever visible
                # as "##" and "- [ ]" clutter.
                heading = title or "Meeting Notes"
                f.write(f"{heading}\n{'=' * len(heading)}\n\n{notes}\n")
        except Exception as e:  # noqa: BLE001
            summary_error = str(e)
            summary_path = None

        return ({"transcript": pending.transcript_path, "summary": summary_path,
                 "title": title or base, "notes": notes}, summary_error)

    def _rename_to_title(self, pending, title):
        """Give the transcript its real name, now that the model has supplied
        one. Returns the base the summary should use.

        Both files say what they are, and both must agree. They used to be
        "<base>.txt" and "<base>-notes.md", which reads fine until the model
        titles a meeting something like "ptacotty meeting transcript" -- and
        then the folder holds "...-transcript.txt" and "...-transcript-notes.md"
        and neither looks like the summary. That happened, and the summary was
        reported missing when it was sitting right there.

        So when the rename fails -- Windows locks a file somebody has open, and
        by now the user has had a minute to open it -- the provisional base is
        returned unchanged and the summary is named to match the transcript
        where it actually is. A matching ugly pair beats a mismatched pretty one.
        """
        base = f"{datetime.now().strftime('%Y-%m-%d')}_{_slugify(title)}"
        target = _unique(os.path.join(pending.out_dir, f"{base}-transcript.txt"))
        try:
            os.replace(pending.transcript_path, target)
        except OSError as e:
            print(f"[notes] keeping {os.path.basename(pending.transcript_path)}"
                  f" — could not rename: {e}", flush=True)
            return pending.base
        pending.transcript_path = target
        # The base, not the name the file actually got. Two meetings titled the
        # same on the same day make _unique append "-2" to the second, and the
        # summary then wants "<base>-summary-2.txt" -- which is what _unique
        # gives it on its own. Deriving the base back out of the file name
        # instead produced "..._pricing-review-t-summary.txt", because the "-2"
        # moved the suffix and the strip cut into the title.
        return base

    def save(self, event=None):
        """Name the meeting, then write the transcript (.txt) and summary (.md).
        Files are named ``<date>_<meeting-title>``. When ``event`` is given, its
        calendar title is used instead of asking the model to invent one.
        Returns (paths, error); ``paths['title']`` is the human meeting name and
        ``paths['notes']`` is the summary as plain text (for emailing/posting back).

        Both halves back to back, for callers that can afford to wait. Callers
        that cannot run the two themselves -- see :meth:`save_transcript`.
        """
        pending, err = self.save_transcript(event=event)
        if pending is None:
            return None, err
        return self.write_notes(pending)

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
