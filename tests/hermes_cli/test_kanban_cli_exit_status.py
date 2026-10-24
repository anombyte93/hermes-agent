"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_create_blocked_requires_and_surfaces_reason(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    missing = _run_hermes(
        home, "kanban", "create", "blocked probe",
        "--initial-status", "blocked", "--json",
    )
    assert missing.returncode == 2
    assert "--block-reason is required" in missing.stderr

    created = _run_hermes(
        home, "kanban", "create", "blocked probe",
        "--initial-status", "blocked",
        "--block-reason", "Waiting for access",
        "--json",
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    assert payload["status"] == "blocked"
    assert payload["block_reason"] == "Waiting for access"

    listed = _run_hermes(home, "kanban", "list", "--json")
    assert listed.returncode == 0, listed.stderr
    listed_task = next(task for task in json.loads(listed.stdout) if task["id"] == payload["id"])
    assert listed_task["status"] == "blocked"
    assert listed_task["block_reason"] == "Waiting for access"

    shown = _run_hermes(home, "kanban", "show", payload["id"])
    assert shown.returncode == 0, shown.stderr
    assert "blocked:   Waiting for access" in shown.stdout


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "exit status probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home,
        "kanban",
        "comment",
        task_id,
        "must be refused",
        marker=True,
    )

    assert refused.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in refused.stderr
