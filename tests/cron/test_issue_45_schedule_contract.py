"""Regression tests for GitHub issue anombyte93/hermes-agent#45.

Two defects from the filed repro (observed 2026-09-06 on release a47f117e):

  Defect 1 - bare duration ('30m') at CREATE silently made a ONE-SHOT job
  ('once in 30m', repeat 'once') instead of the documented recurring
  interval. (Parser fix e8bab87ba7 already changed '30m' to kind='interval';
  these tests pin the contract at the TOOL layer, through cronjob(action=
  'create'), where the issue was actually observed.)

  Defect 2 - the create/update readback is ambiguous: 'repeat: once' showed
  even for recurring cron schedules. Root cause found on this branch: a
  once->recurring schedule update does NOT clear the auto-set repeat.times=1,
  so mark_job_run retires the "recurring" job after ONE fire
  (cron/jobs.py:_mark_job_run_locked retires on completed >= times for ANY
  schedule kind). The readback printed the ambiguity; the stored state was
  the same bug.
"""

import json
import pytest

from cron.jobs import get_job, mark_job_run
from tools.cronjob_tools import cronjob, _repeat_display


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate the cron store (same pattern as tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path / "cron"


# =========================================================================
# Defect 1 - bare duration at create = recurring interval (contract)
# =========================================================================

class TestBareDurationCreateContract:
    def test_bare_duration_create_is_recurring_interval(self):
        """'30m' at create must be kind='interval', not kind='once'.

        The issue's exact repro steps 1-2: create with schedule='30m' and
        read back. On the observed release the readback said
        schedule='once in 30m', repeat='once'.
        """
        created = json.loads(cronjob(action="create", prompt="monitor", schedule="30m"))
        assert created["success"] is True
        stored = get_job(created["job_id"])
        assert stored["schedule"]["kind"] == "interval"
        assert stored["schedule"]["minutes"] == 30
        assert created["schedule"] == "every 30m"
        assert created["repeat"] == "forever"

    @pytest.mark.parametrize("sched", ["2h", "every 2h"])
    def test_hourly_forms_recur(self, sched):
        created = json.loads(cronjob(action="create", prompt="p", schedule=sched))
        assert created["success"] is True
        stored = get_job(created["job_id"])
        assert stored["schedule"]["kind"] == "interval"
        assert stored["schedule"]["minutes"] == 120
        assert created["repeat"] == "forever"

    def test_explicit_in_duration_stays_one_shot(self):
        """'in 30m' remains the explicit one-shot form (schema documents it)."""
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        assert created["success"] is True
        stored = get_job(created["job_id"])
        assert stored["schedule"]["kind"] == "once"
        assert created["repeat"] == "once"


# =========================================================================
# Defect 2 - readback prints the resolved schedule TYPE unambiguously
# =========================================================================

class TestScheduleTypeReadback:
    def test_create_readback_names_interval_type(self):
        created = json.loads(cronjob(action="create", prompt="p", schedule="30m"))
        assert created["success"] is True
        assert created["schedule_type"] == "recurring-interval"
        assert created["job"]["schedule_type"] == "recurring-interval"

    def test_create_readback_names_cron_type(self):
        pytest.importorskip("croniter")
        created = json.loads(
            cronjob(action="create", prompt="p", schedule="*/30 * * * *")
        )
        assert created["success"] is True
        assert created["schedule_type"] == "cron"
        assert created["job"]["schedule_type"] == "cron"

    def test_create_readback_names_oneshot_type(self):
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        assert created["success"] is True
        assert created["schedule_type"] == "one-shot"
        assert created["job"]["schedule_type"] == "one-shot"

    def test_update_readback_names_interval_type(self):
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        updated = json.loads(
            cronjob(action="update", job_id=created["job_id"], schedule="every 2h")
        )
        assert updated["success"] is True
        assert updated["job"]["schedule_type"] == "recurring-interval"

    def test_update_readback_names_cron_type(self):
        pytest.importorskip("croniter")
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        updated = json.loads(
            cronjob(action="update", job_id=created["job_id"], schedule="*/30 * * * *")
        )
        assert updated["success"] is True
        assert updated["job"]["schedule"] == "*/30 * * * *"
        assert updated["job"]["schedule_type"] == "cron"

    def test_update_readback_names_oneshot_type(self):
        created = json.loads(cronjob(action="create", prompt="p", schedule="every 1h"))
        updated = json.loads(
            cronjob(action="update", job_id=created["job_id"], schedule="in 45m")
        )
        assert updated["success"] is True
        assert updated["job"]["schedule_type"] == "one-shot"


# =========================================================================
# Defect 2 (root cause) - once->recurring update must clear repeat.times=1
# =========================================================================

class TestOneshotToRecurringRepeatReset:
    def test_update_to_cron_clears_stale_oneshot_repeat(self):
        """A once->cron update must reset repeat to forever, not keep times=1.

        This is the issue's step 3 ('update schedule=*/30 * * * * -> repeat
        still once'): mark_job_run retires ANY job at completed >= times, so
        the stale times=1 would silently kill the recurring job after one
        fire.
        """
        pytest.importorskip("croniter")
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        jid = created["job_id"]
        assert get_job(jid)["repeat"]["times"] == 1  # auto-set for once

        updated = json.loads(
            cronjob(action="update", job_id=jid, schedule="*/30 * * * *")
        )
        assert updated["success"] is True
        stored = get_job(jid)
        assert stored["schedule"]["kind"] == "cron"
        assert stored["repeat"]["times"] is None
        assert updated["job"]["repeat"] == "forever"

    def test_update_to_interval_clears_stale_oneshot_repeat(self):
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        jid = created["job_id"]
        updated = json.loads(
            cronjob(action="update", job_id=jid, schedule="every 2h")
        )
        assert updated["success"] is True
        assert get_job(jid)["repeat"]["times"] is None
        assert updated["job"]["repeat"] == "forever"

    def test_mark_job_run_does_not_retire_recurring_after_oneshot_update(self):
        """End-to-end guard: after once->cron, two fires must not retire it."""
        pytest.importorskip("croniter")
        created = json.loads(cronjob(action="create", prompt="p", schedule="in 30m"))
        jid = created["job_id"]
        updated = json.loads(
            cronjob(action="update", job_id=jid, schedule="*/30 * * * *")
        )
        assert updated["success"] is True
        for _ in range(2):
            ok = mark_job_run(jid, success=True)
            assert ok is True
        stored = get_job(jid)
        assert stored["state"] != "completed", (
            "recurring cron job retired after repeat limit from its one-shot past"
        )
        assert stored["enabled"] is True
        assert stored["next_run_at"] is not None


# =========================================================================
# _repeat_display unit pin (readback ambiguity at the pure function level)
# =========================================================================

class TestRepeatDisplay:
    def test_forever_for_infinite_recurring(self):
        assert _repeat_display({"repeat": {"times": None, "completed": 3}}) == "forever"

    def test_once_only_for_unfired_one_shot(self):
        assert _repeat_display({"repeat": {"times": 1, "completed": 0}}) == "once"

    def test_counts_for_finite_repeat(self):
        assert _repeat_display({"repeat": {"times": 3, "completed": 1}}) == "1/3"
        assert _repeat_display({"repeat": {"times": 3, "completed": 0}}) == "3 times"
