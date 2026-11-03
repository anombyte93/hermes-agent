"""Only a reasoned answer may reopen a triage card parked by the block breaker."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools  # Register the real handler.
from tools.registry import registry


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_tools, "_profile_has_kanban_toolset", lambda: True)
    kb.init_db()
    return home


def _loop_triage(conn):
    tid = kb.create_task(conn, title="needs the parent's answer", assignee="worker")
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="parent must answer", kind="dependency")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.block_recurrences == attempt + 1
        assert task.status == ("triage" if attempt + 1 == kb.BLOCK_RECURRENCE_LIMIT else "blocked")
        if task.status == "blocked":
            assert "Unblocked" in kc.run_slash(f"unblock {tid} --reason 'answer supplied'")
    events = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
    assert len(events) == 1
    assert events[0].payload["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT
    return tid


def _unblock(conn, tid, surface, reason):
    if surface == "db":
        ok, detail = kb.unblock_task(conn, tid, reason=reason, with_reason=True)
        return ok, detail or ""
    if surface == "cli":
        output = kc.run_slash(f"unblock {tid}" + (f" --reason '{reason}'" if reason is not None else ""))
        return "Unblocked" in output, output
    args = {"task_id": tid}
    if reason is not None:
        args["reason"] = reason
    result = json.loads(registry.dispatch("kanban_unblock", args))
    return result.get("ok", False), str(result.get("error", ""))


@pytest.mark.parametrize("surface", ["db", "cli", "tool"])
@pytest.mark.parametrize("pending_parent", [False, True])
def test_reasoned_loop_triage_unblock_resets_and_regates(kanban_home, surface, pending_parent):
    with kbc.connect_closing() as conn:
        tid = _loop_triage(conn)
        if pending_parent:
            parent = kb.create_task(conn, title="new prerequisite", assignee="worker")
            kb.link_tasks(conn, parent_id=parent, child_id=tid)
        # Comments/field edits must not obscure the event that entered triage.
        kb.add_comment(conn, tid, "parent", "answer prepared")
        assert kb.assign_task(conn, tid, "worker")
        before = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        prior_unblocks = [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"]
        for missing in (None, "", "   "):
            ok, detail = _unblock(conn, tid, surface, missing)
            assert not ok
            assert "reason" in detail.lower() and "required" in detail.lower()
            assert kb.get_task(conn, tid) == before
            assert kb.list_runs(conn, tid) == runs
            assert [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"] == prior_unblocks

        reason = "parent answered; prerequisite recorded"
        ok, detail = _unblock(conn, tid, surface, reason)
        assert ok, detail
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == ("todo" if pending_parent else "ready")
        assert task.block_recurrences == 0
        assert task.block_kind is None
        assert task.block_reason is None
        assert task.current_run_id is None
        assert kb.list_runs(conn, tid) == runs
        unblocks = [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"]
        assert len(unblocks) == len(prior_unblocks) + 1
        assert unblocks[-1].payload == {
            "from": "triage", "reason": reason,
            "recurrences_reset": kb.BLOCK_RECURRENCE_LIMIT,
        }
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == task.status
        if pending_parent:
            assert kb.claim_task(conn, parent, claimer="worker") is not None
            assert kb.complete_task(conn, parent, result="prerequisite done")
            kb.recompute_ready(conn)
            assert kb.get_task(conn, tid).status == "ready"
        # The parent's answer starts a fresh block budget, not immediate triage.
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="another answer needed", kind="dependency")
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).block_recurrences == 1


@pytest.mark.parametrize("surface", ["db", "cli", "tool"])
@pytest.mark.parametrize("provenance", ["created", "status_after_loop", "specified_after_loop"])
def test_plain_or_reentered_triage_still_requires_specify(kanban_home, surface, provenance):
    with kbc.connect_closing() as conn:
        if provenance == "created":
            tid = kb.create_task(conn, title="unwritten brief", assignee="worker", triage=True)
        else:
            tid = _loop_triage(conn)
            if provenance == "specified_after_loop":
                assert kb.specify_triage_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
                if provenance == "status_after_loop":
                    kb._append_event(conn, tid, "status", {"status": "triage"})
        kb.add_comment(conn, tid, "parent", "not an unblock-loop answer")
        before = kb.get_task(conn, tid)
        prior_unblocks = [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"]
        ok, detail = _unblock(conn, tid, surface, "parent supplied a reason")
        assert not ok
        assert "specify" in detail.lower()
        assert kb.get_task(conn, tid) == before
        assert [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"] == prior_unblocks
