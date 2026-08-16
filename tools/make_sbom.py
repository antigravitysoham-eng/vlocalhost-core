#!/usr/bin/env python3
"""Generate the CycloneDX SBOM from a built bundle.

    python tools/make_sbom.py --target windows-x64
    python tools/make_sbom.py --target macos-arm64 --out dist/sbom-macos.json

Reads ``build/<target>/manifest.json`` for the exact package pins the installer
ships, then each package's own ``.dist-info/METADATA`` inside the bundled
runtime for licence and description.

An SBOM is a statement about **an artifact**, so it has to be derived from one.
The file this replaces was hand-authored and drifted the moment the product
started shipping its own interpreter: it declared 10 components at version
1.0.0 while the installer carried 37 packages plus CPython, and the versions it
did name were the ``>=`` floors from requirements.txt rather than what a user
received.

It also wrote to one fixed path regardless of which bundle it read, so
generating the macOS SBOM would have silently overwritten the Windows one and
left a document describing a Windows installer under a name implying it covered
everything. Output is per-target, and the default name says which target it is.

Optional integration libraries are listed with ``scope: optional``: they are
supported, they are not in the installer, and a reviewer needs to see both
facts rather than guess which list they are reading.
"""

import argparse
import glob
import hashlib
import io
import json
import os
import re
import uuid
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)

# A stable namespace so regenerating the same release keeps its serial number.
NS = uuid.UUID("1b0f5c9a-4c1e-5a7f-9c2d-9f7a3b6e10aa")

# Not shipped in the bundle; installed by the user only when they connect an
# account. Floors, because that is genuinely all that is pinned for them.
OPTIONAL = [
    ("google-api-python-client", "2.100.0", "Apache-2.0",
     "Google Calendar + Gmail APIs (optional integration)."),
    ("google-auth-oauthlib", "1.2.0", "Apache-2.0",
     "Google OAuth installed-app flow (optional integration)."),
    ("msal", "1.28.0", "MIT",
     "Microsoft device-code authentication for Graph (optional integration)."),
]

# What each direct dependency is actually for. Upstream summaries describe the
# library; this describes its job here, which is the useful thing in a review.
ROLE = {
    "sounddevice": "Microphone capture via PortAudio.",
    "webrtcvad-wheels": "Fallback voice-activity detection (prebuilt webrtcvad).",
    "soundcard": "System-audio loopback capture, for recording the far end of a call.",
    "faster-whisper": "Local speech-to-text (CTranslate2 Whisper), and the "
                      "Silero voice-activity model bundled with it.",
    "ctranslate2": "Inference engine underneath faster-whisper.",
    "numpy": "PCM to float32 conversion and array math.",
    "requests": "HTTP client for the local Ollama server.",
    "pystray": "System-tray icon and menu.",
    "pillow": "Image rendering for the tray icon.",
    "psutil": "Machine benchmark in Settings.",
    "av": "Audio decoding for faster-whisper.",
    "onnxruntime": "Runs the Silero voice-activity model that decides when "
                   "somebody is speaking.",
    "tokenizers": "Whisper tokenizer.",
    "huggingface-hub": "Downloads model weights on first use.",
}

# Classifier and free-text licence phrases that have an SPDX equivalent. A
# validator rejects anything in `id` that is not a real identifier.
SPDX = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "3-Clause BSD License": "BSD-3-Clause",
    "2-Clause BSD License": "BSD-2-Clause",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}

#: What a shipped installer looks like, per platform. The Windows-only glob
#: this used to carry meant a macOS SBOM would name no artifact at all.
INSTALLER_PATTERNS = ("*setup.exe", "*.dmg", "*.tar.gz", "*.zip")


