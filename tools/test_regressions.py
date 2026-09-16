#!/usr/bin/env python3
"""One test per bug that reached a build. None of these may come back.

    python tools/test_regressions.py              # against this source tree
    python tools/test_regressions.py --app DIR    # against a built bundle's app/

Run it in CI, run it before a tag, run it when something feels wrong. Every
case below is a defect that was *found in a build a person was using*, not a
hypothetical — which is why each one names the symptom a user would report
rather than the function that was wrong. If you are reading this because a test
failed, the docstring tells you what the user would have seen.

**Two of the original versions of these tests were themselves wrong** -- one
matched the docstring explaining a bug and reported the bug as present, another
missed a step because it was double-quoted. A test that cries wolf gets
ignored, which is worse than no test. So when one fails, check the test before
changing the app.

Anything needing a running window, a model, or audio hardware lives in
`test_smoke.py` and the bench tools; this file is fast, offline, and has no
dependencies beyond the app itself.
"""

import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CASES = []


def case(symptom):
    """Register a check. ``symptom`` is what the user would have reported."""
    def wrap(fn):
        CASES.append((symptom, fn))
        return fn
    return wrap


def read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def strip_docstrings(text):
    """Comments and docstrings explain these bugs; they are not the bugs."""
    text = re.sub(r'"""ﬁ.*?"""'.replace("ﬁ", ""), "", text, flags=re.S)
    return "\n".join(l for l in text.splitlines()
                     if not l.strip().startswith("#"))


# --------------------------------------------------------------------------
# 1.3.0 — the window
# --------------------------------------------------------------------------

@case("Every time I close the app it says Not Responding")
def close_handler_never_calls_the_page():
    """`_closing` runs on the GUI thread. `evaluate_js` blocks that thread
    until the webview answers, and the webview needs that thread to answer.
    The thread waits on itself; Windows paints that as Not Responding.

    Measured when it shipped: IsHungAppWindow True, still alive after 30s.
    Fixed: 1.9s clean exit.
    """
    shell = read("ui-next", "shell.py")
    body = strip_docstrings(shell[shell.index("def _closing"):shell.index("def main")])
    assert "evaluate_js" not in body, "closing handler calls evaluate_js again"
    assert "api.push" not in body, "closing handler pushes to the page again"
    assert "_shutdown.join" in shell, (
        "nothing waits for the shutdown thread; a summary being written when "
        "the window closed will be lost when the interpreter exits")


@case("The app opens a listening socket just to draw itself")
def page_loads_from_disk_not_a_port():
    """Handed a plain path, pywebview serves the page over bottle and the
    window runs on http://127.0.0.1:<random>/. Nothing leaves the machine, but
    a port is open for the whole session while the pill says "0 bytes out".
    An explicit file:// URL loads the identical page and opens nothing.
    """
    shell = read("ui-next", "shell.py")
    assert "as_uri()" in shell, "not passing a file:// URL any more"
    code = strip_docstrings(shell)
    assert "http_server" not in code, "http_server is being passed to start()"


@case("I downloaded it and the window is blank")
def the_page_is_in_the_build():
    """`ui-next/` is data, not just source, so it cannot ride along on the
    bundler's `*.py` rule. Left out, the app installs, launches, and shows an
    empty frame with a green build.
    """
    bundler = read("tools", "build_bundle.py")
    assert '"ui-next"' in bundler, "ui-next dropped from INCLUDE_DIRS"
    for name in ("index.html", "app.js", "app.css", "tokens.css",
                 "api.py", "shell.py"):
        assert os.path.isfile(os.path.join(ROOT, "ui-next", name)), \
            f"ui-next/{name} is missing"


@case("A fresh clone builds an app that crashes on launch")
def load_bearing_modules_are_tracked():
    """`gui.py` imports notes_panel, which imports notes_view; `ui-next/api.py`
    imports notes_view too. All three were untracked for weeks, so every local
    build worked and a CI checkout would have shipped a crash.
    """
    for name in ("notes_panel.py", "notes_view.py", "theme.py", "ui_shell.py"):
        assert os.path.isfile(os.path.join(ROOT, name)), f"{name} is missing"


# --------------------------------------------------------------------------
# 1.3.0 — setup and first run
# --------------------------------------------------------------------------

