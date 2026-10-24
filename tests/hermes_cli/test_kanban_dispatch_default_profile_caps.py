"""Board-scope dispatch caps come from the default profile's config (#124).

``hermes kanban dispatch`` read its concurrency caps from ``load_config()``,
which resolves the invoking (sticky) profile's config.yaml — so a dispatch run
without ``--profile default`` ignored ``kanban.max_in_progress_by_assignee``
and over-spawned. A board's caps describe the board's shared capacity and must
hold whichever profile runs a dispatcher. The declared rule (see
``hermes_cli/kanban_board_caps.py``), on every dispatch path (the CLI
``dispatch`` verb, the standalone daemon, the gateway's embedded dispatcher and
the dashboard nudge), re-resolved on every call:

1. the six board-cap keys from the DEFAULT profile's raw config.yaml, then the
   managed-scope overlay (managed wins per entry);
2. literal values only, each entry or group validated on its own: an invalid
   one (``${...}``, ``null``, ``{}``, a string int) is dropped and its key
   named, every valid entry (managed or user) is kept;
3. a missing default config.yaml is an empty user layer (managed still applies);
4. an unreadable/unparseable default config.yaml refuses spawning (no cache,
   no backup recovery, never the invoking profile's caps);
5. integer caps are nonnegative ints and ``0`` stays ``0`` (scalars, named
   assignee caps and group ``max`` alike);
6. warnings name keys only, never a value read from any profile;
7. managed provenance only where a valid managed value contributed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
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
    # A test may drive the daemon twice: always wrap the real loop, never a wrapper.
    real_run_daemon = getattr(kbd.run_daemon, "_real", kbd.run_daemon)

    def fake_dispatch_once(conn, **kwargs):
        calls.append(dict(kwargs))
        if on_call(len(calls), kwargs):
            stop.set()
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    def run_daemon(**kw):
        return real_run_daemon(stop_event=stop, **kw)

    run_daemon._real = real_run_daemon
    monkeypatch.setattr(kbd, "run_daemon", run_daemon)
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
# Rule 4 — an unreadable default config refuses to spawn (fail closed)
# ---------------------------------------------------------------------------


def test_unreadable_default_with_uncapped_invoker_refuses_to_spawn(sticky_profile_home, capsys):
    """An unparseable default config refuses to spawn; it never runs on the
    invoking profile's (absent) caps. r2 P2-4 (removal): the backup recovery
    path is gone, so a last-known-good backup under ``backups/config/`` is
    not consulted either."""
    root = sticky_profile_home
    backup = root / "backups" / "config" / "config.yaml.good.20261023-000000"
    _write_cfg(backup, "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    evo: 2\n")
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
    assert "backup" not in out.out + out.err, out


# ---------------------------------------------------------------------------
# Every dispatch path, captured: what each one hands ``dispatch_once``
# ---------------------------------------------------------------------------


@pytest.fixture
def managed_dir(tmp_path, monkeypatch):
    """A managed scope directory (``HERMES_MANAGED_DIR``), caches cleared."""
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    yield managed
    managed_scope.invalidate_managed_cache()


def _kwargs_on_every_path(monkeypatch) -> dict:
    """``{path: kwargs}`` each dispatcher passes to ``dispatch_once`` right now:
    the CLI verb, the standalone daemon, the gateway's per-tick refresh and
    the dashboard nudge (when FastAPI is installed)."""
    from gateway import kanban_watchers_dispatcher as kwd

    captured: list = []

    def capture(conn, **kw):
        captured.append(dict(kw))
        return kb.DispatchResult()

    out: dict = {}
    monkeypatch.setattr(kbd, "dispatch_once", capture)
    monkeypatch.setattr(kwd._kbd(), "dispatch_once", capture)
    kanban_ops._cmd_dispatch(_dispatch_args())
    out["cli"] = captured[-1]
    out["daemon"] = _drive_real_daemon(monkeypatch, lambda n, kw: True)[0]
    monkeypatch.setattr(kwd._kbd(), "dispatch_once", capture)
    kwd._KanbanDispatcher(kb, kwd._resolve_dispatcher_settings({}, kb)).tick_once()
    out["gateway"] = captured[-1]
    try:
        import fastapi  # noqa: F401
    except ImportError:
        return out
    plugin = _load_dashboard_plugin()
    monkeypatch.setattr(plugin.kbd, "dispatch_once", capture)
    plugin.dispatch(dry_run=True, max_n=None, board=None)  # ``max`` omitted
    out["dashboard"] = captured[-1]
    return out


# ---------------------------------------------------------------------------
# Review r2 P2-4 — no last-good cache: a broken default refuses on every path
# ---------------------------------------------------------------------------


def test_malformed_default_refuses_on_every_path_even_after_a_good_read(
    sticky_profile_home, managed_dir, monkeypatch, capsys,
):
    """r2 P2-4: managed ``evo: 2`` resolves once, then the default YAML breaks
    and managed policy tightens to ``evo: 1``. Head kept forwarding the cached
    ``2`` and kept spawning. Now nothing is cached: every path refuses to
    spawn (``max_spawn=0``) until the file is fixed."""
    root = sticky_profile_home
    _write_cfg(managed_dir / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")
    _write_cfg(root / "config.yaml", "kanban:\n  max_in_progress: 20\n")
    first = _kwargs_on_every_path(monkeypatch)
    assert {p: kw["max_in_progress_by_assignee"] for p, kw in first.items()} == {
        p: {"evo": 2} for p in first
    }
    _write_cfg(managed_dir / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 1\n")
    _write_cfg(root / "config.yaml", "kanban: [unclosed\n")
    capsys.readouterr()
    after = _kwargs_on_every_path(monkeypatch)
    assert {p: kw["max_spawn"] for p, kw in after.items()} == {p: 0 for p in after}
    err = capsys.readouterr().err
    assert "unreadable" in err and "refusing to spawn" in err, err


# ---------------------------------------------------------------------------
# Review r2 P1-1 — a missing default config is an empty user layer + managed
# ---------------------------------------------------------------------------


def test_missing_default_config_still_applies_managed_caps_on_every_path(
    sticky_profile_home, managed_dir, monkeypatch, capsys,
):
    """r2 P1-1: no default config.yaml in a fresh process. Head treated that as
    empty caps and dropped managed ``evo: 2`` on all four paths."""
    root = sticky_profile_home
    assert not (root / "config.yaml").exists()
    _write_cfg(managed_dir / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")
    got = _kwargs_on_every_path(monkeypatch)
    assert {p: kw["max_in_progress_by_assignee"] for p, kw in got.items()} == {
        p: {"evo": 2} for p in got
    }
    assert all(kw["max_spawn"] != 0 for p, kw in got.items()), got


# ---------------------------------------------------------------------------
# Review r2 P1-2 — literal values only: ${VAR} is never expanded
# ---------------------------------------------------------------------------


def test_env_reference_in_board_cap_is_ignored_whatever_the_invokers_env(
    sticky_profile_home, monkeypatch, capsys,
):
    """r2 P1-2: the same ``${CAP}`` yielded ``evo: 2`` or ``evo: 10`` depending
    on the invoker's environment. A ``${VAR}`` board-cap key is now invalid:
    warned and ignored, so every path resolves the same caps under any env."""
    root = sticky_profile_home
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n  default_assignee: ${H124_WHO}\n"
        "  max_in_progress_by_assignee:\n    evo: ${H124_CAP}\n",
    )
    seen = []
    for cap in ("2", "10"):
        monkeypatch.setenv("H124_CAP", cap)
        monkeypatch.setenv("H124_WHO", f"who{cap}")
        capsys.readouterr()
        got = _kwargs_on_every_path(monkeypatch)
        seen.append({
            p: (kw["max_in_progress_by_assignee"], kw["default_assignee"], kw["max_in_progress"])
            for p, kw in got.items()
        })
        err = capsys.readouterr().err
        assert "max_in_progress_by_assignee" in err and "${" in err, err
        assert "default_assignee" in err, err
    assert seen[0] == seen[1], seen
    assert all(v == ({}, None, 20) for v in seen[0].values()), seen


# ---------------------------------------------------------------------------
# Review r2 P1-3 — one integer parser: 0 stays 0 on every path
# ---------------------------------------------------------------------------


def test_zero_integer_caps_stay_zero_on_every_path(sticky_profile_home, monkeypatch):
    """r2 P1-3: the gateway refresh turned ``max_spawn: 0`` into ``None``
    (uncapped). ``max_spawn``, ``max_in_progress`` and
    ``max_in_progress_per_profile`` share one parser that keeps ``0``."""
    root = sticky_profile_home
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_spawn: 0\n  max_in_progress: 0\n  max_in_progress_per_profile: 0\n",
    )
    got = _kwargs_on_every_path(monkeypatch)
    for path, kw in got.items():
        # r3 P1-2: an omitted dashboard ``max`` is no override; ``0`` holds there too.
        assert (kw["max_spawn"], kw["max_in_progress"], kw["max_in_progress_per_profile"]) == (
            0, 0, 0,
        ), (path, kw)


def test_gateway_refresh_zero_max_spawn_refuses_with_global_headroom(sticky_profile_home):
    """r2 P1-3, the reviewer's case: ``max_spawn: 0`` with global headroom of 20
    must refuse spawning through the gateway refresh, not permit 20."""
    from gateway import kanban_watchers_dispatcher as kwd

    root = sticky_profile_home
    _write_cfg(root / "config.yaml", "kanban:\n  max_spawn: 0\n  max_in_progress: 20\n")
    dispatcher = kwd._KanbanDispatcher(kb, kwd._resolve_dispatcher_settings({}, kb))
    dispatcher.refresh_board_caps()
    assert dispatcher.settings.max_spawn == 0
    with kbc.connect_closing() as conn:
        may_spawn, _budget = kbd._tick_spawn_budget(
            conn, kb.DispatchResult(), max_spawn=dispatcher.settings.max_spawn,
            max_in_progress=dispatcher.settings.max_in_progress, board=None,
        )
    assert may_spawn is False


def test_zero_per_profile_cap_defers_every_assignee(sticky_profile_home, capsys):
    """``max_in_progress_per_profile: 0`` means no profile may start a worker;
    it must not read as "no per-profile cap"."""
    root = sticky_profile_home
    _write_cfg(root / "config.yaml", "kanban:\n  max_in_progress: 20\n  max_in_progress_per_profile: 0\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert "Spawned:      0" in out, out
    assert _deferred(out, "evo", 0, 0) == 1, out


# ---------------------------------------------------------------------------
# Review r2 P2-5 — gateway boot seeds sanitized caps (boot-to-tick)
# ---------------------------------------------------------------------------


def test_gateway_boot_to_tick_seeds_sanitized_default_profile_caps(sticky_profile_home, monkeypatch):
    """r2 P2-5: ``default_assignee: 123`` in the default config (which the
    common parser ignores) crashed the gateway bootstrap with
    ``AttributeError`` before the loop started. Boot must seed the sanitized
    ``dispatch_caps()`` values (never the gateway's own capacity keys) and the
    first tick must dispatch with them."""
    import asyncio

    from gateway import kanban_watchers as kw
    from gateway import kanban_watchers_dispatcher as kwd

    root = sticky_profile_home
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  default_assignee: 123\n  max_spawn: 0\n  max_in_progress: 20\n"
        "  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    _write_cfg(
        root / "profiles" / "evo" / "config.yaml",
        "kanban:\n  auto_decompose: false\n  dispatch_interval_seconds: 1\n"
        "  default_assignee: kimi\n  max_spawn: 9\n"
        "  max_in_progress_by_assignee:\n    kimi: 9\n",
    )
    captured: list = []
    boot_settings: list = []
    monkeypatch.setattr(
        kwd._kbd(), "dispatch_once",
        lambda conn, **k: (captured.append(dict(k)), kb.DispatchResult())[1],
    )
    monkeypatch.setattr(kwd._kbd(), "reap_worker_zombies", lambda: [])

    async def inline(fn, *args):
        return fn(*args)

    monkeypatch.setattr(kw, "_to_thread_process_service", inline)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(kw.asyncio, "sleep", lambda _d: real_sleep(0))

    class RecordingDispatcher(kwd._KanbanDispatcher):
        def __init__(self, kb_mod, settings):
            boot_settings.append(settings)
            super().__init__(kb_mod, settings)

    monkeypatch.setattr(kw, "_KanbanDispatcher", RecordingDispatcher)

    class Runner(kw.GatewayKanbanWatchersMixin):
        _running = True

        async def _sleep_between_ticks(self, interval):
            self._running = False

    asyncio.run(asyncio.wait_for(Runner()._kanban_dispatcher_watcher(), timeout=30))
    assert boot_settings, "the dispatcher loop never started"
    seeded = boot_settings[0]
    assert (seeded.default_assignee, seeded.max_spawn, seeded.max_in_progress) == (None, 0, 20)
    assert seeded.max_in_progress_by_assignee == {"evo": 2}
    assert seeded.interval == 1.0  # runtime keys still come from the gateway's own profile
    assert captured, "the first tick never dispatched"
    first = captured[0]
    assert (first["default_assignee"], first["max_spawn"], first["max_in_progress"]) == (None, 0, 20)
    assert first["max_in_progress_by_assignee"] == {"evo": 2}


# ---------------------------------------------------------------------------
# Non-blocking — the summary reports effective values and their real sources
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


def test_dry_run_summary_labels_managed_values_managed(sticky_profile_home, managed_dir, capsys):
    """r2 non-blocking: managed-overlay values were labelled "default profile
    config". Each value now carries its own provenance."""
    root = sticky_profile_home
    _write_cfg(
        managed_dir / "config.yaml",
        "kanban:\n  max_spawn: 3\n  max_in_progress_by_assignee:\n    evo: 2\n",
    )
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_spawn: 5\n  max_in_progress: 20\n"
        "  max_in_progress_by_assignee:\n    evo: 9\n    kimi: 4\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert "  max_spawn: 3 (managed scope)" in out, out
    assert "  max_in_progress: 20 (default profile config)" in out, out
    assert "  max_in_progress_by_assignee.evo: 2 (managed scope)" in out, out
    assert "  max_in_progress_by_assignee.kimi: 4 (default profile config)" in out, out


# ---------------------------------------------------------------------------
# Review r3 P1-1 — an invalid entry is dropped on its own; valid ones stay
# ---------------------------------------------------------------------------


def _warning() -> str:
    from hermes_cli import kanban_board_caps as kbcaps

    return kbcaps.read_board_caps().warning or ""


def test_reference_entry_keeps_valid_managed_and_user_assignee_caps(
    sticky_profile_home, managed_dir, monkeypatch, capsys,
):
    """r3 P1-1, the reviewer's case: user ``cloud: ${CAP}`` plus managed
    ``evo: 2`` resolved to ``{}``, so a saturated evo spawned. Only ``cloud``
    is invalid: managed ``evo: 2`` and user ``kimi: 4`` stay on every path,
    the CLI defers both evo cards, and the warning names only the key."""
    root = sticky_profile_home
    monkeypatch.setenv("H124_CAP", "7")
    _write_cfg(managed_dir / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n"
        "  max_in_progress_by_assignee:\n    cloud: ${H124_CAP}\n    kimi: 4\n",
    )
    got = _kwargs_on_every_path(monkeypatch)
    assert {p: kw["max_in_progress_by_assignee"] for p, kw in got.items()} == {
        p: {"evo": 2, "kimi": 4} for p in got
    }
    warning = _warning()
    assert "max_in_progress_by_assignee.cloud" in warning, warning
    assert "H124_CAP" not in warning, warning


def test_reference_entry_does_not_unblock_a_saturated_managed_cap(
    sticky_profile_home, managed_dir, capsys,
):
    """The production consequence of r3 P1-1: evo is at its managed cap of 2,
    so both ready evo cards must defer despite the user's ``${CAP}`` entry."""
    root = sticky_profile_home
    _write_cfg(managed_dir / "config.yaml", "kanban:\n  max_in_progress_by_assignee:\n    evo: 2\n")
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n  max_in_progress_by_assignee:\n    cloud: ${H124_CAP}\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=2, ready=2)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert _spawned_for(out, "evo") == 0, out
    assert _deferred(out, "evo", 2, 2) == 2, out