def read_manifest(target, stage=""):
    if stage:
        path = os.path.join(stage, "manifest.json")
        if not os.path.isfile(path):
            raise SystemExit(f"No manifest.json in {stage}")
        found = [path]
    else:
        # build_bundle.py stages into build/stage-<target>. Naming the target
        # without the prefix found nothing, which the original hid by only ever
        # being run with no --target at all and globbing whatever was newest --
        # fine with one bundle on disk, and a document describing the wrong
        # platform as soon as there were two.
        patterns = ([os.path.join(CORE, "build", "stage-" + target, "manifest.json"),
                     os.path.join(CORE, "build", target, "manifest.json")]
                    if target else
                    [os.path.join(CORE, "build", "*", "manifest.json")])
        found = []
        for pattern in patterns:
            found = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
            if found:
                break
    if not found:
        raise SystemExit(
            f"No bundle manifest for target {target or 'any'} — run "
            f"tools/build_bundle.py first.")
    with io.open(found[0], encoding="utf-8") as f:
        data = json.load(f)
    data["_dir"] = os.path.dirname(found[0])
    return data


def metadata_for(stage_dir, name, version):
    """The package's own METADATA, found the way pip names the directory.

    Windows puts site-packages at ``runtime/Lib/site-packages``; the macOS and
    Linux runtimes put it under ``runtime/lib/pythonX.Y/``. The search is
    recursive from whichever exists, so both work without a per-platform table.
    """
    site = os.path.join(stage_dir, "runtime", "Lib", "site-packages")
    if not os.path.isdir(site):
        site = os.path.join(stage_dir, "runtime", "lib")
    norm = re.sub(r"[-_.]+", "_", name).lower()
    for entry in glob.glob(os.path.join(site, "**", "*.dist-info"), recursive=True):
        base = os.path.basename(entry)[: -len(".dist-info")]
        if "-" not in base:
            continue
        dist, _, dver = base.rpartition("-")
        if re.sub(r"[-_.]+", "_", dist).lower() == norm and dver == version:
            path = os.path.join(entry, "METADATA")
            if os.path.isfile(path):
                with io.open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()
    return ""


def field(text, key):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(key), text, re.M)
    return m.group(1).strip() if m else ""


def licence_of(text):
    """Licence id, preferring the modern expression field, then the classifier.

    A ``License:`` value is free text — anything from "MIT" to three paragraphs
    of a BSD notice — so a long one is reported as the classifier or as
    NOASSERTION rather than pasted into the document.
    """
    expr = field(text, "License-Expression")
    if expr:
        return expr
    classifiers = re.findall(r"^Classifier: License :: (.+)$", text, re.M)
    for c in classifiers:
        tail = c.split(" :: ")[-1].strip()
        if tail and tail != "OSI Approved":
            return SPDX.get(tail, tail)
    raw = field(text, "License")
    if raw and len(raw) <= 40:
        # Same normalisation as the classifiers: protobuf writes "3-Clause BSD
        # License" here and nowhere else, and that is not an SPDX id.
        return SPDX.get(raw, raw)
    return "NOASSERTION"


def component(name, version, licence, description, scope="required",
              ctype="library"):
    node = {
        "type": ctype,
        "bom-ref": "pkg:pypi/%s@%s" % (name.lower(), version),
        "name": name,
        "version": version,
        "purl": "pkg:pypi/%s@%s" % (name.lower(), version),
        "scope": scope,
    }
    if description:
        node["description"] = description
    node["licenses"] = [licence_node(licence)]
    return node


