"""Workflow API tests: evidence projections (attention/changes/timeline) and
the explicit readiness / continuation / hold routes (K2, K5).

The workflow routes shell out to the RELEASED ``atlas-kanban-call`` helper
(the same boundary as the /evidence/* bridge) but inherit the real
``ATLAS_KANBAN_WRITE_BOARDS`` and validate ROOT receipts (readiness/continuation
are not data-wrapped projections). Tests run the real FastAPI router against a
temporary fake helper executable on PATH; write routes mock only the helper
process boundary and assert the fixed tool/flat-stdin contract, scope/alignment
refusals before the helper, extra-field rejection and malformed-PASS -> UNKNOWN.
No live board is mutated and no worker is dispatched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


_PLUGIN_MODULE_CACHE: dict[str, object] = {}


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_wftest", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _PLUGIN_MODULE_CACHE["mod"] = mod
    return mod


@pytest.fixture
def evidence_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.fixture
def client(evidence_home):
    mod = _load_plugin_module()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    c = TestClient(app)
    c._evidence_module = mod
    return c


# ---------------------------------------------------------------------------
# Fake helper executable
# ---------------------------------------------------------------------------


def _write_fake_helper(directory: Path, behavior: str) -> Path:
    script = '''#!/usr/bin/env python3
import json, os, sys

behavior = {behavior!r}
record_path = os.environ["FAKE_HELPER_RECORD"]

invocation = {{"argv": sys.argv}}

def read_stdin():
    data = sys.stdin.buffer.read()
    try:
        invocation["stdin"] = json.loads(data.decode("utf-8"))
    except Exception:
        invocation["stdin"] = None
    return invocation.get("stdin")

args = read_stdin() or {{}}
tool = sys.argv[1] if len(sys.argv) > 1 else ""

def projection():
    if tool == "kanban_attention":
        return {{
            "board": args.get("board"),
            "read_at": 1789216932.8,
            "observed_at": 1789216932.8,
            "freshness": {{"read_at": 1789216932.8, "note": "one bounded read"}},
            "cards": [{{"id": "t_00000001", "status": "blocked"}}],
            "returned": 1,
            "has_more": False,
            "next_cursor": None,
            "omitted": 0,
            "incomplete": False,
        }}
    if tool == "kanban_changes":
        return {{
            "board": args.get("board"),
            "read_at": 1789216932.8,
            "observed_at": 1789216932.8,
            "freshness": {{"read_at": 1789216932.8, "note": "poll"}},
            "first_read_policy": "baseline-now",
            "events": [{{"id": 1, "kind": "commented"}}],
            "returned": 1,
            "has_more": False,
            "next_cursor": None,
            "total_events": 1,
            "baseline_id": 1,
            "anchor_state": "baseline",
            "incomplete": False,
        }}
    if tool == "kanban_timeline":
        return {{
            "board": args.get("board"),
            "card": args.get("card"),
            "card_status": "blocked",
            "read_at": 1789216932.8,
            "observed_at": 1789216932.8,
            "freshness": {{"read_at": 1789216932.8, "note": "bounded"}},
            "boundary": {{"read_at": 1789216932.8, "clamped_at": 1789216932.8, "note": "n"}},
            "intervals": [{{"kind": "blocked", "start": 1.0, "end": 2.0, "duration_seconds": 1, "source_runs": [], "source_events": []}}],
            "returned": 1,
            "has_more": False,
            "next_cursor": None,
            "totals": {{"covered_window": {{}}, "page": {{}}, "all_time": None}},
            "coverage": {{"window_start": 1.0, "window_end": 2.0, "gaps": [], "note": "n"}},
            "incomplete": False,
        }}
    return {{}}

def workflow():
    if tool == "kanban_readiness":
        return {{
            "state": "PASS",
            "requested": {{"board": args.get("board"), "profile": args.get("profile"), "provider": args.get("provider"), "model": args.get("model"), "workspace": args.get("workspace"), "expected_revision": args.get("expected_revision"), "parents": [], "check_model": args.get("check_model"), "python": args.get("python")}},
            "observed_at": 1789216932.8,
            "freshness": {{"checked_at": 1789216932.8, "stale_after": 1789216932.8 + 300, "note": "n"}},
            "ready_to_release": True,
            "checks": [{{"name": "board_permission", "state": "PASS", "reason": "board exists and may mutate"}}],
            "execution_host": "evo",
            "boundary": "readiness never dispatches",
        }}
    if tool == "kanban_continuation_draft":
        return {{
            "state": "PASS",
            "board": args.get("board"),
            "card": args.get("card"),
            "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "original": {{"status": "blocked", "assignee": "evo", "result_excerpt": "", "latest_run_summary_excerpt": "", "source": "unverified"}},
            "worker": {{"verdict": "STOPPED", "reason": "safe", "aggregate": {{}}}},
            "passed_checks": list(args.get("passed_checks") or []),
            "remaining_checks": list(args.get("remaining_checks") or []),
            "verification_note": args.get("verification_note") or "",
            "commission": {{"workspace": args.get("workspace"), "profile": args.get("profile"), "provider": args.get("provider"), "model": args.get("model"), "max_runtime_minutes": args.get("max_runtime_minutes"), "creator": args.get("creator"), "title": args.get("title")}},
            "read_at": {{"original": 1789216932.8, "worker": 1789216932.9, "note": "n"}},
            "mutation_authorized": True,
            "limitation": "optimistic concurrency check",
            "no_mutation_performed": True,
        }}
    if tool == "kanban_continue":
        return {{
            "state": "PASS",
            "board": args.get("board"),
            "original_card": args.get("card"),
            "new_card": "t_f21c7cd4",
            "new_card_status": "blocked",
            "new_card_assignee": "evo",
            "held": True,
            "released_after_previous_creation": False,
            "reblocked_after_release": False,
            "origin": {{"board": args.get("board"), "card": args.get("card"), "original_status": "blocked", "note": "n"}},
            "worker": {{"verdict": "STOPPED", "reason": "safe"}},
            "no_original_mutation": True,
        }}
    if tool == "kanban_hold":
        return {{
            "state": "PASS",
            "read_back": {{"state": "PASS", "data": {{"id": args.get("card"), "title": "x", "status": "blocked", "assignee": "evo"}}}},
            "worker_before": {{}},
            "worker_after": {{}},
            "warning": "Holding blocks dispatch, not the process.",
        }}
    return {{}}

def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()

env_write = os.environ.get("ATLAS_KANBAN_WRITE_BOARDS", "<unset>")
invocation["env_write_boards"] = env_write

with open(record_path, "a", encoding="utf-8") as fh:
    json.dump(invocation, fh)
    fh.write("\\n")

if behavior == "malformed":
    sys.stdout.write("not json <<<")
    sys.stdout.flush()
    sys.exit(0)

if behavior == "noshape":
    # PASS receipt missing the fields the validator requires -> UNKNOWN.
    if tool in ("kanban_readiness", "kanban_continuation_draft", "kanban_continue", "kanban_hold"):
        emit({{"state": "PASS"}})
    else:
        emit({{"state": "PASS", "data": {{}}, "observed_at": 1789216932.8, "execution_host": "evo"}})
    sys.exit(0)

if behavior == "wrongscope":
    if tool in ("kanban_attention", "kanban_changes", "kanban_timeline"):
        r = {{"state": "PASS", "data": projection(), "observed_at": 1789216932.8, "execution_host": "evo"}}
        r["data"]["board"] = "a-different-board"
        if tool == "kanban_timeline":
            r["data"]["card"] = "t_ffffffff"
        emit(r)
    else:
        r = workflow()
        r["board"] = "a-different-board"
        emit(r)
    sys.exit(0)

if behavior == "fail":
    if tool in ("kanban_attention", "kanban_changes", "kanban_timeline"):
        emit({{"state": "FAIL", "reason": "Board database is absent or invalid", "observed_at": 1789216932.8, "execution_host": "evo"}})
        sys.exit(1)
    elif tool == "kanban_readiness":
        emit({{
            "state": "FAIL",
            "requested": {{"board": args.get("board")}},
            "observed_at": 1789216932.8,
            "freshness": {{"checked_at": 1789216932.8, "stale_after": 1789216932.8 + 300, "note": "n"}},
            "ready_to_release": False,
            "checks": [{{"name": "board_permission", "state": "FAIL", "reason": "read-only for the board"}}],
            "execution_host": "evo",
        }})
        sys.exit(0)
    else:
        emit({{"state": "FAIL", "reason": "original card is running", "board": args.get("board"), "card": args.get("card")}})
        sys.exit(1)

if behavior == "unknown":
    emit({{"state": "UNKNOWN", "reason": "remote unreachable"}})
    sys.exit(0)

# default: pass
if tool in ("kanban_readiness", "kanban_continuation_draft", "kanban_continue", "kanban_hold"):
    emit(workflow())
else:
    emit({{"state": "PASS", "data": projection(), "observed_at": 1789216932.8, "execution_host": "evo"}})
sys.exit(0)
'''.format(behavior=behavior)
    exe = directory / "atlas-kanban-call"
    exe.write_text(script, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


@pytest.fixture
def helper_bin(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    record = tmp_path / "helper-record.jsonl"
    record.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_HELPER_RECORD", str(record))

    def install(behavior: str):
        return _write_fake_helper(bindir, behavior)

    install.record_path = record
    return install


def _invocations(record_path: Path) -> list[dict]:
    rows = []
    for line in record_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _get(client, path):
    return client.get(f"/api/plugins/kanban{path}")


# ---------------------------------------------------------------------------
# Aligned EVO board with a real task (for workflow routes)
# ---------------------------------------------------------------------------


@pytest.fixture
def aligned_evo(evidence_home, monkeypatch):
    """Hostname evo + a real default-board DB + a task, so the workflow routes
    can resolve the aligned local task."""
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "evo")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    conn = kb.connect(board="default")
    try:
        workspace = str(Path(__file__).resolve().parents[2])  # repo root, under /home/hayden/
        tid = kb.create_task(
            conn,
            title="Workflow test card",
            assignee="evo",
            created_by="atlas-relay",
            workspace_path=workspace,
            model_override="deepseek-v4-pro",
            provider_override="deepseek",
        )
    finally:
        conn.close()
    return tid


def _task_workspace_head() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Evidence projections: attention / changes / timeline
# ---------------------------------------------------------------------------


def test_attention_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/attention?board=evo-alpha&limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["helper"]["tool"] == "kanban_attention"
    assert body["evidence"]["cards"] == [{"id": "t_00000001", "status": "blocked"}]
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_attention", "-"]
    assert inv["stdin"] == {"board": "evo-alpha", "limit": 50}


def test_changes_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/changes?board=evo-alpha&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "PASS"
    assert body["helper"]["tool"] == "kanban_changes"
    assert body["evidence"]["events"] == [{"id": 1, "kind": "commented"}]
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_changes", "-"]


def test_timeline_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/timeline?board=evo-alpha&card=t_deadbeef&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "PASS"
    assert body["helper"]["tool"] == "kanban_timeline"
    assert body["evidence"]["intervals"][0]["kind"] == "blocked"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_timeline", "-"]
    assert inv["stdin"]["card"] == "t_deadbeef"


def test_projection_fail_preserves_reason(client, helper_bin):
    helper_bin("fail")
    r = _get(client, "/evidence/attention?board=no-such-board&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "FAIL"
    assert "Board database is absent" in body["reason"]
    assert "exited 1" in body["reason"]


def test_projection_wrong_scope_is_unknown(client, helper_bin):
    helper_bin("wrongscope")
    r = _get(client, "/evidence/attention?board=evo-alpha&limit=5")
    assert r.status_code == 200
    assert r.json()["state"] == "UNKNOWN"
    assert "does not match requested board" in r.json()["reason"]


def test_projection_malformed_pass_is_unknown(client, helper_bin):
    helper_bin("noshape")
    r = _get(client, "/evidence/changes?board=evo-alpha&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert "data object" in body["reason"]


def test_projection_missing_helper_is_unknown(client, helper_bin, monkeypatch):
    import shutil

    real_which = shutil.which

    def _which_none(name, *a, **kw):
        return None if name == "atlas-kanban-call" else real_which(name, *a, **kw)

    monkeypatch.setattr(shutil, "which", _which_none)
    r = _get(client, "/evidence/timeline?board=evo-alpha&card=t_deadbeef")
    assert r.status_code == 200
    assert r.json()["state"] == "UNKNOWN"
    assert "not installed" in r.json()["reason"]


def test_timeline_requires_card(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/timeline?board=evo-alpha")
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_pass_resolves_task_and_git_head(client, helper_bin, aligned_evo):
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/readiness",
        params={"board": "default"},
        json={"card": aligned_evo, "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["helper"]["tool"] == "kanban_readiness"
    assert body["evidence"]["ready_to_release"] is True
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_readiness", "-"]
    stdin = inv["stdin"]
    assert stdin["board"] == "default"
    assert stdin["profile"] == "evo"
    assert stdin["provider"] == "deepseek"
    assert stdin["model"] == "deepseek-v4-pro"
    assert stdin["expected_revision"] == _task_workspace_head()
    workspace = str(Path(__file__).resolve().parents[2])
    assert stdin["workspace"] == workspace
    assert stdin["python"] == os.path.join(workspace, ".venv", "bin", "python")
    assert stdin["check_model"] is False
    assert stdin["parents"] == []


def test_readiness_check_model_flag_forwarded(client, helper_bin, aligned_evo):
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/readiness",
        params={"board": "default"},
        json={"card": aligned_evo, "check_model": True},
    )
    assert r.status_code == 200, r.text
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["check_model"] is True


def test_readiness_incomplete_config_is_unknown(client, helper_bin, aligned_evo, monkeypatch):
    # A card with no workspace can't be readied; nothing is invented.
    conn = kb.connect(board="default")
    try:
        bare = kb.create_task(conn, title="no workspace", assignee="evo")
    finally:
        conn.close()
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/readiness",
        params={"board": "default"},
        json={"card": bare, "check_model": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "workspace" in body["reason"]
    assert body.get("remedy")
    assert _invocations(helper_bin.record_path) == []


def test_readiness_unaligned_is_refused(client, helper_bin, aligned_evo, monkeypatch):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "archie")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/readiness",
        params={"board": "default"},
        json={"card": aligned_evo, "check_model": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "not the aligned EVO board" in body["reason"]
    assert _invocations(helper_bin.record_path) == []


def test_readiness_fail_forward_checks(client, helper_bin, aligned_evo):
    helper_bin("fail")
    r = client.post(
        "/api/plugins/kanban/workflow/readiness",
        params={"board": "default"},
        json={"card": aligned_evo, "check_model": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "FAIL"
    assert body["evidence"]["ready_to_release"] is False
    assert body["evidence"]["checks"][0]["name"] == "board_permission"


# ---------------------------------------------------------------------------
# Continuation draft
# ---------------------------------------------------------------------------

DRAFT_BODY = {
    "card": "t_deadbeef",
    "passed_checks": ["Parent source validation passed"],
    "remaining_checks": [
        {"check": "c", "evidence": "e", "acceptance": "a"},
    ],
    "verification_note": "synthetic",
}


def test_draft_derives_commission_from_task(client, helper_bin, aligned_evo):
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continuation-draft",
        params={"board": "default"},
        json={**DRAFT_BODY, "card": aligned_evo},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_continuation_draft", "-"]
    stdin = inv["stdin"]
    # Commission fields derived from the aligned local task when omitted.
    assert stdin["workspace"] == str(Path(__file__).resolve().parents[2])
    assert stdin["profile"] == "evo"
    assert stdin["provider"] == "deepseek"
    assert stdin["model"] == "deepseek-v4-pro"
    assert stdin["creator"] == "atlas-relay"
    assert stdin["title"] == "Workflow test card"


def test_draft_explicit_commission_wins(client, helper_bin, aligned_evo):
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continuation-draft",
        params={"board": "default"},
        json={**DRAFT_BODY, "card": aligned_evo, "profile": "other", "title": "explicit title"},
    )
    assert r.status_code == 200, r.text
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["profile"] == "other"
    assert inv["stdin"]["title"] == "explicit title"


def test_draft_extra_field_rejected(client, helper_bin, aligned_evo):
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continuation-draft",
        params={"board": "default"},
        json={**DRAFT_BODY, "card": aligned_evo, "unexpected": True},
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


# ---------------------------------------------------------------------------
# Continue (write)
# ---------------------------------------------------------------------------

CONTINUE_BODY = {
    "card": "t_deadbeef",
    "passed_checks": ["Parent source validation passed"],
    "remaining_checks": [{"check": "c", "evidence": "e", "acceptance": "a"}],
    "verification_note": "synthetic",
    "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "workspace": "/home/hayden/atlas/work/x",
    "profile": "evo",
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "creator": "atlas-relay",
    "title": "Continuation",
}


def test_continue_empty_scope_refused_before_helper(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continue",
        params={"board": "default"},
        json={**CONTINUE_BODY, "card": aligned_evo},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "FAIL"
    assert "no write authority" in body["reason"]
    assert _invocations(helper_bin.record_path) == []


def test_continue_cross_board_refused(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "some-other-board")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continue",
        params={"board": "default"},
        json={**CONTINUE_BODY, "card": aligned_evo},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "FAIL"
    assert _invocations(helper_bin.record_path) == []


def test_continue_unaligned_refused(client, helper_bin, aligned_evo, monkeypatch):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "archie")
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "default")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continue",
        params={"board": "default"},
        json={**CONTINUE_BODY, "card": aligned_evo},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "not the aligned EVO board" in body["reason"]
    assert _invocations(helper_bin.record_path) == []


def test_continue_pass_held_and_no_dispatch(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "default")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continue",
        params={"board": "default"},
        json={**CONTINUE_BODY, "card": aligned_evo},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["evidence"]["held"] is True
    assert body["evidence"]["new_card"] == "t_f21c7cd4"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_continue", "-"]  # no dispatch/unblock tool
    assert inv["stdin"]["fingerprint"] == CONTINUE_BODY["fingerprint"]


def test_continue_extra_field_rejected(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "default")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/continue",
        params={"board": "default"},
        json={**CONTINUE_BODY, "card": aligned_evo, "forge": "board"},
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


def test_continue_malformed_pass_is_unknown(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "default")
    helper_bin("noshape")
    r = client.post(
        "/api/plugins/kanban/workflow/continue",
        params={"board": "default"},
        json={**CONTINUE_BODY, "card": aligned_evo},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None


# ---------------------------------------------------------------------------
# Hold (write)
# ---------------------------------------------------------------------------


def test_hold_pass_forwards_reason(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "default")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/hold",
        params={"board": "default"},
        json={"card": aligned_evo, "reason": "held for review"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["evidence"]["read_back"]["data"]["status"] == "blocked"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_hold", "-"]
    assert inv["stdin"]["reason"] == "held for review"


def test_hold_empty_scope_refused(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "")
    helper_bin("pass")
    r = client.post(
        "/api/plugins/kanban/workflow/hold",
        params={"board": "default"},
        json={"card": aligned_evo, "reason": "held for review"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "FAIL"
    assert _invocations(helper_bin.record_path) == []


def test_workflow_helper_inherits_write_boards(client, helper_bin, aligned_evo, monkeypatch):
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "default")
    helper_bin("pass")
    client.post(
        "/api/plugins/kanban/workflow/hold",
        params={"board": "default"},
        json={"card": aligned_evo, "reason": "held"},
    )
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["env_write_boards"] == "default"


# ---------------------------------------------------------------------------
# Auth: production middleware covers the workflow + projection paths
# ---------------------------------------------------------------------------


def test_workflow_paths_covered_by_dashboard_auth_gate():
    _load_plugin_module()
    from hermes_cli import web_server

    for public in getattr(web_server, "_PUBLIC_API_PATHS", ()):
        assert not public.startswith("/api/plugins/kanban/workflow")
        assert not public.startswith("/api/plugins/kanban/evidence/attention")
        assert not public.startswith("/api/plugins/kanban/evidence/changes")
        assert not public.startswith("/api/plugins/kanban/evidence/timeline")


def test_workflow_routes_present_and_typed():
    mod = _load_plugin_module()
    methods: dict[str, set] = {}
    for route in mod.router.routes:
        if "/workflow/" in getattr(route, "path", ""):
            methods[route.path] = set(route.methods or ())
    assert methods["/workflow/readiness"] == {"POST"}
    assert methods["/workflow/continuation-draft"] == {"POST"}
    assert methods["/workflow/continue"] == {"POST"}
    assert methods["/workflow/hold"] == {"POST"}
