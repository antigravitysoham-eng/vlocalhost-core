# ui-next — Aurora, built from scratch

The Vlocalhost window, rebuilt from
`Vlocalhost-Personal/3-brand/app-ui-aurora-light.html` (Direction 01, "Aurora —
light, and nothing hidden").

> **This is now the window the app opens**, as of 15 Sep 2026. Two sentences
> here used to say the opposite — *"nothing here is wired to the engine yet,
> and nothing in `core/` imports it"* — and both are false now. It moved into
> `core/` because `tools/build_bundle.py` builds from `core/`, so a page beside
> it could never reach an installer. `core/ui_shell.py` imports it (this
> directory's hyphen is why that shim has to exist), `core/vlocalhost.py` opens
> it first and falls back to the tkinter window, and `core/audio_listener.py`
> gained the `on_level` hook this page's meter needed.

```
python vlocalhost.py            # from core/ — the app, and this window
python vlocalhost.py --classic  # the tkinter window instead
python ../run_ui.py             # the Pro launcher: same window, plus vlocalhost_pro

open index.html            # or: python -m http.server 8777 -- the prototype
```

Five screens, plus the first-run setup that used to be a separate tkinter
wizard. The page has no framework and no build step; the shell is pywebview and
nothing else. **Opened as a file it is still the prototype** — scripted
meeting, canned notes — and opened by the app it is the application. Same page,
same code path; see "The shell" below.

---

## Why this is not the tkinter app

The tkinter Record tab can show the notes, and does. What it cannot show is
Aurora: no rounded surfaces, no backdrop blur, no gradient wash, no real
control over type. The Spatial direction's own cost table already reached the
same conclusion one toolkit up — *"Qt Widgets cannot do this… it needs Qt Quick
/ QML"* — and tkinter is well below Qt Widgets. So this is HTML, which is what
Aurora was drawn in, and which an embedded webview renders unchanged.

## What changed from the mockup, and why

The mockup is a picture; this has to survive being used. Three changes, all of
them forced by measurement rather than taste. Guideline citations are to the
Apple HIG pages in the `apple-design` skill.

### 1. Small text got its own colour tokens

Aurora's two most important marks were its two least legible:

| Token | Used for | On white | Floor |
| --- | --- | --- | --- |
| `--amber #E08A17` | the timestamp citation — *"that timestamp is the product"* | **2.69:1** | 4.5:1 |
| `--mint #0FA894` | the `0 bytes out` pill — *"the whole privacy argument"* | **2.98:1** | 4.5:1 |
| `--ink-4 #A7AEBE` | every `.meta`, `.k` and `.ts` label | **2.22:1** | 4.5:1 |
| `--ink-3 #767E92` | `.sub`, `.d` | **4.06:1** | 4.5:1 |

On their own soft grounds they were worse: amber on `--amber-soft` is 2.45:1,
mint on `--mint-soft` is 2.71:1.

> `accessibility.md` › Color and effects: text up to 17 pt needs **4.5:1**;
> 18 pt or bold needs 3:1.

The fix keeps the brand. Each accent now has **two** tokens: the original value
for fills and strokes, where contrast rules do not apply — the meter, the logo,
the left rule on a fact — and a darker same-hue value for anything set small.
`--amber #E08A17` still draws the rule; `--amber-text #A86811` (4.5:1) writes
the timestamp. Aurora looks the same and its labels can be read.

### 2. Nothing below 11px

Aurora sets labels at `.58rem` (9.3px) and timestamps at `.62rem` (9.9px).

> `typography.md` › Specifications: macOS default **13 pt**, minimum **10 pt**.

`--t-label` is 11px and is the floor. Body is 14px, prose 15px.

### 3. The fonts are not fetched

Aurora links Inter Tight and IBM Plex Mono from `fonts.googleapis.com`. That is
a network request, made on launch, by an app whose entire claim is that it makes
none — the pill in the corner would read `0 bytes out` while the page was still
downloading a typeface. The stack falls back through the system's UI font, and
the brand faces get **bundled as local files** if they are wanted.

This is the one finding that is about the product rather than the guidelines,
and it is the one that matters most.

---

## What was kept exactly

- The aurora wash — three blurred radials, amber / lilac / mint. It is a
  background *beneath* content, not a material over it, which is the line
  `materials.md` draws: *"Don't use Liquid Glass in the content layer."* The one
  blurred surface in the app is the chrome bar, which is the floating layer
  where glass belongs.
- The shell: 212px grouped sidebar, `ON-DEVICE` badge pinned to its foot.
- **The meter is the page.** It is the single loud element; everything else is
  quiet, which is the rule about spending boldness once.
- The fact pattern: hairline left rule, role label, claim, the second it was
  said. Red rule for Risk.
- Aurora's reading order on a meeting — *"the notes, and underneath them the
  facts that can be checked"* — summary first, facts under it.

## What was added

- **Collapsible sections** throughout, via real disclosure triangles.
  `disclosure-controls.md`: *"A disclosure triangle points inward from the
  leading edge when its content is hidden and down when its content is
  visible."* Each is a `<button>` carrying `aria-expanded`, so the state is
  announced rather than drawn.
- **Background summarizing.** Stop returns the record button immediately and the
  meeting summarizes in a row below, one at a time, saying how many are ahead of
  it. This is the behaviour already shipped in `core/engine.py`.
- **A dark appearance**, from the same semantic tokens.
- Reduced-motion, reduced-transparency and increased-contrast answers.
- Visible keyboard focus on everything.

---

## The three screens that were placeholders

Ask, Assistants and Settings existed only as "not built in this prototype".
They are built now, and each one is grounded in code that already exists rather
than in a guess about what it might do.

### Ask — `next_actions/across.py`

That module opens by stating two rules. This screen is the two rules made
visible:

> **Every answer says what it searched.** An answer drawn from 12 of 40 meetings
> is a different answer from one drawn from all 40, and a model handed the first
> without being told will present it as the second.
>
> **Every fact keeps its citation.** An action item without a source is a rumour
> with a due date.

So the scope line — `Searched 38 of 41 meetings · 3 not indexed, and not
included below` — is the **first** thing in an answer, not a footnote, and the
strip that says the same thing sits above the question field, before anything
is asked. And the citation grew: across an archive `00:18:22` identifies
nothing, so it carries the meeting too, and moves to its own line as one block
rather than splitting `Launch checkpoint ·` from the time it belongs to.

The six suggested questions are the six real functions — `open_action_items`,
`search_decisions`, `open_questions`, `meeting_timeline`, `brief_me`,
`stale_commitments` — which is what `generative-ai.md › Inputs` asks for:
*"offer diverse, predefined example inputs that hint at what's possible."*
Nothing matched is answered by coaching, not by prose: the same page says to
*"help people improve requests when blocked"*, and a search over extracted facts
has no business inventing a paragraph when it finds none.

Indexing is a button, not a background job, because it costs one extraction per
meeting — so it says how many and roughly how long before it starts.

### Assistants — the three questions, in order

Straight from `assistants_tab.py`: how do I connect one, what will it be able
to see, and what has it looked at. The third gets equal room, because it is the
one no config-file integration ever answers.

The scope window is `scope.py`'s, including the reason it is off by default,
and it is stated as a count of meetings rather than a number of days —
*"12 of 41 meetings. The other 29 are refused"* — because days are the setting
and meetings are the consequence. `privacy.md › Best practices`: *"Give people
precise control over their data by making your permission requests as specific
as possible."*

The two panels say what an assistant can and cannot read. Their glyphs carry the
difference, not their colour, so the pair survives greyscale.

### Settings — every key in `settings.EDITABLE`

Deliberately the whole editable surface, because that file makes the argument
itself: a setting that only exists as a line in `config.py` is reverted by the
next update, so anything the app tells someone to configure has to be reachable
from the UI. Eight collapsed groups keep it from reading as a wall.

Three choices are worth naming:

- **Capture source moved out.** It is on the Record screen, in the action row,
  beside the meter that shows it — and it goes unavailable while a recording
  runs, because it cannot change under a live capture. `settings.md` is
  explicit: *"prefer letting people modify task-specific options without going
  to your settings area … Putting this type of option in a separate settings
  area disconnects it from its context."* Settings keeps the same control, and
  the two stay in step.
- **Switches govern; checkboxes are governed.** `toggles.md` says a switch suits
  *"a group of settings, instead of just one"*, and that checkboxes are what
  *"communicate grouping"*. So Delivery is one switch over three checkboxes, and
  Sealed mode is a switch over all of it — turning it on visibly darkens
  everything that needs a network and says why, rather than accepting settings
  it will refuse to honour.
- **Transcribe vs translate is a pair of radio buttons**, not a checkbox.
  "Translate" is not "not transcribe", which is the case `toggles.md` reserves
  radios for.

A setting saves the moment it changes and says so in a line under the heading.
`feedback.md` keeps that in the interface; an alert for a saved checkbox would
be absurd.

## Appearance

The page follows the system. `dark-mode.md`: *"Avoid offering an app-specific
appearance setting… they may think your app is broken because it doesn't respond
to their systemwide appearance choice."*

**The review affordances are gone, as of 15 Sep 2026.** `?appearance=light|dark`
let both appearances be inspected on one machine, and `?screen=` / `?ask=` let a
headless render reach a screen without a click. They were always marked as
shipping out of the real build, and they left together: the block at the top of
`app.js`, the two lines that read it at the bottom, the `:root[data-appearance]`
rules in `tokens.css`, and the `--appearance=` / `--screen=` flags in
`shell.py`. There is now one appearance path — the system's.

One thing they taught us, worth keeping written down: forcing an appearance has
to re-declare `color-scheme`, or the native controls go on following the OS
while the tokens follow the override. On a dark machine, `?appearance=light`
drew a light page with dark radio buttons and a dark slider track, because
`color-scheme` — not a custom property — is what the browser paints form
controls from.

**Known papercut.** `shell.py` paints the native window Aurora's *light* ground
before the first frame, so a machine set to dark gets a brief pale flash on
launch. Matching it means reading the OS appearance — a registry key on Windows,
`defaults` on macOS — and that platform-specific code was judged not worth
adding on the way into a release. It is the value the file has always carried.

---

## The shell: pywebview, not Tauri

Tauri renders this fine on Windows (WebView2) and macOS (WKWebView). Linux is
WebKitGTK, where `backdrop-filter` and `color-mix()` are the weak spots — and
the one blurred surface in the app is `.chrome`, whose background is a
`color-mix()`. On an old webkit2gtk that degrades to nothing rather than to a
solid bar. Worth knowing, and fixable with a fallback.

The disqualifying problem is a different one. **Tauri's backend is Rust and this
app is Python** — Whisper, Ollama, `sounddevice`, the MCP server, `engine.py`.
Tauri means a Rust shell *plus* a PyInstaller sidecar *plus* IPC between them:
three moving parts where there is one today, on a product whose binaries are not
even signed yet.

`pywebview` is the same HTML in the same system webviews on the same three
platforms, with the backend in the language the backend is already written in.
Packaging stays PyInstaller. It is a native window, not a browser tab, so the
no-browser rule survives. And where Tauri is stuck with WebKitGTK on Linux,
pywebview can be pointed at the Qt/Chromium backend there instead.

### What was built

Three files, and `core/` is untouched — the window is another front end onto the
same `AppEngine` the tray and the MCP server already drive, which is what makes
it impossible for them to disagree about who holds the microphone.

| | |
|---|---|
| `ui-next/api.py` | The bridge. Every method is callable from the page as `window.pywebview.api.<name>()`. Engine callbacks — a transcribed line, a notes job changing state — come back the other way as one `vl` CustomEvent carrying a type, rather than a window global per event. |
| `ui-next/shell.py` | The window, its size and minimum, the close behaviour, and the native menu bar. |
| `run_ui.py` | The launcher. Same `sys.path` integration `run.py` does. |

The seam itself did not move: below the `the seam` banner in `app.js` there are
now **two drivers behind one interface**. `demo` is the prototype — a scripted
meeting, a five-second summary — and `live` calls into Python. The page above
cannot tell which it is holding, so what ships is the code path that was
reviewed, and the screenshots in this README stay honest.

Verified against a real machine: 63 meetings read out of the notes folder, the
real `settings.json` loaded into every control, the microphone picker listing
the actual device, no page errors.

```js
// prototype                         // pywebview
engine.queue({title, duration})  ->  pywebview.api.stop_and_save({defer_notes: true})
setTimeout(... ready ...)        ->  addEventListener('notes', e => card.ready(e.detail))
```

Python's side of the second line is `window.evaluate_js(...)` dispatching the
event. The real `AppEngine` already emits exactly what this expects — `queued`,
`running`, `done`, `failed`, one job at a time — so the shapes line up.

The menu bar is built: `designing-for-macos.md` wants every command reachable
from one and a webview supplies none of its own. Each item drives the *page*
rather than the engine — `vl.menu('record')` clicks the button the person would
have clicked — so the window and the menu can never show different things.

### What is still owed

- ~~**The meter has nothing real behind it.**~~ **Done, 15 Sep 2026.** It was
  one `on_level` hook in Core's audio path, exactly as this list predicted.
  `_Segmenter` reports the peak since the last call, about twelve times a
  second, with the detector's own speech flag beside it — peak rather than an
  instant, because a meter sampled on a timer misses the syllable between two
  samples and reads quiet during the speech it exists to show. It is opt-in the
  same way `on_partial` is: a front end that passes nothing runs none of the
  arithmetic. Proven not to disturb capture — the segmenter emits byte-identical
  utterances with the hook on and off.
- **The citations are still missing**, for the reason the next section gives.
  A meeting opened from the library shows its claims and omits the second.
- ~~**Ask and Assistants are still the prototype's data.**~~ **Assistants is
  real** as of 15 Sep: `api.mcp()` reads Core's host registry, so the list is
  every destination this build can offer, each with the config block it wants.
  It claims no "connected" state, because deciding that means reading another
  program's config file and a tick that is a guess is worse than no tick.
  **Ask is still the prototype's data and still needs Pro.**

## The open question

Aurora is built on cited facts: Decided, Owed, Still open, Risk, each carrying a
timestamp. **Core cannot supply any of that.** Its summariser produces four prose
sections and its prompt explicitly forbids clock times. The citations come from
`vlocalhost_pro`'s `ContextPack`, which already holds `decisions`, `actions`,
`open_questions` and `risks` with a `Fact.at` on every one.

So this design needs the pack, not the notes file — which costs one extraction
(~45 s on a local model) per meeting, currently only paid when someone clicks a
next-step button. A meeting without a pack shows the role and the claim and
simply omits the second, rather than printing a plausible-looking time nobody
said.

That is a product decision, not a design one, and it is the thing to settle
before this replaces the tkinter tab.