def licence_node(value):
    """CycloneDX is strict about which of the three shapes a licence takes.

    ``id`` must be an SPDX identifier, a compound like "MPL-2.0 AND MIT" is an
    ``expression``, and anything else — a classifier phrase, an unknown name —
    has to go in ``name`` or a validator rejects the document.
    """
    if not value or value == "NOASSERTION":
        return {"license": {"name": "NOASSERTION"}}
    if re.search(r"\s(AND|OR|WITH)\s", value):
        return {"expression": value}
    if re.match(r"^[A-Za-z0-9.+-]+$", value):
        return {"license": {"id": value}}
    return {"license": {"name": value}}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build(target="", stage="", out=""):
    """Write the SBOM. Returns (path, component_count, target)."""
    man = read_manifest(target, stage)
    version = man.get("version", "0.0.0")
    target = man.get("target", target or "unknown")
    stage_dir = man["_dir"]

    comps = []
    for pin in sorted(man.get("packages", []),
                      key=lambda p: p.split("==")[0].lower()):
        name, _, ver = pin.partition("==")
        meta = metadata_for(stage_dir, name, ver)
        desc = ROLE.get(name.lower()) or field(meta, "Summary")
        comps.append(component(name, ver, licence_of(meta), desc))

    # The interpreter: shipped, and invisible to any pip-derived tool.
    py = man.get("python", "")
    if py:
        comps.append({
            "type": "application",
            "bom-ref": "pkg:generic/cpython@%s" % py,
            "name": "CPython",
            "version": py,
            "purl": "pkg:generic/cpython@%s" % py,
            "scope": "required",
            "description": "Interpreter shipped inside the installer "
                           "(python-build-standalone %s), so no Python is "
                           "needed on the machine." % man.get("python_build", ""),
            "licenses": [licence_node("PSF-2.0")],
        })

    for name, ver, lic, desc in OPTIONAL:
        comps.append(component(name, ver, lic, desc, scope="optional"))

    props = [{"name": "vlocalhost:target", "value": target},
             {"name": "vlocalhost:bundled-packages",
              "value": str(len(man.get("packages", [])))}]

    # Bind the document to the file a user actually downloads.
    for pattern in INSTALLER_PATTERNS:
        for art in sorted(glob.glob(os.path.join(CORE, "dist", pattern))):
            base = os.path.basename(art)
            # dist/ can hold several targets' output at once on a dev machine.
            if version not in base or (target != "unknown" and target not in base):
                continue
            # One property per artifact, in the same "<hash>  <file>" shape the
            # published .sha256 files use. Emitting the name and the digest as
            # two separate properties meant a release with both an .exe and a
            # .zip produced four properties under two repeated names, and any
            # consumer reading them into a mapping kept one artifact and
            # silently dropped the other.
            props.append({"name": "vlocalhost:artifact",
                          "value": "%s  %s" % (sha256_of(art), base)})

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:%s" % uuid.uuid5(NS, "%s/%s" % (version, target)),
        "version": 1,
        "metadata": {
            "timestamp": date.today().isoformat() + "T00:00:00Z",
            "tools": {"components": [{"type": "application",
                                      "name": "make_sbom.py",
                                      "version": "1.1"}]},
            "component": {
                "type": "application",
                "bom-ref": "pkg:generic/vlocalhost@%s" % version,
                "name": "vlocalhost",
                "version": version,
                "description": "On-device meeting notes: local transcription "
                               "and local summaries, no audio uploaded.",
                "licenses": [licence_node("Apache-2.0")],
            },
            "properties": props,
        },
        "components": comps,
    }

    out = out or os.path.join(CORE, "dist",
                              "vlocalhost-%s-%s.cyclonedx.json" % (version, target))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(bom, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return out, comps, version, target


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="",
                    help="bundle target, e.g. windows-x64 or macos-arm64")
    ap.add_argument("--stage", default="",
                    help="staged bundle directory, if not build/<target>")
    ap.add_argument("--out", default="",
                    help="where to write (default dist/vlocalhost-<v>-<target>"
                         ".cyclonedx.json)")
    args = ap.parse_args()

    out, comps, version, target = build(args.target, args.stage, args.out)
    shipped = len([c for c in comps if c.get("scope") == "required"])
    print("%s — vlocalhost %s (%s), %d shipped components (+%d optional)"
          % (os.path.relpath(out, CORE), version, target, shipped,
             len(comps) - shipped))


if __name__ == "__main__":
    main()
