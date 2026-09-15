"""Turn a raw transcript into structured meeting notes — bring your own model.

The transcript may be in any language, or several — lines are tagged with the
speaker and the detected language code. ``config.NOTES_LANGUAGE`` decides
whether the notes come back in English or in whatever was spoken.

Like :mod:`transcriber`, the engine is pluggable and the rest of the app never
hardcodes one. ``config.SUMMARY_ENGINE`` picks a built-in; ``CUSTOM_SUMMARIZER``
attaches anything else. Use :func:`build_summarizer` to get the configured
engine.

Any engine needs two methods, and may add a third:
    summarize(self, transcript)  -> Markdown notes
    title(self, transcript)      -> a short title, or "" if unavailable
    load(self)                   -> warm up (optional)

Everything above that line -- the prompts, the language rule, the timestamp
handling, the plain-text rendering -- belongs to the *product*, not to any one
engine, so it stays here and every backend gets it for free. A backend's whole
job is to turn a prompt into text.

Why this shape, when Ollama was the only engine for a year: because "bring your
own model" is the claim on the website, and a claim that rests on one process
being installed is one process away from being untrue. Ollama stays a
first-class backend and the default; it is simply no longer the only door.
"""

import importlib
import re

import requests

import config
import languages

#: Tokens held back from the context window for the model's own answer. Notes
#: are capped at 1024 tokens, so a prompt allowed to fill the rest would leave
#: nothing to write them in. One source of truth: :mod:`rolling` reads this too.
OUTPUT_RESERVE_TOKENS = 1024

# "[10:04:22] " or "[10:04] " at the head of a transcript line.
_TS = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", re.MULTILINE)
# The same marker anywhere in a line, for scrubbing what the model returns.
_TS_ANY = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")


def strip_timestamps(text: str) -> str:
    """Remove ``[HH:MM:SS]`` markers from a transcript, or from model output.

    The summary used to come back full of timestamps, which made the notes
    read as a second copy of the transcript rather than a summary of it. The
    cause was not the model misbehaving. It was handed a timestamp on every
    line and told the transcript was timestamped, so it mirrored the format
    back -- exactly what a language model is supposed to do. The fix is to
    stop showing it something we do not want returned.

    Speaker labels are deliberately kept: "You:" and "Them:" are what make an
    action item attributable to somebody.
    """
    return _TS.sub("", text or "")


def scrub_timestamps(text: str) -> str:
    """Belt and braces for the model's output.

    The instruction alone is not reliable across the range of local models
    people run -- a 3B model will cheerfully ignore it, and the failure is
    silent and ugly. Cheap to do, so do it.
    """
    return _TS_ANY.sub("", text or "")

_LANGUAGE_RULE = """
The transcript may be in any language, and may switch between languages. Lines \
may be tagged with the speaker and a language code, like "You (hi):". \
Those tags are metadata — never copy them into the notes. \
{directive}
"""

_PROMPT = """You are a meeting notes assistant. Below is a raw transcript of a \
meeting captured from a microphone. It may contain transcription errors, filler \
words, and incomplete sentences. Produce clean, professional meeting notes in \
Markdown with exactly these sections:

## Summary
A short paragraph (3-5 sentences) capturing what the meeting was about.

## Key Discussion Points
- Bullet points of the main topics discussed.

## Decisions
- What the group settled on. Decisions in real meetings are short and plain --
"then let's ship on Friday", "park it until the installer lands", "we'll go
with the second option". They are rarely announced as decisions, so look for
the sentence that ends an argument rather than a heading.

Something can be a discussion point and a decision at once; put it here too if
it is one. Do not leave this section empty just because the point appears
above. Write "None recorded." only after looking and finding none.

## Action Items
One line per task, a checkbox, in this shape:

- [ ] what was agreed — who said they would do it — when

Every part comes from the transcript. Leave out the owner or the when if they
were not said. The three descriptions above are labels for the parts, not an
answer: never write "what was agreed", "Task", "owner" or "due date" into the
notes, and never invent a name or a day to fill a gap.

Anything anyone committed to doing counts. A commitment is usually first
person and plain -- "I will have it done by Thursday", "I'll own that one",
"leave it with me" -- and a question like "can you own X?" answered "yes" is a
commitment by whoever answered. The owner is the person who said it. A day
named out loud ("Thursday", "end of the week") is the due date.

A decision says what will happen; an action item says who is doing it and by
when. The same piece of work can appear in both. Do not leave this section
empty just because the work is mentioned above. Write "None recorded." only if
nobody committed to anything.

Never write the same line twice. Each bullet must say something the others do
not.

Only use information present in the transcript. Do not invent details.

Some lines will be garbled, repeated, or half-finished -- a microphone picks up
room noise and the far end of a call, and speech recognition renders that as
plausible-looking nonsense. Ignore any line you cannot make sense of. Do not
summarise it, do not treat it as a topic, and do not let it dilute the notes.
A short set of notes drawn from the lines that are clear beats a long one that
takes the noise seriously.

Do not write clock times such as 10:04 unless a time was actually spoken as part of a decision or a deadline. These are notes, not a log. The timestamped record already exists in the transcript file saved beside this one, and repeating it here would make the two files copies of each other.
{language_rule}
TRANSCRIPT:
{transcript}
"""


