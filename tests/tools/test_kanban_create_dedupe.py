"""Follow-up dedupe on kanban_create (#126).

Workers filed three duplicate cards for one already-fixed blocker. kanban_create
now returns an advisory ``possible_duplicates`` list (same board, title token
overlap over 0.6, created or archived in the last 24 h, excluding the worker's
own card) and defaults an omitted/blank assignee to a dispatcher worker's own
profile. These tests pin those contracts against a temp board.
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Dispatcher-worker context: isolated HERMES_HOME, own claimed card,
    HERMES_KANBAN_TASK pointing at it (mirrors test_kanban_tools.worker_env)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _create_card(title, assignee="evo"):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.create_task(conn, title=title, assignee=assignee)
    finally:
        conn.close()


def _age_task_and_archive_event(task_id, seconds):
    """Stamp a card (and its archived event, if any) ``seconds`` into the past."""
    from hermes_cli import kanban_db_connect as kbc
    stamp = int(time.time()) - seconds
    conn = kbc.connect()
    try:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (stamp, task_id))
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'archived'",
            (stamp, task_id))
        conn.commit()
    finally:
        conn.close()


def _get_task(task_id):
    """Fetch a task, asserting it exists (a missing card is a test failure)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None, f"task {task_id} missing"
        return task
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# possible_duplicates
# ---------------------------------------------------------------------------

def test_create_flags_recent_same_subject_card(worker_env):
    """(a) An overlapping open card comes back in possible_duplicates and the
    create still succeeds (the list is advisory)."""
    from tools import kanban_tools as kt
    existing = _create_card("Fix gitleaks gate in test_notify_needs")

    d = json.loads(kt._handle_create({
        "title": "Fix gitleaks gate in test_notify_needs",
        "assignee": "peer",
    }))
    assert d["ok"] is True
    assert d["task_id"] and d["task_id"] != existing
    assert [c["task_id"] for c in d["possible_duplicates"]] == [existing]
    assert d["possible_duplicates"][0]["title"] == "Fix gitleaks gate in test_notify_needs"
    assert d["possible_duplicates"][0]["status"] == "ready"
    # Advisory only: the new card exists too.
    assert _get_task(d["task_id"]) is not None


def test_create_unrelated_title_has_no_duplicates(worker_env):
    """(b) An unrelated title returns an empty list."""
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({
        "title": "Rotate the cron certificates",
        "assignee": "peer",
    }))
    assert d["ok"] is True
    assert d["possible_duplicates"] == []


def test_create_flags_card_archived_an_hour_ago(worker_env):
    """(c) A same-subject card archived an hour ago is flagged."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    gone = _create_card("Fix gitleaks gate in test_notify_needs")
    conn = kbc.connect()
    try:
        kb.archive_task(conn, gone)
    finally:
        conn.close()
    _age_task_and_archive_event(gone, 3600)

    d = json.loads(kt._handle_create({
        "title": "Fix gitleaks gate in test_notify_needs",
        "assignee": "peer",
    }))
    assert d["ok"] is True
    assert [c["task_id"] for c in d["possible_duplicates"]] == [gone]
    assert d["possible_duplicates"][0]["status"] == "archived"


def test_create_ignores_card_archived_two_days_ago(worker_env):
    """(c) A same-subject card archived two days ago is outside the window."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    gone = _create_card("Fix gitleaks gate in test_notify_needs")
    conn = kbc.connect()
    try:
        kb.archive_task(conn, gone)
    finally:
        conn.close()
    _age_task_and_archive_event(gone, 2 * 24 * 3600)

    d = json.loads(kt._handle_create({
        "title": "Fix gitleaks gate in test_notify_needs",
        "assignee": "peer",
    }))
    assert d["ok"] is True
    assert d["possible_duplicates"] == []