def test_reference_group_keeps_valid_user_and_managed_groups(
    sticky_profile_home, managed_dir, monkeypatch,
):
    """A ``${...}`` member invalidates that one group; the user's valid group
    and the managed group both stay."""
    root = sticky_profile_home
    _write_cfg(
        managed_dir / "config.yaml",
        "kanban:\n  assignee_groups:\n    gpu2:\n      members: [qwen]\n      max: 1\n",
    )
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  assignee_groups:\n"
        "    gpu0:\n      members: [evo, kimi]\n      max: 1\n"
        "    gpu1:\n      members:\n        - evo\n        - ${H124_WHO}\n      max: 2\n",
    )
    got = _kwargs_on_every_path(monkeypatch)
    assert {p: kw["assignee_groups"] for p, kw in got.items()} == {
        p: {
            "gpu0": {"members": ["evo", "kimi"], "max": 1},
            "gpu2": {"members": ["qwen"], "max": 1},
        }
        for p in got
    }
    warning = _warning()
    assert "assignee_groups.gpu1" in warning and "H124_WHO" not in warning, warning


def test_reference_in_managed_group_keeps_valid_managed_and_user_groups(
    sticky_profile_home, managed_dir, monkeypatch,
):
    """r3 P1-1 for managed groups: a managed ``${...}`` group max is dropped
    on its own; the managed valid group and the user's group both stay."""
    root = sticky_profile_home
    monkeypatch.setenv("H124_GMAX", "9")
    _write_cfg(
        managed_dir / "config.yaml",
        "kanban:\n  assignee_groups:\n"
        "    gpu1:\n      members: [kimi]\n      max: ${H124_GMAX}\n"
        "    gpu2:\n      members: [qwen]\n      max: 1\n",
    )
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  assignee_groups:\n    gpu0:\n      members: [evo]\n      max: 1\n",
    )
    got = _kwargs_on_every_path(monkeypatch)
    assert {p: kw["assignee_groups"] for p, kw in got.items()} == {
        p: {
            "gpu0": {"members": ["evo"], "max": 1},
            "gpu2": {"members": ["qwen"], "max": 1},
        }
        for p in got
    }
    warning = _warning()
    assert "assignee_groups.gpu1" in warning, warning
    assert "H124_GMAX" not in warning, warning


