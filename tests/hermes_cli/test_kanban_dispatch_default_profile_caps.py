"""Board-scope dispatch caps come from the default profile's config (#124).

``hermes kanban dispatch`` read its concurrency caps from ``load_config()``,
which resolves the invoking (sticky) profile's config.yaml — so a dispatch run
without ``--profile default`` ignored ``kanban.max_in_progress_by_assignee``
and over-spawned. A board's caps describe the board's shared capacity and must
hold whichever profile runs ``dispatch``. The fix reads the six board-scope
keys from the DEFAULT profile's config, warns when the invoking profile's raw
caps differ, names the cap source in the dry-run summary, and falls back to
the invoking profile's config (with a warning) when the default config.yaml
cannot be parsed.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops


@pytest.fixture
def sticky_profile_home(tmp_path, monkeypatch):
    """A hermes root seen the way issue #124 reproduces it.

    ``HERMES_HOME`` points at the ``evo`` profile (a sticky-profile dispatch),
    while the board and the default profile's config.yaml live at the root.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "evo").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "evo"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


def _write_cfg(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    # Push mtime past any coarse-grained cache resolution.
    future = time.time() + 5 + len(body)
    os.utime(path, (future, future))


def _seed(conn, *, running: int, ready: int, assignee: str = "evo") -> None:
    for i in range(running):
        tid = kb.create_task(conn, title=f"running{i}", assignee=assignee)
        assert kb.claim_task(conn, tid) is not None
    for i in range(ready):
        kb.create_task(conn, title=f"ready{i}", assignee=assignee)


def _dispatch_args():
    return argparse.Namespace(
        dry_run=True, json=False, max=None, max_per_assignee=None,
        failure_limit=kbd.DEFAULT_FAILURE_LIMIT,
    )


def test_dispatch_caps_come_from_default_profile_config(sticky_profile_home, capsys):
    """#124 acceptance: default-profile config caps evo at 2 with 2 evo tasks
    running and 2 ready; a dispatch from the sticky profile (whose config has
    no kanban caps) must defer BOTH ready cards, warn about the differing
    configs, and name the cap source in the dry-run summary."""
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")
    _write_cfg(root / "profiles" / "evo" / "config.yaml", "model: local\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert "Spawned:      0" in out.out, f"expected no spawns, got:\n{out.out}"
    assert out.out.count("Deferred (evo at per-profile cap 2, 2 running):") == 2, (
        f"expected both ready cards deferred, got:\n{out.out}"
    )
    # One-line warning on stderr naming both configs' values.
    assert "profile caps differ from default profile caps" in out.err, out.err
    assert "max_in_progress_by_assignee" in out.err, out.err
    # The dry-run summary names where each cap came from.
    assert "Caps source: default profile config" in out.out, out.out
    assert str(root / "config.yaml") in out.out, out.out


def test_dispatch_falls_back_to_invoking_profile_when_default_unreadable(
    sticky_profile_home, capsys
):
    """A default config.yaml that cannot be parsed must not crash dispatch:
    fall back to the invoking profile's caps (still enforced) and warn."""
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", "kanban: [unclosed\n")
    _write_cfg(
        root / "profiles" / "evo" / "config.yaml",
        "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert "Spawned:      0" in out.out, f"expected no spawns, got:\n{out.out}"
    assert out.out.count("Deferred (evo at per-profile cap 2, 2 running):") == 2, (
        f"fallback caps must still defer both ready cards, got:\n{out.out}"
    )
    assert "unreadable" in out.err, out.err
    assert "falling back to the invoking profile" in out.err, out.err