_TITLE_PROMPT = """Give a short, descriptive title for the meeting described by \
the transcript below. Use 3 to 6 words. Respond with ONLY the title text — no \
quotes, no "Title:" label, no trailing punctuation, no explanation. \
Write the title in English even if the transcript is in another language, and \
use only plain ASCII letters — the title becomes a file name.
Name what the meeting was ABOUT. Never use the words "transcript", "recording", "notes", "summary" or "audio" — those describe the file, not the conversation, and they end up duplicated in the file name.

TRANSCRIPT:
{transcript}
"""


def _language_directive() -> str:
    """Just the "write them in X" sentence, with no transcript talk around it.

    The full rule tells the model what a ``You (hi):`` tag is and not to copy
    it. That is right in front of a transcript and wrong in front of anything
    else -- the merge step in :mod:`rolling` is handed lists of facts, and
    explaining transcript markup there is an instruction about something not
    present, which is how a small model starts inventing speakers.
    """
    setting = (getattr(config, "NOTES_LANGUAGE", "en") or "en").lower()
    if setting == "same":
        directive = ("Write the notes in the same language the meeting was "
                     "conducted in. If several were used, choose the dominant "
                     "one and keep the whole set of notes in it.")
    else:
        directive = (f"Write the notes in {languages.name_for(setting)}, "
                     "translating as needed, no matter what language was "
                     "spoken. Keep names, products and quoted phrases as they "
                     "were said.")
    return directive


def _language_rule() -> str:
    """The language directive, wrapped in what a transcript needs said with it."""
    return _LANGUAGE_RULE.format(directive=_language_directive())


#: The persona, as a system message. Deliberately short: it says who the reader
#: is and what they asked for, and then hands the actual rules back to the
#: prompt. Everything it is allowed to influence is named, and so is everything
#: it is not.
#:
#: The user's own words are quoted rather than pasted in as instructions. They
#: typed them into a settings box, which means they may say something the notes
#: must not do -- "make me sound decisive", "always give a revenue number" --
#: and a 3B model handed that as an instruction will oblige by inventing. As a
#: quoted description of a person it informs; as an instruction it would
#: corrupt.
SEP = chr(10)

_PERSONA = """You are writing meeting notes for one particular person.

{who}

This changes the Summary paragraph only -- what it leads with, and the words it chooses. The lists below it are not yours to shape.

Everything that was said belongs in the notes whether or not it looks relevant to this person's work. Every name, every date and every commitment stays, with whoever said it still attached to it -- a task whose owner has been dropped is worse than no task at all. Nothing that was not said may be added. Where this description and those rules disagree, the rules win."""


def persona() -> str:
    """The system message for this user, or "" when they told us nothing.

    Empty is the out-of-the-box state and a perfectly good answer: with no
    field and no context this returns "", no system message is sent, and the
    model sees exactly what it saw before any of this existed.
    """
    field = (getattr(config, "USER_FIELD", "") or "").strip()
    tone = (getattr(config, "USER_TONE", "") or "").strip()
    context = (getattr(config, "USER_CONTEXT", "") or "").strip()
    if not field and not tone and not context:
        return ""

    lines = []
    if field:
        lines.append(f"Their area of work is {field}.")
    if tone:
        lines.append(f"They asked for notes that read: {tone}.")
    if context:
        # Collapsed to one paragraph: a settings box collects newlines, and a
        # system message reads better without them.
        tidy = " ".join(context.split())
        lines.append("In their own words, asked what they use this for and how "
                     f"they want notes written: \"{tidy}\"")
    return _PERSONA.format(who=SEP.join(lines))


def _notes_prompt(transcript: str) -> str:
    """The notes prompt, timestamps stripped on the way in."""
    return _PROMPT.format(transcript=strip_timestamps(transcript),
                          language_rule=_language_rule())


def _title_prompt(transcript: str) -> str:
    """The title prompt, from the opening of the meeting only."""
    # 1200 characters, not 4000. Naming a meeting needs the opening minutes,
    # not the whole thing, and the cost of this call scales with what it is
    # given: measured on llama3.2 over a 7k-character transcript, the title
    # took 49.7s from 4000 characters, 15.3s from 1500 and 8.0s from 800 --
    # for a *better* title, because a short excerpt gives the model less to
    # ramble about. This call sits directly between a user pressing Stop and
    # their notes appearing, so it is worth being cheap.
    return _TITLE_PROMPT.format(transcript=transcript[:1200])


