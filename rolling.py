"""Notes for a meeting that does not fit in the model's context window.

The whole-transcript call is the right one whenever it works, and it stays the
default. This is what happens when it cannot run at all.

Measured, on the built-in engine at ``n_ctx=8192``:

    transcript   prompt tokens   fits (7168 after reserving output)
       409 w         1 281       yes
     3 002 w         4 676       yes
     4 504 w         6 643       yes
     6 006 w         8 610       no
     9 008 w        12 541       no

So the ceiling is about 4 500 words, which at ordinary speaking pace is half an
hour. Past it llama.cpp raises ``ValueError: Requested tokens (12570) exceed
context window of 8192``, :meth:`NoteTaker.save` catches it into
``summary_error``, and the user gets a transcript and no notes. Not a
degradation -- nothing.

How this fixes it
-----------------
Split the transcript into parts that comfortably fit, ask the model what each
part *contains*, then build the notes from those answers. The model never sees
more than one part.

Two decisions carry this, and both were arrived at by watching the obvious
version fail.

**The map step returns structured facts, not prose.** A summary of summaries
goes mushy -- each pass paraphrases the last and detail evaporates -- while
decisions and commitments are already discrete and merge without loss.

**The merge is code, not a model call.** Handing a 1.5B model every part's
answer and asking for four sections produced, on a 7 917-word meeting, a
fifteen-sentence Summary, one discussion point, and nothing else -- no
Decisions and no Action Items, from a transcript full of both. An earlier
attempt lost the action-item format entirely. Merging lists is deduplication,
not language, and code does it exactly every time. The model is left with the
one job that needs language: the opening paragraph. Everything a reader comes
back to is carried through mechanically from an answer that was looking at the
real transcript, so no bullet can be something invented at merge time.

What it costs, measured on an 8 755-word meeting the whole-transcript call
cannot summarise at all:

    chunk size   parts   after Stop   total CPU   sections
       300 w      32       45.1 s      527.4 s      4/4
       500 w      18       29.4 s      209.2 s      4/4
       800 w      11       38.2 s      187.1 s      4/4

and on a 4 487-word meeting, where both approaches run:

    whole transcript        120.2 s     120.2 s      4/4
       800 w       6       26.5 s       93.0 s      4/4
      1200 w       4       24.0 s       75.0 s      4/4

800 words is the default because it is the smallest size whose total cost has
stopped falling. 300 is three times the work for no benefit -- more parts means
more reduce input, and the reduce is the expensive call.

Honest limits
-------------
Nothing here has been shown to produce *better* notes than the whole-transcript
call, and this module does not claim it. On the 4 487-word meeting both
produced four sections and neither recovered most of the specific facts a
scoring pass looked for -- the whole-transcript call scored 3/17 and chunking
5/17, which is too close and too low to separate them. What is established is
that chunking produces notes where the alternative produces an error, and that
the wait after Stop stops growing with the length of the meeting.
"""

from __future__ import annotations

import config
from summarizer import OUTPUT_RESERVE_TOKENS, strip_timestamps

#: Words per part, before overlap. See the module docstring for the numbers.
#: Config-overridable because the right value depends on the model's context,
#: and somebody running a 32k model has no reason to be held to a 1.5B's limit.
DEFAULT_CHUNK_WORDS = 800

#: Carried from the end of one part into the start of the next. A commitment
#: often straddles a turn boundary -- "can you own that?" / "yes, by Thursday"
#: -- and a split between those two lines loses the owner and the date, which
#: are the two things an action item is for.
OVERLAP_WORDS = 40

#: Tokens per word, for engines with no tokenizer to ask. Measured across the
#: fixtures at 1.31 for transcript text; rounded up, because guessing low here
#: means overflowing the context and guessing high only means smaller parts.
TOKENS_PER_WORD = 1.6


