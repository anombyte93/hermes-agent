"""External lifecycle reaches subscribers without starting or stopping an owner."""
import asyncio
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_external as external
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


@pytest.mark.parametrize("action,words,wakes", [
    ("claim", "bound", False), ("complete", "done", True),
    ("review", "review", True), ("changes", "changes", True),
    ("release", "released", True), ("handover", "handed over", False),
])
def test_external_lifecycle_notifier_tick_is_routed_deduplicated_and_token_free(tmp_path, monkeypatch, action, words, wakes):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    identity = dict(session="owner", id="$7", created="1000", pane="%9", pid="424242",
                    backend="claude", conversation="fixture-conversation")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="external fixture", execution_kind="external_session")
        if action == "claim":
            kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                thread_id="topic-7", chat_type="thread", delivery_mode="notify+wake")
        active = external.external_claim(conn, tid, board="default", owner_host="fixture-host",
            incarnation=identity, worktree="/fixture/worktree", reservation_id="fixture-r1",
            reservation_token="a" * 64)
        if action == "changes":
            external.external_complete(conn, tid, active["run_id"], reservation_token="a" * 64,
                summary="review evidence", review=True)
        if action != "claim":
            kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                thread_id="topic-7", chat_type="thread", delivery_mode="notify+wake")
        if action == "complete":
            external.external_complete(conn, tid, active["run_id"], reservation_token="a" * 64,
                summary="verified evidence")
        elif action == "release":
            external.external_release(conn, tid, active["run_id"], reservation_token="a" * 64, reason="give up")
        elif action == "changes":
            external.external_request_changes(conn, tid, active["run_id"], reservation_token="a" * 64, reason="revise")
        elif action == "handover":
            external.external_handover(conn, tid, active["run_id"], reservation_token="a" * 64,
                owner_host="fixture-host", incarnation={**identity, "pid": "434343"},
                worktree="/fixture/worktree", reservation_id="fixture-r2", new_reservation_token="b" * 64,
                evidence="explicit transfer", board="default")
        elif action == "review":
            external.external_complete(conn, tid, active["run_id"], reservation_token="a" * 64,
                summary="review evidence", review=True)

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    real_sleep = asyncio.sleep

    async def one_tick():
        runner._running = True

        async def fake_sleep(delay):
            if delay == 5:
                return
            runner._running = False
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await runner._kanban_notifier_watcher(interval=1)

    asyncio.run(one_tick())
    assert adapter.sent
    text = "\n".join(item[1] for item in adapter.sent)
    assert words in text and tid in text
    assert "a" * 64 not in text and "b" * 64 not in text
    assert all(item[0] == "chat-1" and item[2]["thread_id"] == "topic-7" for item in adapter.sent)
    assert bool(adapter.handled) == wakes
    if wakes:
        assert adapter.handled[0].source.thread_id == "topic-7"
        assert "gateway.kanban.wake.external_" not in adapter.handled[0].text
    counts = len(adapter.sent), len(adapter.handled)
    asyncio.run(one_tick())
    assert (len(adapter.sent), len(adapter.handled)) == counts
