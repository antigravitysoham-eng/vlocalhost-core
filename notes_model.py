"""Where the note-writing weights come from, and how they get here.

Three ways a machine can have the model, in the order they are preferred:

1. **A path the user set.** ``config.NOTES_MODEL`` wins whenever it is filled
   in. Somebody who converted their own model should never be second-guessed.
2. **A copy the installer shipped.** ``config.BUNDLED_MODELS_DIR`` -- how an
   offline install arrives with the weights already present, so first run needs
   no network at all.
3. **A copy this app downloaded.** One curated model, pinned to a revision,
   fetched once into the user's data folder.

That order is the whole design. The online installer ships without weights and
falls to (3); the offline installer ships with them and stops at (2); a person
bringing their own model sets (1) and never touches either. One code path, and
which one you get is a consequence of how you installed, not a decision anyone
has to understand.

Nothing here is required for the app to work. Without a notes model you still
get a full transcript -- only the summary is skipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class ModelSpec:
    """One downloadable set of weights, pinned so every machine gets the same.

    ``engine`` is the built-in that runs it. Recording the pairing here means a
    settings screen can offer "the built-in one" without knowing which runtime
    that happens to be this release -- and means changing it later is one line.
    """

    label: str          #: what to call it in front of a person
    repo: str           #: Hugging Face repo id
    revision: str       #: pinned commit, so two installs are never different
    folder: str         #: directory name this model is stored under
    files: tuple        #: every file to fetch, relative to the repo root
    #: What the engine is handed. Empty means the folder itself, which is what
    #: CTranslate2 loads; a filename means that file inside it, which is what
    #: llama.cpp wants. One field is the whole difference between the two.
    entry: str
    size_mb: int        #: rounded, for telling somebody before they commit
    engine: str         #: which built-in engine runs it
    licence: str        #: what lets us redistribute or fetch it
    licence_url: str    #: where a person can read those terms for themselves

    def url(self, filename: str) -> str:
        """Where one of this model's files lives, at the pinned revision."""
        return (f"https://huggingface.co/{self.repo}/resolve/"
                f"{self.revision}/{filename}")


#: Two built-in engines, because no single one covers every platform we ship.
#:
#: llama.cpp is the better engine here -- roughly 14s against 34s on the same
#: transcript, and it produces all four sections unaided while the CTranslate2
#: conversion needs a generation floor to stop it ending after the Summary.
#: Where it can be shipped, it should be.
#:
#: It cannot be shipped everywhere. ``llama-cpp-python`` publishes no wheels on
#: PyPI, only an sdist, but does publish ABI-independent ``py3-none-<platform>``
#: wheels on the maintainer's own index (see requirements.txt). Checked
#: 1 September 2026 for the current 0.3.35:
#:
#:     Windows x64            py3-none-win_amd64                  yes
#:     macOS Apple Silicon    py3-none-macosx_11_0_arm64          yes
#:     Linux x86_64/aarch64   py3-none-manylinux / musllinux      yes
#:     macOS Intel            -                                   NO
#:
#: No ``py3-none`` x86_64 macOS wheel has ever existed; the newest usable one
#: there is 0.3.2. So macOS Intel gets CTranslate2, which ships everywhere for
#: free as a transitive dependency of faster-whisper and needs no index, no
#: wheel and no compiler.
#:
#: The two engines want different files -- one GGUF, or a converted folder --
#: which is why ModelSpec carries a file list rather than a filename.
#:
#: Licence, checked the same day against the Hugging Face API: both repos
#: declare apache-2.0 and neither is gated, which is what allows the app to
#: fetch them without an account and an installer to ship them without passing
#: extra terms downstream.
#:
#: The trap for whoever changes these: **Qwen2.5 is not uniformly Apache.**
#: ``Qwen2.5-3B-Instruct`` declares ``other`` / ``qwen-research``. Bumping a
#: size up for quality would silently move the product onto a research licence.
#: Re-check the declared licence of the exact repo every time, and update the
#: SBOM with it.

