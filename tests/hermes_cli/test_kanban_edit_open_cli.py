"""CLI surface for amending an open card: `hermes kanban edit` two modes (#180).

`edit <id> --result ...` keeps today's done-card recovery behaviour byte for
byte. `edit <id> --body-file F [--title T]` (or `--title` alone) amends the
brief of a card that is not done: usage errors are usage errors, a worker
context never rewrites a brief, and a running card prints the next-run note.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _edit(*argv: str) -> int:
    """Drive ``kanban edit`` through the real parser and dispatch; return the
    exit code (argparse usage errors surface as their SystemExit code, exactly
    as a shell caller would see them)."""
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    try:
        args = parser.parse_args(["kanban", "edit", *argv])
    except SystemExit as exc:
        return int(exc.code or 2)
    return kc.kanban_command(args)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


NEW_BODY = "Corrected brief via the CLI."


def _make(tmp_path, title, body="Original brief.", status="ready"):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, body=body)
        if status != "ready":
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            conn.commit()
        return tid


def _body_file(tmp_path, text):
    path = tmp_path / "body.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Amend mode
# ---------------------------------------------------------------------------


def test_edit_body_file_amends_ready_card(kanban_home, tmp_path):
    tid = _make(tmp_path, "ready card")
    out = kc.run_slash(f"edit {tid} --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "Edited" in out or "edited" in out
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == NEW_BODY


def test_edit_body_file_on_running_card_prints_next_run_note(kanban_home, tmp_path):
    tid = _make(tmp_path, "running card")
    with kbc.connect() as conn:
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.status == "running"
    out = kc.run_slash(f"edit {tid} --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "next run" in out
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == NEW_BODY
        assert kb.get_task(conn, tid).status == "running"


def test_edit_title_alone_amends(kanban_home, tmp_path):
    tid = _make(tmp_path, "title card")
    out = kc.run_slash(f"edit {tid} --title 'Fixed title'")
    assert "Edited" in out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.title == "Fixed title"
        assert task.body == "Original brief."


def test_edit_amend_refused_for_worker_context(kanban_home, tmp_path, monkeypatch):
    tid = _make(tmp_path, "worker guard")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_somethingelse")
    out = kc.run_slash(f"edit {tid} --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "orchestrator-only" in out
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == "Original brief."


def test_edit_amend_usage_errors(kanban_home, tmp_path):
    tid = _make(tmp_path, "usage card")
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")

    # Neither mode.
    out = kc.run_slash(f"edit {tid}")
    assert "usage" in out.lower()
    # Both modes.
    out = kc.run_slash(f"edit {tid} --result r --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "usage" in out.lower() or "--result" in out
    # Empty body file.
    out = kc.run_slash(f"edit {tid} --body-file {empty}")
    assert "blank" in out.lower() or "empty" in out.lower()
    # Missing file.
    out = kc.run_slash(f"edit {tid} --body-file {tmp_path / 'nope.md'}")
    assert "nope.md" in out or "read" in out.lower()
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == "Original brief."
        assert [e.kind for e in kb.list_events(conn, tid) if e.kind == "body_edited"] == []


def test_edit_amend_noop_body_refused_not_accepted(kanban_home, tmp_path, capsys):
    tid = _make(tmp_path, "noop guard", body="same text")
    rc = _edit(tid, "--body-file", _body_file(tmp_path, "same text"))
    out = capsys.readouterr()
    assert rc == 1, out
    assert "nothing changed" in (out.out + out.err).lower()
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == "same text"
        assert [e.kind for e in kb.list_events(conn, tid) if e.kind == "body_edited"] == []


def test_edit_amend_noop_title_refused_not_accepted(kanban_home, tmp_path, capsys):
    tid = _make(tmp_path, "same")
    rc = _edit(tid, "--title", "same")
    out = capsys.readouterr()
    assert rc == 1, out
    assert "nothing changed" in (out.out + out.err).lower()
    with kbc.connect() as conn:
        assert [e.kind for e in kb.list_events(conn, tid) if e.kind == "body_edited"] == []


def test_edit_amend_noop_body_with_changed_title_still_edits_title(kanban_home, tmp_path, capsys):
    tid = _make(tmp_path, "same", body="same text")
    rc = _edit(tid, "--body-file", _body_file(tmp_path, "same text"), "--title", "Fixed title")
    out = capsys.readouterr()
    assert rc == 0, out
    assert "Edited" in out.out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.title == "Fixed title"
        assert task.body == "same text"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("body_edited") == 1


def test_edit_amend_refuses_result_mode_flags_before_any_read(kanban_home, tmp_path, capsys):
    tid = _make(tmp_path, "flag guard")
    # A missing body file must not even be reported: the usage refusal comes
    # before anything is read or written.
    rc = _edit(tid, "--body-file", str(tmp_path / "nope.md"), "--summary", "dropped?")
    out = capsys.readouterr()
    assert rc == 2, out
    text = out.out + out.err
    assert "usage" in text.lower() and "--summary" in text
    # The same refusal with a perfectly valid body file: the flags are the
    # problem, not the input.
    rc = _edit(tid, "--body-file", _body_file(tmp_path, NEW_BODY), "--summary", "dropped?")
    out = capsys.readouterr()
    assert rc == 2, out
    assert "usage" in (out.out + out.err).lower()
    # --title alone takes no --metadata either.
    rc = _edit(tid, "--title", "Fixed title", "--metadata", '{"x": 1}')
    out = capsys.readouterr()
    assert rc == 2, out
    text = out.out + out.err
    assert "usage" in text.lower() and "--metadata" in text
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.body == "Original brief."
        assert [e.kind for e in kb.list_events(conn, tid) if e.kind == "body_edited"] == []


def test_edit_result_mode_still_takes_summary_and_metadata(kanban_home, tmp_path, capsys):
    tid = _make(tmp_path, "recovery flags")
    with kbc.connect() as conn:
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="first take") is True
    rc = _edit(tid, "--result", "better", "--summary", "s2", "--metadata", '{"k": 1}')
    out = capsys.readouterr()
    assert rc == 0, out
    assert "Edited" in out.out


def test_edit_amend_done_card_refuses_with_pointer_to_result(kanban_home, tmp_path):
    tid = _make(tmp_path, "done via cli")
    with kbc.connect() as conn:
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done") is True
    out = kc.run_slash(f"edit {tid} --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "--result" in out
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == "Original brief."


def test_edit_amend_archived_card_refuses(kanban_home, tmp_path):
    tid = _make(tmp_path, "archived via cli")
    with kbc.connect() as conn:
        assert kb.archive_task(conn, tid) is True
    out = kc.run_slash(f"edit {tid} --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "archived" in out


def test_edit_amend_delegated_child_refused(kanban_home, tmp_path, monkeypatch):
    tid = _make(tmp_path, "child guard")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    out = kc.run_slash(f"edit {tid} --body-file {_body_file(tmp_path, NEW_BODY)}")
    assert "delegate_task child contexts" in out
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).body == "Original brief."


# ---------------------------------------------------------------------------
# Recovery mode is unchanged
# ---------------------------------------------------------------------------


def test_edit_result_on_done_card_still_backfills(kanban_home, tmp_path):
    tid = _make(tmp_path, "recovery card")
    with kbc.connect() as conn:
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="first take") is True
    out = kc.run_slash(f"edit {tid} --result 'better take'")
    assert "Edited" in out
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).result == "better take"


def test_edit_result_on_open_card_still_refuses(kanban_home, tmp_path):
    tid = _make(tmp_path, "open card")
    out = kc.run_slash(f"edit {tid} --result 'nope'")
    assert "not done" in out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.result is None
        assert task.body == "Original brief."
