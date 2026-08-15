#!/usr/bin/env python3
"""Wrap a built bundle into Vlocalhost.app and a drag-to-install DMG.

    python tools/build_bundle.py --target macos-arm64
    python tools/build_macos_app.py --target macos-arm64

macOS has no "choose your install location" wizard by convention — the Finder
is the wizard, and the answer is /Applications or anywhere else the user drops
the icon. So the app *is* the bundle: everything lives inside the .app, which
can be moved to any volume and still runs, because every path inside it is
resolved relative to the bundle.

**Without an Apple Developer account this app is only ad-hoc signed, and
recent macOS will refuse to open it.** Gatekeeper blocks anything that is not
notarized, and the old right-click-to-open escape hatch is gone — the user has
to go to System Settings › Privacy & Security and approve it explicitly. The
ad-hoc signature does not change that; it only means the bundle is internally
consistent, so the refusal is the ordinary "unidentified developer" one rather
than a claim that the download is damaged. :func:`sign_and_notarize` signs for
real, and notarizes, when the credentials are present in the environment, and
says loudly when they are not.
"""

import argparse
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from version import __version__ as APP_VERSION  # noqa: E402

APP_NAME = "Vlocalhost"
BUNDLE_ID = "ai.vlocalhost.app"


def log(message):
    print(f"[macos] {message}", flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_app(stage, out_dir):
    """Assemble the .app around an already-built bundle."""
    app = os.path.join(out_dir, f"{APP_NAME}.app")
    shutil.rmtree(app, ignore_errors=True)
    macos = os.path.join(app, "Contents", "MacOS")
    resources = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)

    log("copying the bundle into the app")
    shutil.copytree(os.path.join(stage, "runtime"),
                    os.path.join(resources, "runtime"), symlinks=True)
    shutil.copytree(os.path.join(stage, "app"),
                    os.path.join(resources, "app"), symlinks=True)
    manifest = os.path.join(stage, "manifest.json")
    if os.path.isfile(manifest):
        shutil.copy2(manifest, resources)

    icon = os.path.join(ROOT, "assets", "vlocalhost.icns")
    has_icon = os.path.isfile(icon)
    if has_icon:
        shutil.copy2(icon, os.path.join(resources, "vlocalhost.icns"))

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": "Vlocalhost.AI",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "vlocalhost",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # Without this key macOS kills the process the moment it opens the
        # microphone, with no dialog and no log line anyone can act on.
        "NSMicrophoneUsageDescription":
            "Vlocalhost transcribes your meetings on this device. Audio never "
            "leaves your Mac.",
    }
    if has_icon:
        info["CFBundleIconFile"] = "vlocalhost.icns"
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    launcher = os.path.join(macos, "vlocalhost")
    with open(launcher, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            '#!/bin/sh\n'
            '# Resolve everything relative to the bundle so the app can be\n'
            '# moved to any volume and still find its own interpreter.\n'
            'HERE="$(cd "$(dirname "$0")/../Resources" && pwd)"\n'
            'exec "$HERE/runtime/bin/python3" "$HERE/app/vlocalhost.py" "$@"\n')
    os.chmod(launcher, 0o755)

    python = os.path.join(resources, "runtime", "bin", "python3")
    if os.path.exists(python):
        os.chmod(python, 0o755)
    log(f"built {os.path.basename(app)}")
    return app


#: Mach-O magic numbers, thin and fat, both byte orders. Everything carrying
#: one has to be signed in its own right; the rest of the bundle is data that
#: the enclosing signature already covers.
_MACHO_MAGIC = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",   # 32-bit
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",   # 64-bit
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",   # universal
}


