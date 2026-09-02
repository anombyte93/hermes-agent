"""Controls for scripts/kanban_scratch_run.py — live-board pollution guard.

Independent review of the issue #5 workspace lease (commit 83b952ee60) found
four synthetic rows written onto the LIVE ``hermes-agent-overnight`` board by
an ad-hoc red/green repro script. The committed pytest suite was NOT the
culprit — ``tests/conftest.py`` deletes the kanban pins and installs a
fail-closed write guard. The culprit was process discipline: the script set
its own ``HERMES_HOME`` to a tempdir, but
:func:`hermes_cli.kanban_db.kanban_db_path` gives ``HERMES_KANBAN_DB`` strict
precedence over ``HERMES_HOME``, and the script inherited that variable from
the dispatched worker that ran it.

These controls pin the fix so the recurrence is caught in CI rather than in
review:

* the scrub removes EVERY ``HERMES_KANBAN_*`` name kanban_db actually reads,
  discovered from the source rather than hand-listed;
* a script run through the helper with ``HERMES_KANBAN_DB`` pinned at a
  canary DB leaves that canary byte-identical (the causal reproduction from
  the review, inverted into a passing assertion);
* the same script run WITHOUT the helper does write to the canary, so the
  control is proven to be able to fail.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "kanban_scratch_run.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import kanban_scratch_run  # noqa: E402


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_scrub_removes_every_kanban_pin_kanban_db_actually_reads():
    """The prefix sweep must cover every HERMES_KANBAN_* name in kanban_db.

    Discovered from the source, so a newly-added knob fails this control on
    the day it lands rather than silently escaping the scrub.
    """
    source = (REPO_ROOT / "hermes_cli" / "kanban_db.py").read_text()
    names = set(re.findall(r"HERMES_KANBAN_[A-Z0-9_]+", source))
    assert names, "expected kanban_db.py to reference kanban env vars"

    polluted = {name: "/live/board" for name in names}
    polluted["HERMES_HOME"] = "/live/home"
    cleaned = kanban_scratch_run.scrubbed_env(polluted, home="/scratch/home")

    leaked = {
        n for n in names
        if n in cleaned and cleaned[n] != "/scratch/home"
        and n != "HERMES_KANBAN_SCRATCH_ROOT"
    }
    assert leaked == set(), f"kanban pins survived the scrub: {sorted(leaked)}"
    assert cleaned["HERMES_HOME"] == "/scratch/home"


def test_scrub_does_not_disturb_unrelated_environment():
    """Bounded blast radius: only kanban/home pins are touched."""
    base = {
        "PATH": "/usr/bin",
        "HERMES_KANBAN_DB": "/live/board/kanban.db",
        "HERMES_PROVIDER": "anthropic",
        "HOME": "/home/someone",
    }
    cleaned = kanban_scratch_run.scrubbed_env(base, home="/scratch")
    assert cleaned["PATH"] == "/usr/bin"
    assert cleaned["HERMES_PROVIDER"] == "anthropic"
    assert cleaned["HOME"] == "/home/someone"
    assert "HERMES_KANBAN_DB" not in cleaned


@pytest.fixture()
def canary(tmp_path):
    """A byte-identical stand-in for a live board DB, plus a writer script."""
    import sqlite3

    db = tmp_path / "canary.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO tasks VALUES ('t_preexisting')")
    conn.commit()
    conn.close()

    writer = tmp_path / "writer.py"
    writer.write_text(
        "import os, sqlite3, sys\n"
        # Mirrors kanban_db_path()'s precedence: HERMES_KANBAN_DB wins over
        # HERMES_HOME. This is the exact behaviour that polluted the live
        # board, reproduced in miniature so the control is honest.
        "target = os.environ.get('HERMES_KANBAN_DB', '').strip()\n"
        "if not target:\n"
        "    target = os.path.join(os.environ['HERMES_HOME'], 'kanban.db')\n"
        "    os.makedirs(os.path.dirname(target), exist_ok=True)\n"
        "conn = sqlite3.connect(target)\n"
        "conn.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY)')\n"
        "conn.execute(\"INSERT OR REPLACE INTO tasks VALUES ('t_synthetic')\")\n"
        "conn.commit()\n"
        "conn.close()\n"
        "sys.stdout.write(target)\n"
    )
    return db, writer


def test_helper_prevents_the_live_board_write(canary, tmp_path):
    """GREEN: through the helper, the pinned canary is untouched."""
    db, writer = canary
    before = _md5(db)

    env = dict(os.environ)
    env["HERMES_KANBAN_DB"] = str(db)  # the worker-inherited pin
    env.pop("PYTHONPATH", None)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(writer)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    assert _md5(db) == before, (
        "kanban_scratch_run did NOT isolate the script: the canary DB "
        "changed, which is the live-board pollution shape from review"
    )
    # And it genuinely ran — it wrote somewhere, just not the canary.
    assert proc.stdout.strip()
    assert str(db) not in proc.stdout


def test_control_can_fail_without_the_helper(canary):
    """RED: the same script run directly DOES write to the canary.

    A guard that has never been seen to fail is not a guard.
    """
    db, writer = canary
    before = _md5(db)

    env = dict(os.environ)
    env["HERMES_KANBAN_DB"] = str(db)

    proc = subprocess.run(
        [sys.executable, str(writer)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert _md5(db) != before, (
        "expected the unprotected script to pollute the canary; if this "
        "fails the isolation control above proves nothing"
    )