#: The map prompt, and the reason it is a placeholder template.
#:
#: Three versions, and the two that failed are worth keeping written down.
#:
#: A *description* -- four heading names on four lines, then prose explaining
#: each -- was read as a list. The model answered "TOPICS: - DECISIONS -
#: ACTIONS - QUESTIONS", copying the labels back as content, and another part
#: came back "ACTIONS: You, end of day", an owner and a date attached to no
#: task.
#:
#: A *worked example* with realistic content fixed the shape and broke
#: something far worse: the model copied the example into the notes. A meeting
#: about incident triage and budget produced notes about an office move, a
#: server room and "Nadia: book the van", none of which existed anywhere but in
#: this file. That is fabricated content in a user's notes, which is the one
#: failure this product cannot have -- and it is only visible here because the
#: example was written about nothing a real meeting would contain.
#:
#: So: the shape, with placeholders that cannot be mistaken for findings. A
#: model that copies these emits "<...>" and the mistake is obvious to anyone
#: reading, rather than a plausible meeting that never happened.
#: The shape of a map answer, kept as its own constant so a test can check that
#: every line in it is still a placeholder rather than something a model could
#: lift into somebody's notes. See :data:`_MAP_PROMPT` for what happened when
#: it was not.
ANSWER_SHAPE = """TOPICS
- <what was discussed, in the words that were used>
DECISIONS
- <what the group settled on>
ACTIONS
- <who committed> : <what they will do> — <when, if a day was named>
QUESTIONS
- <what was asked and left unanswered>"""


_MAP_PROMPT = """You are reading one part of a longer meeting transcript and \
writing down what is in it. Answer in exactly this shape, keeping all four \
headings and their order, and replacing each placeholder with what the \
transcript actually says:

""" + ANSWER_SHAPE + """

Rules:
- The four headings are labels. Never write them as items.
- ACTIONS is for something a person committed to doing. Start the line with who \
said it, then what, then any day they named. "I'll take that one" is a \
commitment; so is "yes" answering "can you own this?".
- DECISIONS is what the group settled on. It is rarely announced as a decision \
-- look for the sentence that ends an argument.
- Use the words that were actually said. Do not summarise them into a category.
- Write "none" under a heading with nothing under it. Do not invent a name, a \
date or a task to fill one.
- Ignore any line you cannot make sense of, and ignore small talk about audio, \
calendars and whether people can hear each other. A microphone picks up room \
noise and speech recognition renders it as plausible-looking nonsense.
- This is one part of a longer meeting and may start or end mid-topic. Report \
what is here. Do not guess at what came before or after.

PART:
{part}

YOUR ANSWER
"""


#: Only the opening paragraph is written by the model. The three lists are
#: assembled from the map answers in code -- see :func:`_assemble` for why.
_SUMMARY_PROMPT = """Below is everything that happened in one meeting, already \
gathered into lists. Write ONE paragraph of 3 to 5 sentences saying what the \
meeting was about as a whole.

Only the paragraph. No headings, no bullet points, no list, no preamble, and \
nothing after it. Do not mention parts, excerpts or lists -- the reader was in \
the meeting and does not know it was gathered this way.

Use only what is below and do not add anything that is not there.
{language_rule}
WHAT HAPPENED:
{facts}

THE PARAGRAPH:
"""


def chunks(transcript: str, words: int = 0, overlap: int = OVERLAP_WORDS):
    """Split a transcript into overlapping parts, never mid-utterance.

    Splitting on a word count alone would cut through the middle of a sentence,
    and half a sentence handed to a model is worse than no sentence -- it gets
    completed rather than reported. Lines are whole utterances as the segmenter
    produced them, so they are the natural seam.
    """
    words = words or int(getattr(config, "NOTES_CHUNK_WORDS", 0) or
                         DEFAULT_CHUNK_WORDS)
    lines = [l for l in strip_timestamps(transcript).splitlines() if l.strip()]
    out, cur, n = [], [], 0
    for line in lines:
        cur.append(line)
        n += len(line.split())
        if n >= words:
            out.append("\n".join(cur))
            tail, kept = [], 0
            for l in reversed(cur):
                tail.insert(0, l)
                kept += len(l.split())
                if kept >= overlap:
                    break
            cur, n = list(tail), kept
    # A trailing remnant that is only the overlap is already in the last part;
    # emitting it again would put a duplicate copy of it in front of the model.
    if cur and (not out or sum(len(l.split()) for l in cur) > overlap):
        out.append("\n".join(cur))
    return out


def count_tokens(engine, text: str) -> int:
    """How many tokens ``text`` costs on this engine.

    An engine that carries a tokenizer answers exactly. One that does not gets
    an estimate, deliberately on the high side: estimating low overflows the
    context window and estimating high only makes the parts smaller.
    """
    counter = getattr(engine, "count_tokens", None)
    if callable(counter):
        try:
            got = counter(text)
            if got is not None:
                return int(got)
        except Exception:                          # noqa: BLE001
            pass                                   # fall through to the estimate
    return int(len(text.split()) * TOKENS_PER_WORD) + 1


