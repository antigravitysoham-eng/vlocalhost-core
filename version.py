"""The version number, in one place.

It used to live in four: ``diagnostics.APP_VERSION`` and a hardcoded
``VERSION=`` line in each of the three installer scripts. Four copies of one
fact is three chances to ship a mismatch, and the failure is quiet — the
installer fetches a tag that does not exist yet, or the diagnostic report names
a release the user is not running.

Everything now reads this. The build and the release workflow read it too, so
tagging is the only place a human types a version.
"""

__version__ = "1.1.1"

#: Bumped when the on-disk layout changes in a way :mod:`migrate` must handle.
DATA_LAYOUT = 2
