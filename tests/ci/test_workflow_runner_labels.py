"""Regression coverage for public GitHub-hosted workflow runner labels."""

from pathlib import Path
import re

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
STANDARD_PUBLIC_RUNNER_LABELS = {
    "macos-latest",
    "ubuntu-24.04-arm",
    "ubuntu-latest",
    "windows-latest",
}
STATIC_RUNNER_LABEL = re.compile(
    r"^\s*(?:runner|runs-on):\s*([a-z0-9][a-z0-9.-]*)\s*(?:#.*)?$"
)


def _unsupported_runner_labels(workflows: list[Path]) -> list[str]:
    unsupported = []
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = STATIC_RUNNER_LABEL.match(line)
            if match and match.group(1) not in STANDARD_PUBLIC_RUNNER_LABELS:
                unsupported.append(f"{workflow.name}:{line_number}: {match.group(1)}")
    return unsupported


def test_checker_accepts_standard_public_runner_labels(tmp_path: Path) -> None:
    workflow = tmp_path / "known-good.yml"
    workflow.write_text(
        "runs-on: ubuntu-latest\n"
        "    runner: windows-latest\n"
        "    runner: macos-latest\n"
        "    runner: ubuntu-24.04-arm\n",
        encoding="utf-8",
    )

    assert _unsupported_runner_labels([workflow]) == []


def test_checker_rejects_unavailable_private_size_label(tmp_path: Path) -> None:
    workflow = tmp_path / "known-bad.yml"
    workflow.write_text("runs-on: ubuntu-latest-32-core\n", encoding="utf-8")

    assert _unsupported_runner_labels([workflow]) == [
        "known-bad.yml:1: ubuntu-latest-32-core"
    ]


def test_repository_workflows_use_standard_public_runner_labels() -> None:
    workflows = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )

    assert workflows
    for workflow in workflows:
        assert isinstance(yaml.safe_load(workflow.read_text(encoding="utf-8")), dict)
    assert _unsupported_runner_labels(workflows) == []
