"""Kanban workers must carry ``no_new_privs`` (#195).

A brief line ("No sudo, ever") does not bind the model or the code it runs: a
card worker ran ``sudo -n true`` as root on 3 Nov 2026. The fix is a mechanism
— ``_default_spawn`` wraps every worker argv in ``setpriv --no-new-privs --``
so the worker and everything it execs carries the kernel flag and a setuid
binary (sudo, su, pkexec) cannot raise privileges.

Both kanban spawn routes exec in place — the direct ``in_process`` argv and
``systemd-run --user --scope`` (whose client execs the worker from the same
Popen child) — so one outer wrap covers both. The kernel proof reads
``/proc/<pid>/status``; no test ever invokes sudo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _make_task(task_id: str = "t_nnp_195") -> kb.Task:
    return kb.Task(
        id=task_id,
        title="no_new_privs probe",
        body=None,
        assignee="coder",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def _fake_hermes_bin(tmp_path: Path, receipt_env: str, receipt_path: Path) -> Path:
    """A ``hermes`` on PATH that records its own NoNewPrivs status line."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "hermes"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib\n"
        "status = pathlib.Path('/proc/self/status').read_text()\n"
        "line = next(ln for ln in status.splitlines() if ln.startswith('NoNewPrivs:'))\n"
        f"pathlib.Path(os.environ[{receipt_env!r}]).write_text(line + '\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    assert receipt_path.parent.exists()
    return bin_dir


@pytest.fixture
def synthetic_spawn_env(monkeypatch, tmp_path):
    """os.environ exactly as the dispatcher sees it, with the fake hermes first
    on PATH and a receipt location the fake hermes writes to."""
    receipt = tmp_path / "receipt" / "nnp.txt"
    receipt.parent.mkdir()
    bin_dir = _fake_hermes_bin(tmp_path, "FAKE_HERMES_RECEIPT", receipt)
    root = tmp_path / "hermes-home"
    root.mkdir()
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(os, "environ", {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "HERMES_HOME": str(root),
        "FAKE_HERMES_RECEIPT": str(receipt),
    })
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env", lambda _name: str(root),
    )
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    return {"receipt": receipt, "workspace": workspace, "bin_dir": bin_dir}


# ---------------------------------------------------------------------------
# The in-process route (standalone dispatcher / non-managed gateway)
# ---------------------------------------------------------------------------

@pytest.mark.linux_only
def test_default_spawn_wraps_worker_argv_with_setpriv(monkeypatch, tmp_path):
    """The spawn argv asks for the flag: setpriv --no-new-privs -- outermost.

    Red at base: ``_default_spawn`` passed the worker argv straight to Popen,
    so nothing requested no_new_privs (#195).
    """
    captured = {}

    class FakeProc:
        pid = 4301

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = _make_task()
    kbd._default_spawn(task, str(tmp_path / "ws"))

    cmd = captured["cmd"]
    from tools.process_registry import _SETPRIV_CANDIDATES

    # D2: the binary comes from the fixed system candidates, never PATH.
    assert cmd[0] in _SETPRIV_CANDIDATES, cmd
    assert cmd[:3] == [cmd[0], "--no-new-privs", "--"], cmd
    # The worker argv survives unchanged after the wrap separator.
    inner = cmd[3:]
    assert inner[0].endswith("/hermes") or inner[0] == "hermes", inner
    assert "-p" in inner and "coder" in inner


@pytest.mark.linux_only
def test_real_worker_child_carries_the_kernel_flag(synthetic_spawn_env):
    """A real child spawned through _default_spawn reads NoNewPrivs: 1.

    E2E kernel proof through the real Popen/exec path (a fake ``hermes`` on
    PATH writes its own /proc/self/status line). Under the setpriv-wrapped
    test runner a base child also reads 1 (inherited from pytest), so the
    base-red proof for the mechanism is the argv contract test above; this
    test pins the end-to-end behaviour at the fixed head.
    """
    task = _make_task("t_nnp_real_child")
    pid = kbd._default_spawn(task, str(synthetic_spawn_env["workspace"]))
    assert pid

    receipt = synthetic_spawn_env["receipt"]
    deadline = time.monotonic() + 20
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert receipt.exists(), "fake hermes never wrote its NoNewPrivs receipt"
    assert "NoNewPrivs:\t1" in receipt.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The systemd route (managed gateway: systemd-run --user --scope)
# ---------------------------------------------------------------------------

