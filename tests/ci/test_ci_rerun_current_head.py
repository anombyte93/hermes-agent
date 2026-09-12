"""Workflow-level tests for the CI stale-head rerun and the aggregate gate.

Two defects captured in ISSUE60.md:

1. ``label-rerun`` captured the PR head when ``ci-reviewed`` landed, waited
   for the run, then reran the old SHA even after a new commit arrived —
   cancelling CI for the newer commit. The fix re-reads the live PR head
   immediately before the rerun and refuses to rerun a stale one.

2. The ``all-checks-pass`` aggregate gate only failed on ``failure``. A
   ``cancelled`` required job read as success, so a merge could be
   authorised on work that never ran. The gate now treats ``cancelled`` as
   blocking while still passing ``success`` and ``skipped``.

These tests extract the EXACT ``run`` scripts from the final workflow YAML
and execute them as subprocesses. The rerun script is driven against a mock
``gh`` command boundary, so the assertions see the real commands the
workflow issues, not a string search or an uncalled duplicate of the logic.
The gate script runs the real ``.github/scripts/evaluate_gate.py`` through
the real ``echo "$NEEDS" | python3 ...`` step.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ─── YAML + helper loading ──────────────────────────────────────────────


def _yaml(rel: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def _load_gate_helper():
    path = ROOT / ".github/scripts/evaluate_gate.py"
    spec = importlib.util.spec_from_file_location("evaluate_gate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["evaluate_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


_gate = _load_gate_helper()


# ─── Mock `gh` at the command boundary ───────────────────────────────────


_MOCK_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    argv = sys.argv[1:]
    calls_file = os.environ.get("CALLS_FILE", "")
    if argv[:2] == ["run", "list"]:
        print(os.environ.get("MOCK_RUN_INFO", "42 completed"))
    elif argv[:2] == ["pr", "view"]:
        if os.environ.get("MOCK_PR_VIEW_FAIL"):
            print("mock gh: pr view failed", file=sys.stderr)
            sys.exit(1)
        print(os.environ.get("MOCK_LIVE_HEAD", os.environ.get("HEAD_SHA", "")))
    elif argv[:2] == ["run", "rerun"]:
        if calls_file:
            with open(calls_file, "a") as f:
                f.write("rerun %s\\n" % (argv[2] if len(argv) > 2 else ""))
        if os.environ.get("MOCK_RERUN_FAIL"):
            print("mock gh: rerun failed", file=sys.stderr)
            sys.exit(1)
    elif argv[:2] == ["run", "view"]:
        if "conclusion" in argv:
            print(os.environ.get("MOCK_CONCLUSION", "failure"))
        else:
            print("completed")
    elif argv[:2] == ["run", "watch"]:
        sys.exit(0)
    else:
        print("mock gh: unexpected call: %s" % argv, file=sys.stderr)
        sys.exit(99)
    """
)


def _label_rerun_run_script() -> str:
    doc = _yaml(".github/workflows/label-rerun.yml")
    steps = doc["jobs"]["rerun-review-labels"]["steps"]
    runs = [s["run"] for s in steps if isinstance(s.get("run"), str)]
    assert len(runs) == 1, "label-rerun should have exactly one run step"
    return runs[0]


def _run_label_rerun(tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    mock = tmp_path / "gh"
    mock.write_text(_MOCK_GH, encoding="utf-8")
    mock.chmod(0o755)
    calls = tmp_path / "calls"
    full_env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "REPO": "owner/test-repo",
        "PR": "5",
        "HEAD_SHA": "a" * 40,
        "CALLS_FILE": str(calls),
    }
    full_env.update(env)
    return subprocess.run(
        ["bash", "-c", _label_rerun_run_script()],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )


def _rerun_calls(tmp_path: Path) -> str:
    calls = tmp_path / "calls"
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


# ─── Rerun workflow: the extracted run script ────────────────────────────


def test_label_rerun_same_head_reruns_failed_jobs(tmp_path):
    """Positive control: an unchanged head permits the rerun (exit 0)."""
    result = _run_label_rerun(tmp_path, {"MOCK_LIVE_HEAD": "a" * 40})

    assert result.returncode == 0, result.stderr
    assert "rerun 42" in _rerun_calls(tmp_path), "the completed run is rerun"


def test_label_rerun_changed_head_is_a_successful_noop(tmp_path):
    """A new commit during the wait: superseded is a no-op, not a rerun (the defect)."""
    result = _run_label_rerun(tmp_path, {"MOCK_LIVE_HEAD": "b" * 40})

    assert result.returncode == 0, result.stderr
    assert _rerun_calls(tmp_path) == "", "changed head: zero rerun calls"
    assert "changed while waiting" in result.stdout


def test_label_rerun_unreadable_head_fails_closed(tmp_path):
    """An unreadable live head must fail the step, never rerun blind."""
    result = _run_label_rerun(tmp_path, {"MOCK_PR_VIEW_FAIL": "1"})

    assert result.returncode != 0, "API failure must propagate as a non-zero exit"
    assert _rerun_calls(tmp_path) == "", "API failure: zero rerun calls"


def test_label_rerun_rerun_call_failure_propagates(tmp_path):
    """A failed ``gh run rerun`` call must fail the step, not be swallowed."""
    result = _run_label_rerun(
        tmp_path, {"MOCK_LIVE_HEAD": "a" * 40, "MOCK_RERUN_FAIL": "1"}
    )

    assert result.returncode != 0, "rerun failure must propagate as a non-zero exit"


