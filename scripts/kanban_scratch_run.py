#!/usr/bin/env python3
"""Run a throwaway kanban script against a scratch board, never a live one.

WHY THIS EXISTS
---------------
:func:`hermes_cli.kanban_db.kanban_db_path` gives ``HERMES_KANBAN_DB`` strict
precedence over ``HERMES_HOME``. A dispatched worker has ``HERMES_KANBAN_DB``
pinned at the REAL board in its environment, so an ad-hoc repro script run
inside that worker writes to the live board **even when it carefully sets its
own** ``HERMES_HOME`` **to a tempdir**. That is not hypothetical: during the
issue #5 workspace-lease work an ad-hoc red/green control script wrote four
synthetic rows onto the live ``hermes-agent-overnight`` board exactly this
way, and independent review reproduced the cause.

``tests/conftest.py`` already deletes those variables and installs a
fail-closed write guard, which is why the committed pytest suite is safe. Ad
hoc scripts get no such protection. This module is that protection, made
reusable: run any script through it and the kanban environment is scrubbed
and re-pinned at a fresh scratch root before the script's first import.

USAGE
-----
    python3 scripts/kanban_scratch_run.py path/to/repro.py [args...]
    python3 scripts/kanban_scratch_run.py --keep path/to/repro.py

The sandbox root is a fresh temp directory, removed on exit unless
``--keep`` is passed. The child sees ``HERMES_HOME``, ``TMPDIR`` and
``HERMES_KANBAN_SCRATCH_ROOT`` pointing into it, and every inherited
``HERMES_KANBAN_*`` pin removed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Optional, Sequence

# Every variable that can steer kanban path resolution away from HERMES_HOME,
# plus the per-task worker identity pins. Kept as an explicit prefix sweep
# rather than a hand-maintained list so a NEW HERMES_KANBAN_* knob added to
# kanban_db.py is scrubbed on the day it lands instead of the day someone
# remembers to update this file. ``tests/test_kanban_scratch_run.py`` asserts
# the sweep covers every such name actually read by kanban_db.
KANBAN_ENV_PREFIX = "HERMES_KANBAN_"

# Not prefixed, but still redirects resolution.
EXTRA_SCRUBBED = ("HERMES_HOME", "HERMES_PROFILE")


def scrubbed_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    home: Optional[str] = None,
    tmpdir: Optional[str] = None,
) -> dict[str, str]:
    """Return a copy of ``base`` with every kanban pin removed.

    Pure and side-effect free so it can be asserted on directly. When
    ``home`` is given it is re-pinned as ``HERMES_HOME`` *after* the scrub,
    so the caller's scratch root is the only thing left steering resolution.
    """
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name.startswith(KANBAN_ENV_PREFIX) or name in EXTRA_SCRUBBED:
            del env[name]
    if home is not None:
        env["HERMES_HOME"] = str(home)
        env["HERMES_KANBAN_SCRATCH_ROOT"] = str(home)
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def run(argv: Sequence[str], *, keep: bool = False) -> int:
    """Execute ``argv`` inside a fresh scratch kanban sandbox."""
    root = Path(tempfile.mkdtemp(prefix="kanban-scratch-"))
    home = root / ".hermes"
    tmp = root / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = scrubbed_env(home=str(home), tmpdir=str(tmp))
    # HERMES_KANBAN_SCRATCH_ROOT survived the scrub above only because it is
    # re-added afterwards; re-point it at the sandbox root, not just home.
    env["HERMES_KANBAN_SCRATCH_ROOT"] = str(root)
    print(f"kanban_scratch_run: sandbox {root}", file=sys.stderr)
    try:
        return subprocess.call(list(argv), env=env, cwd=os.getcwd())
    finally:
        if keep:
            print(f"kanban_scratch_run: kept {root}", file=sys.stderr)
        else:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    keep = False
    if args and args[0] == "--keep":
        keep = True
        args = args[1:]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    if args[0].endswith(".py"):
        args = [sys.executable, *args]
    return run(args, keep=keep)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
