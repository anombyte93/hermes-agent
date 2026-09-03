"""Regression coverage for public GitHub-hosted workflow runner labels."""

from pathlib import Path
import re
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
STANDARD_PUBLIC_RUNNER_LABELS = {
    "macos-latest",
    "ubuntu-24.04-arm",
    "ubuntu-latest",
    "windows-latest",
}
RUNNER_EXPRESSION = re.compile(
    r"^\$\{\{\s*(matrix|inputs)\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}$"
)


class _MarkedString(str):
    line: int

    def __new__(cls, value: str, line: int) -> "_MarkedString":
        instance = super().__new__(cls, value)
        instance.line = line
        return instance


class _WorkflowLoader(yaml.SafeLoader):
    pass


def _construct_marked_string(
    _loader: _WorkflowLoader, node: yaml.nodes.ScalarNode
) -> _MarkedString:
    return _MarkedString(node.value, node.start_mark.line + 1)


def _construct_unique_mapping(
    loader: _WorkflowLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_WorkflowLoader.add_constructor(
    "tag:yaml.org,2002:str", _construct_marked_string
)
_WorkflowLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _unsupported_runner_labels(workflows: list[Path]) -> list[str]:
    unsupported: list[str] = []

    def report(workflow: Path, source: Any, detail: str) -> None:
        line_number = getattr(source, "line", None)
        if line_number is None:
            line_number = next(
                (
                    number
                    for number, line in enumerate(
                        workflow.read_text(encoding="utf-8").splitlines(), start=1
                    )
                    if str(source) in line
                ),
                1,
            )
        finding = f"{workflow.name}:{line_number}: {detail}"
        if finding not in unsupported:
            unsupported.append(finding)

    documents: dict[Path, Any] = {}
    for workflow in workflows:
        try:
            documents[workflow] = yaml.load(
                workflow.read_text(encoding="utf-8"), Loader=_WorkflowLoader
            )
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", str(exc))
            mark = getattr(exc, "problem_mark", None)
            line_number = mark.line + 1 if mark is not None else 1
            unsupported.append(
                f"{workflow.name}:{line_number}: invalid YAML: {problem}"
            )
            documents[workflow] = None

    def check_literal(workflow: Path, value: Any, source: Any) -> None:
        if isinstance(value, list):
            for item in value:
                check_literal(workflow, item, item)
            return
        if not isinstance(value, str):
            report(workflow, source, f"unresolved runner source: {value!r}")
            return
        if "${{" in value:
            report(workflow, source, f"unresolved runner source: {value}")
        elif value not in STANDARD_PUBLIC_RUNNER_LABELS:
            report(workflow, value, str(value))

    def check_runs_on(
        workflow: Path,
        document: dict[str, Any],
        job: dict[str, Any],
        value: Any,
    ) -> None:
        if not isinstance(value, str):
            check_literal(workflow, value, str(value))
            return

        expression = RUNNER_EXPRESSION.fullmatch(value)
        if not expression:
            check_literal(workflow, value, value)
            return

        namespace, name = expression.groups()
        sources: list[Any] = []
        resolved_from_caller = False
        if namespace == "matrix":
            matrix = job.get("strategy", {}).get("matrix", {})
            if isinstance(matrix, dict):
                has_direct_axis = name in matrix
                if has_direct_axis:
                    sources.append(matrix[name])
                include = matrix.get("include", [])
                if isinstance(include, list):
                    for entry in include:
                        if isinstance(entry, dict) and name in entry:
                            sources.append(entry[name])
                        elif isinstance(entry, dict) and not has_direct_axis:
                            location = next(iter(entry.values()), value)
                            report(
                                workflow,
                                location,
                                f"unresolved runner source: matrix.{name} is missing",
                            )
        else:
            has_default = False
            trigger = document.get("on", document.get(True, {}))
            if isinstance(trigger, dict):
                workflow_call = trigger.get("workflow_call", {})
                if isinstance(workflow_call, dict):
                    inputs = workflow_call.get("inputs", {})
                    declaration = inputs.get(name, {}) if isinstance(inputs, dict) else {}
                    if isinstance(declaration, dict) and "default" in declaration:
                        has_default = True
                        sources.append(declaration["default"])

            for caller_workflow, caller_document in documents.items():
                if not isinstance(caller_document, dict):
                    continue
                caller_jobs = caller_document.get("jobs", {})
                if not isinstance(caller_jobs, dict):
                    continue
                for caller_job in caller_jobs.values():
                    if not isinstance(caller_job, dict):
                        continue
                    uses = caller_job.get("uses")
                    if not isinstance(uses, str) or Path(uses).name != workflow.name:
                        continue
                    supplied = caller_job.get("with", {})
                    if isinstance(supplied, dict) and name in supplied:
                        resolved_from_caller = True
                        check_runs_on(
                            caller_workflow,
                            caller_document,
                            caller_job,
                            supplied[name],
                        )
                    elif not has_default:
                        resolved_from_caller = True
                        report(
                            caller_workflow,
                            uses,
                            f"unresolved runner source: input {name} is not supplied",
                        )

        if not sources and not resolved_from_caller:
            report(workflow, value, f"unresolved runner source: {value}")
            return
        for source in sources:
            check_literal(workflow, source, source)

    def walk(workflow: Path, document: dict[str, Any], value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "runs-on":
                    check_runs_on(workflow, document, value, child)
                else:
                    walk(workflow, document, child)
        elif isinstance(value, list):
            for child in value:
                walk(workflow, document, child)

    for workflow, document in documents.items():
        if not isinstance(document, dict):
            report(workflow, "", "unresolved workflow document")
            continue
        walk(workflow, document, document)
    return unsupported


def test_checker_accepts_standard_public_runner_labels(tmp_path: Path) -> None:
    workflow = tmp_path / "known-good.yml"
    workflow.write_text(
        "jobs:\n"
        "  linux:\n    runs-on: ubuntu-latest\n"
        "  windows:\n    runs-on: windows-latest\n"
        "  macos:\n    runs-on: macos-latest\n"
        "  arm:\n    runs-on: ubuntu-24.04-arm\n",
        encoding="utf-8",
    )

    assert _unsupported_runner_labels([workflow]) == []


def test_checker_rejects_unavailable_private_size_label(tmp_path: Path) -> None:
    workflow = tmp_path / "known-bad.yml"
    workflow.write_text("runs-on: ubuntu-latest-32-core\n", encoding="utf-8")

    assert _unsupported_runner_labels([workflow]) == [
        "known-bad.yml:1: ubuntu-latest-32-core"
    ]


def _write_workflow(tmp_path: Path, name: str, contents: str) -> Path:
    workflow = tmp_path / name
    workflow.write_text(contents, encoding="utf-8")
    return workflow


def test_checker_rejects_quoted_private_size_label(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "quoted.yml",
        'jobs:\n  test:\n    runs-on: "ubuntu-latest-32-core"\n',
    )

    assert any(
        "ubuntu-latest-32-core" in finding
        for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_rejects_list_form_private_size_label(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "list.yml",
        "jobs:\n  test:\n    runs-on:\n      - self-hosted\n"
        "      - ubuntu-latest-32-core\n",
    )

    findings = _unsupported_runner_labels([workflow])
    assert any("self-hosted" in finding for finding in findings)
    assert any("ubuntu-latest-32-core" in finding for finding in findings)


def test_checker_accepts_quoted_standard_public_runner_label(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "quoted-good.yml",
        "jobs:\n  test:\n    runs-on: 'ubuntu-latest'\n",
    )

    assert _unsupported_runner_labels([workflow]) == []


def test_checker_resolves_matrix_runner_literals(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "matrix.yml",
        "jobs:\n  test:\n    runs-on: ${{ matrix.runner }}\n"
        "    strategy:\n      matrix:\n        include:\n"
        "          - runner: 'ubuntu-latest'\n"
        "          - runner: 'windows-latest-32-core'\n",
    )

    assert any(
        "windows-latest-32-core" in finding
        for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_reports_matrix_include_row_missing_runner(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "matrix-missing.yml",
        "jobs:\n  test:\n    runs-on: ${{ matrix.runner }}\n"
        "    strategy:\n      matrix:\n        include:\n"
        "          - runner: ubuntu-latest\n            target: linux\n"
        "          - target: windows\n",
    )

    assert any(
        "unresolved" in finding for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_resolves_reusable_workflow_runner_default(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "reusable.yml",
        "on:\n  workflow_call:\n    inputs:\n      runner:\n"
        "        type: string\n        default: 'ubuntu-latest-32-core'\n"
        "jobs:\n  test:\n    runs-on: ${{ inputs.runner }}\n",
    )

    assert any(
        "ubuntu-latest-32-core" in finding
        for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_resolves_reusable_workflow_runner_override(tmp_path: Path) -> None:
    reusable = _write_workflow(
        tmp_path,
        "reusable.yml",
        "on:\n  workflow_call:\n    inputs:\n      runner:\n"
        "        type: string\n        default: ubuntu-latest\n"
        "jobs:\n  test:\n    runs-on: ${{ inputs.runner }}\n",
    )
    caller = _write_workflow(
        tmp_path,
        "caller.yml",
        "jobs:\n  call:\n    uses: ./.github/workflows/reusable.yml\n"
        "    with:\n      runner: 'ubuntu-latest-32-core'\n",
    )

    assert any(
        "ubuntu-latest-32-core" in finding
        for finding in _unsupported_runner_labels([reusable, caller])
    )


def test_checker_reports_each_missing_required_runner_override(tmp_path: Path) -> None:
    reusable = _write_workflow(
        tmp_path,
        "reusable.yml",
        "on:\n  workflow_call:\n    inputs:\n      runner:\n"
        "        type: string\n        required: true\n"
        "jobs:\n  test:\n    runs-on: ${{ inputs.runner }}\n",
    )
    supplied = _write_workflow(
        tmp_path,
        "supplied.yml",
        "jobs:\n  call:\n    uses: ./.github/workflows/reusable.yml\n"
        "    with:\n      runner: ubuntu-latest\n",
    )
    missing = _write_workflow(
        tmp_path,
        "missing.yml",
        "jobs:\n  call:\n    uses: ./.github/workflows/reusable.yml\n",
    )

    assert any(
        finding.startswith("missing.yml:") and "unresolved" in finding
        for finding in _unsupported_runner_labels([reusable, supplied, missing])
    )


def test_checker_rejects_duplicate_runner_keys(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "duplicate.yml",
        "jobs:\n  test:\n    runs-on: ubuntu-latest-32-core\n"
        "    runs-on: ubuntu-latest\n",
    )

    assert any(
        "duplicate" in finding for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_reports_unresolved_runner_expression(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "unresolved.yml",
        "jobs:\n  test:\n    runs-on: ${{ needs.setup.outputs.runner }}\n",
    )

    assert any(
        "unresolved" in finding
        for finding in _unsupported_runner_labels([workflow])
    )


def test_python_suite_derives_worker_count_from_runner_cpu() -> None:
    document = yaml.safe_load(
        (WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8")
    )
    run_tests_step = next(
        step for step in document["jobs"]["test"]["steps"] if step.get("name") == "Run tests"
    )

    commands = [line.strip() for line in run_tests_step["run"].splitlines() if line.strip()]
    assert commands == [
        "source .venv/bin/activate",
        'HERMES_TEST_WORKERS="$(nproc)" scripts/run_tests.sh',
    ]
    assert "HERMES_TEST_WORKERS" not in run_tests_step.get("env", {})


def test_repository_workflows_use_standard_public_runner_labels() -> None:
    workflows = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )

    assert workflows
    for workflow in workflows:
        assert isinstance(yaml.safe_load(workflow.read_text(encoding="utf-8")), dict)
    assert _unsupported_runner_labels(workflows) == []