def test_label_rerun_no_run_is_a_noop(tmp_path):
    """No CI run for the head: nothing to rerun, a clean no-op."""
    result = _run_label_rerun(tmp_path, {"MOCK_RUN_INFO": ""})

    assert result.returncode == 0, result.stderr
    assert _rerun_calls(tmp_path) == ""
    assert "nothing to rerun" in result.stdout


# ─── Rerun workflow: wiring (the helper is gone) ────────────────────────


def test_label_rerun_has_no_checkout_and_no_helper_reference():
    """The fresh-head guard is inline; no helper bootstrap, no PR checkout."""
    doc = _yaml(".github/workflows/label-rerun.yml")
    job = doc["jobs"]["rerun-review-labels"]
    steps = job["steps"]

    assert all("uses" not in s for s in steps), (
        "label-rerun must not check out anything (trusted inline script only)"
    )
    run = _label_rerun_run_script()
    assert "rerun_current_head.py" not in run
    assert "gh pr view" in run, "the live head recheck must be inline"
    assert "gh run rerun" in run, "the rerun must be inline"


def test_label_rerun_rerun_preceded_by_head_recheck():
    """The stale-head recheck is immediately before the rerun, every time."""
    run = _label_rerun_run_script()
    # The live-head read and the rerun are the actual command lines, not the
    # comments that mention "gh run rerun only works on completed runs".
    pr_view = run.index('gh pr view "$PR"')
    rerun = run.index('gh run rerun "$RUN_ID"')
    assert pr_view < rerun
    assert "exit 0" in run[pr_view:rerun], "superseded path exits before rerun"


# ─── Aggregate gate: the extracted run script (real helper) ─────────────


def _gate_run_script() -> str:
    doc = _yaml(".github/workflows/ci.yaml")
    steps = doc["jobs"]["all-checks-pass"]["steps"]
    runs = [s["run"] for s in steps if isinstance(s.get("run"), str)]
    gate = next(r for r in runs if "evaluate_gate.py" in r)
    return gate


def _run_gate(tmp_path: Path, results: dict) -> subprocess.CompletedProcess:
    needs = json.dumps({name: {"result": r} for name, r in results.items()})
    return subprocess.run(
        ["bash", "-c", _gate_run_script()],
        env={
            "PATH": "/usr/bin:/bin",
            "NEEDS": needs,
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        },
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )


def test_gate_workflow_cancelled_blocks(tmp_path):
    """A cancelled required job must block the merge (ISSUE60 hard half)."""
    result = _run_gate(tmp_path, {"detect": "success", "tests": "cancelled"})
    assert result.returncode != 0, result.stdout


def test_gate_workflow_failure_blocks(tmp_path):
    result = _run_gate(tmp_path, {"detect": "success", "tests": "failure"})
    assert result.returncode != 0, result.stdout


def test_gate_workflow_success_passes(tmp_path):
    result = _run_gate(tmp_path, {"detect": "success", "tests": "success"})
    assert result.returncode == 0, result.stderr


def test_gate_workflow_skipped_is_permitted(tmp_path):
    """An intentional path-filter skip is not a defect."""
    result = _run_gate(tmp_path, {"detect": "success", "e2e": "skipped"})
    assert result.returncode == 0, result.stderr


def test_gate_workflow_success_and_skips_pass(tmp_path):
    result = _run_gate(
        tmp_path, {"detect": "success", "tests": "success", "e2e": "skipped"}
    )
    assert result.returncode == 0, result.stderr


# ─── Aggregate gate: direct helper units (fast, precise) ────────────────


def _needs(**results) -> dict:
    return {name: {"result": result} for name, result in results.items()}


def test_gate_evaluate_all_success_passes():
    compact, blocking = _gate.evaluate(_needs(detect="success", tests="success"))
    assert blocking == []
    assert compact == {"detect": "success", "tests": "success"}


def test_gate_evaluate_failure_blocks():
    compact, blocking = _gate.evaluate(_needs(detect="success", tests="failure"))
    assert blocking == ["tests"]


def test_gate_evaluate_cancelled_blocks():
    compact, blocking = _gate.evaluate(_needs(detect="success", tests="cancelled"))
    assert blocking == ["tests"]


def test_gate_evaluate_skipped_is_not_blocking():
    compact, blocking = _gate.evaluate(_needs(detect="success", e2e="skipped"))
    assert blocking == []


# ─── Gate wiring: ci.yaml calls the helper ──────────────────────────────


def test_ci_gate_workflow_calls_the_evaluate_helper():
    run = _gate_run_script()
    assert "evaluate_gate.py" in run
    # The aggregate logic lives in the helper, not inline python in YAML.
    assert "sys.exit(1)" not in run


def test_successful_run_needs_no_rerun(tmp_path):
    result = _run_label_rerun(tmp_path, {"MOCK_CONCLUSION": "success", "MOCK_RERUN_FAIL": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _rerun_calls(tmp_path) == ""


def test_unreadable_conclusion_cannot_authorise_rerun(tmp_path):
    result = _run_label_rerun(tmp_path, {"MOCK_CONCLUSION": ""})
    assert result.returncode != 0
    assert _rerun_calls(tmp_path) == ""
