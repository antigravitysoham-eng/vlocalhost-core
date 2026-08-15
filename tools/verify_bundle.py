#!/usr/bin/env python3
"""Re-check a built bundle against its recorded SHA-256.

Run in CI straight after the build, so a corrupted zip is a red build rather
than a download that fails halfway on somebody's laptop.

    python tools/verify_bundle.py dist

Exits non-zero on the first mismatch, and says which file and both digests.
"""

import hashlib
import os
import sys


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Everything a release hands out and records a digest for. The DMG is as much
#: a download as the zip is, and checking only the zip meant the macOS asset
#: was the one thing shipped unverified.
CHECKED = (".zip", ".dmg")


def main(argv):
    directory = argv[0] if argv else "dist"
    archives = sorted(f for f in os.listdir(directory)
                      if f.endswith(CHECKED))
    if not archives:
        print(f"No {' or '.join(CHECKED)} files in {directory}",
              file=sys.stderr)
        return 1

    failed = 0
    for name in archives:
        archive = os.path.join(directory, name)
        record = archive + ".sha256"
        if not os.path.isfile(record):
            print(f"FAIL  {name}: no .sha256 alongside it")
            failed += 1
            continue
        with open(record, encoding="utf-8") as f:
            recorded = f.read().split()[0]
        computed = sha256(archive)
        size = os.path.getsize(archive) / 1_000_000
        if computed == recorded:
            # ASCII only: a Windows CI console is cp1252 and raises
            # UnicodeEncodeError on anything else, failing a passing build.
            print(f"OK    {name}  {size:.0f} MB  {computed[:16]}...")
        else:
            print(f"FAIL  {name}\n        recorded {recorded}\n"
                  f"        computed {computed}")
            failed += 1

    print(f"\n{len(archives) - failed}/{len(archives)} verified")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
