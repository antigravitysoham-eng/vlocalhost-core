"""Smoke test: the notes-engine seam and Sealed Mode.

Not a unit-test suite and not a substitute for one. It is the check that the
things most easily broken by an unrelated change still work -- the app imports,
every outbound call is still declared and still refusable, the notes engine is
still swappable, and a real meeting still turns into real notes.

    cd core && python test_smoke.py

Exits non-zero on the first failure so CI can gate on it. Writes nothing: the
user's settings are read, never saved, and the live round-trip is skipped when
Ollama is not running.
"""
import importlib
import os
import subprocess
import sys
import types


PASS, FAIL = [], []


def check(name, fn):
    try:
        detail = fn()
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  FAIL  {name}: {e}")
    except Exception as e:                                   # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  ERROR {name}: {type(e).__name__}: {e}")


def section(t):
    print(f"\n{t}\n" + "-" * len(t))


# =====================================================================
section("1. Modules import")

for mod in ["network", "summarizer", "transcriber", "config", "settings",
            "updates", "diagnostics", "vlocalhost", "setup_wizard",
            "integrations", "notetaker", "gui"]:
    check(f"import {mod}", lambda m=mod: importlib.import_module(m) and None)

import config           # noqa: E402
import network          # noqa: E402
import summarizer       # noqa: E402


# =====================================================================
section("2. Network contract")

EXPECTED_KEYS = {"model_download", "update_check", "note_model_pull",
                 "notes_model_download", "local_model", "local_control",
                 "calendar", "email_delivery"}


def declared_connections():
    got = {c.key for c in network.CONNECTIONS}
    missing, extra = EXPECTED_KEYS - got, got - EXPECTED_KEYS
    assert not missing, f"undeclared: {sorted(missing)}"
    assert not extra, f"new connection not in the smoke test: {sorted(extra)}"
    return f"{len(got)} connections"


check("the declared set is exactly what we expect", declared_connections)

check("every connection has a unique key", lambda: (
    None if len({c.key for c in network.CONNECTIONS}) == len(network.CONNECTIONS)
    else (_ for _ in ()).throw(AssertionError("duplicate keys"))))

check("contract audit is clean", lambda: (
    None if not network.audit()
    else (_ for _ in ()).throw(AssertionError("; ".join(network.audit())))))

check("report is pure ASCII", lambda: (
    None if network.report().isascii()
    else (_ for _ in ()).throw(AssertionError("non-ASCII would mangle on a Windows console"))))

check("report names every connection", lambda: (
    None if all(c.label in network.report() for c in network.CONNECTIONS)
    else (_ for _ in ()).throw(AssertionError("a connection is missing from the report"))))


# =====================================================================
section("3. Sealed Mode enforcement")

config.SEALED_MODE = False
check("unsealed: sealable connections permitted", lambda: (
    None if all(network.allowed(c.key) for c in network.CONNECTIONS)
    else (_ for _ in ()).throw(AssertionError("blocked while unsealed"))))

config.SEALED_MODE = True
check("sealed: every sealable connection blocked", lambda: (
    None if all(not network.allowed(c.key) for c in network.CONNECTIONS if c.sealable)
    else (_ for _ in ()).throw(AssertionError("a sealable connection stayed open"))))

check("sealed: loopback still allowed", lambda: (
    None if all(network.allowed(c.key) for c in network.CONNECTIONS if not c.sealable)
    else (_ for _ in ()).throw(AssertionError("loopback was blocked"))))


def sealed_update():
    import updates
    try:
        updates.check_now()
    except network.Sealed:
        return "raises Sealed"
    raise AssertionError("update check was NOT blocked")


def sealed_provider():
    import integrations
    try:
        integrations.get_provider("google")
    except network.Sealed:
        return "raises Sealed"
    raise AssertionError("provider was NOT blocked")


def sealed_pull():
    import setup_wizard
    msg = setup_wizard.pull_model("llama3.2", "", lambda *a: None)
    assert "sealed" in msg.lower(), f"pull not blocked: {msg[:60]}"
    return "refuses with a reason"


def sealed_model():
    import transcriber
    old = config.WHISPER_MODEL
    config.WHISPER_MODEL = "definitely-not-a-real-model-xyz"
    try:
        transcriber.FasterWhisperTranscriber().load()
        raise AssertionError("absent model loaded while sealed")
    except RuntimeError as e:
        assert "sealed" in str(e).lower(), f"wrong message: {e}"
        return "clear message"
    finally:
        config.WHISPER_MODEL = old


check("sealed: update check blocked", sealed_update)
check("sealed: calendar/email providers blocked", sealed_provider)
check("sealed: Ollama model pull blocked", sealed_pull)
check("sealed: absent speech model fails clearly", sealed_model)

check("sealed: report says so", lambda: (
    None if "Sealed" in network.summary()
    else (_ for _ in ()).throw(AssertionError(network.summary()))))

config.SEALED_MODE = False


# =====================================================================
section("4. Summarizer seam")

from summarizer import summarize, generate_title, to_plain_text  # noqa: E402

check("notetaker's imports still resolve",
      lambda: None if all(callable(f) for f in (summarize, generate_title, to_plain_text))
      else (_ for _ in ()).throw(AssertionError("API changed")))

check("default engine is Ollama", lambda: (
    "OllamaSummarizer" if type(summarizer.engine()).__name__ == "OllamaSummarizer"
    else (_ for _ in ()).throw(AssertionError(type(summarizer.engine()).__name__))))

# a stand-in engine, to prove the seam is real
fake = types.ModuleType("smoke_engine")


class Fake:
    name = "fake"
    def summarize(self, t): return "## Summary\nwritten at [10:04] by a custom engine."
    def title(self, t): return "Custom Engine Works"


class NoTitle:
    def summarize(self, t): return ""


fake.Fake, fake.NoTitle = Fake, NoTitle
sys.modules["smoke_engine"] = fake


def custom_swaps():
    config.CUSTOM_SUMMARIZER = "smoke_engine:Fake"
    try:
        assert type(summarizer.engine()).__name__ == "Fake", "engine did not swap"
        return "swapped"
    finally:
        config.CUSTOM_SUMMARIZER = None


