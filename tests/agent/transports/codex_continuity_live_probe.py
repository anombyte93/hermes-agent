#!/usr/bin/env python3
"""LIVE proof: durable Codex thread continuity through the real Hermes stack.

Not a pytest test — it spawns a REAL `codex app-server` subprocess (twice) and
costs real model turns, so it is run deliberately:

    PYTHONPATH=<worktree> python3 tests/agent/transports/codex_continuity_live_probe.py

What it proves, using the production classes (CodexAppServerSession +
CodexThreadContinuity + the real SessionDB), not fakes:

  P1  process 1: a fresh Hermes session starts a thread and is told a nonce.
  P2  process 2: a NEW session object for the SAME Hermes session id resumes
      the EXACT saved thread id and can repeat the nonce.
  N1  a DIFFERENT Hermes session gets a fresh thread and cannot repeat it.
  N2  a fabricated thread id is refused explicitly (no silent thread/start).

Safety: temporary scoped state only (its own temp state.db and its own temp
cwd), bounded turn timeouts, and it closes every subprocess it opens. It never
touches CODEX_HOME/HOME and never resumes any pre-existing thread other than
the one it started itself in this run.

Writes machine-readable JSON to the path given by --out (default
deliverables/evidence/live-probe.json). Every step records what actually
happened; a failure is recorded as a failure, never smoothed over.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid

from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.codex_thread_continuity import (
    CodexThreadContinuity,
    CodexThreadContinuityError,
    CodexThreadRecord,
    SessionModelConfigThreadStore,
    THREAD_RECORD_KEY,
)
from hermes_state import SessionDB

TURN_TIMEOUT = 180.0


def continuity_for(db, session_id, cwd):
    return CodexThreadContinuity(
        SessionModelConfigThreadStore(db, session_id),
        cwd=cwd,
        permission_profile="read-only",
        model="",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="deliverables/evidence/live-probe.json")
    args = ap.parse_args()

    report = {
        "started_at": time.time(),
        "codex_bin": shutil.which("codex"),
        "steps": {},
        "verdict": "UNKNOWN",
    }
    if not report["codex_bin"]:
        report["verdict"] = "SKIPPED_NO_CODEX"
        _write(args.out, report)
        print("SKIPPED: codex not on PATH")
        return 2

    nonce = "NONCE-" + uuid.uuid4().hex[:12].upper()
    fabricated = str(uuid.uuid4())
    tmp = tempfile.mkdtemp(prefix="codex-continuity-probe-")
    cwd = os.path.join(tmp, "workspace")
    os.makedirs(cwd, exist_ok=True)
    db = SessionDB(db_path=__import__("pathlib").Path(tmp) / "state.db")
    sid = "probe-" + uuid.uuid4().hex[:8]
    sid_other = "probe-other-" + uuid.uuid4().hex[:8]
    db.create_session(sid, source="cli")
    db.create_session(sid_other, source="cli")
    report["nonce"] = nonce
    report["session_id"] = sid
    report["tmp"] = tmp

    ok = True

    # --- P1: process-1 equivalent, fresh thread, tell it the nonce ---------
    s1 = CodexAppServerSession(cwd=cwd, continuity=continuity_for(db, sid, cwd))
    try:
        thread_a = s1.ensure_started()
        r1 = s1.run_turn(
            user_input=(
                "Remember this token for later in this conversation: "
                f"{nonce}. Reply with only the word STORED."
            ),
            turn_timeout=TURN_TIMEOUT,
        )
        report["steps"]["p1_start_and_store"] = {
            "thread_id": thread_a,
            "resumed": s1.resumed,
            "error": r1.error,
            "final_text": (r1.final_text or "")[:400],
        }
        ok = ok and thread_a and not r1.error
    finally:
        s1.close()

    persisted = CodexThreadRecord.from_dict(
        db.get_session_model_config_value(sid, THREAD_RECORD_KEY)
    )
    report["steps"]["persisted_record"] = (
        persisted.to_dict() if persisted else None
    )
    ok = ok and persisted is not None and persisted.thread_id == thread_a

    # --- P2: NEW session object, same Hermes session → exact resume --------
    s2 = CodexAppServerSession(cwd=cwd, continuity=continuity_for(db, sid, cwd))
    try:
        resumed_id = s2.ensure_started()
        r2 = s2.run_turn(
            user_input=(
                "Repeat the exact token I asked you to remember earlier in "
                "this conversation. Reply with only that token."
            ),
            turn_timeout=TURN_TIMEOUT,
        )
        recalled = nonce in (r2.final_text or "")
        report["steps"]["p2_restart_recall"] = {
            "thread_id": resumed_id,
            "same_thread": resumed_id == thread_a,
            "resumed_flag": s2.resumed,
            "nonce_recalled": recalled,
            "error": r2.error,
            "final_text": (r2.final_text or "")[:400],
        }
        ok = ok and resumed_id == thread_a and s2.resumed and recalled
    finally:
        s2.close()

    # --- N1: a separate Hermes session must NOT know the nonce -------------
    s3 = CodexAppServerSession(
        cwd=cwd, continuity=continuity_for(db, sid_other, cwd)
    )
    try:
        other_id = s3.ensure_started()
        r3 = s3.run_turn(
            user_input=(
                "If you have previously been told a token starting with "
                "NONCE- in this conversation, repeat it exactly. Otherwise "
                "reply exactly: NO_TOKEN_KNOWN"
            ),
            turn_timeout=TURN_TIMEOUT,
        )
        leaked = nonce in (r3.final_text or "")
        report["steps"]["n1_separate_session"] = {
            "thread_id": other_id,
            "distinct_thread": other_id != thread_a,
            "resumed_flag": s3.resumed,
            "nonce_leaked": leaked,
            "final_text": (r3.final_text or "")[:400],
        }
        ok = ok and other_id != thread_a and not s3.resumed and not leaked
    finally:
        s3.close()

    # --- N2: fabricated id must be refused, never silently restarted -------
    db.patch_session_model_config(sid, {THREAD_RECORD_KEY: CodexThreadRecord(
        thread_id=fabricated, cwd=cwd, permission_profile="read-only",
        version=1,
    ).to_dict()})
    s4 = CodexAppServerSession(cwd=cwd, continuity=continuity_for(db, sid, cwd))
    refusal = None
    try:
        s4.ensure_started()
    except CodexThreadContinuityError as exc:
        refusal = f"{type(exc).__name__}: {exc}"
    finally:
        s4.close()
    report["steps"]["n2_fabricated_id"] = {
        "fabricated_thread_id": fabricated,
        "refused": refusal is not None,
        "refusal": refusal,
    }
    ok = ok and refusal is not None

    report["verdict"] = "PASS" if ok else "FAIL"
    report["ended_at"] = time.time()
    _write(args.out, report)
    print(f"{report['verdict']}: wrote {args.out}")
    return 0 if ok else 1


def _write(path, report):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)


if __name__ == "__main__":
    sys.exit(main())