def test_invalid_managed_scalar_keeps_the_valid_user_scalar(
    sticky_profile_home, managed_dir, monkeypatch,
):
    """Managed ``max_spawn: ${X}`` is dropped on its own: the user's literal
    ``max_spawn: 3`` and the managed ``max_in_progress: 5`` both apply."""
    root = sticky_profile_home
    _write_cfg(
        managed_dir / "config.yaml",
        "kanban:\n  max_spawn: ${H124_SPAWN}\n  max_in_progress: 5\n",
    )
    _write_cfg(root / "config.yaml", "kanban:\n  max_spawn: 3\n")
    got = _kwargs_on_every_path(monkeypatch)
    assert {p: (kw["max_spawn"], kw["max_in_progress"]) for p, kw in got.items()} == {
        p: (3, 5) for p in got
    }
    assert "max_spawn" in _warning()


@pytest.mark.parametrize(
    "body, invalid_key",
    [
        ("  max_spawn: '3'\n", "max_spawn"),          # a string is not a literal int
        ("  max_spawn: true\n", "max_spawn"),         # nor is a bool
        ("  max_spawn: null\n", "max_spawn"),
        ("  max_in_progress_per_profile: -1\n", "max_in_progress_per_profile"),
        ("  default_assignee: ${H124_WHO}\n", "default_assignee"),
        ("  max_in_progress_by_assignee:\n    evo: null\n", "max_in_progress_by_assignee.evo"),
        ("  max_in_progress_by_assignee:\n    evo: {}\n", "max_in_progress_by_assignee.evo"),
        ("  max_in_progress_by_assignee:\n    evo: -1\n", "max_in_progress_by_assignee.evo"),
        ("  assignee_groups:\n    gpu0:\n      members: [evo]\n      max: -1\n", "assignee_groups.gpu0"),
        ("  assignee_groups:\n    gpu0: null\n", "assignee_groups.gpu0"),
        ("  assignee_groups:\n    gpu0: {}\n", "assignee_groups.gpu0"),
    ],
)
def test_each_invalid_entry_is_dropped_and_named(sticky_profile_home, body, invalid_key):
    """Literal non-negative integers only (names for ``default_assignee`` and
    group members): each invalid entry is dropped and its key named, while
    the valid ``kimi: 4`` beside it is kept."""
    from hermes_cli import kanban_board_caps as kbcaps

    root = sticky_profile_home
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n" + body.replace(
            "  max_in_progress_by_assignee:\n", "  max_in_progress_by_assignee:\n    kimi: 4\n",
        ) + ("" if "max_in_progress_by_assignee" in body
             else "  max_in_progress_by_assignee:\n    kimi: 4\n"),
    )
    caps = kbcaps.read_board_caps()
    kw = kbcaps.dispatch_caps(caps).kwargs
    assert kw["max_in_progress_by_assignee"] == {"kimi": 4}, kw
    assert kw["max_in_progress"] == 20
    assert invalid_key in (caps.warning or ""), caps.warning
    top = invalid_key.split(".")[0]
    if top in ("max_spawn", "max_in_progress_per_profile", "default_assignee"):
        assert kw[top] is None, kw
    if top == "assignee_groups":
        assert kw["assignee_groups"] == {}, kw


