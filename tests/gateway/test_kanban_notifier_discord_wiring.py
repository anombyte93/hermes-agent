"""Notifier wiring for the Discord Kanban notice format (#111).

Kept free of imports from the new renderer module so that, on the base
commit, the non-Discord golden tests here run (and pass: legacy text is
byte-identical) while the Discord one fails (known-red control).

Ported to the deployed notifier split: the loop lives in
``gateway/kanban_watchers.py`` but formatting/delivery happen in
``_KanbanNotification`` (``gateway/kanban_watchers_notifier.py``) — the
runner needs the attributes that class reads.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn


class _Recording:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _deliver(tmp_path, monkeypatch, platform, *, block=False):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{platform.value}.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Verify: port of #332", assignee="claude")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform.value, chat_id="chat-1")
        if block:
            kb.block_task(conn, tid, reason="needs a token")
        else:
            kb.complete_task(
                conn, tid,
                summary="Opened https://github.com/o/r/pull/7 and verified.\nsecond line",
            )
    finally:
        conn.close()
    adapter = _Recording()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {platform: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = ""  # type: ignore[attr-defined]
    runner._profile_adapters = {}
    runner.config = None  # type: ignore[assignment]
    asyncio.run(_tick(monkeypatch, runner))
    return tid, adapter.sent


def test_notifier_uses_discord_renderer_for_discord(tmp_path, monkeypatch):
    tid, sent = _deliver(tmp_path, monkeypatch, Platform.DISCORD)
    assert sent == [
        "✅ **Verify: port of #332**\n"
        "Opened [PR #7](https://github.com/o/r/pull/7) and verified.\n"
        f"-# {tid} · claude"
    ]


def test_discord_blocked_uses_discord_renderer(tmp_path, monkeypatch):
    tid, sent = _deliver(tmp_path, monkeypatch, Platform.DISCORD, block=True)
    assert sent == [
        "🛑 **Verify: port of #332**\n"
        "Blocked: needs a token\n"
        f"-# {tid} · claude"
    ]


def test_telegram_completed_output_unchanged(tmp_path, monkeypatch):
    tid, sent = _deliver(tmp_path, monkeypatch, Platform.TELEGRAM)
    assert sent == [
        f"✔ [default] @claude Kanban {tid} done — Verify: port of #332\n"
        "Opened https://github.com/o/r/pull/7 and verified."
    ]


def test_telegram_blocked_output_unchanged(tmp_path, monkeypatch):
    tid, sent = _deliver(tmp_path, monkeypatch, Platform.TELEGRAM, block=True)
    assert sent == [f"⏸ [default] @claude Kanban {tid} blocked: needs a token"]


def test_slack_completed_output_unchanged(tmp_path, monkeypatch):
    tid, sent = _deliver(tmp_path, monkeypatch, Platform.SLACK)
    assert sent == [
        f"✔ [default] @claude Kanban {tid} done — Verify: port of #332\n"
        "Opened https://github.com/o/r/pull/7 and verified."
    ]
