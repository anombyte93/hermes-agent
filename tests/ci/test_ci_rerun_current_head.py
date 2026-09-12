"""Tests for the CI stale-head rerun and aggregate-gate helpers.

Two defects captured in ISSUE60.md:

1. ``label-rerun`` captured the PR head when ``ci-reviewed`` landed, waited
   for the run, then reran the old SHA even after a new commit arrived —
   cancelling CI for the newer commit. The rerun helper re-reads the live
   head and refuses to rerun a stale one.

2. The ``all-checks-pass`` aggregate gate only failed on ``failure``. A
   ``cancelled`` required job read as success, so a merge could be
   authorised on work that never ran. The gate helper treats ``cancelled``
   as blocking while still passing ``success`` and ``skipped``.

The helpers live under ``.github/scripts/`` and are imported directly, so
these tests exercise the exact modules the workflows run — not a copy of
their logic. ``_mock_gh`` fakes the ``gh`` command boundary; every call the
helper makes is recorded so a test can assert what really ran.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_rerun = _load("rerun_current_head", ".github/scripts/rerun_current_head.py")
_gate = _load("evaluate_gate", ".github/scripts/evaluate_gate.py")


def _mock_gh(
    monkeypatch,
    head_stdout: str | None = None,
    head_raises: bool = False,
    rerun_raises: bool = False,
):
    """Fake ``subprocess.run`` at the ``gh`` command boundary.

    Routes ``gh pr view`` to the configured head read and ``gh run rerun``
    to a success (or failure). Records every call in the returned list so
    tests assert on the exact commands the helper issued.
    """
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[1] == "pr" and args[2] == "view":
            if head_raises:
                raise _rerun.subprocess.CalledProcessError(1, args)
            stdout = (head_stdout or "").rstrip("\n") + "\n"
            return _rerun.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args[1] == "run" and args[2] == "rerun":
            if rerun_raises:
                raise _rerun.subprocess.CalledProcessError(1, args)
            return _rerun.subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(_rerun.subprocess, "run", fake_run)
    return calls


def _ran_rerun(calls) -> bool:
    return any(c[1] == "run" and c[2] == "rerun" for c in calls)


# ─── Rerun helper ────────────────────────────────────────────────────────


def test_same_head_reruns_failed_jobs(monkeypatch):
    """The positive control: an unchanged head permits the rerun."""
    calls = _mock_gh(monkeypatch, head_stdout="abc123")

    outcome, live = _rerun.rerun_if_head_unchanged("owner/repo", "5", "abc123", "42")

    assert outcome == "rerun"
    assert live == "abc123"
    assert _ran_rerun(calls), "a completed run for the same head is rerun"


def test_changed_head_makes_no_rerun_call(monkeypatch):
    """A new commit during the wait must not rerun the stale SHA (the defect)."""
    calls = _mock_gh(monkeypatch, head_stdout="newsha")

    outcome, live = _rerun.rerun_if_head_unchanged("owner/repo", "5", "oldsha", "42")

    assert outcome == "superseded"
    assert live == "newsha"
    assert not _ran_rerun(calls), "changed head: zero rerun calls"


def test_api_failure_never_reruns(monkeypatch):
    """When the live head cannot be read, refuse to rerun blind (the defect)."""
    calls = _mock_gh(monkeypatch, head_raises=True)

    outcome, live = _rerun.rerun_if_head_unchanged("owner/repo", "5", "oldsha", "42")

    assert outcome == "api-error"
    assert live is None
    assert not _ran_rerun(calls), "API failure: zero rerun calls"


def test_rerun_failure_is_explicit(monkeypatch):
    """A failed rerun call is reported, never swallowed."""
    calls = _mock_gh(monkeypatch, head_stdout="abc123", rerun_raises=True)

    outcome, live = _rerun.rerun_if_head_unchanged("owner/repo", "5", "abc123", "42")

    assert outcome == "rerun-failed"
    assert live == "abc123"
    assert _ran_rerun(calls)


def test_rerun_main_exit_codes(monkeypatch):
    """superseded → 2, api-error → 1, rerun-failed → 1, success → 0."""
    codes = {
        "rerun": 0,
        "superseded": 2,
        "api-error": 1,
        "rerun-failed": 1,
    }
    for outcome, code in codes.items():
        monkeypatch.setattr(
            _rerun,
            "rerun_if_head_unchanged",
            lambda *a, _o=outcome: (_o, "sha"),
        )
        assert _rerun.main(
            ["--repo", "r", "--pr", "5", "--captured-head", "c", "--run-id", "42"]
        ) == code


# ─── Aggregate gate helper ───────────────────────────────────────────────


def _needs(**results) -> dict:
    return {name: {"result": result} for name, result in results.items()}


def test_gate_all_success_passes():
    compact, blocking = _gate.evaluate(_needs(detect="success", tests="success"))
    assert blocking == []
    assert compact == {"detect": "success", "tests": "success"}


def test_gate_failure_blocks():
    compact, blocking = _gate.evaluate(_needs(detect="success", tests="failure"))
    assert blocking == ["tests"]


def test_gate_cancelled_blocks():
    """A cancelled required job must block the merge (ISSUE60 hard half)."""
    compact, blocking = _gate.evaluate(_needs(detect="success", tests="cancelled"))
    assert blocking == ["tests"]


def test_gate_skipped_is_not_blocking():
    """An intentional path-filter skip is not a defect."""
    compact, blocking = _gate.evaluate(_needs(detect="success", e2e="skipped"))
    assert blocking == []


def test_gate_cancelled_blocks_while_skips_and_success_pass():
    needs = _needs(
        detect="success",
        tests="cancelled",
        e2e="skipped",
        lint="success",
    )
    compact, blocking = _gate.evaluate(needs)
    assert blocking == ["tests"]
    assert "e2e" not in blocking
    assert "lint" not in blocking
    assert "detect" not in blocking


def test_gate_main_exit_nonzero_on_cancelled(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_needs(tests="cancelled"))))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert _gate.main([]) == 1
    assert "cancelled" in capsys.readouterr().out


def test_gate_main_exit_zero_on_success_and_skips(monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(_needs(detect="success", e2e="skipped"))),
    )
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert _gate.main([]) == 0


# ─── Wiring: the workflows run these helpers, not a copy ────────────────


def _yaml(rel: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((_ROOT / rel).read_text(encoding="utf-8"))


def test_label_rerun_workflow_calls_the_rerun_helper():
    doc = _yaml(".github/workflows/label-rerun.yml")
    step = next(
        s for job in doc["jobs"].values() for s in job["steps"]
        if isinstance(s.get("run"), str) and "rerun_current_head.py" in s["run"]
    )
    # The helper owns the head recheck + rerun; the workflow no longer
    # issues the direct `gh run rerun --failed` command on its own.
    assert "gh run rerun --failed" not in step["run"]
    assert "--captured-head" in step["run"]
    assert "--run-id" in step["run"]


def test_ci_gate_workflow_calls_the_evaluate_helper():
    doc = _yaml(".github/workflows/ci.yaml")
    run = next(
        s["run"] for s in doc["jobs"]["all-checks-pass"]["steps"]
        if isinstance(s.get("run"), str) and "evaluate_gate.py" in s["run"]
    )
    assert "evaluate_gate.py" in run
    # The aggregate logic moved out of the YAML into the helper.
    assert "sys.exit(1)" not in run
