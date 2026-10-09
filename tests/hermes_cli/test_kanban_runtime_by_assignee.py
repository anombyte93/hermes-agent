"""Per-assignee default max runtime (``kanban.max_runtime_by_assignee``).

A card created without an explicit max runtime takes its assignee's configured
default; an explicit value always wins; other assignees, and an install with no
config, are untouched. Applied at every creation entry point: ``create_task``
(library, CLI, the agent-facing ``kanban_create`` tool, swarm, dashboard) and the
decompose fan-out, which inserts children directly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return root


@pytest.fixture
def configured(home):
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  max_runtime_by_assignee:\n"
        "    evo: 1800\n"
        "    Kimi: '600'\n"      # name canonicalised, string value normalised
        "    broken: nope\n"     # dropped, like parse_assignee_caps does for caps
        "    zero: 0\n"          # dropped: below 1
    )
    return home


def _runtime(task_id):
    with kbc.connect() as conn:
        return kb.get_task(conn, task_id).max_runtime_seconds


def _create(**kw):
    with kbc.connect() as conn:
        return kb.create_task(conn, **kw)


def test_assignee_default_applied_when_no_explicit_runtime(configured):
    assert _runtime(_create(title="t", assignee="evo")) == 1800


def test_explicit_runtime_always_wins(configured):
    assert _runtime(_create(title="t", assignee="evo", max_runtime_seconds=90)) == 90


def test_other_assignees_untouched(configured):
    assert _runtime(_create(title="t", assignee="claude")) is None
    assert _runtime(_create(title="t")) is None


def test_config_names_are_canonicalised_and_values_normalised(configured):
    assert _runtime(_create(title="t", assignee="kimi")) == 600
    assert _runtime(_create(title="t", assignee="broken")) is None
    assert _runtime(_create(title="t", assignee="zero")) is None


def test_no_config_means_unchanged(home):
    assert _runtime(_create(title="t", assignee="evo")) is None


def test_parked_cards_also_carry_the_default(configured):
    tid = _create(title="t", assignee="evo", initial_status="blocked", block_reason="later")
    assert _runtime(tid) == 1800


def test_config_default_is_an_empty_mapping():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["max_runtime_by_assignee"] == {}


def test_cli_create_applies_default_and_explicit_wins(configured):
    from hermes_cli import kanban as kc
    kc.run_slash("create 'cli default' --assignee evo")
    kc.run_slash("create 'cli explicit' --assignee evo --max-runtime 120")
    with kbc.connect() as conn:
        by_title = {t.title: t for t in kb.list_tasks(conn)}
    assert by_title["cli default"].max_runtime_seconds == 1800
    assert by_title["cli explicit"].max_runtime_seconds == 120


def test_decomposed_children_take_their_assignee_default(configured):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    root = _create(title="rough idea", triage=True)
    with kbc.connect() as conn:
        ids = decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "a", "assignee": "evo", "parents": []},
                      {"title": "b", "assignee": "claude", "parents": []}],
            author="decomposer",
        )
    assert _runtime(ids[0]) == 1800
    assert _runtime(ids[1]) is None


def test_swarm_workers_take_their_assignee_default(configured):
    from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm
    with kbc.connect() as conn:
        created = create_swarm(
            conn, goal="g",
            workers=[SwarmWorkerSpec(profile="evo", title="w1", body=""),
                     SwarmWorkerSpec(profile="evo", title="w2", body="", max_runtime_seconds=45)],
            verifier_assignee="claude", synthesizer_assignee="claude",
        )
    assert [_runtime(w) for w in created.worker_ids] == [1800, 45]


def test_agent_kanban_create_tool_applies_default(configured, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    with kbc.connect() as conn:
        me = kb.create_task(conn, title="worker", assignee="test-worker")
        kb.claim_task(conn, me)
    monkeypatch.setenv("HERMES_KANBAN_TASK", me)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "child", "assignee": "evo"}))
    assert out["ok"] is True
    assert _runtime(out["task_id"]) == 1800
