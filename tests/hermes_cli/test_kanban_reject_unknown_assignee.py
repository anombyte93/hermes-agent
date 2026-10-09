"""A dispatchable card must name a profile that exists, or it is stranded for ever.

Measured 2026-09-23 across 176 boards: 32 open cards were assigned to names that are not
Hermes profiles (`developer` x10, `molly-app` x10, `astra-executor`, `w`, `reviewer`...).
The dispatcher correctly found nothing to do for them, while the built-in diagnostic said
"Ready for 523.9h with no worker" and nobody read it. The place to refuse is create.

Enforcement is ``kanban.require_known_assignee`` (default off): upstream fixtures assign
to ``worker``/``alice`` under a HOME that holds other profiles, and they must keep working.
Human-parked cards (``initial_status="blocked"``, ``triage=True``) are exempt even when it
is on, so marker assignees like ``hayden`` keep working; so is a HOME with no profiles.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "profiles" / "alpha" / "config.yaml").write_text("model: x\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def enforcing(home):
    (home / "config.yaml").write_text("kanban:\n  require_known_assignee: true\n")
    return home


@pytest.fixture
def conn(home, tmp_path):
    c = connect(tmp_path / "kanban.db")
    yield c
    c.close()


def test_enforcing_refuses_a_dispatchable_card_for_an_unknown_assignee_by_name(enforcing, conn):
    with pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="FleetLeaseFlow: My Deals", assignee="developer")
    msg = str(exc.value)
    assert "developer" in msg
    assert "alpha" in msg, "the refusal must name the profiles that DO exist"


def test_default_is_off_so_upstream_fixtures_keep_working(conn):
    tid = kb.create_task(conn, title="route completion", assignee="worker")
    assert kb.get_task(conn, tid).assignee == "worker"


def test_the_message_is_available_even_when_not_enforcing(home):
    assert "worker" in kb.unknown_assignee_message("worker")
    assert kb.unknown_assignee_message("alpha") is None
    assert kb.unknown_assignee_message("default") is None


def test_enforcing_accepts_a_real_profile(enforcing, conn):
    tid = kb.create_task(conn, title="ok", assignee="alpha")
    assert kb.get_task(conn, tid).assignee == "alpha"


def test_enforcing_lets_a_human_parked_card_carry_a_marker_assignee(enforcing, conn):
    tid = kb.create_task(conn, title="decide", assignee="hayden", initial_status="blocked",
                         block_reason="waiting on a human decision")
    assert kb.get_task(conn, tid).status == "blocked"


def test_enforcing_lets_a_triage_card_carry_a_marker_assignee(enforcing, conn):
    tid = kb.create_task(conn, title="look", assignee="hayden", triage=True)
    assert kb.get_task(conn, tid).status == "triage"


def test_no_profiles_on_disk_means_nothing_to_check_against(tmp_path, monkeypatch):
    bare = tmp_path / "bare"; bare.mkdir()
    (bare / "config.yaml").write_text("kanban:\n  require_known_assignee: true\n")
    monkeypatch.setenv("HERMES_HOME", str(bare))
    c = connect(tmp_path / "bare.db")
    try:
        assert kb.create_task(c, title="x", assignee="anyone")
    finally:
        c.close()


# --- CLI surface (`hermes kanban create`, via the same run_slash entry the gateway uses) ---

def _cli_board(home, tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()


def test_cli_create_refuses_unknown_assignee_when_enforcing(enforcing, tmp_path, monkeypatch):
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_db_connect as kbc
    _cli_board(enforcing, tmp_path, monkeypatch)
    out = kc.run_slash("create 'stranded card' --assignee developer")
    assert "developer" in out and "alpha" in out
    with kbc.connect() as c:
        assert not [t for t in kb.list_tasks(c) if t.title == "stranded card"]


def test_cli_create_warns_but_creates_when_not_enforcing(home, tmp_path, monkeypatch):
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_db_connect as kbc
    _cli_board(home, tmp_path, monkeypatch)
    out = kc.run_slash("create 'warned card' --assignee developer")
    assert "not a Hermes profile" in out
    with kbc.connect() as c:
        assert [t for t in kb.list_tasks(c) if t.title == "warned card"]
