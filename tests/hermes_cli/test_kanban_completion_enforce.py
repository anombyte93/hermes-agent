"""Completion-contract enforcement in complete_task (kanban.completion_contract).

Levels:
- warn (default): the completion lands, but a `completion_warnings` comment
  (+ event) records every unverified claim so the board can tell a verified
  handoff from a bare claim.
- strict: the completion is refused with the problem list; the task stays
  running so the worker can fix its metadata.
- off: no validation.

Non-git workspaces skip the git checks (info note only, never blocks).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_completion import resolve_completion_contract, validate_completion


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"],
                   check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _git_task(kanban_home, tmp_path, monkeypatch, title, contract_level="warn"):
    """A running task with a dir workspace over a real git repo; returns (tid, repo_head)."""
    repo = tmp_path / f"repo-{title.replace(' ', '-')}"
    head = _init_git_repo(repo)
    monkeypatch.setattr(kb, "_kanban_config", lambda: {"completion_contract": contract_level})
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="worker",
                             workspace_kind="dir", workspace_path=str(repo))
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    return tid, head, repo


def _status(tid):
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid).status
    finally:
        conn.close()


def _comments(tid):
    conn = kbc.connect()
    try:
        return [(c.author, c.body) for c in kb.list_comments(conn, tid)]
    finally:
        conn.close()


def _run_metadata(tid):
    conn = kbc.connect()
    try:
        run = kb.latest_run(conn, tid)
        return dict(run.metadata) if run and run.metadata else {}
    finally:
        conn.close()


def _problems_text(metadata):
    return ""  # reserved for dogfood reporting


# ---------------------------------------------------------------------------
# warn (default): completion lands + completion_warnings comment
# ---------------------------------------------------------------------------


def test_warn_accepts_but_comments_on_forged_head_sha(kanban_home, tmp_path, monkeypatch):
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "forged warn")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done",
                          metadata={"head_sha": "f" * 40, "tests_run": {"unit": 3}})
    assert ok is True
    assert _status(tid) == "done"
    warns = [b for a, b in _comments(tid) if b.startswith("completion_warnings")]
    assert len(warns) == 1
    assert "f"*40 in warns[0] and "unknown_commit" not in warns[0]  # human-readable message
    md = _run_metadata(tid)
    assert md["completion_evidence"] == "warnings"
    assert md["completion_warnings"][0]["code"] == "unknown_commit"
    assert _status(tid) == "done"


def test_warn_clean_handoff_no_warning_comment(kanban_home, tmp_path, monkeypatch):
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "clean warn")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done", metadata={
        "head_sha": head, "changed_files": ["README.md"], "tests_run": {"unit": "pytest — 3 passed"},
    })
    assert ok is True
    assert not [b for a, b in _comments(tid) if b.startswith("completion_warnings")]
    assert _run_metadata(tid).get("completion_evidence") == "verified"
    assert _status(tid) == "done"


# ---------------------------------------------------------------------------
# strict: refusal
# ---------------------------------------------------------------------------


def test_strict_refuses_forged_head_sha_task_stays_running(kanban_home, tmp_path, monkeypatch):
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "forged strict", "strict")
    conn = kbc.connect()
    try:
        with pytest.raises(kb.CompletionContractError) as exc:
            kb.complete_task(conn, tid, summary="done", metadata={"head_sha": "f" * 40})
    finally:
        conn.close()
    assert _status(tid) == "running"
    assert "f" * 40 in str(exc.value)
    # audit event
    conn = kbc.connect()
    try:
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    finally:
        conn.close()
    assert "completion_contract_refused" in kinds


def test_strict_accepts_truthful_handoff(kanban_home, tmp_path, monkeypatch, ):
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "clean strict", "strict")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done", metadata={
        "head_sha": head, "changed_files": ["README.md"],
        "tests_run": {"unit": "pytest — 3 passed"},
    })
    assert ok is True
    assert _status(tid) == "done"
    assert not [b for a, b in _comments(tid) if b.startswith("completion_warnings")]


# ---------------------------------------------------------------------------
# off / non-git / no metadata
# ---------------------------------------------------------------------------


def test_off_completes_without_comment(kanban_home, tmp_path, monkeypatch):
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "off", "off")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done",
                          metadata={"head_sha": "f" * 40})
    assert ok is True
    assert not [b for a, b in _comments(tid) if b.startswith("completion_warnings")]
    assert _run_metadata(tid).get("completion_evidence") is None


def test_scratch_task_no_git_checks_completes_with_tests_run_only(kanban_home, tmp_path, monkeypatch):
    """Scratch workspaces (the common non-git case) must not be refused: only
    tests_run shape checks apply. Unverifiable metadata is evidence to record,
    not a blocker."""
    monkeypatch.setattr(kb, "_kanban_config", lambda: {"completion_contract": "strict"})
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="scratch", assignee="worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done",
                          metadata={"tests_run": {"unit": "pytest — 3 passed"}})
    assert ok is True
    assert _status(tid) == "done"


def test_summary_only_handoff_warns_under_warn(kanban_home, tmp_path, monkeypatch):
    """The 'summary-only' worker: no metadata at all → warning under warn, and
    under strict a refusal that names what's missing."""
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "summary only")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done")
    assert ok is True
    warns = [b for a, b in _comments(tid) if b.startswith("completion_warnings")]
    assert len(warns) == 1
    assert "head_sha" in warns[0]

    tid2, _, _ = _git_task(kanban_home, tmp_path, monkeypatch, "summary only strict", "strict")
    conn = kbc.connect()
    try:
        with pytest.raises(kb.CompletionContractError):
            kb.complete_task(conn, tid2, summary="done")
    finally:
        conn.close()
    assert _status(tid2) == "running"


