"""Model truth on task_runs (#7): model_used / provider_used / fallback_from.

A board's model override is a *request*. Which model actually answered is a
fact the worker learns only at the first successful API call — and a provider
fallback (missing key, 429, quota) can silently substitute a different model
entirely. These tests pin the recording of that fact on the run row, its
exposure through show/list output, and the dispatcher's human-visible
``model_fallback`` comment when the fact contradicts the request.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture(autouse=True)
def _reset_truth_latch():
    """The once-latch is process-global; isolate it per test."""
    from agent import kanban_model_truth as kmt
    kmt._recorded = False
    yield
    kmt._recorded = False


@pytest.fixture
def db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _running_task_with_run(db, *, model_override=None, provider_override=None):
    tid = kb.create_task(
        db, title="probe", assignee="codex",
        model_override=model_override, provider_override=provider_override,
    )
    claimed = kb.claim_task(db, tid, claimer="test:1", ttl_seconds=300)
    assert claimed, "claim failed"
    with kb.write_txn(db):
        run_id = kb._current_run_id(db, tid)
    assert run_id, "no run row opened"
    return tid, run_id


# ---------------------------------------------------------------------------
# Pure derivation: agent state -> (model_used, provider_used, fallback_from)
# ---------------------------------------------------------------------------


class _Snap:
    """Minimal agent stand-in: only the attributes model_truth_snapshot reads."""

    def __init__(self, model, provider, fallback_route=None):
        self.model = model
        self.provider = provider
        self._primary_runtime = {"model": "glm-5.3", "provider": "zai"}
        if fallback_route is not None:
            self._provider_fallback_route = fallback_route


def test_model_truth_plain_route_reads_current_model():
    from agent.kanban_model_truth import model_truth_snapshot

    snap = _Snap("glm-5.3", "zai")
    assert model_truth_snapshot(snap) == {
        "model_used": "glm-5.3", "provider_used": "zai", "fallback_from": None,
    }


def test_model_truth_fallback_route_derives_original_from_primary_runtime():
    from agent.kanban_model_truth import model_truth_snapshot

    # After a fallback the agent's model/provider ARE the fallback's; the
    # original request survives only in the primary-runtime snapshot.
    snap = _Snap(
        "gpt-5.6-sol", "openai-codex",
        fallback_route=("gpt-5.6-sol", "openai-codex"),
    )
    assert model_truth_snapshot(snap) == {
        "model_used": "gpt-5.6-sol", "provider_used": "openai-codex",
        "fallback_from": "glm-5.3 (zai)",
    }


def test_model_truth_fallback_without_snapshot_falls_back_to_route():
    from agent.kanban_model_truth import model_truth_snapshot

    snap = _Snap("gpt-5.6-sol", "openai-codex",
                 fallback_route=("gpt-5.6-sol", "openai-codex"))
    snap._primary_runtime = None
    # No snapshot (older session shape): the route itself is the best evidence.
    assert model_truth_snapshot(snap)["fallback_from"] == "gpt-5.6-sol (openai-codex)"


def test_model_truth_missing_attributes_return_current_route():
    from agent.kanban_model_truth import model_truth_snapshot

    class _Bare:
        model = "m1"
        provider = "p1"

    # getattr-defaults keep a bare object safe.
    assert model_truth_snapshot(_Bare()) == {
        "model_used": "m1", "provider_used": "p1", "fallback_from": None,
    }


# ---------------------------------------------------------------------------
# record_run_model_truth: first-write-wins on the run row
# ---------------------------------------------------------------------------


def test_record_run_model_truth_writes_columns(db):
    tid, run_id = _running_task_with_run(db)
    ok = kb.record_run_model_truth(
        db, tid, run_id, model_used="glm-5.3", provider_used="zai")
    assert ok is True
    run = kb.get_run(db, run_id)
    assert (run.model_used, run.provider_used, run.fallback_from) == ("glm-5.3", "zai", None)


def test_record_run_model_truth_first_write_wins(db):
    tid, run_id = _running_task_with_run(db)
    assert kb.record_run_model_truth(db, tid, run_id, model_used="glm-5.3", provider_used="zai")
    # A later call (restored primary) must NOT overwrite the first fact.
    assert kb.record_run_model_truth(db, tid, run_id, model_used="glm-5.3", provider_used="zai",
                                     fallback_from="glm-5.3 (zai)") is False
    run = kb.get_run(db, run_id)
    assert run.fallback_from is None


def test_record_run_model_truth_wrong_task_or_unknown_run(db):
    tid, run_id = _running_task_with_run(db)
    other_tid = kb.create_task(db, title="other", assignee="codex")
    # run belongs to another task: refuse.
    assert kb.record_run_model_truth(db, other_tid, run_id, model_used="m", provider_used="p") is False
    # unknown run id: refuse.
    assert kb.record_run_model_truth(db, tid, 99999, model_used="m", provider_used="p") is False


def test_record_run_model_truth_schema_migrates_legacy_db(db):
    """A task_runs table predating the columns is migrated on connect."""
    import sqlite3

    tid, run_id = _running_task_with_run(db)
    db.close()
    raw = sqlite3.connect(str(kb.kanban_db_path()))
    cols = {r[1] for r in raw.execute("PRAGMA table_info(task_runs)")}
    assert {"model_used", "provider_used", "fallback_from"} <= cols
    raw.close()


# ---------------------------------------------------------------------------
# record_model_truth_once: env-gated agent bridge
# ---------------------------------------------------------------------------


def test_record_model_truth_once_gated_on_env(db, monkeypatch):
    from agent import kanban_model_truth as kmt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    tid, run_id = _running_task_with_run(db)
    # No env: no write, no crash.
    kmt.record_model_truth_once(_Snap("glm-5.3", "zai"))
    run = kb.get_run(db, run_id)
    assert run.model_used is None

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    calls = []
    monkeypatch.setattr(kmt, "_write_model_truth", lambda *a, **k: calls.append((a, k)) or True)
    assert kmt.record_model_truth_once(_Snap("glm-5.3", "zai")) is True
    assert kmt.record_model_truth_once(_Snap("glm-5.3", "zai")) is False  # once
    assert len(calls) == 1


def test_record_model_truth_once_survives_board_failure(db, monkeypatch):
    from agent import kanban_model_truth as kmt

    tid, run_id = _running_task_with_run(db)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    def boom(*a, **k):
        raise RuntimeError("board unavailable")

    monkeypatch.setattr(kmt, "_write_model_truth", boom)
    assert kmt.record_model_truth_once(_Snap("glm-5.3", "zai")) is False  # never raises
    # Failure does not consume the once-latch: a later activity tick retries.
    monkeypatch.setattr(kmt, "_write_model_truth", lambda *a, **k: True)
    assert kmt.record_model_truth_once(_Snap("glm-5.3", "zai")) is True


# ---------------------------------------------------------------------------
# CLI surfacing: show / list print the answering model
# ---------------------------------------------------------------------------


def _run_show(tid):
    from hermes_cli import kanban as kc

    return kc.run_slash(f"show {tid}")


def test_show_prints_answering_model_matching_override(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    assert kb.record_run_model_truth(db, tid, run_id, model_used="glm-5.3", provider_used="zai")
    out = _run_show(tid)
    assert "model:" in out and "glm-5.3 (zai)" in out
    assert "fallback" not in out.lower()


def test_show_prints_fallback_line_when_model_differs(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    assert kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    out = _run_show(tid)
    assert "model:" in out
    assert "glm-5.3 (zai) → gpt-5.6-sol (openai-codex)" in out
    assert "⚠ fallback" in out


def test_show_falls_back_to_override_only_when_no_truth_recorded(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    out = _run_show(tid)
    assert "model:" in out and "glm-5.3 (zai)" in out
    assert "fallback" not in out.lower()


def test_show_without_override_prints_used_model(db):
    tid, run_id = _running_task_with_run(db)
    assert kb.record_run_model_truth(db, tid, run_id, model_used="kimi-k3", provider_used="openrouter")
    out = _run_show(tid)
    assert "model:" in out and "kimi-k3 (openrouter)" in out


def test_list_lines_carry_model_marker(db):
    from hermes_cli import kanban as kc

    tid_ok, run_ok = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(db, tid_ok, run_ok, model_used="glm-5.3", provider_used="zai")
    tid_fb, run_fb = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(
        db, tid_fb, run_fb, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")

    out = kc.run_slash("list")
    ok_line = next(line for line in out.splitlines() if tid_ok in line)
    fb_line = next(line for line in out.splitlines() if tid_fb in line)
    assert "[glm-5.3 (zai)]" in ok_line
    assert "[glm-5.3 (zai) → gpt-5.6-sol (openai-codex) ⚠ fallback]" in fb_line


# ---------------------------------------------------------------------------
# Dispatcher: model_fallback comment on the card
# ---------------------------------------------------------------------------


def _fallback_comment(db, tid):
    return [c for c in kb.list_comments(db, tid) if c.body.startswith("model_fallback:")]


def test_dispatch_comments_model_fallback_on_mismatch(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    kbd.notify_model_fallbacks(db)
    comments = _fallback_comment(db, tid)
    assert comments, "expected a model_fallback comment"
    body = comments[0].body
    assert "glm-5.3 (zai)" in body
    assert "gpt-5.6-sol (openai-codex)" in body


def test_dispatch_no_comment_when_model_matches_override(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(db, tid, run_id, model_used="glm-5.3", provider_used="zai")
    kbd.notify_model_fallbacks(db)
    assert _fallback_comment(db, tid) == []


def test_dispatch_comment_once_per_run(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    kbd.notify_model_fallbacks(db)
    kbd.notify_model_fallbacks(db)  # second tick must not duplicate
    assert len(_fallback_comment(db, tid)) == 1


def test_dispatch_no_comment_without_override(db):
    # No override: profile default answered; nothing to contradict.
    tid, run_id = _running_task_with_run(db)
    kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    kbd.notify_model_fallbacks(db)
    assert _fallback_comment(db, tid) == []


def test_dispatch_wiring_notify_model_fallbacks_runs_each_tick(db, monkeypatch):
    """dispatch_once calls notify_model_fallbacks every tick (dry_run too)."""
    called = []
    monkeypatch.setattr(kbd, "notify_model_fallbacks", lambda conn: called.append(True))
    kbd.dispatch_once(db, dry_run=True)
    assert called == [True]


# ---------------------------------------------------------------------------
# JSON + API exposure
# ---------------------------------------------------------------------------


def test_show_json_exposes_run_model_fields(db):
    from hermes_cli import kanban as kc

    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    payload = json.loads(kc.run_slash(f"show {tid} --json"))
    run = next(r for r in payload["runs"] if r["id"] == run_id)
    assert run["model_used"] == "gpt-5.6-sol"
    assert run["provider_used"] == "openai-codex"
    assert run["fallback_from"] == "glm-5.3 (zai)"


# ---------------------------------------------------------------------------
# latest_model_truth aggregate
# ---------------------------------------------------------------------------


def test_latest_model_truth_returns_latest_run_per_task(db):
    tid, run1 = _running_task_with_run(db)
    kb.record_run_model_truth(db, tid, run1, model_used="kimi-k3", provider_used="openrouter")
    # Second run (retry): release the claim the way the dispatcher's reclaim
    # does, then re-claim — recorded truth must be the NEWEST run's, not the first.
    with kb.write_txn(db):
        kb._end_run(db, tid, outcome="crashed", error="retry", status="failed")
        db.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, last_heartbeat_at = NULL WHERE id = ?",
            (tid,),
        )
    claimed2 = kb.claim_task(db, tid, claimer="test:2", ttl_seconds=300)
    assert claimed2, "re-claim failed"
    run2 = kb._current_run_id(db, tid)
    assert run2 and run2 != run1
    kb.record_run_model_truth(db, tid, run2, model_used="glm-5.3", provider_used="zai")
    truth = kb.latest_model_truth(db, [tid])
    assert truth[tid]["model_used"] == "glm-5.3"
    assert truth[tid]["provider_used"] == "zai"


def test_latest_model_truth_omits_tasks_without_truth(db):
    tid, run_id = _running_task_with_run(db)
    assert kb.latest_model_truth(db, [tid]) == {}
    assert kb.latest_model_truth(db, []) == {}


# ---------------------------------------------------------------------------
# Dashboard API: /board and /runs/{id} expose model truth
# ---------------------------------------------------------------------------


def _load_dashboard_router():
    import importlib.util
    import sys as _sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    mod_name = "hermes_dashboard_plugin_kanban_model_truth_test"
    if mod_name in _sys.modules:
        return _sys.modules[mod_name].router
    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(_load_dashboard_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_api_board_rows_expose_model_truth(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    client = _api_client()
    resp = client.get("/api/plugins/kanban/board")
    assert resp.status_code == 200
    tasks = [t for col in resp.json()["columns"] for t in col["tasks"]]
    row = next(t for t in tasks if t["id"] == tid)
    assert row["model_truth"] == {
        "model_used": "gpt-5.6-sol",
        "provider_used": "openai-codex",
        "fallback_from": "glm-5.3 (zai)",
    }


def test_api_board_rows_without_truth_keep_legacy_shape(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    client = _api_client()
    resp = client.get("/api/plugins/kanban/board")
    assert resp.status_code == 200
    tasks = [t for col in resp.json()["columns"] for t in col["tasks"]]
    row = next(t for t in tasks if t["id"] == tid)
    assert "model_truth" not in row


def test_api_task_endpoint_exposes_model_truth(db):
    tid, run_id = _running_task_with_run(
        db, model_override="glm-5.3", provider_override="zai")
    kb.record_run_model_truth(
        db, tid, run_id, model_used="gpt-5.6-sol", provider_used="openai-codex",
        fallback_from="glm-5.3 (zai)")
    client = _api_client()
    resp = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["model_truth"]["model_used"] == "gpt-5.6-sol"
    run = next(r for r in body["runs"] if r["id"] == run_id)
    assert run["fallback_from"] == "glm-5.3 (zai)"


def test_api_run_endpoint_exposes_model_truth(db):
    tid, run_id = _running_task_with_run(db)
    kb.record_run_model_truth(
        db, tid, run_id, model_used="glm-5.3", provider_used="zai")
    client = _api_client()
    resp = client.get(f"/api/plugins/kanban/runs/{run_id}")
    assert resp.status_code == 200
    run = resp.json()["run"]
    assert run["model_used"] == "glm-5.3"
    assert run["provider_used"] == "zai"
    assert run["fallback_from"] is None