_GGUF = ModelSpec(
    label="Qwen2.5 1.5B Instruct",
    repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
    revision="91cad51170dc346986eccefdc2dd33a9da36ead9",
    folder="qwen2.5-1.5b-instruct-gguf",
    files=("qwen2.5-1.5b-instruct-q4_k_m.gguf",),
    entry="qwen2.5-1.5b-instruct-q4_k_m.gguf",
    #: Measured against the pinned URL, not taken from Ollama's packaging of the
    #: same model -- those are different files and differ by ~80 MB.
    size_mb=1065,
    engine="llamacpp",
    licence="Apache-2.0",
    licence_url="https://www.apache.org/licenses/LICENSE-2.0",
)

_CT2 = ModelSpec(
    label="Qwen2.5 1.5B Instruct",
    repo="jncraton/Qwen2.5-1.5B-Instruct-ct2-int8",
    revision="55bb006d27f0b32c8b872a49fecc1d27b5071276",
    folder="qwen2.5-1.5b-instruct-ct2-int8",
    files=("config.json", "generation_config.json", "model.bin",
           "tokenizer.json", "vocabulary.json"),
    entry="",
    #: Summed over every file above. The int8 conversion is larger than the
    #: q4_k_m GGUF of the same model, 1485 MB against 1065.
    size_mb=1485,
    engine="ctranslate2",
    licence="Apache-2.0",
    licence_url="https://www.apache.org/licenses/LICENSE-2.0",
)


def _has_llamacpp_wheel() -> bool:
    """True where ``llama-cpp-python`` can actually be installed.

    Everywhere except macOS on Intel. Decided from the platform rather than by
    importing llama_cpp, because this has to give the same answer on a machine
    where the package has not been installed yet -- a settings screen deciding
    what to offer cannot depend on the thing it is deciding about.
    """
    import platform

    if platform.system() != "Darwin":
        return True
    return platform.machine().lower() in ("arm64", "aarch64")


def default_spec() -> ModelSpec:
    """The model this platform should fetch, and the engine that will run it."""
    return _GGUF if _has_llamacpp_wheel() else _CT2


#: Resolved once at import. Platform does not change under a running process,
#: and every caller wants the same answer.
DEFAULT = default_spec()

#: Every model this app knows how to fetch, so a label lookup or a cleanup can
#: recognise a folder it did not choose -- a machine that switched engines, or
#: a bundle built for the other one.
SPECS = (_GGUF, _CT2)


def spec_for(engine: str) -> ModelSpec:
    """The model that belongs to an engine, not the one this platform prefers.

    ``DEFAULT`` answers "what should this machine fetch". It is the wrong
    question once somebody picks an engine by hand under Advanced: on Windows
    the default is the GGUF, so selecting CTranslate2 there used to hand it a
    .gguf file -- the wrong format entirely -- and the failure surfaced as
    "could not load (RuntimeError)" with nothing pointing at the cause.
    """
    for spec in SPECS:
        if spec.engine == engine:
            return spec
    return DEFAULT


def _app_dir() -> str:
    """The folder the application was installed into."""
    return os.path.dirname(os.path.abspath(__file__))


def bundled_dir() -> str:
    """Where an installer put the weights, or "" if it did not.

    Resolved against the install when the setting is relative, because an
    installer cannot know the absolute path it will land on and a user who
    moves the folder should not lose the model it contains.
    """
    raw = (getattr(config, "BUNDLED_MODELS_DIR", "") or "").strip()
    if not raw:
        return ""
    return raw if os.path.isabs(raw) else os.path.join(_app_dir(), raw)


def download_dir() -> str:
    """Where this app puts a model it fetched itself.

    Under the user's data folder rather than the program folder: it survives an
    update, it does not need administrator rights, and an uninstall that removes
    the program leaves a gigabyte the user can delete when they mean to.
    """
    from integrations import store

    return os.path.join(store.data_dir(), "models")


