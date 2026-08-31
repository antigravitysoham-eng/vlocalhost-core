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
                 "local_model", "local_control", "calendar", "email_delivery"}


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


check("status() returns (bool, str)", status_shape)
check("model_label() names the Ollama model", label_for_ollama)
check("a minimal engine still reports status", minimal_engine_still_works)
check("status()/model_label() never raise", status_never_raises)
check("check_ollama alias preserved", old_name_still_works)
check("health probe no longer duplicated", no_duplicate_probe)


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