@case("Setup told me to download a gigabyte and then wrote no notes")
def builtin_engine_is_gated_on_being_runnable():
    """llama-cpp-python was dropped in 1.2.3 (failed pip-audit, broke the macOS
    build), so no shipped build can import llama_cpp. Setup offered "Built in"
    anyway, as the *default*, and wrote SUMMARY_ENGINE="llamacpp".

    `notes_model.present()` answers whether the weights resolve, not whether
    the engine can load them, and a developer machine usually has llama_cpp
    installed from earlier work -- so this passes in the place it does not
    matter and fails in the bundle, where it does.
    """
    sw = read("setup_wizard.py")
    assert "def builtin_usable" in sw, "the runnability check is gone"
    assert "import llama_cpp" in sw, "builtin_usable no longer asks the import"
    assert "builtin_usable()" in sw, "nothing consults it"
    js = read("ui-next", "app.js")
    assert "builtin_usable" in js, "the window offers Built in unconditionally"


@case("The work question feels like part of the installer")
def setup_asks_only_plumbing():
    """Setup blocks the window, so a question asked there is asked before
    anybody has seen the app. Folder/language/model/engine belong there; "what
    kind of work do you do" does not -- it is about the person, has a perfectly
    good empty answer, and is asked in the app on first open instead.
    """
    js = read("ui-next", "app.js")
    block = js[js.index("const SU_STEPS"):js.index("function suDraw")]
    steps = block.count("\n  {\n")
    assert steps == 5, f"setup has {steps} steps, expected 5"
    assert "kind of work" not in block, "the work question is back in setup"
    assert "askWhoFor" in js, "the in-app first-open card is gone"


@case("It asks me who the notes are for every single launch")
def the_first_open_card_is_asked_once():
    """Leaving both fields empty is a real answer, so "are they empty" cannot
    be the test. USER_ASKED is its own flag, set by Save and by Not now alike.
    """
    import config
    import settings
    assert hasattr(config, "USER_ASKED"), "the asked-once flag is gone"
    assert "USER_ASKED" in settings.EDITABLE, "USER_ASKED is not persistable"
    js = read("ui-next", "app.js")
    assert "user_asked" in js, "the window no longer checks it"
    assert "USER_ASKED: true" in js, "nothing sets it, so it will ask forever"


# --------------------------------------------------------------------------
# 1.3.0 — notes and personalisation
# --------------------------------------------------------------------------

@case("My notes lost who owned an action item")
def the_persona_cannot_drop_facts():
    """The first persona said "let this change what you put first and the words
    you choose", and a six-fact transcript run through two personas came back
    missing the same fact both times: who owned the forecast. An owner silently
    dropped is the worst thing this app can do.

    The prompt now scopes the persona to the Summary paragraph and says every
    name, date and commitment stays attached. Re-measured: 6/6 facts under
    every persona. These clauses are load-bearing, not decoration.
    """
    sm = read("summarizer.py")
    assert "Summary paragraph only" in sm, "the persona's scope clause is gone"
    assert "still attached" in sm, "the owner-protection clause is gone"
    assert "Where this description and those rules disagree, the rules win" in sm, \
        "the precedence clause is gone; a small model follows the last thing it read"


@case("Personalisation changed which facts appear, not just their order")
def the_map_step_is_never_personalised():
    """The chunked path finds facts in one step and writes prose in another.
    Personalising the finding step invites it to leave out what does not look
    relevant, and a fact dropped there cannot be recovered later.
    """
    rolling = read("rolling.py")
    body = rolling[rolling.index("def _map_part"):rolling.index("def parse_facts")]
    assert "system=" not in strip_docstrings(body), \
        "a persona is reaching the map step; facts will start going missing"


@case("I never set a persona but the model is being told about me")
def no_persona_means_no_system_message():
    """Out of the box every field is empty and the request must be byte
    identical to the one this made before personalisation existed.
    """
    import config
    import summarizer
    keep = (config.USER_FIELD, config.USER_TONE, config.USER_CONTEXT)
    try:
        config.USER_FIELD = config.USER_TONE = config.USER_CONTEXT = ""
        assert summarizer.persona() == "", "a persona is built from nothing"
    finally:
        config.USER_FIELD, config.USER_TONE, config.USER_CONTEXT = keep


@case("Running the classic window wiped the persona I set")
def a_window_that_never_asked_cannot_clear_it():
    """`plan()` tests `"user_field" in choices`, not truthiness. The tkinter
    wizard does not collect these keys, so it must leave them alone -- while
    clearing the box in Settings still has to save "".
    """
    import setup_wizard as w
    tk_like = {"language": "en", "profile": "balanced", "notes_kind": "ollama"}
    got = w.plan(tk_like)
    assert "USER_FIELD" not in got, "a wizard that never asked is clearing it"
    assert "USER_TONE" not in got, "a wizard that never asked is clearing it"
    assert w.plan({"user_field": ""}) == {"USER_FIELD": ""}, \
        "clearing the field in Settings no longer saves"


