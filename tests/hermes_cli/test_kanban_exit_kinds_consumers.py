"""Card #2 follow-up: the new exit-classification kinds must reach consumers.

The classifier now emits ``signaled`` / ``output_limit_reached`` event kinds
and ``signaled:<n>`` / ``protocol_violation`` / ``output_limit_reached`` run
outcomes. Distinct kinds that no consumer reads are a silent regression: on
main a signal death fired the ``crashed`` event (notified + woke the creator);
this suite pins the whole path — classification → event kind → notifier claim
→ wake key → diagnostics counting → tick telemetry.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _drive_worker_exit(conn, tid, fake_pid, raw_status):
    """Claim ``tid``, record ``raw_status`` for its dead worker pid, and run
    one reaper pass (fresh module objects; see the crash-classification suite
    for why the resolve matters)."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    _kbd._set_worker_pid(conn, tid, fake_pid)
    _kbd._record_worker_exit(fake_pid, raw_status)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kbd.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def _write_worker_log(home, tid, tail):
    log_dir = kb.worker_logs_dir(board=None)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{tid}.log").write_bytes(tail)


_BANNER = b"  Response truncated due to output length limit" + b" " * 34 + b"\r\n"


# ---------------------------------------------------------------------------
# 1. Gateway notifier: the new kinds are claimed, formatted, and wake
# ---------------------------------------------------------------------------


class TestGatewayNotifierKinds:
    """``gateway/kanban_watchers_notifier.py`` must claim the new event kinds
    (else the creator is never told a signal-killed worker died)."""

    def test_new_exit_kinds_are_claimed_formatted_and_waking(self):
        pytest.importorskip("gateway.kanban_watchers_notifier")
        from gateway import kanban_watchers_notifier as gkn

        # `signaled` MUST behave exactly like `crashed` did on main: claimed,
        # formatted, and waking the creator.
        assert "signaled" in gkn.TERMINAL_KINDS, (
            "signal deaths fire the `signaled` event kind; the gateway notifier "
            "must claim it or the creator never learns the worker died"
        )
        assert "signaled" in gkn._WAKE_KINDS, (
            "a signal death must wake the creator like `crashed` did on main"
        )
        assert "signaled" in gkn._EVENT_FORMATTERS, (
            "a claimed-but-unformatted kind is silently dropped"
        )
        # The failure-free requeue kind must notify (work likely finished but
        # could not report) without waking for a decision.
        assert "output_limit_reached" in gkn.TERMINAL_KINDS
        assert "output_limit_reached" in gkn._EVENT_FORMATTERS
        # rc=0-no-complete keeps its main behaviour (event kind unchanged) but
        # is pinned here so it can never silently drop out either.
        assert "protocol_violation" in gkn.TERMINAL_KINDS
        assert "protocol_violation" in gkn._EVENT_FORMATTERS

    def test_wake_i18n_keys_exist(self):
        pytest.importorskip("gateway.kanban_watchers_notifier")
        from agent.i18n import t
        from gateway import kanban_watchers_notifier as gkn

        for kind in gkn._WAKE_KINDS:
            key = f"gateway.kanban.wake.{kind}"
            value = t(key)
            assert value != key, f"missing i18n wake key: {key}"


# ---------------------------------------------------------------------------
# 2. TUI notification poller: the new kinds format to text
# ---------------------------------------------------------------------------


class TestTuiPollerKinds:
    SUB = {"task_id": "t_abc123"}
    TASK = SimpleNamespace(title="lane A", assignee="worker", result=None)

    @pytest.mark.parametrize("kind", ["signaled", "protocol_violation", "output_limit_reached"])
    def test_new_kinds_format_to_text(self, kanban_home, kind):
        pytest.importorskip("tui_gateway.session_notifications")
        from tui_gateway.session_notifications import _format_kanban_event_text

        ev = SimpleNamespace(kind=kind, payload={"exit_code": 7})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "main")
        assert text is not None, f"`{kind}` events must not be silent on the TUI/desktop side"
        assert "t_abc123" in text

    def test_signaled_text_names_the_signal(self, kanban_home):
        pytest.importorskip("tui_gateway.session_notifications")
        from tui_gateway.session_notifications import _format_kanban_event_text

        ev = SimpleNamespace(kind="signaled", payload={"exit_code": 7})
        text = _format_kanban_event_text(self.SUB, self.TASK, ev, "main")
        assert "signal 7" in text, "the notification must say WHICH signal killed the worker"


