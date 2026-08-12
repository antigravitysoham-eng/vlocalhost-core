#!/usr/bin/env python3
"""Install this repository's git hooks. Run once per clone.

    python tools/install_hooks.py

Hooks live in ``tools/`` so they are version-controlled and reviewable; git
only runs what is in ``.git/hooks``, which is not. This copies one to the
other. Re-run after pulling a change to a hook.
"""

import os
import shutil
import stat
import subprocess
import sys

HOOKS = ("pre-push",)
HERE = os.path.dirname(os.path.abspath(__file__))


def hooks_dir() -> str:
    # --absolute-git-dir, not --git-path: the latter answers relative to the
    # working directory, which is easy to rejoin against the wrong base and
    # quietly install the hook outside the repository.
    out = subprocess.run(["git", "rev-parse", "--absolute-git-dir"],
                         capture_output=True, text=True, cwd=HERE)
    if out.returncode != 0:
        sys.exit("Not a git repository — run this from inside the clone.")
    return os.path.join(out.stdout.strip(), "hooks")


def main() -> int:
    target_dir = hooks_dir()
    os.makedirs(target_dir, exist_ok=True)
    for name in HOOKS:
        src = os.path.join(HERE, name)
        if not os.path.isfile(src):
            print(f"  missing: {src}")
            continue
        dst = os.path.join(target_dir, name)
        shutil.copyfile(src, dst)
        os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        print(f"  installed {name} -> {dst}")
    print("\nDone. Bypass a hook deliberately with: git push --no-verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
