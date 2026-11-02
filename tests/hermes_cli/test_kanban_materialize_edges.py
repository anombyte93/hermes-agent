"""Precedence and lifecycle boundaries for canonical plan identities."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_materialize import materialize_task


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(key, raising=False)
    with connect_closing(board="default") as conn:
        yield conn


def materialize(conn, revision="r1", proposed="work", **kwargs):
    return materialize_task(conn, project="fixture", plan_revision=revision,
        proposed_task_id=proposed, intent_fingerprint="same-intent", title="Acceptance", **kwargs)


def test_unchanged_intent_never_ignores_a_mapping_to_another_card(store):
    canonical = materialize(store)["task_id"]
    other = materialize(store, proposed="another-task")["task_id"]
    assert canonical != other
    # Ruling 17: validate the explicit map before returning existing.
    assert materialize(store, revision="r2", maps_from="r1:another-task") == {
        "state": "conflict", "task_id": canonical, "reason": "logical_id_retired"}
    assert store.execute("SELECT task_id FROM task_materializations WHERE "
                         "plan_revision='r2' AND proposed_task_id='work'").fetchone() is None
    assert [event.kind for event in kb.list_events(store, canonical)] == ["created"]


def test_archive_and_other_projects_never_destroy_or_duplicate_canonical_identity(store):
    tid = materialize(store)["task_id"]
    store.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
    archived = kb.get_task(store, tid)
    assert materialize(store) == {"state": "existing", "task_id": tid}
    assert materialize(store, revision="r2") == {"state": "existing", "task_id": tid}
    assert kb.get_task(store, tid) == archived
    separate = materialize_task(store, project="different-project", plan_revision="r1",
        proposed_task_id="work", intent_fingerprint="same-intent", title="Other project")
    assert separate["state"] == "created" and separate["task_id"] != tid
    assert store.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
