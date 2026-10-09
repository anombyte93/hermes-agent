"""Every CI job must land on a runner this repository can actually get.

GitHub never fails a job whose ``runs-on`` names a runner label nobody
provides: the job sits "queued" forever and the PR shows a pending check that
no one is coming to pick up. That is what happened to every PR against this
fork while the workflows asked for upstream's private larger runners
(``ubuntu-latest-96-core``, ``ubuntu-latest-32-core``,
``windows-latest-32-core``, ``ubuntu-latest-32-arm-core``).

These tests resolve each job's runner the way Actions would (static labels,
``matrix.*`` expansions, and ``inputs.*`` threaded through reusable-workflow
callers) and require a standard GitHub-hosted label. They also check that the
Python suite, which used to rely on one 96-core machine, is sharded so each
standard four-core runner gets a bounded share of it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"

# Standard GitHub-hosted labels. Larger runners (``-N-core``, ``-large``,
# ``-xlarge``) and self-hosted labels are deliberately excluded: the fork has
# none of them, so a job requesting one never starts.
_STANDARD_LABEL = re.compile(
    r"^(?:"
    r"ubuntu-(?:latest|\d{2}\.\d{2})(?:-arm)?"
    r"|windows-(?:latest|20\d{2}|11-arm)"
    r"|macos-(?:latest|1\d|2\d)(?:-intel)?"
    r")$"
)

# A standard hosted Linux/Windows runner has four vCPUs. A hard-coded worker
# count above that was tuned for a larger machine and oversubscribes this one.
_STANDARD_RUNNER_CORES = 4

_EXPR = re.compile(r"^\$\{\{\s*(matrix|inputs)\.([A-Za-z0-9_-]+)\s*\}\}$")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML parses the bare key ``on`` as boolean True.
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def _workflows() -> dict[str, dict]:
    return {
        p.name: _load(p)
        for p in sorted(_WORKFLOWS.iterdir())
        if p.suffix in (".yml", ".yaml")
    }


def _labels(runs_on: object) -> list[str]:
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [str(x) for x in runs_on]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels", [])
        return [labels] if isinstance(labels, str) else list(labels)
    raise AssertionError(f"unrecognised runs-on shape: {runs_on!r}")


def _matrix_values(job: dict, key: str) -> list[object]:
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        raise AssertionError(f"matrix.{key} used but the matrix is not static")
    values: list[object] = []
    if isinstance(matrix.get(key), list):
        values.extend(matrix[key])
    for row in matrix.get("include", []) or []:
        if isinstance(row, dict) and key in row:
            values.append(row[key])
    if not values:
        raise AssertionError(f"matrix.{key} has no static values")
    return values


def _callers(workflows: dict[str, dict], target: str):
    """Yield (caller workflow name, caller job) for jobs that call ``target``."""
    suffix = f".github/workflows/{target}"
    for name, wf in workflows.items():
        for job in (wf.get("jobs") or {}).values():
            if isinstance(job, dict) and str(job.get("uses", "")).endswith(suffix):
                yield name, job


def _resolve(value: object, workflow: str, job: dict, workflows: dict, depth: int = 0) -> list[str]:
    """Resolve a runner label expression to the concrete labels it can take."""
    assert depth < 8, "runner resolution recursed too deep"
    text = str(value)
    m = _EXPR.match(text)
    if not m:
        assert "${{" not in text, (
            f"{workflow}: runner expression {text!r} cannot be resolved statically; "
            "use a literal label, matrix.<key> or inputs.<key>"
        )
        return [text]
    kind, key = m.groups()
    if kind == "matrix":
        out: list[str] = []
        for v in _matrix_values(job, key):
            out.extend(_resolve(v, workflow, job, workflows, depth + 1))
        return out
    # inputs.<key>: the reusable workflow's default plus every caller's value.
    inputs = ((workflows[workflow].get("on") or {}).get("workflow_call") or {}).get("inputs") or {}
    assert key in inputs, f"{workflow}: runs-on reads undeclared input {key!r}"
    out = []
    if "default" in inputs[key]:
        out.append(str(inputs[key]["default"]))
    for caller_name, caller_job in _callers(workflows, workflow):
        passed = (caller_job.get("with") or {}).get(key)
        if passed is not None:
            out.extend(_resolve(passed, caller_name, caller_job, workflows, depth + 1))
    return out


def _all_runner_labels() -> list[tuple[str, str, str]]:
    workflows = _workflows()
    found = []
    for name, wf in workflows.items():
        for job_id, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict) or "runs-on" not in job:
                continue
            for raw in _labels(job["runs-on"]):
                for label in _resolve(raw, name, job, workflows):
                    found.append((name, job_id, label))
    return found


def test_every_job_requests_a_standard_hosted_runner():
    found = _all_runner_labels()
    assert len(found) > 20, "resolved suspiciously few runner labels; the walk is broken"
    bad = sorted({(wf, job, label) for wf, job, label in found if not _STANDARD_LABEL.match(label)})
    assert bad == [], (
        "job(s) request a runner label the fork does not provide, so they "
        f"queue forever: {bad}"
    )


@pytest.mark.parametrize(
    "label, ok",
    [
        ("ubuntu-latest", True),
        ("ubuntu-24.04-arm", True),
        ("windows-latest", True),
        ("macos-latest", True),
        ("ubuntu-latest-96-core", False),
        ("ubuntu-latest-32-core", False),
        ("ubuntu-latest-32-arm-core", False),
        ("windows-latest-32-core", False),
        ("macos-latest-xlarge", False),
        ("self-hosted", False),
    ],
)
def test_standard_label_classifier(label, ok):
    """Pin the classifier so a loosened regex cannot quietly pass large runners."""
    assert bool(_STANDARD_LABEL.match(label)) is ok


def test_no_job_hardcodes_more_test_workers_than_a_standard_runner_has():
    offenders = []
    for name, wf in _workflows().items():
        for job_id, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            envs = [job.get("env") or {}] + [
                (s.get("env") or {}) for s in job.get("steps", []) or [] if isinstance(s, dict)
            ]
            for env in envs:
                value = env.get("HERMES_TEST_WORKERS")
                if isinstance(value, int) and value > _STANDARD_RUNNER_CORES:
                    offenders.append((name, job_id, value))
    assert offenders == [], f"worker counts tuned for a larger runner: {offenders}"


def _tests_workflow() -> dict:
    return _load(_WORKFLOWS / "tests.yml")


def _generate(slices: int) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "run_tests_parallel.py"), "--generate-slices", str(slices)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_python_suite_is_sharded_across_standard_runners():
    """One standard runner cannot carry the whole suite inside the job timeout.

    The suite must be split into a matrix of slices fed by the generator, and
    every discovered test file must land in exactly one slice.
    """
    wf = _tests_workflow()
    slice_count = wf["on"]["workflow_call"]["inputs"]["slice_count"]["default"]
    assert slice_count >= 4, "too few slices for four-core runners"

    test_job = wf["jobs"]["test"]
    assert "generate" in (test_job.get("needs") or [])
    matrix_expr = test_job["strategy"]["matrix"]
    assert "needs.generate.outputs.matrix" in matrix_expr
    run_step = next(
        s for s in test_job["steps"] if "run_tests.sh" in str(s.get("run", ""))
    )
    assert "matrix.slice.files" in run_step["run"], "each slice must run only its own files"

    matrix = _generate(slice_count)
    slices = matrix["slice"]
    assert [s["index"] for s in slices] == list(range(1, slice_count + 1))
    per_slice = [[f for f in s["files"].split(":") if f] for s in slices]
    assert all(per_slice), "a slice with no files would run an empty, green job"

    everything = [f for f in _generate(1)["slice"][0]["files"].split(":") if f]
    sharded = [f for files in per_slice for f in files]
    assert len(sharded) == len(set(sharded)), "a test file was assigned to two slices"
    assert sorted(sharded) == sorted(everything), "a test file was dropped from every slice"


def test_sharded_suite_has_a_single_aggregate_result():
    """A failed generate job skips every slice; skipped must not read as green."""
    jobs = _tests_workflow()["jobs"]
    gate = jobs["test-result"]
    assert set(gate["needs"]) >= {"generate", "test"}
    assert "always()" in gate["if"]