def test_create_excludes_workers_own_card(worker_env):
    """The creator's own task never counts as a duplicate of itself."""
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({
        "title": "worker-test",  # identical to the fixture's own card title
        "assignee": "peer",
    }))
    assert d["ok"] is True
    assert worker_env not in [c["task_id"] for c in d["possible_duplicates"]]


# ---------------------------------------------------------------------------
# Threshold edge (d)
# ---------------------------------------------------------------------------

def test_title_overlap_threshold_boundary():
    """The overlap score itself: 3 shared of 5 union is exactly 0.6 (not over),
    4 of 6 is ~0.667 (over)."""
    from tools.kanban_tools import DUPLICATE_TITLE_OVERLAP, _title_overlap
    exact = _title_overlap("alpha beta gamma delta", "alpha beta gamma epsilon")
    above = _title_overlap("alpha beta gamma delta", "alpha beta gamma delta epsilon zeta")
    assert exact == pytest.approx(DUPLICATE_TITLE_OVERLAP)
    assert not exact > DUPLICATE_TITLE_OVERLAP  # at the threshold: not a duplicate
    assert above == pytest.approx(4 / 6)
    assert above > DUPLICATE_TITLE_OVERLAP


def test_create_at_threshold_not_flagged_just_above_flagged(worker_env):
    """(d) Through the real handler: an exact-0.6 overlap is not flagged; a
    just-over-0.6 overlap is."""
    from tools import kanban_tools as kt
    base = _create_card("alpha beta gamma delta")

    at_threshold = json.loads(kt._handle_create({
        "title": "alpha beta gamma epsilon", "assignee": "peer"}))
    assert at_threshold["ok"] is True
    assert at_threshold["possible_duplicates"] == []

    just_above = json.loads(kt._handle_create({
        "title": "alpha beta gamma delta epsilon zeta", "assignee": "peer"}))
    assert just_above["ok"] is True
    assert base in [c["task_id"] for c in just_above["possible_duplicates"]]


# ---------------------------------------------------------------------------
# Assignee default (#126: no paid frontier lane by default)
# ---------------------------------------------------------------------------

def test_create_defaults_blank_assignee_to_worker_profile(worker_env):
    """A dispatcher worker omitting (or blanking) the assignee gets its own
    profile, never an error and never a paid frontier lane."""
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "Follow-up work"}))
    assert d["ok"] is True
    assert _get_task(d["task_id"]).assignee == "test-worker"

    blank = json.loads(kt._handle_create({"title": "More follow-up", "assignee": "  "}))
    assert blank["ok"] is True
    assert _get_task(blank["task_id"]).assignee == "test-worker"


def test_create_explicit_assignee_still_wins(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({
        "title": "Follow-up work", "assignee": "peer"}))
    assert d["ok"] is True
    assert _get_task(d["task_id"]).assignee == "peer"


def test_create_still_requires_assignee_for_orchestrators(monkeypatch, worker_env):
    """Orchestrator fan-out (no HERMES_KANBAN_TASK) keeps the hard requirement:
    the default must not silently reassign orchestrated children."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = kt._handle_create({"title": "Orchestrated child"})
    assert "assignee is required" in out


# ---------------------------------------------------------------------------
# Prompt rule 6 carries the dedupe ideas (invariant, not wording)
# ---------------------------------------------------------------------------

def test_followup_rule_carries_dedupe_and_lane_default():
    """Rule 6 must tell workers to (1) re-check the remote before filing a
    follow-up, (2) look for an existing card and link/comment instead, and
    (3) default the assignee to their own profile, not a paid lane."""
    from agent.prompt_builder import KANBAN_GUIDANCE
    assert "git fetch" in KANBAN_GUIDANCE          # remote re-check
    assert "origin/" in KANBAN_GUIDANCE            # ...against the base branch
    assert "possible_duplicates" in KANBAN_GUIDANCE  # board search signal
    assert "kanban_link" in KANBAN_GUIDANCE        # link instead of filing
    assert "own profile" in KANBAN_GUIDANCE        # assignee default
