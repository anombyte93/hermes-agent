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
    r"^\$\{\{\s*(matrix|inputs)\."
    r"([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\s*\}\}$"
)


class _MarkedString(str):
    line: int

    def __new__(cls, value: str, line: int) -> "_MarkedString":
        instance = super().__new__(cls, value)
        instance.line = line
        return instance


class _MarkedList(list[Any]):
    line: int


class _MarkedMapping(dict[Any, Any]):
    line: int


class _WorkflowLoader(yaml.SafeLoader):
    pass


def _construct_marked_string(
    _loader: _WorkflowLoader, node: yaml.nodes.ScalarNode
) -> _MarkedString:
    return _MarkedString(node.value, node.start_mark.line + 1)


def _construct_unique_mapping(
    loader: _WorkflowLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping = _MarkedMapping()
    mapping.line = node.start_mark.line + 1
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


def _construct_marked_sequence(
    loader: _WorkflowLoader, node: yaml.nodes.SequenceNode, deep: bool = False
) -> _MarkedList:
    sequence = _MarkedList(loader.construct_sequence(node, deep=deep))
    sequence.line = node.start_mark.line + 1
    return sequence


_WorkflowLoader.add_constructor(
    "tag:yaml.org,2002:str", _construct_marked_string
)
_WorkflowLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)
_WorkflowLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _construct_marked_sequence
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

    def key_source(mapping: Any, name: str, fallback: Any) -> Any:
        if isinstance(mapping, dict):
            return next((key for key in mapping if key == name), fallback)
        return fallback

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
            if not value:
                report(workflow, value, "unresolved runner source: empty list")
                return
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

    def resolve_path(
        workflow: Path,
        values: list[Any],
        path: list[str],
        display_name: str,
    ) -> list[Any]:
        current = values
        for segment in path:
            resolved: list[Any] = []
            for value in current:
                candidates = value if isinstance(value, list) else [value]
                if isinstance(value, list) and not value:
                    report(
                        workflow,
                        value,
                        f"unresolved runner source: {display_name} is missing",
                    )
                    continue
                for candidate in candidates:
                    if isinstance(candidate, dict) and segment in candidate:
                        resolved.append(candidate[segment])
                    else:
                        report(
                            workflow,
                            candidate,
                            f"unresolved runner source: {display_name} is missing",
                        )
            current = resolved
        return current

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

        namespace, dotted_name = expression.groups()
        path = dotted_name.split(".")
        name = path[0]
        display_name = f"{namespace}.{dotted_name}"
        sources: list[Any] = []
        resolved_from_caller = False
        if namespace == "matrix":
            strategy = job.get("strategy")
            if not isinstance(strategy, dict):
                report(
                    workflow,
                    key_source(job, "strategy", value),
                    "malformed strategy: expected mapping",
                )
                return
            matrix = strategy.get("matrix")
            if not isinstance(matrix, dict):
                if isinstance(matrix, str) and "${{" in matrix:
                    report(workflow, value, f"unresolved runner source: {value}")
                else:
                    report(
                        workflow,
                        key_source(strategy, "matrix", strategy),
                        "malformed matrix: expected mapping or expression",
                    )
                return

            has_direct_axis = name in matrix
            if has_direct_axis:
                sources.extend(
                    resolve_path(
                        workflow,
                        [matrix[name]],
                        path[1:],
                        display_name,
                    )
                )
            include = matrix.get("include", [])
            if isinstance(include, list) and not has_direct_axis:
                sources.extend(
                    resolve_path(workflow, include, path, display_name)
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
                        sources.extend(
                            resolve_path(
                                workflow,
                                [declaration["default"]],
                                path[1:],
                                display_name,
                            )
                        )

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
                        resolved = resolve_path(
                            caller_workflow,
                            [supplied[name]],
                            path[1:],
                            display_name,
                        )
                        for source in resolved:
                            check_runs_on(
                                caller_workflow,
                                caller_document,
                                caller_job,
                                source,
                            )
                    elif not has_default:
                        resolved_from_caller = True
                        report(
                            caller_workflow,
                            uses,
                            f"unresolved runner source: input {dotted_name} is not supplied",
                        )

        if not sources and not resolved_from_caller:
            report(workflow, value, f"unresolved runner source: {value}")
            return
        for source in sources:
            check_literal(workflow, source, source)

    for workflow, document in documents.items():
        if not isinstance(document, dict):
            report(workflow, "", "unresolved workflow document")
            continue
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            report(
                workflow,
                key_source(document, "jobs", document),
                "malformed jobs: expected mapping",
            )
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                report(
                    workflow,
                    job_name,
                    f"malformed job {job_name}: expected mapping",
                )
                continue
            if "runs-on" in job:
                check_runs_on(workflow, document, job, job["runs-on"])
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
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest-32-core\n",
        encoding="utf-8",
    )

    assert _unsupported_runner_labels([workflow]) == [
        "known-bad.yml:3: ubuntu-latest-32-core"
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


def test_checker_ignores_nested_non_job_runs_on_keys(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "action-input.yml",
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: example/action@v1\n"
        "        with:\n          runs-on: arbitrary-action-input\n",
    )

    assert _unsupported_runner_labels([workflow]) == []


def test_checker_resolves_nested_matrix_runner_literals(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "nested-matrix.yml",
        "jobs:\n  test:\n    runs-on: ${{ matrix.config.runner }}\n"
        "    strategy:\n      matrix:\n        config:\n"
        "          - runner: ubuntu-latest\n"
        "          - runner: windows-latest\n",
    )

    assert _unsupported_runner_labels([workflow]) == []


def test_checker_rejects_nested_matrix_private_runner_literal(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "nested-matrix-private.yml",
        "jobs:\n  test:\n    runs-on: ${{ matrix.config.runner }}\n"
        "    strategy:\n      matrix:\n        config:\n"
        "          - runner: ubuntu-latest\n"
        "          - runner: ubuntu-latest-32-core\n",
    )

    assert any(
        finding.startswith("nested-matrix-private.yml:8:")
        and "ubuntu-latest-32-core" in finding
        for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_reports_missing_nested_matrix_runner(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        "nested-matrix-missing.yml",
        "jobs:\n  test:\n    runs-on: ${{ matrix.config.runner }}\n"
        "    strategy:\n      matrix:\n        config:\n"
        "          - runner: ubuntu-latest\n"
        "          - image: windows\n",
    )

    assert any(
        finding.startswith("nested-matrix-missing.yml:8:")
        and "matrix.config.runner is missing" in finding
        for finding in _unsupported_runner_labels([workflow])
    )


def test_checker_rejects_empty_and_recursively_empty_runner_lists(
    tmp_path: Path,
) -> None:
    empty = _write_workflow(
        tmp_path,
        "empty.yml",
        "jobs:\n  test:\n    runs-on: []\n",
    )
    nested = _write_workflow(
        tmp_path,
        "nested-empty.yml",
        "jobs:\n  test:\n    runs-on:\n      - []\n",
    )

    assert _unsupported_runner_labels([empty]) == [
        "empty.yml:3: unresolved runner source: empty list"
    ]
    assert _unsupported_runner_labels([nested]) == [
        "nested-empty.yml:4: unresolved runner source: empty list"
    ]


def test_checker_reports_malformed_strategy_with_source_line(tmp_path: Path) -> None:
    for value in ("null", "[]", "invalid"):
        workflow = _write_workflow(
            tmp_path,
            f"strategy-{value}.yml",
            "jobs:\n  test:\n    runs-on: ${{ matrix.runner }}\n"
            f"    strategy: {value}\n",
        )

        assert _unsupported_runner_labels([workflow]) == [
            f"strategy-{value}.yml:4: malformed strategy: expected mapping"
        ]


def test_checker_reports_malformed_matrix_with_source_line(tmp_path: Path) -> None:
    for value in ("null", "[]", "invalid"):
        workflow = _write_workflow(
            tmp_path,
            f"matrix-{value}.yml",
            "jobs:\n  test:\n    runs-on: ${{ matrix.runner }}\n"
            f"    strategy:\n      matrix: {value}\n",
        )

        assert _unsupported_runner_labels([workflow]) == [
            f"matrix-{value}.yml:5: malformed matrix: expected mapping or expression"
        ]


def test_python_suite_uses_fail_closed_duration_balanced_slices() -> None:
    document = yaml.safe_load(
        (WORKFLOWS_DIR / "tests.yml").read_text(encoding="utf-8")
    )
    trigger = document.get("on", document.get(True))
    assert trigger["workflow_call"]["inputs"]["slice_count"]["default"] == 8

    generate = document["jobs"]["generate"]
    assert generate["outputs"]["matrix"] == "${{ steps.matrix.outputs.matrix }}"
    generate_step = next(
        step for step in generate["steps"] if step.get("id") == "matrix"
    )
    assert "--generate-slices ${{ inputs.slice_count }}" in generate_step["run"]

    test_job = document["jobs"]["test"]
    assert test_job["needs"] == "generate"
    assert test_job["strategy"]["fail-fast"] is False
    assert test_job["strategy"]["matrix"] == "${{ fromJSON(needs.generate.outputs.matrix) }}"
    assert test_job["timeout-minutes"] == 30
    run_tests_step = next(
        step for step in test_job["steps"] if step.get("name", "").startswith("Run tests")
    )
    commands = [
        line.strip() for line in run_tests_step["run"].splitlines() if line.strip()
    ]
    assert commands == [
        "source .venv/bin/activate",
        "HERMES_TEST_WORKERS=\"$(nproc)\" scripts/run_tests.sh --files '${{ matrix.slice.files }}'",
    ]

    aggregate = document["jobs"]["test-result"]
    assert aggregate["if"] == "${{ always() }}"
    assert aggregate["needs"] == ["generate", "test"]
    result_step = next(
        step for step in aggregate["steps"] if step.get("name") == "Require every slice"
    )
    assert result_step["env"] == {
        "GENERATE_RESULT": "${{ needs.generate.result }}",
        "TEST_RESULT": "${{ needs.test.result }}",
    }
    assert 'test "$GENERATE_RESULT" = success' in result_step["run"]
    assert 'test "$TEST_RESULT" = success' in result_step["run"]


def test_js_workspace_checks_use_one_shared_cpu_budget() -> None:
    document = yaml.safe_load(
        (WORKFLOWS_DIR / "js-tests.yml").read_text(encoding="utf-8")
    )
    run_step = next(
        step
        for step in document["jobs"]["check"]["steps"]
        if step.get("name") == "Run all workspace checks"
    )

    assert run_step["run"] == (
        "node .github/scripts/run-workspace-checks.mjs --concurrency 1"
    )


def test_repository_workflows_use_standard_public_runner_labels() -> None:
    workflows = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )

    assert workflows
    for workflow in workflows:
        assert isinstance(yaml.safe_load(workflow.read_text(encoding="utf-8")), dict)
    assert _unsupported_runner_labels(workflows) == []
