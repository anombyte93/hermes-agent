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


def _age_task_and_archive_event(task_id, created_ago, archived_ago=None):
    """Stamp a card ``created_ago`` seconds into the past and its archived event
    (if any) ``archived_ago`` seconds into the past (default: same as created)."""
    from hermes_cli import kanban_db_connect as kbc
    now = int(time.time())
    archived_ago = created_ago if archived_ago is None else archived_ago
    conn = kbc.connect()
    try:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - created_ago, task_id))
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'archived'",
            (now - archived_ago, task_id))
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
    """(c) A same-subject card archived an hour ago is flagged, even though it was
    CREATED three days ago: freshness is the archived event, not created_at."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    gone = _create_card("Fix gitleaks gate in test_notify_needs")
    conn = kbc.connect()
    try:
        kb.archive_task(conn, gone)
    finally:
        conn.close()
    _age_task_and_archive_event(gone, created_ago=3 * 24 * 3600, archived_ago=3600)

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
    _age_task_and_archive_event(gone, created_ago=3 * 24 * 3600,
                                archived_ago=2 * 24 * 3600)

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


def test_own_task_scope_never_reveals_inaccessible_peer(worker_env, tmp_path):
    """Under ``kanban.worker_scope: own_task`` a worker may not read any other card,
    so kanban_create must not leak one through possible_duplicates: the list is
    always empty, and creation still succeeds."""
    from tools import kanban_tools as kt
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "kanban:\n  worker_scope: own_task\n", encoding="utf-8")
    peer = _create_card("Fix gitleaks gate in test_notify_needs", assignee="coder")
    # Control: the peer really is inaccessible to this worker.
    shown = json.loads(kt._handle_show({"task_id": peer}))
    assert "own_task" in shown.get("error", "")

    out = kt._handle_create({
        "title": "Fix gitleaks gate in test_notify_needs", "assignee": "peer"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["possible_duplicates"] == []
    assert peer not in out  # no id, title echo or status of the peer card
    assert _get_task(d["task_id"]).title == "Fix gitleaks gate in test_notify_needs"


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


def test_schema_does_not_require_assignee():
    """The schema must not require ``assignee``: the deferred-call validator rejects
    a missing required field before the handler (and its worker default) runs."""
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    params = KANBAN_CREATE_SCHEMA["parameters"]
    assert params["required"] == ["title"]
    desc = params["properties"]["assignee"]["description"]
    assert "own profile" in desc and "orchestrator" in desc.lower()


def test_omitted_assignee_passes_deferred_validation_and_defaults(worker_env):
    """End to end through the deferred tool_call path: the same
    validate_deferred_call_args model_tools/tool_executor run, then registry
    dispatch. A worker's omitted assignee validates and lands on its own profile."""
    import tools.kanban_tools  # noqa: F401  (registers kanban_create)
    from tools.registry import registry
    from tools.tool_search import validate_deferred_call_args

    args = {"title": "Follow-up from deferred call"}
    assert validate_deferred_call_args("kanban_create", dict(args)) is None
    d = json.loads(registry.dispatch("kanban_create", dict(args)))
    assert d["ok"] is True
    assert _get_task(d["task_id"]).assignee == "test-worker"


def test_omitted_assignee_still_refused_for_orchestrator_after_validation(
        monkeypatch, worker_env):
    """Schema validation now admits the omission; the handler keeps the hard
    requirement for orchestrators (no HERMES_KANBAN_TASK)."""
    import tools.kanban_tools  # noqa: F401
    from tools.registry import registry
    from tools.tool_search import validate_deferred_call_args

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    args = {"title": "Orchestrated child"}
    assert validate_deferred_call_args("kanban_create", dict(args)) is None
    out = registry.dispatch("kanban_create", dict(args))
    assert "assignee is required" in (out if isinstance(out, str) else json.dumps(out))


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
# Prompt rule 6: accurate dedupe guidance (sequence, not keywords)
# ---------------------------------------------------------------------------

def _rule_6() -> str:
    from agent.prompt_builder import KANBAN_GUIDANCE
    start = KANBAN_GUIDANCE.index("6. **If follow-up work appears")
    end = KANBAN_GUIDANCE.index("7. **", start)
    return KANBAN_GUIDANCE[start:end]


def test_rule_6_checks_fetched_base_before_filing_a_failure_follow_up():
    """Fetch, then inspect origin/<base> itself for a fix to the failing paths
    (log + diff against the fetched ref), then comment instead of filing. Re-running
    the step in the current checkout cannot see a fix that only exists upstream."""
    rule = _rule_6()
    fetch = rule.index("`git fetch origin`")
    log = rule.index("`git log --oneline HEAD..origin/<base> -- <failing paths>`")
    diff = rule.index("`git diff HEAD origin/<base> -- <failing paths>`")
    comment_instead = rule.index("comment instead of filing")
    assert fetch < log < diff < comment_instead
    assert "re-run the failing step" not in rule


def test_rule_6_describes_possible_duplicates_as_post_create_advice():
    """possible_duplicates arrives after the card exists: the rule must say so,
    and route the duplicate to the orchestrator (comment on the new card + final
    summary), not claim the response prevents creation."""
    rule = _rule_6()
    returned = rule.index("`possible_duplicates`")
    after = rule.index("after the card is created", returned)
    comment = rule.index("`kanban_comment` on the new card", after)
    summary = rule.index("final summary", comment)
    assert returned < after < comment < summary
    assert "does not prevent creation" in rule
    assert "new card id" in rule and "duplicate ids" in rule
    assert "retire" in rule
    assert "instead of filing a new card" not in rule  # the old pre-create claim


def test_rule_6_keeps_lane_default():
    assert "own profile" in _rule_6()


def test_create_flags_card_rearchived_an_hour_ago(worker_env):
    """(c, r3) Freshness uses the NEWEST archived event: a card archived two days
    ago, restored, then archived again an hour ago is flagged (list_events is
    oldest-first, so reading its first archived event misses this card)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    gone = _create_card("Fix gitleaks gate in test_notify_needs")
    conn = kbc.connect()
    try:
        kb.archive_task(conn, gone)
    finally:
        conn.close()
    _age_task_and_archive_event(gone, created_ago=3 * 24 * 3600,
                                archived_ago=2 * 24 * 3600)
    conn = kbc.connect()
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'archived', ?)",
            (gone, int(time.time()) - 3600))
        conn.commit()
    finally:
        conn.close()

    d = json.loads(kt._handle_create({
        "title": "Fix gitleaks gate in test_notify_needs",
        "assignee": "peer",
    }))
    assert d["ok"] is True
    assert [c["task_id"] for c in d["possible_duplicates"]] == [gone]
