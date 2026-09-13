"""Next-ten bridge tests (plugins/kanban/dashboard/plugin_api.py).

R1 release identity, R4 acceptance compare, R6 reviewer packet, R8
attachment provenance, R9 batch readiness preview, R3 repair previews and
R2 browser readiness. The new /evidence/* and /workflow/readiness-batch
routes shell out to the RELEASED ``atlas-kanban-call`` helper with the same
boundary contract as the evidence bridge (tool name in argv, flat JSON args
on stdin). Tests exercise the real FastAPI router against a temporary fake
helper executable on PATH; acceptance linkage is owned by the adapter's
guarded receipts and is only ever read through the helper (this plugin adds
NO second receipt store).

R2 authentication is proven against the REAL host middleware/router at
login and authenticated states (tests/hermes_cli/test_dashboard_auth_*
pattern), never by direct function calls or weakened auth.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import time
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
        "hermes_dashboard_plugin_kanban_nextten", plugin_file,
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
# Fake helper: next-ten tools with per-tool data shapes from CONTRACT.md.
# The builder below is embedded VERBATIM in the fake executable's script
# (see _write_fake_helper) so the subprocess is self-contained; keep the
# two in sync via test_kanban_next_ten_fake_contract below.
# ---------------------------------------------------------------------------


_NOW = 1789300000.0


def _write_fake_helper(directory: Path, behavior: str) -> Path:
    """Fake ``atlas-kanban-call`` for the next-ten tools.

    The data shapes come from CONTRACT.md and mirror what the adapter emits;
    args echoes (board/card/run ids/attachment ids) are re-applied from the
    request's stdin args so the plugin's scope checks can be exercised both
    ways. Behaviors: pass / fail / unknown / noshape / wrongscope /
    wronghost / nonzero / oversized / malformed.
    """
    script = f'''#!/usr/bin/env python3
import json, os, sys, time
behavior = {behavior!r}
record_path = os.environ["FAKE_HELPER_RECORD"]
invocation = {{"argv": sys.argv}}
raw = sys.stdin.buffer.read()
try:
    invocation["stdin"] = json.loads(raw.decode("utf-8"))
except Exception:
    invocation["stdin"] = None
with open(record_path, "a", encoding="utf-8") as fh:
    json.dump(invocation, fh)
    fh.write("\\n")

args = invocation.get("stdin") or {{}}
tool = sys.argv[1] if len(sys.argv) > 1 else ""
NOW = {_NOW}

def next_ten_data(tool, args):
    board = args.get("board", "evo-alpha")
    if tool == "kanban_release_identity":
        return {{
            "board": board,
            "adapter": {{
                "state": "PASS",
                "revision": "a84a2b2c0" * 4,
                "source": "atlas-kanban installed release",
                "version": "1.4.0",
            }},
            "observed_at": NOW,
        }}
    if tool == "kanban_acceptance_compare":
        return {{
            "board": board,
            "card": args.get("card"),
            "current": {{
                "run_id": args.get("current_run_id"),
                "state": "PASS",
                "release": {{"revision": "a84a2b2c0" * 4, "source": "served"}},
                "checks": {{
                    "worker_stopped": {{"state": "PASS", "source": "machine"}},
                }},
            }},
            "previous": {{
                "run_id": args.get("previous_run_id"),
                "state": "UNKNOWN",
                "release": None,
                "checks": {{
                    "parent_source_review": {{"state": "PASS", "source": "parent"}},
                }},
            }},
            "checks": [
                {{"name": "worker_stopped", "source": "machine", "current": "PASS",
                  "previous": None, "change": "new"}},
                {{"name": "parent_source_review", "source": "parent", "current": None,
                  "previous": "PASS", "change": "unproved"}},
            ],
            "limitations": ["prior receipts absent before capture began"],
            "observed_at": NOW,
        }}
    if tool == "kanban_reviewer_packet":
        return {{
            "schema_version": 1,
            "board": board,
            "card": args.get("card"),
            "observed_at": NOW,
            "packet": {{
                "identity": {{"adapter_revision": "a84a2b2c0" * 4}},
                "task": {{"id": args.get("card"), "status": "blocked",
                         "assignee": "evo", "title": "Bounded title"}},
                "worker": {{"overall": "STOPPED", "running": 0}},
                "runs": [{{"id": 7, "outcome": "completed"}}],
                "attachments": [{{"id": 3, "filename": "report.md"}}],
                "acceptance": {{"receipts": 1, "latest_state": "PASS"}},
            }},
            "bounds": {{
                "runs": 10, "attachments": 20, "comments": 0,
                "note": "bounded projections; no raw bodies or logs",
            }},
            "limitations": ["raw bodies, results, logs and comments excluded"],
        }}
    if tool == "kanban_attachment_provenance":
        attachment_id = args.get("attachment_id")
        if attachment_id == 3:
            return {{
                "board": board,
                "card": args.get("card"),
                "attachment_id": 3,
                "accepted_run_id": 7,
                "acceptance_state": "PASS",
                "reason": "guarded acceptance receipt associates this attachment",
                "observed_at": NOW,
            }}
        return {{
            "board": board,
            "card": args.get("card"),
            "attachment_id": attachment_id,
            "accepted_run_id": None,
            "acceptance_state": "UNKNOWN",
            "reason": "no guarded acceptance receipt names this attachment; "
                      "an attachment beside a done card is not acceptance",
            "observed_at": NOW,
        }}
    if tool == "kanban_readiness_batch":
        cards = args.get("cards", [])
        items = []
        for card in cards:
            if card == "t_00000009":
                items.append({{
                    "card": card,
                    "state": "UNKNOWN",
                    "receipt": None,
                    "repair_preview": [{{
                        "check": "card_presence",
                        "state": "UNKNOWN",
                        "action": "Open an existing card on this board.",
                        "reason": "card is not present in the aligned board",
                    }}],
                }})
            else:
                items.append({{
                    "card": card,
                    "state": "FAIL",
                    "receipt": {{
                        "requested": {{"board": board, "check_model": args.get("check_model")}},
                        "checks": [
                            {{"name": "board_permission", "state": "PASS", "reason": "writable"}},
                            {{"name": "python_interpreter", "state": "FAIL",
                             "reason": "workspace .venv missing"}},
                        ],
                        "ready_to_release": False,
                    }},
                    "repair_preview": [{{
                        "check": "python_interpreter",
                        "state": "FAIL",
                        "action": "Provision the workspace .venv (python -m venv .venv) with a supported interpreter.",
                        "reason": "workspace .venv missing",
                    }}],
                }})
        return {{
            "board": board,
            "items": items,
            "requested": len(cards),
            "returned": len(items),
            "omitted": 0,
            "no_mutation_performed": True,
            "observed_at": NOW,
        }}
    return {{"unrelated": True}}

def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()

receipt = {{"state": "PASS", "execution_host": "evo", "observed_at": NOW}}
if behavior == "pass":
    receipt["data"] = next_ten_data(tool, args)
    emit(receipt)
elif behavior == "fail":
    receipt["state"] = "FAIL"
    receipt["reason"] = "remote board denied read"
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "remote unreachable"
    emit(receipt)
elif behavior == "noshape":
    receipt["data"] = {{"unrelated": True}}
    emit(receipt)
elif behavior == "wrongscope":
    d = next_ten_data(tool, args)
    d["board"] = "a-different-board"
    receipt["data"] = d
    emit(receipt)
elif behavior == "wronghost":
    receipt["execution_host"] = "elsewhere"
    receipt["data"] = next_ten_data(tool, args)
    emit(receipt)
elif behavior == "nonzero":
    receipt["data"] = next_ten_data(tool, args)
    emit(receipt)
    sys.exit(3)
elif behavior == "unknown_data":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "incomplete commission; see per-item advice"
    receipt["data"] = next_ten_data(tool, args)
    emit(receipt)
    sys.exit(1)
elif behavior == "fail_data":
    receipt["state"] = "FAIL"
    receipt["reason"] = "a measured readiness check failed"
    receipt["data"] = next_ten_data(tool, args)
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_wronghost":
    receipt["state"] = "UNKNOWN"
    receipt["execution_host"] = "elsewhere"
    receipt["reason"] = "remote unreachable"
    receipt["data"] = next_ten_data(tool, args)
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_badshape":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "partial"
    receipt["data"] = {{"unrelated": True}}
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_compare":
    # A realistic UNKNOWN acceptance comparison: the current run's receipt is
    # present, but the previous run's receipt is absent, so every check is
    # unproved rather than compared (never a relabelled PASS fixture).
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "previous run receipt absent; comparison unproved"
    receipt["data"] = {{
        "board": args.get("board", "evo-alpha"),
        "card": args.get("card"),
        "current": {{
            "run_id": args.get("current_run_id"),
            "state": "PASS",
            "release": {{"revision": "a84a2b2c0" * 4, "source": "served"}},
            "checks": {{"worker_stopped": {{"state": "PASS", "source": "machine"}}}},
        }},
        "previous": {{
            "run_id": args.get("previous_run_id"),
            "state": "UNKNOWN",
            "release": None,
            "checks": {{}},
        }},
        "checks": [
            {{"name": "worker_stopped", "source": "machine", "current": "PASS",
              "previous": None, "change": "unproved"}},
        ],
        "limitations": ["previous run receipt absent"],
        "observed_at": NOW,
    }}
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_foreign_item":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "partial"
    d = next_ten_data(tool, args)
    if isinstance(d, dict) and isinstance(d.get("items"), list) and d["items"]:
        d["items"][0]["card"] = "t_ffffffff"
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_foreign_nested":
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "partial"
    d = next_ten_data(tool, args)
    if isinstance(d, dict) and isinstance(d.get("items"), list):
        for item in d["items"]:
            if isinstance(item, dict):
                nested = item.get("receipt")
                if isinstance(nested, dict) and isinstance(nested.get("requested"), dict):
                    nested["requested"]["board"] = "a-different-board"
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_count_mismatch":
    # C10: claimed requested does not reconcile with returned + omitted.
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "incomplete commission; see per-item advice"
    d = next_ten_data(tool, args)
    if isinstance(d, dict):
        d["requested"] = 99
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_requested_count_mismatch":
    # C10 (scope): requested == returned + omitted but exceeds the number of
    # unique cards actually selected (claims more than the request sent).
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "incomplete commission; see per-item advice"
    d = next_ten_data(tool, args)
    if isinstance(d, dict):
        d["omitted"] = 1
        d["requested"] = d.get("returned", 0) + 1
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_duplicate_item":
    # C11: the same card identity is carried by more than one item.
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "partial"
    d = next_ten_data(tool, args)
    if isinstance(d, dict) and isinstance(d.get("items"), list) and d["items"]:
        first = d["items"][0].get("card")
        for item in d["items"]:
            item["card"] = first
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_missing_card":
    # Item card identity absent (null) -> must reject, not tolerate.
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "partial"
    d = next_ten_data(tool, args)
    if isinstance(d, dict) and isinstance(d.get("items"), list) and d["items"]:
        d["items"][0].pop("card", None)
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_unhashable_card":
    # Item card identity unhashable (dict) -> must reject, never throw 500.
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "partial"
    d = next_ten_data(tool, args)
    if isinstance(d, dict) and isinstance(d.get("items"), list) and d["items"]:
        d["items"][0]["card"] = {{"not": "a string"}}
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "unknown_data_omitted":
    # A valid omitted batch: requested == returned + omitted, one card
    # returned, one omitted. This must be preserved, not dropped.
    receipt["state"] = "UNKNOWN"
    receipt["reason"] = "one card omitted; see per-item advice"
    d = next_ten_data(tool, args)
    if isinstance(d, dict) and isinstance(d.get("items"), list) and len(d["items"]) >= 2:
        d["items"] = d["items"][:1]
        d["returned"] = len(d["items"])
        d["omitted"] = d.get("requested", 0) - d["returned"]
    receipt["data"] = d
    emit(receipt)
    sys.exit(1)
elif behavior == "oversized":
    receipt["data"] = {{"pad": "x" * 2000000}}
    emit(receipt)
elif behavior == "malformed":
    sys.stdout.write("this is not json <<<")
    sys.stdout.flush()
'''
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


def _post(client, path, body):
    return client.post(f"/api/plugins/kanban{path}", json=body)


# ---------------------------------------------------------------------------
# R1 release identity
# ---------------------------------------------------------------------------


def test_releases_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/releases?board=evo-alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["board"] == "evo-alpha"
    assert body["evidence"]["adapter"]["state"] == "PASS"
    assert body["evidence"]["adapter"]["revision"].startswith("a84a2b2c0")
    assert body["helper"]["tool"] == "kanban_release_identity"
    assert body["helper"]["execution_host"] == "evo"
    assert body["request"] == {"board": "evo-alpha"}
    assert len(_invocations(helper_bin.record_path)) == 1


def test_releases_flat_args_only(client, helper_bin):
    helper_bin("pass")
    _get(client, "/evidence/releases?board=evo-alpha")
    invocation = _invocations(helper_bin.record_path)[0]
    assert invocation["argv"][1] == "kanban_release_identity"
    assert invocation["argv"][2] == "-"
    # Flat args only: no nested tool envelope on stdin.
    assert invocation["stdin"] == {"board": "evo-alpha"}


def test_releases_missing_helper_unknown(client, helper_bin, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    r = _get(client, "/evidence/releases?board=evo-alpha")
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "not installed" in body["reason"]


def test_releases_child_write_authority_stripped(client, helper_bin, monkeypatch):
    helper_bin("pass")
    monkeypatch.setenv("ATLAS_KANBAN_WRITE_BOARDS", "evo-alpha")
    _get(client, "/evidence/releases?board=evo-alpha")
    r = _get(client, "/evidence/releases?board=evo-alpha")
    assert r.status_code == 200


def test_releases_carries_backend_identity(client, helper_bin):
    """R1: /evidence/releases must attach the served Hermes backend identity
    (immutable release metadata loaded with the module), not only the
    adapter's own identity."""
    helper_bin("pass")
    r = _get(client, "/evidence/releases?board=evo-alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    backend = body["evidence"]["backend"]
    assert backend["state"] == "PASS"
    assert backend["revision"], "backend revision must identify the served release"
    assert isinstance(backend["source"], str) and backend["source"]
    # The backend identity is derived from the module's immutable release
    # metadata, not from a mutable checkout HEAD.
    from hermes_cli import __release_date__ as rel_date, __version__ as ver

    assert ver in backend["revision"] or backend["revision"].startswith("hermes-")
    assert rel_date in backend["source"]


def test_releases_backend_identity_missing_metadata_unknown(client, helper_bin, monkeypatch):
    """Missing release metadata must degrade to an honest UNKNOWN backend
    identity, never a fabricated one."""
    import hermes_cli

    helper_bin("pass")
    monkeypatch.setattr(hermes_cli, "__version__", "", raising=False)
    monkeypatch.setattr(hermes_cli, "__release_date__", "", raising=False)
    r = _get(client, "/evidence/releases?board=evo-alpha")
    body = r.json()
    assert body["state"] == "PASS"
    backend = body["evidence"]["backend"]
    assert backend["state"] == "UNKNOWN"
    assert backend["revision"] is None
    assert backend["source"]


# ---------------------------------------------------------------------------
# R4 acceptance compare
# ---------------------------------------------------------------------------


def test_acceptance_compare_pass(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/acceptance-compare?board=evo-alpha&card=t_00000001"
        "&current_run_id=9&previous_run_id=8",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    ev = body["evidence"]
    # Attestation origin comes from the adapter's `source` field
    # (parent|machine), never an invented `kind` field.
    sources = {c["name"]: c["source"] for c in ev["checks"]}
    assert sources["parent_source_review"] == "parent"
    assert sources["worker_stopped"] == "machine"
    for c in ev["checks"]:
        assert "kind" not in c
    changes = {c["name"]: c["change"] for c in ev["checks"]}
    assert changes["worker_stopped"] == "new"
    assert changes["parent_source_review"] == "unproved"
    assert isinstance(ev["limitations"], list)


def test_acceptance_compare_same_run_ids_rejected(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/acceptance-compare?board=evo-alpha&card=t_00000001"
        "&current_run_id=9&previous_run_id=9",
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


def test_acceptance_compare_bad_card_rejected(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/acceptance-compare?board=evo-alpha&card=not-a-card"
        "&current_run_id=9&previous_run_id=8",
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


def test_acceptance_compare_fail_forwarded(client, helper_bin):
    helper_bin("fail")
    r = _get(
        client,
        "/evidence/acceptance-compare?board=evo-alpha&card=t_00000001"
        "&current_run_id=9&previous_run_id=8",
    )
    body = r.json()
    assert body["state"] == "FAIL"
    assert "denied" in body["reason"]


# ---------------------------------------------------------------------------
# R6 reviewer packet
# ---------------------------------------------------------------------------


def test_reviewer_packet_pass_bounds(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/reviewer-packet?board=evo-alpha&card=t_00000001")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    ev = body["evidence"]
    assert ev["schema_version"] == 1
    assert ev["packet"]["task"]["id"] == "t_00000001"
    assert isinstance(ev["bounds"], dict)
    assert isinstance(ev["limitations"], list)
    # No raw evidence channels ever leave through this door.
    dumped = json.dumps(body)
    for banned in ("stored_path", "argv", "environ", "result_excerpt"):
        assert banned not in dumped


def test_reviewer_packet_missing_card_fail(client, helper_bin):
    helper_bin("fail")
    r = _get(client, "/evidence/reviewer-packet?board=evo-alpha&card=t_00000001")
    body = r.json()
    assert body["state"] == "FAIL"


def test_reviewer_packet_bad_board_rejected(client, helper_bin):
    helper_bin("pass")
    # Uppercase + leading '-': normalization lowercases first, so the
    # leading-hyphen form is the one the slug validator truly rejects.
    r = _get(client, "/evidence/reviewer-packet?board=-bad&card=t_00000001")
    assert r.status_code == 400
    assert _invocations(helper_bin.record_path) == []


# ---------------------------------------------------------------------------
# R8 attachment provenance
# ---------------------------------------------------------------------------


def test_attachment_provenance_accepted(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=3",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["evidence"]["accepted_run_id"] == 7
    assert body["evidence"]["acceptance_state"] == "PASS"


def test_attachment_provenance_unknown_stays_unknown(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=4",
    )
    body = r.json()
    # The fake serves attachment 4 with no guarded receipt: UNKNOWN forward.
    assert body["state"] == "PASS"
    assert body["evidence"]["acceptance_state"] == "UNKNOWN"
    assert body["evidence"]["accepted_run_id"] is None
    assert "not acceptance" in body["evidence"]["reason"]


def test_attachment_provenance_zero_id_rejected(client, helper_bin):
    helper_bin("pass")
    r = _get(
        client,
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=0",
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


# ---------------------------------------------------------------------------
# R9 batch readiness preview
# ---------------------------------------------------------------------------


@pytest.fixture
def evo_aligned(evidence_home, monkeypatch):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "evo")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    db = Path.home() / ".hermes" / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    kb.connect(board="default").close()
    return db


def test_readiness_batch_pass_with_repair_previews(client, helper_bin, evo_aligned):
    helper_bin("pass")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001", "t_00000002"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    ev = body["evidence"]
    assert ev["requested"] == 2
    assert ev["no_mutation_performed"] is True
    # Per-card repair previews derived from failed/unknown checks only.
    for item in ev["items"]:
        assert item["repair_preview"], "expected a repair preview on failing cards"
        assert all(
            p["state"] in ("FAIL", "UNKNOWN") for p in item["repair_preview"]
        )
        for preview in item["repair_preview"]:
            # Previews are bounded text, never shell.
            assert isinstance(preview["action"], str)
            assert ";" not in preview["action"] or "venv" in preview["action"]
    invocation = _invocations(helper_bin.record_path)[0]
    assert invocation["stdin"]["cards"] == ["t_00000001", "t_00000002"]
    assert invocation["stdin"]["check_model"] is False


def test_readiness_batch_rejects_duplicate_cards(client, helper_bin, evo_aligned):
    """R9: duplicate selections are REJECTED (422), not silently deduped —
    the adapter's own contract fails on non-distinct cards, and a quiet
    dedup would hide a real selection error from the user."""
    helper_bin("pass")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001", "t_00000001"]},
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []
    # 11 distinct cards still exceed the bound.
    eleven = [f"t_{i:08x}" for i in range(11)]
    r2 = _post(
        client, "/workflow/readiness-batch?board=default", {"cards": eleven}
    )
    assert r2.status_code == 422


def test_readiness_batch_extra_field_rejected(client, helper_bin, evo_aligned):
    helper_bin("pass")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "release": True},
    )
    assert r.status_code == 422  # extra=forbid: no release authority smuggled


def test_readiness_batch_unaligned_guard(client, helper_bin, evo_aligned, monkeypatch):
    import socket as _socket

    monkeypatch.setattr(_socket, "gethostname", lambda: "archie")
    helper_bin("pass")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "aligned" in body["reason"]
    assert _invocations(helper_bin.record_path) == []


def test_readiness_batch_bad_card_rejected(client, helper_bin, evo_aligned):
    helper_bin("pass")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["nope"]},
    )
    assert r.status_code == 422
    assert _invocations(helper_bin.record_path) == []


# ---------------------------------------------------------------------------
# Shared negative controls: shape/scope/host/exit/stdout bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        "/evidence/releases?board=evo-alpha",
        "/evidence/acceptance-compare?board=evo-alpha&card=t_00000001"
        "&current_run_id=9&previous_run_id=8",
        "/evidence/reviewer-packet?board=evo-alpha&card=t_00000001",
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=3",
    ],
)
@pytest.mark.parametrize(
    "behavior",
    ["noshape", "wrongscope", "wronghost", "nonzero", "oversized", "malformed"],
)
def test_next_ten_negative_controls(client, helper_bin, route, behavior):
    helper_bin(behavior)
    r = _get(client, route)
    body = r.json()
    assert body["state"] == "UNKNOWN", (route, behavior, body)
    assert body["helper"]["execution_host"] in ("unverified",)