def scrub_applies_to_every_engine():
    config.CUSTOM_SUMMARIZER = "smoke_engine:Fake"
    try:
        out = summarize("irrelevant")
        assert "[10:04]" not in out, f"timestamp survived: {out!r}"
        return "wrapper scrubs, backend need not know"
    finally:
        config.CUSTOM_SUMMARIZER = None


def title_via_custom():
    config.CUSTOM_SUMMARIZER = "smoke_engine:Fake"
    try:
        assert generate_title("x") == "Custom Engine Works"
        return "delegates"
    finally:
        config.CUSTOM_SUMMARIZER = None


def cache_invalidates():
    config.CUSTOM_SUMMARIZER = "smoke_engine:Fake"
    summarizer.engine()
    config.CUSTOM_SUMMARIZER = None
    assert type(summarizer.engine()).__name__ == "OllamaSummarizer", "stale engine cached"
    return "rebuilds on config change"


def bad_engine_name():
    config.SUMMARY_ENGINE = "nonesuch"
    try:
        summarizer.build_summarizer()
        raise AssertionError("unknown engine accepted")
    except ValueError as e:
        assert e.args[0].isascii(), "non-ASCII in a console-facing error"
        return "fails loudly, ASCII"
    finally:
        config.SUMMARY_ENGINE = "ollama"


def incomplete_engine():
    config.CUSTOM_SUMMARIZER = "smoke_engine:NoTitle"
    try:
        summarizer.build_summarizer()
        raise AssertionError("engine missing title() accepted")
    except TypeError:
        return "refused"
    finally:
        config.CUSTOM_SUMMARIZER = None


def title_never_raises():
    class Explode:
        def summarize(self, t): return ""
        def title(self, t): raise RuntimeError("boom")
    fake.Explode = Explode
    config.CUSTOM_SUMMARIZER = "smoke_engine:Explode"
    try:
        assert generate_title("x") == "", "a failing title should degrade to ''"
        return "degrades to timestamp naming"
    finally:
        config.CUSTOM_SUMMARIZER = None


check("custom engine replaces the built-in", custom_swaps)
check("scrub applies to every engine", scrub_applies_to_every_engine)
check("title delegates to the engine", title_via_custom)
check("engine cache invalidates", cache_invalidates)
check("unknown SUMMARY_ENGINE fails loudly", bad_engine_name)
check("incomplete engine refused", incomplete_engine)
check("a failing title never breaks a meeting", title_never_raises)

check("engine choice is user-settable", lambda: (
    None if all(k in __import__("settings").EDITABLE
                for k in ("SUMMARY_ENGINE", "CUSTOM_SUMMARIZER"))
    else (_ for _ in ()).throw(AssertionError("not in settings.EDITABLE"))))


# ---- status()/model_label(): optional on an engine, safe everywhere --------

def status_shape():
    ok, detail = summarizer.status()
    assert isinstance(ok, bool) and isinstance(detail, str), "wrong (ok, detail) shape"
    return f"{ok}, {detail[:34]}"


def label_for_ollama():
    assert summarizer.model_label() == config.OLLAMA_MODEL
    return config.OLLAMA_MODEL


def minimal_engine_still_works():
    """An engine with only summarize/title must not break status or labels."""
    config.CUSTOM_SUMMARIZER = "smoke_engine:Fake"
    try:
        ok, detail = summarizer.status()
        assert ok is True, "a status-less engine should not report unhealthy"
        assert summarizer.model_label() == "fake", summarizer.model_label()
        return "falls back to the engine name"
    finally:
        config.CUSTOM_SUMMARIZER = None


def status_never_raises():
    class Explode:
        def summarize(self, t): return ""
        def title(self, t): return ""
        def status(self): raise RuntimeError("boom")
        def model_label(self): raise RuntimeError("boom")
    fake.Explode2 = Explode
    config.CUSTOM_SUMMARIZER = "smoke_engine:Explode2"
    try:
        ok, detail = summarizer.status()
        assert ok is False and "failed" in detail, detail
        assert summarizer.model_label() in ("custom", "Explode")
        return "degrades instead of throwing"
    finally:
        config.CUSTOM_SUMMARIZER = None


def old_name_still_works():
    import engine as engine_mod
    assert engine_mod.check_ollama is engine_mod.check_notes_engine, "alias lost"
    ok, detail = engine_mod.check_ollama()
    assert isinstance(ok, bool), "wrong shape from the alias"
    return "gui and mcp_server keep working"