# ---------------------------------------------------------------------------
# 3. Diagnostics: signaled:* outcomes still count as crashes
# ---------------------------------------------------------------------------


def _diag_task(**overrides):
    base = {
        "id": "t_diag0000",
        "title": "sigbus diag",
        "assignee": "worker",
        "status": "ready",
        # 1 keeps the unified repeated_failures rule (threshold 2) out of the
        # way so the crash-specific early heads-up is the rule under test.
        "consecutive_failures": 1,
        "last_failure_error": "pid 999 killed by signal 7",
    }
    base.update(overrides)
    return base


class TestDiagnosticsSignaled:
    def test_signaled_outcomes_count_in_repeated_crashes(self):
        from hermes_cli import kanban_diagnostics as kd

        now = int(time.time())
        runs = [
            {"id": 2, "outcome": "signaled:7", "error": "pid 999 killed by signal 7"},
            {"id": 1, "outcome": "signaled:7", "error": "pid 998 killed by signal 7"},
        ]
        diags = kd.compute_task_diagnostics(_diag_task(), [], runs, now=now)
        kinds = {d.kind for d in diags}
        assert "repeated_crashes" in kinds, (
            "`signaled:N` outcomes are real crashes — the early heads-up must "
            "still fire; only the run OUTCOME changed, not the semantics"
        )


# ---------------------------------------------------------------------------
# 4. Tick telemetry: an output-limit requeue is activity, not "idle"
# ---------------------------------------------------------------------------


class TestTickTelemetry:
    def test_output_limit_requeue_is_tick_activity(self, kanban_home):
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="trunc tick", assignee="worker")
            _write_worker_log(kanban_home, tid, b"tail\n" + _BANNER)
            _drive_worker_exit(conn, tid, 22309, 9)
        finally:
            conn.close()

        # detect_crashed_workers ran inside the tick; its side-channel must be
        # readable and the tick-activity set must count the bucket.
        assert getattr(kbd.detect_crashed_workers, "_last_output_limit", None) == [tid]
        assert "output_limit" in kb._TICK_ACTIVITY_FIELDS, (
            "a tick whose only activity is an output-limit requeue must not "
            "book telemetry outcome='idle'"
        )

    def test_dispatch_result_has_output_limit_bucket(self):
        assert hasattr(kbd.DispatchResult(), "output_limit"), (
            "DispatchResult.output_limit must exist so CLI --json / telemetry "
            "can surface output-limit requeues"
        )


# ---------------------------------------------------------------------------
# 5. End-to-end: a signaled death produces a notifier-claimed event kind
# ---------------------------------------------------------------------------


def test_signaled_death_produces_claimed_event_kind(kanban_home):
    pytest.importorskip("gateway.kanban_watchers_notifier")
    from gateway import kanban_watchers_notifier as gkn

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="sigbus flow", assignee="worker")
        _drive_worker_exit(conn, tid, 22310, 7)
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
            ).fetchall()
        ]
    finally:
        conn.close()
    assert "signaled" in kinds
    # Every terminal-ish event the dispatcher emitted must be claimable, so
    # nothing accumulates as forever-undelivered history.
    unclaimed = set(kinds) - set(gkn.TERMINAL_KINDS) - {
        # Non-terminal bookkeeping kinds the notifiers deliberately ignore.
        "created", "claimed", "spawned", "heartbeat", "assigned", "reclaimed",
        "status", "promoted", "unblocked", "archived", "edited",
        "reprioritized",
    }
    assert not unclaimed, f"dispatcher emitted kinds no notifier claims: {sorted(unclaimed)}"


# ---------------------------------------------------------------------------
# 6. Locale parity: new wake keys ship in the English catalog
#    (full cross-locale parity is enforced by tests/agent/test_i18n.py)
# ---------------------------------------------------------------------------


def test_wake_keys_exist_in_english_catalog():
    import yaml

    locales_dir = Path(__file__).resolve().parents[2] / "locales"
    doc = yaml.safe_load((locales_dir / "en.yaml").read_text(encoding="utf-8"))
    wake = doc["gateway"]["kanban"]["wake"]
    # `signaled` joins _WAKE_KINDS, so every catalog needs the key (parity is
    # enforced repo-wide by tests/agent/test_i18n.py).
    assert "signaled" in wake, "en.yaml gateway.kanban.wake.signaled missing"