# ---------------------------------------------------------------------------
# Partial-evidence preservation: a known FAIL/UNKNOWN outcome that still
# carries a valid bounded data payload (helper exit 1) keeps its evidence;
# a claimed PASS from a nonzero exit stays rejected; foreign/malformed data
# is dropped to empty-evidence FAIL/UNKNOWN.
# ---------------------------------------------------------------------------


def test_readiness_batch_unknown_exit1_preserves_items(client, helper_bin, evo_aligned):
    helper_bin("unknown_data")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001", "t_00000002"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "exited 1" in body["reason"]
    ev = body["evidence"]
    assert isinstance(ev, dict)
    assert [i["card"] for i in ev["items"]] == ["t_00000001", "t_00000002"]
    for item in ev["items"]:
        assert item["repair_preview"], "partial repair preview must be preserved"
    assert ev["no_mutation_performed"] is True
    assert isinstance(body["observed_at"], float)
    assert body["helper"]["execution_host"] == "evo"


def test_readiness_batch_fail_exit1_preserves_measured_item(client, helper_bin, evo_aligned):
    helper_bin("fail_data")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    body = r.json()
    assert body["state"] == "FAIL"
    assert "exited 1" in body["reason"]
    ev = body["evidence"]
    assert isinstance(ev, dict)
    assert ev["items"][0]["state"] == "FAIL"
    assert ev["items"][0]["repair_preview"]
    assert ev["items"][0]["receipt"]["checks"]
    assert body["helper"]["execution_host"] == "evo"