# --- the engines ----------------------------------------------------------

class OllamaSummarizer:
    """Notes from a model served by Ollama on ``config.OLLAMA_URL``.

    The default, and the only engine until now. It is kept first-class rather
    than deprecated: Ollama is a model *manager* as well as a runtime, and
    ``ollama pull mistral`` is a better experience than "find a GGUF" for
    anyone who already has it installed.
    """

    name = "ollama"

    def model_label(self) -> str:
        return config.OLLAMA_MODEL

    def status(self):
        """(ok, detail) — never raises, because a health check that throws is
        worse than one that says it does not know."""
        try:
            resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=4)
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception as e:                    # noqa: BLE001
            return False, (f"not reachable at {config.OLLAMA_URL} "
                           f"({e.__class__.__name__})")

        want = config.OLLAMA_MODEL
        # Ollama reports "llama3.2:latest" for a model pulled as "llama3.2".
        if any(n == want or n.split(":")[0] == want.split(":")[0] for n in names):
            return True, f"{want} ready"
        if names:
            return False, f"running, but {want} isn't pulled (ollama pull {want})"
        return False, f"running, but no models pulled (ollama pull {want})"

    def prompt_budget(self) -> int:
        """Prompt tokens this engine will accept before the server starts cutting.

        The app used to send no ``num_ctx`` at all, which meant the ceiling was
        whatever the user's Ollama happened to default to -- and Ollama does not
        refuse an over-long prompt, it silently drops the front of it. So a long
        meeting's notes could describe only its second half, on some machines
        and not others, with nothing anywhere saying so.

        Measured on Ollama 0.33.2 here, which loads 16 384 by default:
        ``prompt_eval_count`` came back 1 300 / 2 730 / 4 695 / 8 629 / 12 560
        for 409 / 1 501 / 3 002 / 6 006 / 9 008 words -- linear, so nothing was
        being cut at any size tested. On an older Ollama defaulting to 4 096 the
        same meetings would have been quietly truncated from about 2 300 words.

        Naming the number makes the ceiling ours instead of theirs, and
        :mod:`rolling` handles what does not fit rather than the server
        deciding for us.
        """
        return max(0, self._num_ctx() - OUTPUT_RESERVE_TOKENS)

    @staticmethod
    def _num_ctx() -> int:
        return int(getattr(config, "OLLAMA_NUM_CTX", 8192) or 8192)

    def _generate(self, prompt: str, timeout: int, system: str = "") -> str:
        """One call to Ollama. ``system`` goes in the system slot, not the prompt.

        `/api/generate` takes a `system` field and applies the model template's
        system slot to it -- measured, not assumed. That is where a persona
        belongs: a slot the model treats as standing context, rather than
        instructions competing with the ones that matter in the prompt body.
        Sent only when there is one, so a user who filled nothing in gets a
        request byte-identical to the one this made before any of it existed.
        """
        payload = {"model": config.OLLAMA_MODEL, "prompt": prompt,
                   "stream": False,
                   "options": {"num_ctx": self._num_ctx()}}
        if system:
            payload["system"] = system
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    def _complete(self, prompt: str, max_tokens: int, min_tokens: int = 0,
                  system: str = "") -> str:
        """The raw prompt-to-text call :mod:`rolling` needs.

        ``system`` is passed through rather than assumed: :mod:`rolling` uses
        this for two different jobs -- pulling facts out of one chunk, and
        writing the closing paragraph -- and only the second should carry a
        persona. Priming extraction is how facts go missing.

        The two embedded engines already had one; this gives Ollama the same
        shape so chunked summarisation is not a feature only some engines get.
        ``max_tokens`` and ``min_tokens`` are accepted and not forwarded --
        Ollama's ``num_predict`` would cap the notes and it has no floor at all,
        and every engine here is allowed to ignore a hint it cannot honour.
        """
        return self._generate(prompt, 600, system=system)

    def title(self, transcript: str) -> str:
        try:
            return self._generate(_title_prompt(transcript), 120,
                                  system=persona())
        except requests.exceptions.RequestException:
            return ""

    def summarize(self, transcript: str) -> str:
        try:
            return self._generate(_notes_prompt(transcript), 600,
                                  system=persona())
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                "Could not reach Ollama. Is it running? Start it with "
                f"`ollama serve` and pull the model with `ollama pull "
                f"{config.OLLAMA_MODEL}`."
            ) from e
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Ollama returned an error: {e}") from e