# --------------------------------------------------------------------------
# 1.3.0 — the transcript
# --------------------------------------------------------------------------

@case("There is a delay before my words appear; it felt instant before")
def provisional_text_is_drawn_not_discarded():
    """The engine was never slower -- measured 1770ms median against a 1746ms
    recorded baseline, with decode *faster* (196 vs 261 ms per audio second).

    What changed is that the window stopped drawing the provisional words. The
    tkinter one painted them into the transcript as you spoke; Aurora received
    the same events and showed only "Transcribing...", so the ~1.8s endpoint
    wait (800ms of silence + decode, by design) became visible dead air.

    The `.ln.interim` styling existed the whole time and nothing filled it.
    """
    js = read("ui-next", "app.js")
    # The 'partial' arm specifically. An earlier version of this check scanned
    # from the 'line' arm onward and passed on *that* arm's `p.text` while the
    # bug was still present -- a false pass is worse than no test.
    start = js.index("else if (type === 'partial')")
    arm = js[start:js.index(chr(10), js.index("}", start))]
    assert "p.text" in arm, (
        "the partial handler discards the provisional text again -- the "
        "transcript will feel ~1.8s slower than the engine actually is. "
        f"arm is: {arm.strip()[:90]}")


@case("A Hindi meeting scored a perfect transcript, which cannot be true")
def the_wer_normaliser_is_unicode_aware():
    """`[^a-z0-9' ]` stripped Devanagari, Bengali and Tamil to nothing. Both
    error functions return 0.0 when reference and hypothesis are both empty, so
    an Indic fixture scored 0% WER however bad the transcript was -- and one
    English word in it flipped that to 100%. It would have told us Indic
    transcription was flawless.
    """
    sys.path.insert(0, HERE)
    import bench_pipeline as bp
    hi = "आपको यह दवा रोज़ लेनी है"
    assert bp.normalise(hi), "non-Latin script still normalises to nothing"
    assert bp.word_error_rate(hi, hi) == 0.0
    wrong = "आपको वह दवा रोज़ लेनी है"
    assert 0 < bp.word_error_rate(hi, wrong) < 100, \
        "a one-word Hindi substitution does not score as one word wrong"


@case("Recording got worse after the meter was added")
def the_meter_cannot_disturb_capture():
    """`on_level` runs on the capture thread. It must be opt-in, must never
    raise into that thread, and must not change what gets transcribed.
    Byte-identical utterances with the hook on and off is the bar.
    """
    import audio_listener as al
    frame_bytes = None
    outs = []
    for hook in (None, lambda db, sp, who: None):
        seg = al._Segmenter(lambda b, lab: outs.append((hook is not None, b)),
                            "mic", on_level=hook)
        frame_bytes = b"\x00\x10" * seg.frame_size
        for _ in range(60):
            seg.feed(frame_bytes)
    without = [b for tagged, b in outs if not tagged]
    with_hook = [b for tagged, b in outs if tagged]
    assert without == with_hook, "the meter changed what the segmenter emitted"

    seg = al._Segmenter(lambda *a: None, "mic", on_level=lambda *a: 1 / 0)
    for _ in range(40):
        seg.feed(frame_bytes)          # a raising listener must not propagate


# --------------------------------------------------------------------------
# settings and defaults
# --------------------------------------------------------------------------

@case("My English meeting was transcribed as French nonsense")
def english_is_the_shipped_default():
    """A saved WHISPER_LANGUAGE of 'fr' cost 33.3% WER against 4.3% on the same
    English audio, and produced confident French for English speech -- the
    failure languages.py warns about. The *default* was always 'en'; this keeps
    it that way so a fresh install can never start wrong.
    """
    import config
    import setup_wizard as w
    assert config.WHISPER_LANGUAGE == "en", \
        f"shipped default is {config.WHISPER_LANGUAGE!r}, not 'en'"
    assert w.DEFAULT_LANGUAGE == "en", "setup no longer preselects English"
    assert w.plan({"language": "en"})["WHISPER_LANGUAGE"] == "en"