def _within(base: str, spec: ModelSpec) -> str:
    """The path an engine is handed, given the directory a copy lives in."""
    root = os.path.join(base, spec.folder)
    return os.path.join(root, spec.entry) if spec.entry else root


def bundled_path(spec: ModelSpec = DEFAULT) -> str:
    folder = bundled_dir()
    return _within(folder, spec) if folder else ""


def downloaded_path(spec: ModelSpec = DEFAULT) -> str:
    return _within(download_dir(), spec)


def resolve(spec: ModelSpec = DEFAULT) -> str:
    """The weights to use, or "" if this machine does not have them yet.

    Never raises and never downloads: a settings screen, a diagnostic report and
    the engine itself all need to ask this question without side effects.
    """
    explicit = (getattr(config, "NOTES_MODEL", "") or "").strip()
    if explicit:
        return explicit
    for candidate in (bundled_path(spec), downloaded_path(spec)):
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def source(spec: ModelSpec = DEFAULT) -> str:
    """Which of the three the current answer came from, for a support report."""
    if (getattr(config, "NOTES_MODEL", "") or "").strip():
        return "set by you"
    if bundled_path(spec) and os.path.exists(bundled_path(spec)):
        return "shipped with the install"
    if os.path.exists(downloaded_path(spec)):
        return "downloaded by this app"
    return "not on this machine"


def present(spec: ModelSpec = DEFAULT) -> bool:
    return bool(resolve(spec))


def downloaded_size_mb(spec: ModelSpec = DEFAULT) -> int:
    """Megabytes our own download is using, or 0 if there is none.

    Only ever measures the copy this app fetched. A bundled copy belongs to the
    installer and a path the user set belongs to them; neither is ours to
    report as reclaimable.
    """
    root = os.path.join(download_dir(), spec.folder)
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total // 1048576


def download(progress, spec: ModelSpec = DEFAULT) -> str:
    """Fetch the weights, reporting progress. Returns "" on success.

    ``progress`` is called with (fraction, text); fraction is None while the
    size is unknown. The same shape as the Ollama pull in setup_wizard, so a
    settings screen can drive either with one progress bar.

    A sealed install refuses, for the same reason the Ollama pull refuses: this
    is a real internet connection, and the switch is worth nothing if it has
    exceptions. A sealed machine is exactly the case the bundled copy exists
    for -- the offline installer is the supported path, not a workaround.

    Downloads to a temporary name and renames on success, so an interrupted
    download can never be mistaken for a model.
    """
    import requests

    import network

    if not network.allowed("notes_model_download"):
        return str(network.refuse("notes_model_download"))

    root = os.path.join(download_dir(), spec.folder)
    os.makedirs(root, exist_ok=True)
    expected = spec.size_mb * 1048576
    done = 0
    written = []

    try:
        for name in spec.files:
            target = os.path.join(root, name)
            os.makedirs(os.path.dirname(target) or root, exist_ok=True)
            partial = target + ".part"
            written.append((partial, target))
            with requests.get(spec.url(name), stream=True,
                              timeout=(10, 600)) as resp:
                resp.raise_for_status()
                with open(partial, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        # One bar across the whole model, not one per file: the
                        # person is waiting for a model, and five bars that each
                        # reach the end and start again reads like a stall.
                        progress(min(done / expected, 1.0),
                                 f"{done // 1048576} of {spec.size_mb} MB")
            os.replace(partial, target)
        return ""
    except Exception as e:                        # noqa: BLE001
        # A half-fetched folder must not look like a model. Remove what this
        # attempt wrote, and leave nothing an engine could try to load.
        for partial, target in written:
            for path in (partial, target):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return str(e)


def remove(spec: ModelSpec = DEFAULT) -> bool:
    """Delete the copy this app downloaded. True if there was one to delete.

    A gigabyte the user did not choose to keep should be removable from inside
    the app that put it there. Only ever touches our own download -- a bundled
    copy belongs to the installer and a path the user set belongs to them.
    """
    import shutil

    root = os.path.join(download_dir(), spec.folder)
    if not os.path.isdir(root):
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not os.path.exists(root)