class _EmbeddedSummarizer:
    """Shared shape for engines that run a model inside this process.

    Both subclasses do the same three things -- find a model on disk, load it
    once, turn a prompt into text -- and differ only in which library does the
    running. The parts that are the same live here so that adding a third
    engine later is a `_complete()` method and nothing else.

    Neither ships enabled. They exist so the choice between them can be made on
    measurements rather than on architecture taste, and so Phase 3 is a config
    change rather than a rewrite. `NOTES_MODEL` points at the weights; until it
    does, `build_summarizer` refuses with a message that says what is missing.
    """

    name = "embedded"
    #: Filled in by the subclass, for the error message when nothing is found.
    needs = "a model"

    def __init__(self, path: str = ""):
        import os

        import notes_model

        # A path passed in wins, then whatever the resolution order finds: the
        # user's own setting, a copy the installer shipped, a copy we
        # downloaded. The engine does not care which of the three it got, and
        # nobody has to know the order exists for the common cases to work.
        #
        # Resolved against *this engine's* model, not the platform default.
        # They differ whenever somebody picks the non-default engine by hand,
        # and a GGUF handed to CTranslate2 fails in a way that names nothing.
        self.path = path or notes_model.resolve(notes_model.spec_for(self.name))
        self._model = None
        if not self.path:
            raise RuntimeError(
                f"The built-in note writer needs its model, and this machine "
                f"does not have it yet. Download it in Settings, or point "
                f"NOTES_MODEL at {self.needs}."
            )
        # Unset and wrong are the same mistake to a user, and both are cheap to
        # catch here. Left to the runtime, a bad path surfaces as whatever the
        # loader happens to raise -- llama.cpp's ValueError, CTranslate2's
        # RuntimeError -- at the end of the first meeting rather than when the
        # setting was chosen, which is the one moment the user can act on it.
        if not os.path.exists(self.path):
            raise RuntimeError(
                f"SUMMARY_ENGINE is {self.name!r} but NOTES_MODEL points at "
                f"{self.path}, which does not exist. Point it at {self.needs}."
            )

    def model_label(self) -> str:
        """A name a person recognises, not the path we happen to load from.

        A Hugging Face snapshot directory is named after a commit hash, so the
        obvious basename turns "Qwen2.5-1.5B-Instruct" into "55bb006d..." in
        the status bar and in every diagnostic report. Recover the repo name
        from the ``models--org--name`` component when the path has that shape,
        and fall back to the basename when it does not.
        """
        import os
        import re

        import notes_model

        # The model this app fetches has a name somebody chose; a filename is
        # only what the file system calls it. Prefer the name whenever the path
        # is one we put there ourselves.
        for spec in notes_model.SPECS:
            known = {q for q in (notes_model.downloaded_path(spec),
                                 notes_model.bundled_path(spec)) if q}
            if self.path in known:
                return spec.label

        path = self.path.rstrip("/\\")
        m = re.search(r"models--[^/\\]+--([^/\\]+)", path)
        if m:
            return m.group(1)
        return os.path.basename(path) or self.name

    def status(self):
        """(ok, detail) without loading the model.

        This used to call ``load()``, which is how an idle app came to be
        holding 1.1 GB of weights: Settings asks for status when it draws, the
        status bar asks again, and each answer pulled the whole model into the
        process that also captures audio -- measured at 2029 MB resident before
        a single meeting was recorded.

        The question being asked is "will this work", and the honest cheap
        answer is whether the weights are where they should be. If they are
        unreadable, the first summary says so, which is the moment a user can
        act on it anyway. Nothing needs a gigabyte resident to say "ready".
        """
        import os

        if not os.path.exists(self.path):
            return False, f"{self.path} not found"
        if os.path.isfile(self.path) and os.path.getsize(self.path) == 0:
            return False, f"{self.path} is empty"
        return True, f"{self.model_label()} ready"

    def load(self):
        raise NotImplementedError

    #: Context window, when the subclass cannot ask the runtime for one.
    CONTEXT_TOKENS = 8192

    def prompt_budget(self) -> int:
        """Prompt tokens this engine accepts, with room left for the answer.

        Asking is worth it rather than assuming: the number decides whether a
        meeting is summarised in one call or in parts, and getting it wrong in
        the optimistic direction is the failure this whole seam exists to
        prevent -- ``ValueError: Requested tokens (12570) exceed context window
        of 8192``, and a user with a transcript and no notes.
        """
        return max(0, self.CONTEXT_TOKENS - OUTPUT_RESERVE_TOKENS)

    def count_tokens(self, text: str):
        """Exact token count, or None when this engine has no tokenizer loaded.

        None rather than a guess, so the caller can apply its own conservative
        estimate instead of trusting a number this class made up. Never loads
        the model to answer: a settings screen asking "will this fit" must not
        pull a gigabyte of weights into a process that is only drawing a label.
        """
        return None

    #: Fewest tokens a set of notes may be. Measured, not guessed: see
    #: :class:`CTranslate2Summarizer` for what happens without it and why this
    #: particular number.
    NOTES_MIN_TOKENS = 120

    def _complete(self, prompt: str, max_tokens: int, min_tokens: int = 0) -> str:
        raise NotImplementedError

    def title(self, transcript: str) -> str:
        # No floor here, deliberately. A title is three to six words; forcing
        # two hundred tokens out of one would produce an essay and take longer
        # than the notes it is naming.
        try:
            return self._complete(_title_prompt(transcript), 32).strip()
        except Exception:                         # noqa: BLE001
            return ""

    def summarize(self, transcript: str) -> str:
        return self._complete(_notes_prompt(transcript), 1024,
                              self.NOTES_MIN_TOKENS).strip()