def test_config_level_resolution(kanban_home, tmp_path, monkeypatch):
    """complete_task reads kanban.completion_contract through kb._kanban_config
    (default warn when key absent) — a forged head_sha on a REAL git workspace
    warns under the default, where off would stamp nothing."""
    monkeypatch.setattr(kb, "_kanban_config", lambda: {})
    repo = tmp_path / "cfg-repo"
    _init_git_repo(repo)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="cfg", assignee="worker",
                             workspace_kind="dir", workspace_path=str(repo))
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done",
                          metadata={"head_sha": "f" * 40})
    assert ok is True  # default warn: accepted with warnings
    assert _run_metadata(tid).get("completion_evidence") == "warnings"


# ---------------------------------------------------------------------------
# kanban_show renders `evidence:` from the stored stamp
# ---------------------------------------------------------------------------


def test_show_renders_evidence_line(kanban_home, tmp_path, monkeypatch, capsys):
    """`hermes kanban show` (and worker context) surface the one-line evidence
    verdict so a human can tell a verified handoff from a warned one."""
    from hermes_cli import kanban as kc

    monkeypatch.setattr(kb, "_kanban_config", lambda: {"completion_contract": "warn"})
    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "show verified")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid, summary="done", metadata={
        "head_sha": head, "changed_files": ["README.md"], "tests_run": {"unit": "pytest — 3 passed"},
    })
    assert ok is True
    out = kc.run_slash(f"show {tid}")
    assert "evidence:  verified" in out

    tid2, _, _ = _git_task(kanban_home, tmp_path, monkeypatch, "show warned")
    ok = kb.complete_task(conn=kbc.connect(), task_id=tid2, summary="done",
                          metadata={"head_sha": "f" * 40})
    assert ok is True
    out = kc.run_slash(f"show {tid2}")
    assert "evidence:  warnings(1)" in out

    # Pre-contract completion (no stamp) shows nothing — renderers fall back cleanly.
    tid3, _, _ = _git_task(kanban_home, tmp_path, monkeypatch, "show legacy")
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid3, summary="legacy done", metadata=None)
        # Simulate a pre-contract run: strip the stamp the completer just added.
        run = kb.latest_run(conn, tid3)
        md = dict(run.metadata or {})
        md.pop("completion_evidence", None)
        md.pop("completion_warnings", None)
        assert kb.edit_completed_task_result(
            conn, tid3, result="legacy done", summary="legacy done", metadata=md)
    finally:
        conn.close()
    kc.run_slash(f"show {tid3}")
    out = capsys.readouterr().out
    assert "evidence:" not in out


# ---------------------------------------------------------------------------
# CLI + tool surfaces under strict: refusal reaches the caller, task stays running
# ---------------------------------------------------------------------------


def test_cli_complete_strict_refusal_is_actionable(kanban_home, tmp_path, monkeypatch):
    """`hermes kanban complete` under strict: the refusal (problem list + retry
    hint) reaches the caller, and the task stays running so the worker can fix
    its metadata and retry."""
    import json as _json

    from hermes_cli import kanban as kc

    tid, head, repo = _git_task(kanban_home, tmp_path, monkeypatch, "cli strict", "strict")
    md = _json.dumps({"head_sha": "f" * 40})
    out = kc.run_slash(f"complete {tid} --summary done --metadata {md!r}")
    assert "completion refused" in out
    assert "fix the metadata" in out
    assert "f" * 40 in out
    assert _status(tid) == "running"
