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
    # The bundler itself is not shipped -- `tools` is in SKIP_DIRS -- so this
    # half only runs against the source tree. Against a bundle the files being
    # present *is* the check, and it is the stronger one: it tests the outcome
    # rather than the intent.
    bundler_path = os.path.join(ROOT, "tools", "build_bundle.py")
    if os.path.isfile(bundler_path):
        assert '"ui-next"' in read("tools", "build_bundle.py"), \
            "ui-next dropped from INCLUDE_DIRS"
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


@case("Transcription got much slower after I added my word list")
def the_glossary_cap_stays_behind_the_cliff():
    """The cap must exist and must be bounded -- but not at sixteen, and this
    test used to insist on sixteen because of a measurement that was wrong.

    The old bench fed Whisper synthetic terms (Termik0, Filler1). Nonsense
    hotwords can tip the decoder into a repetition loop, which is bimodal
    rather than a curve, and it read as a cliff: 8 terms 8.0x, 16 terms 1.1x,
    32 terms 8.8x. A cost curve cannot rise, fall and rise again -- that
    non-monotonicity is what exposed it.

    Re-measured on real vocabulary over 32.6s of audio, 5 reps, median: 0
    terms 1.00x, 8 1.09x, 16 1.28x, 32 1.12x, 64 1.15x, 74 1.26x. Flat.

    So what this guards is the thing that is still true: the list handed to the
    decoder is bounded, and it is measured on *real* words. An unbounded cap
    would let a pasted thousand-line list reach the decoder, where the tail is
    genuinely bad (one 8.45s run at 74 terms against a 2.4s baseline).
    """
    import config
    import transcriber
    cap = getattr(config, "GLOSSARY_MAX_TERMS", 0)
    assert 0 < cap <= 120, (
        f"cap is {cap}; it must stay bounded. Re-measure with "
        "tools/bench_glossary.py on REAL vocabulary -- synthetic terms "
        "produce a false cliff -- before raising it further.")

    keep = config.GLOSSARY
    try:
        config.GLOSSARY = ""
        assert transcriber.glossary_terms() == [],             "an empty glossary must hand over nothing at all, not an empty hint"
        config.GLOSSARY = chr(10).join(f"Term{i}" for i in range(cap + 30))
        assert len(transcriber.glossary_terms()) == cap, "the cap is not applied"
        config.GLOSSARY = chr(10).join(["Helius", "helius", "", "  Helius  ", "BFSI"])
        assert transcriber.glossary_terms() == ["Helius", "BFSI"],             "duplicates and blank lines are reaching the decoder"
    finally:
        config.GLOSSARY = keep


@case("The bottom of the window is off the screen and I cannot reach the buttons")
def the_window_fits_the_screen_it_opens_on():
    """shell.py asked for 1180x820 CSS px regardless of the display. On a
    1920x1200 panel at 150% -- a 1280x800 CSS desktop -- 820 became 1230 device
    pixels against 1200 of screen, so the window opened taller than the monitor
    and the foot of every screen sat under the taskbar with nothing to scroll.
    Measured before the fix: window 1770x1226 at y=38, i.e. 64px past the
    bottom edge.
    """
    import importlib.util

    import webview

    path = os.path.join(ROOT, "ui-next", "shell.py")
    spec = importlib.util.spec_from_file_location("_vlshell_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_fit"), "the window no longer measures the screen"

    sw, sh = webview.screens[0].width, webview.screens[0].height
    w, h, mn = mod._fit(mod.WIDTH, mod.HEIGHT, mod.MIN_SIZE)
    assert w <= sw, f"window {w} wider than the {sw}px screen"
    assert h <= sh, f"window {h} taller than the {sh}px screen"
    assert mn[0] <= w and mn[1] <= h,         "the minimum size is bigger than the window that fits"
    # And it must still hand back the full size on a display with room, or the
    # clamp would have quietly shrunk the app for everybody.
    assert mod._fit(800, 600, (400, 300)) == (800, 600, (400, 300))         or (800 > sw - 40 or 600 > sh - 90),         "a window that already fits was shrunk anyway"


