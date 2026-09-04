"""Regression tests for issue #37: goal-judge infrastructure failures
(e.g. PermissionDeniedError) must NOT strand a completed task in an
unrecoverable state.

Contract under test:

1. When the completion judge is unreachable (transport failure or a raised
   provider error like PermissionDeniedError), ``kanban_complete`` preserves
   the submitted summary/result/metadata and parks the task in a LOUD,
   retryable ``blocked`` state (kind=transient) — never ``triage``, never a
   silent auto-accept.
2. An operator retry (``hermes kanban complete <id>``) reuses the preserved
   submission, transitions the task to ``done``, and releases child
   dependencies.
3. Negative control: a genuine negative judge verdict (transport OK,
   verdict=continue) is still rejected exactly as before — no parking.
"""
import json

import pytest


class PermissionDeniedError(Exception):
    """Stand-in for the provider SDK's PermissionDeniedError."""


@pytest.fixture()
def goal_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with one claimed goal_mode task + a child."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="goal-judge-recovery-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True,
        )
        child = kb.create_task(
            conn, title="verify goal work", assignee="test-worker",
            parents=[tid],
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid, child


def _force_judge_reachable(monkeypatch):
    monkeypatch.setattr(
        "tools.kanban_tools._goal_judge_available", lambda: True
    )


SUBMISSION = {
    "summary": "Implemented X; tests green; evidence attached.",
    "metadata": {"changed_files": ["a.py"], "tests_run": 12},
}


def test_judge_permission_error_preserves_submission_and_parks(
    monkeypatch, goal_env
):
    """A raised PermissionDeniedError must park the task retryably with the
    submission preserved — not reject, not triage, not done."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    tid, _child = goal_env
    _force_judge_reachable(monkeypatch)

    def raising_judge(goal, last_response, **kwargs):
        raise PermissionDeniedError("org disabled subscription access")

    monkeypatch.setattr("tools.kanban_tools.judge_goal", raising_judge)

    out = json.loads(kt._handle_complete(dict(SUBMISSION)))
    # Loud, actionable error back to the worker.
    assert "error" in out
    assert "judge" in out["error"].lower()
    assert "preserved" in out["error"].lower()

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        # Retryable blocked state — NOT triage, NOT done, NOT running.
        assert task.status == "blocked"
        # Submission preserved on the closing run.
        run = kb.latest_run(conn, tid)
        assert run.summary == SUBMISSION["summary"]
        assert run.metadata == SUBMISSION["metadata"]
        # Preserved submission retrievable for operator recovery.
        preserved = kb.get_preserved_completion_submission(conn, tid)
        assert preserved is not None
        assert preserved["summary"] == SUBMISSION["summary"]
        assert preserved["metadata"] == SUBMISSION["metadata"]
        assert "PermissionDeniedError" in (preserved.get("error") or "")
    finally:
        conn.close()


def test_judge_transport_failure_flag_parks_too(monkeypatch, goal_env):
    """judge_goal swallowing the API error internally (transport_failed=True,
    verdict='continue') is the live #37 path — must park, not reject."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    tid, _child = goal_env
    _force_judge_reachable(monkeypatch)

    def transport_failed_judge(goal, last_response, **kwargs):
        return "continue", "judge error: PermissionDeniedError", False, None, True

    monkeypatch.setattr("tools.kanban_tools.judge_goal", transport_failed_judge)

    out = json.loads(kt._handle_complete(dict(SUBMISSION)))
    assert "error" in out
    assert "preserved" in out["error"].lower()

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_preserved_completion_submission(conn, tid) is not None
    finally:
        conn.close()


def test_operator_retry_completes_and_releases_child(monkeypatch, goal_env):
    """After a judge-failure park, ``hermes kanban complete <id>`` (operator,
    no flags) must reuse the preserved submission, complete the task, and
    release the child dependency."""
    import argparse

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban as kcli
    from tools import kanban_tools as kt

    tid, child = goal_env
    _force_judge_reachable(monkeypatch)
    monkeypatch.setattr(
        "tools.kanban_tools.judge_goal",
        lambda *a, **k: (_ for _ in ()).throw(
            PermissionDeniedError("org disabled subscription access")
        ),
    )
    out = json.loads(kt._handle_complete(dict(SUBMISSION)))
    assert "error" in out

    # Operator context: not the worker process.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    args = argparse.Namespace(
        task_ids=[tid], result=None, summary=None, metadata=None,
    )
    rc = kcli._cmd_complete(args)
    assert rc == 0

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        run = kb.latest_run(conn, tid)
        assert run.outcome == "completed"
        assert run.summary == SUBMISSION["summary"]
        # Child released: parent done -> child promoted to ready.
        assert kb.get_task(conn, child).status == "ready"
    finally:
        conn.close()