class CTranslate2Summarizer(_EmbeddedSummarizer):
    """Notes from a CTranslate2-converted model, in this process.

    Worth trying first for one reason that has nothing to do with quality:
    CTranslate2 is already a dependency -- faster-whisper runs on it -- so this
    engine adds no package, no licence and no platform matrix. One inference
    library for both speech and text is a smaller thing to ship and a shorter
    answer in a security review. The cost is a thinner ecosystem of converted
    models; whether that costs accuracy is what the benchmark is for.
    """

    name = "ctranslate2"
    needs = "a CTranslate2 model folder (ct2-transformers-converter output)"

    def load(self):
        """Load the generator and its tokenizer.

        Tokenising goes through ``tokenizers`` and not ``transformers``. Both
        can read the ``tokenizer.json`` a converted model carries, but only one
        of them is already here -- faster-whisper depends on ``tokenizers``, so
        it is in the SBOM and the bundle today. Reaching for ``transformers``
        would have pulled in a large new dependency and quietly destroyed the
        only reason this engine is interesting: that it adds nothing.
        """
        if self._model is None:
            import os

            import ctranslate2
            from tokenizers import Tokenizer

            vocab = os.path.join(self.path, "tokenizer.json")
            if not os.path.isfile(vocab):
                raise RuntimeError(
                    f"{self.path} has no tokenizer.json. Convert with "
                    f"ct2-transformers-converter --copy_files tokenizer.json"
                )
            # Ask for "int8" rather than taking the model's saved type. This
            # conversion is saved as int8_bfloat16, which most CPUs cannot run
            # efficiently, so CTranslate2 silently converts it and prints a
            # warning on every single load -- into the log, and into the
            # diagnostic report a user sends to support. Naming the family and
            # letting CTranslate2 pick the variant this CPU actually has is the
            # same computation without the noise, and it stays right on a
            # machine that does support bfloat16.
            self._model = ctranslate2.Generator(self.path, compute_type="int8")
            self._tok = Tokenizer.from_file(vocab)
        return self._model

    def count_tokens(self, text: str):
        """Exact, once the tokenizer is loaded; None before that.

        The tokenizer is a few hundred kilobytes of JSON and the generator is
        the gigabyte, but they load together, so this does not force the load
        -- it answers exactly when the model is already up and defers to the
        caller's estimate when it is not.
        """
        tok = getattr(self, "_tok", None)
        if tok is None:
            return None
        return len(tok.encode(text).tokens)

    #: Instruct models expect their turns marked up, and CTranslate2's
    #: ``Generator`` is a raw next-token loop -- it applies no chat template at
    #: all. Handed a bare prompt, Qwen2.5-Instruct continues the transcript
    #: instead of summarising it; measured here, llama.cpp did exactly the same
    #: thing before its template was applied, echoing one line back forty times.
    #:
    #: ChatML is hardcoded rather than read from ``tokenizer_config.json``,
    #: because rendering the template stored there needs Jinja, and the whole
    #: point of this engine is that it adds no dependency.
    #:
    #: That is a real limitation and worth stating plainly: Qwen, Mistral and
    #: Phi speak ChatML, but Llama 3 does not -- it uses its own
    #: ``<|start_header_id|>`` markup and would come back as a continuation
    #: loop here, exactly as Qwen did before this was added. A model with
    #: different markup needs either another branch here or its own
    #: ``CUSTOM_SUMMARIZER``. Since the licence work points at Qwen for
    #: bundling anyway, one template is the right amount of complexity today.
    _CHATML = ("<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n")
    _STOP = "<|im_end|>"

    def _complete(self, prompt: str, max_tokens: int, min_tokens: int = 0) -> str:
        """Generate, with a floor under the notes.

        Without ``min_length`` this conversion ends its turn after the Summary
        and skips the other three sections. It is not a length problem: the
        shortest transcript was fine and the longest was worst. The same base
        model through llama.cpp at q4_k_m does not do this, so it belongs to
        this int8 conversion rather than to the prompt.

        Measured on three transcripts, greedy, so the numbers are exact.
        "extra" counts headings the prompt never asked for:

            floor    2 lines        6 lines              10 lines
              0      4/4            3/4                  1/4
            120      4/4            4/4                  4/4
            150      4/4            4/4 +2 extra         4/4
            175      4/4            4/4 +3 extra         4/4
            200      4/4            4/4 +3 extra         4/4 +1 extra

        120 is the smallest floor that clears the premature stop, and the
        largest that does not push the model past its own ending. Above it the
        model keeps writing to satisfy the floor and invents sections --
        "Timestamp", "Language", "Speaker" -- one of which cheerfully prints a
        clock time the prompt explicitly forbids. Raise this and you trade a
        missing section for a fabricated one.

        Do not raise it much further either: at 300 and 450 it degenerates into
        repeating a single line until ``max_length``, which is 1024 tokens and
        206 seconds of it.

        The floor costs time -- roughly 33s against 16s on the ten-line
        transcript -- and that is the price of notes that have all four
        sections.
        """
        gen = self.load()
        tokens = self._tok.encode(self._CHATML.format(prompt=prompt)).tokens
        out = gen.generate_batch(
            [tokens],
            max_length=max_tokens,
            min_length=min_tokens,
            sampling_temperature=0.0,
            include_prompt_in_result=False,
            end_token=[self._STOP],
        )
        produced = out[0].sequences[0]
        ids = [self._tok.token_to_id(t) for t in produced]
        return self._tok.decode([i for i in ids if i is not None])


