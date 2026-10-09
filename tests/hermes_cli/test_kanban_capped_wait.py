"""Concurrency caps, wave 6: host-wide counts, GPU groups, capped-is-not-stuck,
capped visibility and long-wait alerts (follow-up to anombyte93/hermes-agent#83/#84).

Measured 2026-10-09: one 4-slot llama-server serves profile ``evo``
(gpt-oss-120b); five concurrent evo workers decoded at 3.6-7 tok/s instead of
37 and all timed out. The #83 per-assignee cap only counted THIS board's
running rows, so two boards could each run ``cap`` evo workers on one GPU, and
two profiles sharing that GPU could not be capped together at all.

Covered here:

* A. per-assignee / scalar / group caps count running work on EVERY board on
  the host, failing open for an unreadable board;
* B. ``kanban.assignee_groups``: profiles sharing one GPU capped together;
* C. a tick whose only pending ready work was deferred by a cap is NOT a bad
  tick for the daemon's "dispatcher stuck" telemetry;
* D. ``capped=N capped_by={...}`` on the verbose tick line, and one
  ``capped`` task event per wait episode;
* E. ``kanban.capped_alert``: a generic command run once per long wait
  episode, followed by a ``capped_alerted`` event.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for prof in ("evo", "coder", "claude"):
        (home / "profiles" / prof).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    return 12345


def _seed(conn, **counts) -> list[str]:
    ids = []
    for assignee, n in counts.items():
        for i in range(n):
            ids.append(kb.create_task(conn, title=f"{assignee}{i}", assignee=assignee))
    return ids


def _run_on(board, **counts) -> None:
    """Put ``counts`` claimed (status=running) tasks on ``board``."""
    with kbc.connect_closing(board=board) as conn:
        for tid in _seed(conn, **counts):
            assert kb.claim_task(conn, tid) is not None


def _by(res_list):
    out: dict = {}
    for entry in res_list:
        out[entry[1]] = out.get(entry[1], 0) + 1
    return out


def _events(conn, task_id, kind):
    return [e for e in kb.list_events(conn, task_id) if e.kind == kind]


# ---------------------------------------------------------------------------
# A. Host-wide per-assignee counting
# ---------------------------------------------------------------------------


def test_named_cap_counts_running_evo_on_every_board(kanban_home):
    kb.create_board("second")
    _run_on("second", evo=2)
    _run_on(None, evo=1)
    with kbc.connect_closing() as conn:
        _seed(conn, evo=3, coder=2)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 3}, reconcile_orphans=False,
        )
    # 2 on 'second' + 1 here = 3 = cap: no evo spawn anywhere on the host.
    assert _by(res.spawned) == {"coder": 2}
    assert _by(res.skipped_per_profile_capped) == {"evo": 3}
    assert {cur for (_t, _a, cur) in res.skipped_per_profile_capped} == {3}


def test_named_cap_partial_headroom_across_boards(kanban_home):
    kb.create_board("second")
    _run_on("second", evo=2)
    with kbc.connect_closing() as conn:
        _seed(conn, evo=3)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 3}, reconcile_orphans=False,
        )
    assert _by(res.spawned) == {"evo": 1}
    assert _by(res.skipped_per_profile_capped) == {"evo": 2}


def test_scalar_per_profile_cap_is_host_wide_too(kanban_home):
    kb.create_board("second")
    _run_on("second", coder=2)
    with kbc.connect_closing() as conn:
        _seed(conn, coder=2, claude=1)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=2, reconcile_orphans=False,
        )
    assert _by(res.spawned) == {"claude": 1}
    assert _by(res.skipped_per_profile_capped) == {"coder": 2}


def test_unreadable_other_board_fails_open(kanban_home, monkeypatch):
    kb.create_board("second")
    _run_on("second", evo=5)
    real_connect = kbc.connect

    def flaky_connect(*args, **kwargs):
        if kwargs.get("board") == "second":
            raise RuntimeError("database disk image is malformed")
        return real_connect(*args, **kwargs)

    with kbc.connect_closing() as conn:
        _seed(conn, evo=2)
        monkeypatch.setattr(kbc, "connect", flaky_connect)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 1}, reconcile_orphans=False,
        )
    # The broken board is skipped, not fatal: this board alone has headroom 1.
    assert _by(res.spawned) == {"evo": 1}


def test_count_running_by_assignee_host_wide_sums_boards(kanban_home):
    kb.create_board("second")
    _run_on("second", evo=2, coder=1)
    _run_on(None, evo=1)
    with kbc.connect_closing() as conn:
        assert kbd.count_running_by_assignee_host(conn) == {"evo": 3, "coder": 1}


def test_no_caps_does_not_enumerate_other_boards(kanban_home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        kbd, "count_running_by_assignee_host",
        lambda *a, **k: calls.append(1) or {},
    )
    with kbc.connect_closing() as conn:
        _seed(conn, evo=2)
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert _by(res.spawned) == {"evo": 2}
    assert calls == []


# ---------------------------------------------------------------------------
# B. Assignee groups (profiles sharing one GPU)
# ---------------------------------------------------------------------------


def test_parse_assignee_groups_normalises_and_drops_invalid():
    raw = {
        "gpu0": {"members": ["evo", " coder ", "evo"], "max": 3},
        "csv": {"members": "evo, claude", "max": "2"},
        "nomax": {"members": ["evo"]},
        "zero": {"members": ["evo"], "max": 0},
        "boolmax": {"members": ["evo"], "max": True},
        "nomembers": {"members": [], "max": 2},
        "badmembers": {"members": [1, None, ""], "max": 2},
        "": {"members": ["evo"], "max": 1},
        "notamap": ["evo"],
    }
    assert kbd.parse_assignee_groups(raw) == {
        "gpu0": {"members": ["evo", "coder"], "max": 3},
        "csv": {"members": ["evo", "claude"], "max": 2},
    }


@pytest.mark.parametrize("raw", [None, [], "gpu", 3, {"g": None}])
def test_parse_assignee_groups_never_raises(raw):
    assert kbd.parse_assignee_groups(raw) == {}


def test_group_cap_limits_members_together(kanban_home):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=2, coder=2, claude=2)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            assignee_groups={"gpu0": {"members": ["evo", "coder"], "max": 2}},
        )
    spawned = _by(res.spawned)
    assert spawned.get("evo", 0) + spawned.get("coder", 0) == 2
    assert spawned["claude"] == 2
    assert len(res.skipped_per_profile_capped) == 2
    assert {d["scope"] for d in res.capped_details} == {"group:gpu0"}
    assert {d["cap"] for d in res.capped_details} == {2}
    assert {d["running"] for d in res.capped_details} == {2}


def test_group_running_is_host_wide(kanban_home):
    kb.create_board("second")
    _run_on("second", evo=1)
    _run_on(None, coder=1)
    with kbc.connect_closing() as conn:
        _seed(conn, evo=1, coder=1)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True, reconcile_orphans=False,
            assignee_groups={"gpu0": {"members": ["evo", "coder"], "max": 2}},
        )
    assert res.spawned == []
    assert len(res.skipped_per_profile_capped) == 2


def test_own_cap_and_group_cap_both_apply(kanban_home):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=3, coder=3)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 1},
            assignee_groups={"gpu0": {"members": ["evo", "coder"], "max": 3}},
        )
    spawned = _by(res.spawned)
    assert spawned["evo"] == 1
    assert spawned["evo"] + spawned["coder"] == 3
    scopes = {(d["assignee"], d["scope"]) for d in res.capped_details}
    assert ("evo", "assignee") in scopes
    assert ("coder", "group:gpu0") in scopes


def test_run_daemon_forwards_assignee_groups_and_reloads_live(kanban_home, monkeypatch):
    calls: list = []
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        calls.append(dict(kwargs))
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _write_kanban_cfg(
        kanban_home,
        "kanban:\n  assignee_groups:\n    gpu0:\n      members: [evo, coder]\n      max: 4\n",
    )

    def on_tick(res):
        if len(calls) == 1:
            _write_kanban_cfg(
                kanban_home,
                "kanban:\n  assignee_groups:\n    gpu0:\n      members: [evo]\n      max: 2\n"
                "    broken: {members: []}\n",
            )
        else:
            stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)
    assert calls[0]["assignee_groups"] == {"gpu0": {"members": ["evo", "coder"], "max": 4}}
    assert calls[1]["assignee_groups"] == {"gpu0": {"members": ["evo"], "max": 2}}


def test_gateway_settings_carry_assignee_groups_into_dispatch_once(kanban_home, monkeypatch):
    from gateway import kanban_watchers_dispatcher as kwd

    settings = kwd._resolve_dispatcher_settings(
        {"assignee_groups": {"gpu0": {"members": ["evo"], "max": 1}, "bad": 7}}, kb,
    )
    assert settings.assignee_groups == {"gpu0": {"members": ["evo"], "max": 1}}
    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kwd._KanbanDispatcher(kb, settings).tick_once_for_board("default")
    assert captured["assignee_groups"] == {"gpu0": {"members": ["evo"], "max": 1}}


def test_config_defaults_declare_assignee_groups_and_capped_alert():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["assignee_groups"] == {}
    assert DEFAULT_CONFIG["kanban"]["capped_alert"] == {}


# ---------------------------------------------------------------------------
# C + D(verbose). Daemon telemetry: capped is not stuck; capped=N on the line
# ---------------------------------------------------------------------------


def _drive_cmd_daemon(monkeypatch, results, *, verbose=True):
    """Run ``_cmd_daemon --force`` with run_daemon replaced by a loop that
    feeds ``results`` to the real on_tick."""

    def fake_run_daemon(*, on_tick, **kwargs):
        for res in results:
            on_tick(res)

    monkeypatch.setattr(kbd, "run_daemon", fake_run_daemon)
    args = argparse.Namespace(
        force=True, pidfile=None, verbose=verbose, interval=0.01, max=None,
        failure_limit=kbd.DEFAULT_FAILURE_LIMIT, max_per_assignee=None,
    )
    assert kanban_ops._cmd_daemon(args) == 0


def test_capped_only_ticks_do_not_warn_dispatcher_stuck(kanban_home, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        ids = _seed(conn, evo=3)
    capped = [(tid, "evo", 4) for tid in ids]
    results = [kb.DispatchResult(skipped_per_profile_capped=list(capped)) for _ in range(8)]
    _drive_cmd_daemon(monkeypatch, results)
    err = capsys.readouterr().err
    assert "WARN dispatcher stuck" not in err


def test_genuinely_stuck_ticks_still_warn(kanban_home, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=1)
    results = [kb.DispatchResult() for _ in range(8)]
    _drive_cmd_daemon(monkeypatch, results)
    assert "WARN dispatcher stuck" in capsys.readouterr().err


def test_partially_capped_ticks_still_warn(kanban_home, monkeypatch, capsys):
    """One task capped, another spawnable task neither capped nor spawned: stuck."""
    with kbc.connect_closing() as conn:
        evo_id = _seed(conn, evo=1)[0]
        _seed(conn, coder=1)
    results = [
        kb.DispatchResult(skipped_per_profile_capped=[(evo_id, "evo", 2)]) for _ in range(8)
    ]
    _drive_cmd_daemon(monkeypatch, results)
    assert "WARN dispatcher stuck" in capsys.readouterr().err


def test_verbose_tick_line_reports_capped_by(kanban_home, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        ids = _seed(conn, evo=3, coder=1)
    res = kb.DispatchResult(
        spawned=[("x", "claude", "/tmp/x")],
        skipped_per_profile_capped=[
            (ids[0], "evo", 2), (ids[1], "evo", 2), (ids[2], "evo", 2), (ids[3], "coder", 1),
        ],
    )
    _drive_cmd_daemon(monkeypatch, [res])
    out = capsys.readouterr().out
    assert "capped=4" in out
    assert "capped_by={coder:1,evo:3}" in out


def test_verbose_tick_line_capped_zero_has_no_capped_by(kanban_home, monkeypatch, capsys):
    res = kb.DispatchResult(spawned=[("x", "claude", "/tmp/x")])
    _drive_cmd_daemon(monkeypatch, [res])
    out = capsys.readouterr().out
    assert "capped=0" in out
    assert "capped_by" not in out


# ---------------------------------------------------------------------------
# D. "capped" task events, once per wait episode
# ---------------------------------------------------------------------------


def test_capped_event_written_once_per_episode(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_stamp_attempt_base_head", lambda *a, **k: None)
    with kbc.connect_closing() as conn:
        _seed(conn, evo=3)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, max_in_progress_by_assignee={"evo": 1},
            reconcile_orphans=False,
        )
        assert _by(res.spawned) == {"evo": 1}
        deferred = [tid for (tid, _a, _c) in res.skipped_per_profile_capped]
        assert len(deferred) == 2
        for tid in deferred:
            evs = _events(conn, tid, "capped")
            assert len(evs) == 1
            assert evs[0].payload == {"assignee": "evo", "running": 1, "cap": 1, "scope": "assignee"}
        # Still waiting on the next tick: no duplicate event.
        kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, max_in_progress_by_assignee={"evo": 1},
            reconcile_orphans=False,
        )
        for tid in deferred:
            assert len(_events(conn, tid, "capped")) == 1


def test_claim_ends_the_capped_episode(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_stamp_attempt_base_head", lambda *a, **k: None)
    with kbc.connect_closing() as conn:
        _run_on(None, evo=1)
        tid = _seed(conn, evo=1)[0]
        kwargs = dict(spawn_fn=_fake_spawn, max_in_progress_by_assignee={"evo": 1},
                      reconcile_orphans=False)
        kbd.dispatch_once(conn, **kwargs)
        assert len(_events(conn, tid, "capped")) == 1
        # A claim/spawn in between ends the episode; the next defer starts a new one.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "claimed", {"lock": "x"})
        kbd.dispatch_once(conn, **kwargs)
        assert len(_events(conn, tid, "capped")) == 2


def test_capped_event_records_group_scope(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        _run_on(None, coder=2)
        tid = _seed(conn, evo=1)[0]
        kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, reconcile_orphans=False,
            assignee_groups={"gpu0": {"members": ["evo", "coder"], "max": 2}},
        )
        evs = _events(conn, tid, "capped")
    assert [e.payload for e in evs] == [
        {"assignee": "evo", "running": 2, "cap": 2, "scope": "group:gpu0"}
    ]


def test_dry_run_writes_no_capped_event(kanban_home):
    with kbc.connect_closing() as conn:
        _run_on(None, evo=1)
        tid = _seed(conn, evo=1)[0]
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 1}, reconcile_orphans=False,
        )
        assert _by(res.skipped_per_profile_capped) == {"evo": 1}
        assert _events(conn, tid, "capped") == []


# ---------------------------------------------------------------------------
# E. Long-wait alert
# ---------------------------------------------------------------------------


def _capture_cmd(out: Path) -> list[str]:
    return [
        sys.executable, "-c",
        "import sys; open(sys.argv[1], 'a').write(sys.stdin.read() + '\\n')",
        str(out),
    ]


def _backdate_capped(conn, task_id, minutes):
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'capped'",
        (int(time.time()) - int(minutes * 60), task_id),
    )
    conn.commit()


def _capped_task(conn, *, minutes, title="evo-wait", scope="group:gpu0"):
    tid = kb.create_task(conn, title=title, assignee="evo")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "capped",
                         {"assignee": "evo", "running": 2, "cap": 2, "scope": scope})
    _backdate_capped(conn, tid, minutes)
    return tid


def test_parse_capped_alert():
    assert kbd.parse_capped_alert({"after_minutes": 15, "command": ["notify", "-x"]}) == (
        15, ["notify", "-x"],
    )
    assert kbd.parse_capped_alert({"after_minutes": "5", "command": "notify --to ops"}) == (
        5, ["notify", "--to", "ops"],
    )
    for bad in (None, {}, {"after_minutes": 5}, {"command": ["x"]},
                {"after_minutes": -1, "command": ["x"]},
                {"after_minutes": True, "command": ["x"]},
                {"after_minutes": 5, "command": []},
                {"after_minutes": 5, "command": ["x", 3]}, "x"):
        assert kbd.parse_capped_alert(bad) is None


def test_long_capped_wait_runs_command_once_per_episode(kanban_home, tmp_path):
    out = tmp_path / "alert.jsonl"
    with kbc.connect_closing() as conn:
        tid = _capped_task(conn, minutes=20)
        fresh = _capped_task(conn, minutes=1, title="fresh")
        alerted = kbd.check_capped_alerts(
            conn, board="default", after_minutes=10, command=_capture_cmd(out),
        )
        assert alerted == [tid]
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["board"] == "default"
        assert len(payload["alerts"]) == 1
        alert = payload["alerts"][0]
        assert alert["task_id"] == tid
        assert alert["title"] == "evo-wait"
        assert alert["assignee"] == "evo"
        assert alert["scope"] == "group:gpu0"
        assert 19 <= alert["waiting_minutes"] <= 21
        assert len(_events(conn, tid, "capped_alerted")) == 1
        assert _events(conn, fresh, "capped_alerted") == []
        # Same episode: never again.
        assert kbd.check_capped_alerts(
            conn, board="default", after_minutes=10, command=_capture_cmd(out),
        ) == []
        assert len(out.read_text().strip().splitlines()) == 1


def test_alerted_task_still_capped_does_not_start_new_episode(kanban_home, tmp_path):
    out = tmp_path / "alert.jsonl"
    with kbc.connect_closing() as conn:
        _run_on(None, evo=2)
        tid = _capped_task(conn, minutes=30)
        kbd.check_capped_alerts(conn, board="default", after_minutes=10,
                                command=_capture_cmd(out))
        # Next dispatch tick still defers it: the episode continues, so no new
        # "capped" event and no second alert.
        kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, reconcile_orphans=False,
            assignee_groups={"gpu0": {"members": ["evo"], "max": 2}},
        )
        assert len(_events(conn, tid, "capped")) == 1
        assert kbd.check_capped_alerts(conn, board="default", after_minutes=10,
                                       command=_capture_cmd(out)) == []


def test_new_episode_can_alert_again(kanban_home, tmp_path):
    out = tmp_path / "alert.jsonl"
    with kbc.connect_closing() as conn:
        tid = _capped_task(conn, minutes=30)
        kbd.check_capped_alerts(conn, board="default", after_minutes=10,
                                command=_capture_cmd(out))
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "claimed", {"lock": "x"})
            kb._append_event(conn, tid, "capped",
                             {"assignee": "evo", "running": 2, "cap": 2, "scope": "assignee"})
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE id = "
            "(SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'capped')",
            (int(time.time()) - 15 * 60, tid),
        )
        conn.commit()
        assert kbd.check_capped_alerts(conn, board="default", after_minutes=10,
                                       command=_capture_cmd(out)) == [tid]
    assert len(out.read_text().strip().splitlines()) == 2


def test_task_no_longer_ready_is_not_alerted(kanban_home, tmp_path):
    out = tmp_path / "alert.jsonl"
    with kbc.connect_closing() as conn:
        tid = _capped_task(conn, minutes=30)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        conn.commit()
        assert kbd.check_capped_alerts(conn, board="default", after_minutes=10,
                                       command=_capture_cmd(out)) == []
    assert not out.exists()


@pytest.mark.parametrize("command", [
    ["/nonexistent/hermes-alert-binary"],
    [sys.executable, "-c", "import sys; sys.exit(3)"],
])
def test_failing_alert_command_never_raises_and_marks_episode(kanban_home, command):
    with kbc.connect_closing() as conn:
        tid = _capped_task(conn, minutes=30)
        assert kbd.check_capped_alerts(
            conn, board="default", after_minutes=10, command=command,
        ) == [tid]
        assert len(_events(conn, tid, "capped_alerted")) == 1


def test_alert_command_timeout_is_bounded(kanban_home, monkeypatch):
    import subprocess

    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(kbd.subprocess, "run", fake_run)
    with kbc.connect_closing() as conn:
        tid = _capped_task(conn, minutes=30)
        assert kbd.check_capped_alerts(
            conn, board="default", after_minutes=10, command=["slow"],
        ) == [tid]
    assert seen["timeout"] == 30


def test_run_daemon_runs_capped_alert_from_config(kanban_home, monkeypatch, tmp_path):
    out = tmp_path / "alert.jsonl"
    with kbc.connect_closing() as conn:
        tid = _capped_task(conn, minutes=30)
    stop = threading.Event()
    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kw: kb.DispatchResult())
    cmd = json.dumps(_capture_cmd(out))
    _write_kanban_cfg(
        kanban_home, f"kanban:\n  capped_alert:\n    after_minutes: 10\n    command: {cmd}\n",
    )
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda res: stop.set())
    payload = json.loads(out.read_text().strip().splitlines()[0])
    assert [a["task_id"] for a in payload["alerts"]] == [tid]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_kanban_cfg(home: Path, body: str) -> None:
    cfg = home / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    future = time.time() + 5 + len(body)
    os.utime(cfg, (future, future))


def test_cli_dispatch_forwards_config_groups_and_reports_scope(kanban_home, capsys):
    _write_kanban_cfg(
        kanban_home,
        "kanban:\n  assignee_groups:\n    gpu0:\n      members: [evo, coder]\n      max: 1\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, evo=1, coder=1)
    args = argparse.Namespace(
        dry_run=True, json=True, max=None, failure_limit=kbd.DEFAULT_FAILURE_LIMIT,
        max_per_assignee=None,
    )
    assert kanban_ops._cmd_dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["spawned"]) == 1
    assert [d["scope"] for d in out["capped_details"]] == ["group:gpu0"]
