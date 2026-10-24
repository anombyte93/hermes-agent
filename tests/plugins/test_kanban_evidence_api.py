"""Read-only evidence bridge tests (plugins/kanban/dashboard/plugin_api.py).

The /evidence/* routes shell out to the RELEASED ``atlas-kanban-call``
helper executable and must NEVER touch the local Hermes kanban DB. These
tests exercise the real FastAPI router with a real temporary fake
executable placed on PATH — no module-mocked HTTP surface.

The fake's contract mirrors the RELEASED helper (verified against the
real candidate, source 27ef127, on 2026-09-12): the tool name arrives in
argv as ``atlas-kanban-call <tool> -`` and stdin carries ONLY the flat
JSON args object. FAIL receipts exit 1. PASS receipts carry the per-tool
data shapes the real adapter emits, including observed_at.
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
      noshape    — exit 0, PASS receipt whose data misses required fields
      wrongscope — exit 0, PASS receipt echoing a different board/card
      badtypes  — exit 0, PASS receipt with wrong consumed field types
      badreturned — exit 0, PASS page receipt with returned != len(items)
      nonfinite_top — exit 0, PASS receipt with NaN top-level observed_at
      nonfinite_snapshot — exit 0, PASS snapshot with Inf data.observed_at
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
args = payload if isinstance(payload, dict) else {{}}
tool = sys.argv[1] if len(sys.argv) > 1 else ""


def make_data():
    if tool == "kanban_snapshot":
        return {{
            "board": args.get("board", "evo-alpha"),
            "cards": [{{"id": "t_00000001"}}],
            "counts": {{}},
            "status_filter": str(args.get("status", "all")),
            "observed_at": 1789216932.8,
            "omitted": {{"events": 0}},
        }}
    if tool == "kanban_page":
        return {{
            "items": [{{"id": "t_00000001"}}],
            "returned": 1,
            "has_more": False,
        }}
    if tool == "kanban_worker":
        return {{
            "task_id": args.get("card", "t_00000001"),
            "observations": [],
        }}
    # kanban_card
    return {{
        "task": {{"id": args.get("card", "t_00000001")}},
        "runs": [],
        "comments": [],
        "events": [],
    }}


def make_bad_data():
    # Wrong consumed types mirroring the parent malformed-boundary examples
    # (verify-ui-malformed.py): each tool returns one field with a wrong type
    # while the rest stay correctly shaped (scope echoes still match).
    if tool == "kanban_snapshot":
        return {{
            "board": args.get("board", "evo-alpha"),
            "cards": "wrong",
            "counts": {{}},
            "status_filter": str(args.get("status", "all")),
            "observed_at": 1,
        }}
    if tool == "kanban_page":
        return {{
            "items": "wrong",
            "returned": True,
            "has_more": "false",
        }}
    if tool == "kanban_worker":
        return {{
            "task_id": args.get("card", "t_00000001"),
            "observations": {{}},
        }}
    # kanban_card
    return {{
        "task": {{"id": args.get("card", "t_00000001")}},
        "runs": None,
        "comments": [],
        "events": [],
    }}


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()

if behavior == "timeout":
    time.sleep(600)

env_write = os.environ.get("ATLAS_KANBAN_WRITE_BOARDS", "<unset>")
invocation["env_write_boards"] = env_write

with open(record_path, "a", encoding="utf-8") as fh:
    json.dump(invocation, fh)
    fh.write("\\n")

receipt = {{
    "state": "PASS",
    "execution_host": "evo",
    "observed_at": 1789216932.8,
    "data": make_data(),
}}

if behavior == "pass":
    emit(receipt)
elif behavior == "fail":
    receipt["state"] = "FAIL"
    receipt["reason"] = "remote board denied read"
    receipt.pop("data", None)
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "remote unreachable"
    receipt.pop("data", None)
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
elif behavior == "noshape":
    receipt["data"] = {{"unrelated": True}}
    emit(receipt)
elif behavior == "wrongscope":
    receipt["data"] = make_data()
    if tool == "kanban_snapshot":
        receipt["data"]["board"] = "a-different-board"
    else:
        receipt["data"]["task"] = {{"id": "t_ffffffff"}}
        receipt["data"]["task_id"] = "t_ffffffff"
    emit(receipt)
elif behavior == "badtypes":
    receipt["data"] = make_bad_data()
    emit(receipt)
elif behavior == "badreturned":
    receipt["data"] = make_data()
    receipt["data"]["returned"] = 99
    emit(receipt)
elif behavior == "nonfinite_top":
    receipt["observed_at"] = float("nan")
    emit(receipt)
elif behavior == "nonfinite_snapshot":
    receipt["data"]["observed_at"] = float("inf")
    emit(receipt)

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
    assert body["evidence"]["board"] == "evo-alpha"
    assert body["evidence"]["cards"] == [{"id": "t_00000001"}]
    assert body["board"] == "evo-alpha"
    assert body["helper"]["tool"] == "kanban_snapshot"
    # Validated PASS: observed_at and the verified execution host are
    # forwarded from the receipt (never stamped by the bridge itself).
    assert body["observed_at"] == 1789216932.8
    assert body["helper"]["execution_host"] == "evo"
    assert body["limitations"] == {"events": 0}
    # Board binding visible in the wrapper.
    assert body["request"]["board"] == "evo-alpha"
    # Timing surfaces are separate fields, present and non-negative.
    assert body["timing"]["helper_roundtrip_ms"] >= 0
    assert body["timing"]["collection_ms"] >= 0
    assert set(body["timing"].keys()) == {"helper_roundtrip_ms", "collection_ms"}
    # Exactly ONE helper invocation for one snapshot request.
    assert len(_invocations(helper_bin.record_path)) == 1
    inv = _invocations(helper_bin.record_path)[0]
    # Fixed tool name in argv; stdin carries ONLY the flat args object
    # (the real helper rejects the nested {"tool":..., "args":...} envelope).
    assert inv["argv"][1:] == ["kanban_snapshot", "-"]
    assert inv["stdin"]["board"] == "evo-alpha"
    assert inv["stdin"]["card_limit"] == 50
    assert "tool" not in inv["stdin"]
    assert "args" not in inv["stdin"]


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
    assert "cursor" not in inv["stdin"]


def test_snapshot_with_cursor_forwarded(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/snapshot?board=evo-alpha&status=all&card_limit=10&cursor=abc123",
    )
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["cursor"] == "abc123"


def test_snapshot_default_card_limit(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all")
    assert r.status_code == 200
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["card_limit"] == 100


def test_page_cards_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/page?board=evo-alpha&resource=cards&limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["helper"]["tool"] == "kanban_page"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_page", "-"]
    assert inv["stdin"]["resource"] == "cards"
    assert "card" not in inv["stdin"]  # card omitted for cards resource


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
    assert inv["stdin"]["card"] == "t_deadbeef"


def test_worker_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/worker?board=evo-alpha&card=t_deadbeef")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["helper"]["tool"] == "kanban_worker"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["argv"][1:] == ["kanban_worker", "-"]
    assert inv["stdin"] == {"board": "evo-alpha", "card": "t_deadbeef"}


def test_card_pass_fixed_args(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/card?board=evo-alpha&card=t_deadbeef")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["helper"]["tool"] == "kanban_card"
    inv = _invocations(helper_bin.record_path)[0]
    # include_body / recent_items are FIXED — not request-selectable.
    assert inv["stdin"] == {
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
    assert body["reason"].startswith(
        "remote board denied read" if behavior == "fail" else "remote unreachable"
    )
    # FAIL receipts arrive with exit 1 (real helper behaviour): the reason
    # survives, annotated with the exit code but not replaced by it.
    if behavior == "fail":
        assert "exited 1" in body["reason"]
    # Helper identity is never stamped from an unverified receipt.
    assert body["helper"]["execution_host"] == "unverified"


def test_helper_exit_nonzero_with_pass_receipt_is_unknown(client, helper_bin):
    # Nonzero exit + PASS receipt must NOT be trusted as success.
    helper_bin("nonzero")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["reason"]
    assert "exited 3" in body["reason"]


def test_wrong_execution_host_is_unknown(client, helper_bin):
    helper_bin("wronghost")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "execution_host" in body["reason"]
    assert body["helper"]["execution_host"] == "unverified"


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


def test_missing_helper_is_unknown(client, helper_bin, monkeypatch):
    # The released helper IS installed on this host now, so the "not installed"
    # branch must be exercised by replacing ONLY the executable-discovery
    # boundary (shutil.which) for this test — real routes, assertions, no-500
    # and remedy all stay intact. No production change satisfies this.
    import shutil

    real_which = shutil.which

    def _which_no_helper(name, *args, **kwargs):
        if name == "atlas-kanban-call":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", _which_no_helper)

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
    assert inv["stdin"]["board"] == "evo-alpha"


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


# ---------------------------------------------------------------------------
# Real-helper contract repairs (t_8bfaf14e): flat stdin, status=all on page,
# PASS shape/scope validation, FAIL-reason preservation, envelope honesty.
# ---------------------------------------------------------------------------


def test_snapshot_pass_never_sends_nested_tool_envelope(client, helper_bin):
    """RED proof for the original bridge bug: the released helper rejects
    the nested {"tool": ..., "args": ...} stdin envelope. The bridge must
    send ONLY the flat args object; no key the real helper would reject."""
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200, r.text
    inv = _invocations(helper_bin.record_path)[0]
    forbidden = {"tool", "args", "cmd", "name", "method"}
    assert not (forbidden & set(inv["stdin"])), inv["stdin"]


def test_page_accepts_status_all(client, helper_bin):
    """GET /evidence/page with status=all must reach the helper (the MCP
    accepts it) instead of 422ing FastAPI validation."""
    helper_bin("pass")
    r = _get(client, "/evidence/page?board=evo-alpha&resource=cards&status=all&limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["stdin"]["status"] == "all"


def test_page_invalid_status_still_rejected(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/page?board=evo-alpha&resource=cards&status=bogus&limit=5")
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


def test_pass_missing_required_data_shape_is_unknown(client, helper_bin):
    helper_bin("noshape")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "missing data fields" in body["reason"]


def test_pass_wrong_board_scope_is_unknown(client, helper_bin):
    helper_bin("wrongscope")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "does not match requested board" in body["reason"]


def test_pass_wrong_card_scope_is_unknown(client, helper_bin):
    helper_bin("wrongscope")
    r = _get(client, "/evidence/card?board=evo-alpha&card=t_deadbeef")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "does not match requested card" in body["reason"]


# ---------------------------------------------------------------------------
# Typed malformed-boundary controls (t_a8f1ecec): a PASS receipt whose
# consumed field carries the wrong TYPE is UNKNOWN, never coerced to green.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "params", "bad_field"),
    [
        (
            "/evidence/snapshot",
            {"board": "evo-alpha", "status": "all", "card_limit": 50},
            "cards",
        ),
        (
            "/evidence/page",
            {"board": "evo-alpha", "resource": "cards", "limit": 50},
            "items",
        ),
        (
            "/evidence/worker",
            {"board": "evo-alpha", "card": "t_deadbeef"},
            "observations",
        ),
        (
            "/evidence/card",
            {"board": "evo-alpha", "card": "t_deadbeef"},
            "runs",
        ),
    ],
)
def test_wrong_consumed_type_is_unknown(client, helper_bin, route, params, bad_field):
    """The four parent malformed-boundary examples (cards:string,
    page items:string/returned:bool/has_more:string, worker observations:dict,
    card runs:null) must each collapse to UNKNOWN — a wrong type is never
    coerced to a usable value."""
    helper_bin("badtypes")
    r = client.get("/api/plugins/kanban" + route, params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN", body
    assert body["evidence"] is None
    assert bad_field in body["reason"], body["reason"]


def test_page_returned_mismatching_items_length_is_unknown(client, helper_bin):
    """returned must be a non-negative integer consistent with len(items)."""
    helper_bin("badreturned")
    r = _get(client, "/evidence/page?board=evo-alpha&resource=cards&limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN", body
    assert "returned" in body["reason"]


def test_nonfinite_top_observed_at_is_unknown(client, helper_bin):
    """NaN at the top-level observed_at must be rejected before FastAPI
    serialises a non-standard NaN token."""
    helper_bin("nonfinite_top")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN", body
    assert body["evidence"] is None
    assert "observed_at" in body["reason"]


def test_nonfinite_snapshot_observed_at_is_unknown(client, helper_bin):
    """+Infinity in snapshot data.observed_at must be rejected the same way."""
    helper_bin("nonfinite_snapshot")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN", body
    assert body["evidence"] is None
    assert "observed_at" in body["reason"]


def test_correct_typed_shapes_still_pass(client, helper_bin):
    """Correct-shaped positives stay green: the type checks must not reject
    the released helper's real data (list cards/items, dict counts, int
    returned == len(items), bool has_more, finite float observed_at)."""
    helper_bin("pass")
    for path in (
        "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50",
        "/evidence/page?board=evo-alpha&resource=cards&limit=50",
        "/evidence/worker?board=evo-alpha&card=t_deadbeef",
        "/evidence/card?board=evo-alpha&card=t_deadbeef",
    ):
        r = _get(client, path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["state"] == "PASS", (path, body)
    # Snapshot counts and observed_at are the specific fields the type checks
    # consume: assert they are forwarded untouched, not coerced.
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    body = r.json()
    assert isinstance(body["evidence"]["counts"], dict)
    assert isinstance(body["evidence"]["observed_at"], float)
    assert isinstance(body["observed_at"], float)


def test_nonzero_fail_preserves_helper_reason(client, helper_bin):
    """The real helper exits 1 on ordinary FAILs (missing board, missing
    card). The reason must survive so 'board absent' stays distinguishable
    from 'helper unavailable'."""
    helper_bin("fail")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "FAIL"
    assert body["reason"].startswith("remote board denied read")
    assert "exited 1" in body["reason"]
    assert body["helper"]["execution_host"] == "unverified"


def test_env_stripped_and_stderr_never_in_reason(client, helper_bin, monkeypatch):
    helper_bin("fail")
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "prod-board")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    inv = _invocations(helper_bin.record_path)[0]
    assert inv["env_write_boards"] == ""
    # No raw child stderr leaks into the response.
    assert "helper exploded" not in json.dumps(body)


def test_snapshot_envelope_preserves_freshness_fields(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "PASS"
    assert isinstance(body["observed_at"], float)
    assert body["limitations"] == {"events": 0}
    # Completeness/interval fields stay inside evidence.
    assert body["evidence"]["cards"] is not None
    # Execution host is forwarded from a validated PASS receipt only.
    assert body["helper"]["execution_host"] == "evo"
    assert body["request"]["board"] == "evo-alpha"  # request echo


def test_helper_stdout_bound_is_read_bound_not_write_cap(client, helper_bin):
    """Over-cap stdout → UNKNOWN even though the child completed; the cap
    is enforced on the read-back, documented as a read/parse bound."""
    helper_bin("oversized")
    r = _get(client, "/evidence/snapshot?board=evo-alpha&status=all&card_limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "exceeded" in body["reason"]


# ---------------------------------------------------------------------------
# /evidence/context — identity alignment (is this server's DB the EVO DB?)
# ---------------------------------------------------------------------------


@pytest.fixture
def evo_aligned(evidence_home, monkeypatch):
    """Hostname ``evo`` + a real ``~/.hermes/kanban.db`` at the OS account
    home, so the server's local DB for the default board IS the EVO DB.

    ``evidence_home`` sets ``HERMES_HOME=<tmp>/.hermes`` and monkeypatches
    ``Path.home`` to ``<tmp>``; with ``HERMES_KANBAN_DB``/``_HOME`` unset,
    ``kanban_db.kanban_db_path("default")`` lands on ``<tmp>/.hermes/kanban.db``
    — the same file the expected EVO path names."""
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "evo")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    db = Path.home() / ".hermes" / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    return db


def test_context_aligned(client, evo_aligned):
    r = _get(client, "/evidence/context?board=default")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aligned"] is True
    assert body["board"] == "default"
    assert body["hostname"] == "evo"
    assert body["reason"] == "aligned"
    # Read-only identity: never surface raw filesystem paths to the user.
    assert ".hermes" not in json.dumps(body)


def test_context_blank_board_resolves_current(client, evo_aligned, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    r = _get(client, "/evidence/context")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aligned"] is True
    assert body["board"] == "default"


def test_context_wrong_hostname_not_aligned(client, evo_aligned, monkeypatch):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "archie")
    r = _get(client, "/evidence/context?board=default")
    assert r.status_code == 200
    body = r.json()
    assert body["aligned"] is False
    assert "not the EVO host" in body["reason"]


def test_context_db_override_diverges_not_aligned(client, evo_aligned, monkeypatch):
    # HERMES_KANBAN_DB points the local server at a DIFFERENT file than the
    # OS-account home EVO path — hostname evo alone is not sufficient.
    elsewhere = Path.home() / "elsewhere.db"
    elsewhere.write_bytes(b"")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(elsewhere))
    r = _get(client, "/evidence/context?board=default")
    assert r.status_code == 200
    body = r.json()
    assert body["aligned"] is False
    assert "does not match the EVO location" in body["reason"]


def test_context_missing_db_not_aligned(client, monkeypatch, evidence_home):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "evo")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    r = _get(client, "/evidence/context?board=default")
    assert r.status_code == 200
    body = r.json()
    assert body["aligned"] is False
    assert "missing" in body["reason"]


def test_context_symlink_not_aligned(client, monkeypatch, evidence_home):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "evo")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    home = Path.home()
    real = home / ".hermes" / "real.db"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"")
    link = home / ".hermes" / "kanban.db"
    link.symlink_to(real)
    r = _get(client, "/evidence/context?board=default")
    assert r.status_code == 200
    body = r.json()
    assert body["aligned"] is False
    assert "symlink" in body["reason"]


def test_context_named_board_expected_path(client, evo_aligned):
    # A named board's EVO location is ~/.hermes/kanban/boards/<slug>/kanban.db.
    # Without that file present, alignment is False (missing), proving the
    # named-board branch resolves the nested path rather than the default one.
    r = _get(client, "/evidence/context?board=evo-alpha")
    assert r.status_code == 200
    body = r.json()
    assert body["aligned"] is False
    assert "missing" in body["reason"]


@pytest.mark.skipif(
    not Path(
        "/home/hayden/atlas/work/trajectory-20260912/ui-helper-candidate/.venv/bin/atlas-kanban-call"
    ).exists(),
    reason="real helper candidate not staged on this host",
)
class TestRealHelperCandidate:
    """Controls against the actual released helper candidate (source
    27ef127), real local EVO transport, read-only board
    relay-vault-build-20260912. Skipped unless the candidate is staged."""

    HELPER_DIR = "/home/hayden/atlas/work/trajectory-20260912/ui-helper-candidate/.venv/bin"

    @pytest.fixture
    def real_helper(self, evidence_home, monkeypatch):
        monkeypatch.setenv(
            "PATH", self.HELPER_DIR + os.pathsep + os.environ.get("PATH", "")
        )
        return self.HELPER_DIR

    def test_real_card_positive_pass(self, client, real_helper):
        r = _get(
            client,
            "/evidence/card?board=relay-vault-build-20260912&card=t_1b7a5c96",
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "PASS", body
        assert body["evidence"]["task"]["id"] == "t_1b7a5c96"
        assert isinstance(body["observed_at"], float)
        assert body["helper"]["execution_host"] == "evo"

    def test_real_missing_board_fail_preserves_reason(self, client, real_helper):
        r = _get(
            client,
            "/evidence/card?board=no-such-board-xyz&card=t_1b7a5c96",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "FAIL"
        assert "Board database is absent" in body["reason"]
        assert "exited 1" in body["reason"]

    def test_real_missing_card_fail(self, client, real_helper):
        r = _get(
            client,
            "/evidence/card?board=relay-vault-build-20260912&card=t_ffffffffffff",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "FAIL"
        assert "Card does not exist" in body["reason"]

    def test_real_page_status_all(self, client, real_helper):
        r = _get(
            client,
            "/evidence/page?board=relay-vault-build-20260912&resource=cards&status=all&limit=2",
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "PASS", body
        assert isinstance(body["evidence"]["items"], list)