def budget(engine) -> int:
    """Prompt tokens this engine can accept, or 0 when it does not say.

    Zero means "no known ceiling", and every caller here treats that as "the
    whole-transcript call is fine" -- which is the behaviour every engine had
    before this module existed.
    """
    fn = getattr(engine, "prompt_budget", None)
    if not callable(fn):
        return 0
    try:
        return int(fn() or 0)
    except Exception:                              # noqa: BLE001
        return 0


def fits(engine, prompt: str) -> bool:
    """True when ``prompt`` can be sent to ``engine`` as one call."""
    room = budget(engine)
    return room <= 0 or count_tokens(engine, prompt) <= room


def _map_part(engine, part: str) -> str:
    """Pull the facts out of one part. **No persona reaches this call.**

    This step decides *what exists* in the meeting, and the whole design rests
    on it being complete -- every bullet a reader comes back to is carried
    through from here mechanically by :func:`_assemble`. Telling it whose notes
    these are invites it to leave out the part that does not look relevant to
    them, and a fact dropped here is gone: no later step can recover it.

    Personalisation belongs in :func:`_summarise`, which writes prose from
    facts already found.
    """
    # 400 tokens, not the notes cap: this answer is a list of short lines, and
    # a generous cap on a small model is an invitation to write an essay.
    return engine._complete(_MAP_PROMPT.format(part=part), 400, 0).strip()


#: The four map headings, in the order they are written back out.
_HEADINGS = ("TOPICS", "DECISIONS", "ACTIONS", "QUESTIONS")

#: Where each one lands in the finished notes.
#:
#: QUESTIONS is collected and then thrown away, which looks wasteful and is
#: not. Two reasons.
#:
#: It has nowhere to go. The notes have four fixed sections, the same four the
#: whole-transcript path produces, and a fifth heading appearing only on long
#: meetings would make chunked notes a different document from ordinary ones.
#: Folding them into the discussion points was tried instead and was worse:
#: every interrogative the model noticed landed in the notes, so a real meeting
#: came back with "Can everyone still hear me, my connection dropped for a
#: second there" and "The group asked several questions, including:" sitting
#: among the actual topics.
#:
#: And asking for them still earns its place. Given nowhere to put a question,
#: a small model files it under DECISIONS or ACTIONS -- "Should we start the
#: pilot on a single team?" becomes a decision that was never taken. The
#: heading is a bin, and the bin is what keeps the other three clean.
_SECTIONS = (("## Key Discussion Points", ("TOPICS",), False),
             ("## Decisions", ("DECISIONS",), False),
             ("## Action Items", ("ACTIONS",), True))

_NOTHING = {"", "none", "none.", "n/a", "nothing", "no decisions",
            "no actions", "no questions", "none recorded", "none recorded."}

#: Phrases a model uses to say "nothing here" in a whole sentence rather than
#: in one word. Without these the notes carry lines like "There were no
#: questions asked and left unanswered." three times over, which is worse than
#: an empty section: it reads as a finding.
_NOTHING_PHRASES = ("no questions", "no decisions", "no actions", "none were",
                    "nothing was", "nothing to report", "no items",
                    "were not asked", "was not asked", "none recorded",
                    "no specific", "not mentioned", "no further")

#: Longest an item may be. A bullet is one line in a set of notes; anything
#: this long is a model that did not stop, and one such line arrived 900
#: characters long with the same twelve-item list repeated eight times inside
#: it. Rejecting it outright is right -- there is no prefix of that worth
#: keeping, and truncating would leave a sentence cut mid-word.
MAX_ITEM_CHARS = 300

#: Most items any one section will carry. Ten parts each offering four to eight
#: findings makes sixty discussion points, which nobody reads -- the notes stop
#: being a summary at that point and become a second transcript. Items arrive
#: in the order they were said, so keeping the first ones keeps the meeting's
#: shape rather than an arbitrary slice.
MAX_ITEMS_PER_SECTION = 15