def test_operator_retry_with_judge_still_down_completes(monkeypatch, goal_env):
    """An explicit operator retry is a human decision: if the judge is STILL
    unreachable, the CLI warns and accepts the preserved submission rather
    than re-stranding the task."""
    import argparse

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban as kcli
    from tools import kanban_tools as kt

    tid, child = goal_env
    _force_judge_reachable(monkeypatch)
    monkeypatch.setattr(
        "tools.kanban_tools.judge_goal",
        lambda *a, **k: (_ for _ in ()).throw(PermissionDeniedError("nope")),
    )
    json.loads(kt._handle_complete(dict(SUBMISSION)))

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # CLI-side judge gate also transport-fails.
    monkeypatch.setattr(
        kcli, "_goal_mode_handoff_rejection",
        lambda task, evidence: (None, "judge error: PermissionDeniedError"),
    )

    args = argparse.Namespace(
        task_ids=[tid], result=None, summary=None, metadata=None,
    )
    rc = kcli._cmd_complete(args)
    assert rc == 0

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, child).status == "ready"
    finally:
        conn.close()


def test_park_is_sticky_across_recompute_ready(monkeypatch, goal_env):
    """A judge-unavailable park must survive recompute_ready / the
    dispatcher tick. Before the fix the park emitted only a
    ``completion_judge_unavailable`` event, so ``_has_sticky_block`` saw no
    ``blocked`` event and recompute_ready silently re-promoted the task to
    ``ready`` — the dispatcher then respawned a worker on already-finished
    work."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    tid, _child = goal_env
    _force_judge_reachable(monkeypatch)
    monkeypatch.setattr(
        "tools.kanban_tools.judge_goal",
        lambda *a, **k: (_ for _ in ()).throw(
            PermissionDeniedError("org disabled subscription access")
        ),
    )
    out = json.loads(kt._handle_complete(dict(SUBMISSION)))
    assert "error" in out

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
        promoted = kb.recompute_ready(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"judge-unavailable park must be sticky across recompute_ready; "
            f"got status={task.status!r} (promoted={promoted})"
        )
        # The preserved submission must still be retrievable after the tick.
        assert kb.get_preserved_completion_submission(conn, tid) is not None
    finally:
        conn.close()


def test_park_event_visible_to_terminal_notification_path(
    monkeypatch, goal_env
):
    """The park must surface on the existing terminal notification path.

    The gateway notifier claims events via ``claim_unseen_events_for_sub``
    filtered to its TERMINAL_KINDS (which include ``blocked`` but not any
    bespoke kind). Before the fix the park emitted only
    ``completion_judge_unavailable`` — never claimed, never delivered — so
    a subscribed watcher received zero notification for a stranded task."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Mirror of gateway/kanban_watchers.py TERMINAL_KINDS.
    TERMINAL_KINDS = (
        "completed", "blocked", "gave_up", "crashed", "timed_out",
        "status", "archived", "unblocked", "block_loop_detected",
        "review_requested", "changes_requested",
    )

    tid, _child = goal_env
    _force_judge_reachable(monkeypatch)
    monkeypatch.setattr(
        "tools.kanban_tools.judge_goal",
        lambda *a, **k: (_ for _ in ()).throw(
            PermissionDeniedError("org disabled subscription access")
        ),
    )

    # Subscribe BEFORE the park (new subs snap their cursor to the current
    # MAX event id, so only events after subscription are unseen).
    conn = kb.connect()
    try:
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
        )
    finally:
        conn.close()

    out = json.loads(kt._handle_complete(dict(SUBMISSION)))
    assert "error" in out

    conn = kb.connect()
    try:
        old, new, events = kb.claim_unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=TERMINAL_KINDS,
        )
        kinds = [ev.kind for ev in events]
        assert "blocked" in kinds, (
            f"judge-unavailable park must emit a terminal-visible 'blocked' "
            f"event; watcher claimed kinds={kinds}"
        )
        blocked = [ev for ev in events if ev.kind == "blocked"][0]
        payload = blocked.payload or {}
        # The notifier renders payload['reason'] into the user-facing ping.
        assert "judge" in str(payload.get("reason", "")).lower()
    finally:
        conn.close()


def test_negative_judge_verdict_still_rejects_without_parking(
    monkeypatch, goal_env
):
    """Negative control: a reachable judge returning a genuine negative
    verdict must reject completion exactly as before — task stays running,
    nothing preserved, nothing parked."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    tid, _child = goal_env
    _force_judge_reachable(monkeypatch)

    def negative_judge(goal, last_response, **kwargs):
        return "continue", "missing verification evidence", False, None, False

    monkeypatch.setattr("tools.kanban_tools.judge_goal", negative_judge)

    out = json.loads(kt._handle_complete(dict(SUBMISSION)))
    assert "error" in out
    assert "rejected by judge" in out["error"]

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert kb.get_preserved_completion_submission(conn, tid) is None
    finally:
        conn.close()
