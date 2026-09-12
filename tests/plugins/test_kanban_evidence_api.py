"""Read-only evidence bridge tests (plugins/kanban/dashboard/plugin_api.py).

The /evidence/* routes shell out to the RELEASED ``atlas-kanban-call``
helper executable and must NEVER touch the local Hermes kanban DB. These
tests exercise the real FastAPI router with a real temporary fake
executable placed on PATH — no module-mocked HTTP surface.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Router loading (same dynamic-import pattern as test_kanban_dashboard_plugin)
# ---------------------------------------------------------------------------


_PLUGIN_MODULE_CACHE: dict[str, object] = {}


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_evtest", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # Cache under a stable key so monkeypatch targets the module the client
    # fixture actually uses (the client re-loads its own copy).
    _PLUGIN_MODULE_CACHE["mod"] = mod
    return mod


@pytest.fixture
def evidence_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME; NO kanban DB is created (evidence routes must
    never need one, and creating one would prove a fallback to local data)."""
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
    # Expose the module the router lives in so tests can monkeypatch module
    # state (e.g. the timeout constant) on the instance actually serving.
    c._evidence_module = mod
    return c


# ---------------------------------------------------------------------------
# Fake helper executable: parses JSON stdin, emits a bounded receipt,
# records the invocation (argv, env, stdin payload) for assertions.
# ---------------------------------------------------------------------------