#: How much two bullets must overlap to count as the same one.
#:
#: Pro's extractor uses 0.6 for the same job. This is higher, and deliberately,
#: because the two are asked different questions. There it compares a model's
#: claim against transcript lines, where a near miss means a citation points a
#: line off. Here a near miss deletes a finding from somebody's notes, and
#: nothing downstream can recover it.
#:
#: 0.6 was measured wrong here. Short bullets share their scaffolding: "whether
#: invoicing needs rework" and "whether onboarding needs rework" have three
#: words of four in common and score 0.75, and merging those loses a topic
#: entirely. At 0.8 they survive as two, which is right, and so does the pair
#: "The dashboard was not checked this morning" / "...not looked at this
#: morning", which is wrong but only untidy.
#:
#: That is the trade, and it is not symmetric. A duplicate left in the notes is
#: a reader skimming one extra line. A wrong merge is a decision that was taken
#: in the meeting and is not in the notes.
SIMILAR_ENOUGH = 0.8

#: Shared words needed before similarity is even considered. Two three-word
#: bullets can hit any ratio by accident; a bullet that short is better kept.
MIN_SHARED_WORDS = 3

#: Words too common to say anything about whether two lines mean the same
#: thing. Without this every line that mentions "the" looks like every other.
_NOISE = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is",
          "was", "were", "be", "will", "that", "this", "it", "with", "at",
          "by", "as", "we", "i", "you", "they", "he", "she", "not", "no"}


def parse_facts(text: str) -> dict:
    """Pull the four lists out of one map answer.

    Tolerant on purpose. A small model will write ``DECISIONS``, ``**DECISIONS:**``
    or ``### Decisions`` for the same thing, and rejecting two of those would
    throw away most of a meeting over punctuation.
    """
    found = {h: [] for h in _HEADINGS}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip().strip("*#").strip()
        bare = line.rstrip(":").strip().upper()
        if bare in found:
            current = bare
            continue
        if current is None:
            continue
        item = line.lstrip("-*+ ").strip()
        if item.lower() in _NOTHING:
            continue
        # A placeholder copied out of the prompt is not a finding.
        if item.startswith("<") and item.endswith(">"):
            continue
        if item:
            found[current].append(item)
    return found


def _key(item: str) -> str:
    """Case, punctuation and spacing dropped, for an exact-duplicate check."""
    kept = "".join(c if (c.isalnum() or c.isspace()) else " " for c in item.lower())
    return " ".join(kept.split())


def _words(item: str) -> set:
    return {w for w in _key(item).split() if w not in _NOISE and len(w) > 2}


def _similar(a: set, b: set) -> bool:
    """True when two bullets carry substantially the same words.

    Measured against the smaller of the two, not their union: "Ship Friday if
    the duplication test passes" and "The group decided to ship Friday only if
    the duplication test passes" are one decision, and a symmetric measure
    scores that pair too low to catch because one line is half again as long.
    """
    if not a or not b:
        return False
    shared = len(a & b)
    if shared < MIN_SHARED_WORDS:
        return False
    return shared / min(len(a), len(b)) >= SIMILAR_ENOUGH


def _is_nothing(item: str) -> bool:
    """True when the item is a model saying there is nothing to report.

    Only for short lines. "No decisions were recorded" is an empty section;
    "No decision on pricing until legal signs off, so the launch slips" is a
    finding that happens to start with the same two words, and length is what
    separates them without trying to read either.
    """
    # Both forms, because normalising drops the punctuation that makes some of
    # these recognisable: "N/A" keys to "n a", which matches nothing.
    if item.strip().lower() in _NOTHING or _key(item) in _NOTHING:
        return True
    low = _key(item)
    return len(low.split()) <= 12 and any(p in low for p in _NOTHING_PHRASES)


def _looks_runaway(item: str) -> bool:
    """True when one bullet is a generation that did not stop.

    Two signals, because either alone has honest exceptions. Length on its own
    would reject a long but real commitment; repetition on its own would reject
    a line that legitimately says "the pilot" twice. Together they are only
    true of the failure -- the line that arrived was 900 characters holding the
    same twelve items over and over.
    """
    if len(item) > MAX_ITEM_CHARS:
        return True
    words = _key(item).split()
    if len(words) < 24:
        return False
    # A phrase repeated inside one bullet is the model looping.
    grams = [" ".join(words[i:i + 5]) for i in range(len(words) - 4)]
    return len(set(grams)) < len(grams) * 0.8


