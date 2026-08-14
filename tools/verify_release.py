#!/usr/bin/env python3
"""Prove the ZIP and the installer carry the same build, before publishing.

    python tools/verify_release.py --target windows-x64

The two artifacts are packed minutes apart from one staging tree, by different
tools. Anything that touches the tree in between — a smoke test writing
``__pycache__``, an editor saving a file, a half-finished rebuild — makes them
disagree, and nothing downstream would notice: both hash fine, both install,
and the difference only shows up as a user reporting a bug that cannot be
reproduced from the other download.

So compare them directly. Unpack the zip in memory, install nothing, and read
the installer's payload out of the compiled setup with Inno Setup's own
extractor if it is available; otherwise fall back to comparing the zip against
the staging tree the installer was built from.

Exits non-zero with the differing paths named, so a release script can stop.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from version import __version__ as APP_VERSION  # noqa: E402

#: The interpreter is thousands of files and identical by construction — it is
#: moved into the tree once, before either artifact is packed. What matters is
#: the part that changes between builds.
SKIP_PREFIXES = ("runtime/", "unins")


def log(message):
    print(f"[verify] {message}", flush=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def zip_payload(path):
    out = {}
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        root = ""
        first = names[0].split("/")[0] + "/"
        if all(n.startswith(first) for n in names):
            root = first
        for name in names:
            rel = name[len(root):].lower()
            if rel.startswith(SKIP_PREFIXES):
                continue
            with z.open(name) as f:
                out[rel] = digest(f.read())
    return out


def tree_payload(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace("\\", "/").lower()
            if rel.startswith(SKIP_PREFIXES):
                continue
            with open(full, "rb") as f:
                out[rel] = digest(f.read())
    return out


def installer_payload(setup_exe):
    """Extract the installer with innoextract, when it is installed."""
    tool = shutil_which("innoextract")
    if not tool:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run([tool, "-e", "-s", "-d", tmp, setup_exe],
                                capture_output=True, text=True)
        if result.returncode != 0:
            log("innoextract failed; falling back to the staging tree")
            return None
        app = os.path.join(tmp, "app")
        return tree_payload(app if os.path.isdir(app) else tmp)


def shutil_which(name):
    import shutil

    return shutil.which(name)


def compare(left, right, left_name, right_name):
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    shared = sorted(set(left) & set(right))
    differ = [k for k in shared if left[k] != right[k]]

    log(f"{left_name}: {len(left)} files   {right_name}: {len(right)} files")
    log(f"identical: {len(shared) - len(differ)} of {len(shared)} shared")
    for label, items in ((f"only in {left_name}", only_left),
                         (f"only in {right_name}", only_right),
                         ("differing", differ)):
        if items:
            log(f"{label}: {len(items)}")
            for item in items[:12]:
                log(f"    {item}")
            if len(items) > 12:
                log(f"    … and {len(items) - 12} more")
    return not (only_left or only_right or differ)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default="windows-x64")
    args = ap.parse_args(argv)

    zip_path = os.path.join(ROOT, "dist",
                            f"vlocalhost-{APP_VERSION}-{args.target}.zip")
    setup = os.path.join(ROOT, "dist",
                         f"vlocalhost-{APP_VERSION}-{args.target}-setup.exe")
    staging = os.path.join(ROOT, "build", f"stage-{args.target}")

    for path in (zip_path, staging):
        if not os.path.exists(path):
            raise SystemExit(f"missing: {path}")

    log(f"vlocalhost {APP_VERSION} {args.target}")
    zipped = zip_payload(zip_path)

    other, name = None, ""
    if os.path.isfile(setup):
        other, name = installer_payload(setup), "installer"
    if other is None:
        other, name = tree_payload(staging), "staging tree"
        log("comparing the zip against the tree the installer was packed from")

    ok = compare(zipped, other, "zip", name)
    log("IN SYNC" if ok else "OUT OF SYNC")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
