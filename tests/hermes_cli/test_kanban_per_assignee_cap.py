"""Per-assignee concurrency caps for the dispatcher (anombyte93/hermes-agent#83).

``kanban.max_in_progress_per_profile`` is one number for every profile, so it
cannot say "the local single-GPU profile gets 2, the cloud profiles fan out".
Measured 2026-10-09: five ``evo`` (gpt-oss-120b) cards dispatched together onto
a 4-slot llama-server decoded at 3.6-7 tok/s instead of 37 and all five timed
out, while ``coder`` (cloud) cards on the same board were unaffected.

Also covered: the standalone daemon (``hermes kanban daemon --force``) never
forwarded ANY per-profile cap, and the caps re-resolve from config every tick
so an operator can change them without restarting the daemon (which is what
the issue asked for in the first place).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for prof in ("evo", "coder", "claude"):
        (home / "profiles" / prof).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    return 12345


def _seed(conn, **counts):
    for assignee, n in counts.items():
        for i in range(n):
            kb.create_task(conn, title=f"{assignee}{i}", assignee=assignee)


def _by(res_list):
    out: dict = {}
    for entry in res_list:
        out[entry[1]] = out.get(entry[1], 0) + 1
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_assignee_caps_accepts_mapping_and_cli_pairs():
    assert kbd.parse_assignee_caps({"evo": 2, "coder": "5"}) == {"evo": 2, "coder": 5}
    assert kbd.parse_assignee_caps(["evo=2", " coder = 4 "]) == {"evo": 2, "coder": 4}
    # Later CLI pairs win over earlier ones for the same name.
    assert kbd.parse_assignee_caps(["evo=2", "evo=1"]) == {"evo": 1}


@pytest.mark.parametrize("raw", [
    None, {}, [], "evo=2", {"evo": 0}, {"evo": -1}, {"evo": "x"}, {"": 2},
    ["evo"], ["=2"], ["evo=0"], ["evo=two"], {"evo": True},
])
def test_parse_assignee_caps_drops_invalid_entries(raw):
    assert kbd.parse_assignee_caps(raw) == {}


# ---------------------------------------------------------------------------
# dispatch_once
# ---------------------------------------------------------------------------


def test_named_cap_limits_one_assignee_and_leaves_others_uncapped(kanban_home):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=5, coder=4)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 2},
        )
    assert _by(res.spawned) == {"evo": 2, "coder": 4}
    assert _by(res.skipped_per_profile_capped) == {"evo": 3}


def test_named_cap_overrides_the_scalar_per_profile_cap(kanban_home):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=3, coder=4, claude=3)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=2,
            max_in_progress_by_assignee={"evo": 1, "coder": 3},
        )
    # evo tighter than the scalar, coder looser, claude falls back to the scalar.
    assert _by(res.spawned) == {"evo": 1, "coder": 3, "claude": 2}


def test_already_running_workers_count_against_the_named_cap(kanban_home):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=4)
        ids = [r["id"] for r in conn.execute("SELECT id FROM tasks ORDER BY created_at")]
        for tid in ids[:2]:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        conn.commit()
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_by_assignee={"evo": 2},
            reconcile_orphans=False,
        )
    assert _by(res.spawned).get("evo", 0) == 0
    assert _by(res.skipped_per_profile_capped) == {"evo": 2}


def test_no_caps_configured_is_unchanged_behaviour(kanban_home):
    with kbc.connect_closing() as conn:
        _seed(conn, evo=3, coder=2)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert _by(res.spawned) == {"evo": 3, "coder": 2}
    assert res.skipped_per_profile_capped == []


# ---------------------------------------------------------------------------
# Standalone daemon: forwards caps and re-resolves them every tick
# ---------------------------------------------------------------------------


def _write_kanban_cfg(home: Path, body: str) -> None:
    cfg = home / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    # Bump mtime past any coarse-grained cache resolution.
    future = time.time() + 5 + len(body)
    os.utime(cfg, (future, future))


def test_run_daemon_forwards_config_caps_and_reloads_them_live(kanban_home, monkeypatch):
    calls: list = []
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        calls.append(dict(kwargs))
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _write_kanban_cfg(kanban_home, "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")

    def on_tick(res):
        if len(calls) == 1:
            _write_kanban_cfg(
                kanban_home,
                "kanban:\n  max_in_progress_per_profile: 4\n"
                "  max_in_progress_by_assignee:\n    evo: 1\n",
            )
        else:
            stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert calls[0]["max_in_progress_by_assignee"] == {"evo": 2}
    assert calls[0]["max_in_progress_per_profile"] is None
    assert calls[1]["max_in_progress_by_assignee"] == {"evo": 1}
    assert calls[1]["max_in_progress_per_profile"] == 4


def test_run_daemon_cli_caps_win_per_name_over_config(kanban_home, monkeypatch):
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _write_kanban_cfg(
        kanban_home, "kanban:\n  max_in_progress_by_assignee:\n    evo: 3\n    coder: 6\n",
    )
    kbd.run_daemon(
        interval=0.01, stop_event=stop, on_tick=lambda res: stop.set(),
        max_in_progress_by_assignee={"evo": 2},
    )
    assert captured["max_in_progress_by_assignee"] == {"evo": 2, "coder": 6}


def test_run_daemon_reads_board_max_spawn_live_when_no_cli_max(kanban_home, monkeypatch):
    calls: list = []
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        calls.append(dict(kwargs))
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _write_kanban_cfg(kanban_home, "kanban:\n  max_spawn: 8\n")

    def on_tick(res):
        if len(calls) == 1:
            _write_kanban_cfg(kanban_home, "kanban:\n  max_spawn: 3\n")
        else:
            stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)
    assert [c["max_spawn"] for c in calls[:2]] == [8, 3]


def test_run_daemon_explicit_cli_max_is_not_overridden_by_config(kanban_home, monkeypatch):
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    _write_kanban_cfg(kanban_home, "kanban:\n  max_spawn: 3\n")
    kbd.run_daemon(interval=0.01, max_spawn=8, stop_event=stop, on_tick=lambda res: stop.set())
    assert captured["max_spawn"] == 8


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_daemon_and_dispatch_accept_max_per_assignee_flag():
    ns = _parse_kanban(["daemon", "--force", "--max-per-assignee", "evo=2",
                        "--max-per-assignee", "claude=1"])
    assert ns.max_per_assignee == ["evo=2", "claude=1"]
    ns = _parse_kanban(["dispatch", "--dry-run", "--max-per-assignee", "evo=2"])
    assert ns.max_per_assignee == ["evo=2"]


def _parse_kanban(argv):
    import argparse
    from hermes_cli import kanban_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subs = parser.add_subparsers(dest="command")
    kanban_parser.build_parser(subs)
    return parser.parse_args(["kanban", *argv])
