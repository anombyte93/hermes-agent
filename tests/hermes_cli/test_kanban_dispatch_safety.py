"""Dispatcher safety backstops: honest preview and bounded workers."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(db_path=db_path)
    return home


def test_dispatch_dry_run_previews_promotion_and_spawn_without_writes(
    kanban_home: Path,
    all_assignees_spawnable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = kanban_home / "kanban.db"
    spawn_calls: list[str] = []
    monkeypatch.setattr(
        kb,
        "reap_worker_zombies",
        lambda: pytest.fail("dry-run must not reap process state"),
    )

    with kb.connect(db_path=db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="eligible preview",
            assignee="alice",
            initial_status="blocked",
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_task = dict(conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone())
        before_events = [dict(row) for row in conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )]
        before_backup = kanban_home / "before.db"
        shutil.copy2(db_path, before_backup)

        def forbidden_spawn(task, workspace, board=None):
            spawn_calls.append(task.id)
            return 42

        result = kb.dispatch_once(
            conn,
            spawn_fn=forbidden_spawn,
            dry_run=True,
            max_spawn=1,
            reconcile_orphans=False,
        )

        after_backup = kanban_home / "after.db"
        shutil.copy2(db_path, after_backup)
        after_task = dict(conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone())
        after_events = [dict(row) for row in conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )]

    assert result.promoted == 1
    assert result.spawned == [(task_id, "alice", "")]
    assert spawn_calls == []
    assert after_task == before_task
    assert after_events == before_events
    assert after_backup.read_bytes() == before_backup.read_bytes()


def test_claim_inherits_configured_default_runtime_and_explicit_override_wins(
    kanban_home: Path,
    all_assignees_spawnable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"kanban": {"default_max_runtime": 7200}},
    )
    db_path = kanban_home / "kanban.db"

    with kb.connect(db_path=db_path) as conn:
        inherited_id = kb.create_task(
            conn,
            title="inherits default",
            assignee="alice",
        )
        explicit_id = kb.create_task(
            conn,
            title="keeps explicit cap",
            assignee="alice",
            max_runtime_seconds=90,
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 42,
            max_spawn=2,
            reconcile_orphans=False,
        )
        inherited = kb.get_task(conn, inherited_id)
        explicit = kb.get_task(conn, explicit_id)
        inherited_run = conn.execute(
            "SELECT max_runtime_seconds FROM task_runs WHERE task_id = ?",
            (inherited_id,),
        ).fetchone()
        explicit_run = conn.execute(
            "SELECT max_runtime_seconds FROM task_runs WHERE task_id = ?",
            (explicit_id,),
        ).fetchone()

    assert {task_id for task_id, _, _ in result.spawned} == {
        inherited_id,
        explicit_id,
    }
    assert inherited is not None
    assert inherited.max_runtime_seconds == 7200
    assert inherited_run["max_runtime_seconds"] == 7200
    assert explicit is not None
    assert explicit.max_runtime_seconds == 90
    assert explicit_run["max_runtime_seconds"] == 90


def test_shipped_default_runtime_and_disable_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli import config as config_module
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["default_max_runtime"] == 7200

    cases = [
        ({"kanban": {"default_max_runtime": 1800}}, 1800),
        ({"kanban": {"default_max_runtime": "900"}}, 900),
        ({"kanban": {"default_max_runtime": 0}}, None),
        ({"kanban": {"default_max_runtime": "invalid"}}, 7200),
        ({"kanban": {}}, 7200),
    ]
    for config, expected in cases:
        monkeypatch.setattr(
            config_module,
            "load_config_readonly",
            lambda value=config: value,
        )
        assert kb.configured_default_max_runtime() == expected


def test_inherited_runtime_is_enforced(
    kanban_home: Path,
    all_assignees_spawnable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"kanban": {"default_max_runtime": 1}},
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    db_path = kanban_home / "kanban.db"
    signals: list[tuple[int, int]] = []

    with kb.connect(db_path=db_path) as conn:
        task_id = kb.create_task(conn, title="times out", assignee="alice")
        kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 999_998,
            max_spawn=1,
            reconcile_orphans=False,
        )
        conn.execute(
            "UPDATE task_runs SET started_at = 0 WHERE task_id = ?",
            (task_id,),
        )
        timed_out = kb.enforce_max_runtime(
            conn,
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT max_runtime_seconds, outcome FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'timed_out' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert timed_out == [task_id]
    assert signals
    assert task is not None
    assert task.status == "ready"
    assert task.max_runtime_seconds == 1
    assert run["max_runtime_seconds"] == 1
    assert run["outcome"] == "timed_out"
    assert '"limit_seconds": 1' in event["payload"]


def test_fixture_pins_scratch_database(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inherited live-board override must never escape the test fixture."""
    db_path = kanban_home / "kanban.db"

    assert kb.kanban_db_path() == db_path
    assert kb.get_current_board() == "default"


def test_cli_dry_run_labels_preview_honestly(
    kanban_home: Path,
    all_assignees_spawnable,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli import kanban as kanban_cli

    with kb.connect(db_path=kanban_home / "kanban.db") as conn:
        kb.create_task(
            conn,
            title="CLI preview",
            assignee="alice",
            initial_status="blocked",
        )

    args = argparse.Namespace(dry_run=True, max=1, failure_limit=2, json=False)
    assert kanban_cli._cmd_dispatch(args) == 0
    output = capsys.readouterr().out

    assert "Would promote: 1" in output
    assert "Would spawn:   1" in output