# ---------------------------------------------------------------------------
# Review r3 P1-2 — an omitted dashboard ``max`` is no override
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard_client(sticky_profile_home):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin = _load_dashboard_plugin()
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")
    return plugin, TestClient(app)


def test_dashboard_omitted_max_keeps_configured_zero_with_global_headroom(
    sticky_profile_home, dashboard_client,
):
    """r3 P1-2, the reviewer's case: ``max_spawn: 0`` with global headroom 20
    and no ``max`` parameter must spawn nothing (head spawned under 8)."""
    _plugin, client = dashboard_client
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_spawn: 0\n  max_in_progress: 20\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=2)
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true")
    assert r.status_code == 200, r.text
    assert r.json()["spawned"] == [], r.json()


def test_dashboard_omitted_max_uses_configured_max_spawn(sticky_profile_home, dashboard_client, monkeypatch):
    plugin, client = dashboard_client
    captured: list = []
    monkeypatch.setattr(
        plugin.kbd, "dispatch_once",
        lambda conn, **kw: (captured.append(dict(kw)), kb.DispatchResult())[1],
    )
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_spawn: 2\n  max_in_progress: 20\n")
    assert client.post("/api/plugins/kanban/dispatch").status_code == 200
    assert captured[-1]["max_spawn"] == 2
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_in_progress: 20\n")
    assert client.post("/api/plugins/kanban/dispatch").status_code == 200
    assert captured[-1]["max_spawn"] is None  # unset: no spawn cap, like the CLI