def _write_fake_helper(directory: Path, behavior: str, *, cap: int | None = 1) -> Path:
    """Write a python fake ``atlas-kanban-call`` into *directory*.

    behavior cases:
      pass       — exit 0, valid PASS receipt
      fail       — exit 0, FAIL receipt (remote denied)
      unknown    — exit 0, UNKNOWN receipt
      wronghost  — exit 0, PASS receipt but execution_host != evo
      malformed  — exit 0, stdout is not JSON
      nonzero    — exit 1 while emitting a PASS receipt on stdout
      timeout    — sleeps 600s (test patches the timeout boundary down)
      garbled    — exit 0, JSON object missing required receipt keys
      oversized  — exit 0, PASS receipt padded beyond the stdout cap
    """
    script = f'''#!/usr/bin/env python3
import json, os, sys, time

behavior = {behavior!r}
record_path = os.environ["FAKE_HELPER_RECORD"]

invocation = {{"argv": sys.argv, "cwd": os.getcwd()}}

def read_stdin():
    data = sys.stdin.buffer.read()
    invocation["stdin_len"] = len(data)
    try:
        invocation["stdin"] = json.loads(data.decode("utf-8"))
    except Exception:
        invocation["stdin"] = None
    return invocation.get("stdin")

payload = read_stdin()

def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()

if behavior == "timeout":
    time.sleep(600)

env_write = os.environ.get("ATLAS_KANBAN_WRITE_BOARDS", "<unset>")
invocation["env_write_boards"] = env_write

receipt = {{
    "state": "PASS",
    "execution_host": "evo",
    "board": (payload or {{}}).get("args", {{}}).get("board"),
    "tool": (payload or {{}}).get("tool"),
    "data": {{"marker": "evidence", "items": [1, 2, 3]}},
}}

if behavior == "pass":
    emit(receipt)
elif behavior == "fail":
    receipt["state"] = "FAIL"
    receipt["reason"] = "remote board denied read"
    emit(receipt)
elif behavior == "unknown":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "remote unreachable"
    emit(receipt)
elif behavior == "wronghost":
    receipt["execution_host"] = "elsewhere"
    emit(receipt)
elif behavior == "malformed":
    sys.stdout.write("this is not json <<<")
    sys.stdout.flush()
elif behavior == "garbled":
    emit({{"unrelated": True}})
elif behavior == "oversized":
    receipt["data"] = {{"pad": "x" * 2_000_000}}
    emit(receipt)
elif behavior == "nonzero":
    emit(receipt)
    sys.stderr.write("helper exploded\\n")
    sys.exit(3)

with open(record_path, "a", encoding="utf-8") as fh:
    json.dump(invocation, fh)
    fh.write("\\n")
'''
    exe = directory / "atlas-kanban-call"
    exe.write_text(script, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


@pytest.fixture
def helper_bin(tmp_path, monkeypatch):
    """A PATH dir + record file; returns a factory to install behaviors."""
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


PASS_ARGS = {"board": "evo-alpha", "status": "all", "card_limit": 50}


def _get(client, path):
    return client.get(f"/api/plugins/kanban{path}")


# ---------------------------------------------------------------------------
# Happy paths — helper receipt forwarded untouched, exactly one invocation
# ---------------------------------------------------------------------------


def test_snapshot_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["evidence"]["marker"] == "evidence"
    assert body["board"] == "evo-alpha"
    assert body["helper"]["tool"] == "kanban_snapshot"
    # Board binding visible in the wrapper.
    assert body["request"]["board"] == "evo-alpha"
    # Timing surfaces are separate fields, present and non-negative.
    assert body["timing"]["helper_roundtrip_ms"] >= 0
    assert body["timing"]["collection_ms"] >= 0
    assert set(body["timing"].keys()) == {"helper_roundtrip_ms", "collection_ms"}
    # Exactly ONE helper invocation for one snapshot request.
    assert len(_invocations(helper_bin.record_path)) == 1
    inv = _invocations(helper_bin.record_path)[0]
    # Fixed tool name, stdin JSON payload with the validated args.
    assert inv["argv"][1:] == ["kanban_snapshot", "-"]
    assert inv["stdin"]["tool"] == "kanban_snapshot"
    assert inv["stdin"]["args"]["board"] == "evo-alpha"
    assert inv["stdin"]["args"]["card_limit"] == 50


def test_snapshot_pass_never_touches_local_db(client, helper_bin, evidence_home):
    helper_bin("pass")
    _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    # No kanban DB file may be created by an evidence call.
    assert not (evidence_home / "kanban.db").exists()
    # No 'current' board pointer may be written either.
    assert not (evidence_home / "kanban" / "current").exists()
    assert not list(evidence_home.rglob("kanban.db"))


def test_snapshot_empty_cursor_omitted_from_helper_args(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=10")
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert "cursor" not in inv["stdin"]["args"]


def test_snapshot_with_cursor_forwarded(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/snapshot?board=evo-alpha&status=all&card_limit=10&cursor=abc123",
    )
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["args"]["cursor"] == "abc123"


def test_snapshot_default_card_limit(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all")
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["args"]["card_limit"] == 100


def test_page_cards_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/page?board=evo-alpha&resource=cards&limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["helper"]["tool"] == "kanban_page"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_page", "-"]
    assert inv["stdin"]["args"]["resource"] == "cards"
    assert "card" not in inv["stdin"]["args"]  # card omitted for cards resource


def test_page_events_requires_card(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/page?board=evo-alpha&resource=events&limit=10")
    assert r.status_code == 422  # card is required except for resource=cards
    # Validation prevented invocation — no helper subprocess ran.
    assert _invocations(helper_bin.record_path) == []


def test_page_card_forwarded(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/page?board=evo-alpha&resource=runs&card=t_deadbeef&limit=10",
    )
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["args"]["card"] == "t_deadbeef"


def test_worker_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/worker?board=evo-alpha&card=t_deadbeef")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["helper"]["tool"] == "kanban_worker"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_worker", "-"]
    assert inv["stdin"]["args"] == {"board": "evo-alpha", "card": "t_deadbeef"}


def test_card_pass_fixed_args(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/card?board=evo-alpha&card=t_deadbeef")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["helper"]["tool"] == "kanban_card"
    inv = _invocations(helper_bin.record_path)[0]
    # include_body / recent_items are FIXED — not request-selectable.
    assert inv["stdin"]["args"] == {
        "board": "evo-alpha",
        "card": "t_deadbeef",
        "include_body": False,
        "recent_items": 10,
    }


# ---------------------------------------------------------------------------
# FAIL / UNKNOWN envelopes from the helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("behavior", ["fail", "unknown"])
def test_fail_unknown_envelopes_forwarded(client, helper_bin, behavior):
    helper_bin(behavior)
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] in {"FAIL", "UNKNOWN"}
    assert body["evidence"] is None
    assert body["reason"]
    # Helper's own reason preserved without raw logs.
    assert body["reason"] == ("remote board denied read" if behavior == "fail" else "remote unreachable")


def test_helper_exit_nonzero_with_pass_receipt_is_unknown(client, helper_bin):
    # Nonzero exit + PASS receipt must NOT be trusted as success.
    helper_bin("nonzero")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["reason"]


def test_wrong_execution_host_is_unknown(client, helper_bin):
    helper_bin("wronghost")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "execution_host" in body["reason"]


def test_malformed_stdout_is_unknown(client, helper_bin):
    helper_bin("malformed")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None


def test_missing_receipt_keys_is_unknown(client, helper_bin):
    helper_bin("garbled")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    assert r.json()["state"] == "UNKNOWN"


def test_oversized_stdout_is_unknown(client, helper_bin):
    # >1 MiB stdout blows the disk-backed capture cap -> UNKNOWN, no crash.
    helper_bin("oversized")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None


def test_missing_helper_is_unknown(client, helper_bin):
    # No helper installed on PATH — every route returns UNKNOWN, no 500.
    for path in (
        "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50",
        "/evidence/page?board=evo-alpha&resource=cards",
        "/evidence/worker?board=evo-alpha&card=t_deadbeef",
        "/evidence/card?board=evo-alpha&card=t_deadbeef",
    ):
        r = _get(client, path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["state"] == "UNKNOWN", path
        assert body["evidence"] is None
        assert "not installed" in body["reason"], path
        assert body.get("remedy")
    assert _invocations(helper_bin.record_path) == []


def test_timeout_is_unknown(client, helper_bin, monkeypatch):
    helper_bin("timeout")
    # Inject the boundary: patch the subprocess timeout down so the test
    # doesn't wait 75s. The route must surface a timeout UNKNOWN envelope.
    monkeypatch.setattr(
        client._evidence_module, "EVIDENCE_HELPER_TIMEOUT_SECONDS", 0.3
    )
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "timed out" in body["reason"]


# ---------------------------------------------------------------------------
# Input validation prevents invocation
# ---------------------------------------------------------------------------


def test_bad_board_slug_rejected_before_invocation(client, helper_bin):
    helper_bin("pass")
    for bad in ("../escape", "ev o", "evo_alpha!", "", "-lead", "a" * 65):
        r = client.get(
            "/api/plugins/kanban/evidence/snapshot",
            params={"board": bad, "status": "all", "card_limit": 50},
        )
        assert r.status_code == 400, (bad, r.status_code)
    assert _invocations(helper_bin.record_path) == []


def test_board_slug_normalized_not_locality_checked(client, helper_bin):
    """Pure slug validation: mixed case normalises (it is NOT a 404-able
    local-board lookup), and a board that does not exist locally still
    reaches the helper — the EVO-owned board need not exist here."""
    helper_bin("pass")
    r = client.get(
        "/api/plugins/kanban/evidence/snapshot",
        params={"board": "Evo-Alpha", "status": "all", "card_limit": 50},
    )
    assert r.status_code == 200, r.text
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["args"]["board"] == "evo-alpha"


def test_missing_board_rejected(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?status=all&card_limit=50")
    assert r.status_code == 422
    r = _get(client, "/evidence/worker?card=t_deadbeef")
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


def test_bad_status_rejected(client, helper_bin):
    helper_bin("pass")
    r = client.get(
        "/api/plugins/kanban/evidence/snapshot",
        params={"board": "evo-alpha", "status": "bogus", "card_limit": 50},
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


def test_card_limit_bounds(client, helper_bin):
    helper_bin("pass")
    for bad in (0, -1, 201, 99999):
        r = client.get(
            "/api/plugins/kanban/evidence/snapshot",
            params={"board": "evo-alpha", "status": "all", "card_limit": bad},
        )
        assert r.status_code == 422, bad
    assert _invocations(helper_bin.record_path) == []


def test_cursor_length_bound(client, helper_bin):
    helper_bin("pass")
    r = client.get(
        "/api/plugins/kanban/evidence/snapshot",
        params={"board": "evo-alpha", "status": "all", "card_limit": 10, "cursor": "c" * 513},
    )
    assert r.status_code == 422
    r = client.get(
        "/api/plugins/kanban/evidence/snapshot",
        params={"board": "evo-alpha", "status": "all", "card_limit": 10, "cursor": "c" * 512},
    )
    assert r.status_code == 200
    assert _invocations(helper_bin.record_path) != []


def test_card_pattern_enforced(client, helper_bin):
    helper_bin("pass")
    for bad in ("deadbeef", "x_deadbeef", "t_zzzzzz", "t_abc", "t_" + "a" * 33, "t_deadbeef;rm"):
        r = client.get(
            "/api/plugins/kanban/evidence/worker",
            params={"board": "evo-alpha", "card": bad},
        )
        assert r.status_code == 422, bad
    assert _invocations(helper_bin.record_path) == []


def test_card_pattern_accepts_full_range(client, helper_bin):
    helper_bin("pass")
    r = client.get(
        "/api/plugins/kanban/evidence/worker",
        params={"board": "evo-alpha", "card": "t_" + "a" * 32},
    )
    assert r.status_code == 200
    assert len(_invocations(helper_bin.record_path)) == 1


def test_page_bad_resource_rejected(client, helper_bin):
    helper_bin("pass")
    r = client.get(
        "/api/plugins/kanban/evidence/page",
        params={"board": "evo-alpha", "resource": "tasks", "card": "t_deadbeef"},
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


# ---------------------------------------------------------------------------
# Child-process hygiene: env scrub, fixed read tool allowlist
# ---------------------------------------------------------------------------


def test_child_write_authority_empty_and_allowlist(client, helper_bin, monkeypatch):
    helper_bin("pass")
    # Simulate a host that grants write authority — the bridge must strip it.
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "prod-board,other")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["env_write_boards"] == ""


def test_read_tool_allowlist_constant():
    mod = _load_plugin_module()
    assert mod.EVIDENCE_READ_TOOLS == frozenset(
        {"kanban_snapshot", "kanban_page", "kanban_worker", "kanban_card"}
    )
    # Write/mutating tool names can never be selected through the routes:
    # the tool is a fixed literal per route, not a request parameter.
    src = (Path(mod.__file__)).read_text(encoding="utf-8")
    for route_tool in ("kanban_snapshot", "kanban_page", "kanban_worker", "kanban_card"):
        assert f'"{route_tool}"' in src
    assert "kanban_create" not in src
    assert "kanban_dispatch" not in src
    assert "kanban_complete" not in src


# ---------------------------------------------------------------------------
# Auth: these routes live behind the dashboard session-token middleware in
# production. A bare FastAPI app proves nothing about that; assert the
# production gate would cover the paths (real webserver integration check
# below), and state the limit explicitly.
# ---------------------------------------------------------------------------


def test_evidence_paths_covered_by_dashboard_auth_gate():
    """Real-webserver check without broad setup: the production dashboard
    enforces the session token on every /api/ path except a public allowlist.
    The evidence routes are /api/plugins/kanban/evidence/... — assert they
    are NOT in the public allowlist, so auth_middleware covers them.

    LIMIT (explicit): this does not boot the real dashboard or prove a live
    token handshake; it proves the production middleware's public-path
    allowlist does not exempt the evidence routes.
    """
    mod = _load_plugin_module()
    from hermes_cli import web_server

    for public in getattr(web_server, "_PUBLIC_API_PATHS", ()):
        assert not public.startswith("/api/plugins/kanban/evidence")
    # The gate applies to /api/ paths generally.
    src = Path(web_server.__file__).read_text(encoding="utf-8")
    assert 'path.startswith("/api/")' in src


def test_evidence_routes_are_read_only(client, helper_bin):
    """No POST/PUT/PATCH/DELETE handler exists for any /evidence/ path."""
    mod = _load_plugin_module()
    for route in mod.router.routes:
        if getattr(route, "path", "").endswith("/evidence/snapshot") or "/evidence/" in getattr(
            route, "path", ""
        ):
            assert route.methods is None or set(route.methods) <= {"GET", "HEAD"}