def no_duplicate_probe():
    """The /api/tags *call* should live in one place now.

    Matches lines that actually make the request, not prose that mentions the
    endpoint -- an earlier version of this check failed on its own docstring.
    setup_wizard is allowed: installing Ollama is engine-specific onboarding
    and is deliberately still Ollama's, until a second engine exists to design
    that abstraction against.
    """
    import pathlib
    import re
    call = re.compile(r"requests\.(get|post)\([^)]*api/tags")
    hits = [p.name for p in pathlib.Path(".").glob("*.py")
            if not p.name.startswith("test_")
            and call.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert set(hits) <= {"summarizer.py", "setup_wizard.py"}, f"probes in {hits}"
    return f"calls only in {', '.join(sorted(hits))}"


def embedded_engines_registered():
    for name in ("ctranslate2", "llamacpp"):
        assert name in summarizer._ENGINES, f"{name} not registered"
    return ", ".join(sorted(summarizer._ENGINES))


class _NoDownloadedModel:
    """Context manager: pretend this machine has never downloaded the model.

    Two tests below assert what happens when no weights can be found anywhere.
    Both silently changed meaning the moment a real download landed in the user
    data folder -- they started exercising the happy path while still claiming
    to test the empty one. Redirecting the download location keeps them honest
    on a developer machine and a fresh one alike.
    """

    def __enter__(self):
        import shutil
        import tempfile

        import notes_model

        self._notes_model = notes_model
        self._real = notes_model.download_dir
        self._tmp = tempfile.mkdtemp()
        self._shutil = shutil
        notes_model.download_dir = lambda: self._tmp
        return self

    def __exit__(self, *exc):
        self._notes_model.download_dir = self._real
        self._shutil.rmtree(self._tmp, ignore_errors=True)
        return False


def embedded_refuses_without_weights():
    """Selecting an embedded engine with no NOTES_MODEL must say what is missing."""
    old = getattr(config, "NOTES_MODEL", "")
    old_bundle = getattr(config, "BUNDLED_MODELS_DIR", "")
    config.NOTES_MODEL = ""
    config.BUNDLED_MODELS_DIR = ""
    try:
        with _NoDownloadedModel():
            for name in ("ctranslate2", "llamacpp"):
                config.SUMMARY_ENGINE = name
                summarizer._engine = None
                try:
                    summarizer.build_summarizer()
                    raise AssertionError(f"{name} built with no model")
                except RuntimeError as e:
                    assert "NOTES_MODEL" in str(e), f"{name}: unhelpful message"
        return "both name the missing setting"
    finally:
        config.SUMMARY_ENGINE, config.NOTES_MODEL = "ollama", old
        config.BUNDLED_MODELS_DIR = old_bundle
        summarizer._engine = None


def embedded_refuses_wrong_path():
    """A NOTES_MODEL that points nowhere must be caught when it is chosen.

    An empty setting was always refused; a wrong one was not, and built happily
    so that the failure landed on the loader at the end of the first meeting.
    status() has to carry the sentence too, since that is what Settings and
    --diagnose put in front of the user.
    """
    import os

    old_engine = getattr(config, "SUMMARY_ENGINE", "ollama")
    old_model = getattr(config, "NOTES_MODEL", "")
    missing = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "no-such-model.gguf")
    assert not os.path.exists(missing), "the fixture path must not exist"
    try:
        for name in ("ctranslate2", "llamacpp"):
            config.SUMMARY_ENGINE, config.NOTES_MODEL = name, missing
            summarizer._engine = summarizer._engine_key = None
            try:
                summarizer.build_summarizer()
                raise AssertionError(f"{name} built with a path that does not exist")
            except RuntimeError as e:
                assert "does not exist" in str(e), f"{name}: unhelpful message"
            ok, detail = summarizer.status()
            assert not ok, f"{name}: status() called a missing model ready"
            assert "does not exist" in detail, f"{name}: status() hid the reason"
        return "both refuse, and status() says why"
    finally:
        config.SUMMARY_ENGINE, config.NOTES_MODEL = old_engine, old_model
        summarizer._engine = summarizer._engine_key = None


def prompt_has_no_copyable_placeholder():
    """The notes prompt must not contain text a model can paste as an answer.

    Two failures found by running it, in opposite directions. The original
    "- [ ] Task — owner (if mentioned) — due date" was copied literally by
    llama3.2, which emitted "Task — owner — due date" as the action items. A
    concrete example put in to replace it was copied literally by the 1.5B
    built-in, which reported a task assigned to a person who does not exist and
    was never in the transcript -- worse than reporting nothing.

    The shape that survives both is a description with nothing worth pasting.
    """
    prompt = summarizer._notes_prompt("You: nothing in particular.")
    for banned in ("Task — owner", "due date (if mentioned)", "Priya"):
        assert banned not in prompt, f"prompt still contains {banned!r}"
    for heading in ("## Summary", "## Key Discussion Points",
                    "## Decisions", "## Action Items"):
        assert heading in prompt, f"prompt lost {heading}"
    # The escape hatch must still exist, or a quiet meeting has nowhere to go.
    # Matched on collapsed whitespace: the prompt is wrapped for readability, so
    # the phrase is split across a newline in the source and an exact search for
    # it finds one of the two occurrences and misses the other.
    flat = " ".join(prompt.split())
    assert flat.count('"None recorded."') >= 2,         f"only {flat.count(chr(34) + 'None recorded.' + chr(34))} way(s) to report an empty section"
    return "no placeholder a model can paste"


def notes_floor_applies_only_to_notes():
    """A floor under the notes, never under the title.

    The CTranslate2 conversion ends its turn after the Summary without a
    ``min_length``, losing three of four sections. The floor that fixes it must
    not reach the title call: a title is three to six words, and forcing a
    hundred-odd tokens out of one would produce an essay and take longer than
    the notes it names.

    Recorded rather than executed -- this asserts what the engine is asked for,
    so it holds on a machine with no weights at all.
    """
    calls = []

    class Recorder(summarizer._EmbeddedSummarizer):
        name = "recorder"

        def __init__(self):
            self.path = "recorded"
            self._model = None

        def _complete(self, prompt, max_tokens, min_tokens=0):
            calls.append((max_tokens, min_tokens))
            return "notes"

    eng = Recorder()
    eng.title("some transcript")
    eng.summarize("some transcript")
    (title_max, title_min), (notes_max, notes_min) = calls
    assert title_min == 0, f"the title was given a floor of {title_min}"
    assert notes_min == summarizer._EmbeddedSummarizer.NOTES_MIN_TOKENS, \
        f"notes floor was {notes_min}"
    assert notes_min > 0, "the notes floor is switched off"
    # Above ~150 the model pads to satisfy the floor and invents sections;
    # at 300 it degenerates into repeating one line to the token limit.
    assert notes_min <= 150, f"floor of {notes_min} is high enough to pad"
    assert notes_max > notes_min, "the floor must leave room to finish"
    return f"notes floor {notes_min}, title floor {title_min}"


def each_engine_resolves_its_own_model():
    """An engine gets its own model, not the one this platform prefers.

    The two built-in engines want different formats -- one GGUF file, one
    CTranslate2 folder -- and only one of them is the platform default. Before
    this, picking the non-default engine by hand under Advanced resolved to the
    default's model: on Windows that handed CTranslate2 a .gguf, which failed
    as "could not load (RuntimeError)" and named nothing useful.

    Asserts the pairing, not the files, so it holds with no weights present.
    """
    import notes_model

    for engine in ("ctranslate2", "llamacpp"):
        spec = notes_model.spec_for(engine)
        assert spec.engine == engine,             f"{engine} resolved to the {spec.engine} model"
    # An unknown engine falls back rather than raising: a settings screen asks
    # this question about whatever string it happens to be holding.
    assert notes_model.spec_for("nonsense") is notes_model.DEFAULT
    folders = {s.folder for s in notes_model.SPECS}
    assert len(folders) == len(notes_model.SPECS),         "two engines share a folder; one would overwrite the other"
    return "each engine gets its own format"