def test_dashboard_explicit_max_overrides_configured_max_spawn(sticky_profile_home, dashboard_client):
    """An explicit ``?max=`` is the operator's override, zero config included."""
    _plugin, client = dashboard_client
    _write_cfg(sticky_profile_home / "config.yaml", "kanban:\n  max_spawn: 0\n  max_in_progress: 20\n")
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=2)
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=1")
    assert r.status_code == 200, r.text
    assert len(r.json()["spawned"]) == 1, r.json()


# Review r4 P2: the nudge test exercises the real dashboard callback. The
# harness loads the shipped ``index.js`` under node with a minimal hook
# renderer and stub SDK, renders ``KanbanPage`` until its ``BoardToolbar`` is
# on screen, renders that toolbar, clicks the "Nudge dispatcher" button and
# prints every ``SDK.fetchJSON`` request the click made.
_NUDGE_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const [, , jsPath, BOARD] = process.argv;
const calls = [];
let slots = [], idx = 0, effects = [], dirty = false;
function changed(prev, deps) {
  return !prev || !deps || deps.length !== prev.length
    || deps.some(function (d, k) { return !Object.is(d, prev[k]); });
}
const hooks = {
  useState(init) {
    const i = idx++;
    if (!(i in slots)) slots[i] = typeof init === "function" ? init() : init;
    return [slots[i], function (v) {
      slots[i] = typeof v === "function" ? v(slots[i]) : v; dirty = true;
    }];
  },
  useEffect(fn, deps) {
    const i = idx++;
    if (changed(slots[i], deps)) { slots[i] = deps; effects.push(fn); }
  },
  useMemo(fn, deps) {
    const i = idx++;
    if (!slots[i] || changed(slots[i].deps, deps)) slots[i] = { deps: deps, value: fn() };
    return slots[i].value;
  },
  useCallback(fn, deps) { return hooks.useMemo(function () { return fn; }, deps); },
  useRef(init) { const i = idx++; if (!(i in slots)) slots[i] = { current: init }; return slots[i]; },
};
function createElement(type, props) {
  const children = Array.prototype.slice.call(arguments, 2);
  return { type: type, props: Object.assign({}, props || {}, { children: children }) };
}
class Component { constructor(props) { this.props = props; this.state = {}; } setState() {} }
const React = Object.assign({ createElement: createElement, Component: Component }, hooks);
const components = {};
["Card", "CardContent", "Badge", "Button", "Input", "Label", "Select", "SelectOption"]
  .forEach(function (n) { components[n] = { [n]: function () { return null; } }[n]; });