def _assemble(engine, facts, language_directive: str, system: str = "") -> str:
    """Build the notes: three lists in code, one paragraph from the model.

    The merge used to be a single model call that was handed every part's
    answer and asked for all four sections. It does not survive contact with a
    1.5B model. Measured on a 7 917-word meeting: it wrote a fifteen-sentence
    Summary, one Key Discussion Point, and then stopped -- no Decisions, no
    Action Items, on a transcript containing plenty of both. An earlier run put
    "- Dashboard: You, Participants" under Action Items six times, having lost
    the format entirely.

    Both failures are the same mistake. Merging lists is not a language problem
    -- it is deduplication, and code does it exactly, in the right order, with
    the right markers, every time. Asking a small model to do it invites it to
    paraphrase, reorder, drop a section or invent one.

    So the model is left with the one job here that actually needs language:
    three to five sentences of prose. Everything a reader comes back to later
    -- the decisions and the commitments -- is carried through mechanically
    from an answer that was looking at the real transcript, which also means no
    line in those three sections can be something the model made up at merge
    time.
    """
    merged = {h: [] for h in _HEADINGS}
    kept_words = {h: [] for h in _HEADINGS}
    for one in facts:
        for head, items in parse_facts(one).items():
            for item in items:
                if not _key(item) or _is_nothing(item) or _looks_runaway(item):
                    continue
                words = _words(item)
                if any(_similar(words, prev) for prev in kept_words[head]):
                    continue
                if len(merged[head]) >= MAX_ITEMS_PER_SECTION:
                    continue
                kept_words[head].append(words)
                merged[head].append(item)

    out = []
    summary = _summarise(engine, merged, language_directive, system)
    out.append("## Summary\n" + summary if summary else "## Summary\nNone recorded.")
    for title, heads, checkbox in _SECTIONS:
        lines = []
        for head in heads:
            for item in merged[head]:
                lines.append(("- [ ] " if checkbox else "- ") + item)
        out.append(title + "\n" + ("\n".join(lines) if lines else "None recorded."))
    return "\n\n".join(out)


def _summarise(engine, merged, language_directive: str, system: str = "") -> str:
    """The opening paragraph, and only that.

    Capped at 300 tokens rather than the 1024 the notes used to get: this is
    one paragraph, and a generous cap on a small model is an invitation to keep
    going -- which is exactly how the whole of a meeting ended up inside the
    Summary.
    """
    # Only what the notes will actually show. Handing the paragraph the
    # discarded questions puts "the group asked several questions" into prose
    # that is meant to say what the meeting was about.
    shown = [h for _, heads, _ in _SECTIONS for h in heads]
    facts = "\n".join(
        f"{head}\n" + "\n".join(f"- {i}" for i in merged[head])
        for head in _HEADINGS if head in shown and merged[head])
    if not facts.strip():
        return ""
    prompt = _SUMMARY_PROMPT.format(facts=facts, language_rule=language_directive)
    if not fits(engine, prompt):
        # Too many facts to summarise in one go. The lists below are complete
        # either way, so trimming what the paragraph is drawn from costs a
        # little breadth in one sentence and nothing at all in the notes.
        keep = facts[:max(1000, budget(engine) * 2)]
        prompt = _SUMMARY_PROMPT.format(facts=keep, language_rule=language_directive)
    text = engine._complete(prompt, 300, 0, system=system).strip()
    # A model that ignored "no headings" gets them taken off rather than
    # producing notes with a heading inside a paragraph.
    lines = [l for l in text.splitlines()
             if l.strip() and not l.strip().startswith(("#", "-", "*"))]
    return " ".join(lines).strip()


def summarize(engine, transcript: str, language_rule: str = "",
              on_progress=None, system: str = "") -> str:
    """Notes for a transcript too long to summarise in one call.

    ``on_progress(done, total)`` is called after each part, so a caller that has
    a window can say what is happening. This can take minutes on a long meeting
    and a frozen button is how a working program looks broken.
    """
    parts = chunks(transcript)
    facts = []
    for i, part in enumerate(parts):
        facts.append(_map_part(engine, part))
        if on_progress:
            try:
                on_progress(i + 1, len(parts))
            except Exception:                      # noqa: BLE001
                pass                               # never let a UI hook stop the notes
    return _assemble(engine, facts, language_rule, system)