def runaway_repetition_never_ships():
    """A looped summary must not reach the user.

    From a real meeting: a 119-line transcript produced a summary containing
    the same bullet 62 times, cut off mid-sentence at the token limit. Small
    models do this occasionally regardless of the prompt -- the same prompt and
    transcript did not reproduce it on the next run -- so the guard is on the
    output, where the cause does not matter.
    """
    looped = ("## Summary\nThe meeting covered the platform.\n\n"
              "## Key Discussion Points\n"
              + "- The team wants a filter by project.\n" * 62
              + "- The team wants a filter to view")          # cut off
    out = summarizer.collapse_repeats(looped)
    bullets = [l for l in out.splitlines() if l.strip().startswith("-")]
    assert len(bullets) == 1, f"{len(bullets)} bullets survived, expected 1"
    assert "## Summary" in out and "## Key Discussion Points" in out, \
        "headings were eaten"
    assert not out.rstrip().endswith("to view"), "truncated fragment kept"

    # A decision and the action item for the same work are different lines and
    # must both survive.
    both = ("## Decisions\n- Ship on Friday.\n\n"
            "## Action Items\n- [ ] Ship on Friday.\n")
    kept = summarizer.collapse_repeats(both)
    assert "- Ship on Friday." in kept and "- [ ] Ship on Friday." in kept, \
        "cross-section pair was collapsed"
    return "loops dropped, real notes untouched"


def settings_writes_are_attributed():
    """A setting that moves must leave a record of which code moved it."""
    import diagnostics
    import settings as settings_mod

    seen = []
    real = diagnostics.write
    diagnostics.write = lambda line: seen.append(line)
    old = getattr(config, "UPDATE_REMINDER_DAYS", 7)
    try:
        settings_mod.save(UPDATE_REMINDER_DAYS=old)
    finally:
        diagnostics.write = real
        config.UPDATE_REMINDER_DAYS = old
    assert seen, "a settings write logged nothing"
    line = seen[0]
    assert "UPDATE_REMINDER_DAYS" in line, f"key missing: {line}"
    assert "written by" in line, f"caller missing: {line}"
    return "the key and the caller are recorded"


def notes_model_resolution_order():
    """Your setting, then the installer's copy, then ours, then nothing.

    The order is the whole design: it is what lets the online installer and the
    offline installer be the same build with different starting conditions. A
    test that only checked "finds a model" would pass with the order reversed,
    which would silently prefer our download over the copy an offline install
    shipped -- and download a gigabyte on a machine that already had it.
    """
    import os
    import shutil
    import tempfile

    import notes_model

    old_model = getattr(config, "NOTES_MODEL", "")
    old_bundle = getattr(config, "BUNDLED_MODELS_DIR", "")
    tmp = tempfile.mkdtemp()
    bundle = os.path.join(tmp, "bundled")
    os.makedirs(bundle)
    spec = notes_model.DEFAULT
    # A model is a folder now (CTranslate2) or a file inside one (llama.cpp),
    # so the fixtures have to be whatever this spec actually points at.
    bundled_file = os.path.join(bundle, spec.folder)
    os.makedirs(bundled_file, exist_ok=True)
    if spec.entry:
        bundled_file = os.path.join(bundled_file, spec.entry)
        open(bundled_file, "wb").close()
    mine = os.path.join(tmp, "mine-model")
    os.makedirs(mine, exist_ok=True)
    try:
        with _NoDownloadedModel() as sandbox:
            config.NOTES_MODEL, config.BUNDLED_MODELS_DIR = "", ""
            assert notes_model.resolve() == "", "found a model that is not there"
            assert notes_model.source() == "not on this machine"

            # A downloaded copy is found, and ranks below a bundled one.
            ours = os.path.join(sandbox._tmp, spec.folder)
            os.makedirs(ours, exist_ok=True)
            if spec.entry:
                ours = os.path.join(ours, spec.entry)
                open(ours, "wb").close()
            assert notes_model.resolve() == ours, "missed the downloaded copy"
            assert notes_model.source() == "downloaded by this app"

            config.BUNDLED_MODELS_DIR = bundle
            assert notes_model.resolve() == bundled_file, "bundled must outrank ours"
            assert notes_model.source() == "shipped with the install"

            config.NOTES_MODEL = mine
            assert notes_model.resolve() == mine, "an explicit path must win"
            assert notes_model.source() == "set by you"
        return "explicit > bundled > downloaded > none"
    finally:
        config.NOTES_MODEL, config.BUNDLED_MODELS_DIR = old_model, old_bundle
        shutil.rmtree(tmp, ignore_errors=True)


def bundled_dir_is_relocatable():
    """A relative BUNDLED_MODELS_DIR resolves against the install, not the cwd.

    An installer cannot know the absolute path it will land on, and a user who
    moves the folder must not lose the model inside it. Resolving against the
    working directory would make both of those break in ways that look random.
    """
    import os

    import notes_model

    old = getattr(config, "BUNDLED_MODELS_DIR", "")
    try:
        config.BUNDLED_MODELS_DIR = "models"
        resolved = notes_model.bundled_dir()
        assert os.path.isabs(resolved), "stayed relative"
        expected = os.path.join(os.path.dirname(os.path.abspath(
            notes_model.__file__)), "models")
        assert resolved == expected, f"resolved to {resolved}"
        return "relative paths follow the install"
    finally:
        config.BUNDLED_MODELS_DIR = old


def notes_model_download_refuses_when_sealed():
    """The download is a real internet connection, so Sealed Mode must stop it.

    It is proxied through nothing and dressed up as nothing: allowing it while
    sealed would put an exception in the one switch whose value is that it has
    none. The bundled copy is the supported answer on a sealed machine.
    """
    import notes_model

    old = getattr(config, "SEALED_MODE", False)
    calls = []
    try:
        config.SEALED_MODE = True
        message = notes_model.download(lambda *a: calls.append(a))
        assert message, "a sealed download must report why it refused"
        assert "sealed" in message.lower(), f"unhelpful refusal: {message}"
        assert not calls, "a refused download must not report progress"
        return "sealed installs refuse, and say so"
    finally:
        config.SEALED_MODE = old


def notes_model_settable():
    import settings
    assert "NOTES_MODEL" in settings.EDITABLE
    return "survives an update"