@case("Why is the area of work limited to Customer Success?")
def the_area_of_work_shows_its_choices():
    """It was never limited -- it was an <input list=...> datalist, which Edge
    draws as a plain text box with no arrow. Once a value was saved the twelve
    options were invisible, so the field read as locked to whatever it held.
    A control whose choices cannot be seen is a control that does not offer
    them.
    """
    html = read("ui-next", "index.html")
    assert 'id="pick-user-field"' in html, "the area-of-work picker is gone"
    assert 'id="pick-user-tone"' in html, "the notes-tone picker is gone"
    assert 'list="user-fields"' not in html,         "back to a datalist, whose choices Edge does not show"
    js = read("ui-next", "app.js")
    assert "pick('pick-user-field', 'set-user-field'" in js,         "the picker is not populated from setup_options"
    # The input still carries data-setting, or loading and saving break.
    assert 'data-setting="USER_FIELD"' in html
    assert 'data-setting="USER_TONE"' in html


@case("Every sentence appears twice in the live transcript")
def provisional_text_never_repeats_a_finished_line():
    """A partial decoded from audio that had already been finalised arrived
    about a second after the real line and was rendered as provisional text
    beneath it -- four ghosts in thirty-three seconds of speech-clean.wav. The
    old guard only asked whether the queue was empty, which it is again as soon
    as the final has been dequeued and written.
    """
    src = read("notetaker.py")
    assert "_utt_gen" in src, "the utterance generation counter is gone"
    assert "gen != self._utt_gen" in src,         "the decoder no longer checks whether its partial is still current"
    # The bump must happen where an utterance is finalised, not anywhere else.
    i = src.index("def _on_utterance")
    j = src.index("def _on_partial")
    assert "self._utt_gen += 1" in src[i:j],         "the generation is not bumped when a segment is finalised"


@case("Assistants says I have 41 meetings and I have none")
def the_reach_back_line_counts_real_meetings():
    """Three separate invented numbers fed one line.

    `ARCHIVE` shipped as { total: 41, indexed: 38 } and the Aurora Glass change
    emptied it -- but `drawScope()` had already run 1,593 lines earlier and was
    never called again, so the Assistants screen went on saying "all 41 of
    them". Behind it `INSIDE = { 0: 41, 7: 4, 30: 12, 90: 27, 180: 36 }` was a
    second invented archive, so once the total really was 0 the other scopes
    rendered "4 of 0 meetings. The other -4 are refused."

    And `meetings` is seeded with a demo row behind `if (!window.pywebview)` --
    a guard that is false on every real launch, because pywebview injects its
    bridge after the script runs. Counting it made a fresh install claim one
    meeting it did not have.

    A number a reader cannot check is the same problem as a fabricated
    citation, which is the bug the rest of that change exists to fix.
    """
    js = read("ui-next", "app.js")
    assert "const INSIDE = {" not in js, "the invented per-scope archive is back"
    assert "function inReach(" in js,         "the scope counts are no longer taken from the library"
    assert "typeof meetings === 'object' ? meetings.length : 0" not in js,         "recount counts the demo row again"
    assert "Math.max(0, total - n)" in js, "a negative refused-count can return"
    # It must be painted once, from the boot block, after the counts are real.
    assert "recount(); paint(); drawScope();" in js,         "the boot block no longer paints the reach-back line"
    bare = [i for i, line in enumerate(js.split(chr(10)), 1)
            if line.strip() == "drawScope();"]
    assert not bare, (
        "drawScope() is called on its own at line(s) "
        + ", ".join(str(i) for i in bare)
        + " -- at module load ARCHIVE is still a placeholder and libRows is in "
        "its temporal dead zone, which is how 41 survived being deleted.")


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
