"""Amending the body/title of a card that is not yet done (#180).

The parent must be able to correct a brief in any status before ``done``:
the amend is one audited event (hashes and lengths, never the text), the
next run's worker context carries the corrected text, and neither a worker
nor a completion racing the amend can rewrite what a worker reads.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_edit_open as keo


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


NEW_BODY = "Corrected brief: do the real work, not the typo."


def _body_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task(conn, tid):
    task = kb.get_task(conn, tid)
    assert task is not None
    return task


def _edited_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "body_edited"]


def _payload(conn, tid):
    events = _edited_events(conn, tid)
    assert len(events) == 1
    payload = events[0].payload
    assert isinstance(payload, dict)
    return payload


def _set_status(conn, tid, status):
    """Force a status for gate coverage (statuses not reachable from helpers)."""
    if status == "blocked":
        # A trigger refuses blocked without a durable reason.
        conn.execute(
            "UPDATE tasks SET status = ?, block_reason = 'test setup' WHERE id = ?",
            (status, tid))
    else:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))


# ---------------------------------------------------------------------------
# Happy path: every open status amends, exactly one event, next run sees it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["triage", "todo", "scheduled", "ready", "running", "blocked", "review"])
def test_amend_open_task_changes_body_and_records_one_event(kanban_home, status):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="filed wrong", body="Original brief.")
        _set_status(conn, tid, status)
        conn.commit()

        assert keo.amend_open_task(conn, tid, body=NEW_BODY) is True

        task = _task(conn, tid)
        assert task.body == NEW_BODY
        assert task.status == status  # an amend never moves a card

        payload = _payload(conn, tid)
        assert payload["fields"] == ["body"]
        assert payload["old_body_sha256"] == _body_sha("Original brief.")
        assert payload["new_body_sha256"] == _body_sha(NEW_BODY)
        assert payload["old_body_len"] == len("Original brief.")
        assert payload["new_body_len"] == len(NEW_BODY)
        assert payload["status"] == status
        # Never the text itself in the audit trail.
        assert NEW_BODY not in str(payload)
        assert "Original brief." not in str(payload)


def test_amend_running_task_event_carries_current_run_id(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="running card", body="old")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.status == "running"

        assert keo.amend_open_task(conn, tid, body=NEW_BODY) is True
        assert _payload(conn, tid)["current_run_id"] == claimed.current_run_id


def test_amend_title_only_records_title_event_without_body_hashes(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="filed wrong", body="Original brief.")
        assert keo.amend_open_task(conn, tid, title="Fixed title") is True
        task = _task(conn, tid)
        assert task.title == "Fixed title"
        assert task.body == "Original brief."
        # Body untouched: no body hashes to record.
        assert "old_body_sha256" not in _payload(conn, tid)
        assert _payload(conn, tid)["fields"] == ["title"]


def test_amend_body_and_title_is_one_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="filed wrong", body="Original brief.")
        assert keo.amend_open_task(conn, tid, body=NEW_BODY, title="Fixed title") is True
        payload = _payload(conn, tid)
        assert payload["fields"] == ["body", "title"]
        task = _task(conn, tid)
        assert task.body == NEW_BODY and task.title == "Fixed title"


def test_amend_body_reaches_next_run_worker_context(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="context check", body="Original brief.")
        assert keo.amend_open_task(conn, tid, body=NEW_BODY) is True
        ctx = kb.build_worker_context(conn, tid)
        assert NEW_BODY in ctx
        assert "Original brief." not in ctx


def test_amend_fires_field_names_update_hook_after_commit(kanban_home, monkeypatch):
    seen: list[tuple[str, list[str]]] = []

    def fake_notify(conn, task_id, changed_fields, **kwargs):
        seen.append((task_id, list(changed_fields)))

    monkeypatch.setattr(kb, "notify_task_updated", fake_notify)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="hook check", body="old")
        assert keo.amend_open_task(conn, tid, body=NEW_BODY) is True
    assert seen == [(tid, ["body"])]


# ---------------------------------------------------------------------------
# Refusals: done, archived, unknown, blank, no-op
# ---------------------------------------------------------------------------


def test_amend_refuses_done_and_points_at_result_mode(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done card", body="old")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done") is True
        before = _task(conn, tid)

        with pytest.raises(keo.RefusedEditError, match=r"--result"):
            keo.amend_open_task(conn, tid, body=NEW_BODY)

        assert _task(conn, tid).body == before.body
        assert _edited_events(conn, tid) == []


def test_amend_refuses_archived_without_suggestion(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="archived card", body="old")
        assert kb.archive_task(conn, tid) is True

        with pytest.raises(keo.RefusedEditError, match=r"archived"):
            keo.amend_open_task(conn, tid, body=NEW_BODY)

        assert _task(conn, tid).body == "old"
        assert _edited_events(conn, tid) == []


def test_amend_refuses_unknown_id(kanban_home):
    with kbc.connect() as conn:
        with pytest.raises(keo.RefusedEditError, match=r"unknown"):
            keo.amend_open_task(conn, "t_nope", body=NEW_BODY)


def test_amend_refuses_blank_body_and_blank_title(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="blank guard", body="old")
        with pytest.raises(ValueError, match=r"blank"):
            keo.amend_open_task(conn, tid, body="   \n\t")
        with pytest.raises(ValueError, match=r"blank"):
            keo.amend_open_task(conn, tid, title="  ")
        assert _task(conn, tid).body == "old"
        assert _edited_events(conn, tid) == []


def test_amend_identical_body_is_noop_without_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="noop guard", body="same text")
        assert keo.amend_open_task(conn, tid, body="same text") is False
        assert _task(conn, tid).body == "same text"
        assert _edited_events(conn, tid) == []


def test_amend_identical_title_is_noop_without_event(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="same", body="text")
        assert keo.amend_open_task(conn, tid, title="same") is False
        assert _edited_events(conn, tid) == []


def test_amend_requires_at_least_one_field(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="arity guard", body="old")
        with pytest.raises(ValueError, match=r"nothing to"):
            keo.amend_open_task(conn, tid)
        assert _edited_events(conn, tid) == []


def test_amend_mixed_noop_and_change_is_one_event_for_the_change(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="kept", body="same text")
        assert keo.amend_open_task(conn, tid, body="same text", title="changed") is True
        assert _payload(conn, tid)["fields"] == ["title"]


# ---------------------------------------------------------------------------
# The race: a completion that lands between check and write is refused
# ---------------------------------------------------------------------------


def test_amend_refused_when_task_completes_inside_the_txn_window(kanban_home):
    """Two connections: B flips the card to done under an open write txn while
    A's amend is entering its own; A must refuse once it sees the committed
    done, never writing the corrected body onto a completed card."""
    with kbc.connect() as setup:
        tid = kb.create_task(setup, title="race card", body="old")

    conn_b = kbc.connect()
    result: dict[str, object] = {}
    try:
        conn_b.execute("BEGIN IMMEDIATE")
        conn_b.execute("UPDATE tasks SET status = 'done', completed_at = 1 WHERE id = ?", (tid,))

        def amend_from_a():
            # Connections are thread-affine: A opens its own inside the thread.
            conn_a = kbc.connect()
            try:
                result["ok"] = keo.amend_open_task(conn_a, tid, body=NEW_BODY)
            except Exception as exc:  # refusal or lock exhaustion — never a silent write
                result["ok"] = f"raised {type(exc).__name__}"
            finally:
                conn_a.close()

        thread = threading.Thread(target=amend_from_a)
        thread.start()
        # Hold B's write lock long enough that A is parked on its BEGIN
        # IMMEDIATE when the completion commits (loose bound; the assertions
        # hold under either interleaving).
        time.sleep(0.5)
        conn_b.execute("COMMIT")
        thread.join(timeout=30)
        assert not thread.is_alive()

        # Whatever A saw, the card must still be done with its original body and
        # no amend event; a lock timeout is acceptable, a landed amend is not.
        with kbc.connect() as check:
            task = _task(check, tid)
            assert task.status == "done"
            assert task.body == "old"
            assert _edited_events(check, tid) == []
        assert result["ok"] is not True
    finally:
        conn_b.close()


# ---------------------------------------------------------------------------
# Worker boundary: a delegated child can never reach the write
# ---------------------------------------------------------------------------


def test_amend_blocked_for_delegated_child_context(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="child guard", body="old")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    with kbc.connect() as conn:
        with pytest.raises(PermissionError):
            keo.amend_open_task(conn, tid, body=NEW_BODY)
        assert _task(conn, tid).body == "old"