@pytest.mark.linux_only
def test_scoped_route_keeps_setpriv_outside_systemd_run(monkeypatch, tmp_path):
    """The scope form execs in place, so the wrap sits OUTSIDE systemd-run and
    the flag reaches the worker through exec inheritance.

    Red at base: the argv started at ``systemd-run`` with no setpriv wrap.
    """
    captured = {}

    class FakeProc:
        pid = 4302

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    which_map = {
        "systemd-run": "/usr/bin/systemd-run",
        "setpriv": "/usr/bin/setpriv",
    }
    monkeypatch.setattr("shutil.which", lambda name: which_map.get(name))
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("INVOCATION_ID", "managed-gateway-test")
    monkeypatch.setattr(
        "tools.process_registry._is_supervised_gateway_process", lambda: True,
    )
    monkeypatch.setattr(
        "tools.process_registry._systemd_run_user_scope_available", lambda: True,
    )

    kbd._default_spawn(_make_task(), str(tmp_path / "ws"))

    cmd = captured["cmd"]
    # setpriv outermost: the flag covers systemd-run AND the worker it execs.
    assert cmd[:3] == ["/usr/bin/setpriv", "--no-new-privs", "--"], cmd
    assert cmd[3:7] == ["/usr/bin/systemd-run", "--user", "--scope", "--quiet"], cmd
    unit_index = cmd.index("--unit")
    assert cmd[unit_index + 1] == "hermes-worker-kanban-t_nnp_195-run-7"
    # The worker argv still follows the systemd-run separator.
    separator = len(cmd) - 1 - cmd[::-1].index("--")
    assert cmd[separator + 1: separator + 4] == ["hermes", "-p", "coder"], cmd


# ---------------------------------------------------------------------------
# Fail closed + platform guard
# ---------------------------------------------------------------------------

@pytest.mark.linux_only
def test_spawn_fails_closed_when_setpriv_is_missing(monkeypatch, tmp_path):
    """No trusted setpriv on a Linux host -> RuntimeError, no child started.

    The candidates are a fixed system list, so "missing" is proved by
    monkeypatching the list itself — no PATH trick can make this pass.
    """
    popen_calls: list[list[str]] = []

    def fail_popen(cmd, **kwargs):
        popen_calls.append(list(cmd))
        raise AssertionError("an unflagged worker must never be launched")

    monkeypatch.setattr("tools.process_registry._SETPRIV_CANDIDATES", [])
    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(RuntimeError, match="no_new_privs"):
        kbd._default_spawn(_make_task(), str(tmp_path / "ws"))
    assert popen_calls == []


@pytest.mark.linux_only
def test_path_shim_is_not_used_and_real_flag_is_set(synthetic_spawn_env, monkeypatch):
    """D2: a setpriv shim planted FIRST on PATH must not be used.

    Red at the pre-D2 head: the binary was resolved with a bare-name PATH
    lookup, so a worker could plant a shim that strips --no-new-privs and
    every later worker would spawn unflagged, with no error (gen-17
    defect 2). The shim writes a marker if it runs; the marker must be
    absent and the real kernel flag must still reach the worker.
    """
    shim_dir = synthetic_spawn_env["workspace"] / "shim-bin"
    shim_dir.mkdir()
    marker = synthetic_spawn_env["workspace"] / "shim-ran.txt"
    shim = shim_dir / "setpriv"
    shim.write_text(
        "#!/bin/sh\n"
        f"printf 'ran\\n' > {marker}\n"
        "shift; shift; exec \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    # The shim sits FIRST on the dispatcher's PATH, before /usr/bin.
    monkeypatch.setattr(
        os,
        "environ",
        {
            **os.environ,
            "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        },
    )

    task = _make_task("t_nnp_shim")
    pid = kbd._default_spawn(task, str(synthetic_spawn_env["workspace"]))
    assert pid

    receipt = synthetic_spawn_env["receipt"]
    deadline = time.monotonic() + 20
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert receipt.exists(), "fake hermes never wrote its NoNewPrivs receipt"
    assert not marker.exists(), "a PATH shim must never be used as setpriv"
    assert "NoNewPrivs:\t1" in receipt.read_text(encoding="utf-8")


@pytest.mark.linux_only
def test_spawn_fails_closed_when_candidate_fails_ownership_check(monkeypatch, tmp_path):
    """D2: an untrusted setpriv candidate (not owned by root) makes the spawn
    raise; nothing is launched.

    Red at the pre-D2 head: no ownership check existed, so the existing
    /usr/bin/setpriv was used blindly and the spawn proceeded. The stat is
    monkeypatched — nothing is ever chowned.
    """
    import tools.process_registry as pr

    candidate = "/usr/bin/setpriv"
    real_lstat = os.lstat

    def untrusted_lstat(path, *args, **kwargs):
        if str(path) == candidate:
            # Regular executable file owned by uid 1000, not root.
            return os.stat_result((0o100755, 0, 1000, 1000, 1, 1, 47200, 1, 1, 1))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(pr, "_SETPRIV_CANDIDATES", [candidate])
    monkeypatch.setattr(os, "lstat", untrusted_lstat)
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **kw: (_ for _ in ()).throw(AssertionError("spawned"))
    )
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with pytest.raises(RuntimeError, match="no_new_privs"):
        kbd._default_spawn(_make_task(), str(tmp_path / "ws"))


def test_non_linux_platform_spawns_exactly_as_today(monkeypatch, tmp_path):
    """With the module platform flag off, the spawn argv is the base form."""
    from tools import process_registry

    captured = {}

    class FakeProc:
        pid = 4303

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(process_registry, "_IS_LINUX", False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    kbd._default_spawn(_make_task(), str(tmp_path / "ws"))

    cmd = captured["cmd"]
    assert "setpriv" not in " ".join(token for token in cmd if "--" not in token), cmd
    assert cmd[:3] == ["hermes", "-p", "coder"], cmd