def _is_macho(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(4) in _MACHO_MAGIC
    except OSError:
        return False


def _inner_binaries(app):
    """Every Mach-O inside the bundle, deepest first.

    codesign seals what it finds at the moment it runs, so a nested binary
    signed after its container invalidates that container. Sorting by depth
    means each thing is signed before anything that encloses it.
    """
    found = []
    for directory, _, files in os.walk(app):
        for name in files:
            path = os.path.join(directory, name)
            if _is_macho(path):
                found.append(path)
    return sorted(found, key=lambda p: p.count(os.sep), reverse=True)


def sign_and_notarize(app):
    """Sign, notarize and staple — when the credentials are there.

    Expects, from the CI secrets:
        MACOS_SIGN_IDENTITY   "Developer ID Application: Name (TEAMID)"
        AC_API_KEY_ID / AC_API_ISSUER / AC_API_KEY_PATH   App Store Connect key

    With no identity the bundle is still signed, ad hoc. That buys no trust —
    Gatekeeper still refuses it on another Mac — but it does keep the runtime
    we ship internally consistent, so the failure is the honest "unidentified
    developer" prompt rather than "the application is damaged".
    """
    identity = os.environ.get("MACOS_SIGN_IDENTITY")
    adhoc = not identity
    if adhoc:
        log("NOT SIGNED — no MACOS_SIGN_IDENTITY in the environment.")
        log("Gatekeeper will refuse to open this build on another Mac.")
        log("Falling back to an ad-hoc signature.")

    # --deep is explicitly not a supported way to sign for notarization: it
    # applies one set of options to nested code that was never described that
    # way, and Apple rejects the result. Sign inside-out instead.
    entitlements = os.path.join(HERE, "macos-entitlements.plist")
    command = ["codesign", "--force", "--sign", identity or "-"]
    if not adhoc:
        # The hardened runtime is required for notarization, and needs the
        # entitlements that let an interpreter run. Imposing it on an ad-hoc
        # build only breaks the bundle for no gain, since it cannot notarize.
        command += ["--timestamp", "--options", "runtime"]
        if os.path.isfile(entitlements):
            command += ["--entitlements", entitlements]

    binaries = _inner_binaries(app)
    log(f"signing {len(binaries)} nested binaries"
        f"{' (ad hoc)' if adhoc else ''}")
    for binary in binaries:
        subprocess.run(command + [binary], check=True,
                       capture_output=True, text=True)
    log("signing the app")
    subprocess.run(command + [app], check=True)
    subprocess.run(["codesign", "--verify", "--strict", "--verbose=2", app],
                   check=True)
    if adhoc:
        return False

    key_id = os.environ.get("AC_API_KEY_ID")
    issuer = os.environ.get("AC_API_ISSUER")
    key_path = os.environ.get("AC_API_KEY_PATH")
    if not (key_id and issuer and key_path):
        log("signed, but NOT notarized — no App Store Connect key.")
        return False

    log("notarizing (this waits on Apple, and can take several minutes)")
    archive = app + ".zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", app, archive],
                   check=True)
    subprocess.run(["xcrun", "notarytool", "submit", archive,
                    "--key", key_path, "--key-id", key_id,
                    "--issuer", issuer, "--wait"], check=True)
    subprocess.run(["xcrun", "stapler", "staple", app], check=True)
    os.remove(archive)
    log("notarized and stapled")
    return True


def build_dmg(app, out_dir, target):
    """A DMG with the app and an Applications alias to drag it onto."""
    staging = os.path.join(out_dir, "dmg")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    # ditto, not copytree: a code signature lives partly in extended
    # attributes, and copytree drops those. The app would ship looking signed
    # and fail verification on the machine that opened it.
    subprocess.run(["ditto", app, os.path.join(staging,
                                               os.path.basename(app))],
                   check=True)
    os.symlink("/Applications", os.path.join(staging, "Applications"))

    dmg = os.path.join(out_dir, f"vlocalhost-{APP_VERSION}-{target}.dmg")
    if os.path.exists(dmg):
        os.remove(dmg)
    log("building the disk image")
    subprocess.run(["hdiutil", "create", "-volname", f"Vlocalhost {APP_VERSION}",
                    "-srcfolder", staging, "-ov", "-format", "UDZO", dmg],
                   check=True)
    shutil.rmtree(staging, ignore_errors=True)

    # The release collects *.sha256, and publishes them as SHA256SUMS. Without
    # this the DMG is the one asset nobody can check, and — because that glob
    # is also how assets are gathered — the one nobody receives.
    digest = sha256(dmg)
    with open(dmg + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(dmg)}\n")

    log(f"done: {os.path.basename(dmg)} "
        f"({os.path.getsize(dmg) / 1_000_000:.0f} MB)")
    log(f"sha256: {digest}")
    return dmg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default="macos-arm64",
                        choices=["macos-arm64", "macos-x64"])
    parser.add_argument("--stage", default="",
                        help="bundle staging dir (default: build/stage-<target>)")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        raise SystemExit("This must run on macOS — it uses codesign and hdiutil.")

    stage = args.stage or os.path.join(ROOT, "build", f"stage-{args.target}")
    if not os.path.isdir(stage):
        raise SystemExit(f"No bundle at {stage}. Run tools/build_bundle.py first.")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    app = build_app(stage, out_dir)
    sign_and_notarize(app)
    build_dmg(app, out_dir, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