function respond(url) {
  const path = url.split("?")[0];
  if (path.endsWith("/config")) return { render_markdown: true };
  if (path.endsWith("/boards")) return { boards: [{ slug: BOARD, name: BOARD }], current: BOARD };
  if (path.endsWith("/board")) return { columns: [], tenants: [], assignees: [], latest_event_id: 0 };
  return {};
}
const SDK = {
  React: React, hooks: hooks, components: components,
  utils: { cn: function () { return ""; }, timeAgo: function () { return ""; } },
  fetchJSON: function (url, opts) {
    calls.push({ url: url, method: (opts && opts.method) || "GET" });
    return Promise.resolve(respond(url));
  },
  buildWsUrl: function () { return new Promise(function () {}); },
};
let page = null;
const store = { "hermes.kanban.selectedBoard": BOARD };
const noop = function () {};
const window = {
  __HERMES_PLUGIN_SDK__: SDK,
  __HERMES_PLUGINS__: { register: function (name, comp) { if (name === "kanban") page = comp; } },
  localStorage: {
    getItem: function (k) { return k in store ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; },
  },
  addEventListener: noop, removeEventListener: noop,
};
const ctx = {
  window: window, console: console, setTimeout: setTimeout, clearTimeout: clearTimeout,
  setInterval: setInterval, clearInterval: clearInterval, URLSearchParams: URLSearchParams,
  WebSocket: function () {},
};
vm.runInNewContext(fs.readFileSync(jsPath, "utf8"), ctx, { filename: jsPath });
if (!page) throw new Error("kanban page did not register");
function find(node, pred) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const n of node) { const hit = find(n, pred); if (hit) return hit; }
    return null;
  }
  if (pred(node)) return node;
  return find(node.props && node.props.children, pred);
}
function text(node) {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join("");
  return text(node.props && node.props.children);
}
const settle = function () { return new Promise(function (r) { setImmediate(r); }); };
(async function () {
  let toolbar = null;
  for (let pass = 0; pass < 25 && !toolbar; pass++) {
    idx = 0; effects = []; dirty = false;
    const tree = page();
    effects.forEach(function (fn) { fn(); });
    for (let k = 0; k < 5; k++) await settle();
    toolbar = find(tree, function (n) {
      return typeof n.type === "function" && n.type.name === "BoardToolbar";
    });
  }
  if (!toolbar) throw new Error("BoardToolbar never rendered");
  const button = find(toolbar.type(toolbar.props), function (n) {
    return n.type === components.Button && text(n.props.children) === "Nudge dispatcher";
  });
  if (!button) throw new Error("Nudge dispatcher button not found");
  const before = calls.length;
  button.props.onClick();
  await settle();
  process.stdout.write(JSON.stringify({ requests: calls.slice(before) }));
  process.exit(0);
})().catch(function (e) { process.stderr.write(String(e && e.stack || e)); process.exit(3); });
"""

_DASHBOARD_JS = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
_NUDGE_BOARD = "example"


def _click_nudge(js_path: Path, tmp_path: Path) -> list:
    """Every request the real "Nudge dispatcher" click sends, as
    ``[(method, path, query_dict), ...]``."""
    from urllib.parse import parse_qs, urlsplit

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the dashboard bundle")
    harness = tmp_path / "nudge_harness.js"
    harness.write_text(_NUDGE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(js_path), _NUDGE_BOARD],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = []
    for req in json.loads(proc.stdout)["requests"]:
        parts = urlsplit(req["url"])
        # keep_blank_values: a blank ``max=`` (or bare ``max``) is still a parameter
        # the endpoint parses (and FastAPI rejects), so it must not vanish here.
        out.append((req["method"], parts.path, parse_qs(parts.query, keep_blank_values=True)))
    return out


def _assert_nudge_sends_no_max(requests: list) -> None:
    posts = [r for r in requests if r[0] == "POST"]
    assert len(posts) == 1, requests
    _method, path, query = posts[0]
    assert path == "/api/plugins/kanban/dispatch", requests
    assert "max" not in query, f"nudge pinned a max override: {requests}"
    # Fail closed: the nudge sends the board and nothing else, so any other
    # parameter (``dry_run``, a misspelt cap, ...) fails without being enumerated.
    extra = sorted(set(query) - {"board"})
    assert not extra, f"unexpected parameter {extra}: {requests}"
    assert query.get("board") == [_NUDGE_BOARD], f"selected board lost: {requests}"


def test_dashboard_nudge_button_sends_no_max_override(tmp_path):
    """r4 P2: the board's "Nudge dispatcher" button, clicked in the shipped
    bundle, POSTs ``/dispatch`` with the selected board and no ``max`` so the
    configured ``max_spawn`` holds."""
    _assert_nudge_sends_no_max(_click_nudge(_DASHBOARD_JS, tmp_path))


@pytest.mark.parametrize(
    "new, why",
    [
        # The reviewer's r4 mutation: ``?max=8`` appended after the template.
        ('withBoard(`${API}/dispatch` + "?max=8", board)', "max override"),
        # The pre-#124 implementation.
        ("withBoard(`${API}/dispatch?max=8`, board)", "max override"),
        # The selected board must survive the click too.
        ("`${API}/dispatch`", "selected board lost"),
        # Review r4 (round 4 on #142): a blank ``max=`` that ``parse_qs`` drops by
        # default; FastAPI rejects the empty integer, so the click is broken.
        ('withBoard(`${API}/dispatch` + "?max=", board)', "max override"),
        # A bare ``max`` with no ``=`` is the same blank parameter.
        ('withBoard(`${API}/dispatch` + "?max", board)', "max override"),
        # Fail closed: any parameter but ``board`` is refused, not enumerated.
        ('withBoard(`${API}/dispatch` + "?dry_run=1", board)', "unexpected parameter"),
        # A blank selected board is not the selected board.
        ('withBoard(`${API}/dispatch`, "") + "?board="', "selected board lost"),
    ],
    ids=["reviewer-append-mutation", "old-max-8-implementation", "board-dropped",
         "r4-blank-max", "r4-bare-max", "r4-unexpected-param", "r4-blank-board"],
)
def test_nudge_check_catches_a_wrong_request(tmp_path, new, why):
    """Known-red controls for the test above: the same click on a bundle whose
    nudge sends ``max=8`` (or drops the board) must fail
    ``_assert_nudge_sends_no_max``."""
    old = "withBoard(`${API}/dispatch`, board)"
    src = _DASHBOARD_JS.read_text(encoding="utf-8")
    assert src.count(old) == 1, "nudge call site moved; update the control"
    mutated = tmp_path / "index.js"
    mutated.write_text(src.replace(old, new), encoding="utf-8")
    requests = _click_nudge(mutated, tmp_path)
    with pytest.raises(AssertionError, match=why):
        _assert_nudge_sends_no_max(requests)


# ---------------------------------------------------------------------------
# Review r3 P1-3 — diagnostics never carry a value read from any profile
# ---------------------------------------------------------------------------

_SYNTHETIC_PRIVATE = "h124-synthetic-private-marker"
_DEFAULT_LITERAL = "h124literalname"


def _secret_profiles(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("H124_PRIVATE", _SYNTHETIC_PRIVATE)
    _write_cfg(
        root / "config.yaml",
        f"kanban:\n  default_assignee: {_DEFAULT_LITERAL}\n  max_in_progress: 20\n"
        "  max_in_progress_by_assignee:\n    evo: ${env:H124_PRIVATE}\n    kimi: 4\n",
    )
    _write_cfg(
        root / "profiles" / "evo" / "config.yaml",
        "kanban:\n  default_assignee: ${env:H124_PRIVATE}\n  max_in_progress: 11\n"
        "  max_in_progress_by_assignee:\n    kimi: ${H124_PRIVATE}\n",
    )


def _assert_clean(text: str) -> None:
    assert _SYNTHETIC_PRIVATE not in text, text
    assert "H124_PRIVATE" not in text, text
    assert _DEFAULT_LITERAL not in text, text
    assert "invoking=" not in text and "default=" not in text, text


def test_cli_warnings_carry_key_names_only(sticky_profile_home, monkeypatch, capsys):
    """r3 P1-3: an invoking ``default_assignee: ${env:PRIVATE}`` was expanded
    and printed in the difference warning. The invoking profile is now read
    raw and warnings name keys only."""
    _secret_profiles(sticky_profile_home, monkeypatch)
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr()
    assert "profile caps differ" in out.err, out.err
    for key in ("default_assignee", "max_in_progress", "max_in_progress_by_assignee"):
        assert key in out.err, out.err
    _assert_clean(out.err)
    assert _SYNTHETIC_PRIVATE not in out.out, out.out


def test_dashboard_json_carries_key_names_only(sticky_profile_home, dashboard_client, monkeypatch):
    _secret_profiles(sticky_profile_home, monkeypatch)
    _plugin, client = dashboard_client
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "profile caps differ" in body["board_caps_warning"], body
    _assert_clean(body["board_caps_warning"])
    assert _SYNTHETIC_PRIVATE not in json.dumps(body), body


# ---------------------------------------------------------------------------
# Review r3 P2-4 — managed provenance only where a managed value contributed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", ["null", "{}"])
def test_managed_placeholders_leave_user_values_user_scoped(
    sticky_profile_home, managed_dir, capsys, placeholder,
):
    """r3 P2-4: managed ``gpu0: null`` / ``gpu0: {}`` (and the same for an
    assignee entry and a scalar) leave the user's values unchanged, so their
    provenance stays the default profile config."""
    root = sticky_profile_home
    _write_cfg(
        managed_dir / "config.yaml",
        f"kanban:\n  max_spawn: null\n  assignee_groups:\n    gpu0: {placeholder}\n"
        f"  max_in_progress_by_assignee:\n    evo: {placeholder}\n",
    )
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_spawn: 5\n  max_in_progress: 20\n"
        "  max_in_progress_by_assignee:\n    evo: 2\n"
        "  assignee_groups:\n    gpu0:\n      members: [evo, kimi]\n      max: 1\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert "  max_spawn: 5 (default profile config)" in out, out
    assert "  max_in_progress_by_assignee.evo: 2 (default profile config)" in out, out
    assert "  assignee_groups.gpu0: max 1 [evo,kimi] (default profile config)" in out, out
    assert "managed scope" not in out, out


def test_managed_partial_group_override_is_managed(sticky_profile_home, managed_dir, capsys):
    """A managed field that does contribute (``max: 1`` over the user's
    ``gpu0``) wins at the field and labels the group managed."""
    root = sticky_profile_home
    _write_cfg(managed_dir / "config.yaml", "kanban:\n  assignee_groups:\n    gpu0:\n      max: 1\n")
    _write_cfg(
        root / "config.yaml",
        "kanban:\n  max_in_progress: 20\n"
        "  assignee_groups:\n    gpu0:\n      members: [evo, kimi]\n      max: 3\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert "  assignee_groups.gpu0: max 1 [evo,kimi] (managed scope)" in out, out


# ---------------------------------------------------------------------------
# Review r4 P1 — a literal 0 named or group cap is a cap, never "uncapped"
# ---------------------------------------------------------------------------

_ZERO_NAMED_AND_GROUP = (
    "kanban:\n  max_in_progress_by_assignee:\n    evo: 0\n    kimi: 4\n"
    "  assignee_groups:\n    gpu0:\n      members: [qwen]\n      max: 0\n"
)


def _caps_layer(root: Path, managed: Path, layer: str, body: str) -> None:
    """Write ``body`` (a ``kanban:`` section) into the user or managed layer;
    the user file always carries global headroom of 20."""
    if layer == "user":
        _write_cfg(root / "config.yaml", body.replace("kanban:\n", "kanban:\n  max_in_progress: 20\n", 1))
    else:
        _write_cfg(managed / "config.yaml", body)
        _write_cfg(root / "config.yaml", "kanban:\n  max_in_progress: 20\n")


@pytest.mark.parametrize("layer", ["user", "managed"])
def test_zero_named_and_group_caps_survive_every_path(
    sticky_profile_home, managed_dir, monkeypatch, layer,
):
    """r4 P1, the reviewer's probe: ``max_in_progress_by_assignee: {evo: 0}``
    and a group ``max: 0`` were dropped in both layers. Both now reach
    ``dispatch_once`` as ``0`` on every path, unwarned."""
    _caps_layer(sticky_profile_home, managed_dir, layer, _ZERO_NAMED_AND_GROUP)
    got = _kwargs_on_every_path(monkeypatch)
    assert {p: (kw["max_in_progress_by_assignee"], kw["assignee_groups"]) for p, kw in got.items()} == {
        p: ({"evo": 0, "kimi": 4}, {"gpu0": {"members": ["qwen"], "max": 0}}) for p in got
    }
    warning = _warning()
    assert "max_in_progress_by_assignee.evo" not in warning, warning
    assert "assignee_groups.gpu0" not in warning, warning


@pytest.mark.parametrize("layer", ["user", "managed"])
def test_zero_named_cap_blocks_that_profile_while_others_spawn(
    sticky_profile_home, managed_dir, capsys, layer,
):
    """r4 P1 dispatch control: ``evo: 0`` with nothing running and global
    headroom 20 defers evo; kimi (no named cap) still spawns."""
    _caps_layer(
        sticky_profile_home, managed_dir, layer,
        "kanban:\n  max_in_progress_by_assignee:\n    evo: 0\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        _seed(conn, running=0, ready=1, assignee="kimi")
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert _spawned_for(out, "evo") == 0, out
    assert _deferred(out, "evo", 0, 0) == 1, out
    assert _spawned_for(out, "kimi") == 1, out
    assert "Spawned:      1" in out, out


@pytest.mark.parametrize("layer", ["user", "managed"])
def test_zero_group_max_blocks_its_members_while_others_spawn(
    sticky_profile_home, managed_dir, capsys, layer,
):
    """r4 P1 dispatch control: group ``gpu0`` (members evo) with ``max: 0``
    defers evo at the group cap; kimi (outside the group) still spawns."""
    _caps_layer(
        sticky_profile_home, managed_dir, layer,
        "kanban:\n  assignee_groups:\n    gpu0:\n      members: [evo]\n      max: 0\n",
    )
    with kbc.connect_closing() as conn:
        _seed(conn, running=0, ready=1)
        _seed(conn, running=0, ready=1, assignee="kimi")
        assert kanban_ops._cmd_dispatch(_dispatch_args()) == 0
    out = capsys.readouterr().out
    assert _spawned_for(out, "evo") == 0, out
    assert out.count("Deferred (evo at group:gpu0 cap 0, 0 running):") == 1, out
    assert _spawned_for(out, "kimi") == 1, out
    assert "Spawned:      1" in out, out