class LlamaCppSummarizer(_EmbeddedSummarizer):
    """Notes from a GGUF model via llama-cpp-python, in this process.

    The strongest option on quality per byte and by far the largest ecosystem
    of ready-made quantized models -- but it adds a dependency whose prebuilt
    wheels have to exist for all six targets we ship. Verify that before
    choosing it; a source build on a user's machine is not a thing this
    installer can do.
    """

    name = "llamacpp"
    needs = "a .gguf file"

    def load(self):
        if self._model is None:
            from llama_cpp import Llama

            self._model = Llama(model_path=self.path, n_ctx=self.CONTEXT_TOKENS,
                                verbose=False,
                                n_threads=getattr(config, "WHISPER_CPU_THREADS", 0) or None)
        return self._model

    def count_tokens(self, text: str):
        """Exact, from llama.cpp's own tokenizer, once the model is loaded."""
        llm = getattr(self, "_model", None)
        if llm is None:
            return None
        return len(llm.tokenize(text.encode("utf-8")))

    def prompt_budget(self) -> int:
        """The window llama.cpp actually opened, not the one we asked for.

        ``Llama`` can round ``n_ctx`` to what the model or the build supports,
        so reading it back is the difference between a budget that is true and
        one that merely matches the constructor argument.
        """
        llm = getattr(self, "_model", None)
        ctx = llm.n_ctx() if llm is not None else self.CONTEXT_TOKENS
        return max(0, int(ctx) - OUTPUT_RESERVE_TOKENS)

    def _complete(self, prompt: str, max_tokens: int, min_tokens: int = 0) -> str:
        """Go through the chat API, not raw completion.

        ``min_tokens`` is accepted and ignored: llama.cpp's chat API has no
        equivalent, and this engine does not need one -- at q4_k_m it produces
        all four sections unaided, which is what made the CTranslate2 shortfall
        visible as a conversion problem rather than a prompt one.

        Raw ``llm(prompt)`` on an instruct model does not apply the model's
        chat template, so the prompt is continued rather than obeyed. Measured
        here on Qwen2.5-1.5B-Instruct: it echoed one line of the transcript
        back forty times and never wrote a section heading. Ollama applies the
        template for us, which is why the same prompts work there and why this
        engine cannot simply reuse the call shape.

        ``create_chat_completion`` reads the template out of the GGUF metadata,
        so each model gets its own.
        """
        llm = self.load()
        out = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=0.0)
        return out["choices"][0]["message"]["content"] or ""


_ENGINES = {
    "ollama": OllamaSummarizer,
    "ctranslate2": CTranslate2Summarizer,
    "llamacpp": LlamaCppSummarizer,
}


def engine_names():
    """Every built-in engine name, for a settings screen to offer.

    A public view of the registry so the GUI does not reach into a private
    dict -- and so adding a fourth engine shows up in the picker with no
    further change here.
    """
    return sorted(_ENGINES)


def needs_model(name):
    """True if this engine reads NOTES_MODEL, i.e. it runs in this process."""
    engine_cls = _ENGINES.get((name or "").lower())
    return bool(engine_cls) and issubclass(engine_cls, _EmbeddedSummarizer)


def _load_custom(spec):
    """Resolve "module.path:attr" to an instance (calls the attr if callable)."""
    if ":" not in spec:
        raise ValueError(
            f'CUSTOM_SUMMARIZER must look like "module.path:ClassName", got {spec!r}.'
        )
    module_name, attr = spec.split(":", 1)
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if callable(obj) else obj