def cache_tracks_notes_model():
    """Changing NOTES_MODEL must rebuild the engine, not keep the old path.

    An embedded engine reads the path once, at construction. If the cache key
    ignored NOTES_MODEL, pointing at a different model would silently keep the
    previous one loaded -- the kind of bug you only notice in a benchmark.

    Two real files, because an engine now refuses a path that does not exist.
    Neither has to be a model: nothing is loaded until the first summary, so an
    empty file is enough to prove which path the engine took up. Asserting on
    that path rather than on the private cache key also survives the engine
    refusing to build at all -- a refusal records no key, which is correct.
    """
    import os
    import shutil
    import tempfile

    old_engine = getattr(config, "SUMMARY_ENGINE", "ollama")
    old_model = getattr(config, "NOTES_MODEL", "")
    tmp = tempfile.mkdtemp()
    a = os.path.join(tmp, "a.gguf")
    b = os.path.join(tmp, "b.gguf")
    for path in (a, b):
        open(path, "wb").close()
    try:
        config.SUMMARY_ENGINE = "llamacpp"
        config.NOTES_MODEL = a
        summarizer._engine = summarizer._engine_key = None
        assert summarizer.engine().path == a, "first model path not taken up"
        config.NOTES_MODEL = b
        assert summarizer.engine().path == b, "cache ignored NOTES_MODEL"
        return "rebuilds when the model path moves"
    finally:
        config.SUMMARY_ENGINE, config.NOTES_MODEL = old_engine, old_model
        summarizer._engine = summarizer._engine_key = None
        shutil.rmtree(tmp, ignore_errors=True)


def revision_pinned():
    import transcriber
    rev = transcriber._revision_for("base")
    assert rev and len(rev) == 40, f"base is not pinned to a full sha: {rev}"
    return rev[:12]


def path_models_unpinned():
    """A local folder carries no revision; claiming one would be a lie."""
    import os
    import transcriber
    assert transcriber._revision_for(os.getcwd()) is None
    return "paths correctly unpinned"


def telemetry_disabled():
    import os
    import transcriber
    transcriber._prepare_model_env()
    assert os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1"
    return "HF_HUB_DISABLE_TELEMETRY=1"


def sbom_lists_the_weights():
    """The models arrive on a user's machine, so they belong in the SBOM."""
    import json
    import os
    path = os.path.join(os.path.dirname(os.getcwd()), "docs", "sbom.cyclonedx.json")
    if not os.path.exists(path):
        return "SKIPPED - no SBOM built here"
    doc = json.load(open(path, encoding="utf-8"))
    models = [c for c in doc["components"] if c.get("type") == "machine-learning-model"]
    assert models, "no model components in the SBOM"
    assert any("whisper" in c["name"] for c in models), "Whisper missing"
    return f"{len(models)} models declared"


check("status() returns (bool, str)", status_shape)
check("model_label() names the Ollama model", label_for_ollama)
check("both embedded engines registered", embedded_engines_registered)
check("embedded engines refuse without weights", embedded_refuses_without_weights)
check("embedded engines refuse a wrong path", embedded_refuses_wrong_path)
check("notes prompt has no copyable placeholder", prompt_has_no_copyable_placeholder)
check("generation floor applies to notes only", notes_floor_applies_only_to_notes)
check("each engine resolves its own model", each_engine_resolves_its_own_model)
check("runaway repetition never ships", runaway_repetition_never_ships)
check("settings writes are attributed", settings_writes_are_attributed)
check("notes model resolution order", notes_model_resolution_order)
check("bundled model dir is relocatable", bundled_dir_is_relocatable)
check("notes model download is sealable", notes_model_download_refuses_when_sealed)
check("NOTES_MODEL is user-settable", notes_model_settable)
check("engine cache tracks NOTES_MODEL", cache_tracks_notes_model)


# Real generation through the embedded engines, when their weights happen to be
# on this machine. Skipped rather than failed otherwise: the models are over a
# gigabyte each and no one should have to download them to run a smoke test.
#
# These exist because "it refuses cleanly without weights" passed for both
# engines while both were in fact broken — each produced a loop of transcript
# echoes, because neither runtime applies a chat template on its own. Only
# generating something caught it.
_HF = os.environ.get("HF_HUB_CACHE") or os.path.join(
    os.path.expanduser("~"), ".cache", "huggingface", "hub")
