#!/usr/bin/env python3
"""Build a self-contained Vlocalhost bundle for one platform.

The result is a zip that contains a complete CPython, every dependency already
installed into it, and the app. Unpack it and it runs. No system Python, no
``pip``, no PyPI, no network.

    python tools/build_bundle.py --target windows-x64 --out dist

**Dependencies are installed at build time, not at install time.** An earlier
design shipped a folder of wheels and ran ``pip install --no-index
--find-links`` on the user's machine. This is better: the failure modes of
``pip`` — a wheel that will not build, a resolver backtrack, a half-written
site-packages — move from thousands of user machines to one CI runner, where a
failure is a red build instead of a support ticket. What ships is a Python that
already works.

The catch is that wheels are platform-specific, so each bundle must be built on
its own platform. That is what the matrix in ``.github/workflows/release.yml``
is for. Building a macOS bundle on Linux silently produces something that
cannot run, so :func:`check_host` refuses.

**Why a whole interpreter.** Wheels are built per Python version *and* per
platform. Honouring "Python 3.9 or newer" across four platforms means a matrix
of roughly 25 wheel sets. Shipping our own interpreter makes it four. It also
removes the single most common install failure: no Python, or a Python that is
too old, or a Python from the Microsoft Store with a read-only site-packages.

Not the Windows *embeddable* distribution, which is the obvious candidate and
does not include ``tkinter`` or ``pip`` — the app is tkinter from top to
bottom, so that build cannot start. python-build-standalone's ``install_only``
archives are complete, and are explicitly meant to be redistributed.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from version import __version__ as APP_VERSION  # noqa: E402

# --- the interpreter we ship ----------------------------------------------
# Pinned deliberately. An unpinned "latest" means two bundles built a week
# apart contain different Pythons, and a bug report stops being reproducible.
PBS_RELEASE = "20260807"
PYTHON_VERSION = "3.12.13"
PBS_BASE = ("https://github.com/astral-sh/python-build-standalone/releases/"
            f"download/{PBS_RELEASE}")

#: target name -> python-build-standalone triple
TARGETS = {
    "windows-x64": "x86_64-pc-windows-msvc",
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x64": "x86_64-apple-darwin",
    "linux-x64": "x86_64-unknown-linux-gnu",
}

#: What the host must look like to build each target.
_HOST = {
    "windows-x64": ("Windows", None),
    "macos-arm64": ("Darwin", "arm64"),
    "macos-x64": ("Darwin", "x86_64"),
    "linux-x64": ("Linux", None),
}

#: Copied into the bundle. Everything else in the repo is for developers.
INCLUDE_FILES = ("requirements.txt", "LICENSE", "README.md", "TRADEMARK.md")
INCLUDE_DIRS = ("integrations", "assets", "docs")
SKIP_DIRS = {"__pycache__", ".git", ".github", "tools", "notes", ".venv",
             "dist", "build"}


def log(message):
    print(f"[bundle] {message}", flush=True)


def check_host(target):
    """Refuse to build a bundle whose wheels cannot be correct."""
    want_system, want_machine = _HOST[target]
    system = platform.system()
    if system != want_system:
        raise SystemExit(
            f"{target} must be built on {want_system}; this is {system}.\n"
            f"Native wheels cannot be cross-compiled — use the CI matrix.")
    if want_machine and platform.machine().lower() not in (
            want_machine.lower(), "arm64" if want_machine == "aarch64" else ""):
        raise SystemExit(
            f"{target} must be built on {want_machine}; this is "
            f"{platform.machine()}.")


def download(url, dest):
    log(f"downloading {os.path.basename(url)}")
    with urllib.request.urlopen(url) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)
    return dest


def fetch_runtime(target, workdir):
    """Download and unpack CPython. Returns the directory holding it."""
    name = (f"cpython-{PYTHON_VERSION}+{PBS_RELEASE}-{TARGETS[target]}"
            f"-install_only.tar.gz")
    archive = os.path.join(workdir, name)
    if not os.path.isfile(archive):
        download(f"{PBS_BASE}/{name}", archive)

    runtime_parent = os.path.join(workdir, "runtime")
    shutil.rmtree(runtime_parent, ignore_errors=True)
    os.makedirs(runtime_parent, exist_ok=True)
    log("unpacking the interpreter")
    with tarfile.open(archive) as tar:
        # The archive contains a single top-level "python/" directory.
        # filter="data" refuses absolute paths, "..", and device nodes. It is
        # the default from Python 3.14; asking for it explicitly keeps this
        # build reproducible across interpreter versions instead of changing
        # behaviour under us.
        if sys.version_info >= (3, 12):
            tar.extractall(runtime_parent, filter="data")
        else:
            tar.extractall(runtime_parent)
    unpacked = os.path.join(runtime_parent, "python")
    if not os.path.isdir(unpacked):
        raise SystemExit(f"unexpected archive layout in {name}")
    return unpacked


def interpreter(runtime):
    """Path to the python executable inside an unpacked runtime."""
    win = os.path.join(runtime, "python.exe")
    if os.path.isfile(win):
        return win
    for candidate in ("bin/python3", "bin/python"):
        path = os.path.join(runtime, *candidate.split("/"))
        if os.path.isfile(path):
            return path
    raise SystemExit(f"no interpreter found in {runtime}")


def install_dependencies(runtime):
    """Install requirements straight into the shipped interpreter."""
    python = interpreter(runtime)
    requirements = os.path.join(ROOT, "requirements.txt")
    log("installing dependencies into the interpreter")
    subprocess.run([python, "-m", "pip", "install", "--upgrade", "pip",
                    "--disable-pip-version-check", "-q"], check=True)
    subprocess.run([python, "-m", "pip", "install", "-r", requirements,
                    "--disable-pip-version-check", "-q"], check=True)
    # A conflict here would otherwise surface as an ImportError on a user's
    # machine, long after anyone can connect it to the build.
    subprocess.run([python, "-m", "pip", "check"], check=True)
    frozen = subprocess.run([python, "-m", "pip", "freeze"],
                            check=True, capture_output=True, text=True)
    prune_bytecode(runtime)
    return [line for line in frozen.stdout.splitlines() if line.strip()]


def prune_bytecode(root):
    """Delete every ``__pycache__`` from the shipped interpreter.

    Two reasons, and the second one is not obvious:

    * **Size.** Around 43 MB and 2,400 files on a Windows build, for caches
      Python regenerates on first import anyway.
    * **Windows MAX_PATH.** Bytecode names are the longest in the tree
      (``…/fbs/__pycache__/RuntimeOptimizationRecordContainerEntry.cpython-312.pyc``).
      They pushed the full path past 260 characters during installer
      compilation, which fails with nothing more useful than "The system
      cannot find the path specified". Users install into deeper folders than
      we build in, so this is their problem before it is ours.
    """
    removed = 0
    freed = 0
    for folder, dirs, _ in os.walk(root, topdown=False):
        if os.path.basename(folder) != "__pycache__":
            continue
        for entry, _, files in os.walk(folder):
            for name in files:
                try:
                    freed += os.path.getsize(os.path.join(entry, name))
                except OSError:
                    pass
        shutil.rmtree(folder, ignore_errors=True)
        removed += 1
    log(f"pruned {removed} __pycache__ folders ({freed / 1_000_000:.0f} MB)")


def copy_app(dest):
    """Copy the application source, leaving developer clutter behind."""
    os.makedirs(dest, exist_ok=True)
    for name in sorted(os.listdir(ROOT)):
        source = os.path.join(ROOT, name)
        if os.path.isfile(source) and (name.endswith(".py")
                                       or name in INCLUDE_FILES):
            shutil.copy2(source, os.path.join(dest, name))
        elif os.path.isdir(source) and name in INCLUDE_DIRS:
            shutil.copytree(
                source, os.path.join(dest, name),
                ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc"))
    return dest


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(staging, target, packages):
    manifest = {
        "app": "Vlocalhost.AI",
        "version": APP_VERSION,
        "target": target,
        "python": PYTHON_VERSION,
        "python_build": PBS_RELEASE,
        "packages": packages,
    }
    path = os.path.join(staging, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


def write_launchers(staging, target):
    """A double-clickable entry point that uses the bundled interpreter."""
    if target.startswith("windows"):
        # pythonw, so no console window sits behind the app.
        with open(os.path.join(staging, "Vlocalhost.cmd"), "w",
                  encoding="utf-8", newline="\r\n") as f:
            f.write('@echo off\r\n'
                    'start "" "%~dp0runtime\\pythonw.exe" '
                    '"%~dp0app\\vlocalhost.py" %*\r\n')

        # The ZIP has no installer, so nothing creates the desktop icon the
        # .exe would have made. The app can do it — --install-shortcut has
        # shipped since 1.0.3 — but only if somebody knows to type it, and
        # people who took the portable build are precisely the ones least
        # likely to open a terminal. Two double-clickable files instead.
        helpers = {
            "Create desktop shortcut.cmd": "--install-shortcut",
            "Remove desktop shortcut.cmd": "--remove-shortcut",
        }
        for name, flag in helpers.items():
            with open(os.path.join(staging, name), "w",
                      encoding="utf-8", newline="\r\n") as f:
                f.write('@echo off\r\n'
                        '"%~dp0runtime\\python.exe" "%~dp0app\\vlocalhost.py" '
                        + flag + '\r\n'
                        'echo.\r\n'
                        'pause\r\n')
    else:
        path = os.path.join(staging, "Vlocalhost.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\n'
                    'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
                    'exec "$HERE/runtime/bin/python3" "$HERE/app/vlocalhost.py" "$@"\n')
        os.chmod(path, 0o755)

    if target.startswith("linux"):
        # Ship the installer inside the bundle: it expects to be run from the
        # unpacked directory, and a separate download is one more thing to get
        # wrong.
        source = os.path.join(ROOT, "installer", "linux", "install.sh")
        if os.path.isfile(source):
            destination = os.path.join(staging, "install.sh")
            shutil.copy2(source, destination)
            os.chmod(destination, 0o755)


def zip_bundle(staging, out_dir, target):
    os.makedirs(out_dir, exist_ok=True)
    archive = os.path.join(out_dir,
                           f"vlocalhost-{APP_VERSION}-{target}.zip")
    if os.path.exists(archive):
        os.remove(archive)
    log(f"compressing -> {os.path.basename(archive)}")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for folder, dirs, files in os.walk(staging):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                full = os.path.join(folder, name)
                zf.write(full, os.path.relpath(full, staging))
    return archive


def smoke_test(runtime, app_dir):
    """Prove the bundle can import the app before anyone downloads it."""
    python = interpreter(runtime)
    log("smoke-testing the bundle")
    checks = (
        "import tkinter; tkinter.Tcl()",          # the GUI toolkit exists
        "import faster_whisper, numpy, requests",  # the heavy deps import
        "import sounddevice",                      # PortAudio binding loads
        "import vlocalhost, engine, settings, migrate, setup_wizard",
    )
    for snippet in checks:
        result = subprocess.run([python, "-c", snippet], cwd=app_dir,
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"smoke test failed on `{snippet}`:\n"
                             f"{result.stderr.strip()}")
    log("smoke test passed")


def build(target, out_dir, workdir, skip_tests=False):
    check_host(target)
    os.makedirs(workdir, exist_ok=True)
    staging = os.path.join(workdir, f"stage-{target}")
    shutil.rmtree(staging, ignore_errors=True)
    if os.path.exists(staging):
        raise SystemExit(
            f"Could not clear {staging}.\n"
            "Something is holding it open — most often a shell or a file "
            "manager whose current directory is inside it, or the app running "
            "from this build. Close it and run again.")
    os.makedirs(staging)

    runtime = fetch_runtime(target, workdir)
    packages = install_dependencies(runtime)

    log("assembling")
    shutil.move(runtime, os.path.join(staging, "runtime"))
    app_dir = copy_app(os.path.join(staging, "app"))
    write_launchers(staging, target)
    manifest = write_manifest(staging, target, packages)

    if not skip_tests:
        smoke_test(os.path.join(staging, "runtime"), app_dir)

    # The smoke test imports the app, and Python writes __pycache__ where it
    # imports from. Those files are not payload: the zip is packed now and the
    # installer is packed later from the same tree, so anything created between
    # the two makes the artifacts disagree. Remove them here, after the last
    # thing that can create them.
    prune_bytecode(app_dir)

    archive = zip_bundle(staging, out_dir, target)
    digest = sha256(archive)
    with open(archive + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(archive)}\n")

    size = os.path.getsize(archive) / 1_000_000
    log(f"done: {os.path.basename(archive)}  {size:.0f} MB")
    log(f"sha256: {digest}")
    log(f"packages: {len(manifest['packages'])}")
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True, choices=sorted(TARGETS),
                        help="platform to build for; must match this machine")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"),
                        help="where to write the zip (default: <repo>/dist)")
    parser.add_argument("--work", default=os.path.join(ROOT, "build"),
                        help="scratch directory (downloads are cached here)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the smoke test (not for releases)")
    args = parser.parse_args(argv)

    build(args.target, os.path.abspath(args.out), os.path.abspath(args.work),
          skip_tests=args.skip_tests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