@case("A setting exists that Settings cannot reach")
def every_editable_setting_has_a_control():
    """EDITABLE is the contract. A key that can be saved but not seen is a
    setting only a support email can change. USER_ASKED is the one exception:
    a flag the app sets, never a control.
    """
    import settings
    html = read("ui-next", "index.html")
    shown = set(re.findall(r'data-setting="([A-Z_]+)"', html))
    missing = [k for k in settings.EDITABLE
               if k not in shown and k != "USER_ASKED"]
    assert not missing, "no control for: " + ", ".join(missing)


@case("A button in the window does nothing at all")
def every_bridge_call_exists():
    """A typo in a bridge name fails silently in JS -- the button simply does
    nothing, with no error anywhere a user or a log would show it.
    """
    sys.path.insert(0, os.path.join(ROOT, "ui-next"))
    import api
    js = read("ui-next", "app.js")
    called = set(re.findall(r"api\(\)\.([A-Za-z_][A-Za-z0-9_]*)", js))
    missing = [m for m in sorted(called) if not hasattr(api.Api, m)]
    assert not missing, "page calls methods the bridge lacks: " + ", ".join(missing)


@case("A click does nothing because the element it looks for is not there")
def every_element_the_page_reaches_for_exists():
    js = read("ui-next", "app.js")
    html = read("ui-next", "index.html")
    have = set(re.findall(r'id="([^"]+)"', html))
    want = set(re.findall(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)""", js))
    want |= set(re.findall(r"""\$\$\(\s*['"]#([A-Za-z0-9_-]+)""", js))
    missing = sorted(w for w in want if w not in have)
    assert not missing, "page looks for missing ids: " + ", ".join(missing)


@case("The review-only query flags shipped to users")
def review_affordances_are_not_in_the_build():
    """?appearance=, ?screen= and ?ask= exist so both appearances and any
    screen can be inspected without touching an OS setting. dark-mode.md
    forbids an app-level appearance switch, so shipping them would ship a
    hidden feature that contradicts the guidance.
    """
    js = read("ui-next", "app.js")
    for needle in ("location.search", "START_SCREEN", "START_ASK"):
        assert needle not in js, f"{needle} is back in app.js"
    shell = strip_docstrings(read("ui-next", "shell.py"))
    assert "--appearance=" not in shell and "--screen=" not in shell, \
        "the review flags are back in shell.py"


@case("I cannot find a light mode, and the app ignores my system setting")
def appearance_is_a_real_setting():
    """The query-string override went out with the review affordances, which
    left no way to choose at all. dark-mode.md is satisfied by *defaulting* to
    the system, not by refusing to offer a choice -- so APPEARANCE is a stored
    setting with three states and System first.
    """
    import config
    import settings
    assert getattr(config, "APPEARANCE", None) == "system", \
        "the default is no longer 'system'; the app must answer the OS first"
    assert "APPEARANCE" in settings.EDITABLE, "the choice cannot be saved"
    css = read("ui-next", "tokens.css")
    for sel in (':root[data-appearance="light"]', ':root[data-appearance="dark"]'):
        assert sel in css, sel + " is gone; the toggle cannot do anything"
    html = read("ui-next", "index.html")
    assert 'id="appearance"' in html, "the toggle is not in the window"
    assert 'id="appearance-note"' in html, "the toggle has no label beside it"


@case("The amber wash disappears in one of the themes")
def the_brand_wash_survives_every_appearance():
    """The aurora is the brand, not decoration. It was reading the *dark* glow
    value under an explicit light choice -- backwards, since a pale ground
    needs more glow for the amber to register at all. Each appearance states
    its own, last in the file, so nothing above can leak into the wrong one.
    """
    css = read("ui-next", "tokens.css")
    tail = css[css.index("the wash"):]
    assert ":root { --glow: .55; }" in tail, "light no longer states its own glow"
    assert '[data-appearance="dark"] { --glow:' in tail, \
        "dark no longer states its own glow"
    assert "prefers-contrast: more" in tail, \
        "high contrast can no longer retire the wash"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", help="a built bundle's app/ directory")
    args = parser.parse_args(argv)

    global ROOT
    if args.app:
        ROOT = os.path.abspath(args.app)
    sys.path.insert(0, ROOT)

    print(f"regression suite — {len(CASES)} cases")
    print(f"against: {ROOT}\n")
    failed = []
    for symptom, fn in CASES:
        try:
            fn()
            print(f"  ok    {symptom}")
        except Exception as e:
            failed.append(symptom)
            print(f"  BACK  {symptom}")
            print(f"        {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{len(failed)} regression(s) are back:")
        for s in failed:
            print("  -", s)
        return 1
    print("no known bug has come back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
