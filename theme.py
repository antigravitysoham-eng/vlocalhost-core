"""The brand tokens the window is painted with.

Pulled out of :mod:`gui` so that a second widget module can use them without
importing the window that imports it. Values only -- no tkinter, no styles, no
widgets. :meth:`gui.App._style` remains the one place ttk styles are defined.
"""

import platform

# --- Brand tokens ---------------------------------------------------------
INK = "#090C12"       # window ground
PANEL = "#0E1220"     # cards / surfaces
EDGE = "#1C2333"      # hairline borders
AMBER = "#FFB43D"     # the one accent
AMBER_DEEP = "#E08A17"
CYAN = "#38E1CE"      # "on-device / live" — semantic only
PAPER = "#EAEEF4"
MUTED = "#7E8AA0"
DANGER = "#E8624F"

#: One step up from PANEL, for a surface sitting on a surface — a notes card
#: inside the notes column. Anything more than one step and the window starts
#: to look like a stack of boxes rather than a page.
CARD = "#141A2B"

#: The planes of the Spatial direction (3-brand/app-ui-spatial-system.html),
#: rendered flat. That design carries hierarchy in Z — near is now, far is
#: archive, blur is settled — and says so in one line: "depth is a metaphor for
#: the archive, not a rendering of it". Tk has no perspective, no translateZ
#: and no blur, so the metaphor is carried by ground tone instead: the nearest
#: plane is lightest, each one behind it a step darker.
#:
#: This is the design's own reduced-motion state, which it defines as the test
#: of the whole thing -- "if the flat fallback is not usable on its own, the
#: design has failed" -- rather than a compromise invented here.
#: Tk takes no alpha in a colour, so these are the blend already done.
PLANE_NEAR = "#1D2437"   # nearest, most settled: what was decided
PLANE_MID = "#171E2F"    # owed, and what is still open
PLANE_FAR = "#121828"    # furthest: the summary, and what was merely discussed

#: "Live · 0 bytes out". Mint is the live channel in the Spatial palette and
#: means exactly one thing, so nothing else may borrow it.
MINT = "#38E1CE"

_WINDOWS = platform.system() == "Windows"

MONO = ("Cascadia Code", 9) if _WINDOWS else ("Menlo", 11)
BODY = ("Segoe UI", 10) if _WINDOWS else ("Helvetica", 12)
TITLE = ("Segoe UI", 20, "bold") if _WINDOWS else ("Helvetica", 22, "bold")

__all__ = ["AMBER", "AMBER_DEEP", "BODY", "CARD", "CYAN", "DANGER", "EDGE",
           "INK", "MINT", "MONO", "MUTED", "PANEL", "PAPER", "PLANE_FAR",
           "PLANE_MID", "PLANE_NEAR", "TITLE"]
