#!/usr/bin/env python3
"""Regenerate the application icons from the one master image.

    python tools/make_app_icons.py

Writes, from ``tools/icon-master-1024.png``:

    assets/vlocalhost.png    512    Linux .desktop, and the generic fallback
    assets/vlocalhost.ico    7 sizes  Windows: installer, shortcuts, uninstall entry
    assets/vlocalhost.icns   7 sizes  macOS: the .app bundle icon

Why this file exists at all: the icons used to be three hand-made binaries with
no source and no generator. When the brand changed from the oscilloscope mark to
Sunbeam, the site, the decks and the README were all updated and **the app icons
were not** -- because nothing tied them to the brand, so nothing failed when they
drifted. A build step is what makes that drift visible.

The master is a PNG rather than the SVG deliberately: rasterising the SVG needs a
renderer that is not a dependency of this project, and an icon that silently fails
to build is worse than one checked in. If the brand changes, re-export the master
from ``3-brand/logo/vlocalhost-appicon.svg`` at 1024 and re-run this.
"""

from __future__ import annotations

import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")
# Lives in tools/, not assets/: assets/ ships inside every install (see
# build_bundle.INCLUDE_DIRS) and a build-time source file has no business
# being delivered to users.
MASTER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "icon-master-1024.png")

# Windows wants the small sizes present or it scales 256 down badly in the
# taskbar. macOS wants the powers of two; anything missing is interpolated.
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]
PNG_SIZE = 512


def load_master() -> Image.Image:
    if not os.path.exists(MASTER):
        sys.exit(f"missing master icon: {MASTER}\n"
                 "Export 3-brand/logo/vlocalhost-appicon.svg at 1024x1024 to that path.")
    im = Image.open(MASTER).convert("RGBA")
    if im.size != (1024, 1024):
        sys.exit(f"master must be 1024x1024, found {im.size[0]}x{im.size[1]}")
    return im


def main() -> int:
    master = load_master()

    png_path = os.path.join(ASSETS, "vlocalhost.png")
    master.resize((PNG_SIZE, PNG_SIZE), Image.LANCZOS).save(png_path)
    print(f"  wrote {os.path.basename(png_path)}  {PNG_SIZE}px")

    ico_path = os.path.join(ASSETS, "vlocalhost.ico")
    master.save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  wrote {os.path.basename(ico_path)}  {len(ICO_SIZES)} sizes "
          f"({ICO_SIZES[0]}-{ICO_SIZES[-1]})")

    icns_path = os.path.join(ASSETS, "vlocalhost.icns")
    # Pillow builds the container from the largest image it is given and derives
    # the rest, so hand it the master rather than a downscale.
    master.save(icns_path, format="ICNS")
    print(f"  wrote {os.path.basename(icns_path)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
