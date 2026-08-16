# Contributing to Vlocalhost Core

Thanks for being here. This document covers the two things that trip people up
in an open-core project: **what belongs in Core**, and **the CLA**.

## The CLA (please read before you write code)

Vlocalhost Core is open source under the AGPL. Vlocalhost Pro is a separate,
commercial product built on top of it. Because the same maintainers ship both,
every contributor must sign a **Contributor License Agreement** before a pull
request can be merged.

The CLA grants us the right to use your contribution in both the open-source
Core and the commercial Pro edition, while **you keep the copyright to your own
work**. It is not an assignment — you are not signing your code away, and your
contribution stays AGPL in Core forever.

The CLA bot will comment on your first pull request with a one-click link. If
you would rather read it first, ask in the PR and we will link it before you
start.

If that arrangement isn't for you, that's a legitimate position — please open an
issue describing the problem instead of a PR with the fix, and we'll implement
it independently. No hard feelings either way.

## What belongs in Core

Core is the complete local product: capture, transcription, summarization, notes
on disk, the window, the tray, the terminal front end, and the MCP server.
Anything that makes recording your own meetings better belongs here.

Core deliberately ships **no** calendar or email providers. That is not an
oversight and not a crippled build — it is the architecture. A Core install has
no code path that can reach a calendar or a mail server, which is what lets the
privacy claim be structural rather than a promise.

### The one rule

**Core must never know what is installed on top of it.**

Optional capabilities register themselves through extension points. Core asks a
registry what is available and behaves accordingly. Concretely:

- ✅ Add an extension point (a registry, an interface, a hook) that any
  implementation could use.
- ✅ Add a generic empty state for when nothing implements it.
- ❌ Import an optional package by name anywhere outside `plugins.py`.
- ❌ Branch on `if pro:` / `if "google"` / `if edition == ...` to change
  behaviour.
- ❌ Hardcode the names of implementations that Core does not ship.

`plugins.py` is the *only* module that knows optional packages exist, and all it
knows is a package name. `integrations/__init__.py` is the reference example of
an extension point: the interface and registry live in Core, the implementations
live outside it.

If a feature you want needs Core to see something new, the right shape is almost
always "add a generic hook to Core, implement against it elsewhere" — never
"teach Core about this specific thing".

## Getting set up

```bash
git clone https://github.com/antigravitysoham-eng/vlocalhost-core.git
cd vlocalhost-core
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python tools/install_hooks.py     # once per clone
python vlocalhost.py
```

You will also need [Ollama](https://ollama.com/download) running for the
summarization step, and PortAudio on macOS/Linux — see the README.

`install_hooks.py` adds a `pre-push` check that refuses a push whose added
lines read like paid-tier material — this repository is public, and that rule
is easy to forget at the end of a long session. If it stops a push you know to
be generic, `git push --no-verify`.

## Before you open a pull request

- **Run the app.** The bar is that every front end still starts:
  `python vlocalhost.py`, `--tray`, `--no-tray`, `--mcp`, `--devices`.
- **Check it imports clean.** `python -m py_compile *.py integrations/*.py`
- **Measure anything touching audio, the VAD, or the model.** There is no unit
  test suite, but there is a bench, and latency claims made without it have
  been wrong in both directions:

  ```bash
  python tools/make_fixtures.py                      # once; Windows SAPI voices
  python tools/bench_pipeline.py --all --repeat 2
  ```

  Compare against the recorded numbers in [`docs/performance.md`](docs/performance.md).
  Use `--repeat`: a laptop under load varies enough to fake a result.
- **Match the surrounding code.** This codebase writes comments that explain
  *why*, uses full sentences in docstrings, and avoids abbreviations. Please
  keep that.
- **One concern per PR.** A refactor and a feature in the same diff is two PRs.

## Releasing

Versions are [SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`, tagged `vX.Y.Z`.
A release that adds a feature is a **minor** bump even when the diff is small —
a patch release that adds a microphone picker misleads everyone reading the
changelog.

**`version.py` is the only place a human types a version.** The installer
scripts, the build and the release workflow all read it, and the workflow
refuses a tag that disagrees with it.

```bash
# 1. Prove all four platforms build, before creating any tag.
gh workflow run release.yml -f dry_run=true

# 2. Bump version.py, commit.
# 3. Tag and push. A release candidate is v1.2.0-rc.1 against version.py
#    1.2.0 -- the suffix is stripped before the check and marks the draft as a
#    prerelease, which keeps it out of `releases/latest`.
git tag v1.2.0 && git push origin v1.2.0

# 4. Review the draft on GitHub, then publish it by hand.
```

Every release carries, per platform: the installer (`.exe` / `.dmg` /
`.tar.gz`), a `.zip`, a `.sha256` for each, and a CycloneDX SBOM generated on
the runner that built that bundle. The install page resolves its download by
matching `*-windows-x64-setup.exe` and `macos-(arm64|x64).dmg` against
`releases/latest`, **so a release missing an asset silently breaks the website**
— v1.0.1 through v1.0.5 shipped with no assets at all and the download button
just opened the releases page. `draft-release` prints a warning naming any
platform that did not produce one; read it before publishing.

## Reporting bugs

Include your OS, Python version, which front end you were using, and the output
of `python vlocalhost.py --devices` if it is audio-related. Never paste real
meeting transcripts into an issue — redact or synthesize.

## Security

Do not file security issues publicly. See [`SECURITY.md`](SECURITY.md).