def build_summarizer():
    """Return the configured notes engine.

    ``CUSTOM_SUMMARIZER`` wins when set — it imports and runs code the user
    named, which is no more privileged than editing config.py was, but is worth
    being explicit about. Otherwise ``SUMMARY_ENGINE`` selects a built-in.
    """
    spec = getattr(config, "CUSTOM_SUMMARIZER", None)
    if spec:
        engine = _load_custom(spec)
        for method in ("summarize", "title"):
            if not hasattr(engine, method):
                raise TypeError(
                    f"CUSTOM_SUMMARIZER {spec!r} must provide a "
                    f".{method}(transcript) method returning text."
                )
        return engine

    name = (getattr(config, "SUMMARY_ENGINE", "ollama") or "ollama").lower()
    engine_cls = _ENGINES.get(name)
    if engine_cls is None:
        raise ValueError(
            f"Unknown SUMMARY_ENGINE {name!r}. Installed: "
            f"{', '.join(sorted(_ENGINES))} - or set CUSTOM_SUMMARIZER."
        )
    return engine_cls()


# One engine, reused. Rebuilt when the settings that choose it change, so a
# switch in the UI takes effect without a restart -- and so an engine that has
# to load a model from disk pays that cost once rather than per meeting.
_engine = None
_engine_key = None


def engine():
    """The live engine, built on first use and cached until the config moves."""
    global _engine, _engine_key
    # NOTES_MODEL is part of the key, not just the engine name: an embedded
    # engine reads the path once, at construction, so leaving it out would mean
    # pointing at a different model and silently keeping the old one loaded.
    key = (getattr(config, "CUSTOM_SUMMARIZER", None),
           getattr(config, "SUMMARY_ENGINE", "ollama"),
           getattr(config, "NOTES_MODEL", ""))
    if _engine is None or key != _engine_key:
        _engine = build_summarizer()
        _engine_key = key
    return _engine


# --- what the rest of the app calls ----------------------------------------

# ``status`` and ``model_label`` are optional on an engine: the required
# contract stays the two methods that make notes, so somebody can write a
# working backend in ten lines. These fall back to something honest when a
# minimal engine does not implement them, rather than refusing to run it.

def status():
    """(ok, detail) for whatever engine is configured. Never raises."""
    try:
        eng = engine()
    except Exception as e:                        # noqa: BLE001
        # An engine that refuses to be built has already explained why, and that
        # sentence is what Settings and --diagnose should show. A class name on
        # its own tells a user nothing they can act on.
        return False, str(e) or f"notes engine unavailable ({e.__class__.__name__})"
    probe = getattr(eng, "status", None)
    if probe is None:
        return True, f"{model_label()} (this engine reports no status)"
    try:
        return probe()
    except Exception as e:                        # noqa: BLE001
        return False, f"status check failed ({e.__class__.__name__})"


def model_label() -> str:
    """What to show a user when naming the notes model. Never raises."""
    try:
        eng = engine()
    except Exception:                             # noqa: BLE001
        return "unavailable"
    label = getattr(eng, "model_label", None)
    try:
        return label() if label else getattr(eng, "name", "custom")
    except Exception:                             # noqa: BLE001
        return getattr(eng, "name", "custom")

def generate_title(transcript: str) -> str:
    """Return a short human title for the meeting, or '' if unavailable.

    Used to name the saved files. Never raises — naming falls back to a
    timestamp when the model can't be reached.
    """
    try:
        return engine().title(transcript)
    except Exception:  # noqa: BLE001 - a title is never worth failing a meeting
        return ""


def to_plain_text(md: str) -> str:
    """Render the model's Markdown as plain text.

    The summary is saved as .txt and the same string is used for the email body
    and the calendar description. None of those three render Markdown: on
    Windows a .md file often has no default program at all, and an attendee
    reading the email just sees literal "## Summary" and "- [ ]" characters.

    This is not a general Markdown implementation and does not need to be. The
    model is asked for four fixed sections and produces headings, bullets and
    task boxes; those are what this handles.
    """
    out = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        # Inline emphasis and code ticks, which carry no meaning in plain text.
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", line)
        line = line.replace("`", "")

        stripped = line.strip()
        if not stripped:
            out.append("")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            text = heading.group(2).strip()
            # A blank line before a heading, but never a leading one.
            if out and out[-1] != "":
                out.append("")
            out.append(text.upper() if len(heading.group(1)) >= 2 else text)
            continue

        task = re.match(r"^[-*+]\s+\[([ xX])\]\s*(.*)$", stripped)
        if task:
            box = "x" if task.group(1).lower() == "x" else " "
            out.append("  [%s] %s" % (box, task.group(2).strip()))
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            out.append("  - %s" % bullet.group(1).strip())
            continue

        numbered = re.match(r"^(\d+[.)])\s+(.*)$", stripped)
        if numbered:
            out.append("  %s %s" % (numbered.group(1), numbered.group(2).strip()))
            continue

        out.append("  %s" % stripped)

    # Collapse runs of blank lines and trim the ends.
    text, blank = [], False
    for line in out:
        if line == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        text.append(line)
    return "\n".join(text).strip()


