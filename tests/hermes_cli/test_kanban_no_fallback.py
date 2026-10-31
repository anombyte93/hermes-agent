"""Card-level ``--no-fallback`` (#107): stored on the task, passed to the worker,
and a provider failure blocks the card with a reason naming the disabled fallback.

No network and no real worker process: the worker argv is built and parsed in
process, and the worker's failure exit is driven with a synthetic 429 result.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

_RATE_LIMIT_RESULT = {
    "final_response": "API call failed after 3 retries: HTTP 429: weekly (7-day) usage limit reached",
    "failed": True,
    "completed": False,
    "error": "HTTP 429: weekly (7-day) usage limit reached",
    "failure_reason": "rate_limit",
}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _claim(conn, tid):
    assert kb.claim_task(conn, tid, claimer="test:1", ttl_seconds=300)
    task = kb.get_task(conn, tid)
    assert task.current_run_id
    return task


# -- store ------------------------------------------------------------------


def test_create_task_stores_no_fallback(kanban_home):
    with kbc.connect_closing() as conn:
        pinned = kb.create_task(conn, title="restricted review", assignee="coder",
                                model_override="kimi-k3", provider_override="kimi-coding",
                                no_fallback=True)
        plain = kb.create_task(conn, title="ordinary card", assignee="coder")
        assert kb.get_task(conn, pinned).no_fallback is True
        assert kb.get_task(conn, plain).no_fallback is False


def test_legacy_row_without_column_reads_as_fallback_allowed(kanban_home, tmp_path):
    """A DB written before the column existed migrates and reads as fallback allowed."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="old card", assignee="coder")
        conn.execute("ALTER TABLE tasks DROP COLUMN no_fallback")
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "no_fallback" not in cols
        # A row from the pre-migration schema still parses.
        assert kb.get_task(conn, tid).no_fallback is False
    kb.init_db()  # migration adds the column back with the fallback-allowed default
    with kbc.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "no_fallback" in cols
        assert kb.get_task(conn, tid).no_fallback is False


def test_kanban_create_cli_flag_round_trips(kanban_home):
    out = kc.run_slash(
        "create 'restricted review' --assignee coder --model kimi-k3 "
        "--provider kimi-coding --no-fallback --json")
    tid = json.loads(out)["id"]
    plain = json.loads(kc.run_slash("create 'ordinary' --assignee coder --json"))["id"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).no_fallback is True
        assert kb.get_task(conn, plain).no_fallback is False
    shown = json.loads(kc.run_slash(f"show {tid} --json"))
    assert shown["task"]["no_fallback"] is True
    assert "fallback" in kc.run_slash(f"show {tid}")


# -- worker run ---------------------------------------------------------------


def _worker_chat_args(argv):
    """Parse a worker argv the way ``hermes`` would (profile flag is pre-argparse)."""
    from hermes_cli._parser import build_top_level_parser

    assert argv[:3] == ["hermes", "-p", "coder"], argv
    parser = build_top_level_parser()[0]
    return parser.parse_args(argv[3:])


def test_worker_argv_carries_no_fallback_into_chat(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: [])
    with kbc.connect_closing() as conn:
        pinned = kb.create_task(conn, title="restricted", assignee="coder",
                                model_override="kimi-k3", provider_override="kimi-coding",
                                no_fallback=True)
        plain = kb.create_task(conn, title="ordinary", assignee="coder")
        pinned_task, plain_task = kb.get_task(conn, pinned), kb.get_task(conn, plain)

    argv = kbd._worker_argv(pinned_task, "coder", None)
    args = _worker_chat_args(argv)
    assert args.command == "chat"
    assert args.no_fallback is True
    assert args.model == "kimi-k3" and args.provider == "kimi-coding"

    plain_args = _worker_chat_args(kbd._worker_argv(plain_task, "coder", None))
    assert "--no-fallback" not in kbd._worker_argv(plain_task, "coder", None)
    assert getattr(plain_args, "no_fallback", False) is False


def test_worker_429_blocks_card_naming_disabled_fallback(kanban_home, monkeypatch, capsys):
    from hermes_cli.no_fallback import finish_no_fallback_failure

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="restricted", assignee="coder",
                             model_override="kimi-k3", provider_override="kimi-coding",
                             no_fallback=True)
        task = _claim(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))

    reason = finish_no_fallback_failure(_RATE_LIMIT_RESULT, model="kimi-k3", provider="kimi-coding")

    assert reason is not None
    for needle in ("fallback disabled", "--no-fallback", "kimi-k3", "kimi-coding", "429"):
        assert needle in reason, (needle, reason)
    assert reason in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        blocked = kb.get_task(conn, tid)
        run = kb.latest_run(conn, tid)
    assert blocked.status == "blocked"
    assert blocked.block_kind == "capability"
    assert blocked.block_reason == reason
    assert run.outcome == "blocked"
    assert "fallback disabled" in (run.summary or "")


def test_failure_outside_kanban_names_disabled_fallback_without_db(kanban_home, capsys):
    from hermes_cli.no_fallback import finish_no_fallback_failure

    reason = finish_no_fallback_failure(_RATE_LIMIT_RESULT, model="kimi-k3", provider="kimi-coding")
    assert reason and "fallback disabled" in reason
    assert "fallback disabled" in capsys.readouterr().err


@pytest.mark.parametrize("result", [
    {"final_response": "done", "completed": True},
    {"failed": True, "error": "tool crashed"},  # not a provider failure
    None,
])
def test_non_provider_outcomes_are_left_alone(kanban_home, result):
    from hermes_cli.no_fallback import finish_no_fallback_failure

    assert finish_no_fallback_failure(result, model="kimi-k3", provider="kimi-coding") is None


# -- chat flag ----------------------------------------------------------------


def test_chat_parser_accepts_no_fallback():
    from hermes_cli._parser import build_top_level_parser

    parser = build_top_level_parser()[0]
    assert parser.parse_args(["chat", "-q", "hi", "--no-fallback"]).no_fallback is True
    assert parser.parse_args(["chat", "-q", "hi"]).no_fallback is False
