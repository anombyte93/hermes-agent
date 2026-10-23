"""Board-scope dispatch caps come from the default profile's config (#124).

``hermes kanban dispatch`` read its concurrency caps from ``load_config()``,
which resolves the invoking (sticky) profile's config.yaml — so a dispatch run
without ``--profile default`` ignored ``kanban.max_in_progress_by_assignee``
and over-spawned. A board's caps describe the board's shared capacity and must
hold whichever profile runs a dispatcher. The six board-scope keys therefore
come from the DEFAULT profile's EFFECTIVE config (defaults + file + env
expansion + managed overlay, one read per resolution) on every dispatch path:
the CLI ``dispatch`` verb, the standalone daemon (re-read every tick), the
gateway's embedded dispatcher (re-read every tick) and the dashboard nudge.
An unreadable default config keeps the last good caps (in-process, else the
default profile's last-known-good backup) and otherwise refuses to spawn; it
never falls back to the invoking profile's caps.
"""
from __future__ import annotations

import argparse
import importlib.util
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
def sticky_profile_home(tmp_path, monkeypatch):
    """A hermes root seen the way issue #124 reproduces it.

    ``HERMES_HOME`` points at the ``evo`` profile (a sticky-profile dispatch),
    while the board and the default profile's config.yaml live at the root.
    """
    root = tmp_path / ".hermes"
    for prof in ("evo", "kimi"):
        (root / "profiles" / prof).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "evo"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Host memory must not shape the spawn budget in these tests.
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "unknown")
    kb.init_db()
    return root