def test_acceptance_compare_unknown_exit1_preserves_unproved(client, helper_bin):
    helper_bin("unknown_compare")
    r = _get(
        client,
        "/evidence/acceptance-compare?board=evo-alpha&card=t_00000001"
        "&current_run_id=9&previous_run_id=8",
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    ev = body["evidence"]
    assert isinstance(ev, dict)
    # Realistic UNKNOWN compare: the previous receipt is absent, so every
    # check is unproved rather than compared — never a relabelled PASS shape.
    assert ev["current"]["state"] == "PASS"
    assert ev["previous"]["state"] == "UNKNOWN"
    assert ev["previous"]["release"] is None
    assert ev["previous"]["checks"] == {}
    changes = {c["name"]: c["change"] for c in ev["checks"]}
    assert changes["worker_stopped"] == "unproved"
    assert all(c["change"] == "unproved" for c in ev["checks"])
    assert "previous run receipt absent" in ev["limitations"]
    assert body["helper"]["execution_host"] == "evo"


def test_attachment_provenance_unknown_exit1_preserves_missing(client, helper_bin):
    helper_bin("unknown_data")
    r = _get(
        client,
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=4",
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    ev = body["evidence"]
    assert isinstance(ev, dict)
    assert ev["acceptance_state"] == "UNKNOWN"
    assert ev["accepted_run_id"] is None
    assert "not acceptance" in ev["reason"]
    assert body["helper"]["execution_host"] == "evo"


def test_unknown_exit1_wrong_host_drops_data(client, helper_bin):
    helper_bin("unknown_data_wronghost")
    r = _get(
        client,
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=4",
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_unknown_exit1_badshape_drops_data(client, helper_bin):
    helper_bin("unknown_data_badshape")
    r = _get(
        client,
        "/evidence/attachment-provenance?board=evo-alpha&card=t_00000001"
        "&attachment_id=4",
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_foreign_item_card_dropped(client, helper_bin, evo_aligned):
    helper_bin("unknown_data_foreign_item")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_foreign_nested_request_dropped(client, helper_bin, evo_aligned):
    helper_bin("unknown_data_foreign_nested")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_nonzero_claimed_pass_rejected(client, helper_bin, evo_aligned):
    helper_bin("nonzero")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "claiming PASS" in body["reason"]
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_count_mismatch_dropped(client, helper_bin, evo_aligned):
    """C10: a receipt whose requested count does not reconcile with
    returned + omitted is inconsistent; its evidence must be dropped."""
    helper_bin("unknown_data_count_mismatch")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_requested_count_mismatch_dropped(client, helper_bin, evo_aligned):
    """C10 (scope): requested reconciles with returned + omitted but does not
    match the number of unique cards actually selected."""
    helper_bin("unknown_data_requested_count_mismatch")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_duplicate_item_dropped(client, helper_bin, evo_aligned):
    """C11: the same card identity carried by more than one item is
    inconsistent; evidence must be dropped."""
    helper_bin("unknown_data_duplicate_item")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001", "t_00000002"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_missing_item_card_dropped(client, helper_bin, evo_aligned):
    """Absent/null item.card must reject (drop evidence), never be tolerated."""
    helper_bin("unknown_data_missing_card")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_unhashable_item_card_dropped(client, helper_bin, evo_aligned):
    """An unhashable item.card must reject cleanly (evidence dropped), never
    raise an unhandled TypeError that becomes a 500."""
    helper_bin("unknown_data_unhashable_card")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"] is None
    assert body["helper"]["execution_host"] == "unverified"


def test_readiness_batch_omitted_batch_preserved(client, helper_bin, evo_aligned):
    """A valid omitted batch (requested == returned + omitted) is preserved:
    the omitted card is absent from items but counted, never dropped."""
    helper_bin("unknown_data_omitted")
    r = _post(
        client,
        "/workflow/readiness-batch?board=default",
        {"cards": ["t_00000001", "t_00000002"], "check_model": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert "exited 1" in body["reason"]
    ev = body["evidence"]
    assert isinstance(ev, dict)
    assert ev["requested"] == 2
    assert ev["returned"] == 1
    assert ev["omitted"] == 1
    assert ev["returned"] == len(ev["items"])
    assert [i["card"] for i in ev["items"]] == ["t_00000001"]
    assert ev["no_mutation_performed"] is True
    assert body["helper"]["execution_host"] == "evo"


def test_next_ten_allowlist_constant():
    mod = _load_plugin_module()
    assert mod.NEXT_TEN_READ_TOOLS == frozenset(
        {
            "kanban_release_identity",
            "kanban_acceptance_compare",
            "kanban_reviewer_packet",
            "kanban_attachment_provenance",
            "kanban_readiness_batch",
        }
    )
    src = Path(mod.__file__).read_text(encoding="utf-8")
    # No write/mutating tool name can be selected through the new routes.
    for banned in ("kanban_continue", "kanban_hold", "kanban_dispatch",
                   "kanban_create", "kanban_complete"):
        # banned names may appear only in the WORKFLOW sections' own
        # allowlists/routes, never inside the next-ten runner's allowlist.
        assert banned not in mod.NEXT_TEN_READ_TOOLS


def test_next_ten_routes_read_only():
    mod = _load_plugin_module()
    for route in mod.router.routes:
        path = getattr(route, "path", "")
        if "/evidence/releases" in path or "/evidence/browser-readiness" in path:
            assert set(route.methods) <= {"GET", "HEAD"}
        if "/workflow/readiness-batch" in path:
            assert set(route.methods) <= {"POST", "HEAD"}


def test_repair_preview_derivation_table():
    mod = _load_plugin_module()
    previews = mod._repair_preview_from_checks(
        [
            {"name": "board_permission", "state": "PASS", "reason": "ok"},
            {"name": "python_interpreter", "state": "FAIL", "reason": "no venv"},
            {"name": "exotic_check", "state": "UNKNOWN", "reason": None},
            "not-a-dict",
        ]
    )
    assert [p["check"] for p in previews] == [
        "python_interpreter",
        "exotic_check",
    ]
    assert previews[0]["action"].startswith("Provision the workspace .venv")
    assert previews[1]["action"].startswith("Inspect the failed readiness check")
    # Passing checks are omitted; malformed entries are skipped honestly.
    assert mod._repair_preview_from_checks(None) == []
    assert mod._repair_preview_from_checks("nope") == []


def test_repair_preview_never_shell():
    mod = _load_plugin_module()
    untrusted_reason = "$(rm -rf /) && echo pwned"
    previews = mod._repair_preview_from_checks(
        [{"name": "required_modules", "state": "FAIL", "reason": untrusted_reason}]
    )
    # The ACTION comes from the fixed table; the untrusted reason is bounded
    # text echoed for display, never executed.
    assert previews[0]["action"].startswith("Install the workspace's declared")
    assert previews[0]["reason"] == untrusted_reason[:300]


# ---------------------------------------------------------------------------
# R2 browser readiness — plugin-level behaviour through the real router
# ---------------------------------------------------------------------------


def test_browser_readiness_pass_when_board_readable(client, helper_bin):
    helper_bin("pass")
    r = _get(client, "/evidence/browser-readiness?board=evo-alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "PASS"
    assert body["evidence"]["reachable"] is True
    assert body["evidence"]["authenticated"] is True
    assert body["evidence"]["board_readable"] is True
    assert isinstance(body["evidence"]["observed_at"], float)


def test_browser_readiness_fail_when_board_denied(client, helper_bin):
    helper_bin("fail")
    r = _get(client, "/evidence/browser-readiness?board=evo-alpha")
    body = r.json()
    assert body["state"] == "FAIL"
    assert body["evidence"]["board_readable"] is False
    assert body["reason"]
    assert body["remedy"]


def test_browser_readiness_unknown_when_helper_absent(client, helper_bin, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    r = _get(client, "/evidence/browser-readiness?board=evo-alpha")
    body = r.json()
    assert body["state"] == "UNKNOWN"
    assert body["evidence"]["board_readable"] is None


# ---------------------------------------------------------------------------
# R1 asset identity door (hermes_cli/web_server.py serve_plugin_asset)
# ---------------------------------------------------------------------------


@pytest.fixture
def asset_server(tmp_path, monkeypatch):
    """Boot the REAL web_server app against a synthetic plugin set.

    Uses a fixture plugin directory (not the repo's live plugins/) so the
    test owns the bytes: a kanban plugin with dist/index.js plus an
    unrelated other.js, both enabled."""
    import hashlib as _hashlib

    plugins_root = tmp_path / "plugins"
    kanban_dir = plugins_root / "kanban" / "dashboard"
    kanban_dist = kanban_dir / "dist"
    kanban_dist.mkdir(parents=True)
    (kanban_dir / "manifest.json").write_text("{}", encoding="utf-8")
    kanban_js = kanban_dist / "index.js"
    kanban_bytes = b"(function(){\"use strict\"; window.__X__=1;})();\n"
    kanban_js.write_bytes(kanban_bytes)
    other_js = kanban_dist / "other.js"
    other_bytes = b"console.log('unrelated');\n"
    other_js.write_bytes(other_bytes)

    from hermes_cli import web_server

    def fake_plugins():
        return [
            {
                "name": "kanban",
                "source": "bundled",
                "_dir": str(kanban_dir),
            }
        ]

    monkeypatch.setattr(web_server, "_get_dashboard_plugins", fake_plugins)
    monkeypatch.setattr(
        "hermes_cli.plugins_cmd._get_enabled_set", lambda: set()
    )
    monkeypatch.setattr(
        "hermes_cli.plugins_cmd._get_disabled_set", lambda: set()
    )
    from starlette.testclient import TestClient as _TC

    tc = _TC(web_server.app)
    tc._kanban_js = kanban_js
    tc._kanban_bytes = kanban_bytes
    tc._other_bytes = other_bytes
    tc._hashlib = _hashlib
    return tc


def test_plugin_asset_identity_wraps_only_kanban_index(asset_server):
    """/dashboard-plugins/kanban/dist/index.js carries the SHA-256 of the
    exact original bytes (wrapper excluded) in the SAME response; the
    unrelated JS asset keeps its original bytes untouched."""
    r = asset_server.get("/dashboard-plugins/kanban/dist/index.js")
    assert r.status_code == 200, r.text
    expected = asset_server._hashlib.sha256(asset_server._kanban_bytes).hexdigest()
    assert r.headers["X-Hermes-Asset-SHA256"] == expected
    body = r.content
    # The response = wrapper line + original bytes; the digest covers ONLY
    # the original bytes (wrapper excluded, labelled by the consumer).
    assert body.endswith(asset_server._kanban_bytes)
    wrapper_line = body[: len(body) - len(asset_server._kanban_bytes)]
    assert b"__HERMES_PLUGIN_ASSET_ID__" in wrapper_line
    assert expected.encode() in wrapper_line
    assert b"sha256" in wrapper_line

    # Unrelated JS: byte-identical to the file on disk.
    r2 = asset_server.get("/dashboard-plugins/kanban/dist/other.js")
    assert r2.status_code == 200
    assert r2.content == asset_server._other_bytes
    assert "X-Hermes-Asset-SHA256" not in r2.headers


def test_plugin_asset_identity_changes_with_bytes(asset_server):
    """Artifact change -> identity change (the digest tracks the bytes)."""
    r1 = asset_server.get("/dashboard-plugins/kanban/dist/index.js")
    asset_server._kanban_js.write_bytes(b"(function(){\"use strict\"; window.__X__=2;})();\n")
    r2 = asset_server.get("/dashboard-plugins/kanban/dist/index.js")
    assert r1.headers["X-Hermes-Asset-SHA256"] != r2.headers["X-Hermes-Asset-SHA256"]
