"""Gateway-owned dispatch and dispatcher liveness (issue #9).

Two halves of the same reported failure: a ready card sat ``ready`` until an
operator ran an exact-board ``hermes kanban dispatch``, and ``gateway status``
had no positive signal that could tell "dispatcher idle" from "dispatcher
absent" — silence looked healthy.

These controls prove the gateway's own dispatcher watcher spawns a ready card
exactly once with no manual dispatch call, and that the heartbeat advances on a
real tick and reads stale when no watcher is running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway import dispatcher_heartbeat as hb
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


class TestDispatcherHeartbeat:
    def test_absent_heartbeat_is_stale(self, hermes_home):
        assert hb.read_dispatcher_heartbeat() is None
        assert hb.dispatcher_heartbeat_is_stale(None) is True

    def test_tick_writes_a_fresh_record(self, hermes_home):
        rec = hb.record_dispatcher_tick(boards=("default",), spawned=0)
        assert rec["ticks"] == 1
        assert rec["updated_at"]
        assert hb.dispatcher_heartbeat_is_stale(rec) is False
        assert hb.read_dispatcher_heartbeat()["ticks"] == 1

    def test_heartbeat_changes_on_a_real_tick(self, hermes_home):
        first = hb.record_dispatcher_tick(boards=("default",), spawned=0)
        second = hb.record_dispatcher_tick(boards=("default",), spawned=0)
        assert second["ticks"] == first["ticks"] + 1

    def test_last_dispatch_only_advances_when_work_was_spawned(self, hermes_home):
        idle = hb.record_dispatcher_tick(boards=("default",), spawned=0)
        assert idle.get("last_dispatch_at") is None
        busy = hb.record_dispatcher_tick(boards=("default",), spawned=2)
        assert busy["last_dispatch_at"]
        assert busy["last_spawned"] == 2
        idle_again = hb.record_dispatcher_tick(boards=("default",), spawned=0)
        assert idle_again["last_dispatch_at"] == busy["last_dispatch_at"]

    def test_old_record_reads_stale(self, hermes_home):
        rec = hb.record_dispatcher_tick(boards=("default",), spawned=0)
        rec = dict(rec)
        rec["updated_at"] = "2020-01-01T00:00:00+00:00"
        assert hb.dispatcher_heartbeat_is_stale(rec) is True

    def test_dead_writer_pid_reads_stale_even_when_recent(self, hermes_home):
        """A heartbeat file left by a gateway that has since died must not
        keep vouching for a dispatcher that is not running."""
        rec = dict(hb.record_dispatcher_tick(boards=("default",), spawned=0))
        rec["pid"] = 2 ** 22  # not a live PID
        assert hb.dispatcher_heartbeat_is_stale(rec) is True

    def test_malformed_record_is_stale_not_an_exception(self, hermes_home):
        assert hb.dispatcher_heartbeat_is_stale({"updated_at": "not-a-date"}) is True
        assert hb.dispatcher_heartbeat_is_stale("nonsense") is True


# ---------------------------------------------------------------------------
# Gateway-owned auto-dispatch of a ready card
# ---------------------------------------------------------------------------


@pytest.fixture
def board(hermes_home):
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return hermes_home


def _run_watcher_once(runner, spawns, monkeypatch):
    """Drive exactly one dispatcher tick through the gateway watcher path."""
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        # initial 5s settle + one 1s slice → exactly one loop body
        if len(sleeps) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_spawn(task, workspace, *, board=None):
        spawns.append(task.id)
        return 424242

    monkeypatch.setattr(kb, "_default_spawn", fake_spawn)
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: True, raising=False,
    )

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            asyncio.run(runner._kanban_dispatcher_watcher())


def _runner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._kanban_sub_fail_counts = {}
    return runner


def _config(**kanban):
    cfg = {"dispatch_in_gateway": True, "dispatch_interval_seconds": 1,
           "auto_decompose": False}
    cfg.update(kanban)
    return {"kanban": cfg}


class TestGatewayOwnedDispatch:
    def test_ready_card_dispatches_exactly_once_without_manual_dispatch(
        self, board, monkeypatch,
    ):
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="auto", assignee="claude")
            assert conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()["status"] == "ready"

        spawns: list[str] = []
        runner = _runner()
        with patch("hermes_cli.config.load_config", return_value=_config()):
            _run_watcher_once(runner, spawns, monkeypatch)

        assert spawns == [tid], "the gateway watcher must dispatch the ready card"

        # A second tick must not re-spawn the now-running card.
        spawns2: list[str] = []
        runner2 = _runner()
        with patch("hermes_cli.config.load_config", return_value=_config()):
            _run_watcher_once(runner2, spawns2, monkeypatch)
        assert spawns2 == []

    def test_dispatch_tick_stamps_the_heartbeat(self, board, monkeypatch):
        with kb.connect() as conn:
            kb.create_task(conn, title="auto", assignee="claude")

        spawns: list[str] = []
        runner = _runner()
        with patch("hermes_cli.config.load_config", return_value=_config()):
            _run_watcher_once(runner, spawns, monkeypatch)

        rec = hb.read_dispatcher_heartbeat()
        assert rec is not None, "a real dispatcher tick must stamp a heartbeat"
        assert rec["ticks"] >= 1
        assert rec["last_dispatch_at"], "a tick that spawned work sets last_dispatch_at"
        assert hb.dispatcher_heartbeat_is_stale(rec) is False

    def test_no_watcher_means_no_heartbeat(self, board):
        """Control: without a dispatcher pass the field stays absent/stale, so
        silence can never be read as healthy."""
        assert hb.read_dispatcher_heartbeat() is None
        assert hb.dispatcher_heartbeat_is_stale(hb.read_dispatcher_heartbeat()) is True

    def test_disabled_dispatcher_writes_no_heartbeat(self, board, monkeypatch):
        with kb.connect() as conn:
            kb.create_task(conn, title="auto", assignee="claude")
        spawns: list[str] = []
        runner = _runner()
        with patch(
            "hermes_cli.config.load_config",
            return_value=_config(dispatch_in_gateway=False),
        ):
            _run_watcher_once(runner, spawns, monkeypatch)
        assert spawns == []
        assert hb.read_dispatcher_heartbeat() is None