def _write_cfg(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    # Push mtime past any coarse-grained cache resolution.
    future = time.time() + 5 + len(body)
    os.utime(path, (future, future))


def _seed(conn, *, running: int, ready: int, assignee: str = "evo") -> None:
    for i in range(running):
        tid = kb.create_task(conn, title=f"{assignee}-running{i}", assignee=assignee)
        assert kb.claim_task(conn, tid) is not None
    for i in range(ready):
        kb.create_task(conn, title=f"{assignee}-ready{i}", assignee=assignee)


def _dispatch_args(**overrides):
    base = dict(
        dry_run=True, json=False, max=None, max_per_assignee=None,
        failure_limit=kbd.DEFAULT_FAILURE_LIMIT,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _deferred(out: str, assignee: str, cap: int, running: int) -> int:
    return out.count(f"Deferred ({assignee} at per-profile cap {cap}, {running} running):")


def _spawned_for(out: str, assignee: str) -> int:
    return sum(1 for line in out.splitlines() if f"->  {assignee}  @" in line)


# ---------------------------------------------------------------------------
# Acceptance (#124) — strengthened per review r1 non-blocking item 3
# ---------------------------------------------------------------------------


def test_dispatch_caps_come_from_default_profile_config(sticky_profile_home, capsys):
    """#124 acceptance: default-profile config caps evo at 2 with 2 evo tasks
    running and 2 ready; a dispatch from the sticky profile (whose config has
    no kanban caps) must defer BOTH evo cards, warn about the differing
    configs, and name the cap source in the dry-run summary.

    A second assignee (kimi: 2 running, 1 ready, no named cap) with global
    headroom must still spawn: that separates the named per-assignee cap from
    a scalar cap of 2 wrongly applied to every assignee.
    """
    root = sticky_profile_home
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    _write_cfg(root / "profiles" / "evo" / "config.yaml", "model: local\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        _seed(conn, running=2, ready=1, assignee="kimi")
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert _deferred(out.out, "evo", 2, 2) == 2, f"both evo cards must defer:\n{out.out}"
    assert _spawned_for(out.out, "evo") == 0, out.out
    assert _spawned_for(out.out, "kimi") == 1, (
        f"kimi has no named cap and global headroom; it must spawn:\n{out.out}"
    )
    assert "Spawned:      1" in out.out, out.out
    # One-line warning on stderr naming both configs' values.
    assert "profile caps differ from default profile caps" in out.err, out.err
    assert "max_in_progress_by_assignee" in out.err, out.err
    # The dry-run summary names where each cap came from.
    assert "Caps source: default profile config" in out.out, out.out
    assert str(root / "config.yaml") in out.out, out.out


# ---------------------------------------------------------------------------
# Finding 1 — the managed overlay is honoured
# ---------------------------------------------------------------------------


def test_managed_overlay_caps_win_over_default_profile_file(
    sticky_profile_home, tmp_path, monkeypatch, capsys,
):
    """Raw default config ``evo: 10`` plus a managed-scope ``evo: 2`` must
    dispatch under the managed ``2``, exactly as ``load_config()`` resolves it."""
    root = sticky_profile_home
    managed = tmp_path / "managed"
    _write_cfg(managed / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    evo: 10\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    managed_scope.invalidate_managed_cache()
    assert _spawned_for(out.out, "evo") == 0, f"managed cap 2 must hold:\n{out.out}"
    assert _deferred(out.out, "evo", 2, 2) == 2, out.out


# ---------------------------------------------------------------------------
# Finding 2 — the standalone daemon re-reads default-profile caps every tick
# ---------------------------------------------------------------------------


def _drive_real_daemon(monkeypatch, on_call, *, max_per_assignee=None, max_n=None):
    """Run ``_cmd_daemon --force`` through the REAL ``run_daemon`` loop with a
    capturing ``dispatch_once``; ``on_call(n, kwargs)`` returns True to stop."""
    calls: list = []
    stop = threading.Event()
    real_run_daemon = kbd.run_daemon

    def fake_dispatch_once(conn, **kwargs):
        calls.append(dict(kwargs))
        if on_call(len(calls), kwargs):
            stop.set()
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kbd, "run_daemon", lambda **kw: real_run_daemon(stop_event=stop, **kw))
    args = argparse.Namespace(
        force=True, interval=0.01, max=max_n, failure_limit=kbd.DEFAULT_FAILURE_LIMIT,
        max_per_assignee=max_per_assignee, pidfile=None, verbose=False,
    )
    assert kanban_ops._cmd_daemon(args) == 0
    return calls


def test_cmd_daemon_reloads_default_profile_caps_every_tick(sticky_profile_home, monkeypatch):
    """Through the actual command: default config ``evo: 2`` then ``evo: 1``
    must reach dispatch as ``[2, 1]`` — config caps are never frozen as CLI
    overrides."""
    root = sticky_profile_home
    cfg = root / "config.yaml"
    _write_cfg(cfg, "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")

    def on_call(n, _kwargs):
        if n == 1:
            _write_cfg(cfg, "kanban:\n  max_in_progress_by_assignee:\n    evo: 1\n")
            return False
        return True

    calls = _drive_real_daemon(monkeypatch, on_call)
    assert [c["max_in_progress_by_assignee"].get("evo") for c in calls[:2]] == [2, 1]


def test_cmd_daemon_explicit_max_per_assignee_still_wins(sticky_profile_home, monkeypatch):
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 3\n    kimi: 6\n")
    calls = _drive_real_daemon(monkeypatch, lambda n, kw: True, max_per_assignee=["evo=1"])
    assert calls[0]["max_in_progress_by_assignee"] == {"evo": 1, "kimi": 6}


# ---------------------------------------------------------------------------
# Finding 3 — all six capacity keys, on every dispatch path
# ---------------------------------------------------------------------------

_DEFAULT_ALL_CAPS = (
    "kanban:\n"
    "  default_assignee: evo\n"
    "  max_in_progress: 1\n"
    "  max_in_progress_per_profile: 3\n"
    "  max_spawn: 4\n"
    "  max_in_progress_by_assignee:\n    evo: 2\n"
    "  assignee_groups:\n    gpu0:\n      members: [evo, kimi]\n      max: 1\n"
)
_INVOKING_OTHER_CAPS = (
    "kanban:\n"
    "  max_in_progress: 8\n"
    "  max_in_progress_by_assignee:\n    kimi: 9\n"
)
_EXPECTED_ALL_CAPS = {
    "default_assignee": "evo",
    "max_in_progress": 1,
    "max_in_progress_per_profile": 3,
    "max_spawn": 4,
    "max_in_progress_by_assignee": {"evo": 2},
    "assignee_groups": {"gpu0": {"members": ["evo", "kimi"], "max": 1}},
}


def _assert_all_caps(kwargs: dict) -> None:
    got = {k: kwargs.get(k) for k in _EXPECTED_ALL_CAPS}
    assert got == _EXPECTED_ALL_CAPS


def test_daemon_takes_all_six_caps_from_default_profile(sticky_profile_home, monkeypatch):
    """Global, scalar, group, spawn and default-assignee limits come from the
    default profile too, and the invoking profile's named caps do not merge in."""
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", _DEFAULT_ALL_CAPS)
    _write_cfg(root / "profiles" / "evo" / "config.yaml", _INVOKING_OTHER_CAPS)
    calls = _drive_real_daemon(monkeypatch, lambda n, kw: True)
    _assert_all_caps(calls[0])


def test_gateway_dispatcher_takes_all_six_caps_from_default_and_reloads(
    sticky_profile_home, monkeypatch,
):
    """The gateway's embedded dispatcher built its settings from the invoking
    profile once at boot; each tick must apply the default profile's caps."""
    from gateway import kanban_watchers_dispatcher as kwd

    root = sticky_profile_home
    _write_cfg(root / "config.yaml", _DEFAULT_ALL_CAPS)
    _write_cfg(root / "profiles" / "evo" / "config.yaml", _INVOKING_OTHER_CAPS)
    from hermes_cli.config import load_config
    settings = kwd._resolve_dispatcher_settings(load_config()["kanban"], kb)
    captured: list = []
    monkeypatch.setattr(
        kwd._kbd(), "dispatch_once",
        lambda conn, **kw: (captured.append(dict(kw)), kb.DispatchResult())[1],
    )
    dispatcher = kwd._KanbanDispatcher(kb, settings)
    dispatcher.tick_once()
    _assert_all_caps(captured[-1])
    _write_cfg(root / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 1\n")
    dispatcher.tick_once()
    assert captured[-1]["max_in_progress_by_assignee"] == {"evo": 1}
    assert captured[-1]["assignee_groups"] == {}


def _load_dashboard_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_caps_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dashboard_dispatch_nudge_applies_default_profile_caps(sticky_profile_home, monkeypatch):
    pytest.importorskip("fastapi")
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", _DEFAULT_ALL_CAPS)
    _write_cfg(root / "profiles" / "evo" / "config.yaml", _INVOKING_OTHER_CAPS)
    plugin = _load_dashboard_plugin()
    captured: list = []
    monkeypatch.setattr(
        plugin.kbd, "dispatch_once",
        lambda conn, **kw: (captured.append(dict(kw)), kb.DispatchResult())[1],
    )
    plugin.dispatch(dry_run=True, max_n=4, board=None)
    _assert_all_caps(captured[-1])


# ---------------------------------------------------------------------------
# Finding 4 — one read per resolution; a read error is never empty caps
# ---------------------------------------------------------------------------


def test_default_config_broken_after_first_read_keeps_its_caps(
    sticky_profile_home, monkeypatch, capsys,
):
    """The default config parses on its first read, then every later read of
    that file fails (an edit landing mid-resolution). The caps from the read
    that succeeded must be the ones enforced — never ``{}`` with a clean
    provenance and no warning."""
    import hermes_cli.config as cfgmod

    root = sticky_profile_home
    default_cfg = root / "config.yaml"
    _write_cfg(
        default_cfg,
        "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    real_load = cfgmod.fast_safe_load
    reads = {"n": 0}

    def flaky_load(stream, *a, **k):
        if Path(getattr(stream, "name", "")) == default_cfg:
            reads["n"] += 1
            if reads["n"] > 1:
                raise cfgmod.yaml.YAMLError("mid-edit")
        return real_load(stream, *a, **k)

    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
    real_dispatch_once = kbd.dispatch_once
    reads_before_dispatch: list = []

    def counting_dispatch_once(conn, **kwargs):
        reads_before_dispatch.append(reads["n"])
        return real_dispatch_once(conn, **kwargs)

    monkeypatch.setattr(kbd, "dispatch_once", counting_dispatch_once)
    monkeypatch.setattr(cfgmod, "fast_safe_load", flaky_load)
    assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert reads_before_dispatch == [1], (
        f"cap resolution must read the default config once, read {reads_before_dispatch}"
    )
    assert _spawned_for(out.out, "evo") == 0, out.out
    assert _deferred(out.out, "evo", 2, 2) == 2, out.out


# ---------------------------------------------------------------------------
# Finding 5 — unreadable default: keep last good caps, else refuse to spawn
# ---------------------------------------------------------------------------


def test_unreadable_default_with_uncapped_invoker_refuses_to_spawn(sticky_profile_home, capsys):
    """No last good caps exist (fresh process, no backup) and the invoking
    profile sets no caps: spawning is refused, never run uncapped."""
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", "kanban: [unclosed\n")
    _write_cfg(root / "profiles" / "evo" / "config.yaml", "model: local\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        rc = kanban_ops._cmd_dispatch(_dispatch_args())
    out = capsys.readouterr()
    assert rc == 1, out
    assert "Spawned:      0" in out.out, out.out
    assert "unreadable" in out.err, out.err
    assert "refusing to spawn" in out.err, out.err
    assert "invoking profile" not in out.err.split("unreadable", 1)[1], out.err


def test_unreadable_default_keeps_in_process_last_good_caps(sticky_profile_home, capsys):
    root = sticky_profile_home
    cfg = root / "config.yaml"
    _write_cfg(cfg, "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    evo: 2\n")
    _write_cfg(root / "profiles" / "evo" / "config.yaml", "model: local\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
    assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    capsys.readouterr()
    _write_cfg(cfg, "kanban: [unclosed\n")
    assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert _spawned_for(out.out, "evo") == 0, out.out
    assert _deferred(out.out, "evo", 2, 2) == 2, out.out
    assert "keeping the last good board caps" in out.err, out.err


def test_unreadable_default_recovers_default_profiles_backup(sticky_profile_home, capsys):
    """A fresh process recovers the default profile's last-known-good backup
    (the copy the normal config loader keeps under ``backups/config/``)."""
    root = sticky_profile_home
    backup = root / "backups" / "config" / "config.yaml.good.20261023-000000"
    _write_cfg(backup, "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    evo: 2\n")
    _write_cfg(root / "config.yaml", "kanban: [unclosed\n")
    _write_cfg(root / "profiles" / "evo" / "config.yaml", "model: local\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert _spawned_for(out.out, "evo") == 0, out.out
    assert _deferred(out.out, "evo", 2, 2) == 2, out.out
    assert "last-known-good backup" in out.out, out.out


# ---------------------------------------------------------------------------
# Non-blocking 1 — equivalent representations are not a mismatch
# ---------------------------------------------------------------------------


def test_identical_env_templated_caps_do_not_warn(sticky_profile_home, monkeypatch, capsys):
    root = sticky_profile_home
    monkeypatch.setenv("H124_EVO_CAP", "2")
    body = "kanban:\n  max_in_progress_by_assignee:\n    evo: ${H124_EVO_CAP}\n"
    _write_cfg(root / "config.yaml", body)
    _write_cfg(root / "profiles" / "evo" / "config.yaml", body)
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=1)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert "differ" not in out.err, out.err
    assert _deferred(out.out, "evo", 2, 2) == 1, out.out


# ---------------------------------------------------------------------------
# Non-blocking 2 — the summary reports effective values and their real sources
# ---------------------------------------------------------------------------


def test_dry_run_summary_reports_effective_caps_and_sources(sticky_profile_home, capsys):
    root = sticky_profile_home
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_spawn: 5\n  max_in_progress: 20\n"
        "  max_in_progress_by_assignee:\n    evo: 2\n    kimi: 4\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        assert kanban_ops._cmd_dispatch(
            _dispatch_args(max=3, max_per_assignee=["evo=1"])
        ) == 0
    out = capsys.readouterr().out
    assert "  max_spawn: 3 (--max)" in out, out
    assert "  max_in_progress: 20 (default profile config)" in out, out
    assert "  max_in_progress_by_assignee.evo: 1 (--max-per-assignee)" in out, out
    assert "  max_in_progress_by_assignee.kimi: 4 (default profile config)" in out, out