def collapse_repeats(text: str, limit: int = 1) -> str:
    """Remove runaway repetition from generated notes.

    Small models sometimes latch onto a line and emit it until they run out of
    tokens. Observed in a real meeting: a 119-line transcript produced a
    summary containing the same bullet **62 times**, truncated mid-sentence at
    the token limit. Whatever the cause -- prompt, quantisation, an unlucky
    sampling path -- a set of notes that does that is worthless, and it must
    not be possible to save one.

    So this is a guard on the way out rather than a fix for any one cause. It
    keeps the first occurrence of an identical bullet and drops the rest, then
    removes a trailing fragment left by hitting the token cap.

    One, not two: a real set of notes never says the same thing twice. The same
    piece of work appearing under both Decisions and Action Items is not caught
    by this, because an action item is written "- [ ] ..." and a decision
    "- ...", so the two are different lines.
    Headings and blank lines are untouched, and normal notes never reach the
    limit -- a real summary does not repeat a bullet twice, let alone more.
    """
    seen = {}
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            out.append(raw)
            continue
        if not line.startswith(("-", "*", "+")) and not line.startswith("- ["):
            out.append(raw)
            continue
        key = line.lower()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= limit:
            out.append(raw)

    # Generation that stopped at the token cap leaves half a sentence, and it is
    # worth removing. What is not worth it is removing anything else.
    #
    # This used to drop every trailing line that did not end in punctuation, in
    # a loop. Action items end in a bare word far more often than not -- "-- Them
    # -- Thursday", "- [ ] Write up the release notes" -- so the rule deleted the
    # last one, then found the "## Action Items" heading also ended in a letter
    # and deleted that too, and kept going. A set of notes could lose its whole
    # last section and look perfectly well-formed afterwards. Caught by a smoke
    # check whose fake notes ended "- [ ] c".
    #
    # Three limits, each closing off one way that went wrong: never a heading,
    # never a checkbox item, and never more than one line -- hitting a token cap
    # truncates exactly one. A stray fragment surviving is untidy; a deleted
    # commitment is the notes being wrong, and only one of those is worth
    # risking to avoid the other.
    if out:
        last = out[-1].strip()
        if (last
                and not last.startswith("#")
                and not last.startswith("- [")
                and not last.endswith((".", "!", "?", ":", "]", ")"))):
            out.pop()
    return "\n".join(out).rstrip() + "\n"


def summarize(transcript: str, on_progress=None) -> str:
    """Return Markdown notes, or raise RuntimeError if the engine is unreachable.

    Timestamps are stripped on the way in and scrubbed on the way out, so the
    notes stay a summary rather than becoming a second transcript. The scrub
    happens here rather than in each engine: it is a property of the notes we
    want, not of the model that wrote them, and a backend author should not
    have to know about it to be correct.

    A meeting too long for the engine's context window goes through
    :mod:`rolling` instead of one call. That is a fallback and not the default:
    a transcript that fits is summarised exactly the way it always was, because
    the one-call answer is the better one whenever it is available and chunking
    has not been shown to improve on it. What chunking is for is the case where
    the alternative is an exception and no notes at all.

    ``on_progress(done, total)`` is forwarded to the chunked path only. A single
    call has no progress to report.
    """
    eng = engine()

    # Warm the engine before measuring, not after. An engine answers the length
    # question exactly once its tokenizer is up and only with an estimate
    # before that, and the estimate is deliberately pessimistic -- 1.6 tokens
    # per word against the 1.31 actually measured. Left cold, a 4 504-word
    # meeting is counted at 8 828 tokens when it really costs 6 643, so it
    # would be split into parts despite fitting comfortably in one call. This
    # load costs nothing: the very next thing either path does is use it.
    warm = getattr(eng, "load", None)
    if callable(warm):
        try:
            warm()
        except Exception:                         # noqa: BLE001
            pass    # let the real call raise, where the message reaches the user

    # An engine that declares no ceiling behaves exactly as it did before this
    # existed, including a custom one somebody wrote to the two-method contract.
    prompt = _notes_prompt(transcript)
    try:
        import rolling

        long_way = not rolling.fits(eng, prompt)
    except Exception as e:                        # noqa: BLE001
        # Never let the thing that decides *how* to summarise stop the summary.
        print(f"[summarizer] length check unavailable: {e}", flush=True)
        long_way = False

    if not long_way:
        return collapse_repeats(scrub_timestamps(eng.summarize(transcript)))

    return collapse_repeats(scrub_timestamps(rolling.summarize(
        eng, transcript, _language_directive(), on_progress,
        system=persona())))