_WEIGHTS = {
    "ctranslate2": os.path.join(
        _HF, "models--jncraton--Qwen2.5-1.5B-Instruct-ct2-int8",
        "snapshots", "55bb006d27f0b32c8b872a49fecc1d27b5071276"),
    "llamacpp": os.path.join(
        _HF, "models--Qwen--Qwen2.5-1.5B-Instruct-GGUF", "snapshots",
        "91cad51170dc346986eccefdc2dd33a9da36ead9",
        "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
}
_SAMPLE = ("[10:01] You: we ship Linux on Friday.\n"
           "[10:02] Them: I will own the installer, due Thursday.")


def embedded_generates(engine_name):
    def run():
        path = _WEIGHTS[engine_name]
        if not os.path.exists(path):
            return "SKIPPED - weights not on this machine"
        old = (config.SUMMARY_ENGINE, getattr(config, "NOTES_MODEL", ""))
        config.SUMMARY_ENGINE, config.NOTES_MODEL = engine_name, path
        summarizer._engine = None
        try:
            notes = summarizer.summarize(_SAMPLE)
            assert notes.strip(), "produced nothing"
            # The failure this catches: a model that continues the transcript
            # instead of summarising it. Real notes carry headings; a
            # continuation loop repeats the speaker labels instead.
            assert "#" in notes, f"no headings — looks like a continuation: {notes[:80]!r}"
            assert notes.count("Them:") < 3, "looks like a transcript echo loop"
            return f"{len(notes)} chars, {notes.count('#')} headings"
        finally:
            config.SUMMARY_ENGINE, config.NOTES_MODEL = old
            summarizer._engine = None
    return run


check("ctranslate2 writes real notes", embedded_generates("ctranslate2"))
check("llamacpp writes real notes", embedded_generates("llamacpp"))
check("speech model revision is pinned", revision_pinned)
check("local model folders stay unpinned", path_models_unpinned)
check("Hugging Face telemetry disabled", telemetry_disabled)
check("SBOM declares the model weights", sbom_lists_the_weights)
check("a minimal engine still reports status", minimal_engine_still_works)
check("status()/model_label() never raise", status_never_raises)
check("check_ollama alias preserved", old_name_still_works)
check("health probe no longer duplicated", no_duplicate_probe)


# =====================================================================
section("4b. Long meetings (the context ceiling)")

# The defect this guards against is silent in the worst way: a meeting long
# enough to overflow the model's window produced no notes at all, and the only
# sign was an error string beside a saved transcript. None of this needs a
# model -- it is arithmetic and prompt assembly -- so it runs everywhere.

import rolling                                               # noqa: E402


class _FakeEngine:
    """An engine with a known, small window and an exact tokenizer.

    A real engine would make these checks a measurement of Qwen rather than of
    the seam. What is under test is the decision -- one call or several -- and
    that decision must be right on a machine with no weights on it at all.
    """

    NOTES_MIN_TOKENS = 0

    def __init__(self, ctx=2048):
        self.ctx = ctx
        self.calls = []

    def prompt_budget(self):
        return self.ctx - summarizer.OUTPUT_RESERVE_TOKENS

    def count_tokens(self, text):
        return len(text.split())          # one token per word, exactly

    def _complete(self, prompt, max_tokens, min_tokens=0):
        self.calls.append(prompt)
        if "WHAT HAPPENED:" in prompt:    # the one paragraph the model writes
            return "The team met and agreed some things."
        return ("TOPICS\n- the installer and the release\n"
                "DECISIONS\n- ship on Friday\n"
                "ACTIONS\n- Them: rotate the credentials — Thursday\n"
                "QUESTIONS\n- none")

    def summarize(self, transcript):
        return self._complete("whole:" + transcript, 1024)

    def title(self, transcript):
        return "t"


class _install:
    """Put a fake engine behind ``summarizer.engine()`` for one block.

    Setting ``_engine`` alone is not enough and fails quietly: ``engine()``
    rebuilds whenever the cache key does not match the current config, so a
    fake installed without its key is discarded on the first call and the test
    silently measures the real engine instead. That is how two of these checks
    came to report "0 calls" while two others passed by accidentally driving a
    1.5B model.
    """

    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.prev = (summarizer._engine, summarizer._engine_key)
        summarizer._engine = self.engine
        summarizer._engine_key = (getattr(config, "CUSTOM_SUMMARIZER", None),
                                  getattr(config, "SUMMARY_ENGINE", "ollama"),
                                  getattr(config, "NOTES_MODEL", ""))
        return self.engine

    def __exit__(self, *exc):
        summarizer._engine, summarizer._engine_key = self.prev
        return False


def _meeting(words):
    line = "You: we talked about the installer and the release again today"
    n = len(line.split())
    return "\n".join(f"[09:00:0{i % 10}] {line}" for i in range(words // n + 1))


def budget_is_reserved():
    e = _FakeEngine(2048)
    assert rolling.budget(e) == 2048 - 1024, rolling.budget(e)
    return "2048 window - 1024 reserved = 1024 for the prompt"


def unknown_budget_means_one_call():
    """An engine that does not declare a window must behave exactly as before.

    Custom backends are written to a two-method contract that says nothing
    about context, so anything this module does to them without being asked is
    a regression in a promise the product makes.
    """
    class Minimal:
        def summarize(self, t): return "## Summary\nx"
        def title(self, t): return ""
    assert rolling.budget(Minimal()) == 0
    assert rolling.fits(Minimal(), "word " * 100000)
    return "no ceiling declared, so never chunked"


def estimate_errs_high():
    """With no tokenizer, the guess must be too big rather than too small.

    Guessing low overflows the window, which is the failure. Guessing high only
    splits a meeting that need not have been split.
    """
    class NoTokenizer:
        def prompt_budget(self): return 1000
    text = "word " * 1000
    assert rolling.count_tokens(NoTokenizer(), text) > 1000, "estimate too low"
    return f"1000 words estimated at {rolling.count_tokens(NoTokenizer(), text)} tokens"


def chunks_split_on_line_boundaries():
    parts = rolling.chunks(_meeting(3000), words=800)
    assert len(parts) > 1, "long meeting was not split"
    for p in parts:
        assert not p.startswith(" "), "part starts mid-line"
        for line in p.splitlines():
            assert line.startswith("You:"), f"cut through a line: {line[:40]!r}"
    return f"{len(parts)} parts, every one whole lines"


def chunks_overlap():
    """A commitment split across the seam would otherwise lose its owner."""
    parts = rolling.chunks(_meeting(3000), words=800, overlap=40)
    first_tail = set(parts[0].splitlines()[-3:])
    assert first_tail & set(parts[1].splitlines()), "no overlap between parts"
    return "consecutive parts share their boundary lines"


def chunks_drop_timestamps():
    parts = rolling.chunks(_meeting(2000), words=800)
    assert "[09:00:" not in "".join(parts), "timestamps survived into a part"
    return "stripped before the model sees them"


def short_meeting_takes_one_call():
    with _install(_FakeEngine(200000)) as e:
        summarizer.summarize(_meeting(200))
    assert len(e.calls) == 1, f"{len(e.calls)} calls for a short meeting"
    assert e.calls[0].startswith("whole:"), "did not use the whole-transcript path"
    return "one call, unchanged from before this existed"


def long_meeting_is_chunked_not_dropped():
    with _install(_FakeEngine(2048)) as e:
        notes = summarizer.summarize(_meeting(4000))
    assert len(e.calls) > 2, f"only {len(e.calls)} calls"
    assert not any(c.startswith("whole:") for c in e.calls), "sent the whole thing anyway"
    for want in ("## Summary", "## Key Discussion Points",
                 "## Decisions", "## Action Items"):
        assert want in notes, f"missing {want}"
    return f"{len(e.calls)} calls, four sections out"


def progress_is_reported():
    """A chunked run takes minutes. A window with nothing to say looks hung."""
    seen = []
    with _install(_FakeEngine(2048)):
        summarizer.summarize(_meeting(4000),
                             on_progress=lambda d, n: seen.append((d, n)))
    assert seen, "no progress reported"
    assert seen[-1][0] == seen[-1][1], f"last report was {seen[-1]}"
    return f"{len(seen)} updates, ending {seen[-1][0]}/{seen[-1][1]}"


def a_broken_progress_hook_cannot_lose_the_notes():
    def boom(d, n):
        raise RuntimeError("ui gone")

    with _install(_FakeEngine(2048)):
        notes = summarizer.summarize(_meeting(4000), on_progress=boom)
    assert "## Summary" in notes, "a UI callback took the notes down with it"
    return "notes survive a raising callback"


def enormous_meeting_folds_instead_of_failing():
    """More parts than the reduce call can read must not reproduce the bug.

    Without the fold, a long enough meeting overflows the *merge* prompt and
    fails exactly the way the whole-transcript call fails -- which would make
    this module a higher ceiling rather than no ceiling.
    """
    e = _FakeEngine(2048)
    notes = rolling.summarize(e, _meeting(40000))
    for want in ("## Summary", "## Action Items"):
        assert want in notes, f"missing {want}"
    reduces = [c for c in e.calls if "PARTS:" in c]
    for c in reduces:
        assert e.count_tokens(c) <= rolling.budget(e), "a reduce call overflowed"
    return f"{len(e.calls)} calls, {len(reduces)} merges, none over budget"


def ollama_asks_for_a_context_size():
    """The ceiling must be ours, not whatever the user's Ollama defaults to."""
    src = open("summarizer.py", encoding="utf-8").read()
    assert '"num_ctx"' in src, "no num_ctx sent to Ollama"
    e = summarizer.OllamaSummarizer()
    assert e.prompt_budget() > 0, "ollama declares no budget"
    return f"num_ctx {e._num_ctx()}, budget {e.prompt_budget()}"


def every_engine_can_be_chunked():
    """``_complete`` is what :mod:`rolling` drives. An engine without one would
    fall back to the whole-transcript call and keep the bug."""
    for name, cls in summarizer._ENGINES.items():
        assert callable(getattr(cls, "_complete", None)), f"{name} has no _complete"
    return ", ".join(sorted(summarizer._ENGINES))


def the_last_action_item_survives():
    """The trailing-fragment trim must not eat real content.

    Action items end in a bare word most of the time, so a rule that dropped
    unpunctuated trailing lines removed the last commitment in the notes -- and
    then the heading above it, and kept going.
    """
    notes = ("## Summary\nWe met.\n\n## Decisions\n- Ship on Friday\n\n"
             "## Action Items\n- [ ] Rotate the credentials — Them — Thursday")
    out = summarizer.collapse_repeats(notes)
    assert "## Action Items" in out, "lost the heading"
    assert "Rotate the credentials" in out, "lost the last action item"
    return "heading and final commitment both kept"


def a_truncated_fragment_is_still_removed():
    """The guard still has to do its job, or removing it would be simpler."""
    notes = "## Summary\nWe met and agreed to\n\nthe thing we were talking ab"
    out = summarizer.collapse_repeats(notes)
    assert "talking ab" not in out, "half a sentence survived"
    return "mid-word fragment dropped"


def repetition_is_still_collapsed():
    notes = "## Decisions\n" + "- Ship on Friday.\n" * 62
    out = summarizer.collapse_repeats(notes)
    assert out.count("Ship on Friday") == 1, out.count("Ship on Friday")
    return "62 copies of one bullet reduced to 1"


def the_prompt_offers_nothing_to_copy():
    """No prompt may contain content a model could pass off as the meeting.

    A worked example with realistic content in it was copied straight into the
    notes: a meeting about incident triage and budget produced "Nadia: book the
    van — by Friday" and an office move, all of it from the prompt. Fabricated
    content in a user's notes is the one failure this product cannot have, so
    every line the prompts show as a model of the answer must be a placeholder
    that is obviously not a finding.
    """
    shown = [l.strip()[2:].strip() for l in rolling.ANSWER_SHAPE.splitlines()
             if l.strip().startswith("- ")]
    assert shown, "the answer shape has no example lines at all"
    for body in shown:
        assert "<" in body and ">" in body, (
            f"the answer shape shows copyable content: {body[:60]!r}")
    # The model of the answer must also never reach the notes verbatim.
    for body in shown:
        assert body not in summarizer._PROMPT, "shape leaked into the notes prompt"
    return f"{len(shown)} example lines, all placeholders"


def the_lists_are_built_in_code():
    """The three lists must not come from a model call.

    Asked to merge every part's answer into four sections, a 1.5B model wrote a
    fifteen-sentence Summary and then stopped -- no Decisions and no Action
    Items, from a transcript full of both. Deduplicating lists is not a
    language problem, and doing it in code is what makes those sections
    reliably present and reliably formatted.
    """
    facts = ["TOPICS\n- the migration\nDECISIONS\n- ship Friday\n"
             "ACTIONS\n- Mo: book the runner — Tuesday\nQUESTIONS\n- none"]

    class Paragraph:
        def prompt_budget(self): return 100000
        def count_tokens(self, t): return len(t.split())
        def _complete(self, prompt, *a, **k):
            assert "WHAT HAPPENED:" in prompt, "the lists went through the model"
            return "They discussed the migration."

    notes = rolling._assemble(Paragraph(), facts, "")
    assert "- [ ] Mo: book the runner — Tuesday" in notes, notes
    assert "- ship Friday" in notes, notes
    return "action items and decisions carried through verbatim"


def runaway_bullets_are_dropped():
    """One part came back as a 900-character line repeating twelve items."""
    loop = ("SBOM link sent by Wednesday, introduction to Priya, pilot on one "
            "team, dashboard check, export to wiki, ") * 6
    assert rolling._looks_runaway(loop), "a looping bullet survived"
    assert not rolling._looks_runaway(
        "You will prepare the business case for the 14th and circulate it a "
        "week before."), "a real commitment was rejected as runaway"
    return "loop rejected, long real commitment kept"


def saying_nothing_is_not_a_finding():
    for empty in ("There were no questions asked and left unanswered.",
                  "No decisions were recorded.", "none", "N/A"):
        assert rolling._is_nothing(empty), f"{empty!r} treated as content"
    real = "No decision on pricing until legal signs off, so the launch slips"
    assert not rolling._is_nothing(real), "a real finding was discarded"
    return "empty answers filtered, a finding that starts 'No' kept"


def near_duplicates_merge():
    """The same decision arriving from two parts must come out as one line."""
    a = "Ship Friday if the duplication test passes"
    b = "The group decided to ship Friday only if the duplication test passes"
    assert rolling._similar(rolling._words(a), rolling._words(b)), "duplicate kept"
    c = "The renewal is in November."
    assert not rolling._similar(rolling._words(a), rolling._words(c)), "merged two findings"
    return "reworded repeats merge, distinct findings do not"


def distinct_findings_are_never_merged():
    """The tuning must err toward keeping both, because a wrong merge is a
    decision taken in the meeting and missing from the notes.

    These two share three of their four words and scored 0.75 at the threshold
    inherited from Pro's extractor, which silently deleted one of them.
    """
    a = "whether invoicing needs rework"
    b = "whether onboarding needs rework"
    assert not rolling._similar(rolling._words(a), rolling._words(b)), \
        "two different topics were merged into one"
    return "shared scaffolding is not sameness"


def sections_are_capped():
    """Ten parts offering six findings each is sixty bullets, which is not notes."""
    # Each line must be about something genuinely different, or the near-
    # duplicate merge collapses them all and the cap is never reached -- which
    # is what this check did on its first run, reporting "40 offered, 1 kept"
    # and proving nothing about the cap at all.
    subjects = ["invoicing", "onboarding", "latency", "hiring", "backups",
                "pricing", "kubernetes", "recruiting", "telemetry", "packaging",
                "warehouse", "compliance", "translation", "batching", "caching",
                "routing", "indexing", "sharding", "logging", "throttling",
                "quotas", "webhooks", "migrations", "rollback", "canaries",
                "postmortem", "runbooks", "alerting", "dashboards", "budgets",
                "contracts", "renewals", "churn", "expansion", "forecasting",
                "attribution", "segmentation", "retention", "activation", "referrals"]
    facts = ["TOPICS\n" + "\n".join(f"- whether {s} needs rework" for s in subjects)]
    notes = rolling._assemble(_FakeEngine(200000), facts, "")
    body = notes.split("## Key Discussion Points")[1].split("##")[0]
    kept = [l for l in body.splitlines() if l.strip().startswith("- ")]
    assert len(kept) == rolling.MAX_ITEMS_PER_SECTION, (
        f"{len(kept)} kept, cap is {rolling.MAX_ITEMS_PER_SECTION}")
    return f"{len(subjects)} distinct topics offered, {len(kept)} kept"


def every_section_is_always_present():
    notes = rolling._assemble(_FakeEngine(200000), ["TOPICS\n- none"], "")
    for want in ("## Summary", "## Key Discussion Points",
                 "## Decisions", "## Action Items"):
        assert want in notes, f"missing {want}"
    assert "None recorded." in notes, "an empty section was left blank"
    return "four sections, empty ones say so"


check("prompts offer nothing to copy into notes", the_prompt_offers_nothing_to_copy)
check("the three lists are built in code", the_lists_are_built_in_code)
check("runaway bullets are dropped", runaway_bullets_are_dropped)
check("saying nothing is not a finding", saying_nothing_is_not_a_finding)
check("near-duplicate bullets merge", near_duplicates_merge)
check("distinct findings are never merged", distinct_findings_are_never_merged)
check("sections are capped", sections_are_capped)
check("every section is always present", every_section_is_always_present)
check("the last action item survives the trim", the_last_action_item_survives)
check("a truncated fragment is still removed", a_truncated_fragment_is_still_removed)
check("runaway repetition is still collapsed", repetition_is_still_collapsed)
check("output reserve comes off the window", budget_is_reserved)
check("an engine with no ceiling is never chunked", unknown_budget_means_one_call)
check("the tokenizer-free estimate errs high", estimate_errs_high)
check("parts never cut through a line", chunks_split_on_line_boundaries)
check("parts overlap at the seam", chunks_overlap)
check("timestamps stripped before chunking", chunks_drop_timestamps)
check("a short meeting still takes one call", short_meeting_takes_one_call)
check("a long meeting is chunked, not dropped", long_meeting_is_chunked_not_dropped)
check("chunked runs report progress", progress_is_reported)
check("a broken progress hook cannot lose the notes",
      a_broken_progress_hook_cannot_lose_the_notes)
check("an enormous meeting folds instead of failing",
      enormous_meeting_folds_instead_of_failing)
check("Ollama is told the context size", ollama_asks_for_a_context_size)
check("every engine exposes _complete", every_engine_can_be_chunked)


# =====================================================================
section("5. CLI surfaces")


def cli(*args):
    r = subprocess.run([sys.executable, "vlocalhost.py", *args],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"exit {r.returncode}: {(r.stderr or '')[:120]}"
    return r.stdout


check("--network", lambda: "Network" in cli("--network") and "prints the contract" or None)
check("--paths", lambda: "settings" in cli("--paths") and "ok" or None)
check("--get lists new settings", lambda: (
    "SUMMARY_ENGINE present" if "SUMMARY_ENGINE" in cli("--get")
    else (_ for _ in ()).throw(AssertionError("SUMMARY_ENGINE missing from --get"))))
check("--version", lambda: "installed" in cli("--version") and "ok" or None)
check("--diagnose reports network state", lambda: (
    "network line present" if "network" in cli("--diagnose")
    else (_ for _ in ()).throw(AssertionError("no network line"))))


# =====================================================================
section("6. Live round-trip (skipped if Ollama is absent)")


def live_ollama():
    import requests
    try:
        requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2).raise_for_status()
    except Exception:                                        # noqa: BLE001
        return "SKIPPED - Ollama not reachable"
    notes = summarize("You: we ship Linux on Friday. Them: I will own the installer.")
    assert notes.strip(), "empty notes"
    assert "[" not in notes.split("\n")[0], "timestamp leaked into the first line"
    return f"{len(notes)} chars of real notes"


check("Ollama produces notes end to end", live_ollama)


# =====================================================================
print("\n" + "=" * 52)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for n, e in FAIL:
        print(f"  - {n}: {e}")
print("=" * 52)
sys.exit(1 if FAIL else 0)
